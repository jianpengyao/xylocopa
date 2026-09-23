"""Task routes — CRUD, dispatch, cancel, complete, queue status, worktree names."""

import asyncio
import json
import logging
import os
import re
import threading

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import CC_MODEL, OPENAI_API_KEY, VALID_MODELS
from database import get_db
from models import Agent, AgentMode, AgentStatus, Message, MessageRole, MessageStatus, Project, Task, TaskStatus
from schemas import AttemptAgentOut, MessageOut, TaskCreate, TaskDetailOut, TaskOut, TaskUpdate
from task_state_machine import can_transition, InvalidTransitionError
from task_state import TaskStateMachine
from websocket import emit_task_update, emit_agent_update, emit_agent_created
from task_service import build_task
from route_helpers import (
    check_project_capacity, create_tmux_claude_session,
    generate_worktree_name_local, get_task_or_404, resolve_project_path,
    sanitize_worktree_name, tmux_session_candidates, tmux_session_name,
)
from utils import utcnow as _utcnow

logger = logging.getLogger("orchestrator")

router = APIRouter(tags=["tasks"])


# ---- Auto-title (gpt-4o-mini, background) ----

_AUTO_TITLE_SYSTEM = """You convert a user's task description into a short, scannable title for a TODO list.

LANGUAGE: match the description. English in -> English out. Chinese in -> Chinese out. Mixed -> dominant language.

HARD LIMITS:
- <= 50 characters (<= 15 CJK characters)
- 3-8 words (or 6-15 CJK characters)
- no trailing period, no quotes, no markdown
- drop filler: "I want to", "please", "the", "a", "可以", "麻烦"
- preserve specific anchors verbatim: file paths, function names, numbers, error names, PR/issue IDs

TONE: imperative when the task is an action ("Fix X" / "修复 X" / "Add Y"); noun phrase when descriptive ("Login flow audit").

GOOD:
  "Fix login redirect loop on Safari 17"
  "FilePreview: HEAD-probe missing thumbs"
  "修复登录页 token 过期处理"
  "Investigate p99 spike after PR #142"

BAD:
  "I want to fix the login bug"        (filler)
  "Bug"                                  (too vague, no anchor)
  "Fix the login bug that happens..."    (too long)
  "Login bug."                           (trailing period)

Return JSON: {"title": "..."}"""


async def _generate_title_background(task_id: int, description: str) -> None:
    """Call gpt-4o-mini to refine the auto-derived title, then patch the row."""
    if not OPENAI_API_KEY:
        return
    try:
        from openai import AsyncOpenAI
    except ImportError:
        logger.warning("openai package missing — skipping auto-title")
        return

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _AUTO_TITLE_SYSTEM},
                    {"role": "user", "content": f"DESCRIPTION:\n{description}"},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=120,
            ),
            timeout=15.0,
        )
        raw = resp.choices[0].message.content or ""
        parsed = json.loads(raw)
        new_title = re.sub(r"\s+", " ", str(parsed.get("title") or "")).strip().rstrip(".")
        if not new_title:
            return
        if len(new_title) > 80:
            new_title = new_title[:80].rstrip()
    except Exception as e:
        logger.warning(f"auto-title generation failed: {e}")
        return

    from database import SessionLocal
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            return
        task.title = new_title
        db.commit()
        await emit_task_update(task.id, task.status.value, task.project_name or "",
                               title=new_title)
    finally:
        db.close()


# ---- Helpers ----

def _stop_task_agents(db: Session, task, ad, reason, *, emit=True, add_message=False):
    """Stop the agent linked to a task."""
    if task.agent_id:
        agent = db.get(Agent, task.agent_id)
        if agent:
            if ad:
                ad.stop_agent_cleanup(db, agent, reason, add_message=add_message, emit=emit)
            elif agent.status not in (AgentStatus.STOPPED, AgentStatus.ERROR):
                import subprocess as _sp
                agent.status = AgentStatus.STOPPED
                if agent.tmux_pane:
                    for _sess in tmux_session_candidates(agent.id):
                        _kill = _sp.run(["tmux", "kill-session", "-t", _sess],
                                capture_output=True, timeout=5)
                        if _kill.returncode != 0:
                            logger.debug("tmux kill-session %s failed for agent %s", _sess, agent.id[:8])
                    agent.tmux_pane = None
                if emit:
                    asyncio.ensure_future(emit_agent_update(agent.id, "STOPPED", agent.project))


_generate_worktree_name_local = generate_worktree_name_local


