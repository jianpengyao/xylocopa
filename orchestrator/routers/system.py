"""System routes — health, stats, storage, backups, restart, notifications."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import base64
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import BACKUP_DIR, CLAUDE_HOME, DB_PATH, LOG_DIR, UPLOADS_DIR
from plat import platform as _platform
from database import SessionLocal, get_db
from models import Agent, AgentStatus, Message, Project, SystemConfig, Task, TaskStatus
from schemas import HealthResponse
from route_helpers import IMPORT_CHECK_TIMEOUT as _IMPORT_CHECK_TIMEOUT, API_REQUEST_TIMEOUT as _API_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


# Reload-storm detector: counts full-page reload markers per client IP.
# `patch-failed` fires once per fresh page load (inline script in index.html
# runs on every navigation/reload), so it's a reliable reload marker that
# can't be faked by normal pagehide/visibilitychange traffic.
_RELOAD_STORM_WINDOW_SEC = 60
_RELOAD_STORM_THRESHOLD = 5
_RELOAD_STORM_COOLDOWN_SEC = 60
_reload_ts_by_ip: dict = defaultdict(lambda: deque(maxlen=20))
_reload_storm_last_warned: dict = {}


def _check_reload_storm(ip: str, reason: str, path: str) -> None:
    if reason != "patch-failed":
        return
    now = time.monotonic()
    dq = _reload_ts_by_ip[ip]
    dq.append(now)
    cutoff = now - _RELOAD_STORM_WINDOW_SEC
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) < _RELOAD_STORM_THRESHOLD:
        return
    last_warned = _reload_storm_last_warned.get(ip, 0)
    if now - last_warned < _RELOAD_STORM_COOLDOWN_SEC:
        return
    _reload_storm_last_warned[ip] = now
    logger.error(
        "RELOAD_STORM: ip=%s reloaded %d times in last %ds (threshold=%d) path=%s "
        "— likely SW update loop or crashed frontend",
        ip, len(dq), _RELOAD_STORM_WINDOW_SEC, _RELOAD_STORM_THRESHOLD, path,
    )


# ---- Certificate download (for mobile trust setup) ----

# SAN hostname for /api/cert/regenerate: alphanumeric at both ends, so it
# can never start with "-" and be read by mkcert as a flag.
_CERT_DNS_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def _is_ip_address(value: str) -> bool:
    # No IPv6 zone IDs ("fe80::1%eth0"): ipaddress accepts almost any text
    # after the "%", and mkcert rejects them all anyway.
    if "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


@router.get("/api/cert/info")
async def cert_info():
    """Return just the current cert's SAN (IPs + DNS).  Used by /cert-guide
    to tell the user whether their current address is already covered —
    avoids the "click Regenerate not knowing if anything will happen"
    UX gap. Minimal on purpose: no host detection, no diff, no enrichment."""
    import subprocess

    cert_path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "certs", "selfsigned.crt")
    )
    if not os.path.isfile(cert_path):
        raise HTTPException(status_code=404, detail="cert not found")
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", cert_path, "-noout", "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"openssl: {e}")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail="openssl exit nonzero")
    return {
        "ips": re.findall(r"IP Address:([0-9.a-fA-F:]+)", proc.stdout),
        "dns": re.findall(r"DNS:([\w.\-]+)", proc.stdout),
    }


@router.post("/api/cert/regenerate")
async def cert_regenerate(request: Request):
    """Re-sign the leaf cert with mkcert for explicit IPs/DNS.

    Body: {"ips": ["1.2.3.4", ...], "dns": ["xylocopa", "localhost"]}
    Both fields REQUIRED. No auto-detect fallback — host-self-detection
    can't see tailnet-shared IPs (a node-shared machine appears at
    different IPs across tailnets), so a silent fallback would lock those
    clients out of TLS. Same mkcert CA is preserved, so any client that
    trusts the CA root sees the new leaf as valid automatically.
    """
    import subprocess

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    ips = body.get("ips") or []
    dns = body.get("dns") or []
    if not isinstance(ips, list) or not isinstance(dns, list):
        raise HTTPException(status_code=400, detail="ips/dns must be lists")
    if not ips:
        raise HTTPException(
            status_code=400,
            detail="ips list cannot be empty — cert needs at least one explicit IP",
        )
    # Every name ends up as an mkcert argv entry: a leading "-" would be
    # parsed as a flag (e.g. "-cert-file=<path>" overwrites an arbitrary
    # file). The hostname pattern can't start with "-", IPs must parse, and
    # the argv below also puts "--" before the names.
    for ip in ips:
        if not isinstance(ip, str) or not _is_ip_address(ip):
            raise HTTPException(status_code=400, detail=f"invalid IP: {ip!r}")
    for name in dns:
        if not isinstance(name, str) or not _CERT_DNS_NAME_RE.fullmatch(name):
            raise HTTPException(status_code=400, detail=f"invalid DNS name: {name!r}")

    certs_dir = os.path.normpath(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "certs")
    )
    cert_path = os.path.join(certs_dir, "selfsigned.crt")
    key_path = os.path.join(certs_dir, "selfsigned.key")
    os.makedirs(certs_dir, exist_ok=True)

    # Idempotency: if current cert SAN is EXACTLY what we'd sign, skip.
    # Strict equality (not subset) so a stale external IP from a previous
    # regen also triggers a clean re-sign — the design contract is "one
    # external IP at a time, plus the always-resident internal set".
    requested_ips = set(ips) | {"127.0.0.1"}
    requested_dns = set(dns)
    if os.path.isfile(cert_path):
        try:
            san_proc = subprocess.run(
                ["openssl", "x509", "-in", cert_path, "-noout", "-ext", "subjectAltName"],
                capture_output=True, text=True, timeout=5,
            )
            if san_proc.returncode == 0:
                current_ips = set(re.findall(r"IP Address:([0-9.a-fA-F:]+)", san_proc.stdout))
                current_dns = set(re.findall(r"DNS:([\w.\-]+)", san_proc.stdout))
                if requested_ips == current_ips and requested_dns == current_dns:
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": "current cert SAN already matches requested set exactly",
                        "current_ips": sorted(current_ips),
                        "current_dns": sorted(current_dns),
                    }
        except Exception as e:
            logger.warning("[cert] SAN inspection failed, proceeding with regen: %s", e)

    # 127.0.0.1 always included so server-internal callers (vite preview
    # proxy → backend) keep working regardless of what the user typed.
    mkcert_args = [
        "mkcert", "-cert-file", cert_path, "-key-file", key_path, "--",
        *dns, "127.0.0.1", *ips,
    ]
    try:
        proc = subprocess.run(mkcert_args, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="mkcert binary not found — install it from https://github.com/FiloSottile/mkcert",
        )
    if proc.returncode != 0:
        logger.error("[cert] mkcert failed: stdout=%s stderr=%s", proc.stdout, proc.stderr)
        raise HTTPException(
            status_code=500,
            detail=f"mkcert failed: {(proc.stderr or proc.stdout).strip()[:500]}",
        )

    # Best-effort reload so vite preview re-reads the new cert
    # (it caches the cert in memory at startup via fs.readFileSync).
    try:
        subprocess.Popen(
            ["pm2", "reload", "xylocopa-frontend"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    return {"ok": True}


@router.get("/api/cert")
async def download_cert():
    """Serve the CA root certificate for mobile trust setup.

    Tries mkcert CA root first (preferred — iOS trusts CA-issued certs for
    PWA icon fetching), then falls back to the self-signed leaf cert.
    """
    # Prefer mkcert CA root — iOS system processes honour user-installed CAs
    # better than plain self-signed leaf certs (needed for PWA home-screen icons).
    mkcert_root = os.path.expanduser("~/Library/Application Support/mkcert/rootCA.pem")
    if not os.path.isfile(mkcert_root):
        # Linux default
        mkcert_root = os.path.expanduser("~/.local/share/mkcert/rootCA.pem")

    if os.path.isfile(mkcert_root):
        return FileResponse(
            mkcert_root,
            media_type="application/x-x509-ca-cert",
            filename="xylocopa-ca.crt",
        )

    # Fallback: serve the leaf cert directly
    cert_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "certs", "selfsigned.crt")
    cert_path = os.path.normpath(cert_path)
    if not os.path.isfile(cert_path):
        raise HTTPException(status_code=404, detail="Certificate not found")
    return FileResponse(
        cert_path,
        media_type="application/x-x509-ca-cert",
        filename="xylocopa.crt",
    )


@router.get("/api/webclip")
async def download_webclip(request: Request):
    """Generate a .mobileconfig Web Clip profile with the icon embedded.

    iOS Add-to-Home-Screen fetches icons via a system process that rejects
    self-signed / private-CA certs.  A Web Clip profile embeds the icon
    directly, bypassing the fetch entirely.

    NOTE (2026-05-07): likely no longer needed — apple-touch-icon and the
    PWA manifest icons are now served from the public jsdelivr CDN
    (see frontend/vite.config.js ICON_BASE), so iOS's system fetcher no
    longer hits the private CA.  Frontend cert-guide page now points users
    to Safari's native Share → Add to Home Screen instead of this endpoint.
    Keeping the route alive for now in case any old bookmarks still use it;
    safe to remove after a release or two with no reports.
    """
    icon_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "..", "frontend", "public", "apple-touch-icon.png",
    )
    icon_path = os.path.normpath(icon_path)
    if not os.path.isfile(icon_path):
        raise HTTPException(status_code=404, detail="Icon not found")

    with open(icon_path, "rb") as f:
        icon_b64 = base64.b64encode(f.read()).decode()

    # Determine the host the user actually connects to.
    # Explicit ?host= param takes priority (set by the frontend guide page),
    # then origin/referer, then fall back to the request hostname.
    port = os.environ.get("FRONTEND_PORT", "3000")
    host = request.query_params.get("host")
    if not host:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin:
            from urllib.parse import urlparse
            host = urlparse(origin).hostname
    if not host:
        host = request.url.hostname
    import html
    host = html.escape(host)
    url = f"https://{host}:{port}/"

    payload_uuid = str(uuid.uuid4()).upper()
    profile_uuid = str(uuid.uuid4()).upper()

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>FullScreen</key>
      <true/>
      <key>Icon</key>
      <data>{icon_b64}</data>
      <key>IsRemovable</key>
      <true/>
      <key>Label</key>
      <string>Xylocopa</string>
      <key>PayloadDisplayName</key>
      <string>Xylocopa Web Clip</string>
      <key>PayloadIdentifier</key>
      <string>com.xylocopa.webclip.{payload_uuid}</string>
      <key>PayloadType</key>
      <string>com.apple.webClip.managed</string>
      <key>PayloadUUID</key>
      <string>{payload_uuid}</string>
      <key>PayloadVersion</key>
      <integer>1</integer>
      <key>URL</key>
      <string>{url}</string>
    </dict>
  </array>
  <key>PayloadDisplayName</key>
  <string>Xylocopa</string>
  <key>PayloadIdentifier</key>
  <string>com.xylocopa.profile.{profile_uuid}</string>
  <key>PayloadRemovalDisallowed</key>
  <false/>
  <key>PayloadType</key>
  <string>Configuration</string>
  <key>PayloadUUID</key>
  <string>{profile_uuid}</string>
  <key>PayloadVersion</key>
  <integer>1</integer>
  <key>PayloadDescription</key>
  <string>Adds Xylocopa to your Home Screen with the correct app icon.</string>
</dict>
</plist>"""

    return Response(
        content=plist,
        media_type="application/x-apple-aspen-config",
        headers={"Content-Disposition": "attachment; filename=Xylocopa.mobileconfig"},
    )



