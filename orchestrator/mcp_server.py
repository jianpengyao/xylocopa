#!/usr/bin/env python3
"""Xylocopa MCP Server — gives Claude Code agents access to orchestrator data.

Read tools: list_sessions, read_session (previous conversations).
Write tools: create_task (drops a task into the Xylocopa inbox).

Runs as a stdio MCP server, spawned per-agent by Claude Code via .mcp.json.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys

# Add orchestrator dir so we can import jsonl_parser (lightweight, no config side effects)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from jsonl_parser import (  # noqa: E402
    format_tool_summary,
    parse_session_turns,
    strip_agent_preamble,
)
from utils import truncate  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration — all from env vars, no config.py import
# ---------------------------------------------------------------------------
XYLOCOPA_ROOT = os.environ.get(
    "XYLOCOPA_ROOT",
    os.environ.get("AGENTHIVE_ROOT", os.path.dirname(_SCRIPT_DIR)),  # legacy fallback
)
DB_PATH = os.path.join(XYLOCOPA_ROOT, "data", "orchestrator.db")
CLAUDE_HOME = os.path.expanduser(os.environ.get("CLAUDE_HOME", "~/.claude"))

# Max chars of one message/turn in MCP read outputs before truncation
TURN_CONTENT_MAX_CHARS = 3000

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("xylocopa.mcp")


# ---------------------------------------------------------------------------
# Path encoding — replicated from session_cache.py (avoids config.py import
# chain which pulls in dotenv + heavy deps)
# ---------------------------------------------------------------------------

def _encode_project_path(path: str) -> str:
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", path)
    if len(encoded) <= 200:
        return encoded
    h = hashlib.md5(path.encode()).hexdigest()[:8]
    return f"{encoded[:200]}-{h}"


def _session_source_dir(project_path: str) -> str:
    """Locate Claude Code's session directory for a project path.

    Realpath-normalize first so symlinked projects (e.g. xylocopa
    self-hosting) match the dir Claude CLI actually writes to.
    """
    project_path = os.path.realpath(project_path)
    predicted = _encode_project_path(project_path)
    projects_root = os.path.join(CLAUDE_HOME, "projects")

    # Fast path
    if os.path.isdir(os.path.join(projects_root, predicted)):
        return os.path.join(projects_root, predicted)

    # Scan for matching directory (handles encoding differences)
    path_norm = re.sub(r"[^a-zA-Z0-9]", "", project_path).lower()
    try:
        for entry in os.listdir(projects_root):
            if re.sub(r"[^a-zA-Z0-9]", "", entry).lower() == path_norm:
                candidate = os.path.join(projects_root, entry)
                if os.path.isdir(candidate):
                    return candidate
    except OSError:
        pass

    return os.path.join(projects_root, predicted)


# ---------------------------------------------------------------------------
# Database helpers (raw sqlite3, read-only)
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection | None:
    """Open a read-only connection to the orchestrator database."""
    if not os.path.isfile(DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = FastMCP(
    "xylocopa",
    instructions=(
        "Xylocopa orchestrator control plane. Use these tools to inspect "
        "and grow xylocopa state from inside an agent — query projects, "
        "tasks, sessions, and agents; create new tasks and projects; "
        "scaffold or regenerate CLAUDE.md; check orchestrator health.\n\n"

        "## Tools by domain\n"
        "- project: project_list, project_get, project_create, "
        "project_scaffold, project_regenerate_claude_md\n"
        "- task:    task_list, task_get, task_counts, task_create, "
        "task_update, task_dispatch\n"
        "- session: session_list, session_read, session_tail\n"
        "- agent:   agent_list, agent_get\n"
        "- probe:   probe_create, probe_list, probe_get, probe_update "
        "(event-driven chat wake-up; probes wake a target chat when an "
        "external webhook fires)\n"
        "- webapp:  webapp_present, webapp_list (interactive web-app "
        "cards in chat — see section below)\n"
        "- system:  system_health\n"
        "- aliases (kept for back-compat, prefer the domain-prefixed names): "
        "list_sessions, read_session, create_task, update_task, "
        "dispatch_task, list_tasks\n\n"

        "## Safety boundary — by design, only non-destructive verbs are exposed\n"
        "- Allowed verbs (whitelist): list, get, read, tail, create, update, "
        "dispatch, scaffold, regenerate, count, health.\n"
        "- Forbidden verbs (will never be added): delete, archive, kill, "
        "force, reset, drop, wipe, clean. These operations exist in the "
        "web UI but are intentionally NOT exposed via MCP. If you need to "
        "delete a project, archive an agent, restart the server, purge "
        "backups, or otherwise cause data loss, ask the human user — do "
        "not attempt to work around this.\n"
        "- All write tools are idempotent: calling task_create twice with "
        "the same args creates two tasks (by design — title is not a key); "
        "project_create with an existing name is a no-op (active) or "
        "re-activates (archived); scaffold/regenerate are safe to repeat.\n"
        "- task_dispatch handles retries: the state machine permits "
        "FAILED→PENDING and TIMEOUT→PENDING, so dispatching a failed task "
        "re-queues it.\n\n"

        "## File handling\n"
        "When generating or referencing media files (images, videos, plots), "
        "save them inside the project directory so the web UI can display "
        "them. Files in /tmp/ or other external paths cannot be previewed.\n\n"

        "## Interactive web apps (webapp_present / webapp_list)\n"
        "You can hand the user a tappable card in this chat that opens an "
        "interactive web app fullscreen (sandboxed iframe; mouse and touch "
        "work; console output appears in a built-in debug drawer). Call "
        "webapp_present — file paths mentioned in plain message text do "
        "NOT create cards. Prefer this over rendering videos/images "
        "whenever the deliverable is interactive: data explorers, plots "
        "with controls, 3D/point-cloud viewers, demos, training "
        "dashboards.\n"
        "Three kinds, auto-detected from target:\n"
        "- static — project-relative .html entry you wrote. Assets/data "
        "must use RELATIVE paths (./app.js, ./data.json); Vite builds "
        "need base:'./'. Reusable viewers go in webapps/<name>/ at the "
        "project root, committed to git.\n"
        "- port — a localhost service (TensorBoard, vite dev). Proxied "
        "with the prefix stripped, so root-served apps work as-is. Only "
        "for cookie-less services: apps with login sessions (wandb local, "
        "jupyter, grafana) break in the credential-less sandbox — present "
        "those as url instead.\n"
        "- url — an external or cookie-authenticated dashboard (wandb.ai, "
        "wandb local, jupyter). Opens in a new tab with normal cookies; "
        "most such sites forbid iframe embedding anyway.\n"
        "Lifecycle: a port app belongs to the agent that presents it — its "
        "server process is killed when that agent stops. Always launch your "
        "OWN instance on a free port; never point a card at a port another "
        "agent registered. Reuse viewer CODE, not live services: check "
        "webapp_list / webapps/<name>/ for an existing viewer before "
        "building a new one, then start a fresh instance of it."
    ),
)


# Lazy write-session factory — keeps MCP startup cheap for read-only callers.
_WriteSession = None


def _get_write_session():
    """Construct a write-capable SQLAlchemy session on first use.

    Mirrors the main app's pragmas (WAL, FK, busy_timeout) so inserts
    made here are consistent with those made by the backend.
    """
    global _WriteSession
    if _WriteSession is not None:
        return _WriteSession()

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    _WriteSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return _WriteSession()


@server.tool()
def session_list(project: str = "") -> str:
    """List recent Xylocopa agent sessions.

    Shows agent name, project, status, session ID, and last message preview.
    Use this to discover session IDs that can be passed to session_read().

    Args:
        project: Filter by project name (optional — shows all if empty)
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        query = (
            "SELECT id, name, project, session_id, status, "
            "       last_message_preview, last_message_at "
            "FROM agents WHERE session_id IS NOT NULL "
            # Hide meta-agent sessions hosted on synthetic projects
            # (e.g. AI triage on `.xylo-internal`).
            "AND project NOT LIKE '.%' "
        )
        params: tuple = ()
        if project:
            query += "AND project = ? "
            params = (project,)
        query += "ORDER BY last_message_at DESC LIMIT 30"

        rows = db.execute(query, params).fetchall()
    finally:
        db.close()

    if not rows:
        return "No sessions found." + (f" (project filter: {project})" if project else "")

    lines = [f"Found {len(rows)} session(s):\n"]
    for r in rows:
        ts = (r["last_message_at"] or "")[:19]
        preview = (r["last_message_preview"] or "")[:100]
        status = r["status"] or "?"
        lines.append(
            f"- **{r['name']}** [{status}] — {r['project']}\n"
            f"  session: `{r['session_id']}`  agent: `{r['id']}`\n"
            f"  {ts}  {preview}\n"
        )
    return "\n".join(lines)


