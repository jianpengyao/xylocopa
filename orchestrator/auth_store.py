"""Server-side auth sessions, media tokens and WebSocket tickets.

A login issues an opaque session token ("xys_" + 256 random bits); the DB
keeps only its sha256 in auth_sessions, so a session can be revoked one at a
time and idle-locked by the server:

- valid while not revoked, before expires_at (AUTH_SESSION_MAX_DAYS from
  login) and not idle (now - last_active_at > auth_idle_timeout_minutes;
  0 = off). An idle session keeps its row but answers "idle" until a
  password login replaces it.
- only requests flagged as user activity move last_active_at. Writes are
  throttled (ACTIVITY_WRITE_S / SEEN_WRITE_S per session); reads are always
  fresh, so a revoke from the reset CLI or another process applies at once.

Legacy session JWTs (unscoped create_token output, before this module) are
accepted only inside a sunset window anchored on auth_sessions_epoch, the
first boot of this code. Media tokens are scope=media JWTs bound to a
session; WebSocket tickets are single-use, in memory, 30 s.

The tables live on their own declarative base and are created lazily, once
per engine (ensure_auth_schema), so lifespan-off servers, tests that patch
SessionLocal and the reset CLI all work without init_db.
"""

import dataclasses
import hashlib
import logging
import secrets
import threading
import time
import unicodedata
import weakref
from collections import OrderedDict

from sqlalchemy import REAL, Index, Text, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config
from auth import create_token, get_config, get_jwt_secret, peek_token, set_config

logger = logging.getLogger("orchestrator.auth")

SESSION_TOKEN_PREFIX = "xys_"
WS_TICKET_PREFIX = "xyt_"
REVOKE_REASONS = ("logout", "revoked", "replaced", "password-change", "revoke-others", "reset")

# A legacy JWT lived at most 24 h; one extra minute of slack for clock jitter.
_LEGACY_MAX_LIFETIME_S = 86400 + 60
# Revoked / expired rows are kept this long for the record, then purged.
_PURGE_AFTER_S = 30 * 86400
_DEVICE_LABEL_MAX = 80
_USER_AGENT_MAX = 300
_THROTTLE_MAX = 2048
_WS_TICKETS_MAX = 1000
_IDLE_CACHE_S = 5.0


class AuthBase(DeclarativeBase):
    pass