async def _dispatch_task_tmux(db: Session, task: Task, proj: Project, ad) -> str | None:
    """Create a tmux agent for a task. Returns agent_id or None.

    Extracted from launch_tmux_agent endpoint so dispatch_task_v2 can
    create tmux agents through the unified pipeline.
    """
    import secrets
    import shlex
    import subprocess as _sp
    import uuid as _uuid

    from config import CLAUDE_BIN

    prompt = task.description or task.title
    model = task.model or proj.default_model or CC_MODEL
    if model not in VALID_MODELS:
        model = CC_MODEL
    effort = task.effort or "xhigh"
    worktree = None
    if getattr(task, "use_worktree", True):
        # Sanitize here too (belt-and-suspenders): heals any already-stored bad
        # name (e.g. an over-long LLM name) on (re-)dispatch so the launch can't
        # be rejected by the CLI's 64-char `--worktree` limit.
        worktree = sanitize_worktree_name(task.worktree_name or _generate_worktree_name_local(prompt))
        task.worktree_name = worktree
        task.branch_name = task.branch_name or f"worktree-{worktree}"
    skip_permissions = getattr(task, "skip_permissions", True)

    # Get existing tmux session names for collision check
    try:
        _tmux_ls = _sp.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        _existing_tmux = set(_tmux_ls.stdout.strip().splitlines()) if _tmux_ls.returncode == 0 else set()
    except (OSError, _sp.TimeoutExpired):
        _existing_tmux = set()

    # Generate unique agent ID
    for _ in range(20):
        agent_hex = secrets.token_hex(6)
        tmux_session = tmux_session_name(agent_hex)
        if db.get(Agent, agent_hex) is None and tmux_session not in _existing_tmux:
            break
    else:
        return None

    # Pre-generate session UUID and write .owner sidecar
    pre_session_id = str(_uuid.uuid4())
    from agent_dispatcher import _write_session_owner
    from session_cache import session_source_dir
    _sdir = session_source_dir(proj.path)
    os.makedirs(_sdir, exist_ok=True)
    _write_session_owner(_sdir, pre_session_id, agent_hex)

    # Build claude command (interactive mode)
    cmd_parts = [CLAUDE_BIN, "--session-id", pre_session_id,
                 "--output-format", "stream-json", "--verbose"]
    if skip_permissions:
        cmd_parts.append("--dangerously-skip-permissions")
    if model:
        from routers.agents import _model_for_cli
        cmd_parts += ["--model", _model_for_cli(model)]
    if effort:
        cmd_parts += ["--effort", effort]
    if worktree:
        cmd_parts += ["--worktree", worktree]
    claude_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

    from routers.agents import _preflight_claude_project, _launch_tmux_background
    _preflight_claude_project(proj.path)
    # 5 sync `tmux` subprocess calls (kill/new-session/display-message/send-keys×2)
    # would otherwise stall the event loop ~150-300ms during dispatch.
    pane_id = await asyncio.to_thread(
        create_tmux_claude_session,
        tmux_session, proj.path, claude_cmd, agent_hex,
    )

    # Create Agent record
    agent = Agent(
        id=agent_hex,
        project=proj.name,
        name=f"Task: {task.title[:80]}",
        mode=AgentMode.AUTO,
        status=AgentStatus.STARTING,
        model=model,
        cli_sync=True,
        tmux_pane=pane_id,
        effort=effort,
        worktree=worktree if worktree else None,
        skip_permissions=skip_permissions,
        task_id=task.id,
        muted=False,
        last_message_preview=f"Task: {task.title[:80]}",
        last_message_at=_utcnow(),
    )
    db.add(agent)
    db.flush()

    from display_writer import write_retry_marker_for_agent
    write_retry_marker_for_agent(db, agent)

    # Commit the agent row NOW so we release SQLite's exclusive write lock
    # before the ~1s _prepare_dispatch (OpenAI translate + FTS5).  Otherwise
    # any other writer (e.g. sync_loop processing JSONL deltas) blocks for
    # up to busy_timeout=5s and crashes with "database is locked".
    db.commit()
    db.refresh(agent)

    # Notify clients immediately — fire-and-forget so AgentsPage shows the
    # STARTING card without waiting for sync_engine's first user_turn (~2s)
    # or the 5s list poll. Mirrors how emit_task_update drives TasksPage.
    asyncio.ensure_future(emit_agent_created(agent))

    # Schedule launch task EARLY with a prompt future, so its TUI polling
    # (~1s of process-detect + REPL-ready + settle) overlaps with
    # _prepare_dispatch (~1s of OpenAI translate + FTS5).  Without this
    # parallelism the two phases run serially and double the launch latency.
    if prompt and ad:
        prompt_future: asyncio.Future = asyncio.get_running_loop().create_future()
        launch_task = asyncio.ensure_future(
            _launch_tmux_background(
                ad, agent_hex, pane_id, prompt_future, proj.path,
                pre_session_id=pre_session_id,
            )
        )
        ad.track_launch_task(agent_hex, launch_task)

        try:
            msg, launch_prompt, _ = await ad._prepare_dispatch(
                db, agent, proj, prompt,
                source="task",
                wrap_prompt=True,
            )
            msg.status = MessageStatus.COMPLETED
            msg.completed_at = _utcnow()
            db.commit()
            prompt_future.set_result(launch_prompt)
        except BaseException as e:
            if not prompt_future.done():
                prompt_future.set_exception(e)
            raise

    return agent_hex