@server.tool()
def session_read(session_id: str, max_turns: int = 50) -> str:
    """Read a previous Xylocopa conversation by session ID or agent ID.

    Returns formatted conversation turns (user prompts, agent responses,
    system events). Reads the curated display file (small, already stripped
    of thinking blocks and tool noise) and falls back to raw JSONL only
    when the display file doesn't exist yet.

    Args:
        session_id: Session UUID, agent ID, or a prefix of either
        max_turns: Maximum number of turns to return (default 50, most recent)
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        row = _lookup_agent(db, session_id)
    finally:
        db.close()

    if row is None:
        return f"No agent found matching: {session_id}"

    agent_name = row["name"]
    agent_id = row["id"]
    project_name = row["project"]
    actual_session_id = row["session_id"]
    project_path = row["path"]

    # Try display file first — curated, ~6-12% of raw JSONL size
    display_path = os.path.join(XYLOCOPA_ROOT, "data", "display", f"{agent_id}.jsonl")
    if os.path.isfile(display_path):
        return _read_from_display(
            display_path, agent_name, project_name, actual_session_id, max_turns,
        )

    # Fallback: raw JSONL (for agents whose display file hasn't been written yet)
    return _read_from_jsonl(
        agent_name, project_name, actual_session_id, project_path, max_turns,
    )


def _read_from_display(
    display_path: str,
    agent_name: str,
    project_name: str,
    session_id: str,
    max_turns: int,
) -> str:
    """Read chat history from the curated display file."""
    try:
        with open(display_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        return f"Failed to read display file: {e}"

    # Parse lines, dedup by id (last occurrence wins for _replace entries)
    seen: dict[str, dict] = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = obj.get("id")
        if msg_id:
            seen[msg_id] = obj

    entries = list(seen.values())
    total = len(entries)

    if not entries:
        return f"Session {session_id} exists but has no messages in display file."

    if total > max_turns:
        entries = entries[-max_turns:]

    role_map = {"USER": "User", "AGENT": "Agent", "SYSTEM": "System"}
    lines = [
        f"# Session: {agent_name} ({project_name})",
        f"Session ID: `{session_id}`",
        f"Messages: {total} total"
        + (f" (showing last {max_turns})" if total > max_turns else ""),
        "",
    ]

    for entry in entries:
        role = role_map.get(entry.get("role", ""), entry.get("role", ""))
        ts = (entry.get("created_at") or "")[:19]
        content = entry.get("content") or ""

        content = truncate(content, TURN_CONTENT_MAX_CHARS)

        lines.append(f"**[{role}]** {ts}")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)


def _read_from_jsonl(
    agent_name: str,
    project_name: str,
    session_id: str,
    project_path: str,
    max_turns: int,
) -> str:
    """Fallback: read chat history from raw Claude Code JSONL."""
    src_dir = _session_source_dir(project_path)
    jsonl_path = os.path.join(src_dir, f"{session_id}.jsonl")

    # Also check worktree locations
    if not os.path.isfile(jsonl_path):
        wt_base = os.path.join(project_path, ".claude", "worktrees")
        if os.path.isdir(wt_base):
            for wt_name in os.listdir(wt_base):
                wt_session_dir = _session_source_dir(
                    os.path.join(wt_base, wt_name)
                )
                candidate = os.path.join(wt_session_dir, f"{session_id}.jsonl")
                if os.path.isfile(candidate):
                    jsonl_path = candidate
                    break

    if not os.path.isfile(jsonl_path):
        return (
            f"Session not found for {agent_name} ({project_name}).\n"
            f"Neither display file nor session JSONL exists.\n"
            f"The session file may have been cleaned up."
        )

    # Cap at 2MB to avoid OOM on huge sessions
    turns = parse_session_turns(jsonl_path, max_bytes=2 * 1024 * 1024)
    total_turns = len(turns)

    if not turns:
        return f"Session {session_id} exists but has no parseable turns."

    if total_turns > max_turns:
        turns = turns[-max_turns:]

    lines = [
        f"# Session: {agent_name} ({project_name})",
        f"Session ID: `{session_id}`",
        f"Turns: {total_turns} total"
        + (f" (showing last {max_turns})" if total_turns > max_turns else ""),
        "",
    ]

    for role, content, metadata, _uuid, kind, timestamp in turns:
        ts = (timestamp or "")[:19]
        role_label = {"user": "User", "assistant": "Agent", "system": "System"}.get(
            role, role
        )

        if role == "user":
            content = strip_agent_preamble(content)

        if kind == "tool_use" and metadata:
            summary = format_tool_summary(
                metadata.get("tool_name", ""),
                metadata.get("tool_input", {}),
            )
            if summary:
                content = summary

        content = truncate(content, TURN_CONTENT_MAX_CHARS)

        lines.append(f"**[{role_label}]** {ts}")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)


@server.tool()
def session_tail(session_id: str, max_turns: int = 10) -> str:
    """Read just the latest few turns of a xylocopa session.

    Same backend as session_read but optimized for "what just happened" —
    smaller default turn cap. Use session_read when you need fuller history.

    Args:
        session_id: Session UUID, agent ID, or a prefix of either.
        max_turns: Max turns to return (default 10, most recent).
    """
    return session_read(session_id=session_id, max_turns=max_turns)


# ---------------------------------------------------------------------------
# Old-name aliases (session domain) — preserved for backward compatibility
# ---------------------------------------------------------------------------

@server.tool()
def list_sessions(project: str = "") -> str:
    """[Alias for session_list — kept for backward compatibility.]

    See session_list for full docs.
    """
    return session_list(project=project)


@server.tool()
def read_session(session_id: str, max_turns: int = 50) -> str:
    """[Alias for session_read — kept for backward compatibility.]

    See session_read for full docs.
    """
    return session_read(session_id=session_id, max_turns=max_turns)


@server.tool()
def task_create(
    title: str,
    project: str = "",
    description: str = "",
    model: str = "",
    effort: str = "",
    priority: int = 0,
) -> str:
    """Create a task in the Xylocopa inbox.

    Tasks land in INBOX status. The user can promote/queue them in the web UI.

    IMPORTANT — always set the classification tags (model, effort) when
    creating a task. Leaving them empty falls back to project defaults, which
    loses the per-task judgment about complexity and which model tier fits
    the work. Pick deliberately:

      model:
        - claude-haiku-4-5-20251001  → trivial scripts, single-file edits,
                                       no cross-module reasoning needed
        - claude-sonnet-5             → most implementation work: bug fixes,
                                       small features, refactors with clear
                                       scope, frontend tweaks
        - claude-opus-4-6             → architectural decisions, multi-file
                                       redesigns, debugging across layers,
                                       anything needing deep reasoning
        - claude-opus-4-7             → same as 4-6, a newer tier
        - claude-opus-4-8             → previous Opus tier; high-stakes work
        - claude-opus-5               → previous Opus tier; same price as 4.8
        - claude-opus-5-5             → latest Opus tier and the default:
                                       1M context, cheaper than Opus 5
                                       ($4/$20 vs $5/$25), go-to for
                                       complex agentic coding
        - claude-fable-5              → most capable; flagship for the
                                       hardest tasks (~2x cost of Opus)
        - claude-fable-5-1            → newest flagship point release;
                                       same price tier as Fable 5

      effort:
        - low      → < 1 hour, well-scoped (~30 lines or contained tweak)
        - medium   → 1-3 hours, multiple files but bounded
        - high     → half-day, cross-cutting changes with research
        - xhigh    → full day, requires design + implementation + testing
        - max      → multi-day, needs scope decomposition
        - ultracode → xhigh + standing multi-agent workflow orchestration
                     (claude --effort ultracode). Very token-heavy — pick
                     only when the user explicitly asks for ultracode

    Args:
        title: Short task title (required, max 300 chars).
        project: Project name. Leave empty to infer from the caller's cwd
            (the project whose path is the longest prefix of cwd wins,
            which covers worktree subdirectories). Pass explicitly if you
            want a task in a project other than the one you're in.
        description: Longer task body (optional, markdown supported).
        model: Claude model id (see guidance above). Empty = project default.
        effort: low | medium | high | xhigh | max | ultracode (see
            guidance above). Empty = project default.
        priority: 0 (normal) or 1 (high). Default 0.
    """
    # Lazy imports so read-only callers don't pay the cost
    from pydantic import ValidationError

    from models import Project, TaskStatus
    from schemas import TaskCreate
    from task_service import build_task

    inferred_from_cwd = False

    with _get_write_session() as db:
        if project:
            proj = db.query(Project).filter_by(name=project).one_or_none()
            if proj is None:
                available = sorted(
                    p.name for p in db.query(Project).all()
                    if not p.name.startswith(".")
                )
                return (
                    f"Project `{project}` not found.\n"
                    f"Available: {', '.join(available)}"
                )
        else:
            cwd = os.getcwd()
            candidates = [
                p for p in db.query(Project).all()
                if p.path and not p.name.startswith(".")
                and (cwd == p.path or cwd.startswith(p.path + "/"))
            ]
            candidates.sort(key=lambda p: len(p.path), reverse=True)
            if not candidates:
                available = sorted(
                    p.name for p in db.query(Project).all()
                    if not p.name.startswith(".")
                )
                return (
                    f"Could not infer project from cwd ({cwd}).\n"
                    f"Pass `project` explicitly. "
                    f"Available: {', '.join(available)}"
                )
            proj = candidates[0]
            project = proj.name
            inferred_from_cwd = True

        try:
            payload = TaskCreate(
                title=title,
                description=description or None,
                project_name=project,
                model=model or None,
                effort=effort or None,
                priority=priority,
            )
        except ValidationError as e:
            return f"Validation error:\n{e}"

        task = build_task(payload, status=TaskStatus.INBOX)
        db.add(task)
        db.commit()
        task_id = task.id

    project_note = f"{project}" + (" (inferred from cwd)" if inferred_from_cwd else "")
    return (
        f"Created task `{task_id}` in project `{project_note}` [INBOX].\n"
        f"Title: {title}\n"
        f"Model: {model or '(project default)'}  "
        f"Effort: {effort or '(default)'}  Priority: {priority}"
    )


@server.tool()
def task_update(
    task_id: str,
    title: str = "",
    description: str = "",
    project: str = "",
    model: str = "",
    effort: str = "",
    priority: int | None = None,
) -> str:
    """Update fields on an existing Xylocopa task.

    Only fields you pass (non-empty for strings, non-None for priority) are
    updated. Status is intentionally NOT mutable here — use task_dispatch to
    queue a task, or other dedicated tools for status changes.

    Common use case: a task was created without explicit model/effort tags
    (or with wrong ones) and you want to backfill them. See task_create for
    the full guidance on picking model tier (haiku/sonnet/opus) and effort
    bucket (low → max) — the same rules apply here.

    Args:
        task_id: ID of the task to update (required).
        title: New title. Empty = leave unchanged.
        description: New description. Empty = leave unchanged.
        project: New project name. Must exist in the Project table.
            Empty = leave unchanged.
        model: New Claude model id (claude-haiku-4-5-20251001 |
            claude-sonnet-5 | claude-opus-4-6 | claude-opus-4-7 |
            claude-opus-4-8 | claude-opus-5 | claude-opus-5-5 |
            claude-fable-5 | claude-fable-5-1).
            Empty = leave unchanged.
        effort: New effort level (low|medium|high|xhigh|max|ultracode).
            Empty = leave unchanged.
        priority: New priority (0 normal, 1 high). None = leave unchanged.
    """
    # Lazy imports so read-only callers don't pay the cost
    from models import Project, Task

    with _get_write_session() as db:
        task = db.get(Task, task_id)
        if task is None:
            return f"Task {task_id} not found."

        changes: list[str] = []

        if project:
            proj = db.query(Project).filter_by(name=project).one_or_none()
            if proj is None:
                available = sorted(
                    p.name for p in db.query(Project).all()
                    if not p.name.startswith(".")
                )
                return (
                    f"Project `{project}` not found.\n"
                    f"Available: {', '.join(available)}"
                )
            if task.project_name != project:
                task.project_name = project
                changes.append(f"project={project}")

        if title:
            if task.title != title:
                task.title = title
                changes.append("title")

        if description:
            if task.description != description:
                task.description = description
                changes.append("description")

        if model:
            if task.model != model:
                task.model = model
                changes.append(f"model={model}")

        if effort:
            if task.effort != effort:
                task.effort = effort
                changes.append(f"effort={effort}")

        if priority is not None:
            if task.priority != priority:
                task.priority = priority
                changes.append(f"priority={priority}")

        if not changes:
            return f"Task {task_id}: no changes (all provided fields matched current values or were empty)."

        db.commit()
        return f"Updated task {task_id}: {', '.join(changes)}."


@server.tool()
def task_dispatch(task_id: str) -> str:
    """Queue a task for execution by transitioning it to PENDING.

    The orchestrator's background poller picks up PENDING tasks within a
    few seconds and spawns a tmux agent to run them. Use this after a task
    has been created (INBOX) and fully specified, or to retry a FAILED/
    TIMEOUT task (the state machine permits FAILED/TIMEOUT → PENDING).

    Prerequisites:
        - task must exist
        - task must have a project_name
        - task must have a title
        - current status must be one that can transition to PENDING
          (typically INBOX, FAILED, TIMEOUT)

    Args:
        task_id: ID of the task to dispatch (required).
    """
    # Lazy imports so read-only callers don't pay the cost
    from models import Task, TaskStatus
    from task_state_machine import can_transition

    with _get_write_session() as db:
        task = db.get(Task, task_id)
        if task is None:
            return f"Task {task_id} not found."

        if not task.project_name:
            return f"Task {task_id} has no project_name set; cannot dispatch."
        if not task.title:
            return f"Task {task_id} has no title set; cannot dispatch."

        current_status = task.status
        if current_status == TaskStatus.PENDING:
            return f"Task {task_id} is already PENDING; awaiting dispatch."

        if not can_transition(current_status, TaskStatus.PENDING):
            return (
                f"Task {task_id} cannot transition from "
                f"{current_status.value} to PENDING."
            )

        title = task.title

        rows = (
            db.query(Task)
            .filter(Task.id == task_id, Task.status == current_status)
            .update({"status": TaskStatus.PENDING}, synchronize_session="fetch")
        )
        if rows == 0:
            return f"Task {task_id} status changed concurrently; retry."
        db.commit()

    return (
        f"Task {task_id} ({title}) queued for dispatch. "
        f"The orchestrator will spawn an agent within a few seconds."
    )


@server.tool()
def task_list(project: str = "", status: str = "", limit: int = 30) -> str:
    """List Xylocopa tasks, optionally filtered by project and status.

    Results are ordered by created_at DESC.

    Args:
        project: Filter by project name (optional — shows all if empty).
        status: Filter by status, e.g. "INBOX", "PENDING", "EXECUTING",
            "FAILED", "COMPLETE" (case-insensitive). Empty = all statuses.
        limit: Max results (default 30, capped at 100).
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    try:
        clauses: list[str] = []
        params: list = []
        if project:
            clauses.append("project_name = ?")
            params.append(project)
        if status:
            clauses.append("UPPER(status) = ?")
            params.append(status.strip().upper())

        query = (
            "SELECT id, title, project_name, status, priority, created_at "
            "FROM tasks"
        )
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(query, tuple(params)).fetchall()
    finally:
        db.close()

    if not rows:
        filters = []
        if project:
            filters.append(f"project={project}")
        if status:
            filters.append(f"status={status}")
        suffix = f" ({', '.join(filters)})" if filters else ""
        return f"No tasks found.{suffix}"

    lines = [f"Found {len(rows)} task(s):\n"]
    for r in rows:
        ts = (r["created_at"] or "")[:19]
        title = (r["title"] or "(untitled)")[:120]
        lines.append(
            f"- **{title}** [{r['status']}] — {r['project_name'] or '(no project)'}\n"
            f"  id: `{r['id']}`  priority: {r['priority']}  created: {ts}"
        )
    return "\n".join(lines)


