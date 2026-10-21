"""Authentication utilities — password hashing, signed tokens, limiters.

Uses Python stdlib for tokens + bcrypt for password hashing.
Supports transparent migration from legacy SHA-256 hashes on login.
Server-side sessions live in auth_store.py; this module stays DB-light
(SystemConfig helpers only) so the reset-password CLI can import it.
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
from collections import OrderedDict, deque

import bcrypt
from starlette.responses import JSONResponse

import config
import env_file

logger = logging.getLogger("orchestrator.auth")


def is_disable_auth_value(value: str | None) -> bool:
    """True when a DISABLE_AUTH value turns auth off ("1", "true", "yes")."""
    return (value or "").strip() in ("1", "true", "yes")


def auth_disabled() -> bool:
    """True when DISABLE_AUTH turns every password/session check off.

    Read per call: tests flip the env at runtime, and the Monitor > Security
    switch (write_disable_auth) flips it in a running server."""
    return is_disable_auth_value(os.environ.get("DISABLE_AUTH"))


# ---------------------------------------------------------------------------
# Password-protection switch: DISABLE_AUTH in the project .env
# ---------------------------------------------------------------------------
# .env is the one source of truth. pm2 injects it at start (and on
# `pm2 restart --update-env`); config.py loads it with override=False.

def env_file_disable_auth() -> bool | None:
    """What .env says right now (read fresh): True = protection off, False =
    on, None when the file or the DISABLE_AUTH line is missing/unreadable."""
    value = env_file.read_key(env_file.ENV_PATH, "DISABLE_AUTH")
    return None if value is None else is_disable_auth_value(value)


def write_disable_auth(disabled: bool) -> None:
    """Switch protection: rewrite .env first, then the process env, so it
    applies at once and survives a restart. Raises OSError when .env can't
    be written; the running state is then unchanged."""
    value = "1" if disabled else "0"
    env_file.set_key(env_file.ENV_PATH, "DISABLE_AUTH", value)
    os.environ["DISABLE_AUTH"] = value


def sync_disable_auth_from_env_file() -> None:
    """At startup, follow DISABLE_AUTH in .env when the file sets it.

    pm2 replays the env it captured at the last start on a crash respawn and
    on `pm2 resurrect`, which can predate a switch made in Monitor > Security;
    without a DISABLE_AUTH line the process env is left as is."""
    value = env_file.read_key(env_file.ENV_PATH, "DISABLE_AUTH")
    if value is None:
        return
    if is_disable_auth_value(value) != auth_disabled():
        logger.warning("AUTH_PROTECTION %s: process env disagreed with .env DISABLE_AUTH; following .env",
                       "off" if is_disable_auth_value(value) else "on")
    os.environ["DISABLE_AUTH"] = value


# ---------------------------------------------------------------------------
# Session-failure responses (contract: X-Auth-Reason on session 401s only)
# ---------------------------------------------------------------------------

# One body text per X-Auth-Reason. Wrong-password / preview / port-signature
# 401s never carry the header, so the frontend logs out only on these.
AUTH_REASON_DETAIL = {
    "missing": "Not authenticated",
    "invalid": "Token expired or invalid",
    "expired": "Session expired",
    "revoked": "Session revoked",
    "idle": "Locked after inactivity",
}


def auth_error_response(reason: str) -> JSONResponse:
    """401 for a session failure: detail text + X-Auth-Reason, never cached."""
    return JSONResponse(
        {"detail": AUTH_REASON_DETAIL.get(reason, AUTH_REASON_DETAIL["invalid"])},
        status_code=401,
        headers={"X-Auth-Reason": reason, "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Password hashing (bcrypt with transparent SHA-256 migration)
# ---------------------------------------------------------------------------

def _sanitize_password(password: str) -> str:
    """Strip null bytes — some bcrypt implementations truncate at \\x00."""
    return password.replace("\x00", "")


def hash_password(password: str) -> str:
    """Hash a password with bcrypt (cost factor 12)."""
    password = _sanitize_password(password)
    # Pre-hash with SHA-256 so passwords >72 bytes aren't silently truncated by bcrypt
    derived = hashlib.sha256(password.encode()).hexdigest().encode()
    return bcrypt.hashpw(derived, bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored hash.

    Supports two formats:
    - bcrypt: starts with "$2b$"
    - legacy SHA-256: "salt:hex_digest" (auto-migrated on successful verify)
    """
    password = _sanitize_password(password)
    if stored_hash.startswith("$2b$"):
        derived = hashlib.sha256(password.encode()).hexdigest().encode()
        return bcrypt.checkpw(derived, stored_hash.encode())

    # Legacy SHA-256 format: "salt:sha256hex"
    if ":" not in stored_hash:
        return False
    salt, h = stored_hash.split(":", 1)
    return hmac.compare_digest(
        hashlib.sha256((salt + password).encode()).hexdigest(), h
    )


