"""/api/auth/* and /api/settings/auth with auth ENABLED, real routing.

Never-lockout guarantees: G1 (the correct password from IP B succeeds while
IP A is locked), G2 (loopback is never 429), G5 (idle off disables the lock),
plus the reset-CLI limiter hook (auth_limiter_reset_at).
"""

import asyncio
import time
import types

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import auth
import auth_store
from auth import get_config, get_jwt_secret, peek_token, set_config
from models import Base
from tests.auth_helpers import LAN_IP, PASSWORD, call, enable_auth, fast_hash, make_auth_db

IP_B = "100.64.0.11"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    Session, engine = make_auth_db(tmp_path)
    enable_auth(monkeypatch, Session)
    yield Session
    engine.dispose()


async def _login(password=PASSWORD, ip=LAN_IP, extra=None, **kw):
    return await call("POST", "/api/auth/login", json={"password": password, **(extra or {})}, ip=ip, **kw)


def _db_get(Session, fn, *a, **kw):
    db = Session()
    try:
        return fn(db, *a, **kw)
    finally:
        db.close()


def _row(Session, sid):
    return _db_get(Session, lambda db: db.get(auth_store.AuthSession, sid))


LOGIN_KEYS = {"token", "session_id", "expires_at", "idle_timeout_minutes", "expires_minutes",
              "media_token", "media_expires_at"}


# ---- login ----

@pytest.mark.anyio
async def test_login_gives_a_working_session(store):
    status, headers, body = await _login(
        headers=[("user-agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1")])
    assert status == 200 and set(body) == LOGIN_KEYS
    assert headers["cache-control"] == "no-store"
    assert body["token"].startswith("xys_")
    assert body["idle_timeout_minutes"] == body["expires_minutes"] == 30
    claims = peek_token(body["media_token"], _db_get(store, get_jwt_secret))
    assert claims["scope"] == "media" and claims["sid"] == body["session_id"]
    assert abs(body["media_expires_at"] - (time.time() + 12 * 3600)) < 60

    status, _, listing = await call("GET", "/api/auth/sessions", token=body["token"])
    assert status == 200 and listing["current_id"] == body["session_id"]
    assert listing["auth_disabled"] is False
    (row,) = listing["sessions"]
    assert row["current"] is True and row["state"] == "active"
    assert row["device_label"] == "iPhone · Safari" and row["last_ip"] == LAN_IP

    status, _, body2 = await _login(extra={"device_label": "Desk\x00 Mac"})
    assert _row(store, body2["session_id"]).device_label == "Desk Mac"


@pytest.mark.anyio
async def test_wrong_password_401_without_reason(store):
    status, headers, body = await _login("wrong")
    assert status == 401 and body == {"detail": "Wrong password"}
    assert "x-auth-reason" not in headers and headers["cache-control"] == "no-store"
    status, _, body = await call("POST", "/api/auth/login", content_type="application/json", body=b"{nope")
    assert status == 400 and body == {"detail": "Invalid JSON body"}


@pytest.mark.anyio
async def test_lockout_is_per_ip_and_never_loopback(store, monkeypatch):
    calls = []
    real_verify = auth.verify_password_async

    async def counting_verify(pw, h):
        calls.append(pw)
        return await real_verify(pw, h)

    import routers.auth as auth_routes
    monkeypatch.setattr(auth_routes, "verify_password_async", counting_verify)

    for i in range(4):
        status, _, body = await _login("wrong", ip=LAN_IP)
        assert status == 401 and body["detail"] == "Wrong password"
    status, _, body = await _login("wrong", ip=LAN_IP)
    assert status == 401 and body["detail"] == "Wrong password. Locked out for 60s."
    status, headers, body = await _login("wrong", ip=LAN_IP)
    assert status == 429 and body["detail"].startswith("Too many failed attempts. Try again in ")
    assert 0 < int(headers["retry-after"]) <= 60 and "x-auth-reason" not in headers
    # The lock refuses before bcrypt runs — even the correct password.
    n = len(calls)
    status, _, _ = await _login(PASSWORD, ip=LAN_IP)
    assert status == 429 and len(calls) == n
    # G1: another IP is unaffected.
    status, _, body = await _login(PASSWORD, ip=IP_B)
    assert status == 200 and body["token"].startswith("xys_")
    # G2: loopback is never locked (v4 and v6).
    for ip in ("127.0.0.1", "::1"):
        for _ in range(6):
            status, _, body = await _login("wrong", ip=ip)
            assert status == 401 and body["detail"] == "Wrong password"
        status, _, _ = await _login(PASSWORD, ip=ip)
        assert status == 200
    # The reset CLI's auth_limiter_reset_at frees the locked IP at once.
    _db_get(store, set_config, "auth_limiter_reset_at", repr(time.time()))
    status, _, _ = await _login(PASSWORD, ip=LAN_IP)
    assert status == 200


