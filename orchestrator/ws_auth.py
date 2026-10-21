"""WebSocket auth: one-time ?ticket= handshake plus a per-socket watchdog.

HTTP middleware never sees WebSocket handshakes, so /ws/status and
/ws/terminal/{agent_id} authorize in-endpoint. A browser cannot set an
Authorization header on a WebSocket, so it first trades its session for a
30 s single-use ticket (POST /api/auth/ws-ticket) and opens
``/ws/...?ticket=xyt_...``.

Close codes (contract shared with the frontend):
  4001 + reason  missing / invalid / expired / revoked / idle. The client
                 logs out only on expired / revoked / idle; missing and
                 invalid mean "fetch a new ticket and reconnect".
  1011           auth store unavailable (the client reconnects with backoff).

The socket is accepted before a 4001 close so the browser sees the code and
reason (a close before accept surfaces as a bare 1006).

While open, a watchdog re-checks the session every WS_AUTH_RECHECK_S
seconds (revoked, expired, idle; for a legacy JWT, exp and not_before) and
closes with 4001 + reason on failure. Socket traffic never counts as
activity. DISABLE_AUTH or no password: the ticket is ignored and there is
no watchdog (today's behaviour).

Protection can be switched at runtime (Monitor > Security). Switched off: a
watchdog skips its checks until it is back on, so a signed-in device's
socket survives. Switched on: every socket accepted without a session
("open", tracked by track_open) is closed 4001 "missing" after a short delay
(schedule_close_open_sockets), so the device fetches a ticket - or, without
a session, lands on the login screen.
"""

import asyncio
import logging

from sqlalchemy.exc import SQLAlchemyError
from starlette.websockets import WebSocket

import auth_store
import config
from auth import auth_disabled, get_password_hash, normalize_ip

logger = logging.getLogger("orchestrator.auth")

WS_AUTH_CLOSE_CODE = 4001
_STORE_ERROR_CODE = 1011
_CLOSE_TIMEOUT_S = 5

# Delay between switching protection on and closing the open sockets: the
# enabling device stores its new token first, then reconnects with a ticket.
# Read when the close runs (tests shorten it).
OPEN_CLOSE_DELAY_S = 1.5

# Sockets accepted without a session -> the handler's on_fail (stop sending).
_open_sockets: dict = {}
# Keeps scheduled close tasks referenced until they finish.
_pending_closes: set = set()


def _session_local():
    # Looked up per call so tests (and the reset CLI's DB) can patch it.
    import database
    return database.SessionLocal()


def _peer(ws: WebSocket) -> str:
    return normalize_ip(ws.client.host if ws.client else None)


async def _refuse(ws: WebSocket, code: int, reason: str) -> None:
    try:
        await ws.accept()
        await ws.close(code=code, reason=reason)
    except Exception:
        # Client already gone; nothing to tell it.
        pass


async def authorize_ws(ws: WebSocket) -> auth_store.WsAuthCtx | None:
    """Authorize a WebSocket handshake from its ?ticket=.

    Returns the socket's auth context, or None after refusing it (accept +
    close 4001 / 1011). On success the socket is NOT accepted, so each
    handler keeps its own accept()."""
    if auth_disabled():
        return auth_store.WsAuthCtx(kind="open")
    path = ws.scope.get("path", "")
    db = _session_local()
    try:
        if get_password_hash(db) is None:
            return auth_store.WsAuthCtx(kind="open")
        ticket = ws.query_params.get("ticket", "")
        if not ticket:
            ctx, reason = None, "missing"
        else:
            ctx = auth_store.consume_ws_ticket(ticket)
            reason = "invalid" if ctx is None else auth_store.ws_ctx_state(db, ctx)
    except SQLAlchemyError:
        logger.exception("AUTH_STORE_ERROR: WebSocket %s", path)
        await _refuse(ws, _STORE_ERROR_CODE, "Auth store unavailable")
        return None
    finally:
        db.close()

    if reason:
        # Never log ticket material.
        logger.info("WS_AUTH_REJECT reason=%s %s ip=%s", reason, path, _peer(ws))
        await _refuse(ws, WS_AUTH_CLOSE_CODE, reason)
        return None
    return ctx


