"""Per-agent hook nonce: ties /api/hooks/* calls to the agent's own process.

Hook endpoints are auth-exempt (Claude Code cannot hold a session) and only
loopback-reachable (request_guard). Any local process could still post a
forged hook for a managed agent, so each launch gets a random nonce:

- Launch (route_helpers.create_tmux_claude_session): the nonce is written to
  a 0600 file in a 0700 dir, its sha256 is stored in agent_hook_nonces, and
  the pane's setup line runs ``export XY_HOOK_NONCE="$(cat f)" && rm -f f``.
  The value never appears in argv, tmux scrollback or shell history. If the
  shell has not consumed the file after 10 s, the file and the row are
  dropped (fail-open: the agent becomes legacy).
- Header: every http hook carries ``X-Xy-Hook-Nonce: $XY_HOOK_NONCE``
  (inject_header; Claude Code interpolates it because XY_HOOK_NONCE is in
  allowedEnvVars), and session-start.sh sends it from the environment.
- Check (routers/hooks._resolve_agent_for_hook): no row = legacy / unmanaged
  agent, pass. Matching header, pass. Otherwise 403 when XY_HOOK_NONCE_MODE
  is "enforce" and auth is on; else a HOOK_NONCE_MISMATCH log line (at most
  once per agent per 10 min) and pass. Mode "off" skips the check.

The nonce itself is never logged.
"""

import copy
import hashlib
import hmac
import logging
import os
import secrets
import shlex
import stat
import threading
import time
from collections import OrderedDict

from fastapi import HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import SQLAlchemyError

import auth_store
import config
from auth import auth_disabled
from auth_store import AgentHookNonce

logger = logging.getLogger("orchestrator.auth")

NONCE_ENV = "XY_HOOK_NONCE"
NONCE_HEADER = "X-Xy-Hook-Nonce"
MODES = ("enforce", "log", "off")

# Header values that mean "no nonce": Claude Code sends "" for an unset or
# non-allowed variable; the literal is what an uninterpolated header holds.
_ABSENT = ("", "$XY_HOOK_NONCE", "${XY_HOOK_NONCE}")
_CONSUME_TIMEOUT_S = 10.0
_LOG_EVERY_S = 600.0
_LOG_KEYS_MAX = 1024

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_log_lock = threading.Lock()
# agent id -> monotonic time of the last mismatch log line
_last_logged: "OrderedDict[str, float]" = OrderedDict()


def _session_local():
    # Looked up per call so tests can patch database.SessionLocal.
    import database
    return database.SessionLocal()


def _hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def mode() -> str:
    """XY_HOOK_NONCE_MODE; an unknown value falls back to log-only."""
    m = config.XY_HOOK_NONCE_MODE
    return m if m in MODES else "log"


