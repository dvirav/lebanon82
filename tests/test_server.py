"""בדיקות לשרת: הרשאות, תקינות קלט וספירת קולות.  הרצה: python3 -m pytest tests/test_server.py"""

import pytest
from fastapi.testclient import TestClient

import app as server

KEY = "test-key"
META = {"n": 10, "polls": {"p1": {"step": 3, "n": 3}, "p2": {"step": 6, "n": 2}}}


def st(seq, step, **kw):
    return {"seq": seq, "rid": "r", "step": step, "started": 1.0 if step else None,
            "segStart": {}, "manual": {}, "frozen": {}, **kw}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PRESENTER_KEY", KEY)
    monkeypatch.setattr(server, "room", server.Room())
    monkeypatch.setattr(server, "PEERS_DEBOUNCE", 0)
    with TestClient(server.app) as c:
        c.open_ws = []
        yield c
        for ws in c.open_ws:
            try:
                ws.__exit__(None, None, None)
            except Exception:
                pass


def join(c, cid, key="", role="viewer", vote=None):
    ws = c.websocket_connect("/ws").__enter__()
    c.open_ws.append(ws)
    ws.send_json({"type": "hello", "clientId": cid, "key": key, "role": role, "vote": vote})
    w = ws.receive_json()
    assert w["type"] == "welcome"
    return ws, w


def until(ws, typ, pred=lambda m: True):
    for _ in range(50):
        m = ws.receive_json()
        if m["type"] == typ and pred(m):
            return m
    raise AssertionError(f"no {typ}")


def counts(peers, pid, n):
    c = [0] * n
    for p in peers:
        v = p["presence"].get("vote")
        if p["presence"]["role"] == "viewer" and v and v["p"] == pid:
            c[v["o"]] += 1
    return c


def push(admin, s):
    admin.send_json({"type": "state", "data": s, "meta": META})


# ---------------------------------------------------------------- http

def test_http(client):
    assert client.get("/healthz").text == "ok"
    html = client.get("/").text
    assert '<script src="/sync.js"></script>' in html
    assert client.get("/sync.js").status_code == 200
    svg = client.get("/qr.svg").text
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg and "viewBox" in svg


# ---------------------------------------------------------------- auth

def test_admin_only_with_right_key(client):
    assert join(client, "a1", KEY)[1]["admin"] is True
    assert join(client, "a2", "wrong")[1]["admin"] is False
    assert join(client, "a3", "")[1]["admin"] is False


def test_no_admin_when_key_unset(client, monkeypatch):
    monkeypatch.delenv("PRESENTER_KEY")
    assert join(client, "a1", "")[1]["admin"] is False
    assert join(client, "a2", "anything")[1]["admin"] is False


def test_viewer_cannot_set_state(client):
    v, _ = join(client, "v1")
    push(v, st(5, 3))
    v2, w = join(client, "v2")
    assert w["state"] is None


def test_viewer_cannot_claim_presenter_role(client):
    v, w = join(client, "v1", role="screen")
    roles = {p["peer"]: p["presence"]["role"] for p in w["peers"]}
    assert roles["v1"] == "viewer"


# ---------------------------------------------------------------- state

def test_state_reaches_viewers_and_late_joiners(client):
    admin, _ = join(client, "adm", KEY, role="remote")
    v, _ = join(client, "v1")
    push(admin, st(1, 2))
    assert until(v, "state")["data"]["step"] == 2
    _, w = join(client, "late")
    assert w["state"]["step"] == 2


def test_older_and_invalid_state_rejected(client):
    admin, _ = join(client, "adm", KEY, role="remote")
    push(admin, st(5, 2))
    push(admin, st(4, 7))                      # ישן יותר
    push(admin, st(6, 11))                     # step > n
    push(admin, {"seq": "x", "rid": "r", "step": 1})
    admin.send_text("not json")
    admin.send_text("[1,2]")
    _, w = join(client, "late")
    assert w["state"]["seq"] == 5 and w["state"]["step"] == 2


