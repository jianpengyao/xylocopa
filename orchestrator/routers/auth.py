"""Auth routes — login, password setup/change, sessions, auth check, settings,
and the password-protection switch (GET/POST /api/auth/protection).

Sessions are server-side rows (auth_store.py). Exempt from the auth
middleware: check, login, set-password, logout. Everything else here needs a
session; the middleware leaves the caller's credential on
request.state.auth (None while DISABLE_AUTH is set or no password exists).
Every response is Cache-Control: no-store.
"""

import asyncio
import logging
import os
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import auth_store
import ws_auth
from auth import (
    LoopLocal,
    auth_disabled,
    change_password_limiter,
    env_file_disable_auth,
    get_password_hash,
    global_failures,
    hash_password_async,
    is_loopback,
    login_limiter,
    needs_rehash,
    normalize_ip,
    store_password_hash,
    verify_password_async,
    write_disable_auth,
)
from config import AUTH_IDLE_CHOICES, AUTH_SESSION_MAX_DAYS, AUTH_TIMEOUT_MINUTES
from database import get_db
from models import Project

logger = logging.getLogger("orchestrator")

# First-run setup race: held only around the hash-exists check and the store
# (never across request.json() or bcrypt).
_setup_lock = LoopLocal(asyncio.Lock)

MIN_PASSWORD_LENGTH = 8

router = APIRouter(prefix="/api/auth", tags=["auth"])
# /api/settings/auth — outside the /api/auth prefix, always authenticated.
settings_router = APIRouter(tags=["auth"])


def _nostore(content, status_code: int = 200, headers: dict | None = None) -> JSONResponse:
    h = {"Cache-Control": "no-store"}
    if headers:
        h.update(headers)
    return JSONResponse(content, status_code=status_code, headers=h)


def _error(status_code: int, detail: str, headers: dict | None = None) -> JSONResponse:
    return _nostore({"detail": detail}, status_code=status_code, headers=headers)


def _too_many(remaining: int) -> JSONResponse:
    return _error(429, f"Too many failed attempts. Try again in {remaining}s.",
                  headers={"Retry-After": str(remaining)})


async def _json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _str_field(body: dict, key: str) -> str:
    value = body.get(key, "")
    return value if isinstance(value, str) else ""


def _client_ip(request: Request) -> str:
    return normalize_ip(request.client.host if request.client else None)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip() or None
    return None


def _caller(request: Request):
    """The credential the auth middleware resolved, or None."""
    return getattr(request.state, "auth", None)


def _current_session_id(request: Request, db: Session) -> str | None:
    ctx = _caller(request)
    if ctx is not None:
        return ctx.session_id if ctx.kind == "session" else None
    # DISABLE_AUTH / no password: the middleware resolved nothing, but a
    # client that still holds a session token can be told which row is its own.
    return auth_store.lookup_session_id(db, _bearer(request))


def _login_payload(db: Session, token: str, row) -> dict:
    """LoginResponse — shared by login, set-password and change-password."""
    timeout = auth_store.get_idle_timeout(db)
    media, media_exp = auth_store.mint_media_token(
        db, auth_store.AuthResult(ok=True, kind="session", session_id=row.id))
    return {
        "token": token,
        "session_id": row.id,
        "expires_at": row.expires_at,
        "idle_timeout_minutes": timeout,
        "expires_minutes": timeout,  # compat echo of idle_timeout_minutes
        "media_token": media,
        "media_expires_at": media_exp,
    }


def _new_session(db: Session, request: Request, device_label=None):
    return auth_store.create_session(
        db,
        user_agent=request.headers.get("user-agent"),
        device_label=device_label,
        ip=_client_ip(request),
    )


