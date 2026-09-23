"""Shared helpers and constants used across multiple routers."""

import asyncio
import logging
import os
import subprocess as _sp
import tempfile

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

import re

from agent_dispatcher import ACTIVE_STATUSES
from models import Agent, Project, StarredSession, Task
from schemas import AgentBrief

logger = logging.getLogger("orchestrator")

# Serialize tmux agent launches so only one proceeds at a time.
tmux_launch_sem = asyncio.Semaphore(1)

# ---- Module-level constants (extracted from inline magic numbers) ----

# tmux command timeout (seconds) — used for send-keys, kill-pane, etc.
TMUX_CMD_TIMEOUT = 5

# Maximum seconds to wait for Claude TUI to start / initialize
TUI_STARTUP_TIMEOUT = 30

# Seconds to settle after TUI REPL mount before sending prompt.
# The status bar detection in _launch_tmux_background is the
# "REPL fully mounted" signal; the React input handler wires up
# in the next 1-2 frames after status-bar render, so this buffer
# only needs to cover that gap.  Larger values just pad first-prompt
# latency.  Was 3.0 → 0.5 (commit 03285cb) → 0.3 here.  If prompts
# start dropping characters or Enter doesn't fire onSubmit, raise
# back to 0.5.
TUI_SETTLE_DELAY = 0.3

# Max file size for project browser (bytes)
BROWSE_MAX_FILE_SIZE = 512 * 1024  # 512 KB

# Max concurrent agent launches allowed in STARTING state
MAX_STARTING_AGENTS = 10

# Pre-flight import check timeout (seconds)
IMPORT_CHECK_TIMEOUT = 15

# Anthropic API request timeout (seconds)
API_REQUEST_TIMEOUT = 10

# Env vars stripped from claude -p subprocesses to prevent false session
# rotation signals. Includes both the new XY_AGENT_ID and the legacy
# AHIVE_AGENT_ID (kept for back-compat with in-flight panes).
SUBPROCESS_STRIP_VARS = {"XY_AGENT_ID", "AHIVE_AGENT_ID", "TMUX", "TMUX_PANE"}


def subprocess_clean_env() -> dict[str, str]:
    """Return os.environ without vars that can trigger false session rotation."""
    return {k: v for k, v in os.environ.items() if k not in SUBPROCESS_STRIP_VARS}


# SessionStart signal files written by the SessionStart hook and consumed
# by the dispatcher.  New writes use the xy- prefix; reads check both
# prefixes so in-flight signals from pre-rename agents survive an upgrade.
_SESSION_SIGNAL_PREFIX = "xy-"
_SESSION_SIGNAL_LEGACY_PREFIX = "ahive-"


def session_signal_path(agent_id: str) -> str:
    """Path for *writing* a SessionStart signal file (always new prefix)."""
    return os.path.join(tempfile.gettempdir(), f"{_SESSION_SIGNAL_PREFIX}{agent_id}.newsession")


def find_session_signal(agent_id: str) -> str | None:
    """Return existing signal file path (new prefix preferred), or None."""
    new = os.path.join(tempfile.gettempdir(), f"{_SESSION_SIGNAL_PREFIX}{agent_id}.newsession")
    if os.path.exists(new):
        return new
    legacy = os.path.join(tempfile.gettempdir(), f"{_SESSION_SIGNAL_LEGACY_PREFIX}{agent_id}.newsession")
    if os.path.exists(legacy):
        return legacy
    return None


def unlink_session_signals(agent_id: str) -> None:
    """Remove signal files at both new and legacy paths (no-op if missing)."""
    for prefix in (_SESSION_SIGNAL_PREFIX, _SESSION_SIGNAL_LEGACY_PREFIX):
        try:
            os.unlink(os.path.join(tempfile.gettempdir(), f"{prefix}{agent_id}.newsession"))
        except FileNotFoundError:
            pass


# Pending-session entries written by the SessionStart hook for unmanaged
# CLI sessions awaiting user adoption.  New writes use xy-pending-sessions;
# reads scan both directories.
PENDING_SESSIONS_DIR = "/tmp/xy-pending-sessions"
PENDING_SESSIONS_LEGACY_DIR = "/tmp/ahive-pending-sessions"