# ---- Health ----

@router.get("/api/health", response_model=HealthResponse)
async def health(request: Request):
    """System health check — verifies DB is writable and Claude CLI is reachable."""
    result = HealthResponse(status="ok")

    # Check DB
    try:
        db = SessionLocal()
        try:
            db.execute(Agent.__table__.select().limit(1))
        finally:
            db.close()
    except Exception:
        result.db = "error"
        result.status = "degraded"

    # Check Claude CLI
    wm = getattr(request.app.state, "worker_manager", None)
    if wm and wm.ping():
        result.claude_cli = "ok"
    else:
        result.claude_cli = "unavailable"
        result.status = "degraded"

    return result


@router.get("/api/telemetry")
async def get_telemetry_status():
    """Current telemetry config (used by Monitor page toggle)."""
    import telemetry
    return telemetry.get_status()


@router.post("/api/telemetry")
async def set_telemetry_status(request: Request):
    """Toggle telemetry via ~/.xylocopa/config.yaml. Body: {"enabled": bool}."""
    import telemetry
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    return telemetry.set_enabled(enabled)


@router.post("/api/test/notify")
async def test_notify(request: Request):
    """Send a test notification through the notify() gateway.

    Query params:
        channel: notify_at | task_complete | message (default: message)
        agent_id: agent ID to test with (default: "test")
        muted: true/false (default: false)
        in_use: true/false (default: false) — if "auto", uses real _is_agent_in_use()
    """
    params = request.query_params
    channel = params.get("channel", "message")
    agent_id = params.get("agent_id", "test")
    muted = params.get("muted", "false").lower() == "true"
    in_use_param = params.get("in_use", "false")

    # Auto-detect in-use from real signals
    ws_viewed = False
    pane_attached = False
    pane = None
    if in_use_param == "auto" and agent_id != "test":
        dispatcher = getattr(request.app.state, "agent_dispatcher", None)
        if dispatcher:
            from database import SessionLocal
            from models import Agent
            db = SessionLocal()
            try:
                agent = db.get(Agent, agent_id)
                pane = agent.tmux_pane if agent else None
            finally:
                db.close()
            from websocket import ws_manager
            ws_viewed = ws_manager.is_agent_viewed(agent_id)
            has_focus = ws_manager.is_any_client_focused()
            dispatcher._refresh_pane_attached()
            pane_attached = bool(pane and dispatcher._pane_attached.get(pane, False))
            pane_active = bool(pane and dispatcher._pane_active.get(pane, False))
            window_active = bool(pane and dispatcher._window_active.get(pane, False))
            in_use = (ws_viewed and has_focus) or (pane_attached and pane_active and window_active)
        else:
            in_use = False
    else:
        in_use = in_use_param.lower() == "true"

    from notify import notify
    decision = notify(channel, agent_id, "Xylocopa Test",
           f"Test via {channel} (muted={muted}, in_use={in_use})",
           "/agents", muted=muted, in_use=in_use)

    # Emit debug bubble to frontend
    from websocket import ws_manager
    asyncio.get_event_loop().create_task(ws_manager.broadcast(
        "notification_debug",
        {"agent_id": agent_id, "decision": decision,
         "channel": channel, "body": f"test ({decision})"}
    ))

    return {
        "channel": channel,
        "agent_id": agent_id,
        "muted": muted,
        "in_use": in_use,
        "ws_viewed": ws_viewed,
        "pane_attached": pane_attached,
        "tmux_pane": pane,
        "decision": decision,
        "routed_through": "notify()",
    }