# ---- Routes ----

@router.post("/api/v2/tasks", response_model=TaskOut, status_code=201)
async def create_task_v2(body: TaskCreate, db: Session = Depends(get_db)):
    """Create a new task. Starts as INBOX unless auto_dispatch is set."""
    # Auto-generate title from description if blank.
    # Whitespace is collapsed to single spaces — titles must be single-line
    # so content_matcher's task-prompt regex can strip the wrapper on JSONL
    # sync (newlines in title break the dedup match → duplicate user bubble).
    title = re.sub(r"\s+", " ", body.title).strip() if body.title else ""
    title_was_auto = False
    desc_raw = body.description.strip() if body.description else ""
    if not title and desc_raw:
        desc = re.sub(r"\s+", " ", desc_raw).strip()
        if len(desc) <= 60:
            title = desc
        else:
            cut = desc[:60].rsplit(" ", 1)[0] if " " in desc[:60] else desc[:60]
            title = cut + "..."
        title_was_auto = True
    if not title:
        title = "Untitled task"

    # If the description is non-trivial (multi-line or > 50 chars), schedule
    # an LLM-refined title to replace the placeholder in the background.
    needs_llm_title = (
        title_was_auto
        and desc_raw
        and ("\n" in desc_raw or len(desc_raw) > 50)
    )

    initial_status = TaskStatus.INBOX
    if body.auto_dispatch and body.project_name:
        proj = db.query(Project).filter(Project.name == body.project_name).first()
        if not proj:
            raise HTTPException(400, f"Project not found: {body.project_name}")
        initial_status = TaskStatus.PENDING

    task = build_task(body, status=initial_status, title=title)
    db.add(task)
    db.commit()
    db.refresh(task)
    asyncio.ensure_future(emit_task_update(
        task.id, task.status.value, task.project_name or "",
        title=task.title,
    ))
    if needs_llm_title:
        asyncio.ensure_future(_generate_title_background(task.id, desc_raw))
    return TaskOut.model_validate(task)


