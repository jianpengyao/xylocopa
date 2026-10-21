"""Tests for request_guard.RequestGuardMiddleware (Host allowlist + Origin check)."""

import json
import logging
import os
import subprocess
import sys

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import request_guard
from request_guard import RequestGuardMiddleware, parse_origin, split_host
from tests.asgi_helpers import asgi_request

NODE_SHARE = "100.64.0.10:3000"          # iPhone PWA via Tailscale node share
NODE_SHARE_ORIGIN = "https://100.64.0.10:3000"


async def _ok(request):
    return PlainTextResponse("reached")


async def _ws(websocket):
    await websocket.accept()
    await websocket.send_text("hi")
    await websocket.close()


def _guarded(**kw):
    """Minimal app behind the guard: every path answers 200 "reached"."""
    kw.setdefault("hostname", "box.dept.example.edu")
    inner = Starlette(routes=[
        Route("/{path:path}", _ok, methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]),
        WebSocketRoute("/{path:path}", _ws),
    ])
    return RequestGuardMiddleware(inner, **kw)


async def _status(app, method, path, host=None, origin=None, extra=()):
    headers = list(extra)
    if host is not None:
        headers.append(("host", host))
    if origin is not None:
        headers.append(("origin", origin))
    status, _, body = await asgi_request(app, method, path, headers)
    return status, body


# ---- parsing ----

def test_split_host_forms():
    assert split_host("100.64.0.10:3000") == ("100.64.0.10", 3000)
    assert split_host("LocalHost.:8080") == ("localhost", 8080)
    assert split_host("[FD7A:115C:A1E0::1]:3000") == ("fd7a:115c:a1e0::1", 3000)
    assert split_host("[::1]") == ("::1", None)
    assert split_host("example.com:") == ("example.com", None)
    for bad in ("", "[::1", "[zz::1]:80", "[::1]x", "h:99999", "h:8o", "a b", "evil.com/x.ts.net", "u@h",
                # unbracketed IPv6: not Host syntax, ambiguous with a port
                "::1", "fe80::1", "::1:8080",
                # IPv6 zone IDs: ipaddress accepts "?"/"#" after the "%"
                "fe80::1%eth0", "fe80::1%?", "::1%#", "[fe80::1%eth0]:3000", "[fe80::1%?]:3000"):
        assert split_host(bad) is None, bad


def test_parse_origin_default_ports():
    assert parse_origin("https://100.64.0.10:3000") == ("https", "100.64.0.10", 3000)
    assert parse_origin("https://box.ts.net") == ("https", "box.ts.net", 443)
    assert parse_origin("HTTP://LOCALHOST") == ("http", "localhost", 80)
    assert parse_origin("https://[fd7a::1]:3000") == ("https", "fd7a::1", 3000)
    assert parse_origin("chrome-extension://abc") == ("chrome-extension", "abc", None)
    for bad in ("", "null", "not a url", "https://", "https://h:99999"):
        assert parse_origin(bad) is None, bad


# ---- Host allowlist ----

@pytest.mark.anyio
@pytest.mark.parametrize("host", [
    None,                                   # no Host header
    NODE_SHARE, "127.0.0.1", "127.0.0.1:8080", "192.168.1.5:3000", "8.8.8.8",
    "[::1]:8080", "[::1]", "[fd7a:115c:a1e0:ab12:4843:cd96:6263:f17d]:3000",
    "localhost", "localhost:3000", "LOCALHOST:3000", "localhost.", "app.localhost:5173",
    "box.dept.example.edu:3000", "BOX.DEPT.EXAMPLE.EDU", "box", "box.local:3000",
    "box.dept.example.edu.local",
    "machine.tail1234.ts.net", "machine.tail1234.ts.net:3000",
])
async def test_host_allowed(host):
    status, body = await _status(_guarded(), "GET", "/api/health", host=host)
    assert status == 200 and body == b"reached"