def _seed_default_project(db: Session, request: Request) -> None:
    """First-install onboarding: create a starter 'random-things' project so
    new users can dispatch right away without needing to set up a project first.
    Idempotent — skips if a project with that name already exists (active or archived)."""
    from config import PROJECTS_DIR
    name = "random-things"
    if db.get(Project, name):
        return
    projects_dir = PROJECTS_DIR or "/projects"
    proj = Project(
        name=name,
        display_name=name,
        path=os.path.join(projects_dir, name),
        description="Default workspace — rename or move tasks anytime.",
    )
    db.add(proj)
    db.commit()
    wm = getattr(request.app.state, "worker_manager", None)
    if wm:
        try:
            wm.ensure_project_dir(name)
        except Exception as e:
            logger.warning("seed default project: ensure_project_dir failed: %s", e)
    logger.info("Seeded default project '%s' on first-install", name)


def _session_info(result) -> dict:
    if result.kind == "legacy":
        return {"id": None, "legacy": True, "device_label": None,
                "created_at": None, "expires_at": result.legacy_exp}
    return {"id": result.session_id, "legacy": False, "device_label": result.device_label,
            "created_at": result.created_at, "expires_at": result.expires_at}


@router.post("/check")
async def auth_check(request: Request, db: Session = Depends(get_db)):
    """Report the auth state. Exempt and never 401: the frontend decides from
    `authenticated` + `reason`. Bumps activity only with X-User-Activity: 1."""
    out = {
        "authenticated": False, "needs_setup": False, "auth_disabled": False,
        "reason": None, "session": None, "idle_timeout_minutes": AUTH_TIMEOUT_MINUTES,
        "idle_remaining_s": None, "media_token": None, "media_expires_at": None,
        "server_time": time.time(),
    }
    # auth_disabled lets the frontend skip its own password machinery
    # (idle lock, login redirects) when the server does no password check.
    # Never 503 here: with DISABLE_AUTH the answer doesn't depend on the DB.
    if auth_disabled():
        try:
            out["idle_timeout_minutes"] = auth_store.get_idle_timeout(db)
        except SQLAlchemyError:
            db.rollback()
        out.update(authenticated=True, auth_disabled=True)
        return _nostore(out)
    try:
        out["idle_timeout_minutes"] = auth_store.get_idle_timeout(db)
        if get_password_hash(db) is None:
            out["needs_setup"] = True
            return _nostore(out)
        token = _bearer(request)
        if not token:
            out["reason"] = "missing"
            return _nostore(out)
        result = auth_store.authenticate(
            db, token, activity=request.headers.get("x-user-activity") == "1",
            ip=_client_ip(request))
        if not result.ok:
            out["reason"] = result.reason
            return _nostore(out)
        media, media_exp = auth_store.mint_media_token(db, result)
        out.update(
            authenticated=True,
            session=_session_info(result),
            idle_remaining_s=auth_store.idle_remaining(db, result),
            media_token=media,
            media_expires_at=media_exp,
        )
        return _nostore(out)
    except SQLAlchemyError:
        logger.exception("AUTH_STORE_ERROR: /api/auth/check")
        return _error(503, "Auth store unavailable")


