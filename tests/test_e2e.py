"""בדיקה מקצה לקצה: שרת uvicorn אמיתי, מסך, שלט וצופים בדפדפנים נפרדים.
הרצה: python3 -m pytest tests/test_e2e.py   (צילומי מסך נשמרים ב־tests/screenshots/)"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "tests" / "screenshots"
KEY = "e2e-key"
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PRESENTER_KEY": KEY}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/healthz")
            break
        except OSError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def test_full_show(base_url, browser):
    SHOTS.mkdir(exist_ok=True)
    errors = []

    def device(name, path, viewport=None):
        ctx = browser.new_context(viewport=viewport or {"width": 1280, "height": 800})
        ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort())
        ctx.route("**/fonts.gstatic.com/**", lambda r: r.abort())
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(f"{name}: {e}"))
        pg.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
              and errors.append(f"{name} console: {m.text}"))
        pg.goto(base_url + path)
        return pg

    def wait(pg, js, timeout=5000):
        pg.wait_for_function(js, timeout=timeout)

    # --- המציג: מסך ושלט, התפקיד והמפתח מה־URL
    screen = device("screen", f"/?role=screen&key={KEY}")
    wait(screen, "mode === 'screen' && connected")
    assert "key" not in screen.url, "המפתח לא אמור להישאר בשורת הכתובת"
    assert screen.is_visible("#qrbox")
    assert screen.evaluate("document.querySelector('#qrbox img').naturalWidth") > 0
    screen.screenshot(path=SHOTS / "01_screen_cover.png")

    remote = device("remote", f"/?role=remote&key={KEY}", PHONE)
    wait(remote, "mode === 'remote' && connected")

    # --- צופים: בלי מפתח, ישר למצב צופה
    v1 = device("v1", "/", PHONE)
    v2 = device("v2", "/", PHONE)
    for v in (v1, v2):
        wait(v, "mode === 'viewer' && connected")
        assert not v.is_visible("#picker")
    wait(remote, "viewerCount() === 2")

    # צופה לא יכול לשדר state
    assert v1.evaluate("window.claude.use('room').then(r => r.emit('state', {seq: 999, rid: 'x', step: 5})).then(() => 'ok', e => e.code)") == "not_permitted"

    # --- עד הסקר הראשון
    p1 = remote.evaluate("POLL_IDX.p1")
    for _ in range(p1 + 1):
        remote.click("#rNext")
    for pg in (screen, v1, v2):
        wait(pg, f"state.step === {p1 + 1}")

    v1.click("#poll-p1 .opt[data-i='0']")
    v2.click("#poll-p1 .opt[data-i='0']")
    v2.click("#poll-p1 .opt[data-i='1']")      # שינוי קול
    remote.click("#rTally .pm[data-i='1'][data-d='1']")
    remote.click("#rTally .pm[data-i='1'][data-d='1']")
    for pg in (screen, remote, v1):
        wait(pg, "JSON.stringify(tally('p1').total) === '[1,3,0]'")

    # רענון של טלפון לא מכפיל ולא מוחק קול
    v2.reload()
    wait(v2, "mode === 'viewer' && connected")
    screen.wait_for_timeout(500)
    assert screen.evaluate("tally('p1').total") == [1, 3, 0]
    screen.screenshot(path=SHOTS / "02_screen_poll.png")
    remote.screenshot(path=SHOTS / "03_remote_poll.png")
    v1.screenshot(path=SHOTS / "04_viewer_poll.png")

    # --- "הבא" סוגר ומקפיא. הצבעה מאוחרת נדחית.
    remote.click("#rNext")
    wait(screen, "state.frozen.p1 !== undefined")
    assert screen.evaluate("state.frozen.p1") == [1, 1, 0]
    assert "נסגרה" in screen.inner_text("#hint-p1")
    v1.evaluate("window.claude.use('room').then(r => r.presence({role: 'viewer', vote: {p: 'p1', o: 2}}))")
    screen.wait_for_timeout(500)
    assert screen.evaluate("tally('p1').total") == [1, 3, 0]

    # --- מצטרף באיחור מקבל מיד את המיקום
    late = device("late", "/", PHONE)
    wait(late, "state.step === " + str(remote.evaluate("state.step")))

    # --- המקלדת במסך מזיזה גם את השלט
    before = remote.evaluate("state.step")
    screen.keyboard.press("Space")
    wait(remote, f"state.step === {before + 1}")

    # --- השלט שורד רענון (המפתח נשמר ללשונית, התפקיד נזכר)
    remote.reload()
    wait(remote, f"mode === 'remote' && connected && state.step === {before + 1}")

    # --- עד הסוף
    n = remote.evaluate("N")
    while remote.evaluate("state.step") < n:
        remote.click("#rNext")
    wait(screen, f"state.step === {n}")
    screen.wait_for_timeout(300)
    assert screen.is_visible("#lesson")
    screen.screenshot(path=SHOTS / "05_screen_closing.png")
    remote.click("#rPrev")
    remote.click("#rPrev")
    wait(screen, f"state.step === {n - 2}")
    screen.screenshot(path=SHOTS / "06_screen_lesson2.png")

    # --- איפוס
    remote.on("dialog", lambda d: d.accept())
    remote.click("#rReset")
    for pg in (screen, v1, late):
        wait(pg, "state.step === 0 && state.started === null")
    assert screen.is_visible("#cover")

    # --- תסריט להדפסה
    remote.evaluate("showPicker()")
    remote.click("#roleScript")
    assert "חשבון Claude" not in remote.inner_text("#script")
    remote.set_viewport_size({"width": 900, "height": 1200})
    remote.screenshot(path=SHOTS / "07_script.png", full_page=True)

    assert errors == []


def test_standalone_file_still_works(browser):
    """הקובץ המקורי, בלי שרת: מצגת עצמאית (הגיבוי ביום המופע)."""
    pg = browser.new_page(viewport={"width": 1280, "height": 800})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    pg.goto((ROOT / "reference" / "index.html").as_uri())
    pg.click("#roleLocal")
    for _ in range(13):
        pg.keyboard.press("ArrowRight")
    pg.wait_for_function("state.step === 13")
    pg.click("#poll-p1 .opt[data-i='2']")
    assert pg.evaluate("tally('p1').total") == [0, 0, 1]
    assert errors == []