@router.get("/api/v2/tasks/counts")
# Sync on purpose: the body is all synchronous DB/filesystem work with no
# await, so as `async def` it blocked the event loop for its whole duration
# (p50 43 ms, max 11.8 s measured). FastAPI runs plain `def` handlers in a threadpool.
def task_counts(
    project: str | None = None,
    tz_offset: int = Query(default=0, description="Client timezone offset in minutes (JS getTimezoneOffset)"),
    db: Session = Depends(get_db),
):
    """Return perspective counts + weekly success stats.

    Optional ``project`` query param filters to a single project.
    ``tz_offset`` shifts date grouping to client local time.
    """
    from datetime import timedelta

    # Base filter — optionally scoped to a project
    def _pf(q):
        return q.filter(Task.project_name == project) if project else q

    # Perspective counts (server-side)
    rows = _pf(db.query(Task.status, func.count(Task.id))).group_by(Task.status).all()
    by_status = {s.value: c for s, c in rows}

    done_statuses = ["COMPLETE", "CANCELLED", "REJECTED", "FAILED", "TIMEOUT"]

    counts = {
        "INBOX": by_status.get("INBOX", 0),
        "PLANNING": 0,
        "QUEUE": by_status.get("PENDING", 0),
        "ACTIVE": by_status.get("EXECUTING", 0),
        "REVIEW": 0,
        "DONE": sum(by_status.get(s, 0) for s in done_statuses),
        "DONE_COMPLETED": by_status.get("COMPLETE", 0),
    }

    # Weekly stats — tasks that reached a terminal state this week
    # Shift dates to client local time (tz_offset is JS getTimezoneOffset: -480 for UTC+8)
    tz_hours = -tz_offset / 60  # e.g. -(-480)/60 = +8
    tz_mod = f"{'+' if tz_hours >= 0 else ''}{int(tz_hours)} hours"
    now_local = datetime.now(timezone.utc) + timedelta(hours=tz_hours)
    week_ago = now_local - timedelta(days=7)

    terminal = [TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.TIMEOUT,
                TaskStatus.REJECTED, TaskStatus.CANCELLED]
    weekly_q = _pf(db.query(
        Task.status, func.count(Task.id)
    )).filter(
        Task.status.in_(terminal),
        Task.completed_at >= week_ago,
    ).group_by(Task.status).all()

    weekly_by = {s.value: c for s, c in weekly_q}
    weekly_total = sum(weekly_by.values())
    weekly_completed = weekly_by.get("COMPLETE", 0)

    # Retry count (for display + daily formula)
    weekly_retries_row = _pf(db.query(
        func.coalesce(func.sum(Task.attempt_number - 1), 0)
    )).filter(
        Task.status.in_(terminal),
        Task.completed_at >= week_ago,
    ).scalar()
    weekly_retries = int(weekly_retries_row or 0)

    # First-attempt success: completed on first try (attempt_number == 1)
    weekly_first_attempt = _pf(db.query(func.count(Task.id))).filter(
        Task.status == TaskStatus.COMPLETE,
        Task.attempt_number == 1,
        Task.completed_at >= week_ago,
    ).scalar() or 0

    # Daily breakdown for the last 7 days (for sparkline chart)
    # Use SQLite date() with tz modifier to group by local date
    local_date = func.date(Task.completed_at, tz_mod)
    daily_rows = _pf(db.query(
        local_date.label("day"),
        Task.status,
        func.count(Task.id),
    )).filter(
        Task.status.in_(terminal),
        Task.completed_at >= week_ago,
    ).group_by("day", Task.status).all()

    # Daily retries (sum of attempt_number - 1 per day)
    daily_retry_rows = _pf(db.query(
        local_date.label("day"),
        func.coalesce(func.sum(Task.attempt_number - 1), 0),
    )).filter(
        Task.status.in_(terminal),
        Task.completed_at >= week_ago,
    ).group_by("day").all()
    daily_retries_map = {str(d): int(r) for d, r in daily_retry_rows}

    daily_map: dict[str, dict] = {}
    for day_val, status, cnt in daily_rows:
        d = str(day_val)
        if d not in daily_map:
            daily_map[d] = {"date": d, "total": 0, "completed": 0}
        daily_map[d]["total"] += cnt
        if status == TaskStatus.COMPLETE:
            daily_map[d]["completed"] += cnt

    # Fill missing days and compute retry-adjusted success_pct
    daily = []
    for i in range(7):
        d = (now_local - timedelta(days=6 - i)).strftime("%Y-%m-%d")
        entry = daily_map.get(d, {"date": d, "total": 0, "completed": 0})
        day_retries = daily_retries_map.get(d, 0)
        adj_total = entry["total"] + day_retries
        entry["retries"] = day_retries
        entry["success_pct"] = round(entry["completed"] / adj_total * 100) if adj_total else None
        daily.append(entry)

    # Weekly success pct = average of non-null daily success_pct values
    valid_pcts = [d["success_pct"] for d in daily if d["success_pct"] is not None]
    weekly_pct = round(sum(valid_pcts) / len(valid_pcts)) if valid_pcts else 0

    return {
        **counts,
        "weekly_total": weekly_total,
        "weekly_completed": weekly_completed,
        "weekly_success_pct": weekly_pct,
        "weekly_failed": weekly_by.get("FAILED", 0),
        "weekly_timeout": weekly_by.get("TIMEOUT", 0),
        "weekly_cancelled": weekly_by.get("CANCELLED", 0),
        "weekly_rejected": weekly_by.get("REJECTED", 0),
        "weekly_retries": weekly_retries,
        "weekly_first_attempt": weekly_first_attempt,
        "weekly_first_attempt_pct": round(weekly_first_attempt / weekly_completed * 100) if weekly_completed else 0,
        "daily": daily,
    }