@router.post("/set-password")
async def auth_set_password(request: Request, db: Session = Depends(get_db)):
    """First-time password setup. Only works if no password has been set yet."""
    if get_password_hash(db) is not None:
        return _error(400, "Password already set")
    body = await _json_body(request)
    if body is None:
        return _error(400, "Invalid JSON body")
    password = _str_field(body, "password")
    if len(password) < MIN_PASSWORD_LENGTH:
        return _error(400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    pw_hash = await hash_password_async(password)
    async with _setup_lock():
        if get_password_hash(db) is not None:
            return _error(400, "Password already set")
        store_password_hash(db, pw_hash)
    try:
        _seed_default_project(db, request)
    except Exception as e:
        logger.warning("seed default project failed: %s", e)
    token, row = _new_session(db, request, body.get("device_label"))
    logger.info("Initial password set (session=%s)", row.id)
    return _nostore(_login_payload(db, token, row))


@router.post("/login")
async def auth_login(request: Request, db: Session = Depends(get_db)):
    """Login with password. Returns a new session token.

    Lockouts apply only to the failing client's IP (never loopback), so a
    guesser elsewhere can't lock the owner out; a locked IP gets 429 before
    bcrypt runs. A Bearer of the previous (e.g. idle-locked) token is revoked
    as "replaced" on success."""
    ip = _client_ip(request)
    limited = not is_loopback(ip)
    reset_at = auth_store.get_limiter_reset_at(db)

    if limited:
        locked, remaining = login_limiter.check(ip, reset_at=reset_at)
        if locked:
            return _too_many(remaining)

    pw_hash = get_password_hash(db)
    if pw_hash is None:
        return _error(400, "No password set — use /api/auth/set-password")

    body = await _json_body(request)
    if body is None:
        return _error(400, "Invalid JSON body")
    password = _str_field(body, "password")

    # One attempt per IP at a time, checked again inside: a concurrent burst
    # must see the failures recorded before it (see LoginRateLimiter).
    async with login_limiter.one_at_a_time(ip if limited else None):
        if limited:
            locked, remaining = login_limiter.check(ip, reset_at=reset_at)
            if locked:
                return _too_many(remaining)
        await global_failures.throttle()
        if not await verify_password_async(password, pw_hash):
            global_failures.record()
            detail = "Wrong password"
            if limited:
                now_locked, lock_secs = login_limiter.record_failure(ip, reset_at=reset_at)
                if now_locked:
                    detail += f". Locked out for {lock_secs}s."
                    logger.warning("Login locked for %s after repeated failures (%ds)", ip, lock_secs)
            logger.info("LOGIN_FAIL ip=%s", ip)
            return _error(401, detail)

        if limited:
            login_limiter.record_success(ip)

    # Transparent rehash: migrate legacy SHA-256 to bcrypt on successful login
    if needs_rehash(pw_hash):
        store_password_hash(db, await hash_password_async(password))
        logger.info("Migrated password hash from SHA-256 to bcrypt")

    previous = _bearer(request)
    token, row = _new_session(db, request, body.get("device_label"))
    if previous and auth_store.revoke_token(db, previous, "replaced"):
        logger.info("LOGIN_REPLACED ip=%s session=%s", ip, row.id)
    logger.info("LOGIN_OK ip=%s session=%s", ip, row.id)
    return _nostore(_login_payload(db, token, row))


@router.post("/logout")
async def auth_logout(request: Request, db: Session = Depends(get_db)):
    """Revoke the caller's session. Exempt and idempotent: works on an
    idle-locked or already revoked session (revoked=false then)."""
    revoked = auth_store.revoke_token(db, _bearer(request), "logout")
    return _nostore({"ok": True, "revoked": revoked})


@router.post("/change-password")
async def auth_change_password(request: Request, db: Session = Depends(get_db)):
    """Change password (session required while auth is on). A wrong current
    password is 403 — not a session failure. On success every earlier session
    (the caller's included) is revoked and a fresh one is issued."""
    ctx = _caller(request)
    ip = _client_ip(request)
    if ctx is not None and ctx.kind == "session":
        key, limited = "s:" + ctx.session_id, True
    else:
        key, limited = ip, not is_loopback(ip)
    reset_at = auth_store.get_limiter_reset_at(db)

    # Rate limit — separate bucket from login
    if limited:
        locked, remaining = change_password_limiter.check(key, reset_at=reset_at)
        if locked:
            return _too_many(remaining)

    pw_hash = get_password_hash(db)
    if pw_hash is None:
        return _error(400, "No password set")

    body = await _json_body(request)
    if body is None:
        return _error(400, "Invalid JSON body")
    current = _str_field(body, "current_password")
    new_pw = _str_field(body, "new_password")

    async with change_password_limiter.one_at_a_time(key if limited else None):
        if limited:
            locked, remaining = change_password_limiter.check(key, reset_at=reset_at)
            if locked:
                return _too_many(remaining)
        await global_failures.throttle()
        if not await verify_password_async(current, pw_hash):
            global_failures.record()
            detail = "Current password is wrong"
            if limited:
                now_locked, lock_secs = change_password_limiter.record_failure(key, reset_at=reset_at)
                if now_locked:
                    detail += f". Locked out for {lock_secs}s."
                    logger.warning("Change-password locked for %s after repeated failures (%ds)",
                                   key if key.startswith("s:") else ip, lock_secs)
            return _error(403, detail)

    if len(new_pw) < MIN_PASSWORD_LENGTH:
        return _error(400, f"New password must be at least {MIN_PASSWORD_LENGTH} characters")

    if limited:
        change_password_limiter.record_success(key)
    new_hash = await hash_password_async(new_pw)
    store_password_hash(db, new_hash)
    revoked = auth_store.revoke_all(db, reason="password-change")
    auth_store.bump_not_before(db)

    label = body.get("device_label")
    if not label and ctx is not None and ctx.kind == "session":
        label = ctx.device_label
    token, row = _new_session(db, request, label)
    logger.info("Password changed (revoked %d sessions, new session=%s)", revoked, row.id)
    return _nostore({**_login_payload(db, token, row), "revoked_sessions": revoked})


@router.get("/sessions")
async def auth_sessions(request: Request, db: Session = Depends(get_db)):
    """Signed-in devices: live (not revoked, not expired) sessions."""
    current_id = _current_session_id(request, db)
    return _nostore({
        "sessions": auth_store.list_sessions(db, current_id),
        "current_id": current_id,
        "auth_disabled": auth_disabled(),
    })


@router.post("/sessions/revoke-others")
async def auth_revoke_others(request: Request, db: Session = Depends(get_db)):
    """Sign out every other device. Also cuts off legacy JWTs (not_before),
    unless the caller itself is on one."""
    ctx = _caller(request)
    current_id = _current_session_id(request, db)
    revoked = auth_store.revoke_all(db, except_id=current_id, reason="revoke-others")
    if not (ctx is not None and ctx.kind == "legacy"):
        auth_store.bump_not_before(db)
    logger.info("SESSIONS_REVOKE_OTHERS revoked=%d keep=%s", revoked, current_id)
    return _nostore({"ok": True, "revoked": revoked})


@router.post("/sessions/{session_id}/revoke")
async def auth_revoke_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    """Revoke one session (final; that device must log in again)."""
    if not auth_store.revoke_session(db, session_id, "revoked"):
        return _error(404, "Session not found")
    logger.info("SESSION_REVOKED session=%s", session_id)
    return _nostore({"ok": True, "current": session_id == _current_session_id(request, db)})


@router.post("/activity")
async def auth_activity(request: Request, db: Session = Depends(get_db)):
    """Throttled user-activity ping. The middleware already counted it as
    activity (or answered 401 idle when the session was locked)."""
    ctx = _caller(request)
    return _nostore({
        "ok": True,
        "idle_timeout_minutes": auth_store.get_idle_timeout(db),
        "idle_remaining_s": auth_store.idle_remaining(db, ctx) if ctx is not None else None,
    })


@router.post("/ws-ticket")
async def auth_ws_ticket(request: Request):
    """One-time 30 s ticket for the ?ticket= WebSocket handshake."""
    ctx = _caller(request)
    if auth_disabled() or ctx is None:
        return _nostore({"ticket": None, "expires_in": 0})
    ticket, ttl = auth_store.issue_ws_ticket(ctx)
    return _nostore({"ticket": ticket, "expires_in": ttl})


@router.post("/media-token")
async def auth_media_token(request: Request, db: Session = Depends(get_db)):
    """Fresh scope=media token for ?token= on /api/files, /api/thumbs,
    /api/uploads GETs, bound to the caller's session."""
    ctx = _caller(request)
    if auth_disabled() or ctx is None:
        return _nostore({"token": None, "expires_at": None, "expires_in": 0})
    token, exp = auth_store.mint_media_token(db, ctx)
    return _nostore({"token": token, "expires_at": exp, "expires_in": max(0, round(exp - time.time()))})


# ---- Password-protection switch ----

def _caller_label(ctx) -> str:
    if ctx is None:
        return "-"
    return ctx.session_id if ctx.kind == "session" else ctx.kind


async def _verify_server_password(request: Request, db: Session, password: str,
                                  pw_hash: str) -> JSONResponse | None:
    """Re-check the server password for the protection switch: None when it
    matches, else 429 / 403. Same limiter as change-password (shared bucket,
    so the two don't add up to a bigger guess budget): keyed by the caller's
    session, else its IP; loopback is never limited."""
    ctx = _caller(request)
    ip = _client_ip(request)
    if ctx is not None and ctx.kind == "session":
        key, limited = "s:" + ctx.session_id, True
    else:
        key, limited = ip, not is_loopback(ip)
    reset_at = auth_store.get_limiter_reset_at(db)
    if limited:
        locked, remaining = change_password_limiter.check(key, reset_at=reset_at)
        if locked:
            return _too_many(remaining)

    async with change_password_limiter.one_at_a_time(key if limited else None):
        if limited:
            locked, remaining = change_password_limiter.check(key, reset_at=reset_at)
            if locked:
                return _too_many(remaining)
        await global_failures.throttle()
        if not await verify_password_async(password, pw_hash):
            global_failures.record()
            detail = "Password is wrong"
            if limited:
                now_locked, lock_secs = change_password_limiter.record_failure(key, reset_at=reset_at)
                if now_locked:
                    detail += f". Locked out for {lock_secs}s."
                    logger.warning("Protection switch locked for %s after repeated failures (%ds)",
                                   key if key.startswith("s:") else ip, lock_secs)
            # 403, never 401: a 401 makes the frontend log out.
            return _error(403, detail)
    if limited:
        change_password_limiter.record_success(key)
    return None


def _write_protection(enabled: bool) -> JSONResponse | None:
    """.env first, then the process env (auth.write_disable_auth). None on
    success; a 500 with the running state unchanged when .env can't be written."""
    try:
        write_disable_auth(not enabled)
    except OSError as e:
        reason = e.strerror or type(e).__name__
        logger.warning("AUTH_PROTECTION_ENV_WRITE_FAILED %s: %s", type(e).__name__, reason)
        return _error(500, f"Could not update .env: {reason}")
    return None


@router.post("/protection")
async def auth_protection(request: Request, db: Session = Depends(get_db)):
    """Switch password protection for the whole server on or off.

    Body: {"enabled": bool, "password": str, "new_password"?: str,
    "device_label"?: str}. DISABLE_AUTH in the project .env is the one source
    of truth (0 = on, 1 = off): .env is rewritten first, then the process
    env, so the switch applies at once and survives a restart.

    Not auth-exempt: while protection is on the middleware already demands a
    session; while it is off the middleware lets everything through, so the
    password check here is the gate. A wrong password is 403 (never 401).
    - on (was off): checks the password (or, with none set yet, stores
      new_password), then returns a fresh session like login plus
      "settings" and "protection" (GET /protection's state); sockets
      accepted without a session are closed 4001 shortly after, so every
      device has to present a ticket.
    - off (was on): checks the password; sessions are kept.
      {"settings", "protection"}.
    - already in that state: {"settings", "protection"}; a .env that
      disagrees (a hand edit not yet applied) is rewritten to match.
    """
    body = await _json_body(request)
    if body is None:
        return _error(400, "Invalid JSON body")
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return _error(400, "enabled must be true or false")
    password = _str_field(body, "password")
    ip = _client_ip(request)

    if enabled == (not auth_disabled()):
        return await _protection_resync(request, db, enabled, password, ip)
    if not enabled:
        return await _protection_off(request, db, password, ip)

    pw_hash = get_password_hash(db)
    if pw_hash is not None:
        denied = await _verify_server_password(request, db, password, pw_hash)
        if denied is not None:
            return denied
        failed = _write_protection(True)
        if failed is not None:
            return failed
    else:
        # No password yet: the switch sets the first one.
        new_pw = _str_field(body, "new_password")
        if len(new_pw) < MIN_PASSWORD_LENGTH:
            return _error(400, f"New password must be at least {MIN_PASSWORD_LENGTH} characters")
        new_hash = await hash_password_async(new_pw)
        async with _setup_lock():
            if get_password_hash(db) is not None:
                return _error(400, "Password already set")
            failed = _write_protection(True)
            if failed is not None:
                return failed
            try:
                store_password_hash(db, new_hash)
            except SQLAlchemyError:
                db.rollback()
                logger.exception("AUTH_STORE_ERROR: /api/auth/protection")
                # Back to off: on without a password would be wide open.
                if _write_protection(False) is not None:
                    logger.error("AUTH_PROTECTION could not switch back off after a store error")
                return _error(503, "Auth store unavailable")
        logger.info("Initial password set via the protection switch")

    previous = _bearer(request)
    token, row = _new_session(db, request, body.get("device_label"))
    if previous and auth_store.revoke_token(db, previous, "replaced"):
        logger.info("LOGIN_REPLACED ip=%s session=%s", ip, row.id)
    logger.info("AUTH_PROTECTION on ip=%s session=%s", ip, row.id)
    ws_auth.schedule_close_open_sockets()
    return _nostore({**_login_payload(db, token, row), "settings": _auth_settings(db),
                     "protection": _protection_state(db)})


async def _protection_off(request: Request, db: Session, password: str, ip: str) -> JSONResponse:
    pw_hash = get_password_hash(db)
    if pw_hash is None:
        return _error(400, "No password set")
    denied = await _verify_server_password(request, db, password, pw_hash)
    if denied is not None:
        return denied
    failed = _write_protection(False)
    if failed is not None:
        return failed
    logger.info("AUTH_PROTECTION off ip=%s session=%s", ip, _caller_label(_caller(request)))
    return _nostore({"settings": _auth_settings(db), "protection": _protection_state(db)})


async def _protection_resync(request: Request, db: Session, enabled: bool, password: str,
                             ip: str) -> JSONResponse:
    """Already in the requested state. When .env says otherwise, rewrite it
    so the next start agrees with the running server."""
    env_on = env_file_disable_auth() is not True
    if env_on != enabled:
        if not enabled:
            # Writing DISABLE_AUTH=1 turns protection off from the next
            # start: same password gate as switching it off.
            pw_hash = get_password_hash(db)
            if pw_hash is not None:
                denied = await _verify_server_password(request, db, password, pw_hash)
                if denied is not None:
                    return denied
        failed = _write_protection(enabled)
        if failed is not None:
            return failed
        logger.info("AUTH_PROTECTION %s ip=%s session=%s env_resync=1", "on" if enabled else "off",
                    ip, _caller_label(_caller(request)))
    return _nostore({"settings": _auth_settings(db), "protection": _protection_state(db)})


def _protection_state(db: Session) -> dict:
    disabled = auth_disabled()
    # What .env says now (None: no file / no DISABLE_AUTH line, which a
    # restart reads as protection on). Out of sync after a hand edit.
    env_disabled = env_file_disable_auth()
    return {
        "auth_disabled": disabled,
        "password_set": get_password_hash(db) is not None,
        "env_file_disable_auth": env_disabled,
        "env_file_in_sync": (env_disabled is True) == disabled,
    }


@router.get("/protection")
async def get_auth_protection(db: Session = Depends(get_db)):
    """The switch's state: the running value, what .env says, and whether
    they agree. Kept apart from /api/settings/auth (idle-lock settings)."""
    return _nostore(_protection_state(db))


def _auth_settings(db: Session) -> dict:
    return {
        "idle_timeout_minutes": auth_store.get_idle_timeout(db),
        "choices": list(AUTH_IDLE_CHOICES),
        "default_idle_timeout_minutes": AUTH_TIMEOUT_MINUTES,
        "session_max_days": AUTH_SESSION_MAX_DAYS,
        "auth_disabled": auth_disabled(),
        "password_set": get_password_hash(db) is not None,
    }


@settings_router.get("/api/settings/auth")
async def get_auth_settings(db: Session = Depends(get_db)):
    return _nostore(_auth_settings(db))


@settings_router.put("/api/settings/auth")
async def put_auth_settings(request: Request, db: Session = Depends(get_db)):
    """Set the server-enforced idle lock (minutes; 0 = off)."""
    body = await _json_body(request)
    if body is None:
        return _error(400, "Invalid JSON body")
    try:
        minutes = auth_store.set_idle_timeout(db, body.get("idle_timeout_minutes"))
    except ValueError as e:
        return _error(400, str(e))
    logger.info("AUTH_SETTINGS idle_timeout_minutes=%d", minutes)
    return _nostore(_auth_settings(db))
