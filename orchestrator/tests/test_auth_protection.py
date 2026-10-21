"""POST /api/auth/protection: switching password protection at runtime.

DISABLE_AUTH in .env is the one source of truth: the switch rewrites .env
first, then os.environ. Every test points env_file.ENV_PATH at a temp .env
(the real one is never read or written) and monkeypatch restores
DISABLE_AUTH afterwards. Auth runs against a temp DB (tests/auth_helpers).

WebSocket tests share one event loop between HTTP and WS calls (one blocking
portal on the TestClient), so the delayed close of ticketless sockets that
the switch schedules runs on the loop that owns those sockets - as it does
in the single-worker server.
"""

import asyncio
import hashlib
import logging
import os
import socket
import stat
import time

import anyio
import anyio.from_thread
import pytest
from fastapi import HTTPException
from sqlalchemy import update
from starlette.testclient import TestClient
from types import SimpleNamespace

import auth
import auth_store
import config
import env_file
import hook_auth
import main
import ws_auth
from auth import get_password_hash
from models import Agent, AgentMode, AgentStatus, Project
from routers import terminal
from tests.auth_helpers import HOST, LAN_IP, PASSWORD, call, enable_auth, make_auth_db

IP_B = "100.64.0.11"
LOGIN_KEYS = {"token", "session_id", "expires_at", "idle_timeout_minutes", "expires_minutes",
              "media_token", "media_expires_at"}
SETTINGS_KEYS = {"idle_timeout_minutes", "choices", "default_idle_timeout_minutes",
                 "session_max_days", "auth_disabled", "password_set"}
PROTECTION_KEYS = {"auth_disabled", "password_set", "env_file_disable_auth", "env_file_in_sync"}
ENV_OFF = ("# === Server ===\nPORT=8080\n\n# DISABLE_AUTH=1\nDISABLE_AUTH=1\n"
           "CORS_ORIGINS=https://localhost:3000\n")
ENV_ON = ENV_OFF.replace("\nDISABLE_AUTH=1\n", "\nDISABLE_AUTH=0\n")
AGENT_ID = "protect000agent"


# ---- fixtures / helpers ----

@pytest.fixture
def envfile(tmp_path, monkeypatch):
    d = tmp_path / "root"
    d.mkdir()
    path = d / ".env"
    monkeypatch.setattr(env_file, "ENV_PATH", str(path))
    return path


@pytest.fixture
def store(tmp_path, monkeypatch, envfile):
    Session, engine = make_auth_db(tmp_path)
    enable_auth(monkeypatch, Session)  # records DISABLE_AUTH for restore
    monkeypatch.setattr(ws_auth, "OPEN_CLOSE_DELAY_S", 0.0)
    ws_auth._open_sockets.clear()
    yield Session
    ws_auth._open_sockets.clear()
    engine.dispose()


def _set_state(monkeypatch, envfile, running_off: bool, text: str | None):
    monkeypatch.setenv("DISABLE_AUTH", "1" if running_off else "0")
    if text is None:
        if envfile.exists():
            envfile.unlink()
    else:
        envfile.write_text(text)


async def _drain():
    """Let the scheduled open-socket close (delay 0 here) finish."""
    for _ in range(3):
        await asyncio.sleep(0)
    if ws_auth._pending_closes:
        await asyncio.gather(*list(ws_auth._pending_closes))


async def _protect(enabled, password=PASSWORD, token=None, ip=LAN_IP, **extra):
    status, headers, body = await call("POST", "/api/auth/protection", token=token, ip=ip,
                                       json={"enabled": enabled, "password": password, **extra})
    await _drain()
    return status, headers, body


async def _login(password=PASSWORD, ip=LAN_IP):
    status, _, body = await call("POST", "/api/auth/login", json={"password": password}, ip=ip)
    assert status == 200, body
    return body


def _db(Session, fn, *a):
    db = Session()
    try:
        return fn(db, *a)
    finally:
        db.close()


def _session_count(Session) -> int:
    def _count(db):
        auth_store._ready(db)
        return db.query(auth_store.AuthSession).count()
    return _db(Session, _count)


# ---- switching on ----