@server.tool()
def task_get(task_id: str) -> str:
    """Get full detail for a single task by ID.

    Includes title, description, status, project, model, effort, priority,
    timestamps, attempt count, error message (if any), and worktree info.

    Args:
        task_id: Task ID (required).
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        row = db.execute(
            "SELECT id, title, description, project_name, status, priority, "
            "       model, effort, attempt_number, agent_id, worktree_name, "
            "       branch_name, created_at, started_at, completed_at, "
            "       error_message, agent_summary "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    finally:
        db.close()

    if row is None:
        return f"Task `{task_id}` not found."

    desc = row["description"] or "(no description)"
    if len(desc) > 1000:
        desc = desc[:1000] + "\n... (truncated)"
    summary = row["agent_summary"] or ""
    if summary and len(summary) > 1000:
        summary = summary[:1000] + "\n... (truncated)"

    lines = [
        f"# Task: {row['title']}",
        f"- id: `{row['id']}`",
        f"- status: {row['status']}",
        f"- project: {row['project_name'] or '(none)'}",
        f"- model: {row['model'] or '(default)'}  effort: {row['effort'] or '(default)'}  priority: {row['priority']}",
        f"- attempt: {row['attempt_number']}",
        f"- agent_id: {row['agent_id'] or '(none)'}",
        f"- worktree: {row['worktree_name'] or '(none)'}  branch: {row['branch_name'] or '(none)'}",
        f"- created: {(row['created_at'] or '')[:19]}",
        f"- started: {(row['started_at'] or '(not started)')[:19] if row['started_at'] else '(not started)'}",
        f"- completed: {(row['completed_at'] or '(not completed)')[:19] if row['completed_at'] else '(not completed)'}",
        "",
        "## Description",
        desc,
    ]
    if row["error_message"]:
        err = row["error_message"]
        if len(err) > 500:
            err = err[:500] + "\n... (truncated)"
        lines += ["", "## Error", err]
    if summary:
        lines += ["", "## Agent summary", summary]
    return "\n".join(lines)


@server.tool()
def task_counts(project: str = "") -> str:
    """Get task counts grouped by status, optionally filtered by project.

    Cheaper than task_list when you only need backlog totals.

    Args:
        project: Filter by project name (optional). Empty = all projects.
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        if project:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM tasks WHERE project_name = ? "
                "GROUP BY status ORDER BY status",
                (project,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status"
            ).fetchall()
    finally:
        db.close()

    if not rows:
        suffix = f" (project={project})" if project else ""
        return f"No tasks found.{suffix}"

    total = sum(r[1] for r in rows)
    scope = f"project `{project}`" if project else "all projects"
    lines = [f"Task counts for {scope} (total: {total}):"]
    for status, count in rows:
        lines.append(f"  {status}: {count}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Old-name aliases (task domain) — preserved for backward compatibility
# ---------------------------------------------------------------------------

@server.tool()
def create_task(
    title: str,
    project: str = "",
    description: str = "",
    model: str = "",
    effort: str = "",
    priority: int = 0,
) -> str:
    """[Alias for task_create — kept for backward compatibility.]

    Output format is byte-identical to task_create. See task_create for full docs.
    """
    return task_create(
        title=title, project=project, description=description,
        model=model, effort=effort, priority=priority,
    )


@server.tool()
def update_task(
    task_id: str,
    title: str = "",
    description: str = "",
    project: str = "",
    model: str = "",
    effort: str = "",
    priority: int | None = None,
) -> str:
    """[Alias for task_update — kept for backward compatibility.]

    See task_update for full docs.
    """
    return task_update(
        task_id=task_id, title=title, description=description, project=project,
        model=model, effort=effort, priority=priority,
    )


@server.tool()
def dispatch_task(task_id: str) -> str:
    """[Alias for task_dispatch — kept for backward compatibility.]

    See task_dispatch for full docs.
    """
    return task_dispatch(task_id=task_id)


@server.tool()
def list_tasks(project: str = "", status: str = "", limit: int = 30) -> str:
    """[Alias for task_list — kept for backward compatibility.]

    See task_list for full docs.
    """
    return task_list(project=project, status=status, limit=limit)


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------

def _project_row(db: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Look up a project row by exact name."""
    return db.execute(
        "SELECT name, display_name, path, git_remote, description, archived, "
        "       default_model, max_concurrent, emoji "
        "FROM projects WHERE name = ?",
        (name,),
    ).fetchone()


def _registry_path() -> str:
    return os.path.join(XYLOCOPA_ROOT, "project-configs", "registry.yaml")


def _read_registry() -> dict:
    """Read registry.yaml. Returns {} on missing/empty."""
    import yaml
    path = _registry_path()
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _write_registry(data: dict) -> None:
    import yaml
    path = _registry_path()
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


def _registry_has(name: str) -> bool:
    data = _read_registry()
    return any(p.get("name") == name for p in (data.get("projects") or []))


def _registry_append(entry: dict) -> None:
    """Append a project entry to registry.yaml. Idempotent on name."""
    data = _read_registry()
    if "projects" not in data or data["projects"] is None:
        data["projects"] = []
    if any(p.get("name") == entry["name"] for p in data["projects"]):
        return  # already present, no-op
    data["projects"].append(entry)
    _write_registry(data)


def _registry_remove(name: str) -> None:
    """Remove a project entry from registry.yaml. No-op if absent."""
    data = _read_registry()
    projects = data.get("projects") or []
    filtered = [p for p in projects if p.get("name") != name]
    if len(filtered) != len(projects):
        data["projects"] = filtered
        _write_registry(data)


@server.tool()
def project_list(include_archived: bool = False) -> str:
    """List all xylocopa projects.

    Args:
        include_archived: If True, include archived (soft-deleted) projects too.
            Default False shows only active projects.
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        # `.` prefix is reserved for synthetic system projects (e.g.
        # `.xylo-internal` hosts AI triage). Hide them from the MCP-facing
        # list — agents calling project_list shouldn't see internal hosts.
        if include_archived:
            rows = db.execute(
                "SELECT name, path, git_remote, description, archived, default_model "
                "FROM projects WHERE name NOT LIKE '.%' "
                "ORDER BY archived ASC, name ASC"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT name, path, git_remote, description, archived, default_model "
                "FROM projects WHERE archived = 0 AND name NOT LIKE '.%' "
                "ORDER BY name ASC"
            ).fetchall()
    finally:
        db.close()

    if not rows:
        return "No projects found."

    lines = [f"Found {len(rows)} project(s):\n"]
    for r in rows:
        archived_tag = " [ARCHIVED]" if r["archived"] else ""
        lines.append(
            f"- **{r['name']}**{archived_tag} — `{r['path']}`\n"
            f"  model: {r['default_model'] or '(default)'}  "
            f"git: {r['git_remote'] or '(local)'}\n"
            f"  {r['description'] or ''}"
        )
    return "\n".join(lines)


@server.tool()
def project_get(name: str) -> str:
    """Get detailed info for a single project by name.

    Returns: path, archived flag, default model, agent count, task count by
    status, recent session count.

    Args:
        name: Project name (required).
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        row = _project_row(db, name)
        if row is None:
            available = sorted(
                r["name"] for r in db.execute(
                    "SELECT name FROM projects WHERE archived = 0"
                ).fetchall()
            )
            return (
                f"Project `{name}` not found.\n"
                f"Available: {', '.join(available)}"
            )

        agent_total = db.execute(
            "SELECT COUNT(*) FROM agents WHERE project = ?", (name,)
        ).fetchone()[0]
        agent_active = db.execute(
            "SELECT COUNT(*) FROM agents WHERE project = ? "
            "AND status IN ('STARTING','RUNNING','WAITING')",
            (name,),
        ).fetchone()[0]
        task_counts = db.execute(
            "SELECT status, COUNT(*) FROM tasks WHERE project_name = ? GROUP BY status",
            (name,),
        ).fetchall()
        session_total = db.execute(
            "SELECT COUNT(*) FROM agents WHERE project = ? AND session_id IS NOT NULL",
            (name,),
        ).fetchone()[0]
    finally:
        db.close()

    archived_tag = " [ARCHIVED]" if row["archived"] else ""
    task_breakdown = ", ".join(f"{r[0]}={r[1]}" for r in task_counts) or "(none)"

    lines = [
        f"# Project: {row['name']}{archived_tag}",
        f"- display_name: {row['display_name']}",
        f"- path: `{row['path']}`",
        f"- git_remote: {row['git_remote'] or '(local)'}",
        f"- default_model: {row['default_model']}",
        f"- max_concurrent: {row['max_concurrent']}",
        f"- emoji: {row['emoji'] or '(none)'}",
        f"- description: {row['description'] or '(none)'}",
        "",
        "## Stats",
        f"- agents: {agent_total} total, {agent_active} active",
        f"- sessions: {session_total}",
        f"- tasks: {task_breakdown}",
    ]
    return "\n".join(lines)


@server.tool()
def project_create(
    name: str,
    path: str = "",
    git_url: str = "",
    description: str = "",
) -> str:
    """Register a new project in xylocopa.

    The project's directory must already exist (or be safely creatable).
    MCP does NOT clone git repos — clone yourself first, then call this.
    `git_url` is recorded as metadata only.

    Idempotent:
      - If a project with this name already exists and is active, returns
        existing info unchanged.
      - If a project with this name exists but is archived, re-activates it
        (matches the web UI's "create twice" semantics).

    Rolls back DB insert if registry write fails.

    Args:
        name: Project name (required, alphanumeric + . _ -).
        path: Filesystem path. Defaults to ~/xylocopa-projects/{name}.
        git_url: Optional git remote URL (recorded as metadata).
        description: Optional project description.
    """
    import re as _re

    if not _re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", name):
        return (
            f"Invalid project name `{name}`. Must start with alphanumeric "
            f"and contain only [a-zA-Z0-9._-]."
        )
    if len(name) > 100:
        return f"Project name too long: {len(name)} chars (max 100)."

    if not path:
        path = os.path.expanduser(f"~/xylocopa-projects/{name}")
    path = os.path.abspath(path)

    from models import Project
    from project_scaffolder import scaffold_project

    with _get_write_session() as db:
        existing = db.get(Project, name)
        if existing is not None:
            if existing.archived:
                existing.archived = False
                if git_url and not existing.git_remote:
                    existing.git_remote = git_url
                if description and not existing.description:
                    existing.description = description
                db.commit()
                # Ensure registry has the entry
                entry = {"name": name, "path": existing.path}
                if existing.git_remote:
                    entry["git_remote"] = existing.git_remote
                if existing.description:
                    entry["description"] = existing.description
                _registry_append(entry)
                return (
                    f"Project `{name}` re-activated from archive.\n"
                    f"Path: {existing.path}"
                )
            # Active project with same name — idempotent return
            return (
                f"Project `{name}` already exists (active).\n"
                f"Path: {existing.path}\n"
                f"No changes made."
            )

        # Create the directory if missing
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            return f"Could not create directory {path}: {e}"

        # Insert into DB
        proj = Project(
            name=name,
            display_name=name,
            path=path,
            git_remote=git_url or None,
            description=description or None,
        )
        db.add(proj)
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            return f"DB insert failed: {e}"

        # Append to registry — rollback DB on failure
        entry = {"name": name, "path": path}
        if git_url:
            entry["git_remote"] = git_url
        if description:
            entry["description"] = description
        try:
            _registry_append(entry)
        except Exception as e:
            db.delete(proj)
            db.commit()
            return f"Registry write failed (DB rolled back): {e}"

    # Scaffold (best-effort; don't roll back on failure)
    try:
        scaffold_project(name, path)
    except Exception as e:
        logger.warning("scaffold_project failed for %s: %s", name, e)

    return (
        f"Created project `{name}`.\n"
        f"Path: {path}\n"
        f"Git: {git_url or '(local)'}\n"
        f"Scaffolded CLAUDE.md and PROGRESS.md if missing."
    )


@server.tool()
def project_scaffold(name: str) -> str:
    """Generate CLAUDE.md and PROGRESS.md for a project if missing.

    Idempotent: existing scaffolded files (containing the template header)
    are left untouched. Use project_regenerate_claude_md to force a rewrite.

    Args:
        name: Project name (must exist in xylocopa).
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        row = _project_row(db, name)
    finally:
        db.close()

    if row is None:
        return f"Project `{name}` not found."
    if not os.path.isdir(row["path"]):
        return f"Project `{name}` path does not exist on disk: {row['path']}"

    from project_scaffolder import scaffold_project
    try:
        result = scaffold_project(name, row["path"], force=False)
    except Exception as e:
        return f"Scaffold failed for `{name}`: {e}"

    parts = []
    if result.get("claude_md"):
        parts.append("CLAUDE.md created")
    if result.get("progress_md"):
        parts.append("PROGRESS.md created")
    if not parts:
        parts.append("no changes (files already present)")
    return f"Project `{name}`: {', '.join(parts)}."


@server.tool()
def project_regenerate_claude_md(name: str) -> str:
    """Regenerate CLAUDE.md for a project from the deterministic scaffolder.

    This re-runs the template scaffolder with force=True, preserving the
    project-specific rules section. Synchronous and predictable — does NOT
    use the AI-powered async refresh used by the web UI.

    Args:
        name: Project name (must exist in xylocopa).
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        row = _project_row(db, name)
    finally:
        db.close()

    if row is None:
        return f"Project `{name}` not found."
    if not os.path.isdir(row["path"]):
        return f"Project `{name}` path does not exist on disk: {row['path']}"

    from project_scaffolder import scaffold_project
    try:
        result = scaffold_project(name, row["path"], force=True)
    except Exception as e:
        return f"Regenerate failed for `{name}`: {e}"

    if result.get("claude_md"):
        return f"Regenerated CLAUDE.md for `{name}` (project-specific rules preserved)."
    return f"Project `{name}`: no changes (scaffolder reported no update)."


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

@server.tool()
def agent_list(project: str = "", status: str = "", limit: int = 30) -> str:
    """List xylocopa agents, optionally filtered by project and status.

    Results are ordered by last_message_at DESC (most recently active first).

    Args:
        project: Filter by project name (optional).
        status: Filter by status (e.g. STARTING, RUNNING, WAITING, STOPPED,
            ERROR, COMPLETE), case-insensitive. Empty = all statuses.
        limit: Max results (default 30, capped at 100).
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    try:
        # Hide meta-agents hosted on synthetic projects (e.g. AI triage on
        # `.xylo-internal`). They aren't user-launched agents and shouldn't
        # appear in the MCP-facing list.
        clauses: list[str] = ["project NOT LIKE '.%'"]
        params: list = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if status:
            clauses.append("UPPER(status) = ?")
            params.append(status.strip().upper())

        query = (
            "SELECT id, name, project, status, session_id, model, effort, "
            "       last_message_preview, last_message_at, created_at "
            "FROM agents"
        )
        query += " WHERE " + " AND ".join(clauses)
        query += (
            " ORDER BY COALESCE(last_message_at, created_at) DESC LIMIT ?"
        )
        params.append(limit)

        rows = db.execute(query, tuple(params)).fetchall()
    finally:
        db.close()

    if not rows:
        filters = []
        if project:
            filters.append(f"project={project}")
        if status:
            filters.append(f"status={status}")
        suffix = f" ({', '.join(filters)})" if filters else ""
        return f"No agents found.{suffix}"

    lines = [f"Found {len(rows)} agent(s):\n"]
    for r in rows:
        ts = (r["last_message_at"] or r["created_at"] or "")[:19]
        preview = (r["last_message_preview"] or "")[:80]
        lines.append(
            f"- **{r['name']}** [{r['status']}] — {r['project']}\n"
            f"  id: `{r['id']}`  session: `{r['session_id'] or '(none)'}`\n"
            f"  model: {r['model'] or '(default)'}  "
            f"effort: {r['effort'] or '(default)'}  last: {ts}\n"
            f"  {preview}"
        )
    return "\n".join(lines)


@server.tool()
def agent_get(agent_id: str) -> str:
    """Get full detail for a single agent.

    Accepts agent ID, session ID, or a prefix of either (uses the same
    lookup logic as session_read).

    Args:
        agent_id: Agent ID, session ID, or prefix.
    """
    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        row = _lookup_agent(db, agent_id)
        if row is None:
            return f"No agent found matching: {agent_id}"

        # Fetch the full row for the matched agent
        full = db.execute(
            "SELECT id, name, project, status, session_id, mode, model, effort, "
            "       branch, worktree, tmux_pane, last_message_preview, "
            "       last_message_at, unread_count, created_at, muted, "
            "       parent_id, task_id, is_subagent, has_pending_suggestions "
            "FROM agents WHERE id = ?",
            (row["id"],),
        ).fetchone()
    finally:
        db.close()

    if full is None:
        return f"No agent found matching: {agent_id}"

    preview = (full["last_message_preview"] or "(no messages)")[:200]

    lines = [
        f"# Agent: {full['name']}",
        f"- id: `{full['id']}`",
        f"- project: {full['project']}",
        f"- status: {full['status']}",
        f"- mode: {full['mode']}",
        f"- session_id: `{full['session_id'] or '(none)'}`",
        f"- model: {full['model'] or '(default)'}  effort: {full['effort'] or '(default)'}",
        f"- branch: {full['branch'] or '(none)'}  worktree: {full['worktree'] or '(none)'}",
        f"- tmux_pane: {full['tmux_pane'] or '(none)'}",
        f"- task_id: {full['task_id'] or '(none)'}",
        f"- parent_id: {full['parent_id'] or '(none)'}  is_subagent: {bool(full['is_subagent'])}",
        f"- unread: {full['unread_count']}  muted: {bool(full['muted'])}  "
        f"pending_suggestions: {bool(full['has_pending_suggestions'])}",
        f"- created: {(full['created_at'] or '')[:19]}",
        f"- last_message_at: {(full['last_message_at'] or '(never)')[:19] if full['last_message_at'] else '(never)'}",
        "",
        "## Last message preview",
        preview,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Probe tools — event-driven chat wake-up
# ---------------------------------------------------------------------------

# Per-agent active-probe cap. Keeps DB bloat and the attack surface small.
_PROBE_PER_AGENT_LIMIT = 10
_PROBE_DEFAULT_HOURS = 24
_PROBE_MAX_HOURS = 24 * 7  # 1 week


def _probe_public_base() -> str:
    """Return the URL prefix for the probe trigger endpoint.

    Honors `XYLOCOPA_PUBLIC_URL` if set (e.g. for remote-callable triggers
    behind a reverse proxy); otherwise falls back to the local backend
    PORT. Trailing slash is stripped.
    """
    pub = os.environ.get("XYLOCOPA_PUBLIC_URL", "").strip().rstrip("/")
    if pub:
        return pub
    port = os.environ.get("PORT", "8080").strip()
    return f"http://localhost:{port}"


@server.tool()
def probe_create(
    agent_id: str,
    message: str,
    expires_in_hours: int = _PROBE_DEFAULT_HOURS,
) -> str:
    """Register a webhook probe that wakes a chat when externally triggered.

    Returns a single-fire trigger URL. POSTing to that URL inserts a
    fixed message into the target chat. The body of the trigger POST is
    ignored — wake content is locked at create time. This is the secure
    path for "external monitor → wake chat" without polling.

    Notifications: when the agent finishes responding to the probe, the
    standard stop-hook notification fires (respects per-agent mute and
    global toggle). No probe-specific push channel.

    Args:
        agent_id: Target agent (the chat to be woken). Must be explicit.
        message: Text the chat will see when the probe fires. Wrapped in
            an envelope: "🔔 Probe {id} fired (timestamp)\\n\\n{message}".
            Write this from the perspective of the future you, e.g.
            "gamma containers cleared, restart docker now".
        expires_in_hours: Auto-expiry. Default 24h, max 168h (1 week).
            Expired probes return 410 Gone if triggered. Probes are
            also auto-expired when their target agent transitions to
            STOPPED or ERROR.

    Active per-agent limit: 10. Once reached, expire some via
    `probe_update` (set expires_at to a past time) or wait for them
    to time out.

    ## Behavior contract for the wake target

    When the probe fires, the target chat receives a USER message of
    the form:

        🔔 Probe {id} fired (May 9 10:14pm)

        {message}

    Treat this as an EXTERNAL EVENT TRIGGER, not user input. Specifically:
      - The user did not just type this. An external system called the
        webhook because a condition the chat registered was met.
      - VERIFY the underlying condition before taking action — probes
        can fire on stale or partial state. Read the relevant log,
        re-check the state, etc.
      - For consequential actions (writes, restarts, sends, deletions),
        confirm with the user before executing.
      - Do not just say "thanks for letting me know" — the probe was
        registered with the expectation of work happening when it fires.
    """
    if not agent_id or not isinstance(agent_id, str):
        return "Error: agent_id is required."
    if expires_in_hours <= 0 or expires_in_hours > _PROBE_MAX_HOURS:
        return f"Error: expires_in_hours must be in (0, {_PROBE_MAX_HOURS}]."

    from models import Agent, Probe
    from datetime import timedelta
    from utils import utcnow as _utcnow

    # Centralized validation — same rules apply at dispatch time as defense
    # in depth. Catches embedded envelope spoofing, oversize, control chars.
    err = Probe.validate_message(message)
    if err:
        return f"Error: {err}"

    sess = _get_write_session()
    try:
        agent = sess.get(Agent, agent_id)
        if agent is None:
            return f"Error: agent {agent_id} not found."

        # Active probe cap (unfired AND unexpired).
        now = _utcnow()
        active_count = sess.query(Probe).filter(
            Probe.agent_id == agent_id,
            Probe.fired_at.is_(None),
            Probe.expires_at > now,
        ).count()
        if active_count >= _PROBE_PER_AGENT_LIMIT:
            return (
                f"Error: agent {agent_id[:8]} already has "
                f"{active_count} active probes (limit {_PROBE_PER_AGENT_LIMIT}). "
                f"Use probe_update to expire some, or wait for them to time out."
            )

        probe = Probe(
            agent_id=agent_id,
            message=message.strip(),
            expires_at=now + timedelta(hours=expires_in_hours),
        )
        sess.add(probe)
        sess.commit()
        sess.refresh(probe)

        trigger_url = f"{_probe_public_base()}/api/probe-trigger/{probe.token}"
        return (
            f"Probe **{probe.id}** created for agent `{agent_id[:8]}`.\n\n"
            f"- Trigger URL: `{trigger_url}`\n"
            f"- Expires: {probe.expires_at.isoformat()} (in {expires_in_hours}h)\n"
            f"- Single-fire: subsequent POSTs return 410 Gone.\n\n"
            f"To trigger from a script:\n"
            f"  curl -X POST '{trigger_url}'\n\n"
            f"Body is ignored by design (no prompt injection surface)."
        )
    finally:
        sess.close()


@server.tool()
def probe_list(agent_id: str = "", include_fired: bool = False) -> str:
    """List probes — by default unfired/unexpired only.

    Args:
        agent_id: Filter to one agent. Empty = all agents.
        include_fired: If True, include already-fired probes.
    """
    sess = _get_write_session()
    try:
        from models import Probe
        from utils import utcnow as _utcnow

        q = sess.query(Probe)
        if agent_id:
            q = q.filter(Probe.agent_id == agent_id)
        now = _utcnow()
        if not include_fired:
            q = q.filter(Probe.fired_at.is_(None), Probe.expires_at > now)
        rows = q.order_by(Probe.created_at.desc()).limit(50).all()

        if not rows:
            return "No probes found."

        lines = [f"Found {len(rows)} probe(s):\n"]
        for p in rows:
            state = (
                f"FIRED at {p.fired_at.isoformat()[:19]}"
                if p.fired_at
                else (
                    f"EXPIRED at {p.expires_at.isoformat()[:19]}"
                    if p.expires_at <= now
                    else f"active until {p.expires_at.isoformat()[:19]}"
                )
            )
            lines.append(
                f"- **{p.id}** agent=`{p.agent_id[:8]}` [{state}]\n"
                f"  message: {p.message[:80]}{'...' if len(p.message) > 80 else ''}"
            )
        return "\n".join(lines)
    finally:
        sess.close()


@server.tool()
def probe_get(probe_id: str) -> str:
    """Get full detail for a single probe by ID."""
    sess = _get_write_session()
    try:
        from models import Probe

        probe = sess.get(Probe, probe_id)
        if probe is None:
            return f"No probe found matching: {probe_id}"

        trigger_url = f"{_probe_public_base()}/api/probe-trigger/{probe.token}"
        lines = [
            f"# Probe: {probe.id}",
            f"- agent_id: `{probe.agent_id}`",
            f"- created_at: {probe.created_at.isoformat()}",
            f"- expires_at: {probe.expires_at.isoformat()}",
            f"- fired_at: {probe.fired_at.isoformat() if probe.fired_at else '(not fired)'}",
            f"- trigger URL: `{trigger_url}`",
            "",
            "## Message",
            probe.message,
        ]
        return "\n".join(lines)
    finally:
        sess.close()


@server.tool()
def probe_update(
    probe_id: str,
    expires_at: str = "",
) -> str:
    """Update a probe — extend or shrink expiry.

    To CANCEL a not-yet-fired probe, set `expires_at` to a past time
    (e.g. "1970-01-01T00:00:00Z"). The MCP whitelist forbids `delete`,
    so update-to-past is the supported cancel path.

    Args:
        probe_id: ID of the probe to update.
        expires_at: New ISO timestamp (e.g. "2026-05-10T22:14:33Z").
            Empty = no change.
    """
    sess = _get_write_session()
    try:
        from models import Probe
        from datetime import datetime as _dt

        probe = sess.get(Probe, probe_id)
        if probe is None:
            return f"No probe found matching: {probe_id}"
        if probe.fired_at is not None:
            return f"Probe {probe_id} already fired — cannot update."

        if not expires_at:
            return f"No fields to update for probe {probe_id}."

        try:
            probe.expires_at = _dt.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return f"Error: invalid expires_at ISO timestamp: {expires_at!r}"

        sess.commit()
        return f"Probe {probe_id} updated: expires_at={probe.expires_at.isoformat()}"
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# System tools
# ---------------------------------------------------------------------------

@server.tool()
def system_health() -> str:
    """Lightweight liveness check for the xylocopa orchestrator.

    Verifies that the database file exists and is readable, the registry
    file is parseable, and reports total counts for projects/tasks/agents.
    Use this before queuing work to confirm the orchestrator is up.
    """
    parts = ["# Xylocopa health"]

    # DB check
    if not os.path.isfile(DB_PATH):
        parts.append(f"- db: MISSING ({DB_PATH})")
        return "\n".join(parts)
    parts.append(f"- db: OK (`{DB_PATH}`)")

    db = _get_db()
    if db is None:
        parts.append("- db: UNREADABLE")
        return "\n".join(parts)

    try:
        proj_count = db.execute(
            "SELECT COUNT(*) FROM projects WHERE archived = 0"
        ).fetchone()[0]
        task_count = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        agent_count = db.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        active_agents = db.execute(
            "SELECT COUNT(*) FROM agents "
            "WHERE status IN ('STARTING','RUNNING','WAITING')"
        ).fetchone()[0]
    finally:
        db.close()

    parts.append(f"- projects: {proj_count} (active)")
    parts.append(f"- tasks: {task_count} (all-time)")
    parts.append(f"- agents: {agent_count} total, {active_agents} active")

    # Registry check
    reg_path = _registry_path()
    if not os.path.isfile(reg_path):
        parts.append(f"- registry: MISSING ({reg_path})")
    else:
        try:
            data = _read_registry()
            entries = len(data.get("projects") or [])
            parts.append(f"- registry: OK ({entries} entries)")
        except Exception as e:
            parts.append(f"- registry: PARSE-ERROR ({e})")

    parts.append(f"- xylocopa_root: `{XYLOCOPA_ROOT}`")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Existing helpers (used by older tools below)
# ---------------------------------------------------------------------------

def _lookup_agent(db: sqlite3.Connection, identifier: str) -> dict | None:
    """Look up an agent by session_id, agent_id, or prefix match.

    Returns a dict with agent fields + project path, or None.
    """
    # Join with projects to get path in one query
    base_query = (
        "SELECT a.id, a.name, a.project, a.session_id, p.path "
        "FROM agents a JOIN projects p ON a.project = p.name "
    )

    # Exact session_id match
    row = db.execute(
        base_query + "WHERE a.session_id = ?", (identifier,)
    ).fetchone()
    if row:
        return row

    # Exact agent_id match
    row = db.execute(
        base_query + "WHERE a.id = ?", (identifier,)
    ).fetchone()
    if row:
        return row

    # Prefix match on session_id
    row = db.execute(
        base_query + "WHERE a.session_id LIKE ? ORDER BY a.last_message_at DESC LIMIT 1",
        (f"{identifier}%",),
    ).fetchone()
    if row:
        return row

    # Prefix match on agent_id
    row = db.execute(
        base_query + "WHERE a.id LIKE ? ORDER BY a.last_message_at DESC LIMIT 1",
        (f"{identifier}%",),
    ).fetchone()
    return row


# ---------------------------------------------------------------------------
# Web app tools — present interactive deliverables as tappable cards in chat
# ---------------------------------------------------------------------------

_WEBAPP_KINDS = ("static", "port", "url")


def _webapp_resolve_agent(sess, agent_id: str = ""):
    """Resolve the calling agent → (Agent, error_string).

    Order: explicit agent_id → $TMUX_PANE pane match (authoritative, same
    rationale as hook resolution — XY_AGENT_ID leaks via env inheritance
    into unrelated panes) → $XY_AGENT_ID as fallback.
    """
    from models import Agent

    if agent_id:
        a = sess.get(Agent, agent_id)
        if a is None:
            return None, f"Error: agent {agent_id} not found."
        return a, None
    pane = os.environ.get("TMUX_PANE", "").strip()
    if pane:
        a = sess.query(Agent).filter(Agent.tmux_pane == pane).first()
        if a is not None:
            return a, None
    env_id = os.environ.get(
        "XY_AGENT_ID", os.environ.get("AHIVE_AGENT_ID", "")).strip()
    if env_id:
        a = sess.get(Agent, env_id)
        if a is not None:
            return a, None
    return None, ("Error: could not identify the calling agent "
                  "(no TMUX_PANE / XY_AGENT_ID match). Pass agent_id explicitly.")


def _port_proxy_sig(project: str, port: int) -> str:
    """Stable capability signature — mirrors routers/preview.py port_proxy_sig."""
    import hmac

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT value FROM system_config WHERE key='jwt_secret'").fetchone()
    finally:
        conn.close()
    secret = row["value"]
    msg = f"preview-port:{project}:{port}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def _ping_emit_message(message_id: str) -> None:
    """Best-effort instant-refresh ping; the UI's 5s poll covers failures."""
    import urllib.request

    port = os.environ.get("PORT", "8080").strip()
    req = urllib.request.Request(
        f"http://localhost:{port}/api/hooks/emit-message",
        data=json.dumps({"message_id": message_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception as e:  # noqa: BLE001 — fire-and-forget by design
        logger.debug("emit-message ping failed: %s", e)


@server.tool()
def webapp_present(target: str, title: str = "", description: str = "",
                   kind: str = "", agent_id: str = "") -> str:
    """Present an interactive web app as a tappable card in this chat.

    The user taps the card to open the app fullscreen in a sandboxed
    iframe (mouse + touch work; console output and uncaught errors appear
    in a built-in debug drawer). Prefer this over rendering videos/images
    whenever the deliverable is interactive: data explorers, plots with
    controls, 3D/point-cloud viewers, demos, parameter playgrounds,
    training dashboards. Plain .html paths mentioned in chat text do NOT
    create cards — this tool is the only way to present one.

    Also registers the app in the project's web-app registry (see
    webapp_list) so future agents can discover and reuse the viewer
    code. Port apps are NOT reusable as live services: the server
    process is killed when the presenting agent stops, and presenting
    a port transfers registry ownership to the caller.

    Args:
        target: One of (kind auto-detected):
            - project-relative path to an .html entry, e.g.
              "webapps/pointcloud-viewer/index.html"  → static app
            - a localhost port number, e.g. "6006"    → proxied service
              (TensorBoard, vite dev, any 127.0.0.1 HTTP server)
            - an external URL, e.g. a wandb run page  → opens in new tab
              (most external sites forbid iframe embedding)
        title: Short human label shown on the card.
        description: One-liner shown in webapp_list (what it does, what
            data it expects).
        kind: Override auto-detection: "static" | "port" | "url".
        agent_id: Target chat. Defaults to the calling agent's own chat.

    Static-app conventions:
        - Assets and data must use RELATIVE paths (./app.js, ./data.json);
          Vite builds need base:'./'. fetch()/XHR to relative URLs works,
          including preflighted requests.
        - Reusable viewers belong in webapps/<name>/ at the project root,
          committed to git, data alongside (webapps/<name>/data/).
        - Check webapp_list FIRST before building a new viewer.
        - Uncaught sync exceptions show as muted 'Script error' in the
          drawer — report failures via console.error for full detail.

    Port conventions:
        - The proxy strips its prefix: the service sees root-relative
          requests, so root-served apps (TensorBoard, python -m
          http.server) work unmodified.
        - The returned proxy prefix is stable — safe to reuse across
          service restarts.
        - Launch your OWN server on a FREE port (a bind failure means
          taken — pick another; ports in webapp_list may be dead, the
          registry outlives processes). Never present a port whose
          service another agent is still running: the process dies
          when ITS presenting agent stops.
        - Cookie-less services only. The sandbox is credential-less by
          design, so login-session apps (wandb local, jupyter, grafana)
          render blank or hit a login wall when embedded — present those
          as kind="url" pointing at http://<host>:<port>/... instead.
    """
    target = (target or "").strip()
    if not target:
        return "Error: target is required."
    kind = (kind or "").strip().lower()
    if not kind:
        if re.match(r"^https?://", target):
            kind = "url"
        elif target.isdigit():
            kind = "port"
        else:
            kind = "static"
    if kind not in _WEBAPP_KINDS:
        return f"Error: kind must be one of {_WEBAPP_KINDS}."

    from urllib.parse import quote

    from models import Message, MessageRole, MessageStatus, Project, WebApp
    from utils import utcnow as _utcnow

    sess = _get_write_session()
    try:
        agent, err = _webapp_resolve_agent(sess, agent_id)
        if err:
            return err
        project = agent.project
        proj = sess.get(Project, project)
        if proj is None:
            return f"Error: project {project} not found."

        meta = {"kind": kind, "target": target, "project": project,
                "title": title, "description": description}
        hint = ""
        if kind == "static":
            ppath = os.path.realpath(proj.path)
            t = target
            if t.startswith("/"):
                rt = os.path.realpath(t)
                if not rt.startswith(ppath + "/"):
                    return (f"Error: {target} is outside project {project} "
                            f"({proj.path}) — static apps must live inside "
                            f"the project directory.")
                t = rt[len(ppath) + 1:]
            target = t.lstrip("./") or t
            meta["target"] = target
            if not os.path.isfile(os.path.join(ppath, target)):
                return (f"Error: {target} not found in project {project}. "
                        f"Create the file first.")
        elif kind == "port":
            port = int(target)
            if not 1024 <= port <= 65535:
                return "Error: port must be in 1024-65535."
            backend_port = int(os.environ.get("PORT", "8080"))
            if port == backend_port:
                return "Error: cannot proxy the orchestrator's own port."
            src = f"/api/preview/p/{_port_proxy_sig(project, port)}/{quote(project)}/{port}/"
            meta["src"] = src
            hint = (f"\n- Stable proxy prefix: `{src}`\n"
                    f"- Prefix is stripped when proxying — root-served apps "
                    f"(TensorBoard, http.server) work as-is; vite dev needs "
                    f"base:'./'.")
        else:
            meta["src"] = target

        now = _utcnow()
        row = sess.query(WebApp).filter_by(
            project=project, kind=kind, target=target).first()
        if row is None:
            row = WebApp(project=project, kind=kind, target=target,
                         title=title, description=description,
                         created_by_agent=agent.id, last_presented_at=now)
            sess.add(row)
        else:
            if title:
                row.title = title
            if description:
                row.description = description
            # Last presenter owns the process: ports get recycled, and the
            # reaper kills by creator — without this transfer, a new server
            # launched on a previously-registered port would be reaped as
            # an orphan of the old (stopped) creator.
            row.created_by_agent = agent.id
            row.last_presented_at = now

        label = title or (f"localhost:{target}" if kind == "port" else target)
        msg = Message(
            agent_id=agent.id,
            role=MessageRole.AGENT,
            content=f"🖥️ Web app: {label}",
            status=MessageStatus.COMPLETED,
            source="mcp",
            kind="webapp",
            jsonl_uuid=f"mcp-webapp-{os.urandom(6).hex()}",
            meta_json=json.dumps({"webapp": meta}),
            created_at=now,
            completed_at=now,
        )
        sess.add(msg)
        sess.commit()
        sess.refresh(msg)
        _ping_emit_message(msg.id)
        return (f"Web app card posted to chat (message {msg.id}, kind={kind}).\n"
                f"- Registered: {project} / {kind} / {target}\n"
                f"- The user can tap the card to open it.{hint}")
    finally:
        sess.close()


@server.tool()
def webapp_list(project: str = "") -> str:
    """List registered web apps (interactive viewers, dashboards, demos).

    Check this BEFORE building a new viewer — the CODE may already exist
    (webapps/<name>/). static entries can be re-presented as-is; for port
    entries launch a FRESH instance on a free port and present that — a
    port's process dies with its presenting agent, so never point a card
    at another agent's live port.

    Args:
        project: Project name. Empty = infer from the calling agent,
            falling back to all projects.
    """
    from models import WebApp

    sess = _get_write_session()
    try:
        q = sess.query(WebApp)
        if not project:
            agent, _err = _webapp_resolve_agent(sess)
            if agent is not None:
                project = agent.project
        if project:
            q = q.filter(WebApp.project == project)
        rows = q.order_by(WebApp.last_presented_at.desc().nulls_last()).all()
        if not rows:
            scope = f"project `{project}`" if project else "any project"
            return (f"No web apps registered for {scope}. "
                    f"Present one with webapp_present.")
        lines = [f"Found {len(rows)} web app(s)"
                 + (f" in `{project}`" if project else "") + ":\n"]
        for r in rows:
            label = r.title or r.target
            lines.append(f"- **{label}** — kind={r.kind}, target=`{r.target}`"
                         + (f", project={r.project}" if not project else ""))
            if r.description:
                lines.append(f"  {r.description}")
            if r.last_presented_at:
                lines.append(f"  last presented: {r.last_presented_at:%Y-%m-%d %H:%M}")
        return "\n".join(lines)
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Dropbox sync (read-only, no config.py / engine / auth / client imports)
# ---------------------------------------------------------------------------

def _dropbox_sync_dir() -> str:
    """Resolve the Dropbox sync data directory."""
    return os.environ.get(
        "DROPBOX_SYNC_DIR",
        os.path.join(XYLOCOPA_ROOT, "data", "dropbox"),
    )


def _format_bytes(n: int) -> str:
    """Human-readable byte size (B/KB/MB/GB/TB, one decimal above KB)."""
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


@server.tool()
def dropbox_get() -> str:
    """Dropbox sync status: link state, schedule, last run, per-project counts.

    Read-only — never uploads or modifies anything.
    """
    sync_dir = _dropbox_sync_dir()
    config_path = os.path.join(sync_dir, "config.json")
    state_path = os.path.join(sync_dir, "state.db")
    token_path = os.path.join(sync_dir, "token.json")

    # Load config.json
    cfg: dict = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if not cfg and not os.path.isfile(state_path) and not os.path.isfile(token_path):
        return "Dropbox sync not configured."

    linked = os.path.isfile(token_path)
    paused = cfg.get("paused", False)
    if "interval_minutes" in cfg:
        interval_min = cfg["interval_minutes"]
    elif "interval_hours" in cfg:
        interval_min = cfg["interval_hours"] * 60
    else:
        interval_min = 5

    parts = ["# Dropbox sync status"]
    parts.append(f"- linked: {'yes' if linked else '**not linked**'}")
    parts.append(f"- paused: {'yes' if paused else 'no'}")
    if interval_min == 0:
        parts.append("- check interval: on change (no periodic check)")
    else:
        parts.append(f"- check interval: {interval_min} min")

    # Read state.db (read-only)
    if not os.path.isfile(state_path):
        parts.append("")
        parts.append("No sync state yet (state.db missing).")
        return "\n".join(parts)

    try:
        sdb = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        sdb.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        parts.append("")
        parts.append("Could not open state.db.")
        return "\n".join(parts)

    try:
        # Last run summary
        row = sdb.execute(
            "SELECT id, started_at, finished_at, status, trigger, "
            "files_scanned, files_uploaded, bytes_uploaded, files_deleted, errors, error_sample "
            "FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            parts.append("")
            parts.append("## Last run")
            parts.append(
                f"- id: {row['id']} | trigger: {row['trigger']} | status: {row['status']}"
            )
            parts.append(f"- started: {row['started_at']}")
            if row["finished_at"]:
                parts.append(f"- finished: {row['finished_at']}")
            parts.append(
                f"- scanned: {row['files_scanned']}, uploaded: {row['files_uploaded']} "
                f"({_format_bytes(row['bytes_uploaded'] or 0)}), "
                f"deleted: {row['files_deleted']}, errors: {row['errors']}"
            )
            if row["error_sample"]:
                parts.append(f"- error: {row['error_sample']}")

        # Next run (current running)
        cur_row = sdb.execute(
            "SELECT id, started_at, trigger, project "
            "FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if cur_row:
            parts.append("")
            parts.append(
                f"## Current run (id {cur_row['id']}, trigger: {cur_row['trigger']}, "
                f"started: {cur_row['started_at']}"
                + (f", project: {cur_row['project']}" if cur_row["project"] else "")
                + ")"
            )

        # Per-project table
        prows = sdb.execute(
            "SELECT project, files_synced, bytes_synced, last_synced_at, last_error "
            "FROM project_stats ORDER BY project"
        ).fetchall()
        if prows:
            parts.append("")
            parts.append("## Projects")
            parts.append("| project | files | bytes | last sync | error |")
            parts.append("|---------|------:|------:|-----------|-------|")
            for pr in prows:
                err = pr["last_error"] or ""
                parts.append(
                    f"| {pr['project']} | {pr['files_synced'] or 0} "
                    f"| {_format_bytes(pr['bytes_synced'] or 0)} "
                    f"| {pr['last_synced_at'] or 'never'} | {err} |"
                )

        # Recent errors (up to 5)
        erows = sdb.execute(
            "SELECT at, project, path, message FROM errors ORDER BY id DESC LIMIT 5"
        ).fetchall()
        if erows:
            parts.append("")
            parts.append("## Recent errors")
            for er in erows:
                proj = er["project"] or "?"
                path = er["path"] or ""
                parts.append(f"- [{er['at']}] {proj}: {path} — {er['message']}")
    finally:
        sdb.close()

    return "\n".join(parts)


@server.tool()
def dropbox_count(project: str) -> str:
    """Count files/bytes that would be synced per top-level folder — a dry run.

    Nothing is uploaded or modified.

    Args:
        project: Project name (required).
    """
    import time

    db = _get_db()
    if db is None:
        return "Xylocopa database not found. Is the orchestrator running?"

    try:
        row = db.execute(
            "SELECT name, path, dropbox_folders, dropbox_ignore "
            "FROM projects WHERE name = ? AND archived = 0",
            (project,),
        ).fetchone()
        if row is None:
            available = sorted(
                r[0] for r in db.execute(
                    "SELECT name FROM projects WHERE archived = 0"
                ).fetchall()
            )
            return (
                f"Project `{project}` not found.\n"
                f"Available: {', '.join(available)}"
            )
        proj_path = row["path"]
        folders_json = row["dropbox_folders"]
        ignore_text = row["dropbox_ignore"]
    finally:
        db.close()

    if not proj_path or not os.path.isdir(proj_path):
        return f"Project path `{proj_path}` does not exist."

    # Parse folders
    folders: list[str] | None = None
    if folders_json:
        try:
            folders = json.loads(folders_json)
        except (json.JSONDecodeError, TypeError):
            pass

    # Read config.json for allowlist_mode / allowlist_exts / max_file_mb
    sync_dir = _dropbox_sync_dir()
    config_path = os.path.join(sync_dir, "config.json")
    cfg: dict = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    allowlist_exts: set[str] | None = None
    if cfg.get("allowlist_mode"):
        allowlist_exts = set(cfg.get("allowlist_exts", []))

    max_file_mb = cfg.get("max_file_mb", 2048)
    max_file_bytes = max_file_mb * 1024 * 1024

    # Import scanner and ignore lazily
    from dropbox_sync.ignore import IgnoreRules
    from dropbox_sync.scanner import dry_run

    rules = IgnoreRules.build(
        proj_path,
        folders=folders,
        extra_rules=ignore_text,
        allowlist_exts=allowlist_exts,
    )

    # Timeout after 120 seconds
    deadline = time.monotonic() + 120

    def should_stop() -> bool:
        return time.monotonic() >= deadline

    result = dry_run(
        proj_path,
        rules,
        max_file_bytes=max_file_bytes,
        should_stop=should_stop,
    )

    entries = result["entries"]
    total = result["total"]
    stopped = result["stopped"]

    parts = [f"# Dry-run scan: {project}"]
    # Filter out entries with no files and no skipped (e.g. ignored dirs)
    visible = {
        name: stats for name, stats in entries.items()
        if stats["files"] > 0 or sum(stats["skipped"].values()) > 0
    }
    if visible:
        parts.append("")
        parts.append("| entry | files | bytes | skipped |")
        parts.append("|-------|------:|------:|--------:|")
        for name, stats in visible.items():
            sk = stats["skipped"]
            skip_total = sum(sk.values())
            parts.append(
                f"| {name} | {stats['files']} | {_format_bytes(stats['bytes'])} | {skip_total} |"
            )
    else:
        parts.append("")
        parts.append("No entries found.")

    parts.append("")
    skip_total_all = sum(total["skipped"].values())
    parts.append(
        f"**Total:** {total['files']} files, {_format_bytes(total['bytes'])}, "
        f"{skip_total_all} skipped"
    )

    if stopped:
        parts.append("")
        parts.append("*partial (timed out after 120 s)*")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server.run()
