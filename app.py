"""חדר מצב 1982 — שרת סנכרון.

מגיש את הדף ומחזיק חדר אחד בזיכרון (מופע יחיד, בלי scaling):
- state: ה־state האחרון שהמציג שידר. כל מי שמתחבר מקבל אותו מיד.
- votes: קול אחד לכל clientId לכל סקר. רענון לא מכפיל קול, ומי שהתנתק לא מוריד קול.
- meta: מספר הפריטים ומפת הסקרים, שהמציג שולח מתוך ITEMS כדי שהתוכן יישאר במקום אחד.

שום קלט מהדפדפן לא נחשב אמין: הכול עובר בדיקה, גודל ההודעה וקצב ההודעות מוגבלים.
"""

import asyncio
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import segno
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response

log = logging.getLogger("lebanon82")

STATIC = Path(__file__).parent / "static"

MAX_MESSAGE_BYTES = 16 * 1024
MAX_CONNECTIONS = 150
MAX_VOTERS = 300
RATE_LIMIT = 20  # הודעות לשנייה לחיבור
MAX_STEP = 500
MAX_SEGMENTS = 20
MAX_POLLS = 20
MAX_OPTIONS = 10
PEERS_DEBOUNCE = 0.15
WELCOME_TIMEOUT = 10

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
ROLES = {"viewer", "screen", "remote", "local", "script"}
ADMIN_ROLES = {"screen", "remote"}


def presenter_key() -> str:
    return os.environ.get("PRESENTER_KEY", "")


def is_presenter(key: Any) -> bool:
    expected = presenter_key()
    return bool(expected) and isinstance(key, str) and hmac.compare_digest(key.encode(), expected.encode())


# ---------------------------------------------------------------- validation