def pending_sessions_dirs() -> list[str]:
    """Return both possible pending-sessions dirs (new first, legacy second)."""
    return [PENDING_SESSIONS_DIR, PENDING_SESSIONS_LEGACY_DIR]


def check_project_capacity(db, project_name: str) -> tuple[int, int]:
    """Return (active_count, max_concurrent) for a project.

    Raises HTTPException 429 if at capacity.
    """
    proj = db.get(Project, project_name)
    if not proj:
        return (0, 8)
    active = (
        db.query(func.count(Agent.id))
        .filter(Agent.project == project_name, Agent.status.in_(ACTIVE_STATUSES))
        .scalar() or 0
    )
    # max_concurrent enforcement removed — all agents launch immediately
    return (active, proj.max_concurrent)


def get_agent_or_404(db: Session, agent_id: str) -> Agent:
    """Return the Agent row or raise HTTP 404."""
    agent = db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def get_project_or_404(db: Session, name: str) -> Project:
    """Return the Project row or raise HTTP 404."""
    proj = db.get(Project, name)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    return proj


def get_task_or_404(db: Session, task_id: str) -> Task:
    """Return the Task row or raise HTTP 404."""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def resolve_project_path(name: str, db) -> str:
    """Return the project's absolute path. Checks DB first, then PROJECTS_DIR."""
    proj = db.get(Project, name)
    if proj:
        return proj.path
    # Fallback: project exists on disk but not registered in DB
    from config import PROJECTS_DIR
    projects_dir = PROJECTS_DIR or "/projects"
    candidate = os.path.join(projects_dir, name)
    if os.path.isdir(candidate):
        return candidate
    raise HTTPException(status_code=404, detail=f"Project '{name}' not found")


def compute_successor_id(agent_id: str, db: Session) -> str | None:
    """Return the ID of the most recent successor (non-subagent) agent, if any."""
    successor = db.query(Agent).filter(
        Agent.parent_id == agent_id,
        Agent.is_subagent == False,
    ).order_by(Agent.created_at.desc()).first()
    return successor.id if successor else None


def create_tmux_claude_session(
    session_name: str, project_path: str, claude_cmd: str,
    agent_id: str | None = None,
) -> str:
    """Create a tmux session running Claude. Returns pane_id."""
    # Kill any stale session with same name
    _sp.run(["tmux", "kill-session", "-t", session_name],
            capture_output=True, timeout=TMUX_CMD_TIMEOUT)
    # Create new detached session.
    _sp.run(["tmux", "new-session", "-d", "-s", session_name, "-c", project_path],
            check=True, capture_output=True, timeout=TMUX_CMD_TIMEOUT)
    # Get pane ID. If display-message fails or returns empty (e.g. a
    # concurrent kill-session wiped the new session between create and
    # query), raise a clean error so callers don't blindly run
    # `send-keys -t ''` and surface a misleading subprocess 500.
    pane_result = _sp.run(["tmux", "display-message", "-p", "-t", session_name, "#{pane_id}"],
                          capture_output=True, text=True, timeout=TMUX_CMD_TIMEOUT)
    pane_id = pane_result.stdout.strip()
    if pane_result.returncode != 0 or not pane_id:
        raise HTTPException(
            status_code=500,
            detail=f"tmux session {session_name} disappeared after create "
                   f"(rc={pane_result.returncode}, stderr={pane_result.stderr.strip()!r})",
        )
    # Unset problematic env vars, export XY_AGENT_ID (and legacy
    # AHIVE_AGENT_ID alias) for hooks, and disable prompt suggestions so
    # tmux send-keys Enter always reaches onSubmit (avoids autocomplete
    # intercepting Enter).
    env_setup = "unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT XYLOCOPA_MANAGED AGENTHIVE_MANAGED CLAUDE_CODE_OAUTH_TOKEN"
    env_setup += " && export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false"
    # Suppress CC's "Resume from summary?" interactive menu on resume of old/large
    # sessions. The menu's default ("1. Resume from summary") triggers /compact, and
    # any tmux send-keys Enter from xylocopa's dispatch path lands on it instead of
    # the input box — silently losing the queued message and rewriting the session.
    # AND-gated: breaking either threshold suppresses the menu; we set both for
    # defense in depth against env name churn.
    env_setup += " && export CLAUDE_CODE_RESUME_TOKEN_THRESHOLD=999999999"
    env_setup += " && export CLAUDE_CODE_RESUME_THRESHOLD_MINUTES=999999"
    # Back the bare "opus"/"sonnet" aliases that _model_for_cli emits for the
    # models that still need one (--model drops a [1m] suffix, but the suffix
    # survives when it arrives through these env vars).
    # Opus 4.6 is a 200K model that opts into 1M via the suffix, so it keeps it.
    # Sonnet 5 needs no suffix: CC 2.1.280 lists it as native-1M (window 1e6,
    # native_1m, no supports_1m_suffix), so the bare id already gets the full
    # window. This retires the old "Usage credits required for 1M context"
    # workaround that pinned Sonnet to 200K.
    # Only when the launch actually uses the alias: exporting these
    # unconditionally pinned every session's "opus" alias (and CC's
    # /model default) to Opus 4.6, so newer Opus releases were never picked
    # up automatically. Without the export CC resolves "opus" to its
    # current default Opus.
    if "--model opus" in claude_cmd:
        env_setup += ' && export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-opus-4-6[1m]"'
    if "--model sonnet" in claude_cmd:
        env_setup += ' && export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet-5"'
    if agent_id:
        env_setup += f" && export XY_AGENT_ID={agent_id} && export AHIVE_AGENT_ID={agent_id}"
    _sp.run(["tmux", "send-keys", "-t", pane_id, env_setup, "Enter"],
            check=True, capture_output=True, timeout=TMUX_CMD_TIMEOUT)
    # Launch Claude
    _sp.run(["tmux", "send-keys", "-t", pane_id, claude_cmd, "Enter"],
            check=True, capture_output=True, timeout=TMUX_CMD_TIMEOUT)
    return pane_id


