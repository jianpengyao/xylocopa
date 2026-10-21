"""Built-in actions: notify · message_agent · dispatch_task · run_prompt.

Each action is a coroutine `run(job, db) -> str` registered in ACTIONS. The
scheduler awaits it and stores the returned string as the job's outcome.
Actions may raise; the scheduler records `last_error` and does not retry a
one-shot job.

None of these invent new delivery machinery — they call the paths the app
already uses, so an attention job behaves exactly like the equivalent
manual action:

  notify        → notify.notify() → VAPID web push
  message_agent → display_writer.pre_sent_create (the probe wake path)
  dispatch_task → task_service.build_task + the PENDING dispatch queue
  run_prompt    → `claude -p --model claude-sonnet-5` (the insights path)
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess

from models import Agent, AgentStatus, AttentionJob, Task, TaskStatus
from utils import utcnow as _utcnow
from attention.registry import Action, register_action

logger = logging.getLogger("orchestrator.attention")

# Caps concurrent `claude -p` subprocesses spawned by attention jobs.
# Deliberately separate from routers/projects.INSIGHT_EXECUTOR so a burst of
# digest jobs can't starve insight generation (and vice versa). A semaphore
# rather than a second ThreadPoolExecutor: `asyncio.to_thread` already has a
# pool, we only need the ceiling.
_PROMPT_SLOTS = asyncio.Semaphore(1)

# Wall-clock ceiling for a run_prompt job.
PROMPT_TIMEOUT_SECONDS = 180

# Truncation ceiling for prompt output pushed to the user. A push
# notification body is unreadable past a couple hundred chars anyway, and
# this bounds what a runaway generation can write into the DB.
PROMPT_OUTPUT_LIMIT = 1500


def _config(job) -> dict:
    try:
        cfg = json.loads(job.action_config or "{}")
        return cfg if isinstance(cfg, dict) else {}
    except (TypeError, ValueError):
        logger.warning("attention: job %s has invalid action_config", job.id)
        return {}


def _default_url(job) -> str:
    if job.agent_id:
        return f"/agents/{job.agent_id}"
    return "/"


# ---------------------------------------------------------------------------
# notify — web push
# ---------------------------------------------------------------------------

async def _run_notify(job, db) -> str:
    from notify import notify

    cfg = _config(job)
    title = (cfg.get("title") or job.title or "Reminder").strip()[:120]
    body = (cfg.get("body") or job.source_text or "").strip()[:400]
    url = cfg.get("url") or _default_url(job)

    # The user explicitly asked for this notification, so it rides the
    # "attention" channel — never suppressed by a device's message switch
    # (agents_enabled) or per-agent mute, same contract as notify_at.
    decision = notify(
        "attention", job.agent_id or "", title, body or title, url=url,
        meta={"job_id": job.id, "kind": job.kind},
    )
    return f"notify {decision}"


register_action(Action(
    name="notify",
    description="Send the user a push notification",
    config_schema='{"title": "<short>", "body": "<text>", "url": "<optional in-app path>"}',
    run=_run_notify,
))


# ---------------------------------------------------------------------------
# message_agent — inject a message into a chat
# ---------------------------------------------------------------------------

# Mirrors Probe.ENVELOPE_* so an injected message is visibly machine-origin
# and can't be mistaken for something the user typed.
ENVELOPE_PREFIX = "\U0001F7E7 Attention job "
ENVELOPE_FOOTER = "— end of attention job —"
MAX_MESSAGE_LEN = 4000


def _envelope(job, text: str) -> str:
    fired = _utcnow()
    month_day = fired.strftime("%b %-d")
    hour_min = fired.strftime("%-I:%M") + fired.strftime("%p").lower()
    return (
        f"{ENVELOPE_PREFIX}{job.id} fired ({month_day} {hour_min})\n\n"
        f"{text}\n\n{ENVELOPE_FOOTER}"
    )


def validate_message(text: str | None) -> str | None:
    """Reject injected text that would break the envelope contract.

    Same defense as Probe.validate_message: a message containing the
    envelope markers could fake a nested envelope and confuse the receiving
    chat about where machine input ends and user input begins.
    """
    if not text or not text.strip():
        return "message is empty"
    if len(text) > MAX_MESSAGE_LEN:
        return f"message exceeds {MAX_MESSAGE_LEN} chars"
    if ENVELOPE_PREFIX.strip() in text or ENVELOPE_FOOTER in text:
        return "message may not contain the attention-job envelope markers"
    return None


async def _run_message_agent(job, db) -> str:
    import uuid

    from display_writer import pre_sent_create
    from websocket import emit_pre_sent_created

    cfg = _config(job)
    target_id = cfg.get("agent_id") or job.agent_id
    if not target_id:
        raise ValueError("message_agent needs an agent_id")

    agent = db.get(Agent, target_id)
    if agent is None:
        raise ValueError(f"agent {target_id} no longer exists")

    text = cfg.get("message") or job.source_text or ""
    err = validate_message(text)
    if err:
        raise ValueError(f"message rejected: {err}")

    now = _utcnow()
    envelope = _envelope(job, text)
    msg_id = uuid.uuid4().hex[:12]
    entry = {
        "id": msg_id,
        "role": "USER",
        "content": envelope,
        # source="web" rides the standard web message path, so sync_engine's
        # dedup whitelist needs no new entry (same reasoning as probes).
        "source": "web",
        "status": "queued",
        "created_at": now.isoformat(),
        "scheduled_at": None,
        "metadata": {"attention_job_id": job.id},
    }

    agent.last_message_preview = envelope[:200]
    agent.last_message_at = now
    db.flush()

    pre_sent_create(agent.id, entry)
    asyncio.ensure_future(emit_pre_sent_created(agent.id, msg_id))

    dispatcher = _dispatcher()
    if dispatcher and agent.status != AgentStatus.STOPPED:
        asyncio.ensure_future(dispatcher.dispatch_pending_message(agent.id, delay=0))

    return f"queued message {msg_id} to agent {agent.id[:8]}"


register_action(Action(
    name="message_agent",
    description=(
        "Inject a message into an agent's chat, wrapped in a machine-origin "
        "envelope. Wakes the agent if it is running"
    ),
    config_schema='{"agent_id": "<id>", "message": "<text, max 4000 chars>"}',
    run=_run_message_agent,
))


# ---------------------------------------------------------------------------
# dispatch_task — create (and optionally queue) a task
# ---------------------------------------------------------------------------

async def _run_dispatch_task(job, db) -> str:
    from schemas import TaskCreate
    from task_service import build_task
    from websocket import emit_task_update

    cfg = _config(job)
    title = (cfg.get("title") or job.title or "Attention task").strip()[:300]
    project = cfg.get("project_name") or job.project_name
    dispatch = bool(cfg.get("dispatch"))

    if dispatch and not project:
        raise ValueError("dispatch=true requires a project_name")

    payload = TaskCreate(
        title=title,
        description=cfg.get("description") or job.source_text,
        project_name=project,
    )
    # PENDING hands the task to the existing dispatch queue; INBOX just
    # files it for the user to triage.
    status = TaskStatus.PENDING if dispatch else TaskStatus.INBOX
    task = build_task(payload, status=status, title=title)
    db.add(task)
    db.flush()

    asyncio.ensure_future(emit_task_update(
        task.id, task.status.value, task.project_name or "", title=task.title,
    ))
    return f"created task {task.id} ({status.value})"


register_action(Action(
    name="dispatch_task",
    description=(
        "Create a task. With dispatch=true it enters the execution queue "
        "immediately; otherwise it lands in the inbox for triage"
    ),
    config_schema=(
        '{"title": "<short>", "description": "<optional>", '
        '"project_name": "<required when dispatch is true>", "dispatch": false}'
    ),
    run=_run_dispatch_task,
))


# ---------------------------------------------------------------------------
# run_prompt — one headless Sonnet call, result pushed to the user
# ---------------------------------------------------------------------------

def _claude_p(prompt: str) -> tuple[int, str, str]:
    """Blocking `claude -p` call. Runs in a worker thread via to_thread.

    cwd="/tmp" matches the existing insight/summary invocations: running
    from a project dir loads that project's hooks, and the PreToolUse
    permission hook returns {} for non-agent subprocesses, which yields
    empty output.
    """
    from config import CLAUDE_BIN, SUMMARY_MODEL
    from route_helpers import subprocess_clean_env

    proc = subprocess.run(
        [CLAUDE_BIN, "-p", "-", "--output-format", "text",
         "--no-session-persistence", "--model", SUMMARY_MODEL],
        input=prompt,
        capture_output=True, text=True,
        timeout=PROMPT_TIMEOUT_SECONDS,
        cwd="/tmp",
        env=subprocess_clean_env(),
    )
    return proc.returncode, proc.stdout, proc.stderr


async def _run_run_prompt(job, db) -> str:
    from notify import notify

    cfg = _config(job)
    prompt = (cfg.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("run_prompt needs a prompt")

    # Optional cheap context injection: the digest use case ("summarize what
    # my agents did") needs current state, and gathering it here keeps the
    # LLM call itself stateless.
    if cfg.get("include_agent_state"):
        prompt = _with_agent_state(prompt, db, job.project_name)

    async with _PROMPT_SLOTS:
        try:
            rc, out, err = await asyncio.to_thread(_claude_p, prompt)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"prompt timed out after {PROMPT_TIMEOUT_SECONDS}s")

    if rc != 0:
        raise RuntimeError(f"claude -p exited {rc}: {(err or '')[:200]}")

    text = (out or "").strip()
    if not text:
        return "prompt returned empty output — nothing pushed"
    text = text[:PROMPT_OUTPUT_LIMIT]

    title = (cfg.get("title") or job.title or "Assistant").strip()[:120]
    url = cfg.get("url") or _default_url(job)
    decision = notify(
        "attention", job.agent_id or "", title, text[:400], url=url,
        meta={"job_id": job.id, "kind": job.kind},
    )
    return f"prompt ok ({len(text)} chars), notify {decision}"


def _with_agent_state(prompt: str, db, project: str | None) -> str:
    """Append a compact snapshot of agent/task state to the prompt.

    Deliberately small and column-only (no JSONL reads): a digest is a
    convenience, not an analysis job, and this runs on a schedule.
    """
    q = db.query(Agent).filter(Agent.is_subagent == False)  # noqa: E712
    if project:
        q = q.filter(Agent.project == project)
    agents = q.order_by(Agent.last_message_at.desc().nullslast()).limit(30).all()

    lines = ["", "--- current agent state ---"]
    for a in agents:
        preview = (a.last_message_preview or "").replace("\n", " ")[:120]
        when = a.last_message_at.isoformat() if a.last_message_at else "never"
        lines.append(
            f"[{a.project}] {a.name} · {a.status.value} · unread={a.unread_count} "
            f"· last={when} · {preview}"
        )

    tq = db.query(Task).filter(
        Task.status.in_([TaskStatus.EXECUTING, TaskStatus.REVIEW, TaskStatus.CONFLICT]),
    )
    if project:
        tq = tq.filter(Task.project_name == project)
    tasks = tq.limit(30).all()
    if tasks:
        lines.append("--- open tasks ---")
        for t in tasks:
            lines.append(f"[{t.project_name or '-'}] {t.status.value} · {t.title}")

    return prompt + "\n" + "\n".join(lines)


register_action(Action(
    name="run_prompt",
    description=(
        "Run one headless Sonnet call and push its output to the user. "
        "Set include_agent_state to prepend a snapshot of agent/task state "
        "(for digests). Spends tokens on every fire"
    ),
    config_schema=(
        '{"prompt": "<instruction>", "title": "<push title>", '
        '"include_agent_state": false, "url": "<optional in-app path>"}'
    ),
    run=_run_run_prompt,
    costly=True,
))


# ---------------------------------------------------------------------------

def _dispatcher():
    """The live AgentDispatcher, if the app has one.

    Imported lazily and defensively: the attention layer must remain usable
    from tests and from the MCP process, neither of which has a dispatcher.
    """
    try:
        import main
        return getattr(main.app.state, "agent_dispatcher", None)
    except Exception:
        return None


def resolve_job(db, job_id: str) -> AttentionJob | None:
    return db.get(AttentionJob, job_id)
