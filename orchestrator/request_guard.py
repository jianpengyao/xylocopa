"""Request guard — Host allowlist + Origin check in front of every route.

Pure ASGI middleware covering both "http" and "websocket" scopes. It runs
regardless of DISABLE_AUTH and is installed as the outermost user
middleware in main.py, so nothing behind it (auth, CORS, routes, the
/ws/* handlers) ever sees a request it rejects.

1. Host allowlist (DNS rebinding). A page on attacker.example that re-points
   its own name at this machine reaches the backend with
   "Host: attacker.example". Allowed: no Host header; any IP literal (IPv4
   or bracketed IPv6, any port: LAN, Tailscale and node-share addresses
   such as 100.64.0.10:3000 that are not this host's interface IPs);
   localhost / *.localhost; this machine's hostname (and <name>.local);
   *.ts.net; and the XY_ALLOWED_HOSTS entries ("*" allows any host).

2. Origin check for unsafe methods (anything but GET/HEAD/OPTIONS) and every
   WebSocket handshake. Closes text/plain drive-by POSTs (no CORS preflight)
   and cross-site WebSocket hijacking of /ws/status and /ws/terminal, which
   browsers never block on their own. No Origin header → allowed (curl,
   agent hooks, the MCP server). Anything else must equal the Host header's
   host:port, or be listed in CORS_ORIGINS.
   "Origin: null" (opaque origin) is refused on EVERY method, reads included,
   except for sandboxed preview content (/api/preview/t/, /api/preview/p/):
   CORSMiddleware grants "null" so those previews' preflights pass, which
   would otherwise let any site's sandboxed iframe read /api responses.

3. Agent hook channel (/api/hooks/*), when ``hook_port`` is set: the Host
   must be loopback on the backend's own port. Agents call
   http://localhost:PORT directly; the HTTPS frontend proxy forwards the
   browser's Host (e.g. 100.64.0.10:3000), so anything else came through a
   proxy — a second line behind the proxy's own /api/hooks/ refusal.

4. Every rejection is logged at WARNING ("REQUEST_GUARD ...") at most once
   per minute per (reason, host/origin), with a count of the suppressed
   repeats, so a misconfiguration is diagnosable from orchestrator.log.
"""

import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse

logger = logging.getLogger("orchestrator")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
# Sandboxed preview iframes (opaque origin) send "Origin: null" from their
# XHR/fetch and WebSockets; all of it targets the path-token routes.
_NULL_ORIGIN_PREFIXES = ("/api/preview/t/", "/api/preview/p/")
_HOSTNAME_RE = re.compile(r"^[a-z0-9_.-]+$")
_LOOPBACK_ONLY_PREFIXES = ("/api/hooks/",)
_REJECT_BODY = {
    "host": "Host not allowed",
    "origin": "Cross-origin request blocked",
    "loopback": "Hook endpoints are loopback-only",
}

_LOG_INTERVAL_S = 60.0
_LOG_KEYS_MAX = 512
# WebSocket close before accept → uvicorn answers the handshake with 403.
_WS_POLICY_VIOLATION = 1008
# Own alias so tests can fake the rate-limit clock without patching
# time.monotonic process-wide (the asyncio loop clock reads it too).
_monotonic = time.monotonic


def split_host(value: str) -> tuple[str, int | None] | None:
    """Parse "host[:port]" (Host header / Origin netloc).

    Returns (hostname, port-or-None) with the hostname lowercased, a trailing
    dot dropped and IP literals in canonical form without brackets, or None
    when the value is malformed.
    """
    v = (value or "").strip()
    # "%" only appears in an IPv6 zone ID ("fe80::1%eth0"), which browsers
    # never send and ipaddress accepts with almost any character after it:
    # a "?" or "#" there truncates Starlette's request.url (rebuilt from the
    # Host header) to a different path than the router dispatches on.
    if not v or "@" in v or "%" in v:
        return None
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return None
        try:
            name = str(ipaddress.IPv6Address(v[1:end]))
        except ValueError:
            return None
        rest = v[end + 1:]
        if rest and not rest.startswith(":"):
            return None
        port_s = rest[1:]
    else:
        # Unbracketed IPv6 ("::1", "fe80::1:8080") is not valid Host syntax
        # and ambiguous with a port: it fails the name or port check below.
        name, _, port_s = v.partition(":")
        name = name.lower().rstrip(".")
        if not name or not _HOSTNAME_RE.match(name):
            return None
        try:
            name = str(ipaddress.IPv4Address(name))
        except ValueError:
            pass
    port = None
    if port_s:
        if not (port_s.isascii() and port_s.isdigit()) or int(port_s) > 65535:
            return None
        port = int(port_s)
    return name, port