@router.post("/api/debug/frontend-log")
async def frontend_log(request: Request):
    """Append free-form frontend log lines to logs/frontend-debug.log.

    Body is plain text, one or more newline-separated lines. Used by the
    frontend `clog()` helper so per-render / per-fetch timing logs land
    somewhere a developer (or agent) can `tail -f` without copy-pasting
    out of the browser console.
    """
    _dbg = logging.getLogger("frontend.debug")
    body = (await request.body()).decode("utf-8", errors="replace").rstrip()
    if not body:
        return {"ok": True, "lines": 0}
    lines = body.split("\n")
    for line in lines:
        if line.strip():
            _dbg.info("[fe] %s", line)
    return {"ok": True, "lines": len(lines)}


@router.post("/api/debug/frontend-state")
async def frontend_debug_state(request: Request):
    """Receive and log frontend rendered state for debugging."""
    _dbg = logging.getLogger("frontend.debug")
    body = await request.json()
    agent_id = body.get("agentId", "?")[:8]
    page = body.get("page", "?")
    msgs = body.get("messages", [])
    ws_events = body.get("wsEvents", [])

    dom_els = body.get("domElements", [])

    _dbg.info("=== Frontend State: agent=%s page=%s ===", agent_id, page)
    _dbg.info("Data messages: %d | DOM elements: %d", len(msgs), len(dom_els))
    _dbg.info("--- Data (from React state) ---")
    for m in msgs:
        _dbg.info("  %s role=%-6s kind=%-10s seq=%-4s src=%-5s id=%s content=%.80s",
                   m.get("created_at", "?")[:19] if m.get("created_at") else "?",
                   m.get("role", "?"), m.get("kind") or "null",
                   m.get("session_seq", "?"), m.get("source") or "?",
                   m.get("id", "?"), (m.get("content", "") or "")[:80].replace("\n", " "))
    if dom_els:
        _dbg.info("--- DOM (actual rendered elements) ---")
        for el in dom_els:
            _dbg.info("  y=%-5s h=%-4s vis=%-5s type=%-15s id=%s text=%.60s",
                       el.get("y", "?"), el.get("h", "?"),
                       el.get("visible", "?"), el.get("type", "?"),
                       el.get("msgId", "?"),
                       (el.get("text", "") or "")[:60].replace("\n", " "))
    if ws_events:
        _dbg.info("--- WS events (last 20) ---")
        for ev in ws_events[-20:]:
            _dbg.info("  %s", ev)
    _dbg.info("=== End Frontend State ===")
    return {"ok": True}


@router.post("/api/debug/kb-log")
async def kb_debug_log(request: Request):
    """Receive keyboard viewport debug samples — writes to logs/kb-debug.log."""
    body = await request.json()
    samples = body.get("samples", [])
    _kb = logging.getLogger("kb.debug")
    for s in samples:
        _kb.info("t=%s cH=%s iH=%s vvH=%s vvOT=%s off=%s open=%s",
                 s.get('t', '?'), s.get('cH', '?'), s.get('iH', '?'),
                 s.get('vvH', '?'), s.get('vvOT', '?'),
                 s.get('off', '?'), s.get('open', '?'))
    return {"ok": True, "count": len(samples)}