def _check(ctx: auth_store.WsAuthCtx) -> str | None:
    db = _session_local()
    try:
        return auth_store.ws_ctx_state(db, ctx)
    finally:
        db.close()


async def _watch(ws: WebSocket, ctx: auth_store.WsAuthCtx, on_fail) -> None:
    path = ws.scope.get("path", "")
    while True:
        # Read per iteration: tests and XY_WS_AUTH_RECHECK_S tune it.
        await asyncio.sleep(config.WS_AUTH_RECHECK_S)
        if auth_disabled():
            # Protection switched off at runtime: nothing to enforce (and no
            # activity is recorded, so idle would misfire). Resumes when on.
            continue
        try:
            reason = _check(ctx)
        except SQLAlchemyError:
            # A transient store error never closes a socket; retry next tick.
            logger.warning("WS_AUTH_RECHECK_FAILED %s", path, exc_info=True)
            continue
        if not reason:
            continue
        logger.info("WS_AUTH_CLOSE reason=%s %s session=%s", reason, path, ctx.session_id)
        # on_fail first, so nothing else (broadcasts, the terminal pump)
        # writes to the socket once the close frame is out.
        try:
            on_fail()
        except Exception:
            logger.warning("WS watchdog on_fail raised", exc_info=True)
        try:
            await ws.close(code=WS_AUTH_CLOSE_CODE, reason=reason)
        except Exception:
            pass
        return


def start_watchdog(ws: WebSocket, ctx: auth_store.WsAuthCtx | None, on_fail) -> asyncio.Task | None:
    """Start the periodic session re-check for an accepted socket. None for
    an open (auth off / no password) socket. The caller cancels the task
    when the socket ends."""
    if ctx is None or ctx.kind == "open":
        return None
    return asyncio.create_task(_watch(ws, ctx, on_fail))


def track_open(ws: WebSocket, ctx: auth_store.WsAuthCtx | None, on_fail) -> None:
    """Remember a socket accepted without a session (auth off / no password)
    so close_open_sockets can end it when protection is switched on. The
    handler calls untrack_open when the socket ends."""
    if ctx is not None and ctx.kind == "open":
        _open_sockets[ws] = on_fail


def untrack_open(ws: WebSocket) -> None:
    _open_sockets.pop(ws, None)


async def close_open_sockets(reason: str = "missing") -> int:
    """Close every tracked open socket with 4001 + reason (on_fail first, as
    in the watchdog). Returns how many were closed."""
    victims = list(_open_sockets.items())
    _open_sockets.clear()

    async def _close(ws, on_fail):
        try:
            on_fail()
        except Exception:
            logger.warning("WS open-socket on_fail raised", exc_info=True)
        try:
            await asyncio.wait_for(ws.close(code=WS_AUTH_CLOSE_CODE, reason=reason),
                                   timeout=_CLOSE_TIMEOUT_S)
        except Exception:
            pass  # already gone

    await asyncio.gather(*(_close(ws, f) for ws, f in victims))
    if victims:
        logger.info("WS_AUTH_CLOSE_OPEN reason=%s count=%d", reason, len(victims))
    return len(victims)


def schedule_close_open_sockets(reason: str = "missing") -> asyncio.Task:
    """After OPEN_CLOSE_DELAY_S, close the open sockets - unless protection
    was switched off again meanwhile."""
    async def _later():
        await asyncio.sleep(OPEN_CLOSE_DELAY_S)
        if auth_disabled():
            return
        await close_open_sockets(reason)

    task = asyncio.create_task(_later())
    _pending_closes.add(task)
    task.add_done_callback(_pending_closes.discard)
    return task