def _count_verifies(monkeypatch) -> list:
    calls = []
    real_verify = auth.verify_password_async

    async def counting_verify(pw, h):
        calls.append(pw)
        return await real_verify(pw, h)

    import routers.auth as auth_routes
    monkeypatch.setattr(auth_routes, "verify_password_async", counting_verify)
    return calls


@pytest.fixture()
def wal_store(tmp_path, monkeypatch):
    """Like `store`, on an engine set up like database.engine (NullPool,
    WAL): concurrent requests each hold a session across their awaits."""
    engine = create_engine(f"sqlite:///{tmp_path / 'wal.db'}",
                           connect_args={"check_same_thread": False}, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA busy_timeout=5000")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    _db_get(Session, set_config, "password_hash", fast_hash(PASSWORD))
    enable_auth(monkeypatch, Session)
    yield Session
    engine.dispose()


@pytest.mark.anyio
async def test_concurrent_burst_from_one_ip_is_capped_at_the_threshold(wal_store, monkeypatch):
    # Every request of the burst passes the first check() before any
    # failure is recorded; only the threshold may still reach bcrypt.
    calls = _count_verifies(monkeypatch)
    results = await asyncio.gather(*(_login("wrong") for _ in range(20)),
                                   _login(PASSWORD, ip=IP_B))
    statuses = [status for status, _, _ in results[:-1]]
    assert len([pw for pw in calls if pw == "wrong"]) == 5
    assert statuses.count(401) == 5 and statuses.count(429) == 15
    assert results[-1][0] == 200  # G1 holds during the burst
    status, _, _ = await _login(PASSWORD)
    assert status == 429


@pytest.mark.anyio
async def test_concurrent_change_password_burst_is_capped(wal_store, monkeypatch):
    _, _, s = await _login()
    calls = _count_verifies(monkeypatch)
    results = await asyncio.gather(*(
        call("POST", "/api/auth/change-password", token=s["token"],
             json={"current_password": "wrong", "new_password": "new-pass-123"})
        for _ in range(12)))
    statuses = [status for status, _, _ in results]
    assert len(calls) == 5
    assert statuses.count(403) == 5 and statuses.count(429) == 7


@pytest.mark.anyio
async def test_login_replaces_previous_session(store):
    _, _, first = await _login()
    _, _, second = await _login(token=first["token"])
    assert second["session_id"] != first["session_id"]
    old = _row(store, first["session_id"])
    assert old.revoked_reason == "replaced"
    status, headers, _ = await call("GET", "/api/auth/sessions", token=first["token"])
    assert status == 401 and headers["x-auth-reason"] == "revoked"
    # A wrong password never revokes the presented token.
    status, _, _ = await _login("wrong", token=second["token"])
    assert status == 401
    status, _, _ = await call("GET", "/api/auth/sessions", token=second["token"])
    assert status == 200


# ---- idle ----

@pytest.mark.anyio
async def test_idle_lock_then_unlock_by_login(store, monkeypatch):
    clock = {"now": time.time()}
    monkeypatch.setattr(auth_store, "time", types.SimpleNamespace(
        time=lambda: clock["now"], monotonic=time.monotonic))
    _, _, s = await _login()
    clock["now"] += 31 * 60
    status, headers, body = await call("GET", "/api/auth/sessions", token=s["token"])
    assert status == 401 and headers["x-auth-reason"] == "idle"
    assert body == {"detail": "Locked after inactivity"}
    status, headers, _ = await call("POST", "/api/auth/activity", token=s["token"])
    assert status == 401 and headers["x-auth-reason"] == "idle"
    # /check reports it without a 401.
    status, _, chk = await call("POST", "/api/auth/check", token=s["token"])
    assert status == 200 and chk["authenticated"] is False and chk["reason"] == "idle"
    # Idle keeps the row; the password unlocks with a NEW session.
    assert _row(store, s["session_id"]).revoked_at is None
    status, _, s2 = await _login(token=s["token"])
    assert status == 200 and s2["token"] != s["token"]
    assert _row(store, s["session_id"]).revoked_reason == "replaced"
    status, _, _ = await call("GET", "/api/auth/sessions", token=s2["token"])
    assert status == 200


@pytest.mark.anyio
async def test_idle_off_disables_the_lock(store, monkeypatch):
    clock = {"now": time.time()}
    monkeypatch.setattr(auth_store, "time", types.SimpleNamespace(
        time=lambda: clock["now"], monotonic=time.monotonic))
    _, _, s = await _login()
    status, _, body = await call("PUT", "/api/settings/auth", token=s["token"], json={"idle_timeout_minutes": 0})
    assert status == 200 and body["idle_timeout_minutes"] == 0
    clock["now"] += 3 * 86400
    status, _, act = await call("POST", "/api/auth/activity", token=s["token"])
    assert status == 200 and act == {"ok": True, "idle_timeout_minutes": 0, "idle_remaining_s": None}
    status, _, chk = await call("POST", "/api/auth/check", token=s["token"])
    assert chk["authenticated"] is True and chk["idle_remaining_s"] is None


@pytest.mark.anyio
async def test_activity_ping(store):
    _, _, s = await _login()
    status, headers, body = await call("POST", "/api/auth/activity", token=s["token"])
    assert status == 200 and headers["cache-control"] == "no-store"
    assert body["ok"] is True and body["idle_timeout_minutes"] == 30
    assert 29 * 60 < body["idle_remaining_s"] <= 30 * 60
    status, headers, _ = await call("POST", "/api/auth/activity")
    assert status == 401 and headers["x-auth-reason"] == "missing"


# ---- change password ----

@pytest.mark.anyio
async def test_change_password(store):
    _, _, a = await _login()
    _, _, b = await _login(ip=IP_B)
    tok = a["token"]
    # Needs a session now.
    status, headers, _ = await call(
        "POST", "/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "new-pass-123"})
    assert status == 401 and headers["x-auth-reason"] == "missing"
    # Wrong current password: 403, no reason → never a logout.
    status, headers, body = await call(
        "POST", "/api/auth/change-password", token=tok,
        json={"current_password": "wrong", "new_password": "new-pass-123"})
    assert status == 403 and body == {"detail": "Current password is wrong"}
    assert "x-auth-reason" not in headers
    status, _, _ = await call("GET", "/api/auth/sessions", token=tok)
    assert status == 200
    # Too short.
    status, _, body = await call(
        "POST", "/api/auth/change-password", token=tok,
        json={"current_password": PASSWORD, "new_password": "short"})
    assert status == 400 and body == {"detail": "New password must be at least 8 characters"}

    before = time.time()
    status, headers, body = await call(
        "POST", "/api/auth/change-password", token=tok,
        json={"current_password": PASSWORD, "new_password": "new-pass-123"})
    assert status == 200 and set(body) == LOGIN_KEYS | {"revoked_sessions"}
    assert body["revoked_sessions"] == 2 and headers["cache-control"] == "no-store"
    for old in (a, b):
        status, headers, _ = await call("GET", "/api/auth/sessions", token=old["token"])
        assert status == 401 and headers["x-auth-reason"] == "revoked"
        assert _row(store, old["session_id"]).revoked_reason == "password-change"
    status, _, listing = await call("GET", "/api/auth/sessions", token=body["token"])
    assert status == 200 and [r["id"] for r in listing["sessions"]] == [body["session_id"]]
    assert float(_db_get(store, get_config, "auth_jwt_not_before")) >= before
    # The fresh media token is not cut off by the not-before it just set.
    assert _db_get(store, auth_store.authenticate_media, body["media_token"]).ok
    status, _, _ = await _login(PASSWORD)
    assert status == 401
    status, _, _ = await _login("new-pass-123")
    assert status == 200


@pytest.mark.anyio
async def test_change_password_has_its_own_bucket(store):
    _, _, s = await _login()
    for _ in range(4):
        status, _, _ = await call("POST", "/api/auth/change-password", token=s["token"],
                                  json={"current_password": "wrong", "new_password": "new-pass-123"})
        assert status == 403
    status, _, body = await call("POST", "/api/auth/change-password", token=s["token"],
                                 json={"current_password": "wrong", "new_password": "new-pass-123"})
    assert status == 403 and body["detail"] == "Current password is wrong. Locked out for 60s."
    status, headers, _ = await call("POST", "/api/auth/change-password", token=s["token"],
                                    json={"current_password": PASSWORD, "new_password": "new-pass-123"})
    assert status == 429 and int(headers["retry-after"]) > 0 and "x-auth-reason" not in headers
    # Keyed on the session, not the IP: login from the same IP still works,
    # and the session itself is still usable.
    status, _, _ = await _login()
    assert status == 200
    status, _, _ = await call("GET", "/api/auth/sessions", token=s["token"])
    assert status == 200


async def _wrong_change_password(**kw):
    return await call("POST", "/api/auth/change-password",
                      json={"current_password": "wrong", "new_password": "new-pass-123"}, **kw)


@pytest.mark.anyio
async def test_change_password_bucket_is_per_session_not_per_ip(store):
    _, _, a = await _login()
    _, _, b = await _login()  # same IP, another session
    for _ in range(5):
        status, _, _ = await _wrong_change_password(token=a["token"])
        assert status == 403
    status, _, _ = await _wrong_change_password(token=a["token"])
    assert status == 429
    status, _, body = await _wrong_change_password(token=b["token"])
    assert status == 403 and body == {"detail": "Current password is wrong"}


@pytest.mark.anyio
async def test_change_password_without_session_is_ip_keyed_never_loopback(store, monkeypatch):
    # DISABLE_AUTH (the live setting) and legacy callers have no session:
    # the bucket is the IP, and loopback is never locked (G2).
    monkeypatch.setenv("DISABLE_AUTH", "1")
    for ip in ("127.0.0.1", "::1"):
        for _ in range(7):
            status, _, body = await _wrong_change_password(ip=ip)
            assert status == 403 and body == {"detail": "Current password is wrong"}
    for _ in range(5):
        status, _, _ = await _wrong_change_password(ip=LAN_IP)
        assert status == 403
    status, headers, _ = await _wrong_change_password(ip=LAN_IP)
    assert status == 429 and int(headers["retry-after"]) > 0
    status, _, body = await call("POST", "/api/auth/change-password", ip="127.0.0.1",
                                 json={"current_password": PASSWORD, "new_password": "new-pass-123"})
    assert status == 200 and body["token"].startswith("xys_")


# ---- logout / sessions ----

@pytest.mark.anyio
async def test_logout_idempotent_and_works_when_idle(store, monkeypatch):
    _, _, s = await _login()
    status, headers, body = await call("POST", "/api/auth/logout", token=s["token"])
    assert status == 200 and body == {"ok": True, "revoked": True}
    assert headers["cache-control"] == "no-store"
    assert _row(store, s["session_id"]).revoked_reason == "logout"
    status, _, body = await call("POST", "/api/auth/logout", token=s["token"])
    assert status == 200 and body == {"ok": True, "revoked": False}
    status, _, body = await call("POST", "/api/auth/logout")
    assert status == 200 and body == {"ok": True, "revoked": False}

    clock = {"now": time.time()}
    monkeypatch.setattr(auth_store, "time", types.SimpleNamespace(
        time=lambda: clock["now"], monotonic=time.monotonic))
    _, _, s2 = await _login()
    clock["now"] += 40 * 60
    status, _, body = await call("POST", "/api/auth/logout", token=s2["token"])
    assert status == 200 and body == {"ok": True, "revoked": True}


@pytest.mark.anyio
async def test_sessions_revoke_and_revoke_others(store):
    _, _, a = await _login()
    _, _, b = await _login(ip=IP_B)
    _, _, c = await _login()
    status, _, listing = await call("GET", "/api/auth/sessions", token=a["token"])
    assert {r["id"] for r in listing["sessions"]} == {a["session_id"], b["session_id"], c["session_id"]}
    assert [r["id"] for r in listing["sessions"] if r["current"]] == [a["session_id"]]

    status, _, body = await call("POST", f"/api/auth/sessions/{b['session_id']}/revoke", token=a["token"])
    assert status == 200 and body == {"ok": True, "current": False}
    status, headers, _ = await call("GET", "/api/auth/sessions", token=b["token"])
    assert status == 401 and headers["x-auth-reason"] == "revoked"
    status, _, body = await call("POST", f"/api/auth/sessions/{b['session_id']}/revoke", token=a["token"])
    assert status == 404 and body == {"detail": "Session not found"}
    status, _, body = await call("POST", "/api/auth/sessions/nope/revoke", token=a["token"])
    assert status == 404

    _, _, d = await _login()
    before = time.time()
    status, _, body = await call("POST", "/api/auth/sessions/revoke-others", token=a["token"])
    assert status == 200 and body == {"ok": True, "revoked": 2}
    assert float(_db_get(store, get_config, "auth_jwt_not_before")) >= before
    for other in (c, d):
        status, headers, _ = await call("GET", "/api/auth/sessions", token=other["token"])
        assert status == 401 and headers["x-auth-reason"] == "revoked"
        assert _row(store, other["session_id"]).revoked_reason == "revoke-others"
    status, _, listing = await call("GET", "/api/auth/sessions", token=a["token"])
    assert [r["id"] for r in listing["sessions"]] == [a["session_id"]]
    # The caller's own media token survives the not-before bump.
    status, _, _ = await call("GET", f"/api/files/none/x.png?token={a['media_token']}")
    assert status != 401

    status, _, body = await call("POST", f"/api/auth/sessions/{a['session_id']}/revoke", token=a["token"])
    assert status == 200 and body == {"ok": True, "current": True}
    status, headers, _ = await call("GET", "/api/auth/sessions", token=a["token"])
    assert status == 401 and headers["x-auth-reason"] == "revoked"


@pytest.mark.anyio
async def test_legacy_caller_revoke_others_keeps_not_before(store):
    db = store()
    auth_store.ensure_auth_schema(db.get_bind())
    set_config(db, "auth_sessions_epoch", repr(time.time() + 60))
    legacy = auth.create_token(get_jwt_secret(db))
    db.close()
    _, _, other = await _login()
    status, _, body = await call("POST", "/api/auth/sessions/revoke-others", token=legacy)
    assert status == 200 and body == {"ok": True, "revoked": 1}
    assert _db_get(store, get_config, "auth_jwt_not_before") is None
    status, _, listing = await call("GET", "/api/auth/sessions", token=legacy)
    assert status == 200 and listing["current_id"] is None and listing["sessions"] == []


# ---- settings ----

@pytest.mark.anyio
async def test_settings_get_put_validation(store):
    _, _, s = await _login()
    status, headers, body = await call("GET", "/api/settings/auth", token=s["token"])
    assert status == 200 and headers["cache-control"] == "no-store"
    assert body == {"idle_timeout_minutes": 30, "choices": [0, 5, 15, 30, 60, 120, 240, 480, 1440],
                    "default_idle_timeout_minutes": 30, "session_max_days": 90,
                    "auth_disabled": False, "password_set": True}
    status, _, body = await call("PUT", "/api/settings/auth", token=s["token"], json={"idle_timeout_minutes": 15})
    assert status == 200 and body["idle_timeout_minutes"] == 15
    assert _db_get(store, get_config, "auth_idle_timeout_minutes") == "15"
    for bad in (7, "30", None, 30.5, True):
        status, _, body = await call("PUT", "/api/settings/auth", token=s["token"],
                                     json={"idle_timeout_minutes": bad})
        assert status == 400 and body == {
            "detail": "idle_timeout_minutes must be one of [0, 5, 15, 30, 60, 120, 240, 480, 1440]"}, bad
    status, headers, _ = await call("GET", "/api/settings/auth")
    assert status == 401 and headers["x-auth-reason"] == "missing"


# ---- check / tickets / media ----

@pytest.mark.anyio
async def test_check_shapes(store):
    status, headers, body = await call("POST", "/api/auth/check")
    assert status == 200 and headers["cache-control"] == "no-store"
    assert body["authenticated"] is False and body["reason"] == "missing"
    assert body["needs_setup"] is False and body["auth_disabled"] is False
    status, _, body = await call("POST", "/api/auth/check", token="garbage")
    assert status == 200 and body["reason"] == "invalid"

    _, _, s = await _login()
    status, _, body = await call("POST", "/api/auth/check", token=s["token"])
    assert status == 200
    assert set(body) == {"authenticated", "needs_setup", "auth_disabled", "reason", "session",
                         "idle_timeout_minutes", "idle_remaining_s", "media_token",
                         "media_expires_at", "server_time"}
    assert body["authenticated"] is True and body["reason"] is None
    assert body["session"] == {"id": s["session_id"], "legacy": False, "device_label": "Unknown device",
                               "created_at": pytest.approx(time.time(), abs=60),
                               "expires_at": s["expires_at"]}
    assert body["idle_timeout_minutes"] == 30 and 0 < body["idle_remaining_s"] <= 1800
    assert peek_token(body["media_token"], _db_get(store, get_jwt_secret))["scope"] == "media"
    assert abs(body["server_time"] - time.time()) < 60

    db = store()
    set_config(db, "auth_sessions_epoch", repr(time.time() + 60))
    legacy = auth.create_token(get_jwt_secret(db))
    db.close()
    _, _, body = await call("POST", "/api/auth/check", token=legacy)
    assert body["authenticated"] is True
    assert body["session"]["legacy"] is True and body["session"]["id"] is None
    assert body["idle_remaining_s"] is None and body["media_token"]


@pytest.mark.anyio
async def test_ws_ticket_and_media_token_shapes(store):
    _, _, s = await _login()
    status, headers, body = await call("POST", "/api/auth/ws-ticket", token=s["token"])
    assert status == 200 and headers["cache-control"] == "no-store"
    assert body["ticket"].startswith("xyt_") and body["expires_in"] == 30
    ctx = auth_store.consume_ws_ticket(body["ticket"])
    assert ctx.kind == "session" and ctx.session_id == s["session_id"]

    status, _, body = await call("POST", "/api/auth/media-token", token=s["token"])
    assert status == 200 and set(body) == {"token", "expires_at", "expires_in"}
    assert body["expires_in"] in (43199, 43200)
    claims = peek_token(body["token"], _db_get(store, get_jwt_secret))
    assert claims["scope"] == "media" and claims["sid"] == s["session_id"]

    for path in ("/api/auth/ws-ticket", "/api/auth/media-token"):
        status, headers, _ = await call("POST", path)
        assert status == 401 and headers["x-auth-reason"] == "missing"


@pytest.mark.anyio
async def test_disable_auth_shapes(store, monkeypatch):
    monkeypatch.setenv("DISABLE_AUTH", "1")
    status, _, body = await call("POST", "/api/auth/check")
    assert status == 200
    assert body["authenticated"] is True and body["auth_disabled"] is True
    assert body["reason"] is None and body["session"] is None
    assert body["media_token"] is None and body["media_expires_at"] is None
    assert body["needs_setup"] is False and body["idle_timeout_minutes"] == 30
    status, _, body = await call("POST", "/api/auth/ws-ticket")
    assert status == 200 and body == {"ticket": None, "expires_in": 0}
    status, _, body = await call("POST", "/api/auth/media-token")
    assert status == 200 and body == {"token": None, "expires_at": None, "expires_in": 0}
    status, _, body = await call("GET", "/api/settings/auth")
    assert status == 200 and body["auth_disabled"] is True and body["password_set"] is True
    status, _, body = await call("GET", "/api/auth/sessions")
    assert status == 200 and body == {"sessions": [], "current_id": None, "auth_disabled": True}
    # Login still works (creates a row) — the UI just never calls it.
    status, _, body = await _login()
    assert status == 200 and body["token"].startswith("xys_")
    status, _, listing = await call("GET", "/api/auth/sessions", token=body["token"])
    assert listing["current_id"] == body["session_id"]


@pytest.mark.anyio
async def test_no_password_shapes_and_set_password(tmp_path, monkeypatch):
    Session, engine = make_auth_db(tmp_path, password=None)
    enable_auth(monkeypatch, Session)
    try:
        status, _, body = await call("POST", "/api/auth/check")
        assert body["needs_setup"] is True and body["authenticated"] is False and body["reason"] is None
        status, _, body = await call("POST", "/api/auth/ws-ticket")
        assert status == 200 and body == {"ticket": None, "expires_in": 0}
        status, _, body = await _login()
        assert status == 400 and body == {"detail": "No password set — use /api/auth/set-password"}
        status, _, body = await call("POST", "/api/auth/change-password",
                                     json={"current_password": "x", "new_password": "new-pass-123"})
        assert status == 400 and body == {"detail": "No password set"}

        status, _, _ = await call("POST", "/api/auth/set-password", content_type="text/plain", body=b"{}")
        assert status == 415
        status, _, body = await call("POST", "/api/auth/set-password", json={"password": "short"})
        assert status == 400 and body == {"detail": "Password must be at least 8 characters"}
        status, headers, body = await call("POST", "/api/auth/set-password",
                                           json={"password": "first-pass-123", "device_label": "Laptop"})
        assert status == 200 and set(body) == LOGIN_KEYS and headers["cache-control"] == "no-store"
        assert _row(Session, body["session_id"]).device_label == "Laptop"
        status, _, again = await call("POST", "/api/auth/set-password", json={"password": "other-pass-123"})
        assert status == 400 and again == {"detail": "Password already set"}
        status, _, _ = await call("GET", "/api/auth/sessions", token=body["token"])
        assert status == 200
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_check_store_error(store, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def boom(*a, **kw):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    _, _, s = await _login()
    monkeypatch.setattr(auth_store, "authenticate", boom)
    monkeypatch.setattr(auth_store, "get_idle_timeout", boom)
    status, headers, body = await call("POST", "/api/auth/check", token=s["token"])
    assert status == 503 and body == {"detail": "Auth store unavailable"}
    assert "x-auth-reason" not in headers
    # DISABLE_AUTH: the answer never depends on the store.
    monkeypatch.setenv("DISABLE_AUTH", "1")
    status, _, body = await call("POST", "/api/auth/check")
    assert status == 200 and body["authenticated"] is True and body["idle_timeout_minutes"] == 30