def nonce_dir() -> str:
    """0700 dir for nonce hand-off files: $XDG_RUNTIME_DIR/xylocopa, else
    <repo>/data/run. Refuses a dir that is a symlink or not ours."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isdir(runtime):
        path = os.path.join(runtime, "xylocopa")
    else:
        path = os.path.join(_REPO_ROOT, "data", "run")
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"nonce dir {path} is not a directory owned by this user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def _upsert(db, agent_id: str, nonce_hash: str) -> None:
    auth_store._ready(db)
    row = db.get(AgentHookNonce, agent_id)
    if row is None:
        db.add(AgentHookNonce(agent_id=agent_id, nonce_hash=nonce_hash, created_at=time.time()))
    else:
        row.nonce_hash = nonce_hash
        row.created_at = time.time()
    db.commit()


def register_nonce(agent_id: str) -> tuple[str, str] | None:
    """New nonce for an agent launch: (nonce, hand-off file path), or None
    when the file or the row could not be written (fail-open: legacy)."""
    nonce = secrets.token_urlsafe(24)
    try:
        path = os.path.join(nonce_dir(), f"hook-{agent_id[:12]}-{secrets.token_hex(6)}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(nonce)
    except OSError as e:
        logger.warning("HOOK_NONCE_REGISTER_FAILED agent=%s: %s", agent_id[:8], e)
        return None
    nonce_hash = _hash(nonce)
    try:
        db = _session_local()
        try:
            _upsert(db, agent_id, nonce_hash)
        finally:
            db.close()
    except SQLAlchemyError as e:
        logger.warning("HOOK_NONCE_REGISTER_FAILED agent=%s: %s", agent_id[:8], type(e).__name__)
        _unlink(path)
        return None
    timer = threading.Timer(_CONSUME_TIMEOUT_S, _verify_consumed, args=(agent_id, path, nonce_hash))
    timer.daemon = True
    timer.start()
    return nonce, path


def shell_export(path: str) -> str:
    """Setup-line suffix that moves the nonce from its file into the pane's
    environment and deletes the file."""
    q = shlex.quote(path)
    return f' && export {NONCE_ENV}="$(cat {q})" && rm -f {q}'


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("HOOK_NONCE_UNLINK_FAILED %s: %s", path, e)
        return False


def _verify_consumed(agent_id: str, path: str, nonce_hash: str) -> None:
    """Timer callback: if the pane never read its nonce file, drop the file
    and the row (only the row this launch wrote), so the agent's hooks pass
    as legacy instead of failing."""
    if not _unlink(path):
        return
    try:
        db = _session_local()
        try:
            auth_store._ready(db)
            db.execute(sa_delete(AgentHookNonce).where(
                AgentHookNonce.agent_id == agent_id, AgentHookNonce.nonce_hash == nonce_hash))
            db.commit()
        finally:
            db.close()
    except SQLAlchemyError as e:
        logger.warning("HOOK_NONCE_CLEANUP_FAILED agent=%s: %s", agent_id[:8], type(e).__name__)
        return
    logger.warning("HOOK_NONCE_UNCONSUMED agent=%s: pane never read its nonce; hooks pass "
                   "without one", agent_id[:8])


def forget_nonce(agent_id: str, db) -> None:
    """Drop an agent's nonce row (no commit). For a pane xylocopa did not
    launch being bound to the agent: its hooks carry no nonce."""
    auth_store._ready(db)
    db.execute(sa_delete(AgentHookNonce).where(AgentHookNonce.agent_id == agent_id))


def inject_header(hooks: dict) -> dict:
    """Copy of a hooks config where every http hook sends the nonce header
    and allows $XY_HOOK_NONCE to be interpolated. Pure."""
    out = copy.deepcopy(hooks)
    for entries in out.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                if not isinstance(hook, dict) or hook.get("type") != "http":
                    continue
                headers = dict(hook.get("headers") or {})
                headers[NONCE_HEADER] = f"${NONCE_ENV}"
                hook["headers"] = headers
                allowed = list(hook.get("allowedEnvVars") or [])
                if NONCE_ENV not in allowed:
                    allowed.append(NONCE_ENV)
                hook["allowedEnvVars"] = allowed
    return out


def _should_log(agent_id: str) -> bool:
    now = time.monotonic()
    with _log_lock:
        last = _last_logged.get(agent_id)
        if last is not None and now - last < _LOG_EVERY_S:
            return False
        _last_logged[agent_id] = now
        _last_logged.move_to_end(agent_id)
        while len(_last_logged) > _LOG_KEYS_MAX:
            _last_logged.popitem(last=False)
    return True


def check(request, agent_id: str, db=None) -> None:
    """Verify a resolved hook's nonce (§1.7). Raises HTTPException(403) on a
    mismatch only in enforce mode with auth on; otherwise logs and passes."""
    m = mode()
    if m == "off":
        return
    supplied = (request.headers.get(NONCE_HEADER) or "").strip()
    if supplied in _ABSENT:
        supplied = ""
    own = db is None
    try:
        if own:
            db = _session_local()
        auth_store._ready(db)
        row = db.get(AgentHookNonce, agent_id)
        expected = row.nonce_hash if row is not None else None
    except SQLAlchemyError as e:
        # Never fail a hook because the nonce table is unavailable.
        logger.warning("HOOK_NONCE_LOOKUP_FAILED agent=%s: %s", agent_id[:8], type(e).__name__)
        return
    finally:
        if own and db is not None:
            db.close()
    if expected is None:
        return  # legacy / unmanaged agent
    if supplied and hmac.compare_digest(_hash(supplied), expected):
        return
    kind = "wrong" if supplied else "missing"
    hook = request.url.path.rsplit("/", 1)[-1]
    if m == "enforce" and not auth_disabled():
        if _should_log(agent_id):
            logger.warning("HOOK_NONCE_REJECT agent=%s kind=%s hook=%s", agent_id[:8], kind, hook)
        raise HTTPException(status_code=403, detail="Hook nonce mismatch")
    if _should_log(agent_id):
        logger.warning("HOOK_NONCE_MISMATCH agent=%s kind=%s hook=%s", agent_id[:8], kind, hook)