@router.get("/api/v2/tasks/queue")
async def task_queue_status(
    tz_offset: int = Query(default=0, description="Client timezone offset in minutes (e.g. -420 for PDT)"),
    db: Session = Depends(get_db),
):
    """Return queue status: pending/executing tasks + per-project capacity."""
    # Dispatcher uses these to gate capacity
    dispatcher_active = [AgentStatus.STARTING, AgentStatus.EXECUTING, AgentStatus.IDLE]
    # All non-stopped agents (full picture for UI)
    alive_statuses = [AgentStatus.STARTING, AgentStatus.IDLE, AgentStatus.EXECUTING, AgentStatus.IDLE]

    # ── Source of truth: agents table ──
    # All non-stopped, non-subagent agents
    active_agents = (
        db.query(Agent)
        .filter(Agent.status.in_(alive_statuses), Agent.is_subagent == False)
        .order_by(Agent.created_at.asc())
        .all()
    )

    # Build unified agent list with effective status + task info
    agents_list = []
    for a in active_agents:
        effective = a.status.value
        entry = {
            "agent_id": a.id,
            "name": a.name,
            "project": a.project,
            "status": effective,
            "model": a.model,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        # Attach task info if linked
        if a.task_id:
            task = db.get(Task, a.task_id)
            if task:
                entry["task_id"] = task.id
                entry["task_title"] = task.title
        agents_list.append(entry)

    # Pending tasks (no agent assigned yet) — still need to show in queue
    pending_tasks = (
        db.query(Task)
        .filter(Task.status == TaskStatus.PENDING)
        .order_by(Task.created_at.asc())
        .all()
    )
    pending_list = [TaskOut.model_validate(t).model_dump() for t in pending_tasks]

    # Per-project agent counts (no capacity enforcement)
    from routers.projects import active_projects
    projects = active_projects(db)
    capacity = {}
    for proj in projects:
        active = (
            db.query(func.count(Agent.id))
            .filter(Agent.project == proj.name, Agent.status.in_(dispatcher_active))
            .scalar()
        )
        alive = (
            db.query(func.count(Agent.id))
            .filter(Agent.project == proj.name, Agent.status.in_(alive_statuses))
            .scalar()
        )
        idle = (
            db.query(func.count(Agent.id))
            .filter(Agent.project == proj.name, Agent.status == AgentStatus.IDLE)
            .scalar()
        )
        capacity[proj.name] = {
            "active": active,
            "alive": alive,
            "idle": idle,
        }

    # Today's completed tasks (in client's local timezone)
    client_tz = timezone(timedelta(minutes=-tz_offset))
    today_start = datetime.now(client_tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    terminal = [TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.TIMEOUT,
                TaskStatus.REJECTED, TaskStatus.CANCELLED]
    today_done = (
        db.query(Task)
        .filter(Task.status.in_(terminal), Task.completed_at >= today_start)
        .order_by(Task.completed_at.desc())
        .all()
    )
    today_done_list = [TaskOut.model_validate(t).model_dump() for t in today_done]

    return {
        "agents": agents_list,
        "pending": pending_list,
        "capacity": capacity,
        "today_done": today_done_list,
    }


@router.get("/api/v2/tasks", response_model=list[TaskOut])
async def list_tasks_v2(
    status: str | None = None,
    statuses: str | None = None,
    project: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """List v2 tasks with optional filters."""
    q = db.query(Task)
    if statuses:
        status_list = []
        for s in statuses.split(","):
            s = s.strip()
            if not s:
                continue
            try:
                status_list.append(TaskStatus(s))
            except ValueError:
                raise HTTPException(400, f"Invalid status: {s}")
        if status_list:
            q = q.filter(Task.status.in_(status_list))
    elif status:
        try:
            q = q.filter(Task.status == TaskStatus(status))
        except ValueError:
            raise HTTPException(400, f"Invalid status: {status}")
    if project:
        q = q.filter(Task.project_name == project)
    tasks = q.order_by(Task.created_at.desc()).limit(limit).all()

    # Enrich EXECUTING tasks with agent info
    results = []
    enrich_agent_ids = [t.agent_id for t in tasks if t.status == TaskStatus.EXECUTING and t.agent_id]
    agent_map = {}
    if enrich_agent_ids:
        agents = db.query(Agent).filter(Agent.id.in_(enrich_agent_ids)).all()
        agent_map = {a.id: a for a in agents}

    now = datetime.now(timezone.utc)
    for t in tasks:
        out = TaskOut.model_validate(t)
        if t.status == TaskStatus.EXECUTING and t.agent_id:
            agent = agent_map.get(t.agent_id)
            if agent and agent.last_message_preview:
                out.last_agent_message = agent.last_message_preview[:200]
            if t.started_at:
                started = t.started_at if t.started_at.tzinfo else t.started_at.replace(tzinfo=timezone.utc)
                out.elapsed_seconds = int((now - started).total_seconds())
        results.append(out)
    return results


@router.get("/api/v2/tasks/{task_id}", response_model=TaskDetailOut)
async def get_task_v2(task_id: str, db: Session = Depends(get_db)):
    """Get task detail with agent conversation if assigned."""
    task = get_task_or_404(db, task_id)
    conversation = []
    if task.agent_id:
        msgs = (
            db.query(Message)
            .filter(Message.agent_id == task.agent_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        conversation = [MessageOut.model_validate(m, from_attributes=True) for m in msgs]
    # All agents that worked on this task, ordered by creation time
    attempt_agents_rows = (
        db.query(Agent)
        .filter(Agent.task_id == task_id)
        .order_by(Agent.created_at.asc())
        .all()
    )
    attempt_agents = [
        AttemptAgentOut(agent_id=a.id, created_at=a.created_at, status=a.status.value if a.status else None)
        for a in attempt_agents_rows
    ]
    return TaskDetailOut(
        **TaskOut.model_validate(task).model_dump(),
        conversation=conversation,
        attempt_agents=attempt_agents,
    )



@router.post("/api/v2/tasks/batch-process")
async def batch_process_tasks(request: Request, db: Session = Depends(get_db)):
    """Spawn a tmux agent to triage inbox tasks: refine prompts, assign projects, dispatch."""
    try:
        body_raw = await request.json()
    except Exception:
        body_raw = {}
    task_ids = body_raw.get("task_ids")

    query = db.query(Task).filter(Task.status == TaskStatus.INBOX)
    if task_ids:
        query = query.filter(Task.id.in_(task_ids))
    # Exclude deferred tasks (deferred_to in the future)
    now = datetime.utcnow()
    query = query.filter(
        (Task.deferred_to == None) | (Task.deferred_to <= now)  # noqa: E711
    )
    inbox_tasks = query.order_by(Task.sort_order, Task.created_at.desc()).all()
    if not inbox_tasks:
        raise HTTPException(400, "No inbox tasks to process")

    # Gather available projects
    from routers.projects import active_projects
    projects = active_projects(db)
    project_list = [{"name": p.name, "display_name": getattr(p, "display_name", None) or p.name,
                     "description": getattr(p, "description", None) or ""} for p in projects]

    # Derive config fields dynamically from TaskUpdate schema so future fields are auto-included.
    # Exclude fields the triage is meant to change (title, description, project_name) and status.
    _triage_managed = {"title", "description", "project_name", "status"}
    _config_fields = [f for f in TaskUpdate.model_fields if f not in _triage_managed]

    tasks_data = []
    for t in inbox_tasks:
        task_dict = {
            "id": t.id,
            "title": t.title or "",
            "description": t.description or "",
            "project_name": t.project_name or None,
            "config": {field: getattr(t, field, None) for field in _config_fields},
        }
        tasks_data.append(task_dict)

    # Triage is a meta/global agent — host it on the synthetic
    # `.xylo-internal` placeholder so it doesn't pollute any user
    # project's agents/sessions list.  The placeholder's path
    # (`<PROJECTS_DIR>/.xylo-internal/`) carries its own absolute-path
    # `.mcp.json` so triage gets the orchestrator MCP tools regardless of
    # how/where xylocopa itself is installed.  Bootstrapped at startup by
    # routers.projects.ensure_internal_project (also runs lazily here as a
    # safety net for first-call-before-startup-completes edge cases).
    from routers.projects import (
        INTERNAL_PROJECT_NAME, ensure_internal_project,
    )
    ensure_internal_project(db)
    host_project = INTERNAL_PROJECT_NAME

    prompt = f"""You are a task triage assistant for Xylocopa. Analyze the inbox tasks below, then update each one using the Xylocopa MCP tools.

FOR EACH TASK:
1. **Title**: Rewrite to be clear, specific, and actionable (<80 chars). Keep good titles unchanged.
2. **Description**: Focus on making the problem definition crystal clear — what is the current behavior, what is the desired outcome, and why it matters. Do NOT add implementation steps or technical solutions — the executing agent will explore the codebase and figure out the approach itself. Preserve existing good content. Keep the original language.
3. **Project**: If project_name is null, assign the best-matching project. If none fits, leave null.
4. **Dispatch**: If the task has enough detail AND a project, dispatch it for execution.

AVAILABLE PROJECTS:
{json.dumps(project_list, ensure_ascii=False, indent=2)}

INBOX TASKS:
{json.dumps(tasks_data, ensure_ascii=False, indent=2)}

TOOLS TO USE:
- `mcp__xylocopa__update_task` — update a task's fields. Pass the task id plus the fields you want to change (title, description, project_name, etc.). Preserve every field shown in the task's `config` object verbatim — the task owner already configured those.
- `mcp__xylocopa__dispatch_task` — queue a task for execution once it has enough detail and a project assigned.
- `mcp__xylocopa__list_tasks` — inspect the current inbox if you need more context than the data above.
- `mcp__xylocopa__create_task` — create a new task (use only if you need to split or spawn a task; normal triage does not require this).
- `mcp__xylocopa__read_session` / `mcp__xylocopa__list_sessions` — optional, for pulling context from prior conversations when a task description references earlier work.

Use the MCP tools listed above; do not attempt other ways to reach the orchestrator.

INSTRUCTIONS:
1. First, analyze all tasks and present a summary table of your proposed changes (title, project assignment, ready status)
2. If anything is ambiguous — unclear intent, multiple possible projects, vague descriptions that could go different directions — ask the user to clarify before proceeding. Don't guess on important decisions.
3. Ask the user to confirm before applying changes
4. After confirmation, call `mcp__xylocopa__update_task` for each task that needs updates, then `mcp__xylocopa__dispatch_task` for each task that is ready to execute.
5. Report a final summary of what was changed and dispatched"""

    # Launch via the tmux agent endpoint (no task for the triage agent itself —
    # the individual inbox tasks maintain their own task→agent links when dispatched)
    from routers.agents import launch_tmux_agent

    class _MockRequest:
        """Thin wrapper to forward app state + override json body."""
        def __init__(self, real_request, body):
            self.app = real_request.app
            self._body = body
        async def json(self):
            return self._body

    mock_body = {
        "project": host_project,
        "prompt": prompt,
        "skip_permissions": True,
    }
    mock_req = _MockRequest(request, mock_body)
    agent_out = await launch_tmux_agent(mock_req, db)
    return {"ok": True, "agent_id": agent_out.id}


@router.put("/api/v2/tasks/reorder")
async def reorder_tasks_v2(body: dict, db: Session = Depends(get_db)):
    """Set sort_order for a list of task IDs. Body: { "task_ids": ["id1", "id2", ...] }"""
    task_ids = body.get("task_ids", [])
    if not task_ids:
        raise HTTPException(400, "task_ids required")
    first_task = None
    for i, tid in enumerate(task_ids):
        task = db.get(Task, tid)
        if task:
            task.sort_order = i
            if first_task is None:
                first_task = task
    db.commit()
    # Emit a single task_update on any reordered task — InboxTasksContext
    # listens and refetches the whole list, which picks up the new order.
    if first_task is not None:
        asyncio.ensure_future(emit_task_update(
            first_task.id, first_task.status.value, first_task.project_name or "",
        ))
    return {"ok": True, "count": len(task_ids)}


@router.put("/api/v2/tasks/{task_id}", response_model=TaskOut)
async def update_task_v2(task_id: str, body: TaskUpdate, db: Session = Depends(get_db)):
    """Update task fields. Only allowed for INBOX/PLANNING tasks."""
    task = get_task_or_404(db, task_id)
    # Note is editable regardless of status
    if "note" in body.model_fields_set:
        task.note = body.note
    # All other fields require INBOX/PLANNING
    non_note_fields = body.model_fields_set - {"note"}
    if non_note_fields and task.status not in (TaskStatus.INBOX, TaskStatus.PLANNING):
        raise HTTPException(400, f"Cannot edit task in {task.status.value} status")
    # Support status transitions (e.g. PLANNING → INBOX)
    if hasattr(body, "status") and body.status is not None:
        try:
            new_status = TaskStatus(body.status)
            TaskStateMachine.transition(task, new_status)
        except (ValueError, InvalidTransitionError) as exc:
            raise HTTPException(409, str(exc))
    for field in ("title", "project_name", "model", "effort", "priority"):
        val = getattr(body, field, None)
        if val is not None:
            setattr(task, field, val)
    # Allow explicit null to clear description
    if "description" in body.model_fields_set:
        task.description = body.description
    # Boolean fields: explicit set check (None means "not sent")
    for field in ("skip_permissions", "use_worktree", "use_tmux"):
        if field in body.model_fields_set:
            setattr(task, field, getattr(body, field))
    if "worktree_name" in body.model_fields_set:
        task.worktree_name = body.worktree_name or None
    if "sort_order" in body.model_fields_set and body.sort_order is not None:
        task.sort_order = body.sort_order
    # Time fields: allow explicit null to clear
    if "notify_at" in body.model_fields_set:
        task.notify_at = body.notify_at
    if "deferred_to" in body.model_fields_set:
        task.deferred_to = body.deferred_to
    db.commit()
    db.refresh(task)
    asyncio.ensure_future(emit_task_update(
        task.id, task.status.value, task.project_name or "",
        title=task.title,
    ))
    return TaskOut.model_validate(task)



@router.post("/api/v2/tasks/{task_id}/dispatch", response_model=TaskOut)
async def dispatch_task_v2(task_id: str, request: Request, db: Session = Depends(get_db)):
    """Dispatch a task: create agent synchronously and move to EXECUTING.

    Creates a tmux agent and moves the task to EXECUTING.
    Returns the task with agent_id set so the frontend can navigate immediately.
    """
    task = get_task_or_404(db, task_id)
    if not task.project_name:
        raise HTTPException(400, "Task requires a project_name before dispatch")
    if not task.title:
        raise HTTPException(400, "Task requires a title before dispatch")
    # Validate transition
    if not can_transition(task.status, TaskStatus.EXECUTING):
        raise HTTPException(409, f"Invalid task transition: {task.status.value} -> executing (task {task.id})")

    proj = db.query(Project).filter(Project.name == task.project_name).first()
    if not proj:
        raise HTTPException(400, f"Project not found: {task.project_name}")

    # Enforce per-project capacity
    check_project_capacity(db, task.project_name)

    expected_status = task.status

    # Redo: prepare retry context before creating agent
    if expected_status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
        task.attempt_number += 1
        if task.agent_summary:
            task.retry_context = task.agent_summary
        task.agent_id = None
        task.agent_summary = None
        task.started_at = None
        task.completed_at = None

    ad = getattr(request.app.state, "agent_dispatcher", None)

    # All tasks dispatch via tmux
    agent_id = await _dispatch_task_tmux(db, task, proj, ad)

    if not agent_id:
        raise HTTPException(500, "Failed to create agent for task")

    # Atomic CAS: only update if status hasn't changed since we read it
    from task_state import TaskStateMachine
    update_dict: dict = {
        "status": TaskStatus.EXECUTING,
        "agent_id": agent_id,
        "started_at": _utcnow(),
    }
    # Include retry fields in the CAS for FAILED/TIMEOUT
    if expected_status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
        update_dict["attempt_number"] = task.attempt_number
        update_dict["retry_context"] = task.retry_context
        update_dict["agent_summary"] = None
        update_dict["completed_at"] = None
    rows = (
        db.query(Task)
        .filter(Task.id == task_id, Task.status == expected_status)
        .update(update_dict, synchronize_session="fetch")
    )
    if rows == 0:
        raise HTTPException(409, "Task status changed concurrently")
    db.commit()
    db.refresh(task)
    asyncio.ensure_future(emit_task_update(
        task.id, task.status.value, task.project_name or "",
        title=task.title, agent_id=agent_id,
    ))
    return TaskOut.model_validate(task)


@router.post("/api/v2/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task_v2(task_id: str, request: Request, db: Session = Depends(get_db)):
    """Cancel a task. Stops agent if running."""
    task = get_task_or_404(db, task_id)
    if not can_transition(task.status, TaskStatus.CANCELLED):
        raise HTTPException(409, f"Invalid task transition: {task.status.value} -> cancelled (task {task.id})")
    ad = getattr(request.app.state, "agent_dispatcher", None)
    _stop_task_agents(db, task, ad, "Agent stopped — task cancelled")
    TaskStateMachine.transition(task, TaskStatus.CANCELLED)
    # Clean up git artifacts
    proj = db.query(Project).filter(Project.name == task.project_name).first()
    if proj:
        from git_manager import GitManager
        gm = GitManager()
        if task.worktree_name:
            wt_path = os.path.join(proj.path, ".claude", "worktrees", task.worktree_name)
            gm.remove_worktree(proj.path, wt_path)
        if task.branch_name:
            gm.delete_branch(proj.path, task.branch_name, force=True)
    db.commit()
    db.refresh(task)
    asyncio.ensure_future(emit_task_update(
        task.id, task.status.value, task.project_name or "",
        title=task.title,
    ))
    return TaskOut.model_validate(task)


@router.post("/api/v2/tasks/{task_id}/complete", response_model=TaskOut)
async def complete_task_v2(task_id: str, request: Request, db: Session = Depends(get_db)):
    """Manually complete a task. Stops agent and marks COMPLETE."""
    task = get_task_or_404(db, task_id)
    if not can_transition(task.status, TaskStatus.COMPLETE):
        raise HTTPException(409, f"Cannot complete task in {task.status.value} state")
    ad = getattr(request.app.state, "agent_dispatcher", None)
    _stop_task_agents(db, task, ad, "Agent stopped — task completed")
    TaskStateMachine.transition(task, TaskStatus.COMPLETE)
    task.completed_at = _utcnow()
    db.commit()
    db.refresh(task)
    asyncio.ensure_future(emit_task_update(
        task.id, task.status.value, task.project_name or "",
        title=task.title,
    ))
    return TaskOut.model_validate(task)


@router.post("/api/v2/tasks/{task_id}/regenerate-summary", response_model=TaskOut)
async def regenerate_task_summary(task_id: str, db: Session = Depends(get_db)):
    """Manually re-trigger retry summary generation for a task."""
    task = get_task_or_404(db, task_id)
    if task.attempt_number < 2:
        raise HTTPException(409, "No previous attempt to summarize")

    # Find the most recent stopped agent for this task
    prev_agent = (
        db.query(Agent)
        .filter(Agent.task_id == task_id, Agent.status.in_([AgentStatus.STOPPED, AgentStatus.ERROR]))
        .order_by(Agent.created_at.desc())
        .first()
    )
    if not prev_agent:
        raise HTTPException(404, "No previous agent found for this task")

    project_path = resolve_project_path(task.project_name, db) if task.project_name else None
    if not project_path:
        raise HTTPException(404, "Project path not found")

    # Reset marker
    task.agent_summary = ":::generating:::"
    db.commit()
    db.refresh(task)

    asyncio.ensure_future(emit_task_update(
        task.id, task.status.value, task.project_name or "", title=task.title,
    ))

    # Submit to bounded pool (caps concurrent claude -p subprocesses)
    from routers.projects import _generate_retry_summary_background, INSIGHT_EXECUTOR
    INSIGHT_EXECUTOR.submit(
        _generate_retry_summary_background,
        prev_agent.id, task.id, task.title or "Unknown task",
        task.project_name or "", project_path,
        task.retry_context,
    )
    logger.info("Manual retry summary spawned for task %s (agent %s)", task_id, prev_agent.id)

    return TaskOut.model_validate(task)


@router.post("/api/worktree-name")
async def generate_worktree_name(request: Request):
    """Generate a short branch name from a prompt using GPT-4o-mini."""
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"name": "task"}

    if not OPENAI_API_KEY:
        return {"name": _generate_worktree_name_local(prompt)}

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model=os.getenv("TRIAGE_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": (
                    "Generate a short git branch name (kebab-case, lowercase, "
                    "3-5 words, no special chars) summarizing the task. "
                    "Reply with ONLY the branch name, nothing else."
                )},
                {"role": "user", "content": prompt[:500]},
            ],
            max_tokens=30,
            temperature=0.3,
        )
        name = sanitize_worktree_name(resp.choices[0].message.content)
        if name == "task":  # LLM gave nothing usable — fall back to prompt-derived name
            name = _generate_worktree_name_local(prompt)
        return {"name": name}
    except Exception as e:
        logger.warning("Worktree name generation failed: %s", e)
        return {"name": _generate_worktree_name_local(prompt)}