@pytest.mark.anyio
@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:3000", "attacker.com", "ts.net.evil.com", "evilts.net",
    "localhost.evil.com", "box.evil.com", "notbox", "[::1", "h:99999", "evil.com/x.ts.net",
    "::1", "fe80::1%?", "fe80::1%#", "::1%?", "[fe80::1%?]:3000", "[::1%25x]:8080", "x?", "x#y",
])
async def test_host_rejected(host):
    status, body = await _status(_guarded(), "GET", "/api/health", host=host)
    assert status == 403 and body == b"Host not allowed"


@pytest.mark.anyio
async def test_duplicate_host_rejected():
    status, _ = await _status(_guarded(), "GET", "/", extra=[("host", "127.0.0.1"), ("host", "evil.example")])
    assert status == 403


@pytest.mark.anyio
async def test_host_rejected_for_every_method():
    for method in ("GET", "HEAD", "OPTIONS", "POST", "DELETE"):
        status, _ = await _status(_guarded(), method, "/api/hooks/agent-stop", host="evil.example")
        assert status == 403, method


@pytest.mark.anyio
async def test_extra_allowed_hosts():
    app = _guarded(allowed_hosts=["myhost.example", ".corp.example", "withport.example:3000", " "])
    for host, expected in [
        ("myhost.example", 200), ("myhost.example:3000", 200), ("withport.example", 200),
        ("a.corp.example", 200), ("a.b.corp.example:3000", 200), ("corp.example", 200),
        ("xcorp.example", 403), ("other.example", 403),
    ]:
        assert (await _status(app, "GET", "/", host=host))[0] == expected, host


@pytest.mark.anyio
async def test_allowed_hosts_wildcard():
    app = _guarded(allowed_hosts=["*"])
    assert (await _status(app, "GET", "/", host="anything.example"))[0] == 200
    lone_dot = _guarded(allowed_hosts=[".", ".."])
    assert (await _status(lone_dot, "GET", "/", host="anything.example"))[0] == 403


@pytest.mark.anyio
async def test_local_hostname_from_gethostname(monkeypatch):
    monkeypatch.setattr(request_guard.socket, "gethostname", lambda: "Box01.dept.example.edu")
    app = _guarded(hostname=None)
    for host in ("box01.dept.example.edu:3000", "box01", "box01.local", "BOX01.LOCAL:3000"):
        assert (await _status(app, "GET", "/", host=host))[0] == 200, host
    assert (await _status(app, "GET", "/", host="box02.local"))[0] == 403


# ---- Origin check ----

@pytest.mark.anyio
async def test_no_origin_allowed_for_unsafe_methods():
    """curl, Claude Code HTTP hooks (axios), the MCP server: no Origin header."""
    app = _guarded()
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert (await _status(app, method, "/api/hooks/agent-stop", host="localhost:8080"))[0] == 200


@pytest.mark.anyio
async def test_same_origin_iphone_node_share():
    status, body = await _status(_guarded(), "POST", "/api/agents/x/messages",
                                 host=NODE_SHARE, origin=NODE_SHARE_ORIGIN)
    assert status == 200 and body == b"reached"


@pytest.mark.anyio
async def test_send_beacon_same_origin_post():
    """navigator.sendBeacon → text/plain POST carrying the page's Origin."""
    status, _ = await _status(_guarded(), "POST", "/api/debug/auth-diag", host=NODE_SHARE,
                              origin=NODE_SHARE_ORIGIN,
                              extra=[("content-type", "text/plain;charset=UTF-8")])
    assert status == 200


