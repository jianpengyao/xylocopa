"""Auth-exempt surface of main.auth_middleware.

With auth ENABLED (password set in a temp DB, DISABLE_AUTH unset) every /api
route must answer 401 without a token, except exactly the reviewed public
set below — so a prefix slip (a bare startswith("/api/cert") once exempted
POST /api/cert/regenerate) or a new route under an exempt family fails CI.

Routing is replaced by a stub: a request the middlewares let through answers
200 "reached" without running any endpoint, so walking every route has no
side effects.
"""

import re
import time

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.responses import PlainTextResponse

import auth_store
import database
import main
import routers.preview
from auth import create_token, get_jwt_secret, set_config, set_password_hash
from config import PORT
from models import Base
from request_guard import RequestGuardMiddleware
from tests.asgi_helpers import asgi_request

# Reviewed public (method, route path) set: reachable WITHOUT a token once
# auth is on. Extending it is a security decision — review before adding.
PUBLIC_ROUTES = {
    # login / first-time setup (exact paths: every other /api/auth/* route,
    # change-password included, needs a session)
    ("POST", "/api/auth/check"),
    ("POST", "/api/auth/set-password"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/health"),
    # mobile trust setup (GET only — POST /api/cert/regenerate needs auth)
    ("GET", "/api/cert"),
    ("GET", "/api/cert/info"),
    ("GET", "/api/webclip"),
    # agent hook channel (loopback; the HTTPS proxy refuses /api/hooks/)
    ("POST", "/api/hooks/agent-session-end"),
    ("POST", "/api/hooks/agent-user-prompt"),
    ("POST", "/api/hooks/agent-stop"),
    ("POST", "/api/hooks/agent-post-compact"),
    ("POST", "/api/hooks/agent-tool-activity"),
    ("POST", "/api/hooks/agent-permission"),
    ("POST", "/api/hooks/agent-permission-request"),
    ("POST", "/api/hooks/agent-session-start"),
    ("POST", "/api/hooks/emit-message"),
    # diagnostics the login/locked screen still sends
    ("POST", "/api/debug/auth-diag"),
    ("GET", "/api/debug/clear-cache"),
    ("GET", "/api/debug/mem-introspect"),
    # service-worker push receipt
    ("POST", "/api/push/ack"),
    # capability URLs: the secret is in the path
    ("POST", "/api/probe-trigger/{token}"),
    ("GET", "/api/preview/t/{token}/{project}/{path:path}"),
    ("GET", "/api/preview/p/{sig}/{project}/{port}/{path:path}"),
    ("HEAD", "/api/preview/p/{sig}/{project}/{port}/{path:path}"),
    ("POST", "/api/preview/p/{sig}/{project}/{port}/{path:path}"),
    # OAuth redirect target
    ("GET", "/api/dropbox/callback"),
}

# The http auth middleware never sees WebSocket handshakes; each of these
# handlers authenticates itself (?token= / path signature).
WEBSOCKET_ROUTES = {
    "/ws/status",
    "/ws/terminal/{agent_id}",
    "/api/preview/p/{sig}/{project}/{port}/{path:path}",
}

# Loopback on the backend port: what agents' hook calls carry (request_guard
# refuses /api/hooks/* under any other Host).
HOST = [("host", f"localhost:{PORT}")]
# Exempt routes that parse a body are JSON-only with auth on; every
# legitimate caller sends this, so the walk does too.
JSON = [("content-type", "application/json")]


@pytest.fixture(scope="module")
def _password_db(tmp_path_factory):
    """Temp DB with a password set (bcrypt once per module)."""
    path = tmp_path_factory.mktemp("auth") / "auth.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    set_password_hash(db, "correct horse battery staple")
    secret = get_jwt_secret(db)
    db.close()
    yield Session, secret
    engine.dispose()


@pytest.fixture()
def auth_on(_password_db, monkeypatch):
    """Auth enabled against the temp DB; returns the JWT secret."""
    Session, secret = _password_db

    async def reached(scope, receive, send):
        await PlainTextResponse("reached")(scope, receive, send)

    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(routers.preview, "SessionLocal", Session)
    monkeypatch.setattr(main.app.router, "middleware_stack", reached)
    return secret


def _concrete(path: str) -> str:
    path = re.sub(r"\{[^}]+:path\}", "a/b.html", path)
    return re.sub(r"\{[^}]+\}", "x1", path)


def _api_routes():
    for route in main.app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            for method in sorted(route.methods):
                yield method, route.path


@pytest.mark.anyio
async def test_every_api_route_requires_auth_except_public_set(auth_on):
    seen, wrong = set(), []
    for method, path in _api_routes():
        seen.add((method, path))
        status, _, _ = await asgi_request(main.app, method, _concrete(path), HOST + JSON)
        expected = 200 if (method, path) in PUBLIC_ROUTES else 401
        if status != expected:
            wrong.append(f"{method} {path}: {status} (expected {expected})")
    assert len(seen) > 150  # sanity: the walk saw the whole API
    assert not wrong, (
        "Auth-exempt surface changed. If a new public route is intended, review it "
        "and add it to PUBLIC_ROUTES:\n" + "\n".join(wrong)
    )
    assert PUBLIC_ROUTES <= seen, f"stale PUBLIC_ROUTES entries: {PUBLIC_ROUTES - seen}"


def test_websocket_routes_are_known():
    ws_paths = {r.path for r in main.app.routes if isinstance(r, APIWebSocketRoute)}
    assert ws_paths == WEBSOCKET_ROUTES, (
        "New WebSocket route: the http auth middleware does not cover WebSockets — "
        "authenticate in the handler, then add it to WEBSOCKET_ROUTES."
    )


@pytest.mark.anyio
@pytest.mark.parametrize("method,raw_path,expected", [
    # /api/cert family: exact paths, GET/HEAD only
    ("GET", "/api/cert", 200),
    ("HEAD", "/api/cert", 200),
    ("GET", "/api/cert/info", 200),
    ("HEAD", "/api/cert/info", 200),
    ("POST", "/api/cert", 401),
    ("POST", "/api/cert/info", 401),
    ("POST", "/api/cert/regenerate", 401),
    ("GET", "/api/cert/regenerate", 401),
    ("GET", "/api/certX", 401),
    ("GET", "/api/cert/", 401),
    ("GET", "/api/cert/info/x", 401),
    ("POST", "/api/cert//regenerate", 401),
    # percent-encoding: uvicorn decodes the path, so these are the routes
    # they spell — /api/cert stays public, regenerate stays protected
    ("GET", "/api/%63ert", 200),
    ("POST", "/api/%63ert/regenerate", 401),
    ("POST", "/api/cert%2Fregenerate", 401),
    # hooks family: prefix with trailing slash only
    ("POST", "/api/hooks/agent-stop", 200),
    ("POST", "/api/%68ooks/agent-stop", 200),
    ("POST", "/api/hooks", 401),
    ("POST", "/api/hooksX", 401),
    ("POST", "/api/hooksX/agent-stop", 401),
    ("POST", "/api//hooks/agent-stop", 401),
    # other exact entries don't extend to look-alikes
    ("GET", "/api/healthz", 401),
    ("GET", "/api/health/", 401),
    ("GET", "/api/health/../agents", 401),
    ("GET", "/api/webclipX", 401),
    ("POST", "/api/debug/auth-diagX", 401),
    ("POST", "/api/debug/kb-log", 401),
    ("POST", "/api/push/ack/x", 401),
    ("POST", "/api/push/subscribe", 401),
    ("GET", "/api/dropbox/callbackX", 401),
    ("POST", "/api/preview/token", 401),
    ("GET", "/api/preview/tX/a/b/c", 401),
    ("POST", "/api/probe-triggerX", 401),
    ("POST", "/api/auth", 401),
    ("GET", "/api/agents", 401),
])
async def test_exempt_matching_edge_cases(auth_on, method, raw_path, expected):
    status, _, _ = await asgi_request(main.app, method, raw_path, HOST + JSON)
    assert status == expected


# Host values Starlette can't turn back into the routed path: request.url is
# rebuilt as f"{scheme}://{host}{path}", so a "?" or "#" in Host (smuggled in
# an IPv6 zone ID, which ipaddress accepts) shortened request.url.path to ""
# — "not /api/" — and auth was skipped while the router still ran the route.
ZONE_ID_HOSTS = ["fe80::1%?", "fe80::1%#", "::1%?", "[fe80::1%?]:3000"]


@pytest.mark.anyio
@pytest.mark.parametrize("host", ZONE_ID_HOSTS)
async def test_zone_id_host_refused_by_guard(auth_on, host):
    for method, path in (("GET", "/api/projects"), ("POST", "/api/cert/regenerate")):
        status, _, body = await asgi_request(main.app, method, path, [("host", host)])
        assert status == 403 and body == b"Host not allowed", (method, path)


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["fe80::1%?", "fe80::1%#", "::1%?", "x?", "x#"])
async def test_auth_decides_on_routed_path_not_host(auth_on, monkeypatch, host):
    """Second layer: even with the guard out of the way, auth_middleware uses
    scope["path"] (what the router matches), not request.url.path."""
    monkeypatch.setattr(RequestGuardMiddleware, "check", lambda self, scope: None)
    for method, path in (("GET", "/api/projects"), ("GET", "/api/agents"), ("POST", "/api/cert/regenerate")):
        status, _, _ = await asgi_request(main.app, method, path, [("host", host)])
        assert status == 401, (method, path)


