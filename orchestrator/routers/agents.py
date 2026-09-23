"""Agent routes — create, launch, list, update, messages, interactive answers."""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import time as _time
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import CC_MODEL, CLAUDE_HOME, DISPLAY_DIR, PORT, VALID_MODELS
from database import SessionLocal, get_db
from models import (
    Agent, AgentInsightSuggestion, AgentMode, AgentStatus,
    Message, MessageRole, MessageStatus, Project, Task, TaskStatus,
)
from agent_dispatcher import ACTIVE_STATUSES, ALIVE_STATUSES, TERMINAL_STATUSES
from plat import platform as _platform
from schemas import (
    AgentBrief, AgentCreate, AgentInsightSuggestionOut, AgentOut,
    DisplayEntry, PreSentDisplayResponse, SentDisplayResponse,
    MessageOut, MessageSearchResponse, MessageSearchResult,
    SendMessage, UpdateMessage,
)
from route_helpers import (
    check_project_capacity, compute_successor_id,
    create_tmux_claude_session, enrich_agent_briefs,
    get_agent_or_404, get_project_or_404,
    generate_worktree_name_local, graceful_kill_tmux,
    graceful_kill_tmux_agent,
    subprocess_clean_env, tmux_launch_sem,
    tmux_session_candidates, tmux_session_name,
    TMUX_CMD_TIMEOUT, TMUX_SESSION_PREFIX,
    TUI_STARTUP_TIMEOUT, TUI_SETTLE_DELAY,
    MAX_STARTING_AGENTS,
    IMPORT_CHECK_TIMEOUT, SUBPROCESS_STRIP_VARS,
)
from utils import utcnow as _utcnow, is_interrupt_message
from task_state_machine import can_transition, InvalidTransitionError
from task_state import TaskStateMachine
from websocket import emit_task_update, emit_agent_update

logger = logging.getLogger("orchestrator")

_MODEL_TO_ALIAS = {
    "claude-opus-4-6": "opus",
    "claude-sonnet-5": "sonnet",
}

def _model_for_cli(model: str) -> str:
    """Use alias for 1M-capable models so ANTHROPIC_DEFAULT_*_MODEL env var takes effect."""
    return _MODEL_TO_ALIAS.get(model, model)

router = APIRouter(tags=["agents"])

# Aliases for route_helpers functions (original code used underscore-prefixed names)
_check_project_capacity = check_project_capacity
_create_tmux_claude_session = create_tmux_claude_session
_graceful_kill_tmux = graceful_kill_tmux
_tmux_launch_sem = tmux_launch_sem
_TMUX_CMD_TIMEOUT = TMUX_CMD_TIMEOUT
_TUI_STARTUP_TIMEOUT = TUI_STARTUP_TIMEOUT
_TUI_SETTLE_DELAY = TUI_SETTLE_DELAY
_MAX_STARTING_AGENTS = MAX_STARTING_AGENTS
_IMPORT_CHECK_TIMEOUT = IMPORT_CHECK_TIMEOUT
_generate_worktree_name_local = generate_worktree_name_local
_enrich_agent_briefs = enrich_agent_briefs


def _discover_session_id_from_pane(tmux_pane: str, project_path: str) -> str:
    """Discover the active session_id for a tmux pane.

    Strategy (in order):
    1. Check /tmp/xy-pending-sessions/ (and legacy /tmp/ahive-pending-sessions/)
       for an entry matching this pane
    2. Scan open files for JSONL belonging to the pane's process tree
    3. Read Claude Code's session_id from its tasks dir (latest lock file)
    """
    if not tmux_pane:
        return ""

    # Strategy 1: pending session files written by the SessionStart hook
    from route_helpers import pending_sessions_dirs
    for pending_dir in pending_sessions_dirs():
        if not os.path.isdir(pending_dir):
            continue
        for fname in os.listdir(pending_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(pending_dir, fname)) as f:
                    info = json.load(f)
                if info.get("tmux_pane") == tmux_pane and info.get("session_id"):
                    return info["session_id"]
            except (OSError, json.JSONDecodeError):
                logger.debug("Skipped pending session file: %s", fname)
                continue

    # Strategy 2: scan process tree's open files
    from session_cache import session_source_dir
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-t", tmux_pane, "-p", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return ""
        pane_pid = int(r.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return ""

    # Collect descendant PIDs via platform layer
    desc_pids = {pane_pid}
    for child_pid, _comm in _platform.get_child_pids(pane_pid):
        desc_pids.add(child_pid)

    sdir = session_source_dir(project_path)
    if os.path.isdir(sdir):
        for pid in desc_pids:
            for fpath in _platform.get_open_files(pid):
                if fpath.startswith(sdir) and fpath.endswith(".jsonl"):
                    basename = os.path.basename(fpath)
                    return basename.rsplit(".jsonl", 1)[0]

    # Strategy 3: check Claude Code's tasks directory for the active session
    for pid in desc_pids:
        for fpath in _platform.get_open_files(pid):
            if "/tasks/" in fpath:
                parts = fpath.split("/tasks/")
                if len(parts) > 1:
                    sid_part = parts[1].split("/")[0]
                    if len(sid_part) >= 32 and "-" in sid_part:
                        return sid_part

    return ""


# ---------------------------------------------------------------------------
# Background summary helpers (deferred import from routers.projects)
# ---------------------------------------------------------------------------

def _run_agent_summary_background(*args, **kwargs):
    """Proxy — deferred import from routers.projects to avoid circular imports."""
    from routers.projects import _run_agent_summary_background as _impl
    return _impl(*args, **kwargs)


def _generate_retry_summary_background(*args, **kwargs):
    """Proxy — deferred import from routers.projects to avoid circular imports."""
    from routers.projects import _generate_retry_summary_background as _impl
    return _impl(*args, **kwargs)


# ---------------------------------------------------------------------------
# Hooks config helpers (called from lifespan too)
# ---------------------------------------------------------------------------

# Bundled fallback — identical structure to .xylo-internal/templates/agent-hooks.json
# with {{BASE_URL}} and {{HOOK_SCRIPT}} as substitution placeholders.
_DEFAULT_AGENT_HOOKS_JSON = """\
{
  "PreToolUse": [
    {
      "matcher": "Bash|Write|Edit",
      "hooks": [{"type": "command", "command": "{{HOOK_SCRIPT}}"}]
    },
    {
      "hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-tool-activity",
                 "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                 "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]
    },
    {
      "hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-permission",
                 "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                 "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"],
                 "timeout": 86400}]
    }
  ],
  "PostToolUse": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-tool-activity",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ],
  "PostToolUseFailure": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-tool-activity",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ],
  "SubagentStart": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-tool-activity",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ],
  "SubagentStop": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-tool-activity",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ],
  "PreCompact": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-tool-activity",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ],
  "PostCompact": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-post-compact",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ],
  "PermissionRequest": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-permission-request",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"],
                "timeout": 86400}]}
  ],
  "Stop": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-stop",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ],
  "SessionEnd": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-session-end",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ],
  "UserPromptSubmit": [
    {"hooks": [{"type": "http", "url": "{{BASE_URL}}/agent-user-prompt",
                "headers": {"X-Agent-Id": "$XY_AGENT_ID"},
                "allowedEnvVars": ["XY_AGENT_ID", "AHIVE_AGENT_ID"]}]}
  ]
}
"""


def _load_agent_hooks_template(base_url: str, hook_script: str) -> dict:
    """Load the agent-hooks template from .xylo-internal/templates/agent-hooks.json,
    substitute {{BASE_URL}} and {{HOOK_SCRIPT}} placeholders, and return the dict.

    Falls back to _DEFAULT_AGENT_HOOKS_JSON if the file is missing or corrupt,
    and self-heals by writing the bundled default to disk so subsequent reads
    succeed without a fallback.
    """
    from config import PROJECTS_DIR
    # Hard-fail if PROJECTS_DIR is unset: os.path.join("", ...) returns a
    # relative path, and the loader's self-heal would silently write the
    # bundled template to whatever the process cwd happens to be (worktree
    # subdir, /tmp, etc.) instead of the canonical
    # `<PROJECTS_DIR>/.xylo-internal/templates/`.
    assert PROJECTS_DIR, (
        "PROJECTS_DIR is empty — cannot resolve .xylo-internal templates dir. "
        "Ensure HOST_PROJECTS_DIR or PROJECTS_DIR is exported in the orchestrator "
        "process env (run.sh handles this; manual python invocations need it set)."
    )
    template_path = os.path.join(
        PROJECTS_DIR, ".xylo-internal", "templates", "agent-hooks.json",
    )
    raw: str | None = None
    try:
        with open(template_path) as f:
            raw = f.read()
        json.loads(raw)  # validate before substitution
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("agent-hooks template unavailable (%s), using built-in default", exc)
        raw = _DEFAULT_AGENT_HOOKS_JSON
        try:
            os.makedirs(os.path.dirname(template_path), exist_ok=True)
            with open(template_path, "w") as f:
                f.write(raw)
            logger.info("agent-hooks template written to %s", template_path)
        except OSError as write_exc:
            logger.warning("Could not write agent-hooks template: %s", write_exc)
    substituted = raw.replace("{{BASE_URL}}", base_url).replace("{{HOOK_SCRIPT}}", hook_script)
    return json.loads(substituted)


def _write_agent_hooks_config(project_path: str):
    """Write project-level hooks (PreToolUse safety + activity, PostToolUse, Stop)
    to settings.local.json.

    SessionStart is handled globally via _write_global_session_hook().
    """
    base_url = f"http://localhost:{PORT}/api/hooks"

    hook_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "hooks", "pretooluse-safety.py",
    )

    desired_hooks = _load_agent_hooks_template(base_url, hook_script)

    settings_local_dir = os.path.join(project_path, ".claude")
    settings_local_path = os.path.join(settings_local_dir, "settings.local.json")
    try:
        os.makedirs(settings_local_dir, exist_ok=True)

        existing = {}
        if os.path.isfile(settings_local_path):
            with open(settings_local_path, "r") as f:
                existing = json.load(f)

        current_hooks = existing.get("hooks", {})
        # Remove stale SessionStart from project-level (now global)
        current_hooks.pop("SessionStart", None)
        merged_hooks = {**current_hooks, **desired_hooks}

        if existing.get("hooks") != merged_hooks:
            existing["hooks"] = merged_hooks
            with open(settings_local_path, "w") as f:
                json.dump(existing, f, indent=2)
            logger.info("Preflight: wrote agent hooks to %s", settings_local_path)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Preflight: failed to write agent hooks config: %s", e)


def _write_mcp_config(project_path: str):
    """Write .mcp.json to give agents access to the Xylocopa MCP server.

    For the xylocopa project itself, we skip — the committed .mcp.json
    with a relative path is preferred.  For other projects, we write an
    absolute path so agents can call list_sessions / read_session.

    Transparently migrates legacy "agenthive" key to "xylocopa" when it
    points at our mcp_server.py.
    """
    xylocopa_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

    # Skip if this IS the xylocopa project (committed .mcp.json handles it)
    if os.path.realpath(project_path) == os.path.realpath(xylocopa_root):
        return

    mcp_server_path = os.path.join(xylocopa_root, "orchestrator", "mcp_server.py")
    if not os.path.isfile(mcp_server_path):
        return

    mcp_json_path = os.path.join(project_path, ".mcp.json")
    desired_entry = {
        "command": "python3",
        "args": [mcp_server_path],
    }

    try:
        existing = {}
        if os.path.isfile(mcp_json_path):
            with open(mcp_json_path, "r") as f:
                existing = json.load(f)

        # Merge — don't clobber other MCP servers the project may have
        servers = existing.get("mcpServers", {})
        changed = False

        # Migrate legacy "agenthive" key when it points at our mcp_server.py
        legacy = servers.get("agenthive")
        if isinstance(legacy, dict) and mcp_server_path in (legacy.get("args") or []):
            servers.pop("agenthive", None)
            changed = True

        if servers.get("xylocopa") != desired_entry:
            servers["xylocopa"] = desired_entry
            changed = True

        if changed:
            existing["mcpServers"] = servers
            with open(mcp_json_path, "w") as f:
                json.dump(existing, f, indent=2)
                f.write("\n")
            logger.info("Preflight: wrote MCP config to %s", mcp_json_path)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Preflight: failed to write MCP config: %s", e)


def _write_global_session_hook():
    """Write SessionStart hook to ~/.claude/settings.json (global).

    This ensures ALL claude processes on this machine fire the hook,
    regardless of which project they're in or whether Xylocopa started
    them.  The hook script tries HTTP POST to the orchestrator and falls
    back to writing a local file when the orchestrator is offline.
    """
    hook_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "hooks", "session-start.sh",
    )

    desired_hook = [{
        "hooks": [{
            "type": "command",
            "command": hook_script,
        }],
    }]

    claude_home = os.path.expanduser("~/.claude")
    settings_path = os.path.join(claude_home, "settings.json")
    try:
        existing = {}
        if os.path.isfile(settings_path):
            with open(settings_path, "r") as f:
                existing = json.load(f)

        current_hooks = existing.get("hooks", {})
        if current_hooks.get("SessionStart") == desired_hook:
            return  # Already configured

        current_hooks["SessionStart"] = desired_hook
        existing["hooks"] = current_hooks
        with open(settings_path, "w") as f:
            json.dump(existing, f, indent=2)
        logger.info("Wrote global SessionStart hook to %s", settings_path)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to write global session hook: %s", e)


def _preflight_claude_project(project_path: str):
    """Ensure all Claude Code prerequisites are met before launching.

    Claude Code can show up to 8 blocking dialogs on startup.  This preflight
    pre-accepts all of them so the TUI starts straight into the REPL.

    Dialogs handled (in startup order):
    1. Onboarding wizard (theme, login, security notes)
    2. Custom API key approval
    3. Workspace trust ("do you trust this folder?")
    4. Hooks trust
    5. CLAUDE.md external includes warning
    6. Bypass-permissions mode warning
    7. MCP server approval
    8. Project onboarding

    Config files:
    - ~/.claude.json          — per-project trust + global onboarding state
    - ~/.claude/settings.json — global settings (permissions, cleanup, MCP)

    Trust cascades from parent directories: trusting PROJECTS_DIR root covers
    all projects under it.
    """
    from config import CLAUDE_HOME, PROJECTS_DIR

    # --- 1. ~/.claude.json (global state + per-project trust) ---
    claude_json_path = os.path.join(os.path.expanduser("~"), ".claude.json")
    for _ in range(3):
        try:
            data = {}
            if os.path.isfile(claude_json_path):
                with open(claude_json_path, "r") as f:
                    data = json.load(f)

            changed = False

            # Global onboarding (dialog 1)
            if data.get("hasCompletedOnboarding") is not True:
                data["hasCompletedOnboarding"] = True
                changed = True

            projects = data.setdefault("projects", {})

            # Trust the PROJECTS_DIR root — cascades to all child projects
            # so we don't need per-project entries for trust alone.
            projects_dir = PROJECTS_DIR or ""
            if projects_dir:
                root_cfg = projects.setdefault(projects_dir, {})
                if root_cfg.get("hasTrustDialogAccepted") is not True:
                    root_cfg["hasTrustDialogAccepted"] = True
                    root_cfg["hasTrustDialogHooksAccepted"] = True
                    changed = True

            # Per-project flags (dialogs 3-5, 8)
            proj_cfg = projects.setdefault(project_path, {})
            _trust_fields = {
                "hasTrustDialogAccepted": True,
                "hasTrustDialogHooksAccepted": True,
                "hasCompletedProjectOnboarding": True,
                "hasClaudeMdExternalIncludesApproved": True,
                "hasClaudeMdExternalIncludesWarningShown": True,
            }
            for field, value in _trust_fields.items():
                if proj_cfg.get(field) is not value:
                    proj_cfg[field] = value
                    changed = True
            if not proj_cfg.get("projectOnboardingSeenCount"):
                proj_cfg["projectOnboardingSeenCount"] = 1
                changed = True

            if changed:
                with open(claude_json_path, "w") as f:
                    json.dump(data, f, indent=2)
                logger.info("Preflight: updated ~/.claude.json for %s", project_path)
            break
        except (json.JSONDecodeError, OSError) as e:
            # Retry after brief delay — concurrent Claude agents may be writing
            # to the same ~/.claude.json file, causing transient read/write races
            logger.warning("Preflight: failed to update ~/.claude.json: %s", e)
            import time
            time.sleep(0.1)

    # --- 2. ~/.claude/settings.json (global settings) ---
    settings_path = os.path.join(CLAUDE_HOME, "settings.json")
    try:
        settings = {}
        if os.path.isfile(settings_path):
            with open(settings_path, "r") as f:
                settings = json.load(f)

        changed = False
        _global_flags = {
            "skipDangerousModePermissionPrompt": True,   # dialog 6
            "cleanupPeriodDays": 36500,                  # prevent session cleanup
            "enableAllProjectMcpServers": True,          # dialog 7
        }
        for flag, value in _global_flags.items():
            if settings.get(flag) != value:
                settings[flag] = value
                changed = True

        if changed:
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
            logger.info("Preflight: updated ~/.claude/settings.json")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Preflight: failed to update settings.json: %s", e)

    # --- 3. .claude/settings.local.json (project-level agent hooks) ---
    _write_agent_hooks_config(project_path)

    # --- 4. .mcp.json (MCP server for cross-session reference) ---
    _write_mcp_config(project_path)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/api/agents", response_model=AgentOut, status_code=201)