@pytest.mark.anyio
@pytest.mark.parametrize("host,origin,expected", [
    (NODE_SHARE, "https://evil.example", 403),
    (NODE_SHARE, "http://100.64.0.10:5173", 403),     # same host, other port
    (NODE_SHARE, "https://100.64.0.11:3000", 403),
    ("localhost", "http://localhost", 200),
    ("localhost", "https://localhost", 200),
    ("localhost", "http://localhost:3000", 403),
    ("localhost:3000", "https://localhost:3000", 200),
    ("box.tail.ts.net", "https://box.tail.ts.net", 200),
    ("box.tail.ts.net", "https://box.tail.ts.net:443", 200),
    ("box.tail.ts.net:443", "https://box.tail.ts.net", 200),
    ("box.tail.ts.net:443", "http://box.tail.ts.net", 403),
    ("box.tail.ts.net", "https://other.tail.ts.net", 403),
    ("[fd7a:115c:a1e0::1]:3000", "https://[fd7a:115c:a1e0::1]:3000", 200),
    ("[FD7A:115C:A1E0::1]:3000", "https://[fd7a:115c:a1e0:0::1]:3000", 200),
    ("[fd7a:115c:a1e0::1]:3000", "https://[fd7a:115c:a1e0::2]:3000", 403),
    (NODE_SHARE, "not a url", 403),
    (NODE_SHARE, "https://", 403),
    (None, NODE_SHARE_ORIGIN, 403),                     # Origin but no Host to compare
])
async def test_origin_match(host, origin, expected):
    status, body = await _status(_guarded(), "POST", "/api/x", host=host, origin=origin)
    assert status == expected
    if expected == 403:
        assert body == b"Cross-origin request blocked"


@pytest.mark.anyio
async def test_safe_methods_skip_origin_check():
    app = _guarded()
    for method in ("GET", "HEAD", "OPTIONS"):
        status, _ = await _status(app, method, "/api/health", host=NODE_SHARE, origin="https://evil.example")
        assert status == 200, method


