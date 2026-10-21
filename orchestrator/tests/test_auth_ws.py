"""WebSocket auth (ws_auth.py): one-time ?ticket= handshake, 4001 close codes
and the per-socket session watchdog, on /ws/status and /ws/terminal/{id}.

Auth is on against a temp DB (tests/auth_helpers.py). The watchdog re-check
interval is shortened to 0.2 s; every receive has a timeout so a missing
close fails the test instead of hanging it.
"""

import logging
import socket
import time

import anyio
import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

import auth
import auth_store
import config
import main
import ws_auth
from models import Agent, AgentMode, AgentStatus, Project
from routers import terminal
from tests.auth_helpers import HOST, enable_auth, make_auth_db
from websocket import ws_manager

AGENT_ID = "wsauth0000agent"


@pytest.fixture
def Session(tmp_path, monkeypatch):
    Session, _engine = make_auth_db(tmp_path)
    enable_auth(monkeypatch, Session)
    monkeypatch.setattr(config, "WS_AUTH_RECHECK_S", 0.2)
    return Session


def _client():
    return TestClient(main.app)


def _connect(path: str):
    return _client().websocket_connect(path, headers={"host": HOST[1]})


def _recv(ws, timeout: float = 10.0) -> dict:
    """Next raw message from the server, or TimeoutError."""
    async def _receive():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()
    return ws.portal.call(_receive)


def _close_of(ws, timeout: float = 10.0) -> tuple[int, str]:
    """Skip data frames until the server's close; return (code, reason)."""
    while True:
        msg = _recv(ws, timeout)
        if msg["type"] == "websocket.close":
            return msg.get("code", 1000), msg.get("reason", "")


def _new_session(Session) -> tuple[str, str]:
    db = Session()
    try:
        token, row = auth_store.create_session(db, user_agent="pytest")
        return token, row.id
    finally:
        db.close()