async def create_agent(body: AgentCreate, request: Request, db: Session = Depends(get_db)):
    """Create a new agent with an initial message."""
    project = db.get(Project, body.project)
    if not project:
        raise HTTPException(status_code=400, detail=f"Project '{body.project}' not found")
    if project.archived:
        raise HTTPException(status_code=400, detail="Cannot create agents for archived projects — activate first")

    # Enforce per-project capacity
    _check_project_capacity(db, body.project)

    # Generate agent name from first ~50 chars of prompt
    name = body.prompt[:50].strip()
    if len(body.prompt) > 50:
        name += "..."

    # Resolve model: explicit > project default > global default
    agent_model = body.model or project.default_model or CC_MODEL
    if agent_model not in VALID_MODELS:
        logger.warning("Invalid model %r for agent, falling back to %s", agent_model, CC_MODEL)
        agent_model = CC_MODEL

    # Determine initial status: IDLE if importing CLI session
    is_sync = body.sync_session and body.resume_session_id
    initial_status = AgentStatus.IDLE if is_sync else AgentStatus.STARTING

    # Pre-generate agent ID so we can use it for worktree naming
    import uuid
    agent_id = uuid.uuid4().hex[:12]

    # Resolve worktree name: "auto" → GPT-generated branch name
    wt = body.worktree
    if wt == "auto":
        wt = _generate_worktree_name_local(body.prompt)

    # Infer worktree from session JSONL location when resuming/syncing
    # without an explicit worktree (e.g. Sessions tab resume)
    if not wt and body.resume_session_id:
        from agent_dispatcher import _infer_worktree_from_session
        _inferred = _infer_worktree_from_session(body.resume_session_id, project.path)
        if _inferred:
            wt = _inferred
            logger.info("Inferred worktree=%s from session JSONL path", wt)

    agent = Agent(
        id=agent_id,
        project=body.project,
        name=name,
        mode=body.mode,
        status=initial_status,
        model=agent_model,
        effort=body.effort,
        worktree=wt,
        timeout_seconds=body.timeout_seconds,
        session_id=body.resume_session_id,
        cli_sync=True,  # All agents are tmux-managed
        skip_permissions=body.skip_permissions,
        last_message_preview=name,
        last_message_at=_utcnow(),
    )
    db.add(agent)
    db.flush()  # Get agent.id

    if is_sync:
        # Sync mode: import existing history, don't create initial user message
        db.commit()
        db.refresh(agent)

        # Import history and start live sync in background
        ad = getattr(request.app.state, "agent_dispatcher", None)
        if ad:
            imported = ad.import_session_history(
                agent.id, body.resume_session_id, project.path
            )
            logger.info(
                "Agent %s: imported %d messages from CLI session %s",
                agent.id, imported, body.resume_session_id,
            )
            # Start live sync to tail ongoing CLI activity
            ad.start_session_sync(
                agent.id, body.resume_session_id, project.path
            )
    else:
        # Non-sync mode: launch a tmux session and send the prompt.
        # All agents must be tmux-managed — no subprocess dispatch.
        import shlex
        import secrets
        import subprocess as _sp
        import uuid as _uuid

        from config import CLAUDE_BIN

        # Get existing tmux session names for collision check
        try:
            _tmux_ls = _sp.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5,
            )
            _existing_tmux = set(_tmux_ls.stdout.strip().splitlines()) if _tmux_ls.returncode == 0 else set()
        except (OSError, _sp.TimeoutExpired):
            _existing_tmux = set()

        tmux_session = tmux_session_name(agent_id)
        if tmux_session in _existing_tmux:
            # Collision — regenerate (extremely unlikely since agent_id is unique)
            tmux_session = f"{TMUX_SESSION_PREFIX}{secrets.token_hex(4)}"

        # Pre-generate session UUID and write .owner sidecar
        pre_session_id = str(_uuid.uuid4())
        from agent_dispatcher import _write_session_owner
        from session_cache import session_source_dir
        _sdir = session_source_dir(project.path)
        os.makedirs(_sdir, exist_ok=True)
        _write_session_owner(_sdir, pre_session_id, agent_id)

        # Build claude command (interactive mode — no -p)
        cmd_parts = [CLAUDE_BIN, "--session-id", pre_session_id,
                     "--output-format", "stream-json", "--verbose"]
        if body.skip_permissions:
            cmd_parts.append("--dangerously-skip-permissions")
        if agent_model:
            cmd_parts += ["--model", _model_for_cli(agent_model)]
        if body.effort:
            cmd_parts += ["--effort", body.effort]
        if wt:
            cmd_parts += ["--worktree", wt]
        claude_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

        _preflight_claude_project(project.path)
        pane_id = await asyncio.to_thread(
            _create_tmux_claude_session,
            tmux_session, project.path, claude_cmd, agent_id,
        )
        agent.tmux_pane = pane_id

        # Commit the agent row NOW so we release SQLite's exclusive write lock
        # before the ~1s _prepare_dispatch (OpenAI translate + FTS5).  Otherwise
        # any other writer (e.g. sync_loop processing JSONL deltas) blocks for
        # up to busy_timeout=5s and crashes with "database is locked".
        db.commit()
        db.refresh(agent)

        # Create the initial user message.  Schedule the launch task FIRST
        # with a prompt future so its TUI polling (~1s) overlaps with
        # _prepare_dispatch (OpenAI translate + FTS5, ~1s).
        ad = getattr(request.app.state, "agent_dispatcher", None)
        launch_prompt = None
        prompt_future: asyncio.Future | None = None
        launch_task = None
        if ad and body.prompt:
            prompt_future = asyncio.get_running_loop().create_future()
            launch_task = asyncio.ensure_future(
                _launch_tmux_background(
                    ad, agent.id, pane_id, prompt_future, project.path,
                    pre_session_id=pre_session_id,
                )
            )
            ad.track_launch_task(agent.id, launch_task)

        if ad:
            try:
                msg, launch_prompt, _ = await ad._prepare_dispatch(
                    db, agent, project, body.prompt,
                    source="web",
                    wrap_prompt=True,
                )
            except BaseException as e:
                if prompt_future is not None and not prompt_future.done():
                    prompt_future.set_exception(e)
                raise
        else:
            msg = Message(
                agent_id=agent.id,
                role=MessageRole.USER,
                content=body.prompt,
                source="web",
            )
            db.add(msg)
        msg.status = MessageStatus.COMPLETED
        msg.completed_at = _utcnow()

        db.commit()
        db.refresh(agent)

        # Hand the prepared prompt off to the already-running launch task.
        if prompt_future is not None and not prompt_future.done():
            prompt_future.set_result(launch_prompt)

    logger.info("Agent %s created for project %s (mode %s, sync=%s, tmux=%s)",
                agent.id, agent.project, agent.mode.value, is_sync, bool(agent.tmux_pane))
    return agent


@router.post("/api/agents/launch-tmux", status_code=201)
async def launch_tmux_agent(request: Request, db: Session = Depends(get_db)):
    """Launch an interactive claude CLI session in a new tmux pane.

    Starts Claude in interactive mode (full TUI), then sends the prompt
    as input after Claude finishes loading.  The user can attach to the
    tmux pane to interact with Claude directly.

    A background task detects the session JSONL and starts live-syncing
    the conversation into the webapp.
    """
    import shlex
    import subprocess
    from config import CLAUDE_BIN

    body = await request.json()
    project_name = body.get("project")
    prompt = body.get("prompt", "").strip()
    model = body.get("model")
    effort = body.get("effort")
    worktree = body.get("worktree")
    skip_permissions = body.get("skip_permissions", True)
    task_id = body.get("task_id")

    # Reject if too many agents are already queued for launch
    starting_count = db.query(func.count(Agent.id)).filter(
        Agent.status == AgentStatus.STARTING,
    ).scalar() or 0
    if starting_count >= _MAX_STARTING_AGENTS:
        raise HTTPException(
            status_code=429,
            detail="Too many agents launching — please wait for current launches to finish",
        )

    if not project_name:
        raise HTTPException(status_code=400, detail="Project is required")

    proj = get_project_or_404(db, project_name)
    if not os.path.isdir(proj.path):
        raise HTTPException(status_code=400, detail="Project directory not found on disk")

    # Enforce per-project capacity
    _check_project_capacity(db, project_name)

    # Each agent gets its own tmux session: tmux_session_name(agent_id)
    # Pre-generate agent ID, ensuring no DB or tmux session name collision
    import secrets
    import subprocess as _sp

    # Get existing tmux session names for collision check
    try:
        _tmux_ls = _sp.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        _existing_tmux = set(_tmux_ls.stdout.strip().splitlines()) if _tmux_ls.returncode == 0 else set()
    except (OSError, _sp.TimeoutExpired):
        _existing_tmux = set()

    for _ in range(20):
        agent_hex = secrets.token_hex(6)
        tmux_session = tmux_session_name(agent_hex)
        if db.get(Agent, agent_hex) is None and tmux_session not in _existing_tmux:
            break
    else:
        raise HTTPException(status_code=500, detail="Failed to generate unique agent ID")

    # Resolve worktree name: "auto" → GPT-generated branch name
    if worktree == "auto" and prompt:
        worktree = _generate_worktree_name_local(prompt)

    # Pre-generate session UUID so we can pre-write the .owner sidecar
    # BEFORE launching Claude.  This ensures the session has identity
    # from the very first moment the JSONL file appears.
    import uuid as _uuid
    pre_session_id = str(_uuid.uuid4())

    # Build the claude command in INTERACTIVE mode (no -p, so the user
    # gets the full TUI and can attach via tmux).
    cmd_parts = [CLAUDE_BIN,
                  "--session-id", pre_session_id,
                  "--output-format", "stream-json", "--verbose"]
    if skip_permissions:
        cmd_parts.append("--dangerously-skip-permissions")
    if model:
        cmd_parts += ["--model", _model_for_cli(model)]
    if effort:
        cmd_parts += ["--effort", effort]
    if worktree:
        cmd_parts += ["--worktree", worktree]
    claude_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

    # Pre-write .owner sidecar before launching Claude.
    # Slug is unknown at this point — will be backfilled by the sync loop.
    from agent_dispatcher import _write_session_owner
    from session_cache import session_source_dir
    _sdir = session_source_dir(proj.path)
    os.makedirs(_sdir, exist_ok=True)
    _write_session_owner(_sdir, pre_session_id, agent_hex)

    # Pre-accept the project trust dialog in ~/.claude.json so Claude
    # doesn't show the "Is this a project you trust?" prompt that blocks
    # the TUI from starting.  This dialog appears on first launch in any
    # directory that hasn't been explicitly trusted yet.
    _preflight_claude_project(proj.path)

    pane_id = await asyncio.to_thread(
        _create_tmux_claude_session,
        tmux_session, proj.path, claude_cmd, agent_hex,
    )

    # Create Agent record immediately so the frontend can navigate to it.
    agent_name = (prompt or "CLI session")[:80]
    resolved_model = model or proj.default_model
    if resolved_model not in VALID_MODELS:
        logger.warning("Invalid model %r for tmux agent, falling back to %s", resolved_model, CC_MODEL)
        resolved_model = CC_MODEL
    agent = Agent(
        id=agent_hex,
        project=project_name,
        name=agent_name,
        mode=AgentMode.AUTO,
        status=AgentStatus.STARTING,
        model=resolved_model,
        cli_sync=True,
        tmux_pane=pane_id,
        effort=effort if effort else None,
        worktree=worktree if worktree else None,
        skip_permissions=skip_permissions,
        task_id=task_id if task_id else None,
        last_message_preview=agent_name,
        last_message_at=datetime.now(timezone.utc),
    )
    db.add(agent)
    db.flush()

    from display_writer import write_retry_marker_for_agent
    write_retry_marker_for_agent(db, agent)

    # Link task → agent if task_id provided
    _task_linked = False
    if task_id:
        _task = db.get(Task, task_id)
        if _task and can_transition(_task.status, TaskStatus.EXECUTING):
            _task.agent_id = agent.id
            TaskStateMachine.transition(_task, TaskStatus.EXECUTING)
            _task.worktree_name = worktree if worktree else None
            if worktree:
                _task.branch_name = _task.branch_name or f"worktree-{worktree}"
            _task_linked = True

    # Commit the agent row NOW so we release SQLite's exclusive write lock
    # before the ~1s _prepare_dispatch (OpenAI translate + FTS5).  Otherwise
    # any other writer (e.g. sync_loop processing JSONL deltas) blocks for
    # up to busy_timeout=5s and crashes with "database is locked".
    db.commit()
    db.refresh(agent)
    if _task_linked:
        db.refresh(_task)

    # Save the initial prompt and prepare wrapped version for Claude.
    # Schedule the launch task FIRST with a prompt future so its TUI
    # polling (~1s) overlaps with _prepare_dispatch (~1s).
    launch_prompt = None
    ad = getattr(request.app.state, "agent_dispatcher", None)
    prompt_future: asyncio.Future | None = None
    launch_task = None
    if prompt and ad:
        prompt_future = asyncio.get_running_loop().create_future()
        launch_task = asyncio.ensure_future(
            _launch_tmux_background(
                ad, agent.id, pane_id, prompt_future, proj.path,
                pre_session_id=pre_session_id,
            )
        )
        ad.track_launch_task(agent.id, launch_task)

    if prompt:
        if ad:
            try:
                msg, launch_prompt, _ = await ad._prepare_dispatch(
                    db, agent, proj, prompt,
                    source="web",
                    wrap_prompt=True,
                )
            except BaseException as e:
                if prompt_future is not None and not prompt_future.done():
                    prompt_future.set_exception(e)
                raise
        else:
            msg = Message(
                agent_id=agent.id,
                role=MessageRole.USER,
                content=prompt,
                source="web",
            )
            db.add(msg)
        msg.status = MessageStatus.COMPLETED
        msg.completed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(agent)

    # Emit task update after commit if task was linked
    if _task_linked:
        asyncio.ensure_future(emit_task_update(
            _task.id, _task.status.value, _task.project_name or "",
            title=_task.title,
        ))

    # Hand the prepared prompt off to the already-running launch task.
    if prompt_future is not None and not prompt_future.done():
        prompt_future.set_result(launch_prompt)

    logger.info(
        "Launched tmux claude session in pane %s for project %s (agent %s)",
        pane_id, project_name, agent.id,
    )
    return AgentOut.model_validate(agent)