@router.post("/api/debug/auth-diag")
async def auth_diag(request: Request):
    """Receive auth lifecycle events from the frontend for debugging.

    The frontend sends events whenever clearAuthToken is called (or blocked),
    including which code path triggered it and how long since the last login.
    Also receives reload-trace events from the reload probe (see main.jsx).
    """
    body = await request.json()
    action = body.get("action", "?")
    reason = body.get("reason", "?")
    path = body.get("path", "?")
    if action == "reload-trace":
        persisted = body.get("persisted", "?")
        is_vite = body.get("isVite", "?")
        error = body.get("error", "")
        stack = (body.get("stack") or "").replace("\n", " | ")[:1500]
        extras = []
        if is_vite != "?":
            extras.append(f"isVite={is_vite}")
        if error:
            extras.append(f"error={error}")
        extras_str = (" " + " ".join(extras)) if extras else ""
        logger.info(
            "RELOAD_TRACE: reason=%s path=%s persisted=%s%s stack=%s",
            reason, path, persisted, extras_str, stack,
        )
        ip = request.client.host if request.client else "unknown"
        _check_reload_storm(ip, reason, path)
    else:
        since_ms = body.get("since_login_ms", "?")
        has_token = body.get("has_token", "?")
        logger.info(
            "AUTH_DIAG: action=%s reason=%s since_login=%sms path=%s has_token=%s",
            action, reason, since_ms, path, has_token,
        )
    return {"ok": True}


@router.get("/api/system/stats")
async def system_stats(request: Request):
    """System resource usage — CPU, memory, disk, and optional GPU."""
    import shutil
    import subprocess

    stats = {}

    # CPU usage
    stats["cpu"] = _platform.get_cpu_load()

    # Memory
    stats["memory"] = _platform.get_memory_info()

    # Disk usage
    try:
        usage = shutil.disk_usage("/")
        stats["disk"] = {
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "usage_pct": round(usage.used / usage.total * 100, 1),
        }
    except OSError as e:
        logger.warning("Failed to collect disk stats: %s", e)
        stats["disk"] = None

    # GPU
    stats["gpus"] = _platform.get_gpu_stats()

    # Xylocopa own process usage (uvicorn + vite)
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem_mb = proc.memory_info().rss / (1024 * 1024)
        cpu = proc.cpu_percent(interval=0)
        # Include child processes (worker threads, etc.)
        for child in proc.children(recursive=True):
            try:
                mem_mb += child.memory_info().rss / (1024 * 1024)
                cpu += child.cpu_percent(interval=0)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        stats["xylocopa"] = {
            "mem_mb": round(mem_mb, 1),
            "cpu_pct": round(cpu, 1),
        }
    except ImportError:
        # Fallback: use platform layer for process memory
        mem_mb = _platform.get_process_memory_mb(os.getpid())
        stats["xylocopa"] = {"mem_mb": round(mem_mb, 1), "cpu_pct": 0} if mem_mb else None

    # Legacy alias for older frontends; remove once UI is fully migrated.
    stats["agenthive"] = stats["xylocopa"]

    # xylo-managed chats — RSS of every active xylo agent's claude CLI
    # plus its descendants (MCP server, bash, etc.). These live under
    # the detached tmux server (NOT orchestrator), so the "Xylocopa"
    # widget can't see them via proc.children().
    #
    # Precise lookup (no process_iter scan): DB → active agents → each
    # agent's tmux_pane → tmux's pane_pid → the claude child of that
    # pane's bash. One DB query + one `tmux list-panes` shellout total.
    try:
        import psutil
        from models import Agent, AgentStatus
        # 1) active xylo agents with a tmux pane
        _db = SessionLocal()
        try:
            agents_with_panes = [
                (a.id, a.tmux_pane) for a in _db.query(Agent)
                .filter(
                    Agent.status.in_([AgentStatus.IDLE, AgentStatus.EXECUTING, AgentStatus.STARTING]),
                    Agent.tmux_pane.isnot(None),
                ).all()
            ]
        finally:
            _db.close()
        # 2) tmux pane_id → pane_pid (the bash root inside the pane)
        pane_pids: dict[str, int] = {}
        try:
            _r = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
            for line in _r.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit():
                    pane_pids[parts[0]] = int(parts[1])
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass
        # 3) for each (agent, pane), find the claude child of the bash
        chat_total_mb = 0.0
        chats: list[dict] = []
        for agent_id, pane in agents_with_panes:
            bash_pid = pane_pids.get(pane)
            if not bash_pid:
                continue
            try:
                bash_proc = psutil.Process(bash_pid)
                claude_proc = None
                for child in bash_proc.children(recursive=False):
                    try:
                        if (child.name() or "").lower() == "claude":
                            claude_proc = child
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                if claude_proc is None:
                    continue
                rss = claude_proc.memory_info().rss
                for desc in claude_proc.children(recursive=True):
                    try:
                        rss += desc.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                mb = rss / (1024 * 1024)
                chat_total_mb += mb
                chats.append({"agent_id": agent_id, "mem_mb": round(mb, 1)})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        stats["chats"] = {
            "count": len(chats),
            "total_mb": round(chat_total_mb, 1),
            "items": sorted(chats, key=lambda c: -c["mem_mb"]),
        }
    except Exception:
        logger.debug("system_stats: chat enumeration failed", exc_info=True)
        stats["chats"] = None

    return stats