@pytest.mark.anyio
async def test_cors_origins_listed():
    app = _guarded(cors_origins=["http://localhost:3000", "https://dash.example/"])
    assert (await _status(app, "POST", "/x", host="127.0.0.1:8080", origin="http://localhost:3000"))[0] == 200
    assert (await _status(app, "POST", "/x", host="127.0.0.1:8080", origin="https://dash.example"))[0] == 200
    assert (await _status(app, "POST", "/x", host="127.0.0.1:8080", origin="http://localhost:3001"))[0] == 403
    assert (await _status(app, "POST", "/x", host="127.0.0.1:8080", origin="https://localhost:3000"))[0] == 403
    star = _guarded(cors_origins=["*"])
    assert (await _status(star, "POST", "/x", host="127.0.0.1:8080", origin="https://evil.example"))[0] == 200


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["POST", "GET", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("path,expected", [
    ("/api/preview/p/sig/proj/5173/api/data", 200),
    ("/api/preview/t/tok/proj/index.html", 200),
    ("/api/preview/token", 403),
    ("/api/agents", 403),
    ("/api/files/proj/notes.md", 403),
    ("/api/hooks/agent-stop", 403),
    ("/api/previewX/p/x", 403),
])
async def test_null_origin_only_for_sandboxed_previews(method, path, expected):
    """Reads included: CORSMiddleware grants "null" on every path, so a GET
    from another site's sandboxed iframe would otherwise be readable."""
    status, _ = await _status(_guarded(cors_origins=["null"]), method, path, host=NODE_SHARE, origin="null")
    assert status == expected


@pytest.mark.anyio
async def test_duplicate_origin_rejected():
    status, _ = await _status(_guarded(), "POST", "/x", host=NODE_SHARE,
                              extra=[("origin", NODE_SHARE_ORIGIN), ("origin", "https://evil.example")])
    assert status == 403


@pytest.mark.anyio
async def test_non_http_scopes_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await RequestGuardMiddleware(inner)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


# ---- agent hook channel: loopback Host on the backend port only ----

@pytest.mark.anyio
@pytest.mark.parametrize("host", [
    None, "localhost:8080", "127.0.0.1:8080", "[::1]:8080", "localhost", "127.0.0.1", "LOCALHOST:8080",
])
async def test_hooks_allowed_from_loopback(host):
    """Claude Code HTTP hooks, session-start.sh and the MCP server all call
    http://localhost:PORT (or 127.0.0.1:PORT) directly."""
    status, body = await _status(_guarded(hook_port=8080), "POST", "/api/hooks/agent-stop", host=host)
    assert status == 200 and body == b"reached"


@pytest.mark.anyio
@pytest.mark.parametrize("host", [
    NODE_SHARE,                  # proxied by the HTTPS frontend (browser Host kept)
    "localhost:3000",            # browser on the host itself, through the proxy
    "127.0.0.1:3000",
    "192.168.1.5:8080",          # LAN address on the backend port (BACKEND_HOST=0.0.0.0)
    "box.tail1234.ts.net",
    "box.dept.example.edu:8080",
])
async def test_hooks_refused_unless_loopback_backend_port(host):
    status, body = await _status(_guarded(hook_port=8080), "POST", "/api/hooks/agent-stop", host=host)
    assert status == 403 and body == b"Hook endpoints are loopback-only"
    # Only the hook channel: the same Host reaches every other route.
    status, _ = await _status(_guarded(hook_port=8080), "GET", "/api/agents", host=host)
    assert status == 200


@pytest.mark.anyio
async def test_hook_rule_off_without_port():
    status, _ = await _status(_guarded(), "POST", "/api/hooks/agent-stop", host=NODE_SHARE)
    assert status == 200


# ---- WebSocket handshakes ----

def _ws_connect(app, host, path, origin=None):
    """TestClient.websocket_connect ignores base_url (Host "testserver") and
    can't parse an IPv6 netloc; an explicit Host header covers both."""
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    return TestClient(app).websocket_connect(path, headers=headers)


def test_websocket_same_origin_and_no_origin_accepted():
    app = _guarded()
    with _ws_connect(app, NODE_SHARE, "/ws/status", NODE_SHARE_ORIGIN) as ws:
        assert ws.receive_text() == "hi"
    with _ws_connect(app, NODE_SHARE, "/ws/terminal/abcd1234") as ws:
        assert ws.receive_text() == "hi"
    with _ws_connect(app, "[fd7a:115c:a1e0::1]:3000", "/ws/status", "https://[fd7a:115c:a1e0::1]:3000") as ws:
        assert ws.receive_text() == "hi"


@pytest.mark.parametrize("path,origin", [
    ("/ws/status", "https://evil.example"),
    ("/ws/terminal/abcd1234", "https://evil.example"),
    ("/ws/terminal/abcd1234", "http://100.64.0.10:5173"),
    ("/ws/status", "null"),
])
def test_websocket_cross_origin_rejected_before_accept(path, origin):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_connect(_guarded(), NODE_SHARE, path, origin):
            pass
    assert exc.value.code == 1008


def test_websocket_null_origin_preview_accepted():
    with _ws_connect(_guarded(), NODE_SHARE, "/api/preview/p/sig/proj/5173/ws", "null") as ws:
        assert ws.receive_text() == "hi"


def test_websocket_bad_host_rejected():
    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_connect(_guarded(), "evil.example:3000", "/ws/status"):
            pass
    assert exc.value.code == 1008


# ---- logging ----

@pytest.mark.anyio
async def test_rejections_logged_rate_limited(caplog, monkeypatch):
    clock = [1000.0]
    # The module's own alias: patching time.monotonic would also freeze the
    # asyncio loop clock (any awaited timer would hang the test).
    monkeypatch.setattr(request_guard, "_monotonic", lambda: clock[0])
    app = _guarded()
    caplog.set_level(logging.WARNING, logger="orchestrator")

    def lines():
        return [r.getMessage() for r in caplog.records if "REQUEST_GUARD" in r.getMessage()]

    await _status(app, "GET", "/api/health", host="evil.example")
    assert len(lines()) == 1
    msg = lines()[0]
    assert "reject host" in msg and "GET /api/health" in msg
    assert "host='evil.example'" in msg and "client=127.0.0.1:50000" in msg
    assert caplog.records[-1].levelno == logging.WARNING

    await _status(app, "GET", "/other", host="evil.example")      # same key → suppressed
    assert len(lines()) == 1
    await _status(app, "POST", "/x", host=NODE_SHARE, origin="https://evil.example")
    assert len(lines()) == 2 and "reject origin" in lines()[-1] and "origin='https://evil.example'" in lines()[-1]

    clock[0] += 61
    await _status(app, "GET", "/api/health", host="evil.example")
    assert len(lines()) == 3 and "(+1 similar in the last minute)" in lines()[-1]


# ---- installed in main.app ----

def test_guard_is_outermost_middleware():
    import config
    from main import app
    guard = app.user_middleware[0]
    assert guard.cls is RequestGuardMiddleware
    assert guard.kwargs == {
        "allowed_hosts": config.XY_ALLOWED_HOSTS,
        "cors_origins": config.CORS_ORIGINS,
        "hook_port": config.PORT,
    }


def test_allowed_hosts_env_parsing():
    """config.XY_ALLOWED_HOSTS: comma-separated, blanks dropped. Fresh
    interpreter so the reload can't leak into other tests' config."""
    env = dict(os.environ, XY_ALLOWED_HOSTS=" a.example, .corp.example ,, ")
    out = subprocess.run(
        [sys.executable, "-c", "import json, config; print(json.dumps(config.XY_ALLOWED_HOSTS))"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == ["a.example", ".corp.example"]


@pytest.mark.anyio
async def test_main_app_guard_active_with_auth_disabled(monkeypatch):
    """DISABLE_AUTH=1 skips auth, never the guard: a drive-by text/plain POST
    to the cert endpoint is refused before it reaches the handler."""
    from main import app

    monkeypatch.setenv("DISABLE_AUTH", "1")
    called = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: called.append(a))

    status, _, body = await asgi_request(
        app, "POST", "/api/cert/regenerate",
        [("host", NODE_SHARE), ("origin", "https://evil.example"), ("content-type", "text/plain")],
        body=b'{"ips": ["1.2.3.4"], "dns": []}',
    )
    assert status == 403 and body == b"Cross-origin request blocked"
    assert called == []

    status, _, body = await asgi_request(app, "GET", "/api/health", [("host", "rebind.attacker.example")])
    assert status == 403 and body == b"Host not allowed"


@pytest.mark.anyio
async def test_main_app_null_origin_cannot_read_api(monkeypatch):
    """With DISABLE_AUTH=1 a sandboxed iframe on any site (Origin: null) must
    not get a readable /api response, although CORSMiddleware lists "null"."""
    from main import app

    monkeypatch.setenv("DISABLE_AUTH", "1")
    for path in ("/api/agents", "/api/projects", "/api/system/token-usage"):
        status, headers, body = await asgi_request(
            app, "GET", path, [("host", NODE_SHARE), ("origin", "null")])
        assert status == 403 and body == b"Cross-origin request blocked", path
        assert "access-control-allow-origin" not in headers


@pytest.mark.anyio
async def test_main_app_hooks_refused_through_proxy(monkeypatch):
    """A hook request carrying the browser's Host (i.e. one that came through
    the HTTPS frontend proxy, e.g. /ws/../api/hooks/x) is refused before the
    handler. Loopback callers are covered by the hook tests (Host localhost)."""
    from main import app

    monkeypatch.setenv("DISABLE_AUTH", "1")
    for host in (NODE_SHARE, "localhost:3000"):
        status, _, body = await asgi_request(
            app, "POST", "/api/hooks/agent-stop", [("host", host), ("content-type", "application/json")], b"{}")
        assert status == 403 and body == b"Hook endpoints are loopback-only", host


def test_main_app_websocket_cross_origin_rejected(monkeypatch):
    """Cross-site WebSocket hijacking of /ws/terminal and /ws/status is refused
    before the handler runs (which, with DISABLE_AUTH=1, would accept)."""
    import ws_auth
    from main import app

    monkeypatch.setenv("DISABLE_AUTH", "1")
    # Both handlers authorize through ws_auth.authorize_ws first.
    monkeypatch.setattr(ws_auth, "authorize_ws", lambda ws: pytest.fail("handler reached"))
    for path in ("/ws/terminal/abcd1234", "/ws/status"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with _ws_connect(app, NODE_SHARE, path, "https://evil.example"):
                pass
        assert exc.value.code == 1008
