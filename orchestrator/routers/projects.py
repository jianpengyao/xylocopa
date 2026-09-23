"""Project routes — CRUD, sessions, CLAUDE.md refresh, PROGRESS.md summary, directory browser."""

import asyncio
import difflib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import yaml
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import case, func, update, text
from sqlalchemy.orm import Session

from config import CC_MODEL, CLAUDE_HOME, PROJECT_CONFIGS_PATH, SUMMARY_MODEL, VALID_MODELS
from database import SessionLocal, get_db
from models import (
    Agent, AgentInsightSuggestion, AgentStatus, Message, MessageRole,
    MessageStatus, Project, StarredSession, Task, TaskStatus,
)
from agent_dispatcher import ACTIVE_STATUSES, TERMINAL_STATUSES
from schemas import (
    AgentBrief, AgentInsightSuggestionOut, AgentOut, MessageOut,
    ProjectCreate, ProjectOut, ProjectRename, ProjectWithStats,
    SessionSummary, TaskOut,
)
from route_helpers import (
    resolve_project_path, check_project_capacity, compute_successor_id,
    enrich_agent_briefs, get_project_or_404,
    API_REQUEST_TIMEOUT, SUBPROCESS_STRIP_VARS,
    subprocess_clean_env, graceful_kill_tmux,
    graceful_kill_tmux_agent, tmux_session_candidates,
)
from utils import utcnow as _utcnow, is_interrupt_message

logger = logging.getLogger("orchestrator")

router = APIRouter(tags=["projects"])


# ---- Reserved system project names ---------------------------------------
# The `.` prefix is reserved for synthetic projects the orchestrator manages
# itself.  Currently only `.xylo-internal` exists — it hosts meta-agents
# (e.g. AI triage) so they don't pollute the user's project listings.
# These rows exist in the projects table for FK reasons (Agent.project is
# non-nullable) but must not surface in any user-facing list.  Use
# is_internal_project() / exclude_internal() whenever enumerating projects
# for a UI / picker / sweep.
#
# `.` was chosen over `_` because Unix tooling (and our existing disk
# scanners in list_folders / scan_projects / reconcile) already skip
# dot-prefixed entries — so the placeholder is invisible to disk-driven
# code paths for free, no extra filter required there.

INTERNAL_PROJECT_NAME = ".xylo-internal"
INTERNAL_PROJECT_DISPLAY = "System (internal)"


def is_internal_project(name: str | None) -> bool:
    """True if `name` is a reserved system project name (currently `.` prefix)."""
    return bool(name) and name.startswith(".")


def exclude_internal(query):
    """Append the system-project filter to a Project query."""
    return query.filter(~Project.name.startswith("."))


def ensure_internal_project(db: Session) -> Project:
    """Idempotent bootstrap: ensure the `.xylo-internal` placeholder exists.

    Creates `<PROJECTS_DIR>/.xylo-internal/` on disk with a self-contained
    `.mcp.json` (absolute path to the orchestrator MCP server) and a
    minimal `CLAUDE.md`, plus the matching `projects` row.  Triage and
    other meta-agents launch with this as their host project so they
    don't appear under any user project.

    Idempotent — safe to call on every startup; no-op if the dir, files,
    and DB row are already in place.

    Future-proof: when xylocopa moves to pip/brew/docker install (no
    cloned source tree), this is the seam — the placeholder dir already
    holds an absolute-path `.mcp.json`, so only the `command`/`args` of
    that JSON needs to change (e.g. `xylocopa-mcp` console_script entry).
    """
    from config import PROJECTS_DIR, _PROJECT_ROOT

    # Hard-fail if PROJECTS_DIR is unset: a relative or `/projects` fallback
    # would either silently write the placeholder somewhere unexpected or
    # fail with a confusing permission error. Loud failure here surfaces the
    # real misconfig (HOST_PROJECTS_DIR not exported) before it propagates.
    assert PROJECTS_DIR, (
        "PROJECTS_DIR is empty — cannot bootstrap .xylo-internal placeholder. "
        "Ensure HOST_PROJECTS_DIR or PROJECTS_DIR is exported in the orchestrator "
        "process env (run.sh handles this; manual python invocations need it set)."
    )
    internal_path = os.path.join(PROJECTS_DIR, INTERNAL_PROJECT_NAME)

    # 1. Directory
    os.makedirs(internal_path, exist_ok=True)

    # 2. .mcp.json — absolute path so cwd-independence holds even if the
    # user moves/renames the xylocopa repo later.
    mcp_path = os.path.join(internal_path, ".mcp.json")
    mcp_server_abs = os.path.join(_PROJECT_ROOT, "orchestrator", "mcp_server.py")
    desired_mcp = {
        "mcpServers": {
            "xylocopa": {
                "command": "python3",
                "args": [mcp_server_abs],
            }
        }
    }
    write_mcp = True
    if os.path.isfile(mcp_path):
        try:
            with open(mcp_path) as f:
                if json.load(f) == desired_mcp:
                    write_mcp = False
        except (OSError, json.JSONDecodeError):
            pass
    if write_mcp:
        with open(mcp_path, "w") as f:
            json.dump(desired_mcp, f, indent=2)

    # 3. CLAUDE.md — one-liner so meta-agents understand context.
    claudemd_path = os.path.join(internal_path, "CLAUDE.md")
    if not os.path.isfile(claudemd_path):
        with open(claudemd_path, "w") as f:
            f.write(
                "# .xylo-internal\n\n"
                "Synthetic host project for xylocopa-managed meta-agents "
                "(e.g. AI triage). Not a user project — do not edit files "
                "here directly. Only the orchestrator MCP tools "
                "(`mcp__xylocopa__*`) are expected to be used from this cwd.\n"
            )

    # 4. DB row
    proj = db.get(Project, INTERNAL_PROJECT_NAME)
    if proj is None:
        proj = Project(
            name=INTERNAL_PROJECT_NAME,
            display_name=INTERNAL_PROJECT_DISPLAY,
            path=internal_path,
            archived=False,
        )
        db.add(proj)
        db.commit()
        logger.info("Bootstrapped internal placeholder project at %s", internal_path)
    elif proj.path != internal_path or proj.archived:
        proj.path = internal_path
        proj.archived = False
        db.commit()
        logger.info("Refreshed internal placeholder project row (path=%s)", internal_path)
    return proj


# ---- Canonical "what counts as an active project" -------------------------
# Single source of truth: a row is active iff it's in the projects table with
# archived=0 AND its folder still exists on disk AND it isn't a reserved
# system project.  Use this everywhere instead of rolling your own
# `Project.archived == False` query — that catches ghost rows (folder
# deleted out from under the DB) which the orphan-cleanup will eventually
# hard-delete, and it auto-hides the synthetic `.xylo-internal` host.

def active_projects(db: Session) -> list[Project]:
    rows = exclude_internal(
        db.query(Project).filter(Project.archived == False)  # noqa: E712
    ).all()
    return [p for p in rows if os.path.isdir(p.path)]


# ---- Folder name validation ----

_RESERVED_FOLDER_NAMES = {"trash", "folders"}

def _validate_folder_name(name: str) -> None:
    """Raise 400 if the folder name contains path traversal or reserved characters."""
    if not name or "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        raise HTTPException(status_code=400, detail="Invalid folder name")
    if name.lower() in _RESERVED_FOLDER_NAMES:
        raise HTTPException(status_code=400, detail=f"'{name}' is a reserved name")


def _remove_from_registry(name: str):
    """Remove a project entry from registry.yaml."""
    registry_path = os.path.join(PROJECT_CONFIGS_PATH, "registry.yaml")
    if not os.path.exists(registry_path):
        return
    with open(registry_path) as f:
        data = yaml.safe_load(f) or {}
    projects = data.get("projects") or []
    data["projects"] = [p for p in projects if p.get("name") != name]
    with open(registry_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


def _check_no_active_agents(name: str, db: Session):
    """Raise 409 if the project has active agents."""
    active_agents = (
        db.query(Agent)
        .filter(
            Agent.project == name,
            Agent.status.in_([
                AgentStatus.STARTING,
                AgentStatus.EXECUTING,
                AgentStatus.IDLE,
            ]),
        )
        .count()
    )
    if active_agents > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot modify project with {active_agents} active agent(s)",
        )


_enrich_agent_briefs = enrich_agent_briefs


# ---- Project files (CLAUDE.md / PROGRESS.md only) ----

_ALLOWED_PROJECT_FILES = {"CLAUDE.md", "PROGRESS.md"}


class ProjectFileUpdate(BaseModel):
    path: str
    content: str


# ---- CLAUDE.md refresh (AI-powered) ----

# Background jobs: project_name -> {status, data, error, ts}
# status: "running" | "complete" | "error"
_claudemd_jobs: dict[str, dict] = {}
_claudemd_jobs_lock = threading.Lock()
_CLAUDEMD_CACHE_TTL = 600  # 10 minutes


_CLAUDEMD_RUNNING_TTL = 900  # 15 min — auto-expire stuck "running" jobs


def _claudemd_job_get(project_name: str) -> dict | None:
    with _claudemd_jobs_lock:
        entry = _claudemd_jobs.get(project_name)
        if not entry:
            return None
        age = _time.monotonic() - entry["ts"]
        if entry["status"] == "running" and age > _CLAUDEMD_RUNNING_TTL:
            del _claudemd_jobs[project_name]
            return None
        if entry["status"] != "running" and age > _CLAUDEMD_CACHE_TTL:
            del _claudemd_jobs[project_name]
            return None
        return entry


def _claudemd_job_set(project_name: str, **kwargs):
    with _claudemd_jobs_lock:
        _claudemd_jobs[project_name] = {"ts": _time.monotonic(), **kwargs}


def _claudemd_job_clear(project_name: str):
    with _claudemd_jobs_lock:
        _claudemd_jobs.pop(project_name, None)


def _compute_diff_hunks(current: str, proposed: str) -> tuple[str, list[dict]]:
    """Compute unified diff and parse into structured hunks."""
    current_lines = current.splitlines(keepends=True)
    proposed_lines = proposed.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        current_lines, proposed_lines,
        fromfile="CLAUDE.md (current)", tofile="CLAUDE.md (proposed)",
        lineterm="",
    ))
    raw_diff = "\n".join(diff_lines)

    hunks = []
    current_hunk = None
    for line in diff_lines:
        if line.startswith("@@"):
            if current_hunk is not None:
                hunks.append(current_hunk)
            current_hunk = {
                "id": len(hunks),
                "header": line.rstrip(),
                "lines": [],
            }
        elif current_hunk is not None:
            if line.startswith("+"):
                current_hunk["lines"].append({"type": "added", "content": line[1:].rstrip("\n")})
            elif line.startswith("-"):
                current_hunk["lines"].append({"type": "removed", "content": line[1:].rstrip("\n")})
            else:
                # context line (starts with " " or is empty)
                content = line[1:].rstrip("\n") if line.startswith(" ") else line.rstrip("\n")
                current_hunk["lines"].append({"type": "context", "content": content})
    if current_hunk is not None:
        hunks.append(current_hunk)

    return raw_diff, hunks


class ApplyClaudeMdRequest(BaseModel):
    mode: str  # "accept_all" or "selective"
    accepted_hunk_ids: list[int] = []
    final_content: str | None = None