@router.get("/api/system/storage")
async def system_storage():
    """Disk usage breakdown by storage category."""
    import glob as globmod
    import tempfile

    def _collect():
        """Synchronous work — run in a thread to avoid blocking the event loop."""
        def _walk_size(path: str):
            total = 0
            count = 0
            if not os.path.isdir(path):
                return 0, 0
            for dirpath, _dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                        count += 1
                    except OSError:
                        pass
            return total, count

        def _file_size(path: str):
            try:
                return os.path.getsize(path), 1
            except OSError:
                return 0, 0

        categories = []

        sessions_dir = os.path.join(CLAUDE_HOME, "projects")
        sz, cnt = _walk_size(sessions_dir)
        categories.append({"name": "Session Files", "size_bytes": sz, "file_count": cnt, "color": "cyan"})

        cache_dir = os.path.join(BACKUP_DIR, "session-cache")
        sz, cnt = _walk_size(cache_dir)
        categories.append({"name": "Session Cache", "size_bytes": sz, "file_count": cnt, "color": "violet"})

        sz, cnt = _walk_size(BACKUP_DIR)
        cache_sz, cache_cnt = categories[1]["size_bytes"], categories[1]["file_count"]
        categories.append({"name": "DB Backups", "size_bytes": max(sz - cache_sz, 0), "file_count": max(cnt - cache_cnt, 0), "color": "amber"})

        sz, cnt = _file_size(DB_PATH)
        categories.append({"name": "Database", "size_bytes": sz, "file_count": cnt, "color": "emerald"})

        sz, cnt = _walk_size(LOG_DIR)
        categories.append({"name": "Logs", "size_bytes": sz, "file_count": cnt, "color": "orange"})

        tmp_total = 0
        tmp_count = 0
        for fp in globmod.glob(os.path.join(tempfile.gettempdir(), "claude-output-*.log")):
            try:
                tmp_total += os.path.getsize(fp)
                tmp_count += 1
            except OSError:
                pass
        categories.append({"name": "Tmp Output", "size_bytes": tmp_total, "file_count": tmp_count, "color": "gray"})

        sz, cnt = _walk_size(UPLOADS_DIR)
        # Include per-project upload dirs (<project>/.xylocopa/uploads)
        try:
            _pdb = SessionLocal()
            try:
                for _proj in _pdb.query(Project).filter(Project.archived == False).all():
                    proj_uploads = os.path.join(_proj.path, ".xylocopa", "uploads")
                    _psz, _pcnt = _walk_size(proj_uploads)
                    sz += _psz
                    cnt += _pcnt
            finally:
                _pdb.close()
        except Exception:
            pass
        categories.append({"name": "Uploads", "size_bytes": sz, "file_count": cnt, "color": "rose"})

        total_bytes = sum(c["size_bytes"] for c in categories)
        return {"categories": categories, "total_bytes": total_bytes}

    return await asyncio.get_event_loop().run_in_executor(None, _collect)


@router.get("/api/system/orphans/scan")
async def system_orphan_scan():
    """Scan for orphaned session JSONL files and output logs."""
    from orphan_cleanup import scan_orphans

    def _scan():
        result = scan_orphans()
        # Strip file lists from response (only return counts/sizes)
        return {k: v for k, v in result.items()
                if k not in ("orphan_sessions", "orphan_logs", "empty_dirs", "orphan_projects")}

    return await asyncio.get_event_loop().run_in_executor(None, _scan)


@router.post("/api/system/orphans/clean")
async def system_orphan_clean():
    """Scan and delete orphaned files atomically."""
    from orphan_cleanup import scan_orphans, delete_orphans

    def _clean():
        scan = scan_orphans()
        return delete_orphans(scan)

    return await asyncio.get_event_loop().run_in_executor(None, _clean)


@router.get("/api/system/stale-agents/scan")
async def system_stale_agents_scan(
    max_age_days: int = 30,
    db: Session = Depends(get_db),
):
    """Scan for stale stopped/error agents older than max_age_days."""
    from orphan_cleanup import scan_stale_agents

    result = scan_stale_agents(db, max_age_days=max_age_days)
    # Strip detailed agent list from response (only return counts)
    return {k: v for k, v in result.items() if k not in ("eligible_agents", "orphan_subagent_ids")}


@router.post("/api/system/stale-agents/clean")
async def system_stale_agents_clean(
    max_age_days: int = 30,
    db: Session = Depends(get_db),
):
    """Scan and delete stale agents (stopped/error, older than max_age_days)."""
    from orphan_cleanup import scan_stale_agents, delete_stale_agents

    scan = scan_stale_agents(db, max_age_days=max_age_days)
    return delete_stale_agents(db, scan)


@router.post("/api/system/logs/truncate")
async def system_logs_truncate():
    """Truncate non-essential log files (PM2 logs, frontend-debug, old rotated logs)."""
    import glob as globmod

    def _truncate():
        truncated = []
        freed = 0

        # Files safe to truncate in-place (PM2 keeps the fd open)
        truncatable = [
            "backend-pm2.log", "backend-pm2-error.log",
            "frontend-pm2.log", "frontend-pm2-error.log",
            "frontend-debug.log", "kb-debug.log",
        ]
        for name in truncatable:
            fp = os.path.join(LOG_DIR, name)
            try:
                sz = os.path.getsize(fp)
                if sz > 0:
                    with open(fp, "w"):
                        pass  # truncate
                    freed += sz
                    truncated.append(name)
            except OSError:
                pass

        # Remove old rotated orchestrator logs (keep current orchestrator.log)
        for fp in globmod.glob(os.path.join(LOG_DIR, "orchestrator.log.*")):
            try:
                sz = os.path.getsize(fp)
                os.remove(fp)
                freed += sz
                truncated.append(os.path.basename(fp))
            except OSError:
                pass

        # Remove old rotated PM2 logs (pm2 max_size creates numbered copies)
        for pattern in ["backend-pm2*.log.*", "backend-pm2-error*.log.*",
                        "frontend-pm2*.log.*", "frontend-pm2-error*.log.*",
                        "frontend-debug*.log.*", "kb-debug*.log.*"]:
            for fp in globmod.glob(os.path.join(LOG_DIR, pattern)):
                try:
                    sz = os.path.getsize(fp)
                    os.remove(fp)
                    freed += sz
                    truncated.append(os.path.basename(fp))
                except OSError:
                    pass

        return {"truncated": truncated, "freed_bytes": freed}

    return await asyncio.get_event_loop().run_in_executor(None, _truncate)


