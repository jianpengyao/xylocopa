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