def graceful_kill_tmux(pane_id: str, session_name: str):
    """Send Ctrl-C to interrupt Claude, then kill the pane and session."""
    try:
        _sp.run(["tmux", "send-keys", "-t", pane_id, "C-c"], capture_output=True, timeout=TMUX_CMD_TIMEOUT)
        _sp.run(["tmux", "send-keys", "-t", pane_id, "C-c"], capture_output=True, timeout=TMUX_CMD_TIMEOUT)
        _sp.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True, timeout=TMUX_CMD_TIMEOUT)
    except (OSError, _sp.TimeoutExpired):
        logger.warning("Failed graceful tmux kill for pane %s", pane_id, exc_info=True)
    try:
        _sp.run(["tmux", "kill-session", "-t", session_name], capture_output=True, timeout=TMUX_CMD_TIMEOUT)
    except (OSError, _sp.TimeoutExpired):
        logger.debug("tmux kill-session %s failed (may already be dead)", session_name)


# ---- Tmux session naming -----------------------------------------------------
# New agents launch with `xy-{id[:8]}`; legacy agents used `ah-{id[:8]}`.
# Detection / cleanup must accept both prefixes so running pre-rename agents
# keep working after upgrade.

TMUX_SESSION_PREFIX = "xy-"
TMUX_SESSION_LEGACY_PREFIX = "ah-"


def tmux_session_name(agent_id: str) -> str:
    """Canonical tmux session name for new agents (`xy-{id[:8]}`)."""
    return f"{TMUX_SESSION_PREFIX}{agent_id[:8]}"


def tmux_session_candidates(agent_id: str) -> list[str]:
    """All tmux session names that could belong to this agent (new + legacy)."""
    return [
        f"{TMUX_SESSION_PREFIX}{agent_id[:8]}",
        f"{TMUX_SESSION_LEGACY_PREFIX}{agent_id[:8]}",
    ]


def is_managed_tmux_session(name: str) -> bool:
    """True if this tmux session was launched by the orchestrator."""
    return name.startswith(TMUX_SESSION_PREFIX) or name.startswith(TMUX_SESSION_LEGACY_PREFIX)