@router.get("/api/system/backup")
async def get_backup_status():
    """Return current backup config, on-disk stats, and backup list."""
    from backup import get_runtime_config, list_backups

    cfg = get_runtime_config()
    backups = list_backups()
    total_bytes = sum(b["total_bytes"] for b in backups)

    return {
        **cfg,
        "backup_dir": BACKUP_DIR,
        "backup_count": len(backups),
        "total_bytes": total_bytes,
        "backups": backups,
    }


@router.post("/api/system/backup")
async def trigger_manual_backup():
    """Trigger a manual backup immediately."""
    from backup import do_backup
    name = do_backup()
    return {"detail": "ok", "name": name}


@router.delete("/api/system/backup")
async def purge_backups():
    """Delete ALL backup snapshots (not session-cache)."""
    import glob as globmod
    import shutil

    backup_dirs = sorted(globmod.glob(os.path.join(BACKUP_DIR, "backup_*")))
    if not backup_dirs:
        return {"detail": "No backups to delete", "deleted": 0, "freed_bytes": 0}

    freed = 0
    deleted = 0
    for d in backup_dirs:
        sz = 0
        for dp, _, files in os.walk(d):
            for f in files:
                try:
                    sz += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        try:
            shutil.rmtree(d)
            freed += sz
            deleted += 1
        except OSError as e:
            logger.warning("Failed to remove backup %s: %s", d, e)

    logger.info("Purged %d backups, freed %d bytes", deleted, freed)
    return {"detail": "ok", "deleted": deleted, "freed_bytes": freed}


@router.delete("/api/system/backup/{name}")
async def delete_single_backup(name: str):
    """Delete a single backup snapshot."""
    from backup import delete_backup
    freed = delete_backup(name)
    if freed == 0:
        raise HTTPException(status_code=404, detail="Backup not found")
    return {"detail": "ok", "freed_bytes": freed}