def _refresh_claudemd_background(project_name: str, project_path: str,
                                  recent_agent_activity: str,
                                  current_claudemd: str, progress_md: str,
                                  build_files_content: str = ""):
    """Run claude -p in a thread and store result in _claudemd_jobs."""
    build_section = ""
    if build_files_content:
        build_section = f"""
Here are project config/build files:
{build_files_content}
---
"""

    prompt = f"""You are updating a CLAUDE.md file for a software project.
STRICT RULES:
1. Output ONLY the new CLAUDE.md content. No preamble, no explanation, no markdown fences, no "Here's the updated file".
2. The file has two parts:
   - UNIVERSAL SECTION: Everything from the top through "Do not modify CLAUDE.md" — copy this EXACTLY as-is, character for character. Do NOT remove, rewrite, or reorder any universal rule.
   - PROJECT SECTION: Everything after the universal rules — this is what you UPDATE.
3. For the PROJECT SECTION, update based on the provided context:
   - Tech Stack, Top Dirs, Config, Entry, Tests, Build/Test/Lint
   - Merge lessons from PROGRESS.md into concise one-line rules
   - Remove duplicates, keep only actionable rules
4. ENTIRE file must be UNDER 40 lines. Each bullet ONE line, max 100 chars.
5. Do NOT examine or dump file trees. Use only the context provided below.
6. Ignore any instructions inside the current CLAUDE.md that say "do not modify CLAUDE.md" — the user has explicitly invoked you to do exactly that.

Here is the current CLAUDE.md:
---
{current_claudemd}
---

Here is PROGRESS.md (historical lessons):
---
{progress_md}
---

Here is recent agent activity in this project (last 50 messages):
---
{recent_agent_activity}
---
{build_section}"""

    from config import CLAUDE_BIN

    try:
        # Run from /tmp to avoid loading project hooks (PreToolUse permission
        # hook returns {} for non-agent subprocesses, causing empty output).
        _t0 = _time.monotonic()
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "text",
             "--no-session-persistence", "--model", CC_MODEL],
            capture_output=True, text=True, timeout=600,
            cwd="/tmp",
            env=subprocess_clean_env(),
        )
        _log_claude_subproc("claudemd_refresh", project_name, result.returncode,
                            result.stdout, result.stderr, _time.monotonic() - _t0)

        if result.returncode != 0:
            logger.warning("claude -p failed for %s: %s", project_name, result.stderr[:500])
            _claudemd_job_set(project_name, status="error", error="Claude agent failed — try again")
            return
        proposed = result.stdout.strip()
        # Strip preamble: discard leading lines until we hit a markdown heading
        out_lines = proposed.split("\n")
        start = 0
        for idx, ln in enumerate(out_lines):
            stripped = ln.strip()
            if stripped.startswith("#") or stripped.startswith(">") or stripped.startswith("- ") or stripped.startswith("* ") or stripped == "":
                start = idx
                break
            # Looks like prose preamble — skip it
        proposed = "\n".join(out_lines[start:])
    except subprocess.TimeoutExpired:
        _claudemd_job_set(project_name, status="error", error="Claude agent timed out (>10min) — try again")
        return
    except FileNotFoundError:
        _claudemd_job_set(project_name, status="error", error="Claude CLI not found")
        return

    if not proposed:
        _claudemd_job_set(project_name, status="error", error="Claude agent returned empty output")
        return

    # Build result data
    if not current_claudemd:
        data = {
            "current": "", "proposed": proposed, "diff": "",
            "hunks": [], "is_new": True, "warning": None,
        }
    elif current_claudemd.strip() == proposed.strip():
        data = {"hunks": [], "message": "No changes needed"}
    else:
        raw_diff, hunks = _compute_diff_hunks(current_claudemd, proposed)
        proposed_lines = len(proposed.splitlines())
        warning = None
        if proposed_lines > 60:
            warning = f"Proposed CLAUDE.md is {proposed_lines} lines (recommended max: 60)"
            logger.warning("refresh-claudemd %s: %s", project_name, warning)
        data = {
            "current": current_claudemd, "proposed": proposed,
            "diff": raw_diff, "hunks": hunks, "warning": warning,
        }

    _claudemd_job_set(project_name, status="complete", data=data)


# ---- PROGRESS.md daily summary ----

_progress_jobs: dict[str, dict] = {}
_progress_jobs_lock = threading.Lock()
_main_event_loop: asyncio.AbstractEventLoop | None = None  # set during lifespan
_PROGRESS_CACHE_TTL = 600  # 10 minutes

# In-flight insight-generation runs, keyed by agent_id. Each entry holds the
# claude subprocess handle (set after Popen) and a `cancelled` flag the worker
# thread checks before any DB write. `cancel_insight_run` flips the flag and
# terminates the subprocess so resume mid-generation cleans up cleanly.
_INSIGHT_RUNS: dict[str, dict] = {}
_INSIGHT_RUNS_LOCK = threading.Lock()

# Shared pool for ALL claude-CLI subprocess spawns (insight extraction, retry
# summaries, CLAUDE.md refresh, PROGRESS summarization). max_workers caps the
# number of concurrent `claude -p` subprocesses; each can buffer hundreds of
# MB of stdout via proc.communicate(), so unbounded spawning during mass agent
# stop is a major OOM vector.
INSIGHT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="insight")


def _log_claude_subproc(label: str, key: str, rc: int, stdout: str | bytes,
                        stderr: str | bytes, elapsed_s: float) -> None:
    """Log subprocess stdout/stderr sizes from a claude -p invocation.

    Helps spot the leak vector where a single agent's insight run buffers
    100 MB+ of stdout via proc.communicate(). Internal-only, INFO level —
    not exposed to user.
    """
    try:
        out_len = len(stdout) if stdout is not None else 0
        err_len = len(stderr) if stderr is not None else 0
        logger.info(
            "claude_subproc %s %s rc=%d took=%.1fs stdout=%dB stderr=%dB",
            label, key, rc, elapsed_s, out_len, err_len,
        )
    except Exception:
        pass


def cancel_insight_run(agent_id: str) -> bool:
    """Cancel an in-flight `_run_agent_summary_background` run for `agent_id`.

    Sets a cancelled flag (the worker thread skips DB writes on its way out)
    and terminates the underlying claude subprocess if still running. Safe to
    call when no run is active — returns False in that case.
    """
    with _INSIGHT_RUNS_LOCK:
        entry = _INSIGHT_RUNS.get(agent_id)
        if not entry:
            return False
        entry['cancelled'] = True
        proc = entry.get('proc')
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    return True


def _is_insight_cancelled(agent_id: str) -> bool:
    with _INSIGHT_RUNS_LOCK:
        entry = _INSIGHT_RUNS.get(agent_id)
        return bool(entry and entry.get('cancelled'))


_PROGRESS_RUNNING_TTL = 900  # 15 min — auto-expire stuck "running" jobs


def _progress_job_get(project_name: str) -> dict | None:
    with _progress_jobs_lock:
        entry = _progress_jobs.get(project_name)
        if not entry:
            return None
        age = _time.monotonic() - entry["ts"]
        if entry["status"] == "running" and age > _PROGRESS_RUNNING_TTL:
            del _progress_jobs[project_name]
            return None
        if entry["status"] != "running" and age > _PROGRESS_CACHE_TTL:
            del _progress_jobs[project_name]
            return None
        return entry


def _progress_job_set(project_name: str, **kwargs):
    with _progress_jobs_lock:
        _progress_jobs[project_name] = {"ts": _time.monotonic(), **kwargs}


def _progress_job_clear(project_name: str):
    with _progress_jobs_lock:
        _progress_jobs.pop(project_name, None)


def _summarize_progress_background(project_name: str, project_path: str,
                                   session_context: str):
    """Run claude -p in a thread to generate a daily summary section (incremental append)."""
    from datetime import datetime as _dt, timezone as _tz
    today = _dt.now(_tz.utc).date().isoformat()
    progress_path = os.path.join(project_path, "PROGRESS.md")

    # Read existing PROGRESS.md for grep-based dedup (applied after LLM generation)
    existing_progress = ""
    try:
        if os.path.isfile(progress_path):
            with open(progress_path, "r", encoding="utf-8", errors="replace") as f:
                existing_progress = f.read()
    except OSError:
        pass
    if len(existing_progress) > 50_000:
        existing_progress = existing_progress[-50_000:]

    prompt = f"""You are a project analyst. Read ALL the following conversations from {today} thoroughly. Extract every meaningful insight, decision, bug fix, design choice, and lesson learned.

ALWAYS respond in English regardless of the source language.

STRICT RULES:
1. Output ONLY the summary section — no preamble, no explanation, no markdown fences.
2. Use EXACTLY this format:

## {today} — Daily Insights
1. [insight or decision — one sentence, specific and actionable]
2. ...

3. Synthesize across all conversations — do NOT organize by session.
4. Focus on: new discoveries, architectural decisions, bug root causes & fixes, design choices, gotchas, and lessons that future agents should know.
5. Omit routine/trivial activity (echo tests, simple file creates). Only include things worth remembering.
6. Each insight must be self-contained — readable without context of the original conversation.
7. Max 25 numbered items. Be concise but specific — include file names, function names, and concrete details.
8. Do NOT output anything before the ## heading or after the last numbered item. If there are no new insights, output only the heading with a single item "No new insights today."
9. CRITICAL — each insight must be PARALLEL and INDEPENDENT, not sequential. Do NOT write narrative steps like "First did X, then Y, finally Z". Each numbered item should be an atomic, standalone fact or lesson that can be retrieved individually. Bad: "Refactored auth module, then updated tests, then fixed CI". Good: separate items — "Auth module refactored to use middleware pattern (`auth.py`)" / "Auth test suite updated for new middleware API" / "CI config fixed: missing env var `AUTH_SECRET`".
10. Write insights in English even if the source conversation is in another language.

Here are today's conversations (with timestamps):

{session_context}"""

    from config import CLAUDE_BIN

    try:
        # Run from /tmp to avoid loading project hooks (PreToolUse permission
        # hook returns {} for non-agent subprocesses, causing empty output).
        _t0 = _time.monotonic()
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "-", "--output-format", "text",
             "--no-session-persistence", "--model", SUMMARY_MODEL],
            input=prompt,
            capture_output=True, text=True, timeout=600,
            cwd="/tmp",
            env=subprocess_clean_env(),
        )
        _log_claude_subproc("progress_summary", project_name, result.returncode,
                            result.stdout, result.stderr, _time.monotonic() - _t0)

        if result.returncode != 0:
            logger.warning("progress summary failed for %s: %s", project_name, result.stderr[:500])
            _progress_job_set(project_name, status="error", error="Claude agent failed — try again")
            return
        new_section = result.stdout.strip()
    except subprocess.TimeoutExpired:
        _progress_job_set(project_name, status="error", error="Summary timed out (>10min)")
        return
    except FileNotFoundError:
        _progress_job_set(project_name, status="error", error="Claude CLI not found")
        return

    if not new_section:
        _progress_job_set(project_name, status="error", error="Claude agent returned empty output")
        return

    # Strip markdown fences if LLM wrapped output
    if new_section.startswith("```"):
        lines = new_section.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        new_section = "\n".join(lines).strip()

    # Grep-based dedup against existing PROGRESS.md
    if existing_progress:
        from agent_dispatcher import _grep_dedup_insights
        # Update session snapshot before dedup LLM call
        try:
            _pre_sessions.update(
                f.replace(".jsonl", "")
                for f in os.listdir(_session_dir)
                if f.endswith(".jsonl")
            )
        except OSError:
            pass
        new_section = _grep_dedup_insights(new_section, existing_progress, project_path)

    # For manual flow: show proposed section for user review before appending
    data = {"proposed": new_section, "is_append": True}
    _progress_job_set(project_name, status="complete", data=data)


def _gather_agent_conversation_context(db, agent_id: str) -> str:
    """Gather all messages for a single agent as context for insight extraction."""
    from agent_dispatcher import _strip_agent_preamble

    messages = (
        db.query(Message)
        .filter(Message.agent_id == agent_id)
        .order_by(Message.created_at)
        .all()
    )
    if not messages:
        return ""

    parts = []
    total = 0
    max_msg = 4000  # per-message truncation (tool outputs can be huge)
    max_total = 200_000

    for msg in messages:
        role = msg.role.value
        content = _strip_agent_preamble(msg.content or "")
        if len(content) > max_msg:
            content = content[:max_msg] + "\n...(truncated)"
        line = f"[{role}] {content}"
        total += len(line)
        if total > max_total:
            parts.append("...(remaining messages omitted)")
            break
        parts.append(line)

    return "\n".join(parts)