@pytest.mark.anyio
async def test_turn_on_while_off_returns_a_working_session(store, envfile, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _set_state(monkeypatch, envfile, True, ENV_OFF)
    os.chmod(envfile, 0o640)
    status, _, s = await call("GET", "/api/auth/protection")  # open while off
    assert status == 200 and s["auth_disabled"] is True
    assert s["env_file_disable_auth"] is True and s["env_file_in_sync"] is True

    status, headers, body = await _protect(True, "wrong-password")
    assert (status, body) == (403, {"detail": "Password is wrong"})
    assert "x-auth-reason" not in headers
    assert envfile.read_text() == ENV_OFF and auth.auth_disabled() is True

    status, headers, body = await _protect(True, device_label="Desk")
    assert status == 200 and headers["cache-control"] == "no-store"
    assert set(body) == LOGIN_KEYS | {"settings", "protection"}
    assert body["token"].startswith("xys_")
    assert set(body["settings"]) == SETTINGS_KEYS
    assert set(body["protection"]) == PROTECTION_KEYS
    assert body["settings"]["auth_disabled"] is False
    assert body["protection"]["auth_disabled"] is False
    assert body["protection"]["env_file_disable_auth"] is False
    assert body["protection"]["env_file_in_sync"] is True
    # .env: only the uncommented line changed; mode kept; then the process env.
    assert envfile.read_text() == ENV_ON
    assert stat.S_IMODE(os.stat(envfile).st_mode) == 0o640
    assert os.environ["DISABLE_AUTH"] == "0" and auth.auth_disabled() is False

    # The middleware enforces at once: no token -> 401, the new token works.
    status, headers, _ = await call("GET", "/api/settings/auth")
    assert status == 401 and headers["x-auth-reason"] == "missing"
    status, headers, _ = await call("GET", "/api/auth/protection")
    assert status == 401 and headers["x-auth-reason"] == "missing"
    status, _, listing = await call("GET", "/api/auth/sessions", token=body["token"])
    assert status == 200 and listing["current_id"] == body["session_id"]
    assert listing["sessions"][0]["device_label"] == "Desk"
    status, _, check = await call("POST", "/api/auth/check", token=body["token"])
    assert check["authenticated"] is True and check["auth_disabled"] is False

    assert f"AUTH_PROTECTION on ip={LAN_IP} session={body['session_id']}" in caplog.text
    for secret in (PASSWORD, "wrong-password", body["token"], "CORS_ORIGINS", "PORT=8080"):
        assert secret not in caplog.text


@pytest.mark.anyio
async def test_turn_on_replaces_a_stale_bearer(store, envfile, monkeypatch):
    _set_state(monkeypatch, envfile, True, ENV_OFF)
    old = await _login()  # login still works while off
    status, _, body = await _protect(True, token=old["token"])
    assert status == 200
    row = _db(store, lambda db, sid: db.get(auth_store.AuthSession, sid), old["session_id"])
    assert row.revoked_reason == "replaced"


@pytest.mark.anyio
async def test_turn_on_wrong_password_locks_by_ip_never_loopback(store, envfile, monkeypatch):
    _set_state(monkeypatch, envfile, True, ENV_OFF)
    threshold = auth.change_password_limiter.threshold
    for i in range(threshold):
        status, _, body = await _protect(True, "nope-nope")
        assert status == 403
    assert "Locked out for" in body["detail"]
    status, headers, body = await _protect(True)  # right password, locked IP
    assert status == 429 and "retry-after" in headers
    assert envfile.read_text() == ENV_OFF and auth.auth_disabled() is True

    for _ in range(threshold + 2):  # loopback is never limited
        status, _, _ = await _protect(True, "nope-nope", ip="127.0.0.1")
        assert status == 403
    status, _, body = await _protect(True, ip=IP_B)  # another IP is not locked
    assert status == 200 and body["token"]


@pytest.mark.anyio
async def test_turn_on_without_a_password_sets_the_first_one(tmp_path, envfile, monkeypatch):
    Session, engine = make_auth_db(tmp_path, password=None)
    enable_auth(monkeypatch, Session)
    monkeypatch.setattr(ws_auth, "OPEN_CLOSE_DELAY_S", 0.0)
    try:
        _set_state(monkeypatch, envfile, True, ENV_OFF)
        status, _, s = await call("GET", "/api/settings/auth")
        assert s["password_set"] is False
        for extra in ({}, {"new_password": "short"}, {"new_password": 12345678}):
            status, _, body = await _protect(True, "", **extra)
            assert (status, body) == (400, {"detail": "New password must be at least 8 characters"})
        assert envfile.read_text() == ENV_OFF and _db(Session, get_password_hash) is None

        status, _, body = await _protect(True, "", new_password="first-pass-123")
        assert status == 200 and set(body) == LOGIN_KEYS | {"settings", "protection"}
        assert body["settings"]["password_set"] is True and body["settings"]["auth_disabled"] is False
        assert envfile.read_text() == ENV_ON
        status, _, _ = await call("GET", "/api/auth/sessions", token=body["token"])
        assert status == 200
        status, _, _ = await call("POST", "/api/auth/login", json={"password": "first-pass-123"})
        assert status == 200
    finally:
        engine.dispose()


# ---- switching off ----

@pytest.mark.anyio
async def test_turn_off_needs_a_session_and_the_password(store, envfile, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _set_state(monkeypatch, envfile, False, ENV_ON)
    s = await _login()

    status, headers, _ = await _protect(False)  # middleware: no session
    assert status == 401 and headers["x-auth-reason"] == "missing"
    status, headers, body = await _protect(False, "wrong-password", token=s["token"])
    assert (status, body) == (403, {"detail": "Password is wrong"})
    assert "x-auth-reason" not in headers
    assert envfile.read_text() == ENV_ON and auth.auth_disabled() is False

    status, _, body = await _protect(False, token=s["token"])
    assert status == 200 and set(body) == {"settings", "protection"}
    assert body["settings"]["auth_disabled"] is True
    assert body["protection"]["env_file_disable_auth"] is True
    assert body["protection"]["env_file_in_sync"] is True
    assert envfile.read_text() == ENV_OFF
    assert os.environ["DISABLE_AUTH"] == "1" and auth.auth_disabled() is True
    assert f"AUTH_PROTECTION off ip={LAN_IP} session={s['session_id']}" in caplog.text

    # Open at once; sessions are kept, so this device's token still works
    # after protection comes back on.
    status, _, _ = await call("GET", "/api/settings/auth")
    assert status == 200
    row = _db(store, lambda db, sid: db.get(auth_store.AuthSession, sid), s["session_id"])
    assert row.revoked_at is None
    status, _, _ = await _protect(True)
    assert status == 200
    status, _, _ = await call("GET", "/api/settings/auth", token=s["token"])
    assert status == 200


@pytest.mark.anyio
async def test_turn_off_lockout_is_keyed_by_session(store, envfile, monkeypatch):
    _set_state(monkeypatch, envfile, False, ENV_ON)
    s1 = await _login()
    s2 = await _login()
    for _ in range(auth.change_password_limiter.threshold):
        status, _, body = await _protect(False, "nope-nope", token=s1["token"])
        assert status == 403
    assert "Locked out for" in body["detail"]
    status, _, _ = await _protect(False, token=s1["token"], ip=IP_B)  # same session, other IP
    assert status == 429
    assert auth.auth_disabled() is False
    status, _, _ = await _protect(False, token=s2["token"])  # other session, same IP
    assert status == 200 and auth.auth_disabled() is True


@pytest.mark.anyio
async def test_turn_off_without_a_password_is_refused(tmp_path, envfile, monkeypatch):
    Session, engine = make_auth_db(tmp_path, password=None)
    enable_auth(monkeypatch, Session)
    try:
        _set_state(monkeypatch, envfile, False, ENV_ON)
        status, _, body = await _protect(False, "")
        assert (status, body) == (400, {"detail": "No password set"})
        assert envfile.read_text() == ENV_ON and auth.auth_disabled() is False
    finally:
        engine.dispose()


# ---- already in that state / .env out of sync ----

@pytest.mark.anyio
async def test_already_on(store, envfile, monkeypatch):
    _set_state(monkeypatch, envfile, False, ENV_ON)
    s = await _login()
    status, _, body = await _protect(True, "", token=s["token"])
    assert status == 200 and set(body) == {"settings", "protection"}
    assert body["protection"]["env_file_in_sync"] is True and envfile.read_text() == ENV_ON

    # A hand edit to DISABLE_AUTH=1 not yet applied: out of sync, then resynced.
    envfile.write_text(ENV_OFF)
    _, _, st = await call("GET", "/api/auth/protection", token=s["token"])
    assert st["auth_disabled"] is False and st["env_file_disable_auth"] is True
    assert st["env_file_in_sync"] is False
    status, _, body = await _protect(True, "", token=s["token"])
    assert status == 200 and set(body) == {"settings", "protection"}
    assert body["protection"]["env_file_in_sync"] is True and envfile.read_text() == ENV_ON
    assert auth.auth_disabled() is False

    # No .env / no DISABLE_AUTH line: a restart reads that as on.
    envfile.unlink()
    _, _, st = await call("GET", "/api/auth/protection", token=s["token"])
    assert st["env_file_disable_auth"] is None and st["env_file_in_sync"] is True
    envfile.write_text("PORT=8080\n")
    _, _, st = await call("GET", "/api/auth/protection", token=s["token"])
    assert st["env_file_disable_auth"] is None and st["env_file_in_sync"] is True


@pytest.mark.anyio
async def test_already_off(store, envfile, monkeypatch):
    _set_state(monkeypatch, envfile, True, ENV_OFF)
    status, _, body = await _protect(False, "")
    assert status == 200 and set(body) == {"settings", "protection"} and envfile.read_text() == ENV_OFF

    # A hand edit to DISABLE_AUTH=0 waiting for a restart: undoing it needs
    # the password, like switching off.
    envfile.write_text(ENV_ON)
    _, _, st = await call("GET", "/api/auth/protection")
    assert st["auth_disabled"] is True and st["env_file_disable_auth"] is False
    assert st["env_file_in_sync"] is False
    status, _, body = await _protect(False, "wrong-password")
    assert (status, body) == (403, {"detail": "Password is wrong"})
    assert envfile.read_text() == ENV_ON
    status, _, body = await _protect(False)
    assert status == 200 and body["protection"]["env_file_in_sync"] is True
    assert envfile.read_text() == ENV_OFF and auth.auth_disabled() is True

    # .env missing: a restart would turn protection on -> out of sync.
    envfile.unlink()
    _, _, st = await call("GET", "/api/auth/protection")
    assert st["env_file_disable_auth"] is None and st["env_file_in_sync"] is False


# ---- failures / validation ----

def _readonly_dir(path):
    os.chmod(path, 0o500)
    if os.access(path, os.W_OK):
        os.chmod(path, 0o700)
        pytest.skip("running with privileges that ignore directory modes")


@pytest.mark.anyio
async def test_env_write_failure_is_500_and_changes_nothing(store, envfile, monkeypatch):
    _set_state(monkeypatch, envfile, True, ENV_OFF)
    _readonly_dir(envfile.parent)
    try:
        status, headers, body = await _protect(True)
        assert (status, body) == (500, {"detail": "Could not update .env: Permission denied"})
        assert "x-auth-reason" not in headers
        assert auth.auth_disabled() is True and os.environ["DISABLE_AUTH"] == "1"
        assert _session_count(store) == 0
    finally:
        os.chmod(envfile.parent, 0o700)
    assert envfile.read_text() == ENV_OFF and os.listdir(envfile.parent) == [".env"]

    _set_state(monkeypatch, envfile, False, ENV_ON)
    s = await _login()
    _readonly_dir(envfile.parent)
    try:
        status, _, body = await _protect(False, token=s["token"])
        assert (status, body) == (500, {"detail": "Could not update .env: Permission denied"})
        assert auth.auth_disabled() is False
    finally:
        os.chmod(envfile.parent, 0o700)
    assert envfile.read_text() == ENV_ON


@pytest.mark.anyio
async def test_env_write_failure_keeps_no_password(tmp_path, envfile, monkeypatch):
    Session, engine = make_auth_db(tmp_path, password=None)
    enable_auth(monkeypatch, Session)
    try:
        _set_state(monkeypatch, envfile, True, ENV_OFF)
        _readonly_dir(envfile.parent)
        try:
            status, _, body = await _protect(True, "", new_password="first-pass-123")
            assert status == 500 and body["detail"].startswith("Could not update .env: ")
        finally:
            os.chmod(envfile.parent, 0o700)
        assert _db(Session, get_password_hash) is None and auth.auth_disabled() is True
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_bad_bodies(store, envfile, monkeypatch):
    _set_state(monkeypatch, envfile, True, ENV_OFF)
    for payload in ({"password": PASSWORD}, {"enabled": "true", "password": PASSWORD},
                    {"enabled": 1, "password": PASSWORD}):
        status, _, body = await call("POST", "/api/auth/protection", json=payload)
        assert (status, body) == (400, {"detail": "enabled must be true or false"})
    status, _, body = await call("POST", "/api/auth/protection", content_type="application/json",
                                 body=b"[1, 2]")
    assert (status, body) == (400, {"detail": "Invalid JSON body"})
    assert envfile.read_text() == ENV_OFF


def test_route_is_not_auth_exempt():
    assert main._is_auth_exempt("POST", "/api/auth/protection") is False


# ---- the rest of the server follows the switch without a restart ----

@pytest.mark.anyio
async def test_hook_nonce_enforcement_follows_the_switch(store, envfile, monkeypatch):
    monkeypatch.setattr(config, "XY_HOOK_NONCE_MODE", "enforce")
    hook_auth._last_logged.clear()
    _db(store, lambda db: hook_auth._upsert(db, AGENT_ID, hashlib.sha256(b"real-nonce").hexdigest()))
    req = SimpleNamespace(headers={}, url=SimpleNamespace(path="/api/hooks/agent-stop"))

    _set_state(monkeypatch, envfile, True, ENV_OFF)
    hook_auth.check(req, AGENT_ID)  # off: log only
    status, _, s = await _protect(True)
    assert status == 200
    with pytest.raises(HTTPException) as exc:
        hook_auth.check(req, AGENT_ID)
    assert exc.value.status_code == 403
    status, _, _ = await _protect(False, token=s["token"])
    assert status == 200
    hook_auth.check(req, AGENT_ID)


def test_startup_follows_env_file(envfile, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("DISABLE_AUTH", "1")  # e.g. a stale pm2 env snapshot
    envfile.write_text(ENV_ON)
    auth.sync_disable_auth_from_env_file()
    assert os.environ["DISABLE_AUTH"] == "0" and auth.auth_disabled() is False
    assert "AUTH_PROTECTION on: process env disagreed with .env" in caplog.text

    monkeypatch.setenv("DISABLE_AUTH", "1")
    envfile.write_text("PORT=8080\n# DISABLE_AUTH=0\n")  # no line: env left alone
    auth.sync_disable_auth_from_env_file()
    assert os.environ["DISABLE_AUTH"] == "1"
    envfile.unlink()
    auth.sync_disable_auth_from_env_file()
    assert os.environ["DISABLE_AUTH"] == "1"


# ---- WebSockets ----

class _FakeProc:
    pid = 4343

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
def client(store):
    """A TestClient whose HTTP and WebSocket calls share one event loop."""
    with anyio.from_thread.start_blocking_portal() as portal:
        c = TestClient(main.app)
        c.portal = portal
        yield c


@pytest.fixture
def fake_terminal(store, monkeypatch):
    db = store()
    try:
        db.add(Project(name="protect-proj", display_name="P", path="/tmp/protect-proj"))
        db.flush()
        db.add(Agent(id=AGENT_ID, project="protect-proj", name="P agent", mode=AgentMode.AUTO,
                     status=AgentStatus.IDLE, model="claude-opus-4-7"))
        db.commit()
    finally:
        db.close()
    master, far = socket.socketpair()
    proc = _FakeProc()
    monkeypatch.setattr(terminal, "_resolve_tmux_session", lambda agent: "xy-protect0")
    monkeypatch.setattr(terminal, "_spawn_attach_client", lambda session: (proc, master.detach()))
    yield proc
    far.close()


def _recv(ws, timeout: float = 10.0) -> dict:
    async def _receive():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()
    return ws.portal.call(_receive)


def _close_of(ws, timeout: float = 10.0) -> tuple[int, str]:
    while True:
        msg = _recv(ws, timeout)
        if msg["type"] == "websocket.close":
            return msg.get("code", 1000), msg.get("reason", "")


def _ping(ws, text="ping"):
    ws.send_text(text)
    msg = _recv(ws)
    assert msg["type"] == "websocket.send" and "pong" in msg["text"]


def _http(client, path, token=None, json=None):
    headers = {"host": HOST[1]}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return client.post(path, headers=headers, json=json)


def _ws(client, path):
    return client.websocket_connect(path, headers={"host": HOST[1]})


def _ticket(client, token) -> str:
    r = _http(client, "/api/auth/ws-ticket", token)
    assert r.status_code == 200 and r.json()["ticket"], r.text
    return r.json()["ticket"]


def test_ticketless_sockets_close_4001_after_turn_on_ticketed_survive(
        store, envfile, monkeypatch, client, fake_terminal, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(config, "WS_AUTH_RECHECK_S", 0.2)
    monkeypatch.setattr(ws_auth, "OPEN_CLOSE_DELAY_S", 0.5)
    _set_state(monkeypatch, envfile, False, ENV_ON)
    token, _row = _db(store, lambda db: auth_store.create_session(db, user_agent="pytest"))

    with _ws(client, f"/ws/status?ticket={_ticket(client, token)}") as ticketed:
        _ping(ticketed)
        r = _http(client, "/api/auth/protection", token, {"enabled": False, "password": PASSWORD})
        assert r.status_code == 200 and auth.auth_disabled() is True
        time.sleep(0.5)  # watchdog ticks while off
        _ping(ticketed)

        with _ws(client, "/ws/status") as status_ws, \
                _ws(client, f"/ws/terminal/{AGENT_ID}") as term_ws:
            _ping(status_ws)
            _ping(term_ws, '{"type": "ping"}')
            assert len(ws_auth._open_sockets) == 2

            r = _http(client, "/api/auth/protection", None, {"enabled": True, "password": PASSWORD})
            assert r.status_code == 200
            new_token = r.json()["token"]
            # Not closed with the response: the device stores its token first.
            assert len(ws_auth._open_sockets) == 2
            assert _close_of(status_ws) == (4001, "missing")
            assert _close_of(term_ws) == (4001, "missing")
        assert ws_auth._open_sockets == {}
        assert "WS_AUTH_CLOSE_OPEN reason=missing count=2" in caplog.text

        time.sleep(0.5)  # the ticketed socket's watchdog is back on and passes
        _ping(ticketed)

    # Other devices: no ticket -> 4001 at once; no token -> 401 for a ticket.
    with _ws(client, "/ws/status") as ws:
        assert _close_of(ws) == (4001, "missing")
    r = _http(client, "/api/auth/ws-ticket")
    assert r.status_code == 401 and r.headers["x-auth-reason"] == "missing"
    # The enabling device reconnects with a ticket.
    with _ws(client, f"/ws/status?ticket={_ticket(client, new_token)}") as ws:
        _ping(ws)


def test_switched_off_again_before_the_delay_keeps_sockets(store, envfile, monkeypatch, client):
    monkeypatch.setattr(ws_auth, "OPEN_CLOSE_DELAY_S", 0.4)
    _set_state(monkeypatch, envfile, True, ENV_OFF)
    with _ws(client, "/ws/status") as ws:
        _ping(ws)
        r = _http(client, "/api/auth/protection", None, {"enabled": True, "password": PASSWORD})
        assert r.status_code == 200
        r = _http(client, "/api/auth/protection", r.json()["token"],
                  {"enabled": False, "password": PASSWORD})
        assert r.status_code == 200
        time.sleep(0.8)
        _ping(ws)
        assert len(ws_auth._open_sockets) == 1


def test_watchdog_is_inert_while_off_and_resumes_when_on(store, envfile, monkeypatch, client):
    monkeypatch.setattr(config, "WS_AUTH_RECHECK_S", 0.2)
    _set_state(monkeypatch, envfile, False, ENV_ON)
    token, row = _db(store, lambda db: auth_store.create_session(db, user_agent="pytest"))
    with _ws(client, f"/ws/status?ticket={_ticket(client, token)}") as ws:
        _ping(ws)
        monkeypatch.setenv("DISABLE_AUTH", "1")
        db = store()
        try:
            auth_store.set_idle_timeout(db, 5)
            db.execute(update(auth_store.AuthSession).where(auth_store.AuthSession.id == row.id)
                       .values(last_active_at=time.time() - 3600))
            db.commit()
        finally:
            db.close()
        auth_store._forget_session(row.id)
        time.sleep(0.8)  # several re-checks: no activity is recorded while off
        _ping(ws)
        monkeypatch.setenv("DISABLE_AUTH", "0")
        assert _close_of(ws) == (4001, "idle")