def needs_rehash(stored_hash: str) -> bool:
    """Return True if the stored hash uses a legacy algorithm."""
    return not stored_hash.startswith("$2b$")


class LoopLocal:
    """One asyncio primitive per running event loop.

    asyncio.Lock/Semaphore bind to the first loop that contends on them;
    tests run many loops in one process, production runs one. Rebuilding
    on a loop change keeps a module-level primitive safe for both."""

    def __init__(self, factory):
        self._factory = factory
        self._loop = None
        self._obj = None

    def __call__(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._obj = loop, self._factory()
        return self._obj


# bcrypt costs ~0.25 s of CPU: run it off the event loop, at most two at once
# so a burst of guesses can't occupy every threadpool worker.
_BCRYPT_SLOTS = LoopLocal(lambda: asyncio.Semaphore(2))


async def verify_password_async(password: str, stored_hash: str) -> bool:
    """verify_password in a worker thread (bounded by _BCRYPT_SLOTS)."""
    async with _BCRYPT_SLOTS():
        return await asyncio.to_thread(verify_password, password, stored_hash)


async def hash_password_async(password: str) -> str:
    """hash_password in a worker thread (bounded by _BCRYPT_SLOTS)."""
    async with _BCRYPT_SLOTS():
        return await asyncio.to_thread(hash_password, password)


# ---------------------------------------------------------------------------
# Token management (custom signed payload — stdlib only)
# ---------------------------------------------------------------------------

def create_token(jwt_secret: str, expires_minutes: float | None = None,
                 extra_claims: dict | None = None, *, now: float | None = None) -> str:
    """Create a signed token with expiry.

    Format: base64(json_payload).base64(signature)

    Sessions are opaque server-side rows (auth_store.py) since Auth Step 3.
    This format now signs only SCOPED capabilities: media (?token= on media
    GETs), preview (path) — always pass extra_claims with a "scope". An
    unscoped token is a legacy session JWT, accepted only inside the
    auth_store legacy window; new code must never mint one
    (tests/test_auth_store.py greps for scope-less calls).

    extra_claims merges additional payload fields. Tokens carrying a "scope"
    claim are rejected by verify_token — they only pass the endpoint-specific
    decode_token checks. expires_minutes defaults to 24h; now overrides the
    clock (tests).
    """
    if expires_minutes is None:
        expires_minutes = 24 * 60
    issued = time.time() if now is None else now
    payload = {
        "exp": issued + expires_minutes * 60,
        "iat": issued,
        "jti": secrets.token_hex(8),
    }
    if extra_claims:
        payload.update(extra_claims)
    payload_bytes = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=")
    sig = hmac.new(
        jwt_secret.encode(), payload_bytes, hashlib.sha256
    ).digest()
    sig_bytes = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return payload_bytes.decode() + "." + sig_bytes.decode()


def decode_token(token: str, jwt_secret: str) -> dict | None:
    """Verify token signature + expiry and return the payload dict, or None.

    Accepts any scope — callers that handle scoped tokens (e.g. preview)
    must check the "scope" claim themselves.
    """
    payload = peek_token(token, jwt_secret)
    if payload is None or payload.get("exp", 0) < time.time():
        return None
    return payload


def peek_token(token: str, jwt_secret: str) -> dict | None:
    """Verify the signature only and return the payload dict, or None.

    Expiry is NOT checked — for callers that must tell "expired" from
    "invalid" or run on an injected clock (auth_store)."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts

        # Verify signature
        expected_sig = hmac.new(
            jwt_secret.encode(), payload_b64.encode(), hashlib.sha256
        ).digest()
        # Re-pad base64
        sig_padded = sig_b64 + "=" * (-len(sig_b64) % 4)
        actual_sig = base64.urlsafe_b64decode(sig_padded)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload_padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_padded))
        if not isinstance(payload, dict):
            return None
        return payload
    except (ValueError, KeyError, json.JSONDecodeError, base64.binascii.Error, UnicodeDecodeError) as e:
        logger.debug("Token validation failed: %s", type(e).__name__)
        return None


def verify_token(token: str, jwt_secret: str) -> bool:
    """Verify a legacy session JWT (signature + expiry). Legacy only: the
    middleware authenticates through auth_store.authenticate, which also
    applies the sunset window; kept for tests/test_auth.py.

    Scoped tokens (e.g. scope="preview") are rejected — they ride in URLs
    and must never grant general API access.
    """
    payload = decode_token(token, jwt_secret)
    return payload is not None and not payload.get("scope")


# ---------------------------------------------------------------------------
# Client IP
# ---------------------------------------------------------------------------

def normalize_ip(host: str | None) -> str:
    """Canonical text form of a client address ("::ffff:1.2.3.4" -> "1.2.3.4").

    Non-IP values (a test client's "testclient", "unknown") pass through
    stripped, so they still key the limiter consistently."""
    if not host:
        return "unknown"
    host = host.strip()
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def is_loopback(ip: str | None) -> bool:
    """True for 127.0.0.0/8 and ::1 (and their v4-mapped forms)."""
    try:
        return ipaddress.ip_address(normalize_ip(ip).strip("[]")).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class LoginRateLimiter:
    """In-memory per-client lockout that can never lock the owner out.

    Keys are a normalized client IP (login) or "s:<session id>"
    (change-password). After `threshold` failures the key is locked for
    base_seconds * 2^(locks-1), capped at `max_seconds`, and its failure
    count restarts at 0 — a served lock decays instead of relocking on the
    next single failure. An entry is forgotten after `forget_seconds`
    without failures (and while unlocked), or when its first failure
    predates `reset_at` (SystemConfig auth_limiter_reset_at, written by
    ./run.sh reset-password). At most `max_entries` keys are kept. Callers
    skip loopback clients entirely. Restarting the server clears all state.
    `now` overrides the clock (tests).

    check() and record_failure() sit on either side of an awaited bcrypt,
    so callers run each attempt inside `one_at_a_time(key)` and check()
    again there: a concurrent burst from one key then sees the failures
    recorded before it, and only `threshold` attempts reach bcrypt.
    """

    def __init__(self, threshold: int = config.AUTH_LIMIT_THRESHOLD,
                 base_seconds: int = config.AUTH_LIMIT_BASE_S,
                 max_seconds: int = config.AUTH_LIMIT_MAX_S, *,
                 forget_seconds: float = config.AUTH_LIMIT_FORGET_S,
                 max_entries: int = config.AUTH_LIMIT_MAX_ENTRIES):
        self.threshold = threshold
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self.forget_seconds = forget_seconds
        self.max_entries = max_entries
        # key -> {"failures", "locks", "locked_until", "first_failure",
        # "last_failure"}; insertion order = least recently failed first.
        self._state: OrderedDict[str, dict] = OrderedDict()
        # key -> [asyncio.Lock, holders + waiters], only while in use.
        self._attempts = LoopLocal(dict)

    def __len__(self) -> int:
        return len(self._state)

    def _lockout_duration(self, locks: int) -> int:
        return min(self.base_seconds * (2 ** (locks - 1)), self.max_seconds)

    def _live_entry(self, key: str, now: float, reset_at: float) -> dict | None:
        """The entry for key, after dropping it if forgotten or reset."""
        entry = self._state.get(key)
        if entry is None:
            return None
        forgotten = (entry["locked_until"] <= now
                     and now - entry["last_failure"] > self.forget_seconds)
        if forgotten or entry["first_failure"] < reset_at:
            del self._state[key]
            return None
        return entry

    def check(self, ip: str, *, now: float | None = None,
              reset_at: float = 0.0) -> tuple[bool, int]:
        """Check if a key is locked. Returns (is_locked, seconds_remaining)."""
        now = time.time() if now is None else now
        entry = self._live_entry(ip, now, reset_at)
        if entry is None:
            return False, 0
        remaining = entry["locked_until"] - now
        if remaining <= 0:
            return False, 0
        return True, int(remaining) + 1

    def record_failure(self, ip: str, *, now: float | None = None,
                       reset_at: float = 0.0) -> tuple[bool, int]:
        """Record a failed attempt. Returns (now_locked, lock_seconds)."""
        now = time.time() if now is None else now
        entry = self._live_entry(ip, now, reset_at)
        if entry is None:
            self._make_room(now)
            entry = {"failures": 0, "locks": 0, "locked_until": 0.0,
                     "first_failure": now, "last_failure": now}
            self._state[ip] = entry
        else:
            self._state.move_to_end(ip)
        entry["failures"] += 1
        entry["last_failure"] = now
        if entry["failures"] >= self.threshold:
            entry["locks"] += 1
            entry["failures"] = 0
            duration = self._lockout_duration(entry["locks"])
            entry["locked_until"] = now + duration
            return True, duration
        return False, 0

    def record_success(self, ip: str) -> None:
        """Clear failure count on successful login."""
        self._state.pop(ip, None)

    @contextlib.asynccontextmanager
    async def one_at_a_time(self, key: str | None):
        """Run attempts for one key one at a time (None: no limit, e.g. loopback)."""
        if key is None:
            yield
            return
        slots = self._attempts()
        slot = slots.setdefault(key, [asyncio.Lock(), 0])
        slot[1] += 1
        try:
            async with slot[0]:
                yield
        finally:
            slot[1] -= 1
            if not slot[1]:
                del slots[key]

    def _make_room(self, now: float) -> None:
        """Before adding a key, keep at most max_entries: drop the oldest
        unlocked entry, else the oldest entry."""
        while self._state and len(self._state) >= self.max_entries:
            victim = next((k for k, e in self._state.items()
                           if e["locked_until"] <= now), None)
            if victim is None:
                self._state.popitem(last=False)
            else:
                del self._state[victim]


class GlobalFailureMeter:
    """Global wrong-password backstop: slows every password check while many
    clients fail at once (a distributed guess), never refuses one.

    Above `soft_limit` failures within `window_seconds`, each check first
    sleeps step_seconds per extra failure, capped at max_delay."""

    def __init__(self, window_seconds: float = config.AUTH_GLOBAL_WINDOW_S,
                 soft_limit: int = config.AUTH_GLOBAL_SOFT,
                 step_seconds: float = config.AUTH_GLOBAL_STEP_S,
                 max_delay: float = config.AUTH_GLOBAL_MAX_DELAY_S):
        self.window_seconds = window_seconds
        self.soft_limit = soft_limit
        self.step_seconds = step_seconds
        self.max_delay = max_delay
        # The delay saturates long before this many events; bound memory.
        cap = soft_limit + round(max_delay / step_seconds) + 1 if step_seconds > 0 else soft_limit + 1
        self._events: deque[float] = deque(maxlen=max(cap, 1))

    def record(self, *, now: float | None = None) -> None:
        self._events.append(time.time() if now is None else now)

    def count(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        while self._events and now - self._events[0] > self.window_seconds:
            self._events.popleft()
        return len(self._events)

    def delay(self, *, now: float | None = None) -> float:
        extra = self.count(now=now) - self.soft_limit
        if extra <= 0:
            return 0.0
        return min(self.max_delay, self.step_seconds * extra)

    async def throttle(self) -> float:
        """Sleep the current delay (if any) and return it."""
        d = self.delay()
        if d > 0:
            await asyncio.sleep(d)
        return d

    def clear(self) -> None:
        self._events.clear()


# Singletons — live in memory, cleared on server restart. Login and
# change-password use separate buckets (a wrong current password must not
# lock the device's login, and vice versa).
login_limiter = LoginRateLimiter()
change_password_limiter = LoginRateLimiter()
global_failures = GlobalFailureMeter()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_jwt_secret(db_session) -> str:
    """Get the JWT secret from SystemConfig, creating if missing."""
    from models import SystemConfig

    row = db_session.get(SystemConfig, "jwt_secret")
    if row:
        return row.value
    secret = secrets.token_hex(32)
    db_session.add(SystemConfig(key="jwt_secret", value=secret))
    db_session.commit()
    return secret


def rotate_jwt_secret(db_session, commit: bool = True) -> str:
    """Rotate JWT secret, invalidating all existing tokens (media, preview,
    legacy JWTs) and port-preview signatures. Only `./run.sh reset-password
    --rotate-secret` does this."""
    secret = secrets.token_hex(32)
    set_config(db_session, "jwt_secret", secret, commit=commit)
    return secret


def get_password_hash(db_session) -> str | None:
    """Get stored password hash, or None if not set."""
    from models import SystemConfig

    row = db_session.get(SystemConfig, "password_hash")
    return row.value if row else None


def get_config(db_session, key: str, default: str | None = None) -> str | None:
    """Read one SystemConfig value (a string), or default when absent."""
    from models import SystemConfig

    row = db_session.get(SystemConfig, key)
    return row.value if row else default


def set_config(db_session, key: str, value, commit: bool = True) -> None:
    """Upsert one SystemConfig value (stored as str)."""
    from models import SystemConfig

    row = db_session.get(SystemConfig, key)
    if row:
        row.value = str(value)
    else:
        db_session.add(SystemConfig(key=key, value=str(value)))
    if commit:
        db_session.commit()


def store_password_hash(db_session, password_hash: str, commit: bool = True) -> None:
    """Store an already-computed password hash (see hash_password_async)."""
    set_config(db_session, "password_hash", password_hash, commit=commit)


def set_password_hash(db_session, password: str) -> None:
    """Hash (bcrypt, blocking) and store a new password."""
    store_password_hash(db_session, hash_password(password))
