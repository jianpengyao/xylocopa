"""Web-app preview — serve agent-built static web apps for sandboxed in-chat
preview (iframe), with console-capture injection for the debug drawer.

Security model:
- Preview URLs carry a short-lived, preview-scoped token in the PATH (not a
  query param) so relative subresource references (./app.js, ./data.json)
  inherit it automatically inside the iframe.
- Preview-scoped tokens are rejected by the main auth middleware
  (auth_store.authenticate refuses a JWT with a "scope" claim as Bearer),
  so a leaked preview URL never grants general API access.
- Every response carries a CSP `sandbox` header, forcing an opaque origin
  even when a preview URL is opened directly in a new tab — agent-written JS
  can never read the app origin's localStorage (where the opaque session
  token lives).
"""

import hashlib
import hmac
import logging
import mimetypes
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

import auth_store
from auth import create_token, decode_token, get_jwt_secret
from config import PORT
from database import SessionLocal, get_db
from models import WebApp
from route_helpers import get_project_or_404

logger = logging.getLogger("orchestrator")

router = APIRouter(tags=["preview"])

PREVIEW_TOKEN_TTL_MINUTES = 12 * 60

_PREVIEW_HEADERS = {
    # Force an opaque origin even on direct navigation — preview content must
    # never script against the app origin.
    "Content-Security-Policy": (
        "sandbox allow-scripts allow-forms allow-modals allow-popups "
        "allow-pointer-lock allow-downloads"
    ),
    # The opaque-origin document fetch()es its own data files with
    # Origin: null — allow it. Access control is the token in the path.
    "Access-Control-Allow-Origin": "*",
    # Agents iterate on these files; always serve fresh.
    "Cache-Control": "no-store",
}

# Injected into every served HTML document. Two jobs:
# 1. Storage shim — the opaque-origin sandbox makes localStorage/
#    sessionStorage THROW on access, which kills apps that touch them during
#    init (TensorBoard's feature-flag store aborts all data loading).
#    Replace them with in-memory stand-ins before app scripts run; settings
#    simply don't persist across reloads.
# 2. Console capture — mirrors console.* and uncaught errors to the parent
#    frame via postMessage for the debug drawer. postMessage targets '*'
#    because an opaque origin cannot name its parent; the parent side
#    filters by e.source === iframe.contentWindow.
_CONSOLE_CAPTURE_JS = """<script>
(function () {
  if (window.__xyConsoleHooked) return;
  window.__xyConsoleHooked = true;
  function memStorage() {
    var mem = {};
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null; },
      setItem: function (k, v) { mem[k] = String(v); },
      removeItem: function (k) { delete mem[k]; },
      clear: function () { mem = {}; },
      key: function (i) { var ks = Object.keys(mem); return i < ks.length ? ks[i] : null; },
      get length() { return Object.keys(mem).length; }
    };
  }
  try { void window.localStorage.length; } catch (e) {
    try {
      Object.defineProperty(window, "localStorage", { value: memStorage(), configurable: true });
      Object.defineProperty(window, "sessionStorage", { value: memStorage(), configurable: true });
    } catch (e2) {}
  }
  try { void document.cookie; } catch (e) {
    try {
      Object.defineProperty(document, "cookie", {
        get: function () { return ""; }, set: function () {}, configurable: true
      });
    } catch (e2) {}
  }
  // Worker constructor is blocked for opaque-origin documents (script URL is
  // cross-origin to the sandbox). Fall back to fetching the script (sync XHR
  // — must stay synchronous to preserve constructor semantics) and booting
  // the worker from a same-origin blob: URL. TensorBoard's chart renderer
  // depends on this.
  try {
    var NativeWorker = window.Worker;
    if (NativeWorker) {
      var WrappedWorker = function (url, opts) {
        try {
          return new NativeWorker(url, opts);
        } catch (err) {
          var xhr = new XMLHttpRequest();
          xhr.open("GET", url, false);
          xhr.send(null);
          if (xhr.status < 200 || xhr.status >= 300) throw err;
          var blob = new Blob([xhr.responseText], { type: "text/javascript" });
          return new NativeWorker(URL.createObjectURL(blob), opts);
        }
      };
      WrappedWorker.prototype = NativeWorker.prototype;
      window.Worker = WrappedWorker;
    }
  } catch (e) {}
  var MAX_LEN = 5000;
  function send(level, args) {
    var parts = [];
    for (var i = 0; i < args.length; i++) {
      var a = args[i];
      try {
        if (typeof a === "string") parts.push(a);
        else if (a instanceof Error) parts.push(a.stack || String(a));
        else parts.push(JSON.stringify(a));
      } catch (e) {
        try { parts.push(String(a)); } catch (e2) { parts.push("[unserializable]"); }
      }
    }
    var text = parts.join(" ");
    if (text.length > MAX_LEN) text = text.slice(0, MAX_LEN) + "…";
    try {
      window.parent.postMessage({ __xy_preview: 1, kind: "console", level: level, text: text, ts: Date.now() }, "*");
    } catch (e) {}
  }
  ["log", "info", "warn", "error", "debug"].forEach(function (m) {
    var orig = console[m];
    console[m] = function () {
      send(m, arguments);
      if (orig) orig.apply(console, arguments);
    };
  });
  window.addEventListener("error", function (e) {
    send("error", [e.message + " (" + (e.filename || "?") + ":" + (e.lineno || "?") + ")"]);
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    send("error", ["Unhandled rejection: " + ((r && (r.stack || r.message)) || String(r))]);
  });
})();
</script>"""


