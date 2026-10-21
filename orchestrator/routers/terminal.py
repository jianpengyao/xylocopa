"""Interactive web terminal — attach to an agent's tmux session over WebSocket.

``/ws/terminal/{agent_id}`` bridges a browser xterm.js client to a dedicated
``tmux attach-session`` client running in a server-side PTY. Closing the
socket just detaches that tmux client; the underlying session keeps running.

Protocol:
  client -> server (text/JSON):  {"type": "input",  "data": "<keys>"}
                                 {"type": "resize", "cols": N, "rows": M}
                                 {"type": "ping"}
  server -> client (binary):     raw PTY output bytes
  server -> client (text/JSON):  {"type": "exit"}   tmux client ended (detach/kill)
                                 {"type": "error", "message": "..."}
                                 {"type": "pong"}

Auth mirrors /ws/status (ws_auth.py): HTTP middleware does not cover
WebSocket handshakes, so a one-time ``?ticket=`` from POST /api/auth/ws-ticket
is checked in-endpoint (close 4001 + reason on failure), and a watchdog closes
the socket with 4001 when the session is revoked, expires or goes idle.
A socket accepted while protection was off is closed 4001 when it is
switched on (ws_auth.track_open).
"""

import asyncio
import fcntl
import json
import logging
import os
import pty
import struct
import subprocess
import termios

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect

import ws_auth

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_COLS = 500
_MAX_ROWS = 300


def _resolve_tmux_session(agent) -> str | None:
    """Find the live tmux session for an agent.

    Managed agents live in ``xy-{id[:8]}`` (legacy ``ah-``). CLI-attached
    agents only have a recorded pane — resolve its owning session. ``=``
    forces exact-match targets (no prefix matching).
    """
    for name in (f"xy-{agent.id[:8]}", f"ah-{agent.id[:8]}"):
        r = subprocess.run(
            ["tmux", "has-session", "-t", f"={name}"],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            return name
    if agent.tmux_pane:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", agent.tmux_pane, "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def _spawn_attach_client(session: str) -> tuple[subprocess.Popen, int]:
    """Spawn ``tmux attach`` on a fresh PTY; return (proc, master_fd)."""
    # Most-recently-active client drives the window size, so a small phone
    # client doesn't shrink the user's desktop tmux view. Window option on
    # the session's (single) window; best-effort. Window targets don't
    # accept the "=" exact prefix — "name:" targets the session's windows.
    subprocess.run(
        ["tmux", "set-option", "-w", "-t", f"{session}:", "window-size", "latest"],
        capture_output=True, timeout=5,
    )

    master, slave = pty.openpty()
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["TERM"] = "xterm-256color"
    try:
        proc = subprocess.Popen(
            ["tmux", "attach-session", "-t", f"={session}"],
            preexec_fn=lambda: os.login_tty(slave),  # setsid + ctty + stdio on the PTY
            pass_fds=(slave,),
            env=env,
        )
    finally:
        os.close(slave)
    return proc, master


def _write_all(fd: int, data: bytes) -> None:
    """Blocking full write — runs in executor so a huge paste can't stall the loop."""
    while data:
        n = os.write(fd, data)
        data = data[n:]


def _reap(proc: subprocess.Popen) -> None:
    """Terminate the tmux client (a detach, not a session kill) and reap it."""
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass


@router.websocket("/ws/terminal/{agent_id}")
async def terminal_ws(ws: WebSocket, agent_id: str):
    # Refuses (accept + close 4001) on its own; on success the socket is
    # still unaccepted, so the accept below stays the only one.
    ctx = await ws_auth.authorize_ws(ws)
    if ctx is None:
        return

    from database import SessionLocal
    from models import Agent

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
    finally:
        db.close()
    await ws.accept()
    if agent is None:
        await ws.send_text(json.dumps({"type": "error", "message": "Unknown agent"}))
        await ws.close(code=4404)
        return

    session = _resolve_tmux_session(agent)
    if session is None:
        await ws.send_text(json.dumps(
            {"type": "error", "message": "No live tmux session for this agent"}))
        await ws.close(code=4404)
        return

    try:
        proc, master = _spawn_attach_client(session)
    except OSError as e:
        logger.error("terminal: PTY spawn failed for %s: %s", session, e)
        await ws.send_text(json.dumps({"type": "error", "message": "PTY spawn failed"}))
        await ws.close(code=1011)
        return

    logger.info("terminal: attached ws client to %s (agent %s, pid %d)",
                session, agent_id[:8], proc.pid)

    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue[bytes] = asyncio.Queue()

    def _on_master_readable():
        try:
            data = os.read(master, 65536)
        except OSError:  # EIO — child exited, PTY torn down
            data = b""
        if data:
            out_q.put_nowait(data)
        else:
            loop.remove_reader(master)
            out_q.put_nowait(b"")  # EOF sentinel

    loop.add_reader(master, _on_master_readable)

    async def _pump_output():
        while True:
            chunk = await out_q.get()
            if not chunk:
                break
            await ws.send_bytes(chunk)
        # tmux client ended (user detached inside tmux, or session was killed)
        try:
            await ws.send_text(json.dumps({"type": "exit"}))
            await ws.close(code=1000)
        except Exception:
            pass

    pump = asyncio.create_task(_pump_output())
    # On a 4001 the pump stops first, so no output follows the close frame.
    wd = ws_auth.start_watchdog(ws, ctx, pump.cancel)
    # Accepted without a session: closed 4001 when protection is switched on.
    ws_auth.track_open(ws, ctx, pump.cancel)

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is None:
                raw = msg.get("bytes")
                if raw:
                    await loop.run_in_executor(None, _write_all, master, raw)
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            mtype = payload.get("type")
            if mtype == "input":
                data = str(payload.get("data", "")).encode("utf-8", "ignore")
                if data:
                    await loop.run_in_executor(None, _write_all, master, data)
            elif mtype == "resize":
                try:
                    cols = max(2, min(_MAX_COLS, int(payload.get("cols", 80))))
                    rows = max(2, min(_MAX_ROWS, int(payload.get("rows", 24))))
                    # TIOCSWINSZ delivers SIGWINCH to the tmux client for us
                    fcntl.ioctl(master, termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0))
                except (ValueError, OSError):
                    pass
            elif mtype == "ping":
                await ws.send_text('{"type": "pong"}')
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if wd is not None:
            wd.cancel()
        ws_auth.untrack_open(ws)
        pump.cancel()
        try:
            loop.remove_reader(master)
        except Exception:
            pass
        await loop.run_in_executor(None, _reap, proc)
        try:
            os.close(master)
        except OSError:
            pass
        logger.info("terminal: detached ws client from %s (agent %s)",
                    session, agent_id[:8])