def _ticket_http(token: str) -> str:
    r = _client().post("/api/auth/ws-ticket",
                       headers={"host": HOST[1], "authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_in"] == 30 and body["ticket"].startswith("xyt_")
    return body["ticket"]


def _ticket_for(sid: str, **kw) -> str:
    ticket, _ttl = auth_store.issue_ws_ticket(
        auth_store.AuthResult(ok=True, kind="session", session_id=sid), **kw)
    return ticket


def _revoke(Session, sid: str) -> None:
    db = Session()
    try:
        assert auth_store.revoke_session(db, sid, "revoked")
    finally:
        db.close()


def _make_idle(Session, sid: str) -> None:
    db = Session()
    try:
        auth_store.set_idle_timeout(db, 5)
        db.execute(update(auth_store.AuthSession)
                   .where(auth_store.AuthSession.id == sid)
                   .values(last_active_at=time.time() - 3600))
        db.commit()
    finally:
        db.close()
    auth_store._forget_session(sid)


def _ping(ws) -> None:
    ws.send_text("ping")
    msg = _recv(ws)
    assert msg["type"] == "websocket.send" and msg["text"] == '{"type": "pong"}'


# ---- /ws/status: handshake ----

def test_status_without_ticket_closes_4001_missing(Session):
    with _connect("/ws/status") as ws:
        assert _close_of(ws) == (4001, "missing")


def test_status_legacy_token_param_is_not_a_ticket(Session):
    token, _sid = _new_session(Session)
    with _connect(f"/ws/status?token={token}") as ws:
        assert _close_of(ws) == (4001, "missing")


def test_status_valid_ticket_opens_and_is_single_use(Session):
    token, _sid = _new_session(Session)
    ticket = _ticket_http(token)
    with _connect(f"/ws/status?ticket={ticket}") as ws:
        _ping(ws)
    with _connect(f"/ws/status?ticket={ticket}") as ws:
        assert _close_of(ws) == (4001, "invalid")


def test_status_unknown_and_expired_tickets_close_invalid(Session):
    _token, sid = _new_session(Session)
    with _connect("/ws/status?ticket=xyt_not-a-real-ticket") as ws:
        assert _close_of(ws) == (4001, "invalid")
    stale = _ticket_for(sid, now=time.time() - config.WS_TICKET_TTL_S - 1)
    with _connect(f"/ws/status?ticket={stale}") as ws:
        assert _close_of(ws) == (4001, "invalid")


def test_status_ticket_of_revoked_or_idle_session_closes_with_its_reason(Session, monkeypatch):
    # No watchdog tick within the test: only the handshake check can close.
    monkeypatch.setattr(config, "WS_AUTH_RECHECK_S", 60.0)
    _token, sid = _new_session(Session)
    ticket = _ticket_for(sid)
    _revoke(Session, sid)
    with _connect(f"/ws/status?ticket={ticket}") as ws:
        assert _close_of(ws, timeout=3) == (4001, "revoked")

    _token, sid = _new_session(Session)
    ticket = _ticket_for(sid)
    _make_idle(Session, sid)
    with _connect(f"/ws/status?ticket={ticket}") as ws:
        assert _close_of(ws, timeout=3) == (4001, "idle")


def test_status_ticket_route_needs_a_session(Session):
    r = _client().post("/api/auth/ws-ticket", headers={"host": HOST[1]})
    assert r.status_code == 401 and r.headers["x-auth-reason"] == "missing"


def test_reject_logs_never_carry_the_ticket(Session, caplog):
    caplog.set_level(logging.INFO)
    with _connect("/ws/status?ticket=xyt_SECRETTICKETVALUE") as ws:
        assert _close_of(ws) == (4001, "invalid")
    assert "WS_AUTH_REJECT reason=invalid /ws/status" in caplog.text
    assert "SECRETTICKETVALUE" not in caplog.text


def test_store_error_closes_1011_not_4001(Session, monkeypatch):
    def _boom(_db):
        raise OperationalError("SELECT", {}, Exception("database is locked"))
    monkeypatch.setattr(ws_auth, "get_password_hash", _boom)
    with _connect("/ws/status?ticket=xyt_whatever") as ws:
        code, _reason = _close_of(ws)
    assert code == 1011


# ---- /ws/status: watchdog ----

def test_status_revoke_while_open_closes_4001_revoked(Session):
    token, sid = _new_session(Session)
    with _connect(f"/ws/status?ticket={_ticket_http(token)}") as ws:
        _ping(ws)
        before = len(ws_manager.active)
        _revoke(Session, sid)
        assert _close_of(ws) == (4001, "revoked")
        # on_fail dropped it from the broadcast set before the close.
        assert len(ws_manager.active) == before - 1


def test_status_idle_while_open_closes_4001_idle(Session):
    token, sid = _new_session(Session)
    with _connect(f"/ws/status?ticket={_ticket_http(token)}") as ws:
        _ping(ws)
        _make_idle(Session, sid)
        assert _close_of(ws) == (4001, "idle")


def test_status_healthy_session_stays_open_across_rechecks(Session):
    token, _sid = _new_session(Session)
    with _connect(f"/ws/status?ticket={_ticket_http(token)}") as ws:
        time.sleep(1.0)  # five re-check periods
        _ping(ws)


def test_status_socket_traffic_is_not_activity(Session):
    token, sid = _new_session(Session)
    db = Session()
    try:
        auth_store.set_idle_timeout(db, 5)
        before = auth_store.get_session(db, sid).last_active_at
    finally:
        db.close()
    with _connect(f"/ws/status?ticket={_ticket_http(token)}") as ws:
        for _ in range(3):
            _ping(ws)
    db = Session()
    try:
        assert auth_store.get_session(db, sid).last_active_at == before
    finally:
        db.close()


def test_status_legacy_ctx_cut_off_by_not_before(Session):
    iat = time.time() - 60
    legacy = auth_store.AuthResult(ok=True, kind="legacy", legacy_exp=time.time() + 3600,
                                   legacy_iat=iat)
    ticket, _ = auth_store.issue_ws_ticket(legacy)
    with _connect(f"/ws/status?ticket={ticket}") as ws:
        _ping(ws)
        db = Session()
        try:
            auth_store.bump_not_before(db)
        finally:
            db.close()
        assert _close_of(ws) == (4001, "revoked")


def test_status_open_when_auth_disabled(Session, monkeypatch):
    monkeypatch.setenv("DISABLE_AUTH", "1")
    with _connect("/ws/status") as ws:
        _ping(ws)
    # Under DISABLE_AUTH the ws-ticket route hands out no ticket.
    r = _client().post("/api/auth/ws-ticket", headers={"host": HOST[1]})
    assert r.status_code == 200 and r.json() == {"ticket": None, "expires_in": 0}


def test_status_open_without_password(tmp_path, monkeypatch):
    Session, _engine = make_auth_db(tmp_path, password=None)
    enable_auth(monkeypatch, Session)
    with _connect("/ws/status") as ws:
        _ping(ws)


def test_open_ctx_has_no_watchdog():
    assert ws_auth.start_watchdog(object(), auth_store.WsAuthCtx(kind="open"), lambda: None) is None
    assert ws_auth.start_watchdog(object(), None, lambda: None) is None


# ---- /ws/terminal/{agent_id} ----

class _FakeProc:
    pid = 4242

    def __init__(self):
        self.reaped = False

    def poll(self):
        return 0 if self.reaped else None

    def terminate(self):
        self.reaped = True

    def kill(self):
        self.reaped = True

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_terminal(Session, monkeypatch):
    """An agent row plus a socketpair standing in for the tmux client's PTY."""
    db = Session()
    try:
        db.add(Project(name="ws-proj", display_name="WS", path="/tmp/ws-proj"))
        db.flush()
        db.add(Agent(id=AGENT_ID, project="ws-proj", name="WS agent", mode=AgentMode.AUTO,
                     status=AgentStatus.IDLE, model="claude-opus-4-7"))
        db.commit()
    finally:
        db.close()
    master, far = socket.socketpair()
    proc = _FakeProc()
    spawned = []

    def _spawn(session):
        spawned.append(session)
        return proc, master.detach()

    monkeypatch.setattr(terminal, "_resolve_tmux_session", lambda agent: "xy-wsauth00")
    monkeypatch.setattr(terminal, "_spawn_attach_client", _spawn)
    yield {"proc": proc, "far": far, "spawned": spawned}
    far.close()


def test_terminal_without_ticket_closes_4001_before_agent_lookup(fake_terminal, monkeypatch):
    monkeypatch.setattr(terminal, "_resolve_tmux_session",
                        lambda agent: pytest.fail("agent lookup reached"))
    with _connect(f"/ws/terminal/{AGENT_ID}") as ws:
        assert _close_of(ws) == (4001, "missing")
    with _connect(f"/ws/terminal/{AGENT_ID}?ticket=xyt_bogus") as ws:
        assert _close_of(ws) == (4001, "invalid")
    assert fake_terminal["spawned"] == []


def test_terminal_valid_ticket_attaches_then_revoke_closes_4001(Session, fake_terminal):
    token, sid = _new_session(Session)
    with _connect(f"/ws/terminal/{AGENT_ID}?ticket={_ticket_http(token)}") as ws:
        fake_terminal["far"].sendall(b"hello from tmux")
        msg = _recv(ws)
        assert msg["type"] == "websocket.send" and msg["bytes"] == b"hello from tmux"
        ws.send_text('{"type": "ping"}')
        assert _recv(ws)["text"] == '{"type": "pong"}'
        _revoke(Session, sid)
        assert _close_of(ws) == (4001, "revoked")
    assert fake_terminal["spawned"] == ["xy-wsauth00"]
    # The handler's finally detached (reaped) the tmux client.
    deadline = time.time() + 5
    while not fake_terminal["proc"].reaped and time.time() < deadline:
        time.sleep(0.05)
    assert fake_terminal["proc"].reaped


def test_terminal_idle_closes_4001_idle(Session, fake_terminal):
    token, sid = _new_session(Session)
    with _connect(f"/ws/terminal/{AGENT_ID}?ticket={_ticket_http(token)}") as ws:
        ws.send_text('{"type": "ping"}')
        assert _recv(ws)["text"] == '{"type": "pong"}'
        _make_idle(Session, sid)
        assert _close_of(ws) == (4001, "idle")


def test_terminal_unknown_agent_keeps_4404_after_auth(Session, fake_terminal):
    token, _sid = _new_session(Session)
    with _connect(f"/ws/terminal/nosuchagent?ticket={_ticket_http(token)}") as ws:
        msg = _recv(ws)
        assert msg["type"] == "websocket.send" and "Unknown agent" in msg["text"]
        assert _close_of(ws)[0] == 4404


def test_terminal_open_when_auth_disabled(Session, fake_terminal, monkeypatch):
    monkeypatch.setenv("DISABLE_AUTH", "1")
    with _connect(f"/ws/terminal/{AGENT_ID}") as ws:
        ws.send_text('{"type": "ping"}')
        assert _recv(ws)["text"] == '{"type": "pong"}'
    assert fake_terminal["spawned"] == ["xy-wsauth00"]


def test_ws_auth_reads_session_factory_at_call_time(Session, monkeypatch):
    """authorize_ws must use database.SessionLocal as patched, not a copy
    bound at import (tests and the e2e patch it)."""
    db = ws_auth._session_local()
    try:
        assert db.get_bind() is Session.kw["bind"]
    finally:
        db.close()
    assert auth.auth_disabled() is False