def parse_origin(value: str) -> tuple[str, str, int | None] | None:
    """Parse an Origin header → (scheme, hostname, port), with the scheme's
    default port filled in when the header has none. None when malformed."""
    try:
        parts = urlsplit((value or "").strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if not scheme or not parts.netloc:
        return None
    hp = split_host(parts.netloc)
    if hp is None:
        return None
    name, port = hp
    return scheme, name, port if port is not None else _DEFAULT_PORTS.get(scheme)


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _is_loopback(name: str) -> bool:
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _local_hostnames(hostname: str | None) -> set[str]:
    """This machine's hostname, its first label, and their mDNS ".local" forms
    (gethostname() may return an FQDN such as host.dept.example.edu)."""
    if hostname is None:
        try:
            hostname = socket.gethostname()
        except OSError:
            return set()
    full = hostname.strip().lower().rstrip(".")
    names = set()
    for n in {full, full.split(".")[0]}:
        if n:
            names.update((n, n + ".local"))
    return names


class RequestGuardMiddleware:
    """See module docstring. ``hostname`` defaults to socket.gethostname();
    ``hook_port`` is the backend's own port (None disables rule 3)."""

    def __init__(self, app, allowed_hosts=(), cors_origins=(), hostname=None,
                 hook_port=None):
        self.app = app
        self._hook_port = hook_port
        self._any_host = False
        self._exact_hosts: set[str] = set()
        self._suffix_hosts: list[str] = []
        for entry in allowed_hosts or ():
            entry = (entry or "").strip().lower()
            if not entry:
                continue
            if entry == "*":
                self._any_host = True
            elif entry.startswith("."):
                suffix = entry.rstrip(".")
                if len(suffix) > 1:  # a lone "." must not match every name
                    self._suffix_hosts.append(suffix)
            else:
                hp = split_host(entry)
                if hp is not None:
                    self._exact_hosts.add(hp[0])
        self._local_names = _local_hostnames(hostname)
        self._any_origin = False
        self._cors_origins: set[tuple] = set()
        for o in cors_origins or ():
            o = (o or "").strip()
            if o == "*":
                self._any_origin = True
                continue
            parsed = parse_origin(o)
            if parsed is not None:
                self._cors_origins.add(parsed)
        self._last_logged: dict[tuple[str, str], float] = {}
        self._suppressed: dict[tuple[str, str], int] = {}

    # ---- checks ----

    def _host_allowed(self, name: str) -> bool:
        if _is_ip_literal(name):
            return True
        if name == "localhost" or name.endswith(".localhost"):
            return True
        if name in self._local_names or name.endswith(".ts.net"):
            return True
        if self._any_host or name in self._exact_hosts:
            return True
        return any(name == s[1:] or name.endswith(s) for s in self._suffix_hosts)

    def _origin_allowed(self, origin: str, host: tuple[str, int | None] | None,
                        path: str) -> bool:
        if origin.strip() == "null":
            return path.startswith(_NULL_ORIGIN_PREFIXES)
        parsed = parse_origin(origin)
        if parsed is None:
            return False
        if self._any_origin or parsed in self._cors_origins:
            return True
        if host is None:
            return False
        scheme, o_name, o_port = parsed
        h_name, h_port = host
        if h_port is None:
            # The backend sits behind a proxy and cannot see the browser's
            # scheme; a Host without a port means the Origin scheme's default.
            h_port = _DEFAULT_PORTS.get(scheme)
        return o_port is not None and o_name == h_name and o_port == h_port

    def check(self, scope) -> tuple[str, str, str] | None:
        """Return (reason, host, origin) when the request must be rejected."""
        hosts, origins = [], []
        for k, v in scope.get("headers") or ():
            if k == b"host":
                hosts.append(v.decode("latin-1"))
            elif k == b"origin":
                origins.append(v.decode("latin-1"))
        host_raw = hosts[0] if hosts else ""
        origin_raw = origins[0] if origins else ""

        host = None
        if hosts:
            host = split_host(host_raw) if len(hosts) == 1 else None
            if host is None or not self._host_allowed(host[0]):
                return "host", host_raw, origin_raw

        path = scope.get("path", "")
        if (self._hook_port is not None and host is not None
                and path.startswith(_LOOPBACK_ONLY_PREFIXES)
                and not (_is_loopback(host[0]) and host[1] in (None, self._hook_port))):
            return "loopback", host_raw, origin_raw

        if origin_raw.strip() == "null" and not path.startswith(_NULL_ORIGIN_PREFIXES):
            return "origin", host_raw, origin_raw   # reads too — see docstring
        if scope["type"] == "http" and scope.get("method", "GET").upper() in _SAFE_METHODS:
            return None
        if not origins:
            return None
        if len(origins) > 1 or not self._origin_allowed(origin_raw, host, path):
            return "origin", host_raw, origin_raw
        return None

    # ---- ASGI ----

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        rejected = self.check(scope)
        if rejected is None:
            return await self.app(scope, receive, send)
        reason, host, origin = rejected
        self._log_reject(scope, reason, host, origin)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION, "reason": ""})
            return
        await PlainTextResponse(_REJECT_BODY[reason], status_code=403)(scope, receive, send)

    def _log_reject(self, scope, reason: str, host: str, origin: str) -> None:
        key = (reason, (origin if reason == "origin" else host)[:200])
        now = _monotonic()
        last = self._last_logged.get(key)
        if last is not None and now - last < _LOG_INTERVAL_S:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return
        if len(self._last_logged) >= _LOG_KEYS_MAX:
            self._last_logged.clear()
            self._suppressed.clear()
        self._last_logged[key] = now
        repeats = self._suppressed.pop(key, 0)
        client = scope.get("client")
        logger.warning(
            "REQUEST_GUARD reject %s: %s %s host=%r origin=%r client=%s%s",
            reason,
            "WS" if scope["type"] == "websocket" else scope.get("method", "?"),
            (scope.get("path") or "")[:200], host[:200], origin[:200],
            f"{client[0]}:{client[1]}" if client else "?",
            f" (+{repeats} similar in the last minute)" if repeats else "",
        )