async def _launch_tmux_background(
    ad, agent_id: str, pane_id: str,
    prompt: "str | asyncio.Future[str | None]",
    project_path: str,
    pre_session_id: str | None = None,
):
    """Background task for tmux agent launch.

    1. Wait for Claude's TUI to start (polls for a claude process in the pane)
    2. Send the user prompt
    3. Receive the session_id via SessionStart hook and start the sync loop

    On any failure, transitions the agent to ERROR so it doesn't stay
    stuck in STARTING forever.  Handles cancellation gracefully so that
    stopping the agent while the launch is in progress doesn't leave
    zombie error transitions.
    """
    import subprocess

    from agent_dispatcher import (
        _build_tmux_claude_map,
        capture_tmux_pane,
        send_tmux_message,
    )
    from database import SessionLocal
    from websocket import emit_agent_update

    def _mark_error(reason: str):
        """Transition agent to ERROR status on launch failure."""
        db = SessionLocal()
        try:
            agent = db.get(Agent, agent_id)
            if agent:
                ad.error_agent_cleanup(
                    db, agent, reason,
                    add_message=False, fail_executing=False,
                    cancel_tasks=False,
                )
                db.commit()
        finally:
            db.close()
        logger.warning("tmux launch failed for agent %s: %s", agent_id, reason)

    # Register the SessionStart hook future BEFORE waiting on the
    # semaphore: claude was already started by the synchronous request
    # handler that scheduled us, so its SessionStart hook can fire
    # any moment now.  We must be ready to catch it.
    hook_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    ad._launch_session_futures[agent_id] = hook_future

    await _tmux_launch_sem.acquire()
    # Register this pane so _detect_successor_session skips sessions
    # belonging to this launching agent (prevents cross-agent theft).
    ad._launching_panes[agent_id] = pane_id
    try:
        # Step 1: Wait for Claude's TUI to fully load (up to 30s).
        # Two phases:
        #   a) Detect the claude process in the pane
        #   b) Wait for the TUI input prompt (❯) to appear in the pane content
        # Poll every 200ms — real TUI startup is ~400ms in trusted dirs,
        # so sleep(1) wasted ~2s of the first-prompt latency.
        process_detected = False
        for _ in range(_TUI_STARTUP_TIMEOUT * 5):
            await asyncio.sleep(0.2)
            pane_map = _build_tmux_claude_map()
            if pane_id in pane_map and not pane_map[pane_id]["is_orchestrator"]:
                process_detected = True
                break
        if not process_detected:
            _mark_error(
                "Claude TUI did not start in pane %s within %ds "
                "(project_path: %s)" % (pane_id, _TUI_STARTUP_TIMEOUT, project_path)
            )
            return

        # Wait for the REPL to be fully mounted.
        # IMPORTANT: The ❯ prompt character appears in the welcome box BEFORE
        # the REPL input handler is mounted.  On first launch in a new project
        # directory, showSetupScreens() takes ~4 seconds (vs ~200ms for
        # established projects).  We use the status bar ("⏵⏵ bypass permissions"
        # or "shift+tab to cycle") as the definitive REPL-mounted signal,
        # since it only renders after the full TUI component tree is ready.
        #
        # Also handles the project trust dialog ("Is this a project you
        # trust?") which can appear despite pre-acceptance if ~/.claude.json
        # was regenerated.  If detected, we press Enter to accept it.
        tui_ready = False
        trust_dialog_handled = False
        for _ in range(_TUI_STARTUP_TIMEOUT * 5):
            await asyncio.sleep(0.2)
            pane_text = capture_tmux_pane(pane_id)
            if pane_text is None:
                continue

            # Check for the REPL status bar (definitive ready signal).
            # With --dangerously-skip-permissions: "⏵⏵ bypass ... shift+tab"
            # Without (supervised mode): "? for shortcuts ... /effort"
            for ln in pane_text.split("\n"):
                if ("\u23f5" in ln and "shift+tab" in ln) or \
                   ("? for shortcuts" in ln):
                    tui_ready = True
                    break
            if tui_ready:
                break

            # Check for the project trust dialog and auto-accept it
            if not trust_dialog_handled and "trust this folder" in pane_text.lower():
                subprocess.run(
                    ["tmux", "send-keys", "-t", pane_id, "Enter"],
                    capture_output=True, text=True, timeout=_TMUX_CMD_TIMEOUT,
                )
                trust_dialog_handled = True
                logger.info(
                    "Auto-accepted project trust dialog in pane %s for agent %s",
                    pane_id, agent_id,
                )
        if not tui_ready:
            _mark_error(
                "Claude TUI did not fully initialize in pane %s within %ds "
                "(project_path: %s)" % (pane_id, _TUI_STARTUP_TIMEOUT, project_path)
            )
            return

        # Extra settle time after REPL mount.  On first-launch projects
        # showSetupScreens() finishes ~200ms before REPL mount; add a buffer
        # to ensure the input handler is fully wired up.
        await asyncio.sleep(_TUI_SETTLE_DELAY)

        # Step 2: Send the prompt and wait for the SessionStart hook to
        # tell us the session_id.  start_session_sync() needs the actual
        # pane CWD (not project_path) so worktree agents watch the right
        # session directory.
        actual_cwd = project_path
        try:
            cwd_result = subprocess.run(
                ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_current_path}"],
                capture_output=True, text=True, timeout=5,
            )
            if cwd_result.returncode == 0 and cwd_result.stdout.strip():
                actual_cwd = os.path.realpath(cwd_result.stdout.strip())
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("tmux pane CWD lookup failed for %s: %s", pane_id, e)

        # If the SessionStart hook fired before we got here (its HTTP
        # POST can race the launch task being scheduled), the hook
        # handler will have written the signal file even if the
        # in-memory future was empty at that moment.  Drain it now so
        # we don't pointlessly wait on an already-passed event.
        from route_helpers import find_session_signal as _find_signal
        _stale_signal = _find_signal(agent_id)
        if _stale_signal and not hook_future.done():
            try:
                with open(_stale_signal) as _sf:
                    _early_sid = _sf.read().strip()
                if _early_sid:
                    hook_future.set_result(_early_sid)
                    try:
                        os.unlink(_stale_signal)
                    except OSError:
                        pass
            except OSError as _e:
                logger.debug("read early SessionStart signal: %s", _e)

        # SessionStart hook is mandatory infrastructure (xylocopa installs
        # it into ~/.claude/settings.json on startup). If it doesn't fire
        # within this window something is broken — surface that as a hard
        # error rather than silently stalling launch with a JSONL scan.
        _HOOK_TIMEOUT_SECS = 30.0

        # Resolve the prompt. Callers schedule us with an asyncio.Future
        # so prompt prep (query_insights translate + FTS5, ~1s) overlaps
        # with TUI startup polling above; by the time we get here, prep
        # is usually done and the await is instant.
        if isinstance(prompt, asyncio.Future):
            try:
                prompt = await prompt
            except Exception as e:
                _mark_error(f"prompt preparation failed: {e}")
                return
        if not prompt:
            logger.info("tmux launch agent %s: no prompt to send", agent_id)
            return

        if not send_tmux_message(pane_id, prompt):
            _mark_error(
                "Failed to send prompt to tmux pane %s "
                "(project_path: %s)" % (pane_id, project_path)
            )
            return

        logger.info("tmux launch agent %s: prompt sent", agent_id)

        try:
            session_id = await asyncio.wait_for(
                hook_future, timeout=_HOOK_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            _mark_error(
                "SessionStart hook did not fire within %.0fs for agent %s — "
                "check ~/.claude/settings.json hook configuration "
                "(project_path: %s)" % (_HOOK_TIMEOUT_SECS, agent_id, project_path)
            )
            return

        logger.info(
            "tmux launch agent %s: session %s received via SessionStart hook",
            agent_id, session_id[:12],
        )

        # Update agent with session_id and transition to IDLE
        db = SessionLocal()
        try:
            agent = db.get(Agent, agent_id)
            if not agent or agent.status == AgentStatus.STOPPED:
                return
            # Final guard: verify no other agent grabbed this session
            # in the meantime (race protection)
            existing = db.query(Agent).filter(
                Agent.session_id == session_id,
                Agent.id != agent_id,
            ).first()
            if existing:
                logger.warning(
                    "Session %s already owned by agent %s — "
                    "cannot assign to agent %s",
                    session_id[:12], existing.id, agent_id,
                )
                _mark_error(
                    "Session %s already owned by another agent" % session_id[:12]
                )
                return
            agent.session_id = session_id
            # Status stays STARTING — sync_engine flips it to EXECUTING
            # when it sees the first user turn in JSONL. Writing IDLE
            # here caused the startup flicker (STARTING → IDLE → EXECUTING)
            # because the IDLE write raced the inference path's EXECUTING
            # write. Under the state-machine refactor, the launch task is
            # NOT a status writer — sync_engine is.
            try:
                db.commit()
            except IntegrityError:
                # UNIQUE constraint on session_id — another agent raced us
                db.rollback()
                _mark_error(
                    "Session %s UNIQUE constraint violation" % session_id[:12]
                )
                return
        finally:
            db.close()

        # Start the session sync loop — use actual_cwd so worktree agents
        # watch the correct session directory
        ad.start_session_sync(agent_id, session_id, actual_cwd)
        logger.info(
            "Started sync for launched tmux agent %s (session %s)",
            agent_id, session_id[:12],
        )
    except asyncio.CancelledError:
        logger.info("Launch task cancelled for agent %s", agent_id)
    finally:
        _tmux_launch_sem.release()
        ad._launch_tasks.pop(agent_id, None)
        ad._launching_panes.pop(agent_id, None)
        _fut = ad._launch_session_futures.pop(agent_id, None)
        if _fut and not _fut.done():
            _fut.cancel()


@router.post("/api/agents/scan")
async def scan_agents(request: Request, db: Session = Depends(get_db)):
    """Trigger an immediate liveness scan of all agents.

    Runs the same reaping logic as the periodic dispatcher tick, so dead
    CLI agents are marked STOPPED right away instead of waiting ~30s.
    """
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad:
        ad._reap_dead_agents(db)
        db.commit()
    return {"ok": True}


@router.post("/api/agents/wake-sync-all")
async def wake_all_agent_syncs(request: Request, db: Session = Depends(get_db)):
    """Wake sync loops for all active (non-STOPPED) agents.

    Mirrors the per-agent ``/api/agents/{id}/wake-sync`` endpoint:
    recovers ERROR→IDLE, restarts dead sync loops, and dispatches any
    queued pre-sent messages for idle agents.
    """
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if not ad:
        raise HTTPException(status_code=503, detail="Dispatcher not ready")
    active = db.query(Agent).filter(Agent.status != AgentStatus.STOPPED).all()

    # Phase 1: recover ERROR agents (matches individual wake-sync)
    recovered_ids: list[str] = []
    for agent in active:
        if agent.status == AgentStatus.ERROR:
            agent.status = AgentStatus.IDLE
            agent.error_message = None
            recovered_ids.append(agent.id)
    if recovered_ids:
        db.commit()
        from websocket import emit_agent_update
        for aid in recovered_ids:
            ag = db.get(Agent, aid)
            if ag:
                ad._emit(emit_agent_update(ag.id, "IDLE", ag.project))
        logger.info("wake-sync-all: recovered %d ERROR agents", len(recovered_ids))

    # Phase 2: wake / restart sync loops + dispatch any queued messages
    woken = 0
    for agent in active:
        asyncio.ensure_future(ad.dispatch_pending_message(agent.id, delay=0))
        if ad.wake_sync(agent.id):
            woken += 1
    return {"ok": True, "woken": woken, "recovered": len(recovered_ids), "total": len(active)}


# ---- Unlinked (detected) sessions ----

_UNLINKED_DIR: str | None = None


def _get_unlinked_dir() -> str:
    """Return (and lazily create) the unlinked-sessions directory."""
    global _UNLINKED_DIR
    if _UNLINKED_DIR is None:
        from config import BACKUP_DIR
        _UNLINKED_DIR = os.path.join(BACKUP_DIR, "unlinked-sessions")
    os.makedirs(_UNLINKED_DIR, exist_ok=True)
    return _UNLINKED_DIR


def _safe_unlink(path: str) -> bool:
    """Remove a file, returning True on success. Logs on failure."""
    try:
        os.unlink(path)
        return True
    except OSError as e:
        logger.debug("_safe_unlink: %s: %s", path, e)
        return False


def _allocate_agent_id(db) -> str:
    """Generate a unique 12-hex-char agent ID. Raises on failure."""
    import secrets
    for _ in range(20):
        agent_hex = secrets.token_hex(6)
        if db.get(Agent, agent_hex) is None:
            return agent_hex
    raise HTTPException(status_code=500, detail="Failed to generate agent ID")


def _tmux_pane_alive(pane: str) -> bool:
    """Check if a tmux pane exists and has a running process."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        )
        return bool(r.returncode == 0 and r.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def _clean_stale_unlinked(max_age: int = 3600):
    """Remove unlinked session entries that are no longer actionable."""
    udir = _get_unlinked_dir()
    try:
        fnames = os.listdir(udir)
    except OSError as e:
        logger.debug("_clean_stale_unlinked: cannot list dir: %s", e)
        return 0

    now = _time.time()
    removed = 0
    for fname in fnames:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(udir, fname)
        try:
            with open(fpath) as f:
                info = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("_clean_stale_unlinked: removing malformed %s: %s", fname, e)
            removed += _safe_unlink(fpath)
            continue

        if info.get("rejected"):
            sid = (info.get("session_id") or "").strip()
            if sid:
                from agent_dispatcher import _find_claude_pid_for_session
                cwd_hint = (info.get("cwd") or "").strip() or None
                if _find_claude_pid_for_session(sid, cwd_hint=cwd_hint):
                    continue
            age = now - float(info.get("timestamp") or 0)
            if 0 < age < 10:
                continue
            removed += _safe_unlink(fpath)
            continue

        transcript = info.get("transcript_path", "")
        if transcript and os.path.isfile(transcript):
            if now - os.path.getmtime(transcript) < max_age:
                continue
        tmux_pane = info.get("tmux_pane", "")
        if tmux_pane and _tmux_pane_alive(tmux_pane):
            continue
        removed += _safe_unlink(fpath)

    if removed:
        logger.info("Cleaned %d stale unlinked session entries", removed)
    return removed


@router.get("/api/unlinked-sessions")
async def list_unlinked_sessions(db: Session = Depends(get_db)):
    """List manually-launched Claude Code sessions not bound to any agent."""
    _clean_stale_unlinked()
    udir = _get_unlinked_dir()

    # Session IDs owned by ACTIVE agents — filter them out
    bound_sids: set[str] = {
        r[0] for r in db.query(Agent.session_id).filter(
            Agent.session_id.is_not(None),
            Agent.status.notin_([AgentStatus.STOPPED, AgentStatus.ERROR]),
        ).all()
    }

    sessions = []
    try:
        fnames = sorted(os.listdir(udir))
    except OSError:
        return sessions

    for fname in fnames:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(udir, fname)
        try:
            with open(fpath) as f:
                info = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.debug("Skipped unlinked session file: %s", fname)
            continue
        if info.get("dropped"):
            continue
        sid = info.get("session_id", "")
        if sid in bound_sids:
            _safe_unlink(fpath)
            continue
        if not info.get("project_name"):
            cwd = info.get("cwd", "")
            info["project_name"] = os.path.basename(cwd.rstrip("/")) if cwd else ""
        info["file"] = fname
        sessions.append(info)
    return sessions


@router.post("/api/unlinked-sessions/{file_key}/adopt")
async def adopt_unlinked_session(
    file_key: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Bind an unlinked session to a new agent and start syncing.

    file_key is the stem of the JSON entry (session_id or pane-X).
    Body: {"project": "project-name"}
    Optional: {"agent_id": "existing-agent-id"} to bind to existing agent.
    """
    import secrets
    from session_cache import session_source_dir

    udir = _get_unlinked_dir()
    info_path = os.path.join(udir, f"{file_key}.json")
    if not os.path.isfile(info_path):
        raise HTTPException(status_code=404, detail="Unlinked session not found")

    try:
        with open(info_path) as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read session info: {e}")

    body = await request.json()
    project_name = body.get("project") or os.path.basename(info.get("cwd", "").rstrip("/"))
    existing_agent_id = body.get("agent_id")

    proj = get_project_or_404(db, project_name)

    actual_cwd = info.get("cwd", "")

    # Resolve session_id — may be empty for poll-detected sessions.
    # Discover from the most recent JSONL in the project's session dir.
    session_id = info.get("session_id", "")
    if not session_id:
        tmux_pane = info.get("tmux_pane", "")
        session_id = _discover_session_id_from_pane(tmux_pane, proj.path)
        if not session_id:
            raise HTTPException(
                status_code=400,
                detail="Could not determine session ID for this pane. Is Claude Code running?",
            )

    # Check if session is already bound to an agent
    existing = db.query(Agent).filter(Agent.session_id == session_id).first()
    if existing:
        if existing.status in (AgentStatus.STOPPED, AgentStatus.ERROR):
            existing.session_id = None
            db.flush()
        else:
            _safe_unlink(info_path)
            raise HTTPException(
                status_code=409,
                detail=f"Session already bound to active agent {existing.id} ({existing.name})",
            )

    if not existing_agent_id:
        _check_project_capacity(db, project_name)

    ad = getattr(request.app.state, "agent_dispatcher", None)
    if not ad:
        raise HTTPException(status_code=503, detail="Agent dispatcher not ready")

    if existing_agent_id:
        # Bind to existing agent
        agent = get_agent_or_404(db, existing_agent_id)
        agent.session_id = session_id
        agent.tmux_pane = info.get("tmux_pane")
        if agent.status in (AgentStatus.STOPPED, AgentStatus.ERROR):
            agent.status = AgentStatus.IDLE
        agent.cli_sync = True
    else:
        agent_hex = _allocate_agent_id(db)
        agent = Agent(
            id=agent_hex,
            project=project_name,
            name=(
                f"Detected: {info['tmux_session']}"
                if info.get("tmux_session")
                else f"Manual: {os.path.basename(info.get('cwd', 'session'))}"
            )[:80],
            mode=AgentMode.AUTO,
            status=AgentStatus.IDLE,
            model=proj.default_model or CC_MODEL,
            cli_sync=True,
            session_id=session_id,
            tmux_pane=info.get("tmux_pane"),
            last_message_preview="Confirmed session",
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(agent)

    db.commit()
    db.refresh(agent)

    # Write .owner, start sync, and immediately wake for first import
    ad.start_session_sync(agent.id, session_id, proj.path, cwd=actual_cwd)
    ad.wake_sync(agent.id)

    _safe_unlink(info_path)

    logger.info(
        "Adopted unlinked session %s → agent %s (project %s)",
        session_id[:12], agent.id, project_name,
    )

    asyncio.ensure_future(emit_agent_update(agent.id, agent.status.value, agent.project))

    return AgentOut.model_validate(agent)


@router.post("/api/unlinked-sessions/{file_key}/drop")
async def drop_unlinked_session(file_key: str):
    """Dismiss an unlinked entry without adopting.

    Marks the on-disk file with ``dropped: true`` rather than deleting it,
    so a future SessionStart hook for the same session_id won't re-surface
    it. Once the underlying claude process exits, _clean_stale_unlinked
    deletes the file outright.
    """
    udir = _get_unlinked_dir()
    info_path = os.path.join(udir, f"{file_key}.json")
    if not os.path.isfile(info_path):
        raise HTTPException(status_code=404, detail="Unlinked session not found")
    try:
        with open(info_path) as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read entry: {e}")
    info["dropped"] = True
    info["dropped_at"] = _time.time()
    try:
        with open(info_path, "w") as f:
            json.dump(info, f)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to mark dropped: {e}")
    logger.info("Dropped unlinked entry %s (session %s)",
                file_key, (info.get("session_id") or "")[:12])
    return {"ok": True}


@router.post("/api/unlinked-sessions/{file_key}/convert-and-adopt")
async def convert_and_adopt_unlinked_session(
    file_key: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Convert a non-tmux claude session into a managed tmux agent.

    Used for "rejected" entries (claude running outside tmux). Steps:
      1. Look up the running claude PID by session_id.
      2. SIGTERM it (escalate to SIGKILL on timeout) — the user's terminal
         claude exits. JSONL is flushed by CC on signal.
      3. Create a fresh xy-<new_agent_id> tmux session, resume the same
         session_id inside it.
      4. Register the agent in DB + start sync.

    file_key is "rejected-<sid_prefix>" — the JSON content carries the
    full session_id used everywhere downstream.
    """
    import secrets
    import asyncio as _asyncio
    from agent_dispatcher import _find_claude_pid_for_session, _terminate_pid
    from route_helpers import create_tmux_claude_session, tmux_session_name

    udir = _get_unlinked_dir()
    info_path = os.path.join(udir, f"{file_key}.json")
    if not os.path.isfile(info_path):
        raise HTTPException(status_code=404, detail="Rejected session not found")
    try:
        with open(info_path) as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read entry: {e}")

    if not info.get("rejected"):
        raise HTTPException(
            status_code=400,
            detail="Entry is already adoptable — use /adopt instead",
        )

    session_id = (info.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Entry has no session_id")

    body = await request.json() if (await request.body()) else {}
    project_name = body.get("project") or info.get("project_name")
    proj = db.get(Project, project_name) if project_name else None
    if not proj:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

    if db.query(Agent).filter(Agent.session_id == session_id).first():
        _safe_unlink(info_path)
        raise HTTPException(
            status_code=409, detail="Session is already bound to an agent",
        )

    _check_project_capacity(db, project_name)

    ad = getattr(request.app.state, "agent_dispatcher", None)
    if not ad:
        raise HTTPException(status_code=503, detail="Agent dispatcher not ready")

    entry_cwd = (info.get("cwd") or "").strip()

    # 1. Find + terminate the user's existing claude attached to session_id.
    #    Pass entry_cwd as a hint so we can locate the PID even before any
    #    JSONL has been written (e.g. claude still sitting at welcome /
    #    trust prompt — no JSONL → JSONL-based project lookup returns nil).
    pid = _find_claude_pid_for_session(session_id, cwd_hint=entry_cwd or None)
    if pid:
        logger.info(
            "convert-and-adopt: terminating claude pid=%d for session %s",
            pid, session_id[:12],
        )
        ok = await _asyncio.to_thread(_terminate_pid, pid, 3.0)
        if not ok:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to terminate existing claude (pid={pid}). "
                       "Please exit it manually and retry.",
            )
    else:
        # No live claude — the JSONL is still on disk, resume will work.
        logger.info(
            "convert-and-adopt: no live claude found for session %s, resuming anyway",
            session_id[:12],
        )

    agent_hex = _allocate_agent_id(db)

    # 3. Decide launch flag: --resume needs JSONL data on disk; if the
    #    user's claude was killed before sending any message, the sid has
    #    no JSONL and `claude --resume` fails with "No conversation
    #    found". In that case launch with --session-id <sid> instead so
    #    the new claude inherits the same sid (matches launch_tmux_agent's
    #    pre_session_id pattern) — agent.session_id binding stays valid.
    from session_cache import session_source_dir as _ssd
    launch_cwd = entry_cwd if entry_cwd and os.path.isdir(entry_cwd) else proj.path
    sid_jsonl = os.path.join(_ssd(launch_cwd), f"{session_id}.jsonl")
    has_jsonl = os.path.isfile(sid_jsonl) and os.path.getsize(sid_jsonl) > 0
    sid_flag = "--resume" if has_jsonl else "--session-id"

    # Pre-write .owner so the sync engine can claim the sid on first turn
    # (mirrors launch_tmux_agent's pre-launch ownership stamping).
    try:
        from agent_dispatcher import _write_session_owner
        _sdir = _ssd(launch_cwd)
        os.makedirs(_sdir, exist_ok=True)
        _write_session_owner(_sdir, session_id, agent_hex)
    except OSError as e:
        logger.warning("convert-and-adopt: pre-write .owner failed: %s", e)

    tmux_name = tmux_session_name(agent_hex)
    import shlex
    claude_cmd = " ".join(shlex.quote(p) for p in [
        "claude", "--dangerously-skip-permissions",
        "--model", _model_for_cli(proj.default_model or CC_MODEL),
        sid_flag, session_id,
    ])
    try:
        pane_id = await _asyncio.to_thread(
            create_tmux_claude_session,
            tmux_name, launch_cwd, claude_cmd, agent_hex,
        )
    except Exception as e:
        logger.exception("convert-and-adopt: tmux launch failed")
        raise HTTPException(status_code=500, detail=f"tmux launch failed: {e}")

    # 4. Register the agent.
    agent = Agent(
        id=agent_hex,
        project=project_name,
        name=f"Adopted: {os.path.basename(info.get('cwd', 'session'))}"[:80],
        mode=AgentMode.AUTO,
        status=AgentStatus.IDLE,
        model=proj.default_model or CC_MODEL,
        cli_sync=True,
        session_id=session_id,
        tmux_pane=pane_id,
        last_message_preview="Converted from CLI to tmux",
        last_message_at=datetime.now(timezone.utc),
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    ad.start_session_sync(agent.id, session_id, proj.path, cwd=launch_cwd)
    ad.wake_sync(agent.id)
    _safe_unlink(info_path)

    logger.info(
        "convert-and-adopt: session %s → agent %s (tmux %s pane %s, "
        "killed pid=%s, flag=%s, cwd=%s)",
        session_id[:12], agent.id, tmux_name, pane_id,
        pid or "none", sid_flag, launch_cwd,
    )
    asyncio.ensure_future(emit_agent_update(agent.id, agent.status.value, agent.project))
    return AgentOut.model_validate(agent)


def _do_replay_pending_unlinked(db: Session) -> dict:
    """Replay SessionStart events the hook stashed when backend was offline.

    Reads /tmp/xy-pending-unlinked/, applies a liveness check + the same
    guards as the live hook path (routers/hooks.py:1242-1318), and promotes
    valid events into BACKUP_DIR/unlinked-sessions/ via the same
    _write_unlinked_entry() used by the live path. Stash files are deleted
    on success or skip.

    Used by both the manual HTTP refresh path and the lifespan startup
    handler in main.py — lets entries surface without a manual click after
    a restart.
    """
    from agent_dispatcher import (
        _build_tmux_claude_map,
        _write_rejected_unlinked_entry,
        _write_unlinked_entry,
    )
    from plat import platform as _platform
    from routers.projects import active_projects
    from route_helpers import session_signal_path

    stash_dir = "/tmp/xy-pending-unlinked"
    if not os.path.isdir(stash_dir):
        return {"ok": True, "replayed": 0, "rotated": 0, "rejected": 0, "skipped": 0}

    projects = active_projects(db)
    project_reals = [(p, os.path.realpath(p.path)) for p in projects]
    # Build pane → live-claude map once. Used to drop stash entries whose
    # pane is dead or whose session_id has rotated past the stashed value.
    pane_map = _build_tmux_claude_map()

    replayed = rotated = rejected = skipped = 0
    for fname in os.listdir(stash_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(stash_dir, fname)
        try:
            with open(fpath) as f:
                ev = json.load(f)
        except (OSError, json.JSONDecodeError):
            _safe_unlink(fpath)
            continue

        sid = (ev.get("session_id") or "").strip()
        cwd = (ev.get("cwd") or "").strip()
        pane = (ev.get("tmux_pane") or "").strip()
        if not sid or not cwd:
            _safe_unlink(fpath)
            continue

        # Pane-less stash entry: surface as rejected if cwd is in a project.
        # Mirrors the live hook path so non-tmux claude detections don't
        # vanish when backend was offline at session start.
        if not pane:
            cwd_real = os.path.realpath(cwd)
            matched = next(
                (p for p, p_real in project_reals
                 if cwd_real == p_real or cwd_real.startswith(p_real + "/")),
                None,
            )
            if matched and not db.query(Agent).filter(Agent.session_id == sid).first():
                _write_rejected_unlinked_entry(
                    session_id=sid,
                    cwd=cwd_real,
                    project_name=matched.name,
                    reason="missing_tmux_pane",
                )
                rejected += 1
            else:
                skipped += 1
            _safe_unlink(fpath)
            continue

        # Liveness: pane must still be running a non-orchestrator claude
        # whose cwd matches the stashed cwd.  The stashed event itself is
        # the authoritative sid for this pane (stash files are pane-keyed
        # and overwrite, so the latest hook event is what's on disk).  We
        # only verify the pane still has a live claude in the same dir;
        # any sid-from-PID introspection is unreliable (claude doesn't
        # keep its JSONL fd open) and was the cause of every stashed
        # entry being silently dropped on replay.
        info = pane_map.get(pane)
        if not info or info["is_orchestrator"]:
            _safe_unlink(fpath)
            skipped += 1
            continue
        try:
            live_cwd = _platform.get_process_cwd(info["pid"])
        except OSError:
            live_cwd = ""
        if not live_cwd or os.path.realpath(live_cwd) != os.path.realpath(cwd):
            _safe_unlink(fpath)
            skipped += 1
            continue

        # Guard A: session_id already owned by an agent
        if db.query(Agent).filter(Agent.session_id == sid).first():
            _safe_unlink(fpath)
            skipped += 1
            continue

        # Guard B: pane owned by managed agent → rotation signal, not unlinked
        owner = db.query(Agent).filter(
            Agent.tmux_pane == pane,
            Agent.status.notin_([AgentStatus.STOPPED, AgentStatus.ERROR]),
        ).first()
        if owner:
            try:
                with open(session_signal_path(owner.id), "w") as sf:
                    sf.write(sid)
                rotated += 1
            except OSError as e:
                logger.warning("replay_pending_unlinked: rotation signal failed: %s", e)
            _safe_unlink(fpath)
            continue

        # Guard C: cwd matches a registered project
        cwd_real = os.path.realpath(cwd)
        matched = next(
            (p for p, p_real in project_reals
             if cwd_real == p_real or cwd_real.startswith(p_real + "/")),
            None,
        )
        if not matched:
            _safe_unlink(fpath)
            skipped += 1
            continue

        _write_unlinked_entry(
            session_id=sid,
            cwd=cwd_real,
            tmux_pane=pane,
            tmux_session=(ev.get("tmux_session") or None),
            project_name=matched.name,
        )
        _safe_unlink(fpath)
        replayed += 1

    if replayed or rotated or rejected or skipped:
        logger.info(
            "replay_pending_unlinked: replayed=%d rotated=%d rejected=%d skipped=%d",
            replayed, rotated, rejected, skipped,
        )
    return {
        "ok": True,
        "replayed": replayed,
        "rotated": rotated,
        "rejected": rejected,
        "skipped": skipped,
    }


@router.post("/api/unlinked-sessions/replay")
async def replay_pending_unlinked(request: Request, db: Session = Depends(get_db)):
    """Manual replay endpoint — Agents page refresh button calls this."""
    return _do_replay_pending_unlinked(db)


@router.get("/api/agents", response_model=list[AgentBrief])
async def list_agents(
    request: Request,
    project: str | None = None,
    status: AgentStatus | None = None,
    limit: int | None = None,
    db: Session = Depends(get_db),
):
    """List agents with optional filters."""
    q = db.query(Agent).filter(Agent.is_subagent == False)  # noqa: E712
    if project:
        q = q.filter(Agent.project == project)
    if status:
        q = q.filter(Agent.status == status)
    q = q.order_by(Agent.last_message_at.desc().nulls_last(), Agent.created_at.desc())
    if limit:
        q = q.limit(limit)
    return _enrich_agent_briefs(q.all(), request, db)


@router.get("/api/agents/unread")
async def agents_unread_count(db: Session = Depends(get_db)):
    """Total unread message count across the top 50 agents (matching list limit)."""
    top = (
        db.query(Agent.unread_count)
        .filter(Agent.is_subagent == False)  # noqa: E712
        .order_by(Agent.last_message_at.desc().nulls_last(), Agent.created_at.desc())
        .limit(50)
        .all()
    )
    total = sum(r[0] for r in top if r[0])
    return {"unread": int(total)}


@router.get("/api/agents/unread-list")
async def agents_unread_list(db: Session = Depends(get_db)):
    """Unread agents sorted oldest-first (FIFO) for the notification-jump FAB."""
    rows = (
        db.query(Agent.id, Agent.unread_count, Agent.last_message_at)
        .filter(Agent.is_subagent == False)  # noqa: E712
        .filter(Agent.unread_count > 0)
        .order_by(Agent.last_message_at.asc().nulls_last(), Agent.created_at.asc())
        .limit(50)
        .all()
    )
    return {
        "agents": [
            {
                "id": r[0],
                "unread_count": int(r[1] or 0),
                "last_message_at": r[2].isoformat() if r[2] else None,
            }
            for r in rows
        ],
    }


@router.get("/api/messages/search", response_model=MessageSearchResponse)
async def search_messages(
    q: str,
    project: str | None = None,
    role: MessageRole | None = None,
    limit: int = 50,
    include_subagents: bool = True,
    db: Session = Depends(get_db),
):
    """Full-text search across all message content.

    Supports glob-style wildcards * and ? in q. Without wildcards, performs
    a substring (contains) match. With wildcards, the pattern controls
    anchoring (e.g. `foo*` = starts-with, `*foo` = ends-with, `*foo*` = contains).
    """
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")
    if limit > 200:
        limit = 200

    has_wildcard = "*" in q or "?" in q
    # Always escape SQL LIKE meta-chars in the raw input
    safe_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if has_wildcard:
        # Convert glob wildcards to LIKE wildcards (after escaping above).
        # Auto-wrap with % so wildcards add flexibility without forcing
        # anchoring — `fetch*Project` means "contains fetch...Project anywhere".
        converted = safe_q.replace("*", "%").replace("?", "_")
        like_pattern = converted if converted.startswith("%") else f"%{converted}"
        if not like_pattern.endswith("%"):
            like_pattern = f"{like_pattern}%"
    else:
        like_pattern = f"%{safe_q}%"

    query = (
        db.query(Message, Agent.name, Agent.project)
        .join(Agent, Message.agent_id == Agent.id)
        .filter(or_(
            Message.content.ilike(like_pattern, escape="\\"),
            Agent.id.ilike(like_pattern, escape="\\"),
            Agent.name.ilike(like_pattern, escape="\\"),
        ))
    )
    if project:
        query = query.filter(Agent.project == project)
    if role:
        query = query.filter(Message.role == role)
    if not include_subagents:
        query = query.filter(Agent.is_subagent == False)  # noqa: E712

    total = query.count()
    rows = query.order_by(Message.created_at.desc()).limit(limit).all()

    # Build a snippet matcher: regex when wildcards present, plain substring otherwise
    if has_wildcard:
        import re as _re
        pattern_re = _re.compile(
            ".*?".join(_re.escape(part) for part in q.replace("?", "*").split("*")),
            _re.IGNORECASE,
        ) if q.strip("*?") else None
    else:
        pattern_re = None

    results = []
    for msg, agent_name, agent_project in rows:
        # Build snippet: ~80 chars before and after first match
        content = msg.content or ""
        idx, match_len = -1, len(q)
        if pattern_re is not None:
            m = pattern_re.search(content)
            if m:
                idx, match_len = m.start(), max(1, m.end() - m.start())
        else:
            idx = content.lower().find(q.lower())
        if idx >= 0:
            start = max(0, idx - 80)
            end = min(len(content), idx + match_len + 80)
            snippet = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
        else:
            snippet = content[:160] + ("..." if len(content) > 160 else "")

        results.append(MessageSearchResult(
            message_id=msg.id,
            agent_id=msg.agent_id,
            agent_name=agent_name,
            project=agent_project,
            role=msg.role,
            content_snippet=snippet,
            created_at=msg.created_at,
        ))

    return MessageSearchResponse(results=results, total=total)


@router.get("/api/agents/{agent_id}/context-usage")
async def get_context_usage_endpoint(agent_id: str, db: Session = Depends(get_db)):
    """Token-budget snapshot for an agent.

    Returns headline numbers (total/limit/percent/model) plus the full
    5-component breakdown and rule-based suggestions. The chat-header
    pill bootstraps from this on mount; subsequent updates flow over WS
    via `emit_context_usage` so the popover never refetches separately.
    """
    get_agent_or_404(db, agent_id)
    from context import get_context_breakdown
    return get_context_breakdown(agent_id)


@router.get("/api/agents/{agent_id}/cc-sessions")
async def get_agent_cc_sessions(agent_id: str, db: Session = Depends(get_db)):
    """Tree of CC sessions (top-level + sub-sessions) for one agent.

    Returns the same shape as ``lifetime.cc_sessions`` but standalone for
    clients that just want the drill-down without the rest of the
    lifetime payload. Top-level sessions appear at the top of ``sessions``;
    sub-sessions are nested under their parent's ``sub_sessions`` array.
    """
    from models import CCSession
    from context.lifetime import build_cc_session_tree

    agent = get_agent_or_404(db, agent_id)
    rows = (
        db.query(CCSession)
        .filter(CCSession.agent_id == agent_id)
        .order_by(CCSession.started_at)
        .all()
    )
    tree = build_cc_session_tree(rows, agent.model)
    return {"sessions": tree}


@router.get("/api/agents/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: str, request: Request, include_subagents: bool = True,
                    db: Session = Depends(get_db)):
    """Get full agent details.

    ``include_subagents=false`` omits the embedded child list. The chat page
    polls this endpoint every 3 s and never reads ``subagents``; for an
    agent with hundreds of finished workflow children the list was ~260 KB
    per poll (measured 2026-09-22: 4.8 MB/min on a phone).
    """
    agent = get_agent_or_404(db, agent_id)

    # Compute live session file size + successor link
    result = AgentOut.model_validate(agent)
    result.successor_id = compute_successor_id(agent.id, db)
    if agent.session_id:
        project = db.get(Project, agent.project)
        if project:
            from agent_dispatcher import _resolve_session_jsonl
            jsonl_path = _resolve_session_jsonl(
                agent.session_id, project.path, agent.worktree,
            )
            try:
                result.session_size_bytes = os.path.getsize(jsonl_path)
            except OSError:
                pass
    # Attach child subagents (skipped for the chat-page poll). The ORM
    # relationship already populated result.subagents via model_validate,
    # so the opt-out must clear it explicitly.
    if include_subagents:
        child_rows = db.query(Agent).filter(
            Agent.parent_id == agent.id,
            Agent.is_subagent == True,  # noqa: E712
        ).order_by(Agent.created_at).all()
        if child_rows:
            result.subagents = [AgentBrief.model_validate(r) for r in child_rows]
    else:
        result.subagents = None

    return result


@router.delete("/api/agents/{agent_id}", response_model=AgentOut)
async def stop_agent(agent_id: str, request: Request,
                     generate_summary: bool = False,
                     task_complete: bool = True,
                     task_drop: bool = False,
                     incomplete_reason: str | None = None,
                     db: Session = Depends(get_db)):
    """Stop an agent — marks STOPPED."""
    agent = get_agent_or_404(db, agent_id)
    if agent.status == AgentStatus.STOPPED:
        raise HTTPException(status_code=400, detail="Agent is already stopped")

    # Capture task info before stopping (needed for background summary)
    _task_title = None
    _project_path = None
    _retry_task_id = None
    _retry_project_name = ""
    _retry_task_title = "Unknown task"
    _should_summarize = generate_summary and agent.task_id
    if agent.task_id:
        _t = db.get(Task, agent.task_id)
        if _t:
            _task_title = _t.title or "Unknown task"
        _p = db.get(Project, agent.project)
        _project_path = _p.path if _p else None
        if _should_summarize and not _project_path:
            _should_summarize = False

    # Kill the tmux pane/session if active
    if agent.tmux_pane:
        graceful_kill_tmux_agent(agent.tmux_pane, agent.id)
        logger.info("Killed tmux pane %s for agent %s", agent.tmux_pane, agent.id)

    ad = getattr(request.app.state, "agent_dispatcher", None)

    if ad:
        ad.stop_agent_cleanup(db, agent, "Agent stopped",
                              kill_tmux=False, fail_executing=True,
                              fail_reason="Agent stopped by user",
                              cascade_subagents=True,
                              skip_task_transition=True)
    else:
        agent.status = AgentStatus.STOPPED
        agent.tmux_pane = None

        # Mark any EXECUTING messages as FAILED so they don't stay stuck
        executing_msgs = db.query(Message).filter(
            Message.agent_id == agent.id,
            Message.status == MessageStatus.EXECUTING,
        ).all()
        for m in executing_msgs:
            m.status = MessageStatus.FAILED
            m.error_message = "Agent stopped by user"
            m.completed_at = datetime.now(timezone.utc)

        # Add system message
        db.add(Message(
            agent_id=agent.id,
            role=MessageRole.SYSTEM,
            content="Agent stopped",
            status=MessageStatus.COMPLETED,
            delivered_at=datetime.now(timezone.utc),
        ))

        # Cascade stop to child subagents
        child_subs = db.query(Agent).filter(
            Agent.parent_id == agent.id,
            Agent.is_subagent == True,  # noqa: E712
            Agent.status != AgentStatus.STOPPED,
        ).all()
        for sub in child_subs:
            sub.status = AgentStatus.STOPPED
            if sub.tmux_pane:
                try:
                    import subprocess as _sp2
                    _sp2.run(["tmux", "kill-pane", "-t", sub.tmux_pane],
                             capture_output=True, timeout=_TMUX_CMD_TIMEOUT)
                except (OSError, subprocess.TimeoutExpired):
                    logger.debug("Failed to kill tmux pane for subagent %s", sub.id)
            asyncio.ensure_future(emit_agent_update(sub.id, "STOPPED", sub.project))

        asyncio.ensure_future(emit_agent_update(agent.id, "STOPPED", agent.project))

    db.commit()
    db.refresh(agent)
    logger.info("Agent %s stopped", agent.id)

    # Flush "Agent stopped" to display file. flush_agent auto-emits the
    # new_message WS signal — no explicit emit needed.
    from display_writer import flush_agent as _stop_flush
    _stop_flush(agent.id)

    # Transition linked task based on user choice
    if agent.task_id:
        _linked_task = db.get(Task, agent.task_id)
        if _linked_task and _linked_task.status in (TaskStatus.EXECUTING, TaskStatus.COMPLETE):
            if task_drop:
                TaskStateMachine.transition(_linked_task, TaskStatus.CANCELLED, strict=False)
                if incomplete_reason:
                    db.add(Message(
                        agent_id=agent.id, role=MessageRole.SYSTEM,
                        content=f"Task dropped — {incomplete_reason}",
                        status=MessageStatus.COMPLETED,
                        delivered_at=datetime.now(timezone.utc),
                    ))
                logger.info("Task %s CANCELLED/dropped (agent %s stopped by user)", _linked_task.id, agent.id)
            elif task_complete:
                TaskStateMachine.transition(_linked_task, TaskStatus.COMPLETE, strict=False)
                logger.info("Task %s marked COMPLETE (agent %s stopped by user)", _linked_task.id, agent.id)
            else:
                # Build human-readable retry_context for _build_task_prompt
                _ctx_parts = []
                if incomplete_reason:
                    _ctx_parts.append(f"User feedback: {incomplete_reason}")
                    db.add(Message(
                        agent_id=agent.id, role=MessageRole.SYSTEM,
                        content=f"Redo — {incomplete_reason}",
                        status=MessageStatus.COMPLETED,
                        delivered_at=datetime.now(timezone.utc),
                    ))
                if _ctx_parts:
                    _linked_task.retry_context = "\n".join(_ctx_parts)
                # Mark summary as generating — background thread will replace
                _linked_task.agent_summary = ":::generating:::"
                _linked_task.attempt_number = (_linked_task.attempt_number or 0) + 1
                TaskStateMachine.transition(_linked_task, TaskStatus.INBOX, strict=False)
                logger.info("Task %s returned to INBOX attempt=%d (agent %s stopped by user)",
                            _linked_task.id, _linked_task.attempt_number, agent.id)
                # Always generate retry summary for incomplete tasks
                _retry_task_id = _linked_task.id
                _retry_project_name = _linked_task.project_name or ""
                _retry_task_title = _linked_task.title or "Unknown task"
            db.commit()
            # Flush drop/redo message to display file. flush_agent
            # auto-emits the new_message WS signal — no explicit emit.
            _stop_flush(agent.id)
            asyncio.ensure_future(emit_task_update(
                _linked_task.id, _linked_task.status.value, _linked_task.project_name or "",
                title=_linked_task.title, agent_id=agent.id,
            ))

    # Spawn background summary thread if requested
    if _should_summarize:
        agent.insight_status = "generating"
        db.commit()
        # Push insight_status=generating so other clients flip the pill
        # immediately. The earlier STOPPED emit (line ~1946 / cleanup path)
        # fires before this assignment and carries the stale value.
        asyncio.ensure_future(emit_agent_update(
            agent.id, "STOPPED", agent.project,
            insight_status="generating",
        ))
        from routers.projects import INSIGHT_EXECUTOR
        INSIGHT_EXECUTOR.submit(
            _run_agent_summary_background,
            agent.id, agent.name, _task_title, agent.project, _project_path,
        )
        logger.info("Spawned background summary for agent %s", agent.id)

    # Spawn retry summary for incomplete tasks (always, no toggle needed)
    if not task_complete and _retry_task_id and _project_path:
        from routers.projects import INSIGHT_EXECUTOR
        INSIGHT_EXECUTOR.submit(
            _generate_retry_summary_background,
            agent.id, _retry_task_id, _retry_task_title,
            _retry_project_name, _project_path, incomplete_reason,
        )
        logger.info("Spawned retry summary for task %s", _retry_task_id)

    return agent


# --- Agent Insight Suggestions ---

@router.get("/api/agents/{agent_id}/suggestions", response_model=list[AgentInsightSuggestionOut])
async def get_agent_suggestions(agent_id: str, status: str = "pending",
                                db: Session = Depends(get_db)):
    """Return insight suggestions for an agent, filtered by status.

    status: "pending" (default), "accepted", "rejected", or "processed" (accepted+rejected).
    """
    agent = get_agent_or_404(db, agent_id)
    q = db.query(AgentInsightSuggestion).filter(
        AgentInsightSuggestion.agent_id == agent_id,
    )
    if status == "processed":
        q = q.filter(AgentInsightSuggestion.status.in_(["accepted", "rejected"]))
    else:
        q = q.filter(AgentInsightSuggestion.status == status)
    rows = q.order_by(AgentInsightSuggestion.id).all()
    return rows


class _ApplySuggestionsBody(BaseModel):
    accepted: list[dict] = []  # [{id, edited_content?}]
    rejected_ids: list[int] = []


@router.post("/api/agents/{agent_id}/apply-suggestions")
async def apply_agent_suggestions(agent_id: str, body: _ApplySuggestionsBody,
                                  db: Session = Depends(get_db)):
    """Accept/reject insight suggestions — write accepted ones to PROGRESS.md + FTS5."""
    agent = get_agent_or_404(db, agent_id)

    project = get_project_or_404(db, agent.project)

    accepted_ids = {item["id"] for item in body.accepted}
    edits = {item["id"]: item.get("edited_content") for item in body.accepted}

    # Build PROGRESS.md section from accepted suggestions
    accepted_contents = []
    for item in body.accepted:
        row = db.get(AgentInsightSuggestion, item["id"])
        if not row or row.agent_id != agent_id:
            continue
        content = item.get("edited_content") or row.content
        row.edited_content = item.get("edited_content")
        row.status = "accepted"
        accepted_contents.append(content)

    # Mark rejected
    for rid in body.rejected_ids:
        row = db.get(AgentInsightSuggestion, rid)
        if row and row.agent_id == agent_id:
            row.status = "rejected"

    # Mark remaining pending as rejected too
    remaining = (
        db.query(AgentInsightSuggestion)
        .filter(
            AgentInsightSuggestion.agent_id == agent_id,
            AgentInsightSuggestion.status == "pending",
            AgentInsightSuggestion.id.notin_(accepted_ids),
        )
        .all()
    )
    for r in remaining:
        r.status = "rejected"

    # Clear flag
    agent.has_pending_suggestions = False
    db.commit()

    # Push has_pending_suggestions=False so AgentsPage / ProjectDetailPage
    # drop the Insights badge in real-time instead of waiting for the 5s poll.
    asyncio.ensure_future(emit_agent_update(agent.id, agent.status.value, agent.project))

    # Write accepted insights to PROGRESS.md
    if accepted_contents:
        from agent_dispatcher import store_insights
        today = datetime.now(timezone.utc).date().isoformat()
        progress_path = os.path.join(project.path, "PROGRESS.md")

        # Build section text
        task = db.get(Task, agent.task_id) if agent.task_id else None
        task_label = task.title if task else agent.name
        section_lines = [f"## {today} — {task_label}"]
        for i, c in enumerate(accepted_contents, 1):
            section_lines.append(f"{i}. {c}")
        new_section = "\n".join(section_lines)

        try:
            existing = ""
            if os.path.isfile(progress_path):
                with open(progress_path, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            separator = "\n\n" if existing and not existing.endswith("\n\n") else (
                "\n" if existing and not existing.endswith("\n") else "")
            with open(progress_path, "w", encoding="utf-8") as f:
                f.write(existing + separator + new_section + "\n")
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))

        # Store in FTS5
        n = store_insights(db, agent.project, today, new_section, agent_id=agent_id)
        if n:
            logger.info("Stored %d agent insights in FTS5 for %s", n, agent.project)

    return {"success": True, "accepted": len(accepted_contents)}


@router.delete("/api/agents/{agent_id}/suggestions")
async def discard_agent_suggestions(agent_id: str, db: Session = Depends(get_db)):
    """Reject all pending suggestions for an agent."""
    agent = get_agent_or_404(db, agent_id)

    db.query(AgentInsightSuggestion).filter(
        AgentInsightSuggestion.agent_id == agent_id,
        AgentInsightSuggestion.status == "pending",
    ).update({"status": "rejected"})
    agent.has_pending_suggestions = False
    db.commit()

    # Push has_pending_suggestions=False so AgentsPage / ProjectDetailPage
    # drop the Insights badge in real-time instead of waiting for the 5s poll.
    asyncio.ensure_future(emit_agent_update(agent.id, agent.status.value, agent.project))

    return {"success": True}


@router.post("/api/agents/{agent_id}/regenerate-insights")
async def regenerate_agent_insights(agent_id: str, db: Session = Depends(get_db)):
    """Re-trigger insight generation for a stopped agent (e.g. after interrupted generation)."""
    agent = get_agent_or_404(db, agent_id)
    if agent.status != AgentStatus.STOPPED:
        raise HTTPException(status_code=400, detail="Agent must be stopped")
    if agent.insight_status == "generating":
        raise HTTPException(status_code=400, detail="Already generating")
    if agent.has_pending_suggestions:
        raise HTTPException(status_code=400, detail="Suggestions already pending")

    # Need task + project path for context
    _task_title = "Unknown task"
    _project_path = None
    if agent.task_id:
        _t = db.get(Task, agent.task_id)
        if _t:
            _task_title = _t.title or "Unknown task"
    _p = db.get(Project, agent.project)
    _project_path = _p.path if _p else None
    if not _project_path:
        raise HTTPException(status_code=400, detail="Project path not found")

    agent.insight_status = "generating"
    db.commit()

    # Push insight_status=generating so other clients (AgentsPage,
    # ProjectDetailPage, other tabs) flip the pill from "failed" to
    # "generating" immediately. Local tab already refreshes via onRetry.
    asyncio.ensure_future(emit_agent_update(
        agent.id, agent.status.value, agent.project,
        insight_status="generating",
    ))

    from routers.projects import INSIGHT_EXECUTOR
    INSIGHT_EXECUTOR.submit(
        _run_agent_summary_background,
        agent.id, agent.name, _task_title, agent.project, _project_path,
    )
    logger.info("Regenerating insights for agent %s", agent.id)
    return {"success": True}


@router.delete("/api/agents/{agent_id}/permanent")
async def permanently_delete_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Permanently delete an agent, its messages, session JSONL, and output logs."""
    from session_cache import cleanup_source_session, evict_session

    agent = get_agent_or_404(db, agent_id)
    if agent.status not in (AgentStatus.STOPPED, AgentStatus.ERROR):
        raise HTTPException(status_code=400, detail="Agent must be stopped before deleting")

    # 0. Kill tmux sessions if still alive (try both new xy- and legacy ah-
    #    names; tmux_pane is cleared to None during stop, but sessions may linger)
    import subprocess as _sp
    for sess_name in tmux_session_candidates(agent.id):
        try:
            _sp.run(["tmux", "kill-session", "-t", sess_name],
                    capture_output=True, timeout=5)
            logger.info("Killed tmux session %s for permanent delete of agent %s", sess_name, agent.id)
        except (OSError, _sp.TimeoutExpired):
            logger.debug("tmux kill-session %s failed (may already be dead) for agent %s", sess_name, agent.id)

    # Cancel dispatcher tasks
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad:
        ad._cancel_sync_task(agent.id)
        ad._cancel_launch_task(agent.id)
        ad._stale_session_retries.pop(agent.id, None)
        ad._known_subagents.pop(agent.id, None)

    # 1. Collect all agents to delete (parent + subagents cascade)
    child_agents = db.query(Agent).filter(
        Agent.parent_id == agent_id,
        Agent.is_subagent == True,  # noqa: E712
    ).all()
    agents_to_delete = [agent] + child_agents

    # Collect file info before deleting DB records
    all_agent_ids = [a.id for a in agents_to_delete]
    session_infos = [(a.session_id, a.project, a.worktree) for a in agents_to_delete if a.session_id]
    msg_ids = [m.id for m in db.query(Message.id).filter(Message.agent_id.in_(all_agent_ids)).all()]

    # 2. Delete DB records FIRST (so if this fails, no files are orphaned)
    deleted_msgs = db.query(Message).filter(Message.agent_id.in_(all_agent_ids)).delete(synchronize_session=False)
    # Unlink Tasks that reference these agents (SET NULL, don't delete the tasks)
    db.query(Task).filter(Task.agent_id.in_(all_agent_ids)).update(
        {Task.agent_id: None}, synchronize_session=False
    )
    # Delete children first (FK ordering), then parent
    for child in child_agents:
        db.delete(child)
    db.delete(agent)
    db.commit()

    # 3. Delete session source files (.jsonl + subdir) and cache (safe: DB already committed)
    cleaned_files = []
    for sid, proj_name, worktree in session_infos:
        project = db.query(Project).filter(Project.name == proj_name).first()
        if project:
            if cleanup_source_session(sid, project.path, worktree):
                cleaned_files.append(f"{sid}.jsonl")
            evict_session(sid, project.path, worktree)

    # 4. Delete display files for all agents being removed
    from display_writer import delete_agent as _delete_display
    for aid in all_agent_ids:
        _delete_display(aid)
        cleaned_files.append(f"display/{aid}.jsonl")

    # 5. Delete output log files for all messages
    for mid in msg_ids:
        log_path = os.path.join(tempfile.gettempdir(), f"claude-output-{mid}.log")
        if os.path.isfile(log_path):
            try:
                os.remove(log_path)
                cleaned_files.append(log_path)
            except OSError as e:
                logger.warning("Failed to delete output log %s: %s", log_path, e)

    # 6. Delete session-history files (lifetime tracking sidecar)
    from session_history import remove_history as _remove_history, _history_file
    for aid in all_agent_ids:
        if _remove_history(aid):
            cleaned_files.append(_history_file(aid))

    logger.info("Permanently deleted agent %s (+%d subagents, %d messages, %d files cleaned)",
                agent_id, len(child_agents), deleted_msgs, len(cleaned_files))
    return {
        "detail": "ok",
        "deleted_messages": deleted_msgs,
        "deleted_subagents": len(child_agents),
        "cleaned_files": len(cleaned_files),
    }


@router.post("/api/agents/{agent_id}/resume", response_model=AgentOut)
async def resume_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Resume a stopped or errored agent."""
    agent = get_agent_or_404(db, agent_id)
    if agent.status not in (AgentStatus.STOPPED, AgentStatus.ERROR):
        raise HTTPException(status_code=400, detail="Agent is already running")

    # Block resume if this agent was superseded by a successor (not subagents)
    successor = db.query(Agent).filter(
        Agent.parent_id == agent.id,
        Agent.is_subagent == False,
    ).order_by(Agent.created_at.desc()).first()
    if successor:
        raise HTTPException(
            status_code=409,
            detail=json.dumps({
                "reason": "superseded",
                "successor_id": successor.id,
                "successor_name": successor.name,
                "message": "This agent was continued by a new agent. Open the successor instead.",
            }),
        )

    project = get_project_or_404(db, agent.project)
    if project.archived:
        raise HTTPException(status_code=400, detail="Cannot resume agents for archived projects — activate first")

    wm = getattr(request.app.state, "worker_manager", None)
    if not wm:
        raise HTTPException(status_code=500, detail="Worker manager not available")

    # Parse optional body for resume mode
    body = {}
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        pass  # Empty body or no content-type — use defaults
    resume_mode = body.get("mode")  # "tmux" | None

    wm.ensure_project_ready(project)

    # Clear stale session retry counter so resumed agents get
    # full retry budget for session recovery
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad:
        ad._stale_session_retries.pop(agent.id, None)

    # Resume implicitly discards the prior stop-cycle's insight suggestions.
    # The next stop will regenerate from the full conversation (which now
    # includes the post-resume turns), so keeping the old pending set adds
    # no value and would just clutter the agent card. Accepted/rejected rows
    # and project-level ProgressInsight history are untouched.
    # If a generation is still in flight, terminate its claude subprocess
    # and flip its cancel flag so the worker thread exits without writing.
    from routers.projects import cancel_insight_run
    cancel_insight_run(agent.id)
    db.query(AgentInsightSuggestion).filter(
        AgentInsightSuggestion.agent_id == agent.id,
        AgentInsightSuggestion.status == "pending",
    ).delete(synchronize_session=False)
    agent.has_pending_suggestions = False
    agent.insight_status = None
    if agent.task_id:
        _t = db.get(Task, agent.task_id)
        if _t:
            _t.agent_summary = None

    # Flip to STARTING and commit before the tmux work so a concurrent
    # second Resume click hits the precheck (status not in STOPPED/ERROR)
    # and is rejected with 400 — instead of racing into
    # _create_tmux_claude_session and killing the first call's pane.
    # Frontend's Resume/Stop button toggles off STARTING as well.
    agent.status = AgentStatus.STARTING
    db.commit()
    db.refresh(agent)
    # Pass insight_status="" so other clients clear any stale "failed"/
    # "generating" state in their store (has_pending_suggestions is
    # auto-fetched by emit_agent_update, but insight_status is not).
    asyncio.ensure_future(
        emit_agent_update(agent.id, agent.status.value, agent.project,
                          insight_status="")
    )

    resumed_sync = False

    try:
        if resume_mode == "tmux":
            # Launch a new tmux session and resume the CLI session in it
            import shlex
            import subprocess
            from config import CLAUDE_BIN

            cmd_parts = [CLAUDE_BIN,
                          "--output-format", "stream-json", "--verbose"]
            if agent.skip_permissions:
                cmd_parts.append("--dangerously-skip-permissions")
            if agent.model:
                cmd_parts += ["--model", _model_for_cli(agent.model)]
            if agent.worktree:
                cmd_parts += ["--worktree", agent.worktree]
            if agent.session_id:
                cmd_parts += ["--resume", agent.session_id]
            claude_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

            tmux_session = tmux_session_name(agent.id)
            _preflight_claude_project(project.path)

            # Worktree agents: tmux cwd must be the worktree path so that
            # Claude encodes its session dir as `...<worktree>` — otherwise
            # `claude --resume <sid>` looks in the project-root-encoded dir
            # and can't find the JSONL, exits to bash, and subsequent
            # messages get typed into the shell instead of the CLI.
            launch_cwd = project.path
            if agent.worktree:
                wt_path = os.path.join(
                    project.path, ".claude", "worktrees", agent.worktree,
                )
                if os.path.isdir(wt_path):
                    launch_cwd = wt_path

            pane_id = await asyncio.to_thread(
                _create_tmux_claude_session,
                tmux_session, launch_cwd, claude_cmd, agent.id,
            )

            agent.tmux_pane = pane_id
            agent.status = AgentStatus.IDLE
            if agent.session_id and ad:
                ad.start_session_sync(agent.id, agent.session_id, project.path)
            resumed_sync = True
        elif ad:
            # Default: try to re-establish sync with existing tmux pane
            from agent_dispatcher import _detect_tmux_pane_for_session, _resolve_session_jsonl
            from session_cache import session_source_dir

            sid = agent.session_id

            # If session_id was never assigned (e.g. tmux launch failed
            # before detecting the JSONL), discover it from the project's
            # session directory by picking the most recently modified file.
            # Check both project root and worktree session dirs.
            if not sid:
                sdirs = [session_source_dir(project.path)]
                if agent.worktree:
                    wt_path = os.path.join(project.path, ".claude", "worktrees", agent.worktree)
                    wt_sdir = session_source_dir(wt_path)
                    if os.path.isdir(wt_sdir) and wt_sdir not in sdirs:
                        sdirs.append(wt_sdir)
                best, best_mtime = None, 0.0
                for sdir in sdirs:
                    if not os.path.isdir(sdir):
                        continue
                    try:
                        for fname in os.listdir(sdir):
                            if not fname.endswith(".jsonl"):
                                continue
                            fpath = os.path.join(sdir, fname)
                            mt = os.path.getmtime(fpath)
                            if mt > best_mtime:
                                best, best_mtime = fname.replace(".jsonl", ""), mt
                    except OSError as e:
                        logger.warning(
                            "resume_agent: failed to scan session dir %s for agent %s: %s",
                            sdir, agent.id, e,
                        )
                if best:
                    sid = best
                    agent.session_id = sid
                    logger.info(
                        "Discovered session %s for agent %s on resume",
                        sid, agent.id,
                    )

            if sid:
                jsonl_path = _resolve_session_jsonl(sid, project.path, agent.worktree)
                if os.path.exists(jsonl_path) and not ad._session_has_ended(jsonl_path):
                    pane = _detect_tmux_pane_for_session(sid, project.path)
                    agent.status = AgentStatus.IDLE
                    agent.tmux_pane = pane  # may be None; sync loop will retry
                    ad.start_session_sync(agent.id, sid, project.path)
                    resumed_sync = True
    except Exception:
        logger.exception("resume_agent failed for agent %s (mode=%s)", agent_id, resume_mode)
        # Roll status back so the UI shows Resume again instead of
        # being stuck in STARTING. Use a fresh commit independent of any
        # partial state left on the session.
        db.rollback()
        agent = db.get(Agent, agent_id)
        if agent is not None:
            agent.status = AgentStatus.ERROR
            db.commit()
            asyncio.ensure_future(
                emit_agent_update(agent.id, agent.status.value, agent.project)
            )
        raise

    # If neither resume path worked (no session, no jsonl), leave status
    # as STARTING. Router-owned STARTING / IDLE / ERROR transitions are
    # explicit per Rule 2; no silent fallback IDLE write.

    msg = Message(
        agent_id=agent.id,
        role=MessageRole.SYSTEM,
        content="Agent resumed" + (" — syncing CLI session" if resumed_sync else ""),
        status=MessageStatus.COMPLETED,
        delivered_at=_utcnow(),
    )
    db.add(msg)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Session already owned by another agent",
        )
    db.refresh(agent)
    # Flush the "Agent resumed" sys bubble to display file. flush_agent
    # auto-emits the WS new_message signal so the chat updates immediately
    # — without this the bubble waits for the next sync cycle to surface.
    from display_writer import flush_agent as _resume_flush
    _resume_flush(agent.id)
    asyncio.ensure_future(emit_agent_update(agent.id, agent.status.value, agent.project))
    logger.info("Agent %s resumed (sync=%s, mode=%s)", agent.id, resumed_sync, resume_mode)
    return agent


@router.put("/api/agents/read-all")
async def mark_all_agents_read(db: Session = Depends(get_db)):
    """Mark all agents as read (reset unread count for every agent)."""
    count = db.query(Agent).filter(Agent.unread_count > 0).update({"unread_count": 0})
    db.commit()
    return {"detail": "ok", "updated": count}


@router.put("/api/agents/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Update agent properties (currently: name)."""
    agent = get_agent_or_404(db, agent_id)
    body = await request.json()
    if "name" in body:
        name = str(body["name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        if len(name) > 200:
            raise HTTPException(status_code=400, detail="Name too long (max 200)")
        agent.name = name
    if "muted" in body:
        agent.muted = bool(body["muted"])
    if "deferred_to" in body:
        v = body["deferred_to"]
        if v is None or v == "":
            agent.deferred_to = None
        else:
            try:
                agent.deferred_to = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="deferred_to must be ISO datetime or null")
    db.commit()
    db.refresh(agent)
    # Broadcast to other devices so they update their list grouping
    # (deferred_to → Deferred section), mute icon, and name without waiting
    # for the 5s poll. emit_agent_update fetches the row from DB so the
    # payload reflects the just-committed state.
    asyncio.ensure_future(emit_agent_update(agent.id, agent.status.value, agent.project))
    return agent


@router.get("/api/agents/{agent_id}/display/sent", response_model=SentDisplayResponse)
async def get_agent_display_sent(
    agent_id: str,
    offset: int = Query(0, ge=0),
    tail_bytes: int = Query(0, ge=0),
    focus_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Read sent (delivered) messages from the per-agent JSONL file.

    Three modes:
      - Initial load: tail_bytes > 0 and offset == 0 → read the last
        tail_bytes of the file, aligned to a line boundary.
      - Incremental poll: offset > 0 → read from offset to end of file.
      - Focus-centered slice: focus_id supplied → locate the line whose
        id matches focus_id, return ~tail_bytes/2 before and after it
        (line-aligned). Falls back to tail mode if the id isn't found.
        next_offset still points at the read window's right edge so a
        subsequent incremental poll keeps working unchanged.

    Returns only entries with `seq != null` (sent messages). Pre-sent
    entries (queued / scheduled / cancelled) are exposed by the separate
    /display/pre-sent endpoint.

    Last-occurrence-wins by id handles `_replace` lines from streaming
    updates and delivery-status patches; `_deleted` tombstones drop the
    entry entirely.
    """
    agent = get_agent_or_404(db, agent_id)

    display_path = os.path.join(DISPLAY_DIR, f"{agent_id}.jsonl")
    empty = SentDisplayResponse(messages=[], next_offset=0, has_earlier=False, has_later=False)

    if not os.path.isfile(display_path):
        return empty

    is_focus = bool(focus_id) and tail_bytes > 0 and offset == 0
    is_initial = (not is_focus) and tail_bytes > 0 and offset == 0
    has_earlier = False
    has_later = False
    try:
        with open(display_path, "r", encoding="utf-8") as f:
            file_size = f.seek(0, 2)

            if is_focus:
                # Locate the byte offset of the line containing focus_id.
                # Cheap substring scan: a quoted id is unique enough in the
                # JSONL stream (ids are 12-hex tokens, no false-positive risk
                # at the call sites that produce display lines).
                # Match both compact and pretty JSON spacing variants of the
                # id field (json.dump default vs json.dumps default differ).
                f.seek(0)
                blob = f.read()
                anchor = -1
                for needle in (f'"id":"{focus_id}"', f'"id": "{focus_id}"'):
                    anchor = blob.find(needle)
                    if anchor >= 0:
                        break
                if anchor < 0:
                    # Fallback: behave like a regular tail load when the id
                    # is not present in this file (deleted, wrong agent, etc.).
                    is_focus = False
                    is_initial = True
                else:
                    half = max(1024, tail_bytes // 2)
                    start = max(0, anchor - half)
                    end = min(file_size, anchor + half)
                    # Snap to line boundaries: drop a partial leading line, and
                    # extend the tail to include the rest of the trailing line.
                    if start > 0:
                        nl = blob.find("\n", start)
                        start = (nl + 1) if nl >= 0 else file_size
                    if end < file_size:
                        nl = blob.find("\n", end)
                        end = (nl + 1) if nl >= 0 else file_size
                    raw = blob[start:end]
                    next_offset = end
                    has_earlier = start > 0
                    has_later = end < file_size

            if is_initial:
                start = max(0, file_size - tail_bytes)
                f.seek(start)
                if start > 0:
                    f.readline()  # discard partial line
                has_earlier = f.tell() > 0 and start > 0
                raw = f.read()
                next_offset = f.tell()
            elif not is_focus and offset > 0:
                f.seek(min(offset, file_size))
                has_earlier = True
                raw = f.read()
                next_offset = f.tell()
            elif not is_focus and not is_initial:
                f.seek(0)
                raw = f.read()
                next_offset = f.tell()
    except OSError:
        return empty

    seen: dict[str, DisplayEntry] = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipped malformed JSON in display file for agent %s", agent_id)
            continue
        try:
            entry = DisplayEntry.model_validate(obj)
        except Exception as e:
            logger.warning("Failed to parse display entry: %s", e)
            continue
        seen[entry.id] = entry

    # Sent-only partition: drop tombstones, drop pre-sent (_queued), keep
    # entries with a seq. Sort by seq — file insertion order is not
    # guaranteed to match seq order (e.g. retry_marker seq=0 appended to
    # the end of an existing file by backfill).
    displayed: list[DisplayEntry] = []
    for entry in seen.values():
        if entry.deleted or entry.queued:
            continue
        if entry.seq is not None:
            displayed.append(entry)
    displayed.sort(key=lambda e: e.seq)

    return SentDisplayResponse(
        messages=displayed,
        next_offset=next_offset,
        has_earlier=has_earlier,
        has_later=has_later,
    )


@router.get("/api/agents/{agent_id}/display/pre-sent", response_model=PreSentDisplayResponse)
async def get_agent_display_pre_sent(
    agent_id: str,
    db: Session = Depends(get_db),
):
    """Return the full pre-sent snapshot from the in-memory index.

    Pre-sent entries (status=queued/scheduled/cancelled) are a small,
    state-mutating set — full snapshot every call is cheap and avoids
    the increment-vs-snapshot semantic confusion that gave us the
    "queued bubble disappears" bug.
    """
    agent = get_agent_or_404(db, agent_id)

    from display_writer import pre_sent_list
    entries: list[DisplayEntry] = []
    for raw_entry in pre_sent_list(agent_id):
        try:
            entries.append(DisplayEntry.model_validate(raw_entry))
        except Exception as e:
            logger.warning(
                "get_agent_display_pre_sent: failed to validate entry id=%s: %s",
                raw_entry.get("id"), e,
            )
    return PreSentDisplayResponse(entries=entries)


@router.post("/api/agents/{agent_id}/wake-sync")
async def wake_agent_sync(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Wake the sync loop for an agent to trigger immediate JSONL import.

    For ERROR agents, recovers to IDLE first so the sync loop doesn't
    immediately exit.  This is the only path that does ERROR→IDLE —
    hooks calling wake_sync() won't auto-recover.
    """
    agent = get_agent_or_404(db, agent_id)
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if not ad:
        raise HTTPException(status_code=503, detail="Dispatcher not ready")
    # Explicit user action: recover ERROR agents before waking sync
    if agent.status == AgentStatus.ERROR:
        agent.status = AgentStatus.IDLE
        agent.error_message = None
        db.commit()
        from websocket import emit_agent_update
        ad._emit(emit_agent_update(agent.id, "IDLE", agent.project))
        logger.info("wake-sync: recovered agent %s from ERROR → IDLE", agent_id[:8])
    # Dispatch any queued pre-sent messages if agent is idle
    asyncio.ensure_future(ad.dispatch_pending_message(agent_id, delay=0))
    if ad.wake_sync(agent_id):
        return {"status": "ok", "detail": "Sync woken"}
    raise HTTPException(status_code=409, detail="No active sync loop for this agent")


def _allocate_message_id() -> str:
    """Allocate a fresh 12-hex message id (same pattern as Message._new_uuid)."""
    import uuid as _uuid
    return _uuid.uuid4().hex[:12]


def _synthetic_message_out(agent_id: str, entry: dict) -> MessageOut:
    """Build a MessageOut from a pre-sent entry dict (no DB row).

    Maps pre-sent status ('queued' | 'scheduled') to the corresponding
    legacy MessageStatus enum value so existing frontend code that still
    reads uppercase strings continues to work during Phase 2 transition.
    """
    # Pre-sent entries carry lowercase status strings ("queued", "scheduled",
    # "cancelled", "sent", "delivered", "executed"). MessageOut.status is
    # `str | None`, so we pass through directly — frontend handles the
    # lowercase/uppercase mix (display file vs DB-backed messages).
    status = entry.get("status") or "queued"

    # created_at / scheduled_at may be strings (ISO) from the entry dict;
    # the MessageOut validator normalizes tzinfo but we need to convert
    # strings to datetimes first so Pydantic accepts them.
    def _parse_ts(v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    payload = {
        "id": entry["id"],
        "agent_id": agent_id,
        "role": MessageRole.USER,
        "content": entry.get("content", ""),
        "status": status,
        "source": entry.get("source"),
        "metadata": entry.get("metadata"),
        "created_at": _parse_ts(entry.get("created_at")) or datetime.now(timezone.utc),
        "scheduled_at": _parse_ts(entry.get("scheduled_at")),
        "delivered_at": None,
        "completed_at": None,
        "tool_use_id": None,
    }
    return MessageOut.model_validate(payload)


@router.post("/api/agents/{agent_id}/messages", response_model=MessageOut, status_code=201)
async def send_agent_message(
    agent_id: str,
    body: SendMessage,
    request: Request,
    db: Session = Depends(get_db),
):
    """Send a follow-up message to an agent.

    Pre-sent refactor (Phase 2): this endpoint writes a pre-sent
    entry to the per-agent display file via `display_writer.pre_sent_*`
    and returns a synthetic MessageOut. No DB row is created here — the
    dispatcher creates the DB row at the moment of tmux send (promoting
    the pre-sent entry to sent).
    """
    import slash_commands
    if slash_commands.is_slash_command(body.content) and not slash_commands.is_allowed(body.content):
        raise HTTPException(status_code=400, detail=slash_commands.rejection_message(body.content))

    agent = get_agent_or_404(db, agent_id)
    if agent.status == AgentStatus.STOPPED:
        raise HTTPException(status_code=400, detail="Agent is stopped")

    # --- Scheduled messages: store as pre-sent 'scheduled' entry ---
    scheduled_at = None
    if body.scheduled_at:
        try:
            scheduled_at = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid scheduled_at format")

    # Verify tmux pane (no-send: we just don't want a stale pane reference).
    # Note: unlike pre-refactor behavior, we do NOT call send_tmux_message
    # directly from this endpoint — that's the dispatcher's job now.
    has_tmux = (
        agent.status in (AgentStatus.IDLE, AgentStatus.STARTING, AgentStatus.EXECUTING)
        and agent.tmux_pane
        and not scheduled_at
    )
    if has_tmux:
        from agent_dispatcher import _detect_tmux_pane_for_session, verify_tmux_pane
        if not verify_tmux_pane(agent.tmux_pane):
            recovered_pane = None
            if agent.session_id:
                project = db.get(Project, agent.project)
                if project:
                    candidate = _detect_tmux_pane_for_session(agent.session_id, project.path)
                    if candidate and verify_tmux_pane(candidate):
                        recovered_pane = candidate
            if recovered_pane:
                agent.tmux_pane = recovered_pane
                db.commit()
            else:
                ad_msg = getattr(request.app.state, "agent_dispatcher", None)
                if ad_msg:
                    ad_msg._clear_agent_pane(db, agent, kill_tmux=False)
                else:
                    agent.tmux_pane = None
                db.commit()
                has_tmux = False

    # Build the pre-sent entry.
    project = db.get(Project, agent.project)
    if not project:
        raise HTTPException(status_code=400, detail="Project not found")

    status = "scheduled" if scheduled_at else "queued"
    msg_id = _allocate_message_id()

    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad:
        entry, _prompt, insights_list = await ad._prepare_pre_sent_entry(
            db, agent, project, body.content,
            source="web",
            status=status,
            scheduled_at=scheduled_at,
            wrap_prompt=False,
            msg_id=msg_id,
        )
        # Agent preview mutations were applied to the ORM object; persist them.
        db.commit()
    else:
        # Minimal fallback: no dispatcher available (shouldn't happen in prod).
        entry = {
            "id": msg_id,
            "role": "USER",
            "content": body.content,
            "source": "web",
            "status": status,
            "created_at": _utcnow().isoformat(),
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
            "metadata": None,
        }

    # Write the pre-sent entry + emit WS + kick dispatcher.
    from display_writer import pre_sent_create
    from websocket import emit_pre_sent_created
    pre_sent_create(agent.id, entry)
    asyncio.ensure_future(emit_pre_sent_created(agent.id, entry["id"]))

    if ad and not scheduled_at:
        # IDLE / BUSY: dispatcher decides whether to send immediately or
        # defer based on agent state. Scheduled sends are picked up by
        # the periodic _dispatch_tmux_scheduled loop, not this call.
        asyncio.ensure_future(ad.dispatch_pending_message(agent.id, delay=0))

    logger.info(
        "Message %s pre-sent entry created for agent %s (status=%s)",
        msg_id, agent.id, status,
    )
    return _synthetic_message_out(agent.id, entry)



@router.put("/api/agents/{agent_id}/read")
async def mark_agent_read(agent_id: str, db: Session = Depends(get_db)):
    """Mark agent as read (reset unread count)."""
    agent = get_agent_or_404(db, agent_id)
    agent.unread_count = 0
    db.commit()
    asyncio.ensure_future(emit_agent_update(agent.id, agent.status.value, agent.project))
    return {"detail": "ok"}


@router.delete("/api/agents/{agent_id}/messages/{message_id}")
async def delete_message(agent_id: str, message_id: str, db: Session = Depends(get_db)):
    """Hard-delete a pre-sent message: bubble disappears.

    Accepts any pre-sent state (queued / scheduled / cancelled).
    Storage layer requires `cancelled` before tombstone, so for
    queued/scheduled entries this internally walks cancel→tombstone.
    Sent / delivered / executed messages cannot be deleted via this
    endpoint.

    Distinct from `POST .../messages/{id}/cancel` which only soft-cancels
    (used by ESC to grey-out queued backlog without removing it).
    """
    agent = get_agent_or_404(db, agent_id)

    from display_writer import (
        pre_sent_cancel,
        pre_sent_get,
        pre_sent_tombstone,
    )
    from websocket import emit_pre_sent_tombstoned

    entry = pre_sent_get(agent_id, message_id)
    if entry is None:
        db_msg = db.get(Message, message_id)
        if db_msg and db_msg.agent_id == agent_id:
            raise HTTPException(
                status_code=400,
                detail="Only pre-sent messages can be deleted",
            )
        raise HTTPException(status_code=404, detail="Message not found")

    status = entry.get("status")
    if status not in ("queued", "scheduled", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail="Only pre-sent messages can be deleted",
        )

    if status in ("queued", "scheduled"):
        pre_sent_cancel(agent_id, message_id)
    pre_sent_tombstone(agent_id, message_id)
    asyncio.ensure_future(emit_pre_sent_tombstoned(agent_id, message_id))
    logger.info("Message %s tombstoned for agent %s", message_id, agent_id)
    return {"detail": "deleted"}


@router.post("/api/agents/{agent_id}/messages/{message_id}/cancel")
async def cancel_message(agent_id: str, message_id: str, db: Session = Depends(get_db)):
    """Soft-cancel a queued/scheduled pre-sent message: bubble stays
    visible (greyed) so the user can see what was bailed out of.

    Used by the ESC button to clear queued backlog without making the
    bubbles disappear — distinct from the DELETE endpoint which removes
    the bubble entirely.
    """
    agent = get_agent_or_404(db, agent_id)

    from display_writer import pre_sent_cancel, pre_sent_get
    from websocket import emit_pre_sent_updated

    entry = pre_sent_get(agent_id, message_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Message not found")

    status = entry.get("status")
    if status not in ("queued", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Only queued/scheduled messages can be cancelled",
        )

    pre_sent_cancel(agent_id, message_id)
    asyncio.ensure_future(
        emit_pre_sent_updated(agent_id, message_id)
    )
    logger.info("Message %s cancelled (soft) for agent %s", message_id, agent_id)
    return {"detail": "cancelled"}


@router.put("/api/agents/{agent_id}/messages/{message_id}", response_model=MessageOut)
async def update_message(
    agent_id: str,
    message_id: str,
    body: UpdateMessage,
    db: Session = Depends(get_db),
):
    """Update content and/or scheduled_at of a queued/scheduled pre-sent message."""
    agent = get_agent_or_404(db, agent_id)

    from display_writer import pre_sent_get, pre_sent_update
    from websocket import emit_pre_sent_updated

    entry = pre_sent_get(agent_id, message_id)
    if entry is None or entry.get("status") not in ("queued", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Only queued/scheduled messages can be edited",
        )

    patch: dict = {}
    if body.content is not None:
        content = body.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="Content cannot be empty")
        patch["content"] = content

    if body.scheduled_at is not None:
        if body.scheduled_at == "":
            patch["scheduled_at"] = None
            # Clearing schedule converts a scheduled entry back into queued.
            if entry.get("status") == "scheduled":
                patch["status"] = "queued"
        else:
            try:
                dt = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid scheduled_at format")
            patch["scheduled_at"] = dt.isoformat()
            if entry.get("status") == "queued":
                patch["status"] = "scheduled"

    if patch:
        pre_sent_update(agent_id, message_id, patch)
        asyncio.ensure_future(
            emit_pre_sent_updated(agent_id, message_id)
        )

    # Build the synthetic MessageOut from the updated entry.
    updated = pre_sent_get(agent_id, message_id) or entry
    logger.info("Message %s updated for agent %s", message_id, agent_id)
    return _synthetic_message_out(agent_id, updated)


_CC_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@router.post(
    "/api/agents/{agent_id}/messages/{message_id}/diverge",
    response_model=AgentOut,
)
async def diverge_message(
    agent_id: str,
    message_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Fork the conversation at a message into a new agent.

    Copies the source CC session JSONL up to the chosen message — inclusive
    for AGENT messages (the reply stays, the new direction starts after it),
    exclusive for USER messages (edit-and-resend semantics) — rewrites the
    session id on every line, and launches a fresh tmux agent with
    `claude --resume <new_sid>`. Full fidelity: tool inputs/outputs and
    file-state context replay from the original transcript.

    Body: {"purpose": str | null}. A new Task is created when a purpose is
    given or the source agent is task-bound; its panel attributes (model,
    effort, worktree, priority, ...) inherit from the source task/agent.
    """
    import shlex
    import uuid as _uuid

    from agent_dispatcher import _write_session_owner
    from config import CLAUDE_BIN
    from models import CCSession
    from session_cache import session_source_dir
    from session_fork import fork_session_lines

    agent = get_agent_or_404(db, agent_id)
    project = get_project_or_404(db, agent.project)
    if project.archived:
        raise HTTPException(
            status_code=400,
            detail="Cannot diverge agents in archived projects — activate first",
        )

    msg = db.get(Message, message_id)
    if not msg or msg.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.role not in (MessageRole.USER, MessageRole.AGENT):
        raise HTTPException(
            status_code=400, detail="Only user/agent messages can be diverged",
        )
    # Synthetic anchors (tool-*, interactive-*, sys-*) don't map to a JSONL
    # entry uuid; NULL means the message never echoed through the JSONL.
    if not msg.jsonl_uuid or not _CC_UUID_RE.match(msg.jsonl_uuid):
        raise HTTPException(
            status_code=400,
            detail="This message has no session anchor — cannot diverge here",
        )

    ad = getattr(request.app.state, "agent_dispatcher", None)
    if not ad:
        raise HTTPException(status_code=503, detail="Agent dispatcher not ready")

    _check_project_capacity(db, agent.project)

    body = {}
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        pass
    purpose = (body.get("purpose") or "").strip() if isinstance(body, dict) else ""

    # Candidate session dirs, paired with the cwd claude must launch in so
    # `--resume` encodes the same project dir (worktree sessions live in the
    # worktree-encoded dir — see resume_agent's launch_cwd handling).
    candidates = [(session_source_dir(os.path.realpath(project.path)), project.path)]
    if agent.worktree:
        wt_path = os.path.join(project.path, ".claude", "worktrees", agent.worktree)
        if os.path.isdir(wt_path):
            candidates.append(
                (session_source_dir(os.path.realpath(wt_path)), wt_path)
            )

    # Current session first, then rotated-out predecessors (pre-compact /
    # pre-clear transcripts) — each JSONL is independently resumable.
    sids = [agent.session_id] if agent.session_id else []
    history = (
        db.query(CCSession)
        .filter(
            CCSession.agent_id == agent_id,
            CCSession.is_subagent_session == False,  # noqa: E712
        )
        .order_by(CCSession.started_at.desc())
        .all()
    )
    sids += [r.session_id for r in history if r.session_id not in sids]

    include_target = msg.role == MessageRole.AGENT
    new_sid = str(_uuid.uuid4())
    forked = None
    launch_cwd = session_dir = src_path = None
    for sid in sids:
        for sdir, cwd in candidates:
            path = os.path.join(sdir, f"{sid}.jsonl")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    lines = [ln.rstrip("\n") for ln in f]
            except OSError as e:
                logger.warning("diverge: failed to read %s: %s", path, e)
                continue
            forked = fork_session_lines(lines, msg.jsonl_uuid, include_target, new_sid)
            if forked is not None:
                launch_cwd, session_dir, src_path = cwd, sdir, path
                break
        if forked is not None:
            break
    if forked is None:
        raise HTTPException(
            status_code=404,
            detail="Could not locate this message in any session file — cannot diverge",
        )

    new_jsonl = os.path.join(session_dir, f"{new_sid}.jsonl")
    try:
        with open(new_jsonl, "w", encoding="utf-8") as f:
            f.write("\n".join(forked) + "\n")
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to write forked session file: {e}",
        )

    agent_hex = _allocate_agent_id(db)

    # Pre-write .owner so the SessionStart hook binds the resumed sid to the
    # new agent (mirrors launch_tmux_agent / convert-and-adopt).
    try:
        _write_session_owner(session_dir, new_sid, agent_hex)
    except OSError as e:
        logger.warning("diverge: pre-write .owner failed: %s", e)

    cmd_parts = [CLAUDE_BIN, "--output-format", "stream-json", "--verbose"]
    if agent.skip_permissions:
        cmd_parts.append("--dangerously-skip-permissions")
    if agent.model:
        cmd_parts += ["--model", _model_for_cli(agent.model)]
    if agent.effort:
        cmd_parts += ["--effort", agent.effort]
    if agent.worktree:
        cmd_parts += ["--worktree", agent.worktree]
    cmd_parts += ["--resume", new_sid]
    claude_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

    _preflight_claude_project(project.path)
    tmux_name = tmux_session_name(agent_hex)
    try:
        pane_id = await asyncio.to_thread(
            _create_tmux_claude_session,
            tmux_name, launch_cwd, claude_cmd, agent_hex,
        )
    except Exception as e:
        logger.exception("diverge: tmux launch failed")
        _safe_unlink(new_jsonl)
        raise HTTPException(status_code=500, detail=f"tmux launch failed: {e}")

    try:
        new_agent = Agent(
            id=agent_hex,
            project=agent.project,
            name=(purpose or f"Diverge: {agent.name}")[:200],
            mode=agent.mode,
            status=AgentStatus.IDLE,
            model=agent.model,
            effort=agent.effort,
            worktree=agent.worktree,
            skip_permissions=agent.skip_permissions,
            timeout_seconds=agent.timeout_seconds,
            cli_sync=True,
            session_id=new_sid,
            tmux_pane=pane_id,
            last_message_preview="Diverged conversation",
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(new_agent)
        # Agent row must be flushed before the Task references it (agent_id
        # FK); task_id is backfilled after — same circular-FK dance as
        # launch_tmux_agent.
        db.flush()

        # Task: created when a purpose was given or the source is task-bound.
        # Panel attributes inherit from the source task (fallback: source agent).
        src_task = db.get(Task, agent.task_id) if agent.task_id else None
        new_task = None
        if purpose or src_task:
            title = purpose or f"Diverge: {src_task.title if src_task and src_task.title else agent.name}"
            new_task = Task(
                title=title[:300],
                description=(
                    f"Diverged from agent {agent.id} ({agent.name}) "
                    f"at message {message_id}."
                ),
                project_name=agent.project,
                priority=src_task.priority if src_task else 0,
                status=TaskStatus.EXECUTING,
                agent_id=agent_hex,
                worktree_name=src_task.worktree_name if src_task else agent.worktree,
                branch_name=src_task.branch_name if src_task else None,
                model=(src_task.model if src_task else None) or agent.model,
                effort=(src_task.effort if src_task else None) or agent.effort,
                skip_permissions=agent.skip_permissions,
                use_worktree=src_task.use_worktree if src_task else bool(agent.worktree),
                use_tmux=True,
                timeout_seconds=src_task.timeout_seconds if src_task else agent.timeout_seconds,
                started_at=_utcnow(),
            )
            db.add(new_task)
            db.flush()
            new_agent.task_id = new_task.id

        db.commit()
    except Exception:
        # Don't leave an orphan claude pane + forked JSONL behind.
        db.rollback()
        logger.exception("diverge: DB registration failed, cleaning up")
        _graceful_kill_tmux(pane_id, tmux_name)
        _safe_unlink(new_jsonl)
        _safe_unlink(os.path.join(session_dir, f"{new_sid}.owner"))
        raise HTTPException(
            status_code=500, detail="Failed to register diverged agent",
        )
    db.refresh(new_agent)

    # Import the truncated transcript synchronously so the diverged chat
    # shows the inherited history immediately. The SyncContext is created
    # inside the loop task, so poll briefly until the drain can run.
    ad.start_session_sync(agent_hex, new_sid, project.path, cwd=launch_cwd)
    drained = False
    for _ in range(30):
        try:
            if await ad._drain_session_sync(agent_hex):
                drained = True
                break
        except Exception:
            logger.exception("diverge: history drain failed for %s", agent_hex)
            break
        await asyncio.sleep(0.1)
    if not drained:
        logger.warning("diverge: initial drain did not run for agent %s", agent_hex)
    # No status write here: the forked transcript ends with an interrupt
    # marker (session_fork), so the status deriver lands on IDLE from JSONL
    # truth alone — same single path as a live ESC.
    db.refresh(new_agent)

    logger.info(
        "diverge: agent %s msg %s (%s, include=%s) → agent %s sid %s "
        "(%d lines from %s, task=%s)",
        agent_id, message_id, msg.role.value, include_target, agent_hex,
        new_sid[:12], len(forked), src_path, new_task.id if new_task else "none",
    )
    asyncio.ensure_future(
        emit_agent_update(new_agent.id, new_agent.status.value, new_agent.project)
    )
    if new_task:
        asyncio.ensure_future(emit_task_update(
            new_task.id, new_task.status.value, new_task.project_name or "",
            title=new_task.title,
        ))
    return AgentOut.model_validate(new_agent)


# ---- Interactive Answer (AskUserQuestion / ExitPlanMode via tmux) ----

class AnswerPayload(BaseModel):
    tool_use_id: str
    type: str  # "ask_user_question", "exit_plan_mode", or "permission_prompt"
    selected_index: int | None = None  # 0-based option index (AskUserQuestion)
    question_index: int = 0  # which question in multi-Q AskUserQuestion
    approved: bool | None = None  # (ExitPlanMode only)


_PLAN_LABELS = [
    "Yes, bypass permissions",
    "Yes, manual approval",
    "Give feedback",
]


def _patch_interactive_answer(
    db: Session, agent_id: str, tool_use_id: str,
    selected_index: int, answer_type: str,
    question_index: int = 0,
) -> bool:
    """Immediately mark an interactive item as answered in the DB.

    Builds an answer string from the selected option so the frontend can
    render the selection without waiting for the sync loop to pick up the
    tool_result from the session JSONL.

    For multi-question AskUserQuestion, each call patches one question at a
    time via question_index, accumulating into selected_indices and answer.
    """
    msgs = db.query(Message).filter(
        Message.agent_id == agent_id,
        Message.tool_use_id == tool_use_id,
    ).order_by(Message.created_at.desc()).all()

    for msg in msgs:
        try:
            meta = json.loads(msg.meta_json)
        except (json.JSONDecodeError, TypeError):
            logger.debug("Skipped unparseable meta_json in _patch_interactive_answer, msg_id=%s", msg.id)
            continue
        items = meta.get("interactive")
        if not items:
            continue
        for item in items:
            if item.get("tool_use_id") != tool_use_id:
                continue
            # Don't overwrite a dismissed/rejected answer
            existing_answer = item.get("answer") or ""
            if isinstance(existing_answer, str) and (
                existing_answer.startswith("The user doesn't want to proceed")
                or existing_answer.startswith("User declined")
                or existing_answer.startswith("Tool use rejected")
            ):
                return
            if answer_type == "ask_user_question":
                # Per-question check: skip if this specific question already answered
                sel_indices = item.get("selected_indices", {})
                if sel_indices.get(str(question_index)) is not None:
                    return
                # Store per-question index
                sel_indices[str(question_index)] = selected_index
                item["selected_indices"] = sel_indices
                # Backward compat: also set selected_index for Q0
                if question_index == 0:
                    item["selected_index"] = selected_index
                # Build answer string for this question
                questions = item.get("questions", [])
                if questions and question_index < len(questions):
                    q = questions[question_index]
                    options = q.get("options", [])
                    label = options[selected_index]["label"] if selected_index < len(options) else str(selected_index)
                    part = f'"{q.get("question", "")}"="{label}"'
                else:
                    part = str(selected_index)
                # Append to existing answer (multi-question accumulation)
                existing = item.get("answer")
                if existing and isinstance(existing, str):
                    item["answer"] = existing + "\n" + part
                else:
                    item["answer"] = part
            elif answer_type == "permission_prompt":
                if item.get("answer") is not None:
                    return  # Already answered
                questions = item.get("questions", [])
                if questions:
                    options = questions[0].get("options", [])
                    label = options[selected_index]["label"] if selected_index < len(options) else str(selected_index)
                else:
                    label = str(selected_index)
                item["selected_index"] = selected_index
                item["answer"] = label
            elif answer_type == "exit_plan_mode":
                if item.get("answer") is not None:
                    return  # Already answered
                item["selected_index"] = selected_index
                item["answer"] = _PLAN_LABELS[selected_index] if selected_index < len(_PLAN_LABELS) else str(selected_index)
            msg.meta_json = json.dumps(meta)
            db.commit()
            # Update display file so the answer persists across page refreshes.
            # Branch on display_seq: pre-sent cards land in the queued
            # partition, post-delivery in the main partition.
            from display_writer import update_after_metadata_change as _update_ia
            _update_ia(agent_id, msg.id)
            return {"message_id": msg.id, "metadata": meta}

    logger.debug(
        "No interactive item found for tool_use_id=%s agent=%s (type=%s)",
        tool_use_id, agent_id, answer_type,
    )
    return None


_DISMISS_ANSWER = "The user doesn't want to proceed with this tool call"


def _dismiss_pending_interactive_cards(db: Session, agent_id: str) -> list[dict]:
    """Mark all unanswered interactive items as dismissed for an agent.

    Returns list of {"message_id": ..., "metadata": ...} for each patched message,
    used by callers to emit websocket updates.
    """
    msgs = db.query(Message).filter(
        Message.agent_id == agent_id,
        Message.role == MessageRole.AGENT,
        Message.meta_json.is_not(None),
    ).order_by(Message.created_at.desc()).limit(10).all()

    patched = []
    for msg in msgs:
        try:
            meta = json.loads(msg.meta_json)
        except (json.JSONDecodeError, TypeError):
            continue
        items = meta.get("interactive")
        if not items:
            continue
        changed = False
        for item in items:
            if item.get("auto_approved"):
                continue
            if item.get("answer") is not None:
                continue
            if item.get("selected_index") is not None:
                continue
            item["answer"] = _DISMISS_ANSWER
            changed = True
        if changed:
            msg.meta_json = json.dumps(meta)
            patched.append({"message_id": msg.id, "metadata": meta})

    if patched:
        db.commit()
        # Dismissals are usually post-delivery (the card was visible to the
        # user), but rare pre-sent dismissals exist — branch per-message.
        from display_writer import update_after_metadata_change as _update_dismiss
        for p in patched:
            _update_dismiss(agent_id, p["message_id"])

    return patched


def _count_interactive_questions(db: Session, agent_id: str, tool_use_id: str) -> int:
    """Return the total number of questions for an interactive item."""
    msgs = db.query(Message).filter(
        Message.agent_id == agent_id,
        Message.meta_json.is_not(None),
    ).order_by(Message.created_at.desc()).limit(50).all()
    for msg in msgs:
        try:
            meta = json.loads(msg.meta_json)
        except (json.JSONDecodeError, TypeError):
            logger.debug("Skipped unparseable meta_json in _count_interactive_questions, msg_id=%s", msg.id)
            continue
        for item in meta.get("interactive", []):
            if item.get("tool_use_id") == tool_use_id:
                return len(item.get("questions", []))
    return 1


def _build_answers_from_metadata(db: Session, agent_id: str, tool_use_id: str) -> dict:
    """Build {question_text: selected_label} from accumulated per-question answers."""
    msgs = db.query(Message).filter(
        Message.agent_id == agent_id,
        Message.tool_use_id == tool_use_id,
    ).order_by(Message.created_at.desc()).all()
    for msg in msgs:
        try:
            meta = json.loads(msg.meta_json or "{}")
        except (json.JSONDecodeError, TypeError):
            logger.debug("Skipped unparseable meta_json in _build_answers_from_metadata, msg_id=%s", msg.id)
            continue
        for item in meta.get("interactive", []):
            if item.get("tool_use_id") != tool_use_id:
                continue
            answers = {}
            questions = item.get("questions", [])
            sel_indices = item.get("selected_indices", {})
            for qi, q in enumerate(questions):
                idx = sel_indices.get(str(qi))
                if idx is not None:
                    options = q.get("options", [])
                    if idx < len(options):
                        answers[q["question"]] = options[idx]["label"]
            return answers
    return {}


def _get_questions_from_metadata(db: Session, agent_id: str, tool_use_id: str) -> list:
    """Get the original questions array from interactive metadata."""
    msgs = db.query(Message).filter(
        Message.agent_id == agent_id,
        Message.tool_use_id == tool_use_id,
    ).order_by(Message.created_at.desc()).all()
    for msg in msgs:
        try:
            meta = json.loads(msg.meta_json or "{}")
        except (json.JSONDecodeError, TypeError):
            logger.debug("Skipped unparseable meta_json in _get_questions_from_metadata, msg_id=%s", msg.id)
            continue
        for item in meta.get("interactive", []):
            if item.get("tool_use_id") == tool_use_id:
                return item.get("questions", [])
    return []


@router.post("/api/agents/{agent_id}/answer")
async def answer_agent_interactive(
    agent_id: str,
    body: AnswerPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    """Answer an AskUserQuestion or approve/reject ExitPlanMode via tmux keys."""
    agent = get_agent_or_404(db, agent_id)
    if agent.status not in (AgentStatus.IDLE, AgentStatus.EXECUTING, AgentStatus.IDLE):
        raise HTTPException(status_code=400, detail=f"Agent is {agent.status}, not in interactive state")

    # Agents without an active tmux pane (e.g. pane lost): patch DB only.
    # Claude auto-approves with --dangerously-skip-permissions, so the card is informational.
    has_tmux = bool(agent.tmux_pane)
    if has_tmux:
        from agent_dispatcher import send_tmux_keys, verify_tmux_pane
        if not verify_tmux_pane(agent.tmux_pane):
            raise HTTPException(status_code=400, detail="Tmux pane no longer exists")

    pane_id = agent.tmux_pane
    MAX_INDEX = 20  # safety cap to prevent excessive keystrokes

    if body.type == "ask_user_question":
        if body.selected_index is None or body.selected_index < 0:
            raise HTTPException(status_code=400, detail="selected_index required for ask_user_question")
        if body.selected_index > MAX_INDEX:
            raise HTTPException(status_code=400, detail=f"selected_index too large (max {MAX_INDEX})")

        # Patch DB immediately for instant UI feedback
        patched = _patch_interactive_answer(db, agent_id, body.tool_use_id, body.selected_index, body.type, body.question_index)
        if not patched:
            logger.warning("Interactive patch missed: tool_use_id=%s agent=%s", body.tool_use_id, agent_id)
        else:
            from websocket import emit_metadata_update
            await emit_metadata_update(agent_id, patched["message_id"])

        # Multi-Q: accumulate answers until all questions are answered
        total_questions = _count_interactive_questions(db, agent_id, body.tool_use_id)
        if total_questions > 1 and body.question_index < total_questions - 1:
            return {"detail": "ok", "partial": True, "question_index": body.question_index}

        # All questions answered — try updatedInput path (hook-based, no tmux keys)
        from permissions import PermissionManager
        pm: PermissionManager | None = getattr(request.app.state, "permission_manager", None)
        # Route strictly by the tool_use_id of the card the user answered.
        # No tool-name fallback: if the exact request can't be found,
        # routing to "whichever AskUserQuestion happens to be pending" is
        # the same wrong guess that cross-contaminated answers before. A
        # mismatch means something is already off — surface it instead of
        # guessing. (pending_id stays None → falls through to the tmux path
        # below, the genuine mechanism for non-hook / old-CC cards.)
        pending_id = pm.find_pending_by_tool_use_id(agent_id, body.tool_use_id) if pm else None
        if pm and body.tool_use_id and not pending_id:
            logger.warning(
                "answer: no pending AskUserQuestion request for agent %s "
                "tool_use_id=%s — not guessing, falling through to tmux path",
                agent_id[:8], body.tool_use_id,
            )

        if pending_id:
            # Build updatedInput payload: {questions: [...], answers: {q_text: label}}
            answers = _build_answers_from_metadata(db, agent_id, body.tool_use_id)
            questions = _get_questions_from_metadata(db, agent_id, body.tool_use_id)
            updated_input = {"questions": questions, "answers": answers}
            pm.respond(pending_id, "allow", reason="Answered from Xylocopa", updated_input=updated_input)
            logger.info("AskUserQuestion resolved via updatedInput for agent %s: %s", agent_id[:8], list(answers.keys()))
            return {"detail": "ok", "method": "updatedInput"}
        else:
            # Fallback: tmux keys (no pending hook request — race condition or old CC)
            if not has_tmux:
                return {"detail": "ok", "method": "tmux", "keys_sent": 0, "auto_approved": True}
            if total_questions > 1:
                # Multi-Q fallback can't replay per-question Down×N navigation
                # from a single final-question request — would corrupt earlier
                # answers (CC then writes "User declined"). Dismiss the picker
                # and ask the user to answer in TUI.
                send_tmux_keys(pane_id, ["Escape"])
                logger.warning(
                    "AskUserQuestion multi-Q fallback unavailable for agent %s — picker dismissed",
                    agent_id[:8],
                )
                raise HTTPException(
                    status_code=503,
                    detail="Multi-question fallback unavailable (no live hook). Picker dismissed; please answer in TUI."
                )
            logger.info("AskUserQuestion single-Q tmux fallback for agent %s", agent_id[:8])
            keys = ["Down"] * body.selected_index + ["Enter"]
            if not send_tmux_keys(pane_id, keys):
                raise HTTPException(status_code=500, detail="Failed to send keys to tmux")
            return {"detail": "ok", "method": "tmux", "keys_sent": body.selected_index + 1}

    elif body.type == "exit_plan_mode":
        # Claude Code TUI plan approval options (arrow-navigated):
        # (v2.1.81+: "clear context" hidden by default)
        # 0: "Yes, and bypass permissions"
        # 1: "Yes, manually approve edits"
        # 2: "Type here to tell Claude what to change"

        if body.selected_index is not None and body.selected_index >= 0:
            if body.selected_index > MAX_INDEX:
                raise HTTPException(status_code=400, detail=f"selected_index too large (max {MAX_INDEX})")
            keys = ["Down"] * body.selected_index + ["Enter"]
        elif body.approved is True:
            keys = ["Enter"]  # legacy: approve = first option (bypass permissions)
        elif body.approved is False:
            keys = ["Down", "Enter"]  # legacy: reject → manual approval (safest)
        else:
            raise HTTPException(status_code=400, detail="selected_index or approved required for exit_plan_mode")
        effective_index = body.selected_index
        if effective_index is None:
            effective_index = 0 if body.approved else 1

        # --- Check if this is a planning agent ---
        _task = db.get(Task, agent.task_id) if agent.task_id else None
        is_planning_agent = _task and _task.status == TaskStatus.PLANNING

        if is_planning_agent and effective_index in (0, 1):
            # Planning agent: extract plan, kill tmux, dispatch new -p execution agent.
            # DON'T send tmux keys — the planning agent's job is done.

            # Extract plan text from the interactive metadata
            plan_text = ""
            if last_msg and last_msg.meta_json:
                try:
                    meta = json.loads(last_msg.meta_json)
                    for item in meta.get("interactive", []):
                        if item.get("type") == "exit_plan_mode":
                            plan_text = item.get("plan", "")
                            break
                except (json.JSONDecodeError, AttributeError):
                    logger.debug("Failed to parse plan metadata for agent %s", agent_id)
                    pass

            # Patch metadata to record the approval
            patched = _patch_interactive_answer(db, agent_id, body.tool_use_id, effective_index, body.type)
            if not patched:
                logger.warning("Interactive patch missed: tool_use_id=%s agent=%s", body.tool_use_id, agent_id)
            else:
                from websocket import emit_metadata_update
                await emit_metadata_update(agent_id, patched["message_id"])

            try:
                # Store approved plan on task for the execution agent
                _task.retry_context = plan_text or None

                # Transition PLANNING → PENDING (dispatch picks it up)
                TaskStateMachine.transition(_task, TaskStatus.PENDING)
                _task.agent_id = None  # unlink so dispatch creates new agent
                _task.started_at = None

                # Option 1 (manual approval): disable skip_permissions
                if effective_index == 1:
                    _task.skip_permissions = False

                # Stop planning agent
                agent.task_id = None
                agent.status = AgentStatus.STOPPED
                db.commit()

                # Kill tmux session
                if has_tmux:
                    graceful_kill_tmux_agent(pane_id, agent.id)

                asyncio.ensure_future(emit_task_update(
                    _task.id, _task.status.value, _task.project_name or "",
                    title=_task.title,
                ))
                logger.info(
                    "Planning task %s → PENDING for -p execution (option %d, killed planning agent %s)",
                    _task.id, effective_index, agent.id,
                )
            except InvalidTransitionError as e:
                logger.warning("Failed to transition planning task %s: %s", _task.id, e)

            return {"detail": "ok", "keys_sent": 0, "prompt_type": "planning_handoff", "auto_approved": False}

        # --- Non-planning agent: normal tmux key flow ---
        if has_tmux:
            # Capture pane content BEFORE sending keys for diagnostics
            from agent_dispatcher import capture_tmux_pane, _detect_plan_prompt
            pre_content = capture_tmux_pane(pane_id)
            prompt_type = _detect_plan_prompt(pre_content) if pre_content else "unknown"
            logger.info(
                "ExitPlanMode answer for agent %s: prompt_type=%s, selected_index=%s, pre_pane:\n%s",
                agent_id, prompt_type, body.selected_index,
                (pre_content or "")[-2000:],  # last 2000 chars to avoid huge logs
            )

            # Send tmux keys FIRST — only patch DB on success (Bug 6 race fix)
            if not send_tmux_keys(pane_id, keys):
                raise HTTPException(status_code=500, detail="Failed to send keys to tmux")

            # Patch DB immediately after keys succeed — BEFORE any await.
            patched = _patch_interactive_answer(db, agent_id, body.tool_use_id, effective_index, body.type)
            if not patched:
                logger.warning("Interactive patch missed: tool_use_id=%s agent=%s", body.tool_use_id, agent_id)
            else:
                from websocket import emit_metadata_update
                await emit_metadata_update(agent_id, patched["message_id"])

            # Capture pane content AFTER sending keys for diagnostics
            await asyncio.sleep(0.5)
            post_content = capture_tmux_pane(pane_id)
            logger.info(
                "ExitPlanMode post-keys for agent %s: post_pane:\n%s",
                agent_id,
                (post_content or "")[-2000:],
            )
        else:
            prompt_type = "no-pane"

        # No tmux pane: patch DB immediately (no keys to send)
        if not has_tmux:
            patched = _patch_interactive_answer(db, agent_id, body.tool_use_id, effective_index, body.type)
            if not patched:
                logger.warning("Interactive patch missed: tool_use_id=%s agent=%s", body.tool_use_id, agent_id)
            else:
                from websocket import emit_metadata_update
                await emit_metadata_update(agent_id, patched["message_id"])

        return {"detail": "ok", "keys_sent": len(keys) if has_tmux else 0, "prompt_type": prompt_type, "auto_approved": not has_tmux}

    elif body.type == "permission_prompt":
        # Native Claude Code permission prompt — same TUI nav as AskUserQuestion
        if body.selected_index is None or body.selected_index < 0:
            raise HTTPException(status_code=400, detail="selected_index required for permission_prompt")
        if body.selected_index > MAX_INDEX:
            raise HTTPException(status_code=400, detail=f"selected_index too large (max {MAX_INDEX})")

        if has_tmux:
            keys = ["Down"] * body.selected_index + ["Enter"]
            if not send_tmux_keys(pane_id, keys):
                raise HTTPException(status_code=500, detail="Failed to send keys to tmux")
        else:
            keys = []

        patched = _patch_interactive_answer(db, agent_id, body.tool_use_id, body.selected_index, body.type)
        if not patched:
            logger.warning("Interactive patch missed: tool_use_id=%s agent=%s", body.tool_use_id, agent_id)
        else:
            from websocket import emit_metadata_update
            await emit_metadata_update(agent_id, patched["message_id"])

        return {"detail": "ok", "keys_sent": len(keys), "auto_approved": not has_tmux}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown type: {body.type}")


# ---- Escape (send Escape key to tmux) ----

_last_escape: dict[str, float] = {}  # agent_id → timestamp

@router.post("/api/agents/{agent_id}/escape")
async def send_escape_to_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Send Escape key to the agent's tmux pane to dismiss interactive prompts."""
    import time

    agent = get_agent_or_404(db, agent_id)
    if not agent.tmux_pane:
        raise HTTPException(status_code=400, detail="Agent has no tmux pane")

    # Rate limit: max 1 Escape per 2 seconds per agent
    now = time.time()
    last = _last_escape.get(agent_id, 0)
    if now - last < 2.0:
        raise HTTPException(status_code=429, detail="Escape rate limited (max 1 per 2s)")
    _last_escape[agent_id] = now

    # Dismiss ALL pending hook requests for this agent (AskUserQuestion,
    # ExitPlanMode, permission prompts) so blocked hooks return immediately.
    from permissions import PermissionManager
    pm: PermissionManager | None = getattr(request.app.state, "permission_manager", None)
    if pm:
        for pending in pm.get_pending(agent_id):
            pm.respond(pending["request_id"], "deny", reason="Dismissed by user")
            logger.info("Dismissed pending %s for agent %s via escape",
                        pending.get("tool_name", "?"), agent_id[:8])

    from agent_dispatcher import send_tmux_keys, verify_tmux_pane
    if not verify_tmux_pane(agent.tmux_pane):
        raise HTTPException(status_code=400, detail="Tmux pane no longer exists")

    # Send Ctrl+C (hardcoded app:interrupt in Claude Code) instead of Escape.
    # Escape is context-dependent: with queued prompts it pulls text back to
    # the input bar; in vim mode it switches to NORMAL; in tmux it has a 50ms
    # disambiguation delay.  Ctrl+C always interrupts generation reliably.
    # ESC endpoint manages its own inter-keystroke timing via explicit
    # sleeps between key groups, so it bypasses send_tmux_keys's default
    # 200ms-per-key delay (saves ~800ms on the 4-key sequence below).
    if not send_tmux_keys(agent.tmux_pane, ["C-c"], inter_key_delay=0):
        raise HTTPException(status_code=500, detail="Failed to send interrupt to tmux")
    # C-c interrupt may restore the prompt text to the input bar (CC's
    # behavior varies by version and by whether the prompt was typed vs
    # pasted). Clear the input via C-u (readline-style "discard line"), which
    # CC's TUI binds to a deterministic full-input clear regardless of state.
    # History: C-l (v<=2.1.123) became a no-op in post-generation state in
    # v2.1.126; "Esc Esc + sleep + Esc" worked but depended on CC's internal
    # even/odd Esc parity (empty input + Esc Esc → Rewind menu, needs trailing
    # Esc to back out) and was fragile across versions. C-u sidesteps both
    # state machines — worst case it leaves residual rather than hanging.
    time.sleep(0.3)  # let C-c finish + prompt restore settle
    send_tmux_keys(agent.tmux_pane, ["C-u"], inter_key_delay=0)
    # tmux send-keys returns when keys are written to the PTY, not when CC
    # finishes rendering them. Give CC time to process C-u before we
    # transition to IDLE + dispatch the next queued message — otherwise the
    # next send_tmux_message can paste while the input bar is mid-clear.
    time.sleep(0.1)

    # Escape is a privileged terminal signal: a user-initiated interrupt
    # is itself authoritative, on par with stop_hook/interrupt/rate_limit.
    # We can't rely on sync_engine alone here because CC does NOT always
    # write "[Request interrupted by user]" to JSONL — early interrupts
    # (Esc within a few seconds of sending, before any assistant content
    # is committed) leave no JSONL marker, so the sync_engine path would
    # leave status stuck at EXECUTING forever.
    #
    # Design note: this widens the state-machine invariant from "only
    # sync_engine writes status" to "only sync_engine writes EXECUTING;
    # a few authoritative paths may write IDLE directly." Escape qualifies
    # because the user explicitly demanded the agent stop.
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad:
        ad._stop_generating(agent_id)
        ad.wake_sync(agent_id)
    if agent.status == AgentStatus.EXECUTING:
        agent.status = AgentStatus.IDLE
        agent.generating_msg_id = None
        db.commit()
        from websocket import emit_agent_update
        await emit_agent_update(agent_id, "IDLE", agent.project or "")
        if ad:
            asyncio.ensure_future(ad.dispatch_pending_message(agent_id, delay=0))

    # Patch any unanswered interactive cards so hasPendingInteractive unblocks.
    # C-c interrupts the CLI before tool_result can be written, so the normal
    # PostToolUse backfill path never fires — we must dismiss cards directly.
    dismissed = _dismiss_pending_interactive_cards(db, agent_id)
    if dismissed:
        from websocket import emit_metadata_update
        for d in dismissed:
            await emit_metadata_update(agent_id, d["message_id"])
        logger.info("escape: dismissed %d interactive card(s) for agent %s",
                     len(dismissed), agent_id[:8])

    logger.info("escape: sent C-c to agent %s pane %s, woke sync", agent_id[:8], agent.tmux_pane)
    return {"detail": "ok"}