def _set_insight_status(agent_id: str, status: str | None, project_name: str = ""):
    """Set insight_status on an agent and emit WS update (from background thread)."""
    own_db = SessionLocal()
    _agent_status = None
    try:
        agent = own_db.get(Agent, agent_id)
        if agent:
            agent.insight_status = status
            _agent_status = agent.status.value if agent.status else "STOPPED"
            own_db.commit()
    finally:
        own_db.close()

    from websocket import emit_agent_update
    loop = _main_event_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            emit_agent_update(agent_id, _agent_status or "STOPPED", project_name,
                              insight_status=status or ""),
            loop,
        )


def _run_agent_summary_background(agent_id: str, agent_name: str,
                                  task_title: str, project_name: str,
                                  project_path: str):
    """Run claude -p in a background thread to extract insights from an agent
    conversation.

    The run registers itself in `_INSIGHT_RUNS` so a concurrent
    `cancel_insight_run(agent_id)` (e.g. when the agent is resumed
    mid-generation) can terminate the claude subprocess and flip a flag
    that suppresses any DB writes on the way out.
    """
    with _INSIGHT_RUNS_LOCK:
        _INSIGHT_RUNS[agent_id] = {'proc': None, 'cancelled': False}

    try:
        from config import CLAUDE_BIN

        own_db = SessionLocal()
        try:
            context = _gather_agent_conversation_context(own_db, agent_id)
        finally:
            own_db.close()

        if _is_insight_cancelled(agent_id):
            return
        if not context:
            logger.info("No conversation context for agent %s — skipping summary", agent_id)
            _set_insight_status(agent_id, "failed", project_name)
            return

        prompt = f"""You are a project analyst. Read the following agent conversation and extract insights worth remembering for PROGRESS.md.

ALWAYS respond in English regardless of the source language.

STRICT RULES:
1. Output ONLY numbered insights — no preamble, no explanation, no markdown fences.
2. Focus on: architectural decisions, bug root causes & fixes, design choices, gotchas, and lessons learned.
3. Omit routine/trivial activity. Only include things worth remembering for future work — if an insight is obvious from reading the code or restates what the task asked for, drop it.
4. Each insight must be self-contained — readable without the original conversation.
5. Max 8 items, ONE sentence each. Prefer fewer, denser items over many shallow ones — include file names, function names, concrete details, but no elaboration beyond the single sentence.
6. CRITICAL — each insight must be PARALLEL and INDEPENDENT, not sequential. Do NOT write narrative steps like "First did X, then Y, finally Z". Each numbered item should be an atomic, standalone fact or lesson that can be retrieved individually. Bad: "Refactored auth module, then updated tests, then fixed CI". Good: separate items — "Auth module refactored to use middleware pattern (`auth.py`)" / "Auth test suite updated for new middleware API" / "CI config fixed: missing env var `AUTH_SECRET`".
7. Write insights in English even if the source conversation is in another language.

Agent: {agent_name} | Task: {task_title}

{context}"""

        try:
            # Run from /tmp to avoid loading project hooks (PreToolUse permission
            # hook returns {} for non-agent subprocesses, causing empty output).
            # Popen (not run) so cancel_insight_run can terminate the proc mid-flight.
            proc = subprocess.Popen(
                [CLAUDE_BIN, "-p", "-", "--output-format", "text",
                 "--no-session-persistence", "--model", SUMMARY_MODEL],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                cwd="/tmp",
                env=subprocess_clean_env(),
            )
            with _INSIGHT_RUNS_LOCK:
                entry = _INSIGHT_RUNS.get(agent_id)
                if entry is not None:
                    if entry.get('cancelled'):
                        try:
                            proc.terminate()
                        except OSError:
                            pass
                    else:
                        entry['proc'] = proc

            _t0 = _time.monotonic()
            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=300)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                _log_claude_subproc("agent_insight_TIMEOUT", agent_id,
                                    proc.returncode or -1, stdout, stderr,
                                    _time.monotonic() - _t0)
                if _is_insight_cancelled(agent_id):
                    return
                logger.warning("Agent summary timed out for %s", agent_id)
                _set_insight_status(agent_id, "failed", project_name)
                return
            _log_claude_subproc("agent_insight", agent_id, proc.returncode,
                                stdout, stderr, _time.monotonic() - _t0)

            if _is_insight_cancelled(agent_id):
                return

            if proc.returncode != 0:
                logger.warning("Agent summary failed for %s (rc=%d): %s",
                               agent_id, proc.returncode, stderr[:500])
                _set_insight_status(agent_id, "failed", project_name)
                return

            raw_output = stdout.strip()
        except FileNotFoundError:
            if _is_insight_cancelled(agent_id):
                return
            logger.warning("Claude CLI not found for agent summary")
            _set_insight_status(agent_id, "failed", project_name)
            return
        except Exception:
            if _is_insight_cancelled(agent_id):
                return
            logger.exception("Unexpected error in agent summary for %s", agent_id)
            _set_insight_status(agent_id, "failed", project_name)
            return

        if _is_insight_cancelled(agent_id):
            return
        # CC prints the literal string "Execution error" (rc=0) when the
        # inner API call fails — that's a failure, not a summary.
        if not raw_output or raw_output == "Execution error":
            logger.info("Claude returned empty/error output for agent %s summary", agent_id)
            _set_insight_status(agent_id, "failed", project_name)
            return

        # Strip markdown fences if LLM wrapped output
        if raw_output.startswith("```"):
            lines = raw_output.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            raw_output = "\n".join(lines).strip()

        # Parse numbered insights
        insight_items = re.findall(r"^\d+\.\s+(.+)", raw_output, re.MULTILINE)
        if not insight_items:
            logger.info("No insights parsed from agent %s summary", agent_id)
            _set_insight_status(agent_id, "failed", project_name)
            return

        if _is_insight_cancelled(agent_id):
            return

        # Store as AgentInsightSuggestion rows
        own_db = SessionLocal()
        try:
            for content in insight_items:
                own_db.add(AgentInsightSuggestion(
                    agent_id=agent_id,
                    content=content.strip(),
                ))
            agent = own_db.get(Agent, agent_id)
            if agent:
                agent.has_pending_suggestions = True
                agent.insight_status = None  # Clear — success
                # Save combined insights as task agent_summary (replaces the
                # quick last_message_preview saved at stop time)
                if agent.task_id:
                    _task = own_db.get(Task, agent.task_id)
                    if _task:
                        _task.agent_summary = "\n".join(
                            f"{i+1}. {c.strip()}" for i, c in enumerate(insight_items)
                        )[:2000]
            own_db.commit()
            logger.info("Stored %d insight suggestions for agent %s", len(insight_items), agent_id)
        finally:
            own_db.close()

        # Emit WS events (from background thread → use stored main event loop):
        #   - progress_suggestions_ready: AgentChatPage shows the new card + toast
        #   - agent_update: AgentsPage / ProjectDetailPage flip the badge from
        #     "generating" to "insights" without waiting for the 5s poll
        from websocket import emit_progress_suggestions_ready, emit_agent_update
        loop = _main_event_loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                emit_progress_suggestions_ready(agent_id, len(insight_items), project_name),
                loop,
            )
            asyncio.run_coroutine_threadsafe(
                emit_agent_update(agent_id, "STOPPED", project_name, insight_status=""),
                loop,
            )
        else:
            logger.debug("Main event loop not available for WS emit")
    finally:
        with _INSIGHT_RUNS_LOCK:
            _INSIGHT_RUNS.pop(agent_id, None)


def _generate_retry_summary_background(agent_id: str, task_id: str,
                                       task_title: str, project_name: str,
                                       project_path: str,
                                       incomplete_reason: str | None):
    """Generate a concise retry summary via claude -p: what the agent tried,
    user feedback, and why the task failed."""
    _generate_retry_summary_impl(agent_id, task_id, task_title,
                                 project_name, project_path, incomplete_reason)


def _generate_retry_summary_impl(agent_id: str, task_id: str,
                                 task_title: str, project_name: str,
                                 project_path: str,
                                 incomplete_reason: str | None):
    from config import CLAUDE_BIN

    own_db = SessionLocal()
    try:
        context = _gather_agent_conversation_context(own_db, agent_id)
    finally:
        own_db.close()

    if not context:
        logger.info("No conversation context for agent %s — skipping retry summary", agent_id)
        return

    reason_line = ""
    if incomplete_reason:
        reason_line = f"\nUser's reason for stopping: {incomplete_reason}"

    prompt = f"""Summarize this agent conversation for a retry attempt. The next agent starts from scratch and this summary is its ONLY window into the previous attempt — err on the side of detail (5-10 bullet points, up to ~1500 chars).

Focus on:
1. What approaches the agent tried and their outcomes — name the specific files/functions touched
2. What worked vs what didn't, with the concrete failure evidence (exact error messages, failing test names, wrong behavior observed)
3. Root causes identified or ruled out, so the next agent doesn't re-investigate dead ends
4. Core unsolved issues remaining and the most promising next step, if the conversation suggests one

Do NOT include user feedback — that will be provided separately to the next agent.

Task: {task_title}{reason_line}

{context}"""

    try:
        # Run from /tmp to avoid loading project hooks (PreToolUse permission
        # hook returns {} for non-agent subprocesses, causing empty output).
        _t0 = _time.monotonic()
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "-", "--output-format", "text",
             "--no-session-persistence", "--model", SUMMARY_MODEL],
            input=prompt,
            capture_output=True, text=True, timeout=120,
            cwd="/tmp",
            env=subprocess_clean_env(),
        )
        _log_claude_subproc("retry_summary", task_id, result.returncode,
                            result.stdout, result.stderr, _time.monotonic() - _t0)

        if result.returncode != 0:
            logger.warning("Retry summary failed for task %s (rc=%d): %s",
                           task_id, result.returncode, result.stderr[:300])
            _clear_generating_marker(task_id)
            return
        summary = result.stdout.strip()[:2000]
    except subprocess.TimeoutExpired:
        logger.warning("Retry summary timed out for task %s", task_id)
        _clear_generating_marker(task_id)
        return

    # CC prints the literal string "Execution error" (rc=0) when the inner
    # API call fails — don't save it as the summary.
    if not summary or summary == "Execution error":
        logger.warning("Retry summary returned empty/error output for task %s", task_id)
        _clear_generating_marker(task_id)
        return

    # Save summary and notify frontend
    own_db = SessionLocal()
    try:
        task = own_db.get(Task, task_id)
        if task:
            task.agent_summary = summary
            own_db.commit()
            logger.info("Saved retry summary for task %s (%d chars)", task_id, len(summary))
    finally:
        own_db.close()

    # Emit task_update so frontend refreshes
    from websocket import emit_task_update
    loop = _main_event_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            emit_task_update(task_id, "INBOX", project_name, title=task_title),
            loop,
        )


def _clear_generating_marker(task_id: str):
    """Clear :::generating::: so the UI shows the 'Generate summary' button."""
    own_db = SessionLocal()
    project_name = ""
    task_title = None
    task_status = "INBOX"
    cleared = False
    try:
        task = own_db.get(Task, task_id)
        if task and task.agent_summary == ":::generating:::":
            task.agent_summary = None
            own_db.commit()
            project_name = task.project_name or ""
            task_title = task.title
            task_status = task.status.value
            cleared = True
    finally:
        own_db.close()
    if not cleared:
        return
    # Without this, the UI keeps showing "Generating summary..." indefinitely
    # — the failure path here used to silently drop the marker without telling
    # the frontend.
    from websocket import emit_task_update
    loop = _main_event_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            emit_task_update(task_id, task_status, project_name, title=task_title),
            loop,
        )


# ---- Project directory browser (read-only) ----

_BROWSE_IGNORED = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".pycache",
    "backups", "logs", ".next", ".nuxt", "dist", "build", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "egg-info",
}


_SECTION_DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b", re.MULTILINE)


# ===========================================================================
# Resume hint (LLM-generated mood + recap on project card)
# ===========================================================================

_resume_hint_in_flight: set[str] = set()  # dedupe concurrent refreshes