def _int(v: Any, lo: int, hi: int) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _num(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _counts(v: Any, length: int) -> list[int] | None:
    if not isinstance(v, list):
        return None
    out = []
    for i in range(length):
        x = v[i] if i < len(v) else 0
        out.append(math.floor(x) if _num(x) is not None and 0 <= x < 1000 else 0)
    return out


def clean_meta(m: Any) -> dict[str, Any] | None:
    if not isinstance(m, dict):
        return None
    n = _int(m.get("n"), 1, MAX_STEP)
    polls_in = m.get("polls")
    if n is None or not isinstance(polls_in, dict) or len(polls_in) > MAX_POLLS:
        return None
    polls = {}
    for pid, p in polls_in.items():
        if not PID_RE.match(pid) or not isinstance(p, dict):
            return None
        step, opts = _int(p.get("step"), 1, n), _int(p.get("n"), 1, MAX_OPTIONS)
        if step is None or opts is None:
            return None
        polls[pid] = {"step": step, "n": opts}
    return {"n": n, "polls": polls}


def clean_state(d: Any, meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """אותו היגיון כמו clean() בדף."""
    if not isinstance(d, dict):
        return None
    seq = _int(d.get("seq"), 0, 2**53)
    rid = d.get("rid")
    step = _int(d.get("step"), 0, meta["n"] if meta else MAX_STEP)
    if seq is None or not isinstance(rid, str) or step is None:
        return None
    s: dict[str, Any] = {"seq": seq, "rid": rid[:20], "step": step, "started": _num(d.get("started")),
                         "segStart": {}, "manual": {}, "frozen": {}}
    seg = d.get("segStart")
    if isinstance(seg, dict):
        for i in range(MAX_SEGMENTS):
            v = _num(seg.get(str(i)))
            if v is not None:
                s["segStart"][str(i)] = v
    for key in ("manual", "frozen"):
        src = d.get(key)
        if not isinstance(src, dict):
            continue
        if meta:
            allowed = {pid: p["n"] for pid, p in meta["polls"].items()}
        else:
            allowed = {pid: MAX_OPTIONS for pid in list(src)[:MAX_POLLS] if isinstance(pid, str) and PID_RE.match(pid)}
        for pid, length in allowed.items():
            if pid in src:
                a = _counts(src[pid], length)
                if a is not None:
                    s[key][pid] = a
    return s


def newer(a: dict[str, Any], b: dict[str, Any] | None) -> bool:
    return b is None or a["seq"] > b["seq"] or (a["seq"] == b["seq"] and a["rid"] > b["rid"])


# ---------------------------------------------------------------- room

@dataclass(eq=False)
class Conn:
    ws: WebSocket
    client_id: str
    admin: bool
    role: str = "viewer"
    hits: list[float] = field(default_factory=list)

    def allow(self) -> bool:
        now = time.monotonic()
        self.hits = [t for t in self.hits if now - t < 1.0]
        if len(self.hits) >= RATE_LIMIT:
            return False
        self.hits.append(now)
        return True


class Room:
    def __init__(self) -> None:
        self.conns: set[Conn] = set()
        self.state: dict[str, Any] | None = None
        self.meta: dict[str, Any] | None = None
        self.votes: dict[str, dict[str, int]] = {}  # pid -> {clientId: option}
        self._peers_task: asyncio.Task | None = None

    def live_poll(self) -> str | None:
        if not self.state or not self.meta:
            return None
        return next((pid for pid, p in self.meta["polls"].items() if p["step"] == self.state["step"]), None)

    def peers(self) -> list[dict[str, Any]]:
        """בפורמט של room.peers() ב־claude.ai. vote = הקול בסקר הפתוח בלבד.
        מי שהצביע בסקר הפתוח והתנתק נשאר ברשימה כצופה, כדי שהקול שלו ייספר."""
        live = self.live_poll()
        live_votes = self.votes.get(live, {}) if live else {}
        out: dict[str, dict[str, Any]] = {}
        for c in self.conns:
            prev = out.get(c.client_id)
            role = c.role if prev is None or prev["role"] != "viewer" else "viewer"
            out[c.client_id] = {"role": role}
        for cid in live_votes:
            out.setdefault(cid, {"role": "viewer"})
        result = []
        for cid, pr in out.items():
            vote = {"p": live, "o": live_votes[cid]} if cid in live_votes and pr["role"] == "viewer" else None
            result.append({"peer": cid, "presence": {"role": pr["role"], "vote": vote}})
        return result

    async def send(self, c: Conn, msg: dict[str, Any]) -> None:
        try:
            await c.ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def broadcast(self, msg: dict[str, Any], exclude: Conn | None = None) -> None:
        await asyncio.gather(*(self.send(c, msg) for c in list(self.conns) if c is not exclude))

    def peers_changed(self) -> None:
        """מאחד שינויים קרובים לשידור אחד."""
        if self._peers_task and not self._peers_task.done():
            return

        async def later() -> None:
            await asyncio.sleep(PEERS_DEBOUNCE)
            await self.broadcast({"type": "peers", "peers": self.peers()})

        self._peers_task = asyncio.create_task(later())

    def set_state(self, s: dict[str, Any]) -> None:
        self.state = s
        if s["step"] == 0 and s["started"] is None:  # איפוס
            self.votes.clear()

    def vote(self, client_id: str, p: Any, o: Any) -> bool:
        live = self.live_poll()
        if live is None or p != live:
            return False
        o = _int(o, 0, self.meta["polls"][live]["n"] - 1)
        if o is None:
            return False
        bucket = self.votes.setdefault(live, {})
        if client_id not in bucket and len(bucket) >= MAX_VOTERS:
            return False
        bucket[client_id] = o
        return True

    def handle(self, c: Conn, msg: dict[str, Any]) -> bool:
        """מחזיר True אם השתנה משהו שצריך לשדר ב־peers."""
        t = msg.get("type")
        if t == "presence":
            role = msg.get("role")
            if role in ROLES:
                c.role = role if c.admin or role not in ADMIN_ROLES else "viewer"
            vote = msg.get("vote")
            if isinstance(vote, dict):
                self.vote(c.client_id, vote.get("p"), vote.get("o"))
            return True
        return False


room = Room()


# ---------------------------------------------------------------- http

app = FastAPI(title="חדר מצב 1982", docs_url=None, redoc_url=None, openapi_url=None)

NO_CACHE = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


if not presenter_key():
    log.warning("PRESENTER_KEY is not set: nobody can present (screen/remote are disabled)")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html", headers=NO_CACHE)


@app.get("/sync.js")
async def sync_js() -> FileResponse:
    return FileResponse(STATIC / "sync.js", media_type="text/javascript", headers=NO_CACHE)


@lru_cache(maxsize=8)
def qr_svg(url: str) -> str:
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", border=2, dark="#111b21", light="#ffffff",
                                    xmldecl=False, svgns=True, omitsize=True)  # omitsize → viewBox, כדי שיתרחב ב־<img>
    return buf.getvalue().decode()


@app.get("/qr.svg")
async def qr(request: Request) -> Response:
    """QR לכתובת הצופים, לפי הכתובת שהאתר רץ עליה בפועל (או PUBLIC_URL אם הוגדר)."""
    url = os.environ.get("PUBLIC_URL") or str(request.base_url)
    return Response(qr_svg(url), media_type="image/svg+xml", headers=NO_CACHE)


@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


# ---------------------------------------------------------------- websocket

async def receive_json(ws: WebSocket) -> dict[str, Any] | None:
    raw = await ws.receive_text()
    if len(raw) > MAX_MESSAGE_BYTES:
        return None
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    if len(room.conns) >= MAX_CONNECTIONS:
        await ws.close(code=1013, reason="room full")
        return
    try:
        hello = await asyncio.wait_for(receive_json(ws), WELCOME_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await ws.close(code=1002)
        return
    if not hello or hello.get("type") != "hello":
        await ws.close(code=1002, reason="hello required")
        return

    cid = hello.get("clientId")
    client_id = cid if isinstance(cid, str) and ID_RE.match(cid) else secrets.token_urlsafe(12)
    c = Conn(ws, client_id, admin=is_presenter(hello.get("key")))
    room.conns.add(c)
    room.handle(c, {**hello, "type": "presence"})
    await room.send(c, {"type": "welcome", "clientId": client_id, "admin": c.admin,
                        "state": room.state, "peers": room.peers()})
    room.peers_changed()

    try:
        while True:
            msg = await receive_json(ws)
            if msg is None or not c.allow():
                continue
            if msg.get("type") == "state":
                if not c.admin:
                    continue
                meta = clean_meta(msg.get("meta"))
                if meta:
                    room.meta = meta
                s = clean_state(msg.get("data"), room.meta)
                if s and newer(s, room.state):
                    live_before = room.live_poll()
                    room.set_state(s)
                    await room.broadcast({"type": "state", "data": s}, exclude=c)
                    if room.live_poll() != live_before or not room.votes:
                        room.peers_changed()
            elif room.handle(c, msg):
                room.peers_changed()
    except WebSocketDisconnect:
        pass
    finally:
        room.conns.discard(c)
        room.peers_changed()