def test_state_is_sanitized(client):
    admin, _ = join(client, "adm", KEY, role="remote")
    push(admin, st(1, 3, manual={"p1": [1, -4, 5000, 2], "evil": [9]}, frozen={"p2": "x"},
                   segStart={"0": 5, "1": "x"}, extra={"a": 1}))
    s = join(client, "late")[1]["state"]
    assert s["manual"] == {"p1": [1, 0, 0]}
    assert s["frozen"] == {}
    assert s["segStart"] == {"0": 5}
    assert "extra" not in s


# ---------------------------------------------------------------- votes

def test_vote_only_when_poll_is_live(client):
    admin, _ = join(client, "adm", KEY, role="remote")
    push(admin, st(1, 2))                      # לפני הסקר
    v, _ = join(client, "v1")
    v.send_json({"type": "presence", "role": "viewer", "vote": {"p": "p1", "o": 1}})
    push(admin, st(2, 3))                      # p1 פתוח
    peers = until(admin, "peers", lambda m: any(p["peer"] == "v1" for p in m["peers"]))["peers"]
    assert counts(peers, "p1", 3) == [0, 0, 0]
    v.send_json({"type": "presence", "role": "viewer", "vote": {"p": "p1", "o": 1}})
    v.send_json({"type": "presence", "role": "viewer", "vote": {"p": "p1", "o": 7}})   # מחוץ לטווח
    v.send_json({"type": "presence", "role": "viewer", "vote": {"p": "p2", "o": 0}})   # סקר סגור
    peers = until(admin, "peers", lambda m: counts(m["peers"], "p1", 3) == [0, 1, 0])["peers"]
    assert counts(peers, "p2", 2) == [0, 0]


def test_one_vote_per_client_survives_refresh_and_leave(client):
    admin, _ = join(client, "adm", KEY, role="remote")
    push(admin, st(1, 3))
    a, _ = join(client, "phoneA")
    a.send_json({"type": "presence", "role": "viewer", "vote": {"p": "p1", "o": 0}})
    a.send_json({"type": "presence", "role": "viewer", "vote": {"p": "p1", "o": 2}})   # שינוי קול
    until(admin, "peers", lambda m: counts(m["peers"], "p1", 3) == [0, 0, 1])
    a.close()                                  # יצא: הקול נשאר
    # רענון: אותו clientId, בלי קול בזיכרון של הדף
    _, w = join(client, "phoneA", vote=None)
    assert counts(w["peers"], "p1", 3) == [0, 0, 1]
    b, _ = join(client, "phoneB", vote={"p": "p1", "o": 2})   # קול שמגיע עם hello (אחרי ניתוק)
    until(admin, "peers", lambda m: counts(m["peers"], "p1", 3) == [0, 0, 2])


def test_back_to_poll_restores_its_votes(client):
    admin, _ = join(client, "adm", KEY, role="remote")
    push(admin, st(1, 3))
    v, _ = join(client, "v1", vote={"p": "p1", "o": 1})
    push(admin, st(2, 6))
    v.send_json({"type": "presence", "role": "viewer", "vote": {"p": "p2", "o": 0}})
    push(admin, st(3, 3))                      # חזרה לסקר הראשון
    until(admin, "peers", lambda m: counts(m["peers"], "p1", 3) == [0, 1, 0])


def test_reset_clears_votes(client):
    admin, _ = join(client, "adm", KEY, role="remote")
    push(admin, st(1, 3))
    join(client, "v1", vote={"p": "p1", "o": 1})
    push(admin, st(2, 0))                      # איפוס
    push(admin, st(3, 3))
    _, w = join(client, "late")
    assert counts(w["peers"], "p1", 3) == [0, 0, 0]


def test_invalid_client_id_is_replaced(client):
    _, w = join(client, "<script>")
    assert w["clientId"] != "<script>"
