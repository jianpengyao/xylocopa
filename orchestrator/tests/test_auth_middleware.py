"""main.auth_middleware with auth ENABLED: reason headers, JSON-only exempt
routes, media ?token= scope, store errors, activity flag.

Most tests replace routing with a stub that reports what the middleware left
on request.state.auth, so only the middleware runs. Tests marked "real
routing" let the request reach the actual handlers (non-session 401s).
"""

import logging
import time
import types

import pytest
from sqlalchemy.exc import OperationalError
from starlette.responses import JSONResponse

import auth_store
import main
from auth import create_token, get_jwt_secret
from tests.auth_helpers import call, enable_auth, make_auth_db

T_REAL = time.time


@pytest.fixture()
def store(tmp_path, monkeypatch):
    Session, engine = make_auth_db(tmp_path)
    enable_auth(monkeypatch, Session)
    yield Session
    engine.dispose()


@pytest.fixture()
def stub(store, monkeypatch):
    """Routing replaced: 200 + the credential kind the middleware resolved."""
    async def reached(scope, receive, send):
        ctx = scope.get("state", {}).get("auth")
        await JSONResponse({"reached": True, "kind": getattr(ctx, "kind", None),
                            "sid": getattr(ctx, "session_id", None)})(scope, receive, send)

    monkeypatch.setattr(main.app.router, "middleware_stack", reached)
    return store


def _session(Session, now=None, **kw):
    db = Session()
    try:
        return auth_store.create_session(db, user_agent="pytest", now=now, **kw)
    finally:
        db.close()


def _media(Session, sid):
    db = Session()
    try:
        return auth_store.mint_media_token(db, auth_store.AuthResult(ok=True, kind="session", session_id=sid))[0]
    finally:
        db.close()


# ---- reasons ----

@pytest.mark.anyio
async def test_each_reason_header(stub):
    Session = stub
    now = time.time()
    _, fresh = _session(Session)
    tok_ok, _ = _session(Session)
    tok_rev, rev = _session(Session)
    db = Session()
    auth_store.revoke_session(db, rev.id, "revoked")
    db.close()
    tok_exp, _ = _session(Session, now=now - 91 * 86400)
    tok_idle, _ = _session(Session, now=now - 31 * 60)

    status, _, body = await call("GET", "/api/agents", token=tok_ok)
    assert status == 200 and body["kind"] == "session"

    cases = [
        (None, "missing", "Not authenticated"),
        ("garbage", "invalid", "Token expired or invalid"),
        ("xys_" + "A" * 43, "invalid", "Token expired or invalid"),
        (tok_exp, "expired", "Session expired"),
        (tok_rev, "revoked", "Session revoked"),
        (tok_idle, "idle", "Locked after inactivity"),
    ]
    for token, reason, detail in cases:
        status, headers, body = await call("GET", "/api/agents", token=token)
        assert status == 401, reason
        assert headers["x-auth-reason"] == reason
        assert headers["cache-control"] == "no-store"
        assert body == {"detail": detail}


@pytest.mark.anyio
async def test_url_token_ignored_outside_media_routes(stub, caplog):
    Session = stub
    token, row = _session(Session)
    caplog.set_level(logging.INFO, logger="orchestrator")
    for path in (f"/api/agents?token={token}", f"/api/files/p/x.png?token={token}"):
        status, headers, _ = await call("GET", path)
        # a session token is never a URL credential, not even on media routes
        assert status == 401 and headers["x-auth-reason"] in ("missing", "invalid"), path
    status, headers, _ = await call("GET", f"/api/agents?token={token}")
    assert headers["x-auth-reason"] == "missing"
    assert "url_token=1" in caplog.text
    # No token material in any log line.
    assert token not in caplog.text and token[4:20] not in caplog.text