@pytest.mark.anyio
async def test_session_and_media_credentials(auth_on, _password_db):
    """Session tokens authenticate only as Bearer; ?token= is honoured only
    for scope=media tokens on media GETs (spec: tokens out of URLs)."""
    Session, secret = _password_db
    db = Session()
    try:
        token, row = auth_store.create_session(db, user_agent="pytest")
        media, _ = auth_store.mint_media_token(
            db, auth_store.AuthResult(ok=True, kind="session", session_id=row.id))
        # Legacy window anchored a minute ahead, so a JWT minted now is
        # "issued before Step 3".
        set_config(db, "auth_sessions_epoch", repr(time.time() + 60))
    finally:
        db.close()
    bearer = lambda t: HOST + [("authorization", f"Bearer {t}")]  # noqa: E731

    status, _, body = await asgi_request(main.app, "POST", "/api/cert/regenerate", bearer(token) + JSON)
    assert status == 200 and body == b"reached"
    status, headers, _ = await asgi_request(main.app, "GET", f"/api/agents?token={token}", HOST)
    assert status == 401 and headers["x-auth-reason"] == "missing"
    status, _, body = await asgi_request(main.app, "GET", f"/api/files/p/x.png?token={media}", HOST)
    assert status == 200 and body == b"reached"
    status, headers, _ = await asgi_request(main.app, "GET", "/api/agents", bearer(media))
    assert status == 401 and headers["x-auth-reason"] == "invalid"
    status, _, _ = await asgi_request(
        main.app, "POST", f"/api/files/exists-batch?token={media}", HOST + JSON)
    assert status == 401

    legacy = create_token(auth_on)
    status, _, body = await asgi_request(main.app, "GET", "/api/agents", bearer(legacy))
    assert status == 200 and body == b"reached"
    preview = create_token(auth_on, extra_claims={"scope": "preview"})
    status, headers, _ = await asgi_request(main.app, "GET", "/api/agents", bearer(preview))
    assert status == 401 and headers["x-auth-reason"] == "invalid"