@router.post("/api/system/backup/{name}/restore")
async def restore_from_backup(name: str):
    """Restore database and registry from a backup snapshot."""
    from backup import restore_backup
    try:
        result = restore_backup(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found")
    return {"detail": "ok", **result}


@router.put("/api/system/backup/config")
async def update_backup_config(request: Request):
    """Update backup schedule config (persists to .env)."""
    from backup import update_runtime_config, persist_env_config, get_runtime_config

    body = await request.json()
    enabled = body.get("enabled")
    interval_hours = body.get("interval_hours")
    max_backups = body.get("max_backups")

    # Validate
    if interval_hours is not None and (not isinstance(interval_hours, int) or interval_hours < 1):
        raise HTTPException(status_code=400, detail="interval_hours must be >= 1")
    if max_backups is not None and (not isinstance(max_backups, int) or max_backups < 1):
        raise HTTPException(status_code=400, detail="max_backups must be >= 1")

    update_runtime_config(enabled=enabled, interval_hours=interval_hours, max_backups=max_backups)

    # Persist to .env
    cfg = get_runtime_config()
    persist_env_config(cfg["enabled"], cfg["interval_hours"], cfg["max_backups"])

    return {"detail": "ok", **cfg}


@router.post("/api/system/backup/import")
async def import_backup_upload(request: Request):
    """Import a backup from an uploaded zip file."""
    import tempfile
    from backup import import_backup

    # Read multipart form data
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()
        name = import_backup(tmp.name)
        return {"detail": "ok", "name": name}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


@router.get("/api/system/backup/{name}/download")
async def download_backup(name: str):
    """Download a backup snapshot as a zip file."""
    from backup import export_backup
    try:
        zip_path = export_backup(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{name}.zip",
    )


@router.post("/api/system/restart")
async def system_restart():
    """Restart the Xylocopa server.

    Pre-checks that the code can import successfully (catches syntax
    errors, reserved names, missing deps) before killing the current
    process.  If the check fails, returns 400 instead of restarting
    into a broken state.

    Then spawns a new instance via run.sh and exits.
    """
    import subprocess as _sp
    import sys

    # Resolve project root (two levels up from orchestrator/routers/)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    run_script = os.path.join(project_root, "run.sh")
    orchestrator_dir = os.path.join(project_root, "orchestrator")

    # --- Pre-flight import check ---
    # Spawn a fresh Python process to import main.py.  If it fails
    # (syntax error, SQLAlchemy reserved name, missing module, etc.)
    # we refuse to restart so the current server stays alive.
    try:
        check = _sp.run(
            [sys.executable, "-c", "import main"],
            cwd=orchestrator_dir,
            capture_output=True, text=True, timeout=_IMPORT_CHECK_TIMEOUT,
            env={**os.environ, "XYLOCOPA_IMPORT_CHECK": "1", "AGENTHIVE_IMPORT_CHECK": "1"},
        )
        if check.returncode != 0:
            # Extract the last meaningful error line
            err_lines = [l for l in check.stderr.strip().splitlines() if l.strip()]
            err_summary = err_lines[-1] if err_lines else "Unknown import error"
            logger.error("Restart pre-check failed: %s", err_summary)
            raise HTTPException(
                status_code=400,
                detail=f"Restart aborted — code has errors: {err_summary}",
            )
    except _sp.TimeoutExpired:
        raise HTTPException(
            status_code=400,
            detail="Restart aborted — import check timed out",
        )

    # --- Frontend stale-check + auto-rebuild ---
    # `vite preview` serves dist/ statically.  If src/ has moved ahead,
    # a restart alone wouldn't ship the new code — rebuild first.  Done
    # synchronously so a build failure aborts the restart cleanly.
    try:
        build_check = _sp.run(
            [run_script, "build-frontend-if-stale"],
            cwd=project_root,
            capture_output=True, text=True, timeout=120,
        )
        if build_check.returncode != 0:
            err = (build_check.stderr or build_check.stdout).strip().splitlines()
            err_summary = err[-1] if err else "Unknown build error"
            logger.error("Restart pre-check (frontend build) failed: %s", err_summary)
            raise HTTPException(
                status_code=400,
                detail=f"Restart aborted — frontend build failed: {err_summary}",
            )
        logger.info("Frontend build-if-stale: %s", (build_check.stdout or "").strip().splitlines()[-1:])
    except _sp.TimeoutExpired:
        raise HTTPException(
            status_code=400,
            detail="Restart aborted — frontend build timed out",
        )

    logger.warning("Restart requested via API — spawning pm2 restart")

    ecosystem = os.path.join(project_root, "ecosystem.config.cjs")

    # Let PM2 handle the restart lifecycle — it's the process manager,
    # not us.  --update-env re-reads ecosystem.config.cjs for config
    # changes (ports, max_size, env vars).
    _sp.Popen(
        ["pm2", "restart", ecosystem, "--update-env"],
        cwd=project_root,
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
    )
    return {"status": "restarting"}


def _claude_cli_version() -> str:
    """Detect installed Claude CLI version, cached after first call."""
    if not hasattr(_claude_cli_version, "_v"):
        import subprocess
        try:
            out = subprocess.check_output(["claude", "--version"], timeout=5, text=True).strip()
            _claude_cli_version._v = out.split()[0]  # "2.1.70 (Claude Code)" → "2.1.70"
        except (OSError, subprocess.SubprocessError):
            logger.warning("Claude CLI version detection failed", exc_info=True)
            _claude_cli_version._v = "0.0.0"
    return _claude_cli_version._v


_token_usage_cache: dict = {"data": None, "ts": 0.0}
_TOKEN_USAGE_TTL = 120  # seconds — avoid rate-limiting from Anthropic


@router.get("/api/system/token-usage")
async def token_usage():
    """Query Claude API token usage via OAuth credentials."""
    import time
    import json as _json
    import urllib.request
    import urllib.error

    now = time.monotonic()
    if _token_usage_cache["data"] is not None and now - _token_usage_cache["ts"] < _TOKEN_USAGE_TTL:
        return _token_usage_cache["data"]

    creds = _platform.get_claude_credentials()
    if not creds:
        raise HTTPException(
            status_code=404,
            detail="Claude credentials not found (checked platform-specific storage)",
        )

    access_token = None
    oauth = creds.get("claudeAiOauth") or {}
    access_token = oauth.get("accessToken")
    if not access_token:
        raise HTTPException(status_code=400, detail="No OAuth access token found in credentials")

    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": f"claude-code/{_claude_cli_version()}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_REQUEST_TIMEOUT) as resp:
            data = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        # On rate-limit, return stale cache if available instead of failing
        if exc.code == 429 and _token_usage_cache["data"] is not None:
            return _token_usage_cache["data"]
        # Remap upstream auth errors (401/403) to 502 so the frontend doesn't
        # conflate an expired Anthropic OAuth token with the user's Xylocopa
        # session expiring — otherwise a 401 here triggers handle401 → logout.
        status = 502 if exc.code in (401, 403) else exc.code
        raise HTTPException(status_code=status, detail=f"Anthropic API error: {body}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if _token_usage_cache["data"] is not None:
            return _token_usage_cache["data"]
        raise HTTPException(status_code=502, detail=f"Failed to reach Anthropic API: {exc}")

    # Return only the fields the frontend needs
    result = {}
    five_hour = data.get("five_hour")
    if five_hour:
        result["session"] = {
            "utilization": five_hour.get("utilization"),
            "resets_at": five_hour.get("resets_at"),
        }
    seven_day = data.get("seven_day")
    if seven_day:
        result["weekly"] = {
            "utilization": seven_day.get("utilization"),
            "resets_at": seven_day.get("resets_at"),
        }

    # Model-scoped weekly buckets (e.g. Fable) only appear in the newer
    # `limits` array — the legacy top-level fields (seven_day_opus, ...) are
    # null for these accounts.
    scoped = []
    for lim in data.get("limits") or []:
        if lim.get("kind") != "weekly_scoped":
            continue
        model = (lim.get("scope") or {}).get("model") or {}
        label = model.get("display_name")
        if not label:
            continue
        scoped.append({
            "label": label,
            "utilization": lim.get("percent"),
            "resets_at": lim.get("resets_at"),
        })
    if scoped:
        result["weekly_scoped"] = scoped

    _token_usage_cache["data"] = result
    _token_usage_cache["ts"] = now
    return result


# ---- Settings ----

@router.get("/api/settings/notifications")
async def get_notification_settings(db: Session = Depends(get_db)):
    """Get the legacy global notification flags.

    Kept for backward compatibility only: these flags no longer gate any
    push and the UI does not use them.  Each device's switches live on its
    push subscription (POST /api/push/device).  init_db seeded existing
    subscriptions from these values once, when the per-device columns
    were added.
    """
    agents_row = db.get(SystemConfig, "notifications_agents_enabled")
    tasks_row = db.get(SystemConfig, "notifications_tasks_enabled")
    return {
        "agents_enabled": agents_row.value != "0" if agents_row else True,
        "tasks_enabled": tasks_row.value != "0" if tasks_row else True,
    }


@router.put("/api/settings/notifications")
async def update_notification_settings(request: Request, db: Session = Depends(get_db)):
    """Update the legacy global notification flags (no effect on pushes)."""
    body = await request.json()
    for key in ("agents_enabled", "tasks_enabled"):
        if key in body:
            db_key = f"notifications_{key}"
            row = db.get(SystemConfig, db_key)
            val = "1" if body[key] else "0"
            if row:
                row.value = val
            else:
                db.add(SystemConfig(key=db_key, value=val))
    db.commit()
    return await get_notification_settings(db)


# ---- Sync Audit / Full Scan ----

@router.get("/api/sync/audit/{agent_id}")
async def run_sync_audit(agent_id: str, request: Request):
    """Run sync full scan (read-only audit + pointer reset) for a specific agent."""
    ad = request.app.state.agent_dispatcher
    ctx = ad._sync_contexts.get(agent_id)
    if not ctx:
        raise HTTPException(404, f"No sync context for agent {agent_id}")

    from sync_engine import sync_full_scan
    result = await sync_full_scan(ad, ctx, reason="manual")

    return {
        "agent_id": agent_id,
        **result,
    }


@router.get("/api/sync/status")
async def sync_status(request: Request):
    """Overview of sync state for all agents."""
    ad = request.app.state.agent_dispatcher

    agents = []
    for agent_id, ctx in ad._sync_contexts.items():
        agents.append({
            "agent_id": agent_id,
            "session_id": ctx.session_id,
            "jsonl_path": ctx.jsonl_path,
            "last_turn_count": ctx.last_turn_count,
            "last_offset": ctx.last_offset,
        })

    return {
        "idle_agents": len(agents),
        "agents": agents,
    }


@router.get("/api/debug/mem-introspect")
async def mem_introspect(request: Request, collect: int = 0):
    """Break down where this process's RSS is going.

    Counts live Python objects by type, sizes named in-memory caches we
    know about, and reports gc + /proc stats. If `collect=1`, runs
    gc.collect() and malloc_trim(0) first so the breakdown reflects
    "what's actually retained" instead of "what hasn't been swept yet".
    """
    import gc
    import sys
    from collections import Counter

    if collect:
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

    # 1. /proc/self/status — authoritative RSS
    proc_status = {}
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith(("VmPeak:", "VmSize:", "VmRSS:",
                                    "VmData:", "VmStk:", "VmExe:",
                                    "RssAnon:", "RssFile:", "RssShmem:",
                                    "Threads:")):
                    k, v = line.split(":", 1)
                    proc_status[k.strip()] = v.strip()
    except OSError:
        pass

    # 2. gc stats
    gc_stats = {
        "counts": gc.get_count(),
        "stats": gc.get_stats(),
        "total_objects": len(gc.get_objects()),
    }

    # 3. Top object types by instance count + by aggregate size for the
    # top types. This is the "what's eating RAM" answer.
    type_counter: Counter = Counter()
    for obj in gc.get_objects():
        type_counter[type(obj).__name__] += 1
    top_types = type_counter.most_common(20)

    # 4. Named caches we own — these are the candidate leak sources we
    # identified during the audit. Each entry: (label, size, details).
    caches: list[dict] = []

    def _record(label: str, obj):
        try:
            if isinstance(obj, dict):
                caches.append({"name": label, "kind": "dict", "len": len(obj),
                               "bytes": sys.getsizeof(obj)})
            elif isinstance(obj, (set, list)):
                caches.append({"name": label, "kind": type(obj).__name__,
                               "len": len(obj), "bytes": sys.getsizeof(obj)})
        except Exception:
            pass

    try:
        ad = request.app.state.agent_dispatcher
        for attr in ("_sync_tasks", "_sync_wake", "_sync_manual_wake",
                     "_sync_locks", "_sync_contexts", "_generation_ids",
                     "_generating_agents", "_promote_ack_pending",
                     "_promote_ack_by_agent", "_launch_tasks",
                     "_launching_panes", "_launch_session_futures",
                     "_stale_session_retries", "_idle_no_pane_retries",
                     "_known_subagents"):
            v = getattr(ad, attr, None)
            if v is not None:
                _record(f"AgentDispatcher.{attr}", v)
    except Exception:
        pass

    try:
        import display_writer as _dw
        _record("display_writer._pre_sent_index", _dw._pre_sent_index)
        _record("display_writer._pre_sent_index_ready", _dw._pre_sent_index_ready)
    except Exception:
        pass

    try:
        import session_cache as _sc
        _record("session_cache._encoded_name_cache", _sc._encoded_name_cache)
    except Exception:
        pass

    try:
        import skills as _sk
        _record("skills._cache", _sk._cache)
    except Exception:
        pass

    try:
        from agent_dispatcher import _translate_cache
        _record("agent_dispatcher._translate_cache", _translate_cache)
    except Exception:
        pass

    try:
        from routers.projects import _INSIGHT_RUNS, _claudemd_jobs, _progress_jobs
        _record("routers.projects._INSIGHT_RUNS", _INSIGHT_RUNS)
        _record("routers.projects._claudemd_jobs", _claudemd_jobs)
        _record("routers.projects._progress_jobs", _progress_jobs)
    except Exception:
        pass

    try:
        from routers.system import _reload_ts_by_ip, _reload_storm_last_warned
        _record("routers.system._reload_ts_by_ip", _reload_ts_by_ip)
        _record("routers.system._reload_storm_last_warned", _reload_storm_last_warned)
    except Exception:
        pass

    caches.sort(key=lambda c: -c["len"])

    return {
        "rss_mb": int(proc_status.get("VmRSS", "0 kB").split()[0]) // 1024 if proc_status.get("VmRSS") else None,
        "proc_status": proc_status,
        "gc": gc_stats,
        "top_types": [{"type": t, "count": c} for t, c in top_types],
        "named_caches": caches,
        "collected": bool(collect),
    }


@router.get("/api/debug/clear-cache", response_class=HTMLResponse)
async def clear_client_cache():
    """Serve a page that clears browser-side caches and redirects back.

    Auth-exempt so it works even when the PWA is stuck on stale code.
    Clears: filebrowser localStorage, SW caches, then reloads.
    """
    return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Clearing cache…</title>
<style>body{font-family:system-ui;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;background:#111;color:#eee}
.box{text-align:center;padding:2rem}
</style></head><body><div class="box"><p id="status">Clearing caches…</p></div>
<script>
(async function() {
  const el = document.getElementById('status');
  const log = (m) => { el.textContent = m; console.log(m); };
  try {
    // 1. Clear filebrowser localStorage entries
    const toRemove = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith('filebrowser:')) toRemove.push(k);
    }
    toRemove.forEach(k => localStorage.removeItem(k));
    log('Cleared ' + toRemove.length + ' filebrowser entries');

    // 2. Unregister all service workers
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      for (const r of regs) await r.unregister();
      log('Unregistered ' + regs.length + ' service workers');
    }

    // 3. Clear Cache API
    if ('caches' in window) {
      const names = await caches.keys();
      for (const n of names) await caches.delete(n);
      log('Deleted ' + names.length + ' cache stores');
    }

    log('Done! Redirecting…');
    setTimeout(() => { window.location.href = '/'; }, 800);
  } catch(e) {
    log('Error: ' + e.message);
  }
})();
</script></body></html>""")