@pytest.mark.anyio
async def test_media_token_scope(stub):
    Session = stub
    token, row = _session(Session)
    media = _media(Session, row.id)
    for method, path in (("GET", "/api/files/p/x.png"), ("HEAD", "/api/thumbs/p/x.jpg"),
                         ("GET", "/api/uploads/a.png")):
        status, _, _ = await call(method, f"{path}?token={media}")
        assert status == 200, (method, path)
    status, _, body = await call("GET", f"/api/files/p/x.png?download=1&token={media}")
    assert status == 200 and body["kind"] == "media" and body["sid"] == row.id
    # Not on other methods / routes, never as Bearer.
    status, headers, _ = await call("POST", f"/api/files/exists-batch?token={media}", json={})
    assert status == 401 and headers["x-auth-reason"] == "missing"
    status, headers, _ = await call("GET", f"/api/agents?token={media}")
    assert status == 401 and headers["x-auth-reason"] == "missing"
    status, headers, _ = await call("GET", "/api/files/p/x.png", token=media)
    assert status == 401 and headers["x-auth-reason"] == "invalid"
    # A Bearer wins over ?token= (and is what gets checked).
    status, _, body = await call("GET", "/api/files/p/x.png?token=junk", token=token)
    assert status == 200 and body["kind"] == "session"
    # Media follows its session.
    db = Session()
    auth_store.revoke_session(db, row.id, "revoked")
    db.close()
    status, headers, _ = await call("GET", f"/api/files/p/x.png?token={media}")
    assert status == 401 and headers["x-auth-reason"] == "revoked"
    # Preview / garbage tokens are not media tokens.
    db = Session()
    preview = create_token(get_jwt_secret(db), extra_claims={"scope": "preview"})
    db.close()
    for bad in (preview, "junk"):
        status, headers, _ = await call("GET", f"/api/thumbs/p/x.jpg?token={bad}")
        assert status == 401 and headers["x-auth-reason"] == "invalid"


@pytest.mark.anyio
async def test_state_auth_and_no_password(stub, tmp_path, monkeypatch):
    status, _, body = await call("GET", "/api/agents")
    assert status == 401
    # No password yet: everything passes, request.state.auth is None.
    nopw_dir = tmp_path / "nopw"
    nopw_dir.mkdir()
    Session, engine = make_auth_db(nopw_dir, password=None)
    enable_auth(monkeypatch, Session)
    status, _, body = await call("GET", "/api/agents")
    assert status == 200 and body == {"reached": True, "kind": None, "sid": None}
    engine.dispose()


# ---- JSON-only exempt routes ----