def tmux_session_to_agent_prefix(name: str) -> str | None:
    """Extract agent_id prefix from a managed tmux session name, or None."""
    if name.startswith(TMUX_SESSION_PREFIX):
        return name[len(TMUX_SESSION_PREFIX):]
    if name.startswith(TMUX_SESSION_LEGACY_PREFIX):
        return name[len(TMUX_SESSION_LEGACY_PREFIX):]
    return None


def graceful_kill_tmux_agent(pane_id: str | None, agent_id: str):
    """Kill the agent's tmux pane (if given) and ALL session candidates.

    Tries both the new `xy-` and legacy `ah-` session names so pre-rename
    agents are properly cleaned up.
    """
    if pane_id:
        try:
            _sp.run(["tmux", "send-keys", "-t", pane_id, "C-c"], capture_output=True, timeout=TMUX_CMD_TIMEOUT)
            _sp.run(["tmux", "send-keys", "-t", pane_id, "C-c"], capture_output=True, timeout=TMUX_CMD_TIMEOUT)
            _sp.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True, timeout=TMUX_CMD_TIMEOUT)
        except (OSError, _sp.TimeoutExpired):
            logger.warning("Failed graceful tmux kill for pane %s", pane_id, exc_info=True)
    for session_name in tmux_session_candidates(agent_id):
        try:
            _sp.run(["tmux", "kill-session", "-t", session_name], capture_output=True, timeout=TMUX_CMD_TIMEOUT)
        except (OSError, _sp.TimeoutExpired):
            logger.debug("tmux kill-session %s failed (may already be dead)", session_name)


# Claude Code's `--worktree` flag hard-rejects names longer than this
# ("Invalid worktree name: must be 64 characters or fewer"), which makes the
# CLI exit before the TUI starts and the agent launch fail.
MAX_WORKTREE_NAME_LEN = 64


def sanitize_worktree_name(name: str) -> str:
    """Normalize a worktree name so `claude --worktree <name>` never rejects it.

    LLM- or user-supplied names can carry invalid chars, repeated tokens, or
    run past Claude Code's 64-char limit. Collapse to kebab-case, drop adjacent
    duplicate tokens, and cap length on a token boundary (no dangling word).
    """
    name = re.sub(r"[^a-z0-9-]", "-", (name or "").lower())
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        return "task"
    # Drop adjacent duplicate tokens: "active-xylo-xylo-3dgs" -> "active-xylo-3dgs".
    tokens: list[str] = []
    for tok in name.split("-"):
        if tok and (not tokens or tokens[-1] != tok):
            tokens.append(tok)
    name = "-".join(tokens)
    if len(name) > MAX_WORKTREE_NAME_LEN:
        head = name[:MAX_WORKTREE_NAME_LEN]
        name = (head.rsplit("-", 1)[0] if "-" in head else head).strip("-")
    return name or "task"


def generate_worktree_name_local(prompt: str) -> str:
    """Generate a short branch-style worktree name from the prompt (no API)."""
    words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt).lower().split()
    skip = {"the", "a", "an", "to", "in", "on", "for", "and", "or", "is", "it", "of", "with", "my", "me", "i", "this", "that", "please", "can", "you", "do", "make", "let"}
    words = [w for w in words if w not in skip][:4]
    return sanitize_worktree_name("-".join(words) if words else "task")


def enrich_agent_briefs(rows, request, db: Session | None = None) -> list[AgentBrief]:
    """Convert Agent ORM rows to AgentBrief.

    is_generating is derived from generating_msg_id via property.
    starred is set when the agent's session_id (or legacy agent.id) appears in
    starred_sessions — requires `db` to be passed.
    """
    starred_ids: set[str] = set()
    if db is not None and rows:
        ids_to_check = []
        for r in rows:
            if r.session_id:
                ids_to_check.append(r.session_id)
            ids_to_check.append(r.id)
        if ids_to_check:
            starred_ids = {
                row[0] for row in db.query(StarredSession.session_id)
                .filter(StarredSession.session_id.in_(ids_to_check))
                .all()
            }

    briefs: list[AgentBrief] = []
    for row in rows:
        brief = AgentBrief.model_validate(row)
        if starred_ids and (
            (row.session_id and row.session_id in starred_ids)
            or row.id in starred_ids
        ):
            brief.starred = True
        briefs.append(brief)
    return briefs