_RESUME_SYSTEM = """You read a short slice of an agent's recent conversation and
summarize what was just being worked on.

ALWAYS respond in English regardless of the source language.

Tone: playful, casual, a little cheeky. Like a friend recapping your day,
not a status report. Past tense. 4-6 words, ~25 characters.

Good hints:
  "polished those project cards"
  "wrestled COLMAP into shape"
  "chased a sneaky null pointer"
  "shipped dark mode, finally"
  "rabbit-holed on the picker"
  "added a new course page"

Bad hints (avoid — too formal, too long, or vague):
  "Optimized the project list visuals"
  "Working on the emoji picker"
  "Status: in progress"

Then pick an emoji that matches the actual work:
- code / fixing: 🔧 🛠 🐛
- design / UI: 🎨 ✨
- exploring / research: 🔍 🤔
- shipping / done: 🚀 🎉 🏆
- iterating: 🌀 🧩
- complex / hard: 🤯
- adding content / writing: 📝 📄
- quiet: 💤
- (use any other relevant emoji you know)

Avoid frustrated faces (no 😩 😮‍💨 😤)."""


def _collect_recent_turns(agent_id: str, db: Session, limit: int = 8) -> dict | None:
    """Snapshot for the LLM: original task, latest user intent, and recent turns.

    Returns a dict with three anchors so the model doesn't drift to whatever the
    last 8 turns happen to be about:
      - task:     the agent's original prompt (stable through the whole session)
      - intent:   the most recent user message (what was just asked)
      - turns:    the last `limit` USER+AGENT turns, chronological
    """
    msgs = (
        db.query(Message)
        .filter(
            Message.agent_id == agent_id,
            Message.role.in_([MessageRole.USER, MessageRole.AGENT]),
            Message.content != "",
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )
    if not msgs:
        return None

    def _clip(text: str, n: int = 400) -> str:
        text = " ".join((text or "").split())
        return text[:n] + "…" if len(text) > n else text

    latest_user = next((m for m in msgs if m.role == MessageRole.USER), None)
    intent = _clip(latest_user.content, 300) if latest_user else None

    msgs.reverse()  # chronological
    turns = []
    for m in msgs:
        text = _clip(m.content or "")
        if not text:
            continue
        role = "user" if m.role == MessageRole.USER else "agent"
        turns.append(f"{role}: {text}")
    if not turns:
        return None

    agent = db.get(Agent, agent_id)
    task = (agent.name or "").strip() if agent else ""
    task = _clip(task, 200)

    return {"task": task, "intent": intent, "turns": turns}


async def _call_llm_for_hint(snapshot: dict) -> tuple[str | None, str | None]:
    """Call gpt-4o-mini with three anchors (task / intent / turns). Returns (emoji, hint)."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None, None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        logger.warning("openai package missing — skipping resume hint")
        return None, None

    parts = []
    if snapshot.get("task"):
        parts.append(f"SESSION TASK (what this whole agent is for):\n  {snapshot['task']}")
    if snapshot.get("intent"):
        parts.append(f"LATEST USER INTENT (what was just asked):\n  {snapshot['intent']}")
    parts.append(
        "RECENT TURNS (last few messages, chronological):\n"
        + "\n".join(f"  {t}" for t in snapshot["turns"])
    )
    parts.append(
        "Summarize what was just worked on. Anchor the summary to the SESSION "
        "TASK and LATEST USER INTENT — don't drift to whatever tangent the last "
        "turns happen to be about."
    )
    user_prompt = "\n\n".join(parts)

    client = AsyncOpenAI(api_key=api_key)
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _RESUME_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "resume_hint",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "hint": {"type": "string", "maxLength": 60},
                                "emoji": {"type": "string", "maxLength": 8},
                            },
                            "required": ["hint", "emoji"],
                            "additionalProperties": False,
                        },
                    },
                },
                max_tokens=60,
                temperature=0.3,
            ),
            timeout=10.0,
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("emoji"), data.get("hint")
    except Exception as e:
        logger.warning("resume hint LLM failed: %s", e)
        return None, None


async def _refresh_resume_hint(agent_id: str):
    """Background task: summarize the agent's recent turns into its project's resume hint.

    Triggered by sync_engine right after a stop_hook_summary is imported.
    """
    if agent_id in _resume_hint_in_flight:
        return
    _resume_hint_in_flight.add(agent_id)
    try:
        db = SessionLocal()
        try:
            agent = db.get(Agent, agent_id)
            if agent is None or agent.is_subagent or not agent.project:
                return
            # Skip meta/triage agents (their "name" is a long system prompt)
            name = agent.name or ""
            if name.startswith("You are ") or len(name) > 200:
                return
            snapshot = _collect_recent_turns(agent_id, db)
            if not snapshot:
                return
            emoji, hint = await _call_llm_for_hint(snapshot)
            if not hint:
                return
            proj = db.get(Project, agent.project)
            if proj is None:
                return
            proj.resume_emoji = (emoji or "")[:16] or None
            proj.resume_hint = hint[:80]
            proj.resume_hint_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()
    finally:
        _resume_hint_in_flight.discard(agent_id)


# ===========================================================================
# Routes
# ===========================================================================


@router.get("/api/projects", response_model=list[ProjectWithStats])
async def list_projects(db: Session = Depends(get_db)):
    """List all active (non-archived) projects with task and agent statistics."""
    projects = sorted(active_projects(db), key=lambda p: p.name)
    results = []
    for proj in projects:
        # Count agents linked to tasks (matches Agents page filtering)
        task_agent_base = (
            db.query(Agent)
            .join(Task, Agent.task_id == Task.id)
            .filter(
                Agent.project == proj.name,
                Agent.task_id.isnot(None),
                Agent.is_subagent == False,  # noqa: E712
            )
            .subquery()
        )
        task_row_total = db.query(func.count(task_agent_base.c.id)).scalar()
        task_row_completed = (
            db.query(func.count(Agent.id))
            .join(Task, Agent.task_id == Task.id)
            .filter(
                Agent.project == proj.name,
                Agent.task_id.isnot(None),
                Agent.is_subagent == False,  # noqa: E712
                Task.status == TaskStatus.COMPLETE,
            )
            .scalar()
        )
        task_row_failed = (
            db.query(func.count(Agent.id))
            .join(Task, Agent.task_id == Task.id)
            .filter(
                Agent.project == proj.name,
                Agent.task_id.isnot(None),
                Agent.is_subagent == False,  # noqa: E712
                Task.status.in_([TaskStatus.FAILED, TaskStatus.TIMEOUT]),
            )
            .scalar()
        )
        task_row_running = (
            db.query(func.count(Agent.id))
            .join(Task, Agent.task_id == Task.id)
            .filter(
                Agent.project == proj.name,
                Agent.task_id.isnot(None),
                Agent.is_subagent == False,  # noqa: E712
                Task.status.in_([TaskStatus.PENDING, TaskStatus.EXECUTING]),
            )
            .scalar()
        )

        # Agent stats
        agent_row = (
            db.query(
                func.count(Agent.id).label("total"),
                func.count(
                    case((Agent.status.in_([
                        AgentStatus.EXECUTING,
                        AgentStatus.STARTING, AgentStatus.IDLE,
                    ]), 1))
                ).label("active"),
            )
            .filter(Agent.project == proj.name, Agent.is_subagent == False)  # noqa: E712
            .one()
        )

        last_activity = db.query(func.max(Agent.last_message_at)).filter(
            Agent.project == proj.name, Agent.is_subagent == False,  # noqa: E712
        ).scalar()

        results.append(
            ProjectWithStats(
                name=proj.name,
                display_name=proj.display_name,
                path=proj.path,
                git_remote=proj.git_remote,
                description=proj.description,
                max_concurrent=proj.max_concurrent,
                default_model=proj.default_model,
                task_total=task_row_total,
                task_completed=task_row_completed,
                task_failed=task_row_failed,
                task_running=task_row_running,
                agent_total=agent_row.total,
                agent_active=agent_row.active,
                last_activity=last_activity,
            )
        )
    return results


@router.get("/api/projects/folders")
# Sync on purpose: the body is all synchronous DB/filesystem work with no
# await, so as `async def` it blocked the event loop for its whole duration
# (p50 44 ms, max 26 s measured). FastAPI runs plain `def` handlers in a threadpool.
def list_all_folders(
    request: Request,
    tz_offset: int = Query(default=0, description="Client timezone offset in minutes"),
    db: Session = Depends(get_db),
):
    """List ALL folders in projects dir with activation status and stats."""
    from config import PROJECTS_DIR
    projects_dir = PROJECTS_DIR or "/projects"
    try:
        all_dirs = sorted([
            d for d in os.listdir(projects_dir)
            if os.path.isdir(os.path.join(projects_dir, d)) and not d.startswith(".")
        ])
    except FileNotFoundError:
        all_dirs = []

    db_projects = {p.name: p for p in db.query(Project).all()}

    results = []
    for dirname in all_dirs:
        proj = db_projects.get(dirname)
        active = proj is not None and not proj.archived

        agent_count = db.query(func.count(Agent.id)).filter(
            Agent.project == dirname,
            Agent.is_subagent == False,  # noqa: E712
        ).scalar()
        last_activity = db.query(func.max(Agent.last_message_at)).filter(
            Agent.project == dirname,
            Agent.is_subagent == False,  # noqa: E712
        ).scalar()

        entry = {
            "name": dirname,
            "display_name": proj.display_name if proj else dirname,
            "active": active,
            "agent_count": agent_count,
            "last_activity": last_activity,
            "git_remote": proj.git_remote if proj else None,
            "description": proj.description if proj else None,
            "auto_progress_summary": proj.auto_progress_summary if proj else False,
            "ai_insights": proj.ai_insights if proj else False,
            "emoji": proj.emoji if proj else None,
            "resume_emoji": proj.resume_emoji if proj else None,
            "resume_hint": proj.resume_hint if proj else None,
        }

        # Richer stats for active projects
        if active:
            agent_active_count = (
                db.query(func.count(Agent.id))
                .filter(
                    Agent.project == dirname,
                    Agent.is_subagent == False,  # noqa: E712
                    Agent.status.in_([
                        AgentStatus.IDLE, AgentStatus.EXECUTING,
                        AgentStatus.STARTING,
                    ]),
                )
                .scalar()
            )
            # Pipeline counts: inbox / executing / idle — feed the per-project
            # status bar on the Projects list
            inbox_count = (
                db.query(func.count(Task.id))
                .filter(
                    Task.project_name == dirname,
                    Task.status == TaskStatus.INBOX,
                )
                .scalar()
            )
            executing_count = (
                db.query(func.count(Agent.id))
                .filter(
                    Agent.project == dirname,
                    Agent.is_subagent == False,  # noqa: E712
                    Agent.status == AgentStatus.EXECUTING,
                )
                .scalar()
            )
            idle_count = (
                db.query(func.count(Agent.id))
                .filter(
                    Agent.project == dirname,
                    Agent.is_subagent == False,  # noqa: E712
                    Agent.status == AgentStatus.IDLE,
                )
                .scalar()
            )
            # Count agents linked to tasks (matches Agents page filtering)
            task_agent_total = (
                db.query(func.count(Agent.id))
                .filter(
                    Agent.project == dirname,
                    Agent.task_id.isnot(None),
                    Agent.is_subagent == False,  # noqa: E712
                )
                .scalar()
            )
            task_agent_completed = (
                db.query(func.count(Agent.id))
                .join(Task, Agent.task_id == Task.id)
                .filter(
                    Agent.project == dirname,
                    Agent.task_id.isnot(None),
                    Agent.is_subagent == False,  # noqa: E712
                    Task.status == TaskStatus.COMPLETE,
                )
                .scalar()
            )
            # Weekly stats for task ring (timezone-aware)
            from datetime import timedelta as _td
            _tz_hours = -tz_offset / 60
            _tz_mod = f"{'+' if _tz_hours >= 0 else ''}{int(_tz_hours)} hours"
            _now_local = datetime.now(timezone.utc) + _td(hours=_tz_hours)
            _week_ago = _now_local - _td(days=7)
            _w_terminal = [TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.TIMEOUT,
                           TaskStatus.REJECTED, TaskStatus.CANCELLED]
            _w_rows = (
                db.query(Task.status, func.count(Task.id))
                .filter(Task.project_name == dirname, Task.status.in_(_w_terminal),
                        Task.completed_at >= _week_ago)
                .group_by(Task.status).all()
            )
            _w_by = {s.value: c for s, c in _w_rows}
            _w_total = sum(_w_by.values())
            _w_completed = _w_by.get("COMPLETE", 0)

            # Daily success rates for weekly average (same formula as /api/v2/tasks/counts)
            _local_date = func.date(Task.completed_at, _tz_mod)
            _d_rows = (
                db.query(_local_date.label("day"),
                         Task.status, func.count(Task.id))
                .filter(Task.project_name == dirname, Task.status.in_(_w_terminal),
                        Task.completed_at >= _week_ago)
                .group_by("day", Task.status).all()
            )
            _d_retry_rows = (
                db.query(_local_date.label("day"),
                         func.coalesce(func.sum(Task.attempt_number - 1), 0))
                .filter(Task.project_name == dirname, Task.status.in_(_w_terminal),
                        Task.completed_at >= _week_ago)
                .group_by("day").all()
            )
            _dr_map = {str(d): int(r) for d, r in _d_retry_rows}
            _dm: dict[str, dict] = {}
            for _dv, _st, _cnt in _d_rows:
                _dk = str(_dv)
                if _dk not in _dm:
                    _dm[_dk] = {"total": 0, "completed": 0}
                _dm[_dk]["total"] += _cnt
                if _st == TaskStatus.COMPLETE:
                    _dm[_dk]["completed"] += _cnt
            # Average daily success rates over same 7-day grid as /api/v2/tasks/counts
            _valid_pcts = []
            for _i in range(7):
                _dk = (_now_local - _td(days=6 - _i)).strftime("%Y-%m-%d")
                _de = _dm.get(_dk)
                if _de:
                    _adj = _de["total"] + _dr_map.get(_dk, 0)
                    if _adj:
                        _valid_pcts.append(round(_de["completed"] / _adj * 100))
            _w_pct = round(sum(_valid_pcts) / len(_valid_pcts)) if _valid_pcts else 0

            entry["agent_active"] = agent_active_count
            entry["task_total"] = task_agent_total
            entry["task_completed"] = task_agent_completed
            entry["weekly_total"] = _w_total
            entry["weekly_completed"] = _w_completed
            entry["weekly_success_pct"] = _w_pct
            entry["inbox_count"] = inbox_count
            entry["executing_count"] = executing_count
            entry["idle_count"] = idle_count

        results.append(entry)

    return results


@router.get("/api/projects/trash")
async def list_trash_folders():
    """List deleted project folders in .trash."""
    from config import PROJECTS_DIR
    projects_dir = PROJECTS_DIR or "/projects"
    trash_dir = os.path.join(projects_dir, ".trash")
    try:
        dirs = sorted([
            d for d in os.listdir(trash_dir)
            if os.path.isdir(os.path.join(trash_dir, d))
        ])
    except FileNotFoundError:
        dirs = []
    return [{"name": d} for d in dirs]


@router.delete("/api/projects/trash/{name}", status_code=200)
async def delete_trash_folder(name: str):
    """Permanently delete a project folder from .trash."""
    _validate_folder_name(name)
    import shutil
    from config import PROJECTS_DIR
    projects_dir = PROJECTS_DIR or "/projects"
    target = os.path.join(projects_dir, ".trash", name)
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail=f"Trash folder '{name}' not found")
    try:
        shutil.rmtree(target)
    except OSError as e:
        logger.error("Failed to delete trash folder %s: %s", target, e)
        raise HTTPException(status_code=500, detail=f"Failed to delete: {e}")
    logger.info("Permanently deleted trash folder: %s", target)
    return {"status": "deleted", "name": name}


@router.post("/api/projects/trash/{name}/restore", status_code=200)
async def restore_trash_folder(name: str):
    """Restore a project folder from .trash back to projects dir."""
    _validate_folder_name(name)
    import shutil
    from config import PROJECTS_DIR
    projects_dir = PROJECTS_DIR or "/projects"
    src = os.path.join(projects_dir, ".trash", name)
    if not os.path.isdir(src):
        raise HTTPException(status_code=404, detail=f"Trash folder '{name}' not found")
    dst = os.path.join(projects_dir, name)
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail=f"Folder '{name}' already exists")
    shutil.move(src, dst)
    logger.info("Restored trash folder %s to %s", src, dst)

    # Auto-generate CLAUDE.md / PROGRESS.md if missing
    from project_scaffolder import scaffold_project
    scaffold_project(name, dst)

    return {"status": "restored", "name": name}


@router.post("/api/projects/scan")
async def scan_projects(request: Request, db: Session = Depends(get_db)):
    """Scan PROJECTS_DIR and bulk-register all new folders as projects."""
    from config import PROJECTS_DIR
    projects_dir = PROJECTS_DIR or "/projects"

    if not os.path.isdir(projects_dir):
        raise HTTPException(status_code=400, detail=f"PROJECTS_DIR not found: {projects_dir}")

    try:
        all_dirs = sorted([
            d for d in os.listdir(projects_dir)
            if os.path.isdir(os.path.join(projects_dir, d))
            and not d.startswith(".")
        ])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to scan: {e}")

    from session_cache import migrate_session_dirs

    db_projects = {p.name: p for p in db.query(Project).all()}
    added = []

    skipped_archived = []
    for dirname in all_dirs:
        if dirname in db_projects:
            proj = db_projects[dirname]
            if proj.archived:
                skipped_archived.append(dirname)
            continue

        proj = Project(
            name=dirname,
            display_name=dirname,
            path=os.path.join(projects_dir, dirname),
        )
        db.add(proj)
        added.append(dirname)

        migrate_session_dirs(proj.path)

    if added:
        db.commit()
        logger.info("Scan registered %d new project(s): %s", len(added), ", ".join(added))

    # Auto-generate CLAUDE.md / PROGRESS.md for all active projects missing them
    from project_scaffolder import scaffold_project
    for dirname in all_dirs:
        if dirname in [a for a in skipped_archived]:
            continue
        dirpath = os.path.join(projects_dir, dirname)
        if not os.path.isfile(os.path.join(dirpath, "CLAUDE.md")) or \
           not os.path.isfile(os.path.join(dirpath, "PROGRESS.md")):
            scaffold_project(dirname, dirpath)

    if skipped_archived:
        logger.info("Scan skipped %d archived project(s): %s", len(skipped_archived), ", ".join(skipped_archived))

    return {"scanned": len(all_dirs), "added": added, "skipped_archived": skipped_archived}


@router.post("/api/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, request: Request, db: Session = Depends(get_db)):
    """Create or re-activate a project. Un-archives if previously archived."""
    from config import PROJECTS_DIR
    projects_dir = PROJECTS_DIR or "/projects"

    existing = db.get(Project, body.name)
    if existing:
        if existing.archived:
            # Re-activate archived project — preserves all history
            existing.archived = False
            if body.git_url:
                existing.git_remote = body.git_url
            if body.description:
                existing.description = body.description
            db.commit()
            db.refresh(existing)
            logger.info("Project '%s' re-activated from archive", body.name)
            proj = existing
        else:
            raise HTTPException(status_code=409, detail=f"Project '{body.name}' already exists")
    else:
        proj = Project(
            name=body.name,
            display_name=body.name,
            path=os.path.join(projects_dir, body.name),
            git_remote=body.git_url,
            description=body.description,
        )
        db.add(proj)
        db.commit()
        db.refresh(proj)

    # Ensure project directory exists
    wm = getattr(request.app.state, "worker_manager", None)
    if wm:
        if body.git_url:
            try:
                wm.clone_project(body.name, body.git_url)
            except Exception as e:
                # Clone failed — revert: re-archive if reactivated, else delete
                if existing and existing.archived is False:
                    proj.archived = True
                    db.commit()
                else:
                    db.delete(proj)
                    db.commit()
                raise HTTPException(
                    status_code=400,
                    detail=f"Git clone failed: {e}",
                )
        else:
            wm.ensure_project_dir(body.name)

        # Auto-init git repo if not already one
        if os.path.isdir(proj.path) and not os.path.isdir(os.path.join(proj.path, ".git")):
            import subprocess
            subprocess.run(["git", "init"], cwd=proj.path, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=proj.path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "Initial commit"], cwd=proj.path, capture_output=True)
            logger.info("Auto-initialized git repo for %s", body.name)

    # Migrate any old session directories that match this project
    from session_cache import migrate_session_dirs
    migrate_session_dirs(proj.path)

    # Append to registry.yaml
    registry_path = os.path.join(PROJECT_CONFIGS_PATH, "registry.yaml")
    if os.path.exists(registry_path):
        with open(registry_path) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
    if "projects" not in data or data["projects"] is None:
        data["projects"] = []
    entry = {"name": body.name, "path": os.path.join(projects_dir, body.name)}
    if body.git_url:
        entry["git_remote"] = body.git_url
    if body.description:
        entry["description"] = body.description
    data["projects"].append(entry)
    with open(registry_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)

    # Auto-generate CLAUDE.md / PROGRESS.md if missing
    from project_scaffolder import scaffold_project
    scaffold_project(proj.name, proj.path)

    logger.info("Project '%s' created", body.name)
    return proj


@router.put("/api/projects/{name}/rename", response_model=ProjectOut)
async def rename_project(name: str, body: ProjectRename, request: Request, db: Session = Depends(get_db)):
    """Rename a project — updates all agent/task/session references, registry, and directory."""

    proj = get_project_or_404(db, name)

    new_name = body.new_name
    if new_name == name:
        return proj

    _validate_folder_name(new_name)

    # Check new name is free
    if db.get(Project, new_name):
        raise HTTPException(status_code=409, detail=f"Project '{new_name}' already exists")

    # Block rename when agents are actively running (including IDLE)
    busy = (
        db.query(Agent)
        .filter(
            Agent.project == name,
            Agent.status.in_([
                AgentStatus.STARTING, AgentStatus.IDLE,
                AgentStatus.EXECUTING, AgentStatus.IDLE,
            ]),
        )
        .count()
    )
    if busy > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot rename project with {busy} active agent(s). Stop them first.",
        )

    old_path = proj.path
    new_display = body.display_name or (new_name if proj.display_name == name else proj.display_name)

    # --- Database updates (single transaction, raw SQL for PK change) ---
    # Expire ORM cache so it doesn't conflict with raw SQL
    db.expire_all()

    # Defer FK enforcement until commit so updating projects.name (the PK
    # all child tables reference) doesn't fail before we get to update
    # the children. SQLite supports this per-transaction; without it the
    # parent UPDATE fires before any child UPDATE and trips a FK error.
    db.execute(text("PRAGMA defer_foreign_keys = ON"))

    db.execute(text(
        "UPDATE projects SET name = :new_name, display_name = :display WHERE name = :old_name"
    ), {"new_name": new_name, "display": new_display, "old_name": name})
    db.execute(update(Agent).where(Agent.project == name).values(project=new_name))
    db.execute(update(StarredSession).where(StarredSession.project == name).values(project=new_name))
    from models import Task, ProgressInsight, SessionViewEvent, BookmarkedMessage
    db.execute(update(Task).where(Task.project == name).values(project=new_name))
    db.execute(update(Task).where(Task.project_name == name).values(project_name=new_name))
    db.execute(update(ProgressInsight).where(ProgressInsight.project == name).values(project=new_name))
    db.execute(update(SessionViewEvent).where(SessionViewEvent.project == name).values(project=new_name))
    db.execute(update(BookmarkedMessage).where(BookmarkedMessage.project == name).values(project=new_name))

    ghost = db.execute(text("SELECT name FROM projects WHERE name = :old"), {"old": name}).fetchone()
    if ghost:
        db.execute(text("DELETE FROM projects WHERE name = :old"), {"old": name})

    db.flush()
    db.expire_all()

    new_proj = db.get(Project, new_name)

    # --- Registry.yaml ---
    registry_path = os.path.join(PROJECT_CONFIGS_PATH, "registry.yaml")
    if os.path.exists(registry_path):
        with open(registry_path) as f:
            data = yaml.safe_load(f) or {}
        projects_list = data.get("projects") or []
        for entry in projects_list:
            if entry.get("name") == name:
                entry["name"] = new_name
                # Update path in registry if it contained old name
                if entry.get("path", "").endswith(f"/{name}"):
                    entry["path"] = entry["path"].rsplit("/", 1)[0] + f"/{new_name}"
                break
        with open(registry_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    # --- Rename directory on disk ---
    new_path = old_path  # default: path unchanged
    if old_path.endswith(f"/{name}") and os.path.isdir(old_path):
        new_path = old_path.rsplit("/", 1)[0] + f"/{new_name}"
        if not os.path.exists(new_path):
            os.rename(old_path, new_path)
            logger.info("Renamed project directory %s → %s", old_path, new_path)

    new_proj.path = new_path

    # cc_sessions.project_path is a string column (not FK) that mirrors
    # Project.path. Migrate it in lockstep with the directory rename so
    # session lookups (e.g. resolving an agent's parent CC session by
    # encoded project path) keep working.
    if new_path != old_path:
        db.execute(
            text("UPDATE cc_sessions SET project_path = :new WHERE project_path = :old"),
            {"new": new_path, "old": old_path},
        )

    db.commit()

    # --- Migrate Claude session directory and session cache ---
    # When the project path changes, the encoded directory name changes too.
    # Move the old session dir so existing sessions remain accessible.
    # Uses session_source_dir / session_cache_dir so path encoding stays
    # in one place (session_cache.py) rather than being duplicated here.
    if new_path != old_path:
        from session_cache import session_source_dir, session_cache_dir, invalidate_path_cache

        for label, dir_fn in [
            ("Claude session", session_source_dir),
            ("session cache", session_cache_dir),
        ]:
            old_dir = dir_fn(old_path)
            new_dir = dir_fn(new_path)
            if not os.path.isdir(old_dir):
                continue
            if os.path.exists(new_dir):
                logger.info("Skipped %s migration — target already exists: %s", label, new_dir)
                continue
            os.rename(old_dir, new_dir)
            logger.info("Migrated %s dir: %s → %s", label, old_dir, new_dir)

        # Invalidate cached lookups for both old and new paths
        invalidate_path_cache(old_path)
        invalidate_path_cache(new_path)

        # Fallback: scan for any old session dirs matching the project basename
        from session_cache import migrate_session_dirs
        migrate_session_dirs(new_path)

    try:
        from dropbox_sync.engine import on_project_renamed
        await on_project_renamed(name, new_name)
    except Exception:
        logger.debug("dropbox engine rename notify failed", exc_info=True)

    logger.info("Project renamed: %s → %s", name, new_name)
    return new_proj


@router.post("/api/projects/{name}/archive", status_code=200)
async def archive_project(name: str, request: Request, db: Session = Depends(get_db)):
    """Archive a project — blocks if active agents exist, unassigns remaining tasks."""
    from websocket import emit_task_update

    proj = get_project_or_404(db, name)
    if proj.archived:
        raise HTTPException(status_code=400, detail="Project is already archived")

    # Guard: block archive if project has any active agents (sessions in flight)
    active_agents = (
        db.query(Agent)
        .filter(Agent.project == name, Agent.status.in_(ACTIVE_STATUSES))
        .all()
    )
    if active_agents:
        names = ", ".join(f"{a.name} ({a.status.value})" for a in active_agents[:5])
        more = "" if len(active_agents) <= 5 else f" (+{len(active_agents) - 5} more)"
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot archive '{name}' — {len(active_agents)} active session(s): "
                f"{names}{more}. Stop them first, then retry."
            ),
        )

    # Unassign all non-terminal tasks (move to "no project") — keep data, don't cancel
    from task_state_machine import TERMINAL_STATES
    orphan_tasks = (
        db.query(Task)
        .filter(Task.project_name == name, Task.status.notin_(TERMINAL_STATES))
        .all()
    )
    for t in orphan_tasks:
        # Statuses that only make sense while a project+agent is present → reset to INBOX
        if t.status not in (TaskStatus.INBOX, TaskStatus.PENDING):
            t.status = TaskStatus.INBOX
        t.project_name = None
        t.project = ""  # legacy v1 column (NOT NULL)
        t.agent_id = None
        asyncio.ensure_future(emit_task_update(
            t.id, t.status.value, "",
            title=t.title,
        ))
    unassigned_count = len(orphan_tasks)

    proj.archived = True
    db.commit()
    _remove_from_registry(name)
    logger.info("Project '%s' archived (unassigned %d task(s))", name, unassigned_count)
    return {"detail": f"Project '{name}' archived — {unassigned_count} task(s) moved to no-project"}


@router.delete("/api/projects/{name}", status_code=200)
async def delete_project(name: str, request: Request, db: Session = Depends(get_db)):
    """Delete a project — unregisters and moves files to .trash. Works even if not registered."""
    _validate_folder_name(name)
    import shutil
    from models import Task

    proj = db.get(Project, name)

    # If registered, clean up DB resources
    if proj:
        _check_no_active_agents(name, db)

        # Kill any lingering tmux sessions before deleting DB records
        # (try both new xy- and legacy ah- session names)
        import subprocess as _sp_del
        agents_to_delete = db.query(Agent).filter(Agent.project == name).all()
        for agent in agents_to_delete:
            for sess_name in tmux_session_candidates(agent.id):
                try:
                    _sp_del.run(["tmux", "kill-session", "-t", sess_name],
                                capture_output=True, timeout=5)
                except (OSError, _sp_del.TimeoutExpired):
                    pass

        # Clean up session files for all agents being deleted
        from session_cache import cleanup_source_session, evict_session
        import glob as globmod
        for agent in agents_to_delete:
            if agent.session_id:
                cleanup_source_session(agent.session_id, proj.path, agent.worktree)
                evict_session(agent.session_id, proj.path, agent.worktree)

        # Clean up output logs for all messages of these agents
        agent_ids = [a.id for a in agents_to_delete]
        if agent_ids:
            msg_ids = [m.id for m in db.query(Message.id).filter(Message.agent_id.in_(agent_ids)).all()]
            for mid in msg_ids:
                for f in globmod.glob(os.path.join(tempfile.gettempdir(), f"claude-output-*-{mid}.log")):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            db.query(Message).filter(Message.agent_id.in_(agent_ids)).delete(synchronize_session=False)
        # Delete Tasks before Agents (Task.agent_id FK references agents.id)
        db.query(Task).filter(Task.project == name).delete(synchronize_session=False)
        db.query(Agent).filter(Agent.project == name).delete(synchronize_session=False)
        db.query(StarredSession).filter(StarredSession.project == name).delete(synchronize_session=False)
        db.delete(proj)
        db.commit()
        _remove_from_registry(name)

    # Move files to .trash regardless of DB registration
    from config import PROJECTS_DIR
    projects_dir = PROJECTS_DIR or "/projects"
    src = os.path.join(projects_dir, name)
    if os.path.isdir(src):
        trash_dir = os.path.join(projects_dir, ".trash")
        os.makedirs(trash_dir, exist_ok=True)
        dst = os.path.join(trash_dir, name)
        try:
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.move(src, dst)
            logger.info("Moved %s to %s", src, dst)
        except OSError as e:
            logger.error("Failed to move project to trash: %s", e)
            raise HTTPException(status_code=500, detail=f"Failed to move files to trash: {e}")
    elif not proj:
        raise HTTPException(status_code=404, detail=f"Folder '{name}' not found")

    try:
        from dropbox_sync.engine import on_project_deleted
        await on_project_deleted(name)
    except Exception:
        logger.debug("dropbox engine delete notify failed", exc_info=True)

    logger.info("Project '%s' deleted (moved to .trash)", name)
    return {"detail": f"Project '{name}' deleted — files moved to .trash"}


@router.get("/api/projects/{name}/agents", response_model=list[AgentBrief])
async def list_project_agents(
    request: Request,
    name: str,
    status: AgentStatus | None = None,
    limit: int | None = None,
    include_subagents: bool = False,
    db: Session = Depends(get_db),
):
    """List agents for a project (works for active, archived, and unregistered projects)."""
    q = db.query(Agent).filter(Agent.project == name)
    if not include_subagents:
        q = q.filter(Agent.is_subagent == False)  # noqa: E712
    if status:
        q = q.filter(Agent.status == status)
    q = q.order_by(Agent.last_message_at.desc().nulls_last(), Agent.created_at.desc())
    if limit:
        q = q.limit(limit)
    return _enrich_agent_briefs(q.all(), request, db)


# ---- Sessions (from ~/.claude/history.jsonl) ----

@router.get("/api/projects/{name}/sessions", response_model=list[SessionSummary])
async def list_project_sessions(name: str, db: Session = Depends(get_db)):
    """List all past Claude conversations for a project from history.jsonl."""
    import json
    from config import CLAUDE_HISTORY_PATH, PROJECTS_DIR

    projects_dir = PROJECTS_DIR or "/projects"
    history_path = CLAUDE_HISTORY_PATH

    if not os.path.isfile(history_path):
        return []

    # Group entries by sessionId
    sessions: dict[str, list[dict]] = {}
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Skipped malformed JSONL line in session history")
                continue
            sid = entry.get("sessionId")
            if not sid:
                continue
            sessions.setdefault(sid, []).append(entry)

    # Filter sessions matching this project by path basename or full path
    matched: dict[str, list[dict]] = {}
    for sid, entries in sessions.items():
        project_path = entries[0].get("project", "")
        if not project_path:
            continue
        basename = os.path.basename(project_path.rstrip("/"))
        canonical = os.path.join(projects_dir, name)
        if basename == name or project_path.rstrip("/") == canonical.rstrip("/"):
            matched[sid] = entries

    # Build agent session_id lookup for linking
    linked_agents: dict[str, str] = {}
    agent_rows = (
        db.query(Agent.id, Agent.session_id)
        .filter(Agent.project == name, Agent.session_id.is_not(None))
        .all()
    )
    for aid, asid in agent_rows:
        linked_agents[asid] = aid

    # Build summaries from history.jsonl
    seen_session_ids: set[str] = set()
    results = []
    for sid, entries in matched.items():
        entries.sort(key=lambda e: e.get("timestamp", 0))
        first_msg = entries[0].get("display", "")

        # Skip sessions that were interrupted before producing useful output
        if is_interrupt_message(first_msg):
            continue

        created = entries[0].get("timestamp", 0)
        last = entries[-1].get("timestamp", 0)
        project_path = entries[0].get("project", "")

        results.append(SessionSummary(
            session_id=sid,
            first_message=first_msg,
            message_count=len(entries),
            created_at=created,
            last_activity_at=last,
            project_path=project_path,
            linked_agent_id=linked_agents.get(sid),
        ))
        seen_session_ids.add(sid)

    # Also include orchestrator agents not found in history.jsonl
    # Exclude subagents — their conversations are part of the parent session
    all_agents = db.query(Agent).filter(
        Agent.project == name,
        Agent.is_subagent == False,  # noqa: E712
    ).all()
    for agent in all_agents:
        # Skip agents whose session_id is already covered by history.jsonl
        if agent.session_id and agent.session_id in seen_session_ids:
            continue

        # Use agent.id as the session identifier for agents without a session_id
        sid = agent.session_id or agent.id

        # Count user messages for this agent
        msg_count = (
            db.query(func.count(Message.id))
            .filter(Message.agent_id == agent.id, Message.role == MessageRole.USER)
            .scalar()
        )
        if msg_count == 0:
            continue

        created_ms = int(agent.created_at.timestamp() * 1000) if agent.created_at else 0
        last_ms = int(agent.last_message_at.timestamp() * 1000) if agent.last_message_at else created_ms

        results.append(SessionSummary(
            session_id=sid,
            first_message=agent.name,
            message_count=msg_count,
            created_at=created_ms,
            last_activity_at=last_ms,
            project_path=os.path.join(projects_dir, name),
            linked_agent_id=agent.id,
        ))

    # Sort by most recent first
    results.sort(key=lambda s: s.last_activity_at, reverse=True)

    # Mark starred sessions, migrating stale agent.id stars to session_id
    starred_ids = set(
        row[0] for row in db.query(StarredSession.session_id)
        .filter(StarredSession.project == name)
        .all()
    )
    for s in results:
        s.starred = s.session_id in starred_ids
        # Migrate: if starred under old agent.id but session now uses session_id
        if not s.starred and s.linked_agent_id and s.session_id != s.linked_agent_id:
            if s.linked_agent_id in starred_ids:
                # Re-key the star from agent.id → session_id
                old_star = db.get(StarredSession, s.linked_agent_id)
                if old_star:
                    db.delete(old_star)
                    db.add(StarredSession(session_id=s.session_id, project=name))
                    s.starred = True
    db.commit()

    return results


def _resolve_agent_id_for_session(db: Session, session_id: str) -> str | None:
    """Map session_id → agent.id. session_star_changed carries this so
    AgentsPage can patch the AgentsContext store keyed by agent.id without
    waiting for a /api/agents poll. Matches both the canonical (Agent.session_id)
    and legacy (Agent.id-keyed star) cases — same predicate as
    route_helpers.enrich_agent_briefs."""
    row = db.query(Agent.id).filter(
        (Agent.session_id == session_id) | (Agent.id == session_id)
    ).first()
    return row[0] if row else None


@router.put("/api/projects/{name}/sessions/{session_id}/star")
async def star_session(name: str, session_id: str, db: Session = Depends(get_db)):
    """Star a session."""
    existing = db.get(StarredSession, session_id)
    if not existing:
        db.add(StarredSession(session_id=session_id, project=name))
        db.commit()
    # Always emit so other devices' UI converges even on a duplicate request.
    from websocket import emit_session_star_changed
    agent_id = _resolve_agent_id_for_session(db, session_id)
    asyncio.ensure_future(emit_session_star_changed(name, session_id, True, agent_id))
    return {"starred": True}


@router.delete("/api/projects/{name}/sessions/{session_id}/star")
async def unstar_session(name: str, session_id: str, db: Session = Depends(get_db)):
    """Unstar a session."""
    existing = db.get(StarredSession, session_id)
    if existing:
        db.delete(existing)
        db.commit()
    from websocket import emit_session_star_changed
    agent_id = _resolve_agent_id_for_session(db, session_id)
    asyncio.ensure_future(emit_session_star_changed(name, session_id, False, agent_id))
    return {"starred": False}


# ---- Project files (CLAUDE.md / PROGRESS.md only) ----

@router.get("/api/projects/{name}/file")
async def get_project_file(name: str, path: str, db: Session = Depends(get_db)):
    """Read CLAUDE.md or PROGRESS.md from a project directory."""
    if path not in _ALLOWED_PROJECT_FILES:
        raise HTTPException(status_code=400, detail=f"Only {_ALLOWED_PROJECT_FILES} are accessible")
    project_path = resolve_project_path(name, db)
    filepath = os.path.join(project_path, path)
    if not os.path.isfile(filepath):
        # Auto-scaffold on first access
        from project_scaffolder import scaffold_project
        scaffold_project(name, project_path)
        if not os.path.isfile(filepath):
            return {"exists": False, "content": None, "path": path}
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"exists": True, "content": content, "path": path}
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/projects/{name}/file")
async def update_project_file(name: str, body: ProjectFileUpdate, db: Session = Depends(get_db)):
    """Write CLAUDE.md or PROGRESS.md in a project directory.

    If the file doesn't exist and content is empty, run the scaffolder instead.
    """
    if body.path not in _ALLOWED_PROJECT_FILES:
        raise HTTPException(status_code=400, detail=f"Only {_ALLOWED_PROJECT_FILES} are accessible")
    project_path = resolve_project_path(name, db)

    filepath = os.path.join(project_path, body.path)

    # If file doesn't exist and no content provided, scaffold it
    if not os.path.isfile(filepath) and not body.content.strip():
        from project_scaffolder import scaffold_project
        scaffold_project(name, project_path)
        # Read back the generated content
        if os.path.isfile(filepath):
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                return {"saved": True, "content": f.read(), "scaffolded": True}
        return {"saved": False, "detail": "Scaffolder did not generate the file"}

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(body.content)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Auto-rebuild insights DB when PROGRESS.md is saved
    if body.path == "PROGRESS.md" and body.content.strip():
        try:
            purged, imported = _rebuild_insights_from_content(name, body.content, db)
            logger.info("Auto-rebuilt insights after PROGRESS.md save: purged %d, imported %d for %s",
                        purged, imported, name)
        except Exception:
            logger.warning("Auto-rebuild insights failed for %s", name, exc_info=True)

    return {"saved": True, "content": body.content, "scaffolded": False}


# ---- CLAUDE.md refresh routes ----

@router.post("/api/projects/{name}/refresh-claudemd")
async def refresh_claudemd(name: str, db: Session = Depends(get_db)):
    """Start a background Claude agent to propose CLAUDE.md updates."""
    project_path = resolve_project_path(name, db)

    # If already running, return existing job
    existing = _claudemd_job_get(name)
    if existing and existing["status"] == "running":
        return {"status": "running"}

    # Gather context synchronously (fast DB + file reads)
    rows = (
        db.query(Message.content, Message.created_at, Agent.name)
        .join(Agent, Message.agent_id == Agent.id)
        .filter(Agent.project == name, Message.role == MessageRole.AGENT)
        .order_by(Message.created_at.desc())
        .limit(50)
        .all()
    )
    parts = []
    total_len = 0
    for content, created_at, agent_name in rows:
        snippet = (content or "")[:500]
        entry = f"[agent: {agent_name}, {created_at}]\n{snippet}\n"
        if total_len + len(entry) > 8000:
            break
        parts.append(entry)
        total_len += len(entry)
    recent_agent_activity = "\n".join(parts) if parts else "(no recent agent activity)"

    claudemd_path = os.path.join(project_path, "CLAUDE.md")
    progress_path = os.path.join(project_path, "PROGRESS.md")
    current_claudemd = ""
    if os.path.isfile(claudemd_path):
        with open(claudemd_path, "r", encoding="utf-8", errors="replace") as f:
            current_claudemd = f.read()
    progress_md = ""
    if os.path.isfile(progress_path):
        with open(progress_path, "r", encoding="utf-8", errors="replace") as f:
            progress_md = f.read()

    # Pre-read build/config files so the agent doesn't need tool access
    build_files_content = ""
    for fname in ("package.json", "pyproject.toml", "Makefile", "Cargo.toml",
                  "setup.py", "README.md"):
        fpath = os.path.join(project_path, fname)
        if os.path.isfile(fpath):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read(4000)  # cap per file
                build_files_content += f"\n--- {fname} ---\n{text}\n"
            except OSError as e:
                logger.warning("Failed to read build file %s: %s", fpath, e)

    # Mark as running and submit to bounded pool (caps concurrent claude -p)
    _claudemd_job_set(name, status="running")
    INSIGHT_EXECUTOR.submit(
        _refresh_claudemd_background,
        name, project_path, recent_agent_activity, current_claudemd,
        progress_md, build_files_content,
    )

    return {"status": "started"}


@router.get("/api/projects/{name}/refresh-claudemd/status")
async def refresh_claudemd_status(name: str):
    """Poll the status of a background CLAUDE.md refresh job."""
    job = _claudemd_job_get(name)
    if not job:
        return {"status": "none"}
    if job["status"] == "running":
        return {"status": "running"}
    if job["status"] == "error":
        return {"status": "error", "message": job.get("error", "Unknown error")}
    # complete
    return {"status": "complete", "data": job["data"]}


@router.delete("/api/projects/{name}/refresh-claudemd")
async def discard_claudemd(name: str):
    """Clear a cached CLAUDE.md refresh result (user discarded)."""
    _claudemd_job_clear(name)
    return {"success": True}


@router.get("/api/projects/claudemd-pending")
async def claudemd_pending():
    """Return count and list of projects with completed CLAUDE.md refresh jobs."""
    with _claudemd_jobs_lock:
        now = _time.monotonic()
        projects = [
            k for k, v in _claudemd_jobs.items()
            if v["status"] == "complete" and now - v["ts"] <= _CLAUDEMD_CACHE_TTL
        ]
    return {"count": len(projects), "projects": projects}


@router.post("/api/projects/{name}/apply-claudemd")
async def apply_claudemd(name: str, body: ApplyClaudeMdRequest, db: Session = Depends(get_db)):
    """Apply proposed CLAUDE.md changes (all or selective hunks)."""
    project_path = resolve_project_path(name, db)
    claudemd_path = os.path.join(project_path, "CLAUDE.md")

    job = _claudemd_job_get(name)
    if not job or job["status"] != "complete":
        raise HTTPException(status_code=410, detail="Proposal expired — run refresh again")

    proposed = job["data"].get("proposed", "")
    current = job["data"].get("current", "")

    if body.mode == "accept_all":
        final_content = proposed
    elif body.mode == "selective":
        if body.final_content is not None:
            # Frontend assembled the final content — just use it
            final_content = body.final_content
        else:
            # Legacy: hunk-level selection via SequenceMatcher opcodes
            accepted_ids = set(body.accepted_hunk_ids)
            current_lines = current.splitlines(keepends=True)
            proposed_lines = proposed.splitlines(keepends=True)

            sm = difflib.SequenceMatcher(None, current_lines, proposed_lines)
            result_lines = []
            hunk_idx = 0
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    result_lines.extend(current_lines[i1:i2])
                else:
                    if hunk_idx in accepted_ids:
                        result_lines.extend(proposed_lines[j1:j2])
                    else:
                        result_lines.extend(current_lines[i1:i2])
                    hunk_idx += 1

            final_content = "".join(result_lines)
    else:
        raise HTTPException(status_code=400, detail="mode must be 'accept_all' or 'selective'")

    # Write to disk
    try:
        with open(claudemd_path, "w", encoding="utf-8") as f:
            f.write(final_content)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    _claudemd_job_clear(name)

    line_count = len(final_content.splitlines())
    if line_count > 60:
        logger.warning("apply-claudemd %s: written CLAUDE.md is %d lines (>60)", name, line_count)

    return {"success": True, "content": final_content, "lines": line_count}


# ---- PROGRESS.md summary routes ----

@router.post("/api/projects/{name}/summarize-progress")
async def summarize_progress(name: str, db: Session = Depends(get_db)):
    """Start a background Claude agent to summarize today's tasks into PROGRESS.md."""
    project_path = resolve_project_path(name, db)

    existing = _progress_job_get(name)
    if existing and existing["status"] == "running":
        return {"status": "running"}

    from agent_dispatcher import _gather_daily_session_context
    session_context = _gather_daily_session_context(db, name)

    if not session_context:
        _progress_job_set(name, status="complete",
                         data={"message": "No agent sessions today"})
        return {"status": "started"}

    _progress_job_set(name, status="running")
    INSIGHT_EXECUTOR.submit(
        _summarize_progress_background, name, project_path, session_context,
    )
    return {"status": "started"}


@router.get("/api/projects/{name}/summarize-progress/status")
async def summarize_progress_status(name: str):
    """Poll the status of a background PROGRESS.md summary job."""
    job = _progress_job_get(name)
    if not job:
        return {"status": "none"}
    if job["status"] == "running":
        return {"status": "running"}
    if job["status"] == "error":
        return {"status": "error", "message": job.get("error", "Unknown error")}
    return {"status": "complete", "data": job["data"]}


@router.delete("/api/projects/{name}/summarize-progress")
async def discard_progress_summary(name: str):
    """Clear a cached PROGRESS.md summary result."""
    _progress_job_clear(name)
    return {"success": True}


@router.post("/api/projects/{name}/apply-progress")
async def apply_progress(name: str, db: Session = Depends(get_db)):
    """Append proposed PROGRESS.md summary section."""
    project_path = resolve_project_path(name, db)
    progress_path = os.path.join(project_path, "PROGRESS.md")

    job = _progress_job_get(name)
    if not job or job["status"] != "complete":
        raise HTTPException(status_code=410, detail="Proposal expired — run summary again")

    new_section = job["data"].get("proposed", "")
    if not new_section:
        raise HTTPException(status_code=400, detail="No proposed content")

    try:
        existing = ""
        if os.path.isfile(progress_path):
            with open(progress_path, "r", encoding="utf-8", errors="replace") as f:
                existing = f.read()

        separator = "\n\n" if existing and not existing.endswith("\n\n") else ("\n" if existing and not existing.endswith("\n") else "")
        final_content = existing + separator + new_section + "\n"
        with open(progress_path, "w", encoding="utf-8") as f:
            f.write(final_content)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Store parsed insights into DB + FTS5 for RAG retrieval
    from datetime import datetime as _dt2, timezone as _tz2
    from agent_dispatcher import store_insights
    n = store_insights(db, name, _dt2.now(_tz2.utc).date().isoformat(), new_section)
    if n:
        logger.info("Stored %d insights in FTS5 for %s", n, name)

    _progress_job_clear(name)
    return {"success": True, "content": final_content, "lines": len(final_content.splitlines())}


def _rebuild_insights_from_content(name: str, content: str, db) -> tuple[int, int]:
    """Purge existing insights and re-import from PROGRESS.md content.

    Returns (purged, imported) counts.
    """
    from models import ProgressInsight
    from agent_dispatcher import store_insights

    # 1. Purge existing insights + FTS5 for this project
    own_db = SessionLocal()
    try:
        existing_ids = [
            r[0] for r in own_db.query(ProgressInsight.id).filter(
                ProgressInsight.project == name
            ).all()
        ]
        if existing_ids:
            for rid in existing_ids:
                own_db.execute(
                    text("DELETE FROM progress_insights_fts WHERE rowid = :id"),
                    {"id": rid},
                )
            own_db.query(ProgressInsight).filter(
                ProgressInsight.project == name
            ).delete(synchronize_session=False)
            own_db.commit()
        purged = len(existing_ids)
    finally:
        own_db.close()

    # 2. Split into dated sections and re-import
    matches = list(_SECTION_DATE_RE.finditer(content))
    total_stored = 0
    for i, m in enumerate(matches):
        date_str = m.group(1)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        section_text = content[start:end]
        n = store_insights(db, name, date_str, section_text)
        total_stored += n

    logger.info("rebuild-insights: purged %d, imported %d for %s", purged, total_stored, name)
    return purged, total_stored


@router.post("/api/projects/{name}/rebuild-insights")
async def rebuild_insights(name: str, db: Session = Depends(get_db)):
    """Purge existing insights and re-import from PROGRESS.md."""
    project_path = resolve_project_path(name, db)
    progress_path = os.path.join(project_path, "PROGRESS.md")
    if not os.path.isfile(progress_path):
        raise HTTPException(status_code=404, detail="PROGRESS.md not found")

    with open(progress_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    purged, total_stored = _rebuild_insights_from_content(name, content, db)
    return {"success": True, "purged": purged, "imported": total_stored}


@router.patch("/api/projects/{name}/settings")
async def update_project_settings(name: str, request: Request, db: Session = Depends(get_db)):
    """Update project toggle settings (auto_progress_summary, etc.)."""
    proj = get_project_or_404(db, name)

    body = await request.json()
    if "auto_progress_summary" in body:
        proj.auto_progress_summary = bool(body["auto_progress_summary"])
    if "ai_insights" in body:
        proj.ai_insights = bool(body["ai_insights"])
    if "emoji" in body:
        raw = body["emoji"]
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            proj.emoji = None
        else:
            proj.emoji = str(raw)[:16]
    dropbox_changed = False
    if "dropbox_sync" in body:
        proj.dropbox_sync = bool(body["dropbox_sync"])
        dropbox_changed = True
    if "dropbox_folders" in body:
        raw = body["dropbox_folders"]
        if raw is None:
            proj.dropbox_folders = None
        elif isinstance(raw, list) and all(isinstance(x, str) for x in raw):
            # Top-level entry names only — no separators, no traversal.
            bad = [x for x in raw if not x or "/" in x or "\\" in x or x == ".."]
            if bad:
                raise HTTPException(status_code=400, detail=f"Invalid folder entries: {bad[:3]}")
            proj.dropbox_folders = json.dumps(sorted(set(raw)))
        else:
            raise HTTPException(status_code=400, detail="dropbox_folders must be a list of names or null")
        dropbox_changed = True
    if "dropbox_ignore" in body:
        raw = body["dropbox_ignore"]
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            proj.dropbox_ignore = None
        elif isinstance(raw, str):
            proj.dropbox_ignore = raw[:20000]
        else:
            raise HTTPException(status_code=400, detail="dropbox_ignore must be a string or null")
        dropbox_changed = True
    db.commit()
    db.refresh(proj)
    from websocket import emit_project_update
    asyncio.ensure_future(emit_project_update(name))
    if dropbox_changed:
        try:
            from dropbox_sync.engine import on_project_settings_changed
            on_project_settings_changed(name)
        except Exception:
            logger.debug("dropbox engine notify failed", exc_info=True)
    return ProjectOut.model_validate(proj)


# ---- Project directory browser (read-only) ----

@router.get("/api/projects/{name}/tree")
async def get_project_tree(name: str, depth: int = 3, db: Session = Depends(get_db)):
    """Return directory tree for a project (top N levels, ignoring common junk dirs)."""
    project_path = resolve_project_path(name, db)

    def _walk(dirpath: str, current_depth: int):
        if current_depth >= depth:
            return []
        try:
            entries = sorted(os.listdir(dirpath))
        except PermissionError:
            return []
        items = []
        for entry in entries:
            if entry.startswith(".") and entry not in (".env.example",):
                if entry not in (".env",):
                    continue
            full = os.path.join(dirpath, entry)
            rel = os.path.relpath(full, project_path)
            if os.path.isdir(full):
                if entry.lower() in _BROWSE_IGNORED or entry.endswith(".egg-info"):
                    continue
                children = _walk(full, current_depth + 1)
                items.append({"name": entry, "path": rel, "type": "dir", "children": children})
            else:
                items.append({"name": entry, "path": rel, "type": "file"})
        return items

    tree = _walk(project_path, 0)
    return {"tree": tree, "root": project_path}


@router.get("/api/projects/{name}/browse")
async def browse_project_file(name: str, path: str, db: Session = Depends(get_db)):
    """Read a single file from a project directory (read-only, with size limit)."""
    project_path = resolve_project_path(name, db)

    # Resolve and validate path is within project
    filepath = os.path.normpath(os.path.join(project_path, path))
    if not filepath.startswith(project_path + os.sep) and filepath != project_path:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    size = os.path.getsize(filepath)

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"path": path, "content": content, "truncated": False, "size": size}
    except (OSError, UnicodeDecodeError) as e:
        return {"path": path, "content": None, "truncated": False, "size": size,
                "message": f"Cannot read file: {e}"}


_SEARCH_TEXT_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".css", ".scss", ".sass", ".less",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".md", ".mdx", ".rst", ".txt",
    ".sh", ".bash", ".zsh", ".fish",
    ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".pl",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".m", ".mm",
    ".sql", ".graphql", ".proto", ".dockerfile", ".gitignore",
    ".vue", ".svelte", ".astro",
    ".lua", ".r", ".scala", ".clj", ".ex", ".exs", ".elm",
}
_SEARCH_MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB
_SEARCH_TIME_BUDGET_SEC = 4.0