JSON_ONLY = [
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/set-password"),
    ("POST", "/api/debug/auth-diag"),
    ("POST", "/api/push/ack"),
    ("POST", "/api/hooks/agent-stop"),
    ("POST", "/api/hooks/agent-session-start"),
    ("POST", "/api/hooks/emit-message"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("method,path", JSON_ONLY)
async def test_json_only_exempt_routes(stub, method, path):
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None):
        status, headers, body = await call(method, path, content_type=ctype, body=b"{}")
        assert status == 415, (path, ctype)
        assert body == {"detail": "Content-Type must be application/json"}
        assert "x-auth-reason" not in headers
    for ctype in ("application/json", "application/json; charset=utf-8", "Application/JSON"):
        status, _, body = await call(method, path, content_type=ctype, body=b"{}")
        assert status == 200 and body["reached"], (path, ctype)


@pytest.mark.anyio
async def test_json_only_spares_other_exempt_routes(stub):
    # External webhooks post arbitrary bodies; logout and check parse none.
    for path in ("/api/probe-trigger/x1", "/api/auth/logout", "/api/auth/check"):
        status, _, _ = await call("POST", path, content_type="text/plain", body=b"hi")
        assert status == 200, path
    status, _, _ = await call("GET", "/api/health")
    assert status == 200


# ---- store errors ----

@pytest.mark.anyio
async def test_store_error_is_503_not_401(stub, monkeypatch):
    token, _ = _session(stub)

    def boom(*a, **kw):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(auth_store, "authenticate", boom)
    status, headers, body = await call("GET", "/api/agents", token=token)
    assert status == 503 and body == {"detail": "Auth store unavailable"}
    assert "x-auth-reason" not in headers
    monkeypatch.setattr(main, "get_password_hash", boom)
    status, headers, _ = await call("GET", "/api/agents")
    assert status == 503 and "x-auth-reason" not in headers


# ---- activity ----

@pytest.mark.anyio
async def test_user_activity_header_bumps_only_flagged_requests(stub, monkeypatch):
    Session = stub
    t0 = time.time()
    clock = {"now": t0}
    monkeypatch.setattr(auth_store, "time", types.SimpleNamespace(
        time=lambda: clock["now"], monotonic=time.monotonic))
    token, row = _session(Session, now=t0)

    def stored():
        db = Session()
        try:
            return db.get(auth_store.AuthSession, row.id).last_active_at
        finally:
            db.close()

    clock["now"] = t0 + 20
    await call("GET", "/api/agents", token=token)  # background poll
    await call("GET", "/api/agents", token=token, headers=[("x-user-activity", "true")])
    assert stored() == t0
    await call("GET", "/api/agents", token=token, headers=[("x-user-activity", "1")])
    assert stored() == t0 + 20
    clock["now"] = t0 + 40
    await call("POST", "/api/auth/activity", token=token)  # the ping always counts
    assert stored() == t0 + 40
    # 31 min of background polling only: locked.
    for minutes in (5, 15, 30):
        clock["now"] = t0 + 40 + minutes * 60
        status, _, _ = await call("GET", "/api/agents", token=token)
        assert status == 200
    clock["now"] = t0 + 40 + 31 * 60
    status, headers, _ = await call("GET", "/api/agents", token=token, headers=[("x-user-activity", "1")])
    assert status == 401 and headers["x-auth-reason"] == "idle"
    assert stored() == t0 + 40  # an idle session is not revived by the flag


# ---- DISABLE_AUTH ----

@pytest.mark.anyio
async def test_disable_auth_keeps_everything_inert(stub, monkeypatch):
    monkeypatch.setenv("DISABLE_AUTH", "1")
    status, _, body = await call("POST", "/api/auth/login", content_type="text/plain", body=b"x")
    assert status == 200 and body["reached"]
    for path in ("/api/agents", "/api/agents?token=junk", "/api/auth/sessions", "/api/settings/auth"):
        status, headers, _ = await call("GET", path)
        assert status == 200 and "x-auth-reason" not in headers, path
    status, _, _ = await call("GET", "/api/agents", token="garbage")
    assert status == 200


# ---- real routing: non-session 401s carry no reason ----

@pytest.mark.anyio
async def test_non_session_401s_have_no_reason_header(store):
    Session = store
    token, _ = _session(Session)
    status, headers, body = await call("GET", "/api/preview/t/bad.token/proj/index.html")
    assert status == 401 and body == {"detail": "Invalid preview token"}
    assert "x-auth-reason" not in headers
    status, headers, body = await call("GET", "/api/preview/p/" + "0" * 32 + "/proj/6006/")
    assert status == 401 and "x-auth-reason" not in headers
    status, headers, body = await call("POST", "/api/auth/login", json={"password": "nope"})
    assert status == 401 and body["detail"] == "Wrong password" and "x-auth-reason" not in headers
    status, headers, body = await call(
        "POST", "/api/auth/change-password", token=token,
        json={"current_password": "nope", "new_password": "whatever-123"})
    assert status == 403 and "x-auth-reason" not in headers


@pytest.mark.anyio
async def test_preview_token_cut_off_by_not_before(store):
    Session = store
    db = Session()
    secret = get_jwt_secret(db)
    from models import Project
    db.add(Project(name="proj", display_name="proj", path="/nonexistent-preview-proj"))
    db.commit()
    tok = create_token(secret, expires_minutes=60, extra_claims={"scope": "preview", "project": "proj"},
                       now=T_REAL() - 10)
    db.close()
    status, _, _ = await call("GET", f"/api/preview/t/{tok}/proj/index.html")
    assert status == 404  # token accepted; the file just isn't there
    db = Session()
    auth_store.bump_not_before(db)
    db.close()
    status, headers, body = await call("GET", f"/api/preview/t/{tok}/proj/index.html")
    assert status == 401 and body == {"detail": "Invalid preview token"}
    assert "x-auth-reason" not in headers