class AuthSession(AuthBase):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)          # public handle, token_hex(8)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # sha256 hex
    device_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Epoch seconds, server wall clock.
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    last_seen_at: Mapped[float] = mapped_column(REAL, nullable=False)
    last_active_at: Mapped[float] = mapped_column(REAL, nullable=False)
    expires_at: Mapped[float] = mapped_column(REAL, nullable=False)
    revoked_at: Mapped[float | None] = mapped_column(REAL, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_auth_sessions_live", "revoked_at", "expires_at"),)


class AgentHookNonce(AuthBase):
    """sha256 of the per-agent hook nonce (hook_auth.py). No row = legacy
    agent, hooks pass as before."""
    __tablename__ = "agent_hook_nonces"

    agent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    nonce_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_ensured_engines: "weakref.WeakSet" = weakref.WeakSet()
_ensure_lock = threading.Lock()


def _engine_of(bind):
    return getattr(bind, "engine", bind)


def ensure_auth_schema(bind) -> None:
    """Create the auth tables (checkfirst) and stamp auth_sessions_epoch once
    per engine. Repeat calls on an ensured engine are free."""
    engine = _engine_of(bind)
    if engine in _ensured_engines:
        return
    with _ensure_lock:
        if engine in _ensured_engines:
            return
        from models import SystemConfig

        for attempt in range(2):
            try:
                # system_config normally exists (init_db); a bare engine
                # (fresh CLI target, tests) gets the same DDL init_db uses.
                SystemConfig.__table__.create(bind=engine, checkfirst=True)
                AuthBase.metadata.create_all(bind=engine, checkfirst=True)
                break
            except OperationalError as e:
                # Another process created a table between check and CREATE.
                if attempt or "already exists" not in str(e):
                    raise
        with engine.begin() as conn:
            conn.execute(
                SystemConfig.__table__.insert().prefix_with("OR IGNORE"),
                {"key": "auth_sessions_epoch", "value": repr(time.time())},
            )
        _ensured_engines.add(engine)


def _ready(db):
    """Lazily ensure the schema on db's engine; returns db."""
    ensure_auth_schema(db.get_bind())
    return db


# ---------------------------------------------------------------------------
# Tokens, labels
# ---------------------------------------------------------------------------

def new_session_token() -> str:
    """Opaque session token: "xys_" + 43 chars of base64url (256 bit)."""
    return SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_device_label(label) -> str | None:
    """Client-sent label: control characters dropped, ≤80 chars, or None."""
    if not isinstance(label, str):
        return None
    text = "".join(c for c in label if not unicodedata.category(c).startswith("C")).strip()
    return text[:_DEVICE_LABEL_MAX] or None


def derive_device_label(ua: str | None) -> str:
    """"<OS> · <browser>" from a User-Agent, e.g. "iPhone · Safari"."""
    ua = ua or ""
    if "iPhone" in ua or "iPod" in ua:
        os_name = "iPhone"
    elif "iPad" in ua:
        os_name = "iPad"
    elif "Android" in ua:
        os_name = "Android"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        os_name = "Mac"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Linux" in ua or "X11" in ua:
        os_name = "Linux"
    else:
        os_name = ""
    if "Edg/" in ua or "EdgiOS" in ua or "EdgA/" in ua:
        browser = "Edge"
    elif "Firefox/" in ua or "FxiOS" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua or "Chromium/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua or ("AppleWebKit" in ua and os_name in ("iPhone", "iPad")):
        browser = "Safari"
    else:
        browser = ""
    label = " · ".join(p for p in (os_name, browser) if p)
    return label or "Unknown device"


# ---------------------------------------------------------------------------
# In-memory throttle state (single uvicorn process)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
# session id -> latest activity seen in this process (persisted or not)
_pending_active: "OrderedDict[str, float]" = OrderedDict()
# session id -> when last_active_at / last_seen_at was last written
_last_active_write: "OrderedDict[str, float]" = OrderedDict()
_last_seen_write: "OrderedDict[str, float]" = OrderedDict()


def _lru_set(d: OrderedDict, key: str, value: float) -> None:
    d[key] = value
    d.move_to_end(key)
    while len(d) > _THROTTLE_MAX:
        d.popitem(last=False)


def _forget_session(session_id: str) -> None:
    with _state_lock:
        for d in (_pending_active, _last_active_write, _last_seen_write):
            d.pop(session_id, None)


def _effective_last_active(row) -> float:
    with _state_lock:
        pending = _pending_active.get(row.id)
    return max(row.last_active_at, pending) if pending is not None else row.last_active_at


def _record_use(db, row, *, activity: bool, ip: str | None, now: float) -> None:
    """Note activity / last-seen for an authenticated session, writing the
    row at most every ACTIVITY_WRITE_S / SEEN_WRITE_S. Best effort: a failed
    write is logged and retried on a later request (the in-memory value
    still drives the idle decision)."""
    values = {}
    with _state_lock:
        if activity:
            prev = _pending_active.get(row.id)
            _lru_set(_pending_active, row.id, now if prev is None else max(prev, now))
            last = _last_active_write.get(row.id)
            if last is None or now - last >= config.ACTIVITY_WRITE_S:
                values["last_active_at"] = max(now, row.last_active_at)
        last = _last_seen_write.get(row.id)
        if last is None or now - last >= config.SEEN_WRITE_S:
            values["last_seen_at"] = now
            if ip:
                values["last_ip"] = ip
    if not values:
        return
    try:
        db.execute(update(AuthSession).where(AuthSession.id == row.id).values(**values))
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        logger.warning("AUTH_SESSION_WRITE_FAILED session=%s: %s", row.id, type(e).__name__)
        return
    with _state_lock:
        if "last_active_at" in values:
            _lru_set(_last_active_write, row.id, now)
        if "last_seen_at" in values:
            _lru_set(_last_seen_write, row.id, now)


# ---------------------------------------------------------------------------
# Settings: idle timeout, not-before, epoch, limiter reset
# ---------------------------------------------------------------------------

# engine -> (value, monotonic fetch time)
_idle_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _parse_idle(raw) -> int:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return max(0, config.AUTH_TIMEOUT_MINUTES)
    return max(0, v)


def get_idle_timeout(db) -> int:
    """auth_idle_timeout_minutes (0 = off), cached 5 s per engine."""
    engine = _engine_of(db.get_bind())
    cached = _idle_cache.get(engine)
    mono = time.monotonic()
    if cached is not None and mono - cached[1] < _IDLE_CACHE_S:
        return cached[0]
    raw = get_config(db, "auth_idle_timeout_minutes")
    value = _parse_idle(raw) if raw is not None else max(0, config.AUTH_TIMEOUT_MINUTES)
    _idle_cache[engine] = (value, mono)
    return value


def set_idle_timeout(db, minutes) -> int:
    """Validate against AUTH_IDLE_CHOICES, store, refresh the cache at once."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes not in config.AUTH_IDLE_CHOICES:
        raise ValueError(
            f"idle_timeout_minutes must be one of {list(config.AUTH_IDLE_CHOICES)}")
    set_config(db, "auth_idle_timeout_minutes", minutes)
    _idle_cache[_engine_of(db.get_bind())] = (minutes, time.monotonic())
    return minutes


def _float_config(db, key: str, default: float = 0.0) -> float:
    raw = get_config(db, key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def get_not_before(db) -> float:
    """auth_jwt_not_before: JWTs (legacy, legacy-born media, preview) with an
    earlier iat are rejected."""
    return _float_config(db, "auth_jwt_not_before", 0.0)


def bump_not_before(db, now: float | None = None) -> float:
    now = time.time() if now is None else now
    set_config(db, "auth_jwt_not_before", repr(now))
    return now


def get_sessions_epoch(db) -> float:
    """First boot of the server-session code; anchors the legacy JWT window.
    Unreadable = 0, which accepts no legacy JWT."""
    _ready(db)
    return _float_config(db, "auth_sessions_epoch", 0.0)


def get_limiter_reset_at(db) -> float:
    """auth_limiter_reset_at: limiter entries first failing earlier are dropped."""
    return _float_config(db, "auth_limiter_reset_at", 0.0)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class AuthResult:
    ok: bool
    reason: str | None = None          # missing|invalid|expired|revoked|idle
    kind: str | None = None            # "session" | "legacy" | "media"
    session_id: str | None = None
    token_hash: str | None = None
    legacy_exp: float | None = None
    # Extras for /api/auth/check and WS contexts.
    legacy_iat: float | None = None
    expires_at: float | None = None
    created_at: float | None = None
    device_label: str | None = None
    last_active_at: float | None = None


def _fail(reason: str) -> AuthResult:
    return AuthResult(ok=False, reason=reason)


def purge_stale(db, now: float | None = None) -> int:
    """Delete rows revoked or expired more than 30 days ago."""
    now = time.time() if now is None else now
    cutoff = now - _PURGE_AFTER_S
    res = db.execute(sa_delete(AuthSession).where(
        (AuthSession.revoked_at < cutoff) | (AuthSession.expires_at < cutoff)))
    return res.rowcount or 0


def create_session(db, *, user_agent: str | None = None, device_label=None,
                   ip: str | None = None, now: float | None = None):
    """Issue a new session. Returns (token, row); only the hash is stored."""
    _ready(db)
    now = time.time() if now is None else now
    token = new_session_token()
    ua = (user_agent or "")[:_USER_AGENT_MAX] or None
    row = AuthSession(
        id=secrets.token_hex(8),
        token_hash=hash_token(token),
        device_label=clean_device_label(device_label) or derive_device_label(ua),
        user_agent=ua,
        created_ip=ip,
        last_ip=ip,
        created_at=now,
        last_seen_at=now,
        last_active_at=now,
        expires_at=now + config.AUTH_SESSION_MAX_DAYS * 86400,
    )
    try:
        purge_stale(db, now)
    except SQLAlchemyError as e:
        db.rollback()
        logger.warning("auth session purge failed: %s", type(e).__name__)
    db.add(row)
    db.commit()
    with _state_lock:
        _lru_set(_last_active_write, row.id, now)
        _lru_set(_last_seen_write, row.id, now)
    return token, row


def _get_row(db, session_id: str):
    return db.execute(
        select(AuthSession).where(AuthSession.id == session_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _row_state(db, row, now: float) -> str | None:
    """revoked / expired / idle / None for an existing row."""
    if row.revoked_at is not None:
        return "revoked"
    if now >= row.expires_at:
        return "expired"
    timeout = get_idle_timeout(db)
    last_active = _effective_last_active(row)
    # now < last_active (wall clock stepped back) never counts as idle.
    if timeout > 0 and now >= last_active and now - last_active > timeout * 60:
        return "idle"
    return None


def _session_result(row, token_hash: str | None = None) -> AuthResult:
    return AuthResult(
        ok=True, kind="session", session_id=row.id, token_hash=token_hash,
        expires_at=row.expires_at, created_at=row.created_at,
        device_label=row.device_label, last_active_at=_effective_last_active(row),
    )


def authenticate(db, token: str | None, *, activity: bool = False,
                 ip: str | None = None, now: float | None = None) -> AuthResult:
    """Check a Bearer credential. Opaque sessions get the full state check
    (and an activity/last-seen note on success); legacy JWTs only inside the
    sunset window. Raises SQLAlchemyError on a store failure (caller: 503)."""
    now = time.time() if now is None else now
    if not token:
        return _fail("missing")
    if token.startswith(SESSION_TOKEN_PREFIX):
        _ready(db)
        th = hash_token(token)
        row = db.execute(
            select(AuthSession).where(AuthSession.token_hash == th)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None:
            return _fail("invalid")
        reason = _row_state(db, row, now)
        if reason:
            return _fail(reason)
        _record_use(db, row, activity=activity, ip=ip, now=now)
        return _session_result(row, th)
    if "." in token:
        return _authenticate_legacy(db, token, now)
    return _fail("invalid")


def _authenticate_legacy(db, token: str, now: float) -> AuthResult:
    p = peek_token(token, get_jwt_secret(db))
    if p is None:
        return _fail("invalid")
    if p.get("scope"):
        # media / preview capabilities are never session credentials
        return _fail("invalid")
    try:
        exp = float(p.get("exp", 0))
        iat = float(p.get("iat", 0))
    except (TypeError, ValueError):
        return _fail("invalid")
    if exp < now:
        return _fail("expired")
    if iat < get_not_before(db):
        return _fail("revoked")
    epoch = get_sessions_epoch(db)
    if iat >= epoch or exp > epoch + _LEGACY_MAX_LIFETIME_S:
        return _fail("invalid")
    return AuthResult(ok=True, kind="legacy", legacy_exp=exp, legacy_iat=iat, expires_at=exp)


def session_state(db, session_id: str | None, now: float | None = None) -> str | None:
    """Fresh state of one session: None when usable, else the reason."""
    now = time.time() if now is None else now
    if not session_id:
        return "invalid"
    _ready(db)
    row = _get_row(db, session_id)
    if row is None:
        return "invalid"
    return _row_state(db, row, now)


def lookup_session_id(db, token: str | None) -> str | None:
    """Session id for an opaque token regardless of its state (or None)."""
    if not token or not token.startswith(SESSION_TOKEN_PREFIX):
        return None
    _ready(db)
    return db.execute(
        select(AuthSession.id).where(AuthSession.token_hash == hash_token(token))
    ).scalar_one_or_none()


def get_session(db, session_id: str):
    _ready(db)
    return _get_row(db, session_id)


def revoke_session(db, session_id: str, reason: str, now: float | None = None) -> bool:
    """Revoke one live (not yet revoked) session. Final: an unlock is a new
    login. Returns False when unknown or already revoked."""
    now = time.time() if now is None else now
    _ready(db)
    res = db.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason))
    db.commit()
    _forget_session(session_id)
    return bool(res.rowcount)


def revoke_token(db, token: str | None, reason: str, now: float | None = None) -> bool:
    """revoke_session for the session an opaque token belongs to."""
    sid = lookup_session_id(db, token)
    return revoke_session(db, sid, reason, now=now) if sid else False


def revoke_all(db, *, except_id: str | None = None, reason: str,
               now: float | None = None, commit: bool = True) -> int:
    """Revoke every unexpired live session (optionally sparing one)."""
    now = time.time() if now is None else now
    _ready(db)
    q = update(AuthSession).where(
        AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
    if except_id:
        q = q.where(AuthSession.id != except_id)
    res = db.execute(q.values(revoked_at=now, revoked_reason=reason))
    if commit:
        db.commit()
    with _state_lock:
        for d in (_pending_active, _last_active_write, _last_seen_write):
            for sid in [sid for sid in d if sid != except_id]:
                del d[sid]
    return res.rowcount or 0


def idle_remaining(db, target, now: float | None = None) -> int | None:
    """Seconds until the idle lock for a session (row, AuthResult or id);
    None when idle locking is off or the caller is not a session."""
    now = time.time() if now is None else now
    timeout = get_idle_timeout(db)
    if timeout <= 0:
        return None
    if isinstance(target, AuthResult):
        if target.kind != "session" or not target.session_id:
            return None
        target = target.session_id
    row = get_session(db, target) if isinstance(target, str) else target
    if row is None:
        return None
    last_active = _effective_last_active(row)
    if now < last_active:
        return timeout * 60
    return max(0, int(timeout * 60 - (now - last_active)))


def list_sessions(db, current_id: str | None = None, now: float | None = None) -> list[dict]:
    """Live (not revoked, not expired) sessions, most recently seen first."""
    now = time.time() if now is None else now
    _ready(db)
    rows = db.execute(
        select(AuthSession)
        .where(AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
        .execution_options(populate_existing=True)
    ).scalars().all()
    out = []
    for row in rows:
        out.append({
            "id": row.id,
            "device_label": row.device_label,
            "user_agent": row.user_agent,
            "created_at": row.created_at,
            "last_seen_at": row.last_seen_at,
            "last_active_at": _effective_last_active(row),
            "last_ip": row.last_ip,
            "expires_at": row.expires_at,
            "state": "idle" if _row_state(db, row, now) == "idle" else "active",
            "current": row.id == current_id,
        })
    out.sort(key=lambda r: r["last_seen_at"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Media tokens
# ---------------------------------------------------------------------------

def mint_media_token(db, result: AuthResult, now: float | None = None) -> tuple[str, float]:
    """scope=media JWT for ?token= on media GETs: 12 h, bound to the caller's
    session (sid), or for a legacy caller capped at its JWT's exp."""
    now = time.time() if now is None else now
    exp = now + config.MEDIA_TOKEN_TTL_MINUTES * 60
    sid = result.session_id if result.kind == "session" else None
    if result.kind == "legacy" and result.legacy_exp:
        exp = min(exp, result.legacy_exp)
    token = create_token(
        get_jwt_secret(db), expires_minutes=(exp - now) / 60,
        extra_claims={"scope": "media", "sid": sid}, now=now)
    return token, exp


def authenticate_media(db, token: str | None, now: float | None = None) -> AuthResult:
    """Check a ?token= media credential. A session-bound token lives exactly
    as long as its session (revoked / expired / idle all reject it); a
    legacy-born one (sid null) is cut off by auth_jwt_not_before. Never
    bumps activity or last-seen."""
    now = time.time() if now is None else now
    if not token:
        return _fail("missing")
    p = peek_token(token, get_jwt_secret(db))
    if p is None or p.get("scope") != "media":
        return _fail("invalid")
    try:
        exp = float(p.get("exp", 0))
        iat = float(p.get("iat", 0))
    except (TypeError, ValueError):
        return _fail("invalid")
    if exp < now:
        return _fail("expired")
    sid = p.get("sid")
    if sid:
        reason = session_state(db, sid, now=now)
        if reason:
            return _fail(reason)
        return AuthResult(ok=True, kind="media", session_id=sid, expires_at=exp)
    if iat < get_not_before(db):
        return _fail("revoked")
    return AuthResult(ok=True, kind="media", legacy_exp=exp, legacy_iat=iat, expires_at=exp)


# ---------------------------------------------------------------------------
# WebSocket tickets
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class WsAuthCtx:
    kind: str                          # "session" | "legacy" | "open"
    session_id: str | None = None
    legacy_exp: float | None = None
    legacy_iat: float | None = None


_ws_lock = threading.Lock()
# ticket -> (ctx, expires_at)
_ws_tickets: "OrderedDict[str, tuple[WsAuthCtx, float]]" = OrderedDict()


def issue_ws_ticket(result: AuthResult, now: float | None = None) -> tuple[str, int]:
    """One-time handshake ticket for /ws/*: returns (ticket, ttl seconds)."""
    now = time.time() if now is None else now
    ctx = WsAuthCtx(
        kind="session" if result.kind == "session" else "legacy",
        session_id=result.session_id if result.kind == "session" else None,
        legacy_exp=result.legacy_exp, legacy_iat=result.legacy_iat,
    )
    ticket = WS_TICKET_PREFIX + secrets.token_urlsafe(24)
    with _ws_lock:
        for t in [t for t, (_, exp) in _ws_tickets.items() if exp <= now]:
            del _ws_tickets[t]
        _ws_tickets[ticket] = (ctx, now + config.WS_TICKET_TTL_S)
        while len(_ws_tickets) > _WS_TICKETS_MAX:
            _ws_tickets.popitem(last=False)
    return ticket, config.WS_TICKET_TTL_S


def consume_ws_ticket(ticket: str | None, now: float | None = None) -> WsAuthCtx | None:
    """Pop a ticket (single use). None when unknown, used or expired."""
    if not ticket:
        return None
    now = time.time() if now is None else now
    with _ws_lock:
        entry = _ws_tickets.pop(ticket, None)
    if entry is None:
        return None
    ctx, exp = entry
    if now >= exp:
        return None
    return ctx


def ws_ctx_state(db, ctx: WsAuthCtx, now: float | None = None) -> str | None:
    """Re-check an open socket's credential: None when still valid."""
    now = time.time() if now is None else now
    if ctx.kind == "open":
        return None
    if ctx.kind == "session":
        return session_state(db, ctx.session_id, now=now)
    if ctx.legacy_exp is not None and ctx.legacy_exp < now:
        return "expired"
    if (ctx.legacy_iat or 0.0) < get_not_before(db):
        return "revoked"
    return None