def _inject_console_capture(html: str) -> str:
    """Insert the capture script as early as possible so it hooks console
    before the app's own scripts run."""
    lower = html.lower()
    for tag in ("<head>", "<head "):
        idx = lower.find(tag)
        if idx != -1:
            end = lower.find(">", idx)
            if end != -1:
                return html[: end + 1] + _CONSOLE_CAPTURE_JS + html[end + 1:]
    return _CONSOLE_CAPTURE_JS + html


@router.post("/api/preview/token")
async def mint_preview_token(payload: dict, db: Session = Depends(get_db)):
    """Mint a preview-scoped token for one project (session-token protected)."""
    project = (payload.get("project") or "").strip()
    proj = get_project_or_404(db, project)
    token = create_token(
        get_jwt_secret(db),
        expires_minutes=PREVIEW_TOKEN_TTL_MINUTES,
        extra_claims={"scope": "preview", "project": proj.name},
    )
    return {"token": token, "expires_in": PREVIEW_TOKEN_TTL_MINUTES * 60}


@router.get("/api/preview/t/{token}/{project}/{path:path}")
async def serve_preview_file(token: str, project: str, path: str,
                             db: Session = Depends(get_db)):
    """Serve a project file for sandboxed preview.

    Auth-middleware-exempt — the path-embedded preview token is validated
    here instead, so relative subresource requests authenticate without
    cookies or query params.
    """
    claims = decode_token(token, get_jwt_secret(db))
    if not claims or claims.get("scope") != "preview":
        raise HTTPException(status_code=401, detail="Invalid preview token")
    # Cut off by change-password / revoke-others / reset-password.
    if claims.get("iat", 0) < auth_store.get_not_before(db):
        raise HTTPException(status_code=401, detail="Invalid preview token")
    if claims.get("project") != project:
        raise HTTPException(status_code=403, detail="Token not valid for this project")

    proj = get_project_or_404(db, project)
    base_dir = os.path.realpath(proj.path)
    full_path = os.path.realpath(os.path.join(base_dir, path))
    # Strict containment — unlike /api/files/, no cross-project fallback
    # search here: the token is project-scoped and HTML is executable.
    if full_path != base_dir and not full_path.startswith(base_dir + os.sep):
        raise HTTPException(status_code=400, detail="Invalid path")
    if os.path.isdir(full_path):
        full_path = os.path.join(full_path, "index.html")
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    media_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
    if media_type == "text/html":
        with open(full_path, "rb") as f:
            html = f.read().decode("utf-8", errors="replace")
        return HTMLResponse(_inject_console_capture(html), headers=_PREVIEW_HEADERS)
    return FileResponse(full_path, media_type=media_type, headers=_PREVIEW_HEADERS)