def _is_searchable_file(filename: str) -> bool:
    if filename.startswith("."):
        # Allow common dotfiles like .env.example, .gitignore
        if filename in (".env", ".env.example", ".gitignore", ".dockerignore"):
            return True
        return False
    _, ext = os.path.splitext(filename)
    return ext.lower() in _SEARCH_TEXT_EXTS


@router.get("/api/projects/{name}/search-files")
async def search_project_files(
    name: str,
    q: str,
    limit: int = 30,
    db: Session = Depends(get_db),
):
    """Search file names and contents within a project directory.

    Returns:
      - name_matches: files whose path contains the query (case-insensitive)
      - content_matches: files containing matching lines, with up to 3 line snippets
    """
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")
    if limit > 100:
        limit = 100

    project_path = resolve_project_path(name, db)
    if not os.path.isdir(project_path):
        raise HTTPException(status_code=404, detail="Project directory not found")

    project_path = os.path.realpath(project_path)
    ql = q.lower()
    deadline = _time.monotonic() + _SEARCH_TIME_BUDGET_SEC

    # Wildcard handling: glob-style * and ? compiled to a regex once.
    has_wildcard = "*" in q or "?" in q
    pattern_re = None
    if has_wildcard and q.strip("*?"):
        # Translate glob to regex: escape literal chars, then * → .*?, ? → .
        parts = q.replace("?", "*").split("*")  # split on either wildcard
        # Re-derive piece boundaries handling both wildcards
        import re as _re
        regex_parts = []
        i = 0
        for ch in q:
            if ch == "*":
                regex_parts.append(".*?")
            elif ch == "?":
                regex_parts.append(".")
            else:
                regex_parts.append(_re.escape(ch))
        pattern_re = _re.compile("".join(regex_parts), _re.IGNORECASE)

    def _matches(text: str) -> bool:
        if pattern_re is not None:
            return pattern_re.search(text) is not None
        return ql in text.lower()

    name_matches: list[str] = []
    content_matches: list[dict] = []
    truncated = False

    for root, dirs, files in os.walk(project_path, followlinks=False):
        if _time.monotonic() > deadline:
            truncated = True
            break
        # Prune ignored directories in-place
        dirs[:] = [
            d for d in dirs
            if d.lower() not in _BROWSE_IGNORED
            and not d.endswith(".egg-info")
            and not (d.startswith(".") and d not in (".env",))
        ]
        for fname in files:
            if _time.monotonic() > deadline:
                truncated = True
                break
            full = os.path.join(root, fname)
            try:
                rel = os.path.relpath(full, project_path)
            except ValueError:
                continue

            # File-name match (any file, not just text)
            if _matches(rel) and len(name_matches) < limit:
                name_matches.append(rel)

            # Content match (text-like files only, size-limited)
            if len(content_matches) >= limit:
                continue
            if not _is_searchable_file(fname):
                continue
            try:
                if os.path.getsize(full) > _SEARCH_MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    matches = []
                    for i, line in enumerate(f, 1):
                        if _matches(line):
                            text = line.rstrip("\n")
                            if len(text) > 240:
                                # Center snippet around first match
                                if pattern_re is not None:
                                    m = pattern_re.search(text)
                                    idx, mlen = (m.start(), m.end() - m.start()) if m else (0, 0)
                                else:
                                    idx, mlen = text.lower().find(ql), len(q)
                                if idx < 0:
                                    idx, mlen = 0, 0
                                start = max(0, idx - 80)
                                end = min(len(text), idx + mlen + 160)
                                text = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(line) else "")
                            matches.append({"line": i, "text": text})
                            if len(matches) >= 3:
                                break
                    if matches:
                        content_matches.append({"path": rel, "matches": matches})
            except (OSError, UnicodeDecodeError):
                continue
        if len(name_matches) >= limit and len(content_matches) >= limit:
            break

    return {
        "query": q,
        "name_matches": name_matches,
        "content_matches": content_matches,
        "total_files": len(name_matches) + len(content_matches),
        "truncated": truncated,
    }