# ---------------------------------------------------------------------------
# Port proxy — kind="port" web apps (TensorBoard, dev servers)
# ---------------------------------------------------------------------------
#
# Unlike static previews (short-lived JWT, minted per open), port proxies use
# a stable HMAC capability signature so the prefix /api/preview/p/{sig}/...
# never changes for a given (project, port). That lets prefix-aware services
# be launched once with a fixed base path, and lets cards embed a permanent
# src. Rotating the JWT secret invalidates all signatures. The proxied path
# is forwarded with the prefix STRIPPED (upstream sees /), which matches
# root-served apps (TensorBoard, python -m http.server, vite with base:'./').

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
# Stripped from upstream responses: hop-by-hop, length/encoding (httpx hands
# us decoded bytes), and framing headers we override with _PREVIEW_HEADERS.
_STRIP_RESP = _HOP_BY_HOP | {
    "content-length", "content-encoding", "content-security-policy",
    "access-control-allow-origin", "cache-control", "x-frame-options",
}


def port_proxy_sig(jwt_secret: str, project: str, port: int) -> str:
    """Stable capability signature for one (project, port) proxy prefix."""
    msg = f"preview-port:{project}:{port}".encode()
    return hmac.new(jwt_secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def _check_port_proxy(db, sig: str, project: str, port: int) -> None:
    """Validate a port-proxy request; raises HTTPException on failure."""
    if not 1024 <= port <= 65535:
        raise HTTPException(status_code=400, detail="Port out of range")
    if port == PORT:
        # Never proxy to the orchestrator itself — that would let preview
        # content reach the API without auth.
        raise HTTPException(status_code=403, detail="Port not allowed")
    expected = port_proxy_sig(get_jwt_secret(db), project, port)
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="Invalid signature")
    registered = db.query(WebApp).filter_by(
        project=project, kind="port", target=str(port)).first()
    if registered is None:
        raise HTTPException(status_code=403, detail="Port not registered")


@router.api_route("/api/preview/p/{sig}/{project}/{port}/{path:path}",
                  methods=["GET", "POST", "HEAD"])
async def proxy_preview_port(sig: str, project: str, port: int, path: str,
                             request: Request, db: Session = Depends(get_db)):
    """Reverse-proxy a registered localhost service for sandboxed preview."""
    _check_port_proxy(db, sig, project, port)

    upstream_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        upstream_url += "?" + request.url.query
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and k.lower() not in ("host", "authorization", "cookie")
    }
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=120)) as client:
            up = await client.request(request.method, upstream_url,
                                      headers=fwd_headers, content=body)
    except httpx.ConnectError:
        raise HTTPException(status_code=502,
                            detail=f"Nothing listening on 127.0.0.1:{port}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {type(e).__name__}")

    resp_headers = {
        k: v for k, v in up.headers.items() if k.lower() not in _STRIP_RESP
    }
    resp_headers.update(_PREVIEW_HEADERS)

    ctype = up.headers.get("content-type", "")
    if "text/html" in ctype and request.method != "HEAD":
        html = up.content.decode("utf-8", errors="replace")
        return HTMLResponse(_inject_console_capture(html),
                            status_code=up.status_code, headers=resp_headers)
    return Response(content=up.content, status_code=up.status_code,
                    headers=resp_headers, media_type=ctype or None)


@router.websocket("/api/preview/p/{sig}/{project}/{port}/{path:path}")
async def proxy_preview_port_ws(websocket: WebSocket, sig: str, project: str,
                                port: int, path: str):
    """Bidirectional WebSocket relay (vite HMR, live dashboards)."""
    import asyncio

    import websockets as ws_client

    db = SessionLocal()
    try:
        _check_port_proxy(db, sig, project, port)
    except HTTPException:
        await websocket.close(code=4403)
        return
    finally:
        db.close()

    upstream_uri = f"ws://127.0.0.1:{port}/{path}"
    query = websocket.scope.get("query_string", b"").decode()
    if query:
        upstream_uri += "?" + query
    requested_protocols = [
        p.strip() for p in
        websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()
    ]

    try:
        async with ws_client.connect(
            upstream_uri,
            subprotocols=requested_protocols or None,
            open_timeout=10,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def client_to_upstream():
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])

            async def upstream_to_client():
                async for msg in upstream:
                    if isinstance(msg, str):
                        await websocket.send_text(msg)
                    else:
                        await websocket.send_bytes(msg)

            done, pending = await asyncio.wait(
                [asyncio.create_task(client_to_upstream()),
                 asyncio.create_task(upstream_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except (OSError, ws_client.exceptions.WebSocketException, WebSocketDisconnect) as e:
        logger.debug("port-proxy ws closed: %s", type(e).__name__)
        try:
            await websocket.close()
        except RuntimeError:
            pass
