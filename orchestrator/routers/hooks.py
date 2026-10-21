"""Claude Code Hooks endpoints — extracted from main.py."""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import hook_auth
from database import SessionLocal, get_db
from models import Agent, AgentMode, AgentStatus, Message, MessageRole, MessageStatus, Project, Task, TaskStatus
from utils import utcnow as _utcnow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["hooks"])


# ---- Helpers ----

def _resolve_agent_for_hook(request: Request, body: dict) -> str:
    """Authoritative agent resolution for hook handlers.

    Lookup order (header value is intentionally ignored):
      1. Tmux pane → DB. Pane IDs are owned by the orchestrator's tmux
         server and recorded at agent launch — immune to env-var leakage.
      2. session_id → DB (cli_sync=True only). Covers adopted CLI agents
         that may not have a pane on record yet during the adopt window.

    Why not trust X-Agent-Id: it is populated from $XY_AGENT_ID, which
    leaks through env inheritance into unrelated panes (e.g. a user's
    `tmux new -s cc`). Honouring the leaked value misroutes the hook to
    a stale agent and silently breaks unmanaged adopt detection.

    Returns the agent_id or "" when no authoritative match exists.

    A resolved agent's per-launch hook nonce (X-Xy-Hook-Nonce, hook_auth.py)
    is then checked: a mismatch raises 403 in enforce mode with auth on, and
    is only logged otherwise. It never returns "" on a mismatch, which would
    send agent-session-start down the unmanaged path.
    """
    tmux_pane = (request.headers.get("X-Tmux-Pane") or "").strip()
    sid = ""
    if isinstance(body, dict):
        sid = (body.get("session_id") or "").strip()
    from database import SessionLocal
    db = SessionLocal()
    try:
        if tmux_pane:
            agent = db.query(Agent).filter(
                Agent.tmux_pane == tmux_pane,
                Agent.status.notin_([AgentStatus.STOPPED, AgentStatus.ERROR]),
            ).first()
            if agent:
                hook_auth.check(request, agent.id, db)
                return agent.id
        if sid:
            agent = db.query(Agent).filter(
                Agent.session_id == sid,
                Agent.cli_sync == True,
                Agent.status.notin_([AgentStatus.STOPPED, AgentStatus.ERROR]),
            ).first()
            if agent:
                hook_auth.check(request, agent.id, db)
                return agent.id
    finally:
        db.close()
    return ""


def _is_subprocess_session(agent_id: str, hook_session_id: str, request: Request) -> bool:
    """Return True if a hook is from a Claude Code subprocess, not the main agent.

    When Claude Code's Agent tool spawns ``claude -p`` subprocesses, they
    inherit XY_AGENT_ID and fire hooks with the parent agent's ID.
    These must be ignored to prevent session theft and false state changes.

    Checks if the hook's session_id differs from the agent's tracked
    session in the sync context.
    """
    if not agent_id or not hook_session_id:
        return False
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if not ad:
        return False
    ctx = ad._sync_contexts.get(agent_id)
    if not ctx or not ctx.session_id:
        return False
    return ctx.session_id != hook_session_id


def _reset_compact_abandoned(ad, agent_id: str, reason: str):
    """Reset compact_notified and clean up stale UI artifacts.

    Called when we detect that CC abandoned a compact attempt (sent a
    non-PostCompact hook, or JSONL grew during the compact window).
    """
    ctx = ad._sync_contexts.get(agent_id)
    if not ctx or not ctx.compact_notified:
        return
    ctx.compact_notified = False
    ctx.compact_notified_at = 0.0
    ctx.compact_detected_at = 0.0
    logger.warning("Compact abandoned: agent %s %s, resetting sync pause", agent_id[:8], reason)
    # Close the stale "Compact start" tool_activity record
    from sync_engine import _end_compact_activity
    _db = SessionLocal()
    try:
        _act_id = _end_compact_activity(_db, agent_id, ctx.session_id)
        if _act_id:
            _db.commit()
            from websocket import emit_tool_activity as _emit_ta
            asyncio.ensure_future(_emit_ta(
                agent_id, "Compact", "end",
                tool_output="compact abandoned",
            ))
    finally:
        _db.close()
    ad.wake_sync(agent_id)


# ---- Between-turns compaction hold ----
#
# Claude Code >= 2.1.300 also compacts *between* turns: after ~54 min idle
# (just before the 1h prompt cache expires) it compacts in the background,
# and its summary precompute can run with no turn in flight. Both fire
# PreCompact(trigger="auto") — the same trigger as a mid-turn context-fill
# auto-compact — and no Stop hook ever follows. The hook payloads carry no
# field that tells them apart, but CC only compacts between turns when no
# turn is active, so a PreCompact that finds the agent IDLE, with no
# UserPromptSubmit/Stop in the last _TURN_HOOK_QUIET_S, is one. (The quiet
# window keeps back-to-back turns and blocked-Stop continuations — where the
# DB can briefly read IDLE mid-turn — on the old path; CC's idle compaction
# fires >= 30 min after the turn ends.)
#
# PreCompact still flips such an agent EXECUTING (dispatch must stay
# blocked while the pane is compacting) and takes a hold recording that the
# flip is ours to undo. The hold ends in exactly one of these ways:
#   - PostCompact → flush the trailing entries, flip IDLE, dispatch queued
#   - UserPromptSubmit / Stop → a real turn owns the status; just drop it
#   - the compaction fork's SubagentStop (unknown subagent whose output
#     carries <summary>), then no PostCompact within
#     _IDLE_COMPACT_FORK_GRACE_S (a summary precompute that only applies on
#     the next turn, or a failed apply) → flip IDLE, dispatch queued
#   - no PostCompact within _IDLE_COMPACT_MAX_S (blocked / failed / aborted
#     compaction; real compactions have taken up to ~315s on 1M-token
#     contexts) → flip IDLE, dispatch queued
# Module-level rather than on SyncContext: SessionStart(source=compact)
# rotates the session and installs a fresh SyncContext mid-window.
_IDLE_COMPACT_FORK_GRACE_S = 30.0
_IDLE_COMPACT_MAX_S = 600.0
_TURN_HOOK_QUIET_S = 60.0
_idle_compact_holds: dict[str, object] = {}
_last_turn_hook_at: dict[str, float] = {}  # agent_id -> monotonic time of last USP/Stop


def _note_turn_hook(agent_id: str, event: str) -> None:
    """UserPromptSubmit / Stop: a real turn owns the status from here on."""
    _last_turn_hook_at[agent_id] = time.monotonic()
    if _idle_compact_holds.pop(agent_id, None) is not None:
        logger.info("Idle compact hold dropped for %s: %s", agent_id[:8], event)


def _take_idle_compact_hold(ad, agent_id: str) -> None:
    """Start (or renew) the between-turns hold; a new token voids old watchdogs."""
    token = object()
    _idle_compact_holds[agent_id] = token
    asyncio.ensure_future(_idle_compact_watchdog(
        ad, agent_id, token, _IDLE_COMPACT_MAX_S, "no PostCompact",
    ))


def _jsonl_idle_before_last_compact(jsonl_path: str,
                                    tail_bytes: int = 8 << 20) -> bool | None:
    """Was the session between turns when its latest compaction started?

    Fallback for a PostCompact that has no hold because the orchestrator
    restarted mid-compaction. CC (>= 2.1.300) appends the compact_boundary,
    so walk back from the last one to the nearest turn marker: a
    stop_hook_summary / turn_duration entry means the previous turn had
    ended; a user / assistant entry or a queued prompt means a turn was in
    flight. None when undecidable (caller keeps the old behavior).
    """
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            lines = f.read().splitlines()
    except OSError:
        return None
    for i in range(len(lines) - 1, -1, -1):
        if b'"compact_boundary"' in lines[i]:
            break
    else:
        return None
    for line in reversed(lines[:i]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        etype = entry.get("type")
        if etype == "system" and entry.get("subtype") in ("stop_hook_summary", "turn_duration"):
            return True
        if etype == "queue-operation":
            return False
        if etype in ("user", "assistant") and not entry.get("isMeta"):
            return False
    return None


def _flip_executing_to_idle(agent_id: str, reason: str) -> bool:
    """EXECUTING → IDLE (DB + WS emit). Returns True if it flipped."""
    db = SessionLocal()
    try:
        ag = db.get(Agent, agent_id)
        if not ag or ag.status != AgentStatus.EXECUTING:
            return False
        ag.status = AgentStatus.IDLE
        ag.generating_msg_id = None
        db.commit()
        from websocket import emit_agent_update
        asyncio.ensure_future(emit_agent_update(agent_id, "IDLE", ag.project))
        logger.info("%s: agent %s → IDLE", reason, agent_id[:8])
        return True
    except Exception:
        db.rollback()
        logger.exception("%s: failed to flip %s to IDLE", reason, agent_id[:8])
        return False
    finally:
        db.close()


async def _idle_compact_watchdog(ad, agent_id: str, token: object,
                                 delay: float, reason: str) -> None:
    """Release the hold if it is still `token` after `delay` seconds."""
    await asyncio.sleep(delay)
    if _idle_compact_holds.get(agent_id) is not token:
        return
    del _idle_compact_holds[agent_id]
    logger.warning("Idle compact hold expired for %s after %.0fs (%s)",
                   agent_id[:8], delay, reason)
    flipped = _flip_executing_to_idle(agent_id, "Idle compact hold expired")
    try:
        _reset_compact_abandoned(ad, agent_id, reason)
    except Exception:
        logger.exception("Idle compact hold: compact-flag reset failed for %s",
                         agent_id[:8])
    if flipped:
        await ad.dispatch_pending_message(agent_id, delay=0)


async def _note_dropbox_activity(agent_id: str, reason: str) -> None:
    """Fire-and-forget helper: notify dropbox sync engine of agent activity.

    Imports lazily and swallows every exception so it never affects hook
    latency or correctness.
    """
    try:
        from dropbox_sync.engine import note_agent_activity
        await note_agent_activity(agent_id, reason)
    except Exception:
        pass


async def _await_jsonl_flush(
    ad, agent_id: str, *,
    wait_for: bytes | None = None,
    timeout: float = 10.0,
) -> bool:
    """Wait for CC to flush new JSONL content after the hook fired.

    Two-phase strategy:

    1. **Fixed 150ms sleep** (JSONL_FLUSH_DELAY) — covers the common case
       where CC flushes within this window. After the sleep, check the
       new bytes appended since the *baseline at function entry*:
       - ``wait_for=None``  → any growth counts (legacy any-grew judgement).
       - ``wait_for=bytes`` → only return True if the marker is in the new
         bytes (semantic judgement; eliminates the Stop-hook race where
         Phase 1 fires on a late-flushed assistant entry instead of the
         actual ``stop_hook_summary`` we are waiting for).

    2. **Watchdog loop** — if Phase 1 didn't detect the marker, install
       a watchdog listener and re-check after each modify event, until
       the marker appears or the remaining budget is spent. The loop
       (vs the original one-shot) matters under ``wait_for=bytes``:
       an unrelated entry can wake the watchdog before the marker lands.

    Why baseline is "size at function entry" and NOT ctx.last_offset:
    unrelated housekeeping writes (e.g. earlier PreToolUse attachments)
    that happened between the last sync and the current hook would inflate
    a last_offset-based check, causing a false-positive "flush observed"
    that misses the actual content we're waiting for.

    Also briefly polls for the sync context to be installed — at
    SessionStart, the launch task races with this hook to register ctx.

    Returns True if flush observed (in either phase), False on timeout or
    if no sync context could be resolved.
    """
    if not ad:
        return False
    ctx = ad._sync_contexts.get(agent_id)
    if ctx is None:
        # Brief poll for ctx install (SessionStart race; ~10-20ms typical).
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 0.5
        while ctx is None and loop.time() < deadline:
            await asyncio.sleep(0.02)
            ctx = ad._sync_contexts.get(agent_id)
        if ctx is None:
            return False
    if not ctx.jsonl_path:
        return False

    try:
        baseline = os.path.getsize(ctx.jsonl_path)
    except OSError:
        return False

    def _check_satisfied() -> tuple[bool, int]:
        """Return (satisfied, current_size). Reads file once."""
        try:
            cur = os.path.getsize(ctx.jsonl_path)
        except OSError:
            return False, baseline
        if cur <= baseline:
            return False, cur
        if wait_for is None:
            return True, cur
        # Semantic check: read the tail bytes since baseline and look for
        # the marker. Bytes are contiguous (we read from a known offset
        # to current size), so the marker can't be split across reads.
        try:
            with open(ctx.jsonl_path, "rb") as f:
                f.seek(baseline)
                tail = f.read(cur - baseline)
        except OSError:
            return False, cur
        return (wait_for in tail), cur

    # Phase 1: fixed 150ms sleep, then check.
    from config import JSONL_FLUSH_DELAY
    await asyncio.sleep(JSONL_FLUSH_DELAY)
    satisfied, cur = _check_satisfied()
    if satisfied:
        logger.info(
            "_await_jsonl_flush: agent=%s phase=1 %s (baseline=%d → %d, grew=%d bytes)",
            agent_id[:8],
            "marker found" if wait_for else "grew",
            baseline, cur, cur - baseline,
        )
        return True

    # Phase 2: watchdog loop — wake on each modify and re-check.
    from agent_dispatcher import wait_for_jsonl_flush
    remaining = max(0.05, timeout - JSONL_FLUSH_DELAY)
    t_p2_start = time.monotonic()
    deadline = t_p2_start + remaining
    wakes = 0
    while True:
        budget = deadline - time.monotonic()
        if budget <= 0:
            break
        woke = await wait_for_jsonl_flush(ctx.jsonl_path, timeout=budget)
        if not woke:
            break  # watchdog timed out
        wakes += 1
        satisfied, cur = _check_satisfied()
        if satisfied:
            elapsed_ms = (time.monotonic() - t_p2_start) * 1000
            logger.info(
                "_await_jsonl_flush: agent=%s phase=2 %s after %.0fms / %d wakes "
                "(baseline=%d → %d, grew=%d bytes)",
                agent_id[:8],
                "marker found" if wait_for else "grew",
                elapsed_ms, wakes, baseline, cur, cur - baseline,
            )
            return True
        # Otherwise loop: marker not present yet, keep waiting.

    elapsed_ms = (time.monotonic() - t_p2_start) * 1000
    try:
        cur = os.path.getsize(ctx.jsonl_path)
    except OSError:
        cur = baseline
    logger.warning(
        "_await_jsonl_flush: agent=%s phase=2 TIMEOUT after %.0fms / %d wakes "
        "(baseline=%d → %d, grew=%d bytes, wait_for=%r — CC flush > %ds)",
        agent_id[:8], elapsed_ms, wakes, baseline, cur, cur - baseline,
        wait_for, int(remaining),
    )
    return False


# ---- Claude Code Hooks Endpoints ----

# Stop hook signal file directory.  The dispatcher reads (and deletes)
# these when harvesting task completions.
_HOOK_SIGNAL_DIR = os.path.join(tempfile.gettempdir(), "xy-hooks")




# Claude Code's SessionEnd `reason` values that mean "session is rotating —
# a new SessionStart will follow shortly, keep the agent slot alive".  Anything
# else (prompt_input_exit, logout, other, bypass_permissions_disabled, …) is
# terminal: the underlying CLI process is gone for good and the agent should
# transition to STOPPED so /api/messages stops accepting work for it.
_SESSION_END_ROTATION_REASONS = frozenset({"clear", "resume"})


@router.post("/api/hooks/agent-session-end")
async def hook_agent_session_end(request: Request):
    """Receive SessionEnd hook — deterministic signal that a CLI session ended.

    Replaces JSONL tail scanning (_session_has_ended polling) as the primary
    mechanism for detecting session completion.  The sync loop's polling-based
    check remains as a fallback for abnormal exits that don't fire hooks.

    Branches on the hook payload's `reason` field:
      - clear / resume     → session rotating, expect new SessionStart.
      - everything else    → terminal exit (e.g. /exit, logout), stop the agent.

    Agent-id resolution goes through _resolve_agent_for_hook — pane → DB
    is authoritative, session_id → DB (cli_sync only) is the fallback. The
    X-Agent-Id header is intentionally ignored (env-leak-immune). Brand-new
    claude sessions xylocopa has never seen do not match either path, so
    the handler early-returns and no destructive action fires.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    agent_id = _resolve_agent_for_hook(request, body)
    if not agent_id:
        logger.warning("hook_agent_session_end: no agent match (pane=%s sid=%s)",
                       request.headers.get("X-Tmux-Pane", ""),
                       (body.get("session_id") if isinstance(body, dict) else "") or "")
        return {}

    # Guard: ignore hooks from subprocess sessions (Agent tool inherits XY_AGENT_ID)
    hook_sid = body.get("session_id", "") if isinstance(body, dict) else ""
    if _is_subprocess_session(agent_id, hook_sid, request):
        logger.info("hook_agent_session_end: ignoring subprocess session %s for agent %s",
                    hook_sid[:12], agent_id[:8])
        return {}

    ad = getattr(request.app.state, "agent_dispatcher", None)
    if not ad:
        logger.warning("hook_agent_session_end: no agent_dispatcher on app.state for agent %s", agent_id[:8])
        return {}

    reason = (body.get("reason") if isinstance(body, dict) else None) or "other"
    is_rotation = reason in _SESSION_END_ROTATION_REASONS

    ctx = ad._sync_contexts.get(agent_id)
    if ctx and is_rotation:
        # Rotation: SessionStart should accept the next session in this slot.
        ctx.awaiting_rotation = True

    # Mark any EXECUTING long-running command (/loop, /goal) as completed —
    # Stop hook skips these because Stop fires after each iteration/round,
    # but SessionEnd is terminal.
    import slash_commands as _sc
    _sc.mark_long_running_completed(agent_id)

    # Drain old session's pending JSONL turns into DB before rotation (or
    # before STOPPED takes effect, so the final turns make it in).  Without
    # this, any turn produced in the hook-silent window since the last sync
    # (e.g. final assistant turn before /clear) would never be imported.
    #
    # No `wait_for` marker: this is an intentional generic-drain — we want
    # to flush whatever pending writes are queued, not a specific entry.
    # Unlike Stop hook (which needs to wait for the specific subtype that
    # CC writes AFTER the hook returns), SessionEnd's relevant entries
    # are already written by the time the hook fires; "any growth" within
    # the 150ms window is the correct semantic here.
    if ctx:
        await _await_jsonl_flush(ad, agent_id)
        await ad._drain_session_sync(agent_id)

    # Terminal exit (/exit, logout, etc.) → STOP the agent.  Subsequent
    # /api/messages POSTs will be rejected with "Agent is stopped" at
    # agents.py's existing gate.
    if not is_rotation:
        from database import SessionLocal
        from models import Agent as _Agent
        _db = SessionLocal()
        try:
            _agent = _db.get(_Agent, agent_id)
            if _agent:
                ad.stop_agent_cleanup(
                    _db, _agent,
                    reason=f"session ended ({reason})",
                    kill_tmux=True,
                    emit=True,
                    add_message=True,
                    fail_executing=False,
                    cancel_tasks=True,
                )
                _db.commit()
                # stop_agent_cleanup writes the "session ended" system
                # message to DB but doesn't flush display — do it here so
                # the bubble appears in chat without a manual page refresh.
                from display_writer import flush_agent
                flush_agent(agent_id)
        finally:
            _db.close()

    logger.info("hook_agent_session_end: agent=%s reason=%s rotation=%s",
                agent_id[:8], reason, is_rotation)
    return {}


    # Slash command delivery/completion moved to slash_commands module.


@router.post("/api/hooks/agent-user-prompt")
async def hook_agent_user_prompt(request: Request):
    """Receive UserPromptSubmit hook — flip status, ring the bell.

    This handler does NOT touch Message rows or the display file. It does
    two hook-owned things:

      1. Flip agent.status IDLE/STARTING → EXECUTING (DB write + WS emit).
         JSONL is the canonical truth for status, but the user turn isn't
         always flushed by the time wake_sync would otherwise run, so the
         hook itself writes EXECUTING for snappy + reliable UI feedback.
         Stop-side transitions still flow through sync (they need the
         JSONL stop_hook entry first).
      2. Wake the sync loop once CC actually flushes the user turn (via
         _await_jsonl_flush watchdog) so sync can import the newly-written
         turn, match it to the pre-dispatched web message (if any), set
         delivered_at/jsonl_uuid/status in one commit, and promote it into
         the display file's delivered partition.

    The green "delivered" tick therefore surfaces ~300-500ms after the hook
    fires (JSONL flush delay + sync cycle). Gained in exchange: there is no
    longer a window where delivered_at is set but jsonl_uuid is not, so a
    mid-turn restart can't leave a row that confuses the subsequent
    sync-time promotion. Single-writer → no promote-vs-flush race.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    agent_id = _resolve_agent_for_hook(request, body)
    if not agent_id:
        logger.warning("hook_agent_user_prompt: no agent match (pane=%s sid=%s)",
                       request.headers.get("X-Tmux-Pane", ""),
                       (body.get("session_id") if isinstance(body, dict) else "") or "")
        return {}

    # Guard: ignore hooks from subprocess sessions (Agent tool inherits XY_AGENT_ID)
    hook_sid = body.get("session_id", "") if isinstance(body, dict) else ""
    if _is_subprocess_session(agent_id, hook_sid, request):
        logger.info("hook_agent_user_prompt: ignoring subprocess session %s for agent %s",
                    hook_sid[:12], agent_id[:8])
        return {}

    logger.info("hook_agent_user_prompt: received for agent %s", agent_id[:8])
    # A real turn now owns EXECUTING; its Stop ends it, not PostCompact.
    _note_turn_hook(agent_id, "UserPromptSubmit")

    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad:
        # Compact-abandoned detection (same logic as tool-activity handler)
        ctx = ad._sync_contexts.get(agent_id)
        if ctx and ctx.compact_notified:
            _reset_compact_abandoned(ad, agent_id, "sent UserPromptSubmit")

        # GHOST_DELIVERED probe: ack the most recent un-acked promote
        # so we can correlate hook-arrival to send-keys.
        try:
            ad._ack_promote_on_user_prompt(agent_id)
        except Exception:
            logger.exception("GHOST_PROBE ack_on_user_prompt failed")
        # Flip IDLE/STARTING → EXECUTING here for snappy UI feedback. The
        # JSONL-driven path (sync_engine._infer_status_from_signals) is
        # still the canonical writer, but _await_jsonl_flush below can
        # time out (10s) if CC stalls; the hook-side write avoids leaving
        # the agent stuck IDLE in that edge case. Stop-side transitions
        # still flow through sync (they need the JSONL stop_hook entry to
        # be promoted first).
        try:
            _db_usp = SessionLocal()
            try:
                _ag = _db_usp.get(Agent, agent_id)
                if _ag and _ag.status in (AgentStatus.IDLE, AgentStatus.STARTING):
                    _ag.status = AgentStatus.EXECUTING
                    _db_usp.commit()
                    from websocket import emit_agent_update as _eau
                    ad._emit(_eau(agent_id, "EXECUTING", _ag.project))
                    logger.info(
                        "hook_agent_user_prompt: flipped %s IDLE→EXECUTING",
                        agent_id[:8],
                    )
            finally:
                _db_usp.close()
        except Exception:
            logger.exception("hook_agent_user_prompt: status flip failed")
        # Wake sync so message-state writer (delivered tick, jsonl_uuid
        # match) still runs once CC flushes the user turn.
        #
        # No `wait_for` marker: USP fires AFTER CC writes the user entry,
        # so the entry-of-interest is either already in baseline or lands
        # well within the 150ms Phase 1 window.  "Any growth" is reliable
        # here because nothing else writes between user-entry flush and
        # our hook return (CC is waiting for our HTTP response before it
        # invokes the model and starts streaming the assistant turn).
        # This is structurally different from Stop hook's race.
        logger.info("hook_agent_user_prompt: waking sync for %s", agent_id[:8])
        async def _post_prompt_sync(_aid):
            await _await_jsonl_flush(ad, _aid)
            ad.wake_sync(_aid)
        asyncio.ensure_future(_post_prompt_sync(agent_id))

    return {}


@router.post("/api/hooks/agent-stop")
async def hook_agent_stop(request: Request):
    """Receive Stop hook from Claude Code agents.

    Caches the last_assistant_message for the dispatcher and clears
    generating state so the frontend receives agent_stream_end.

    Push notifications are triggered from the JSONL sync loop (in
    agent_dispatcher) at the same moment unread_count increments, so
    badge and push are always in sync.

    Stop fires per conversation turn, not just at task completion, so this
    endpoint deliberately does NOT transition task state.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    agent_id = _resolve_agent_for_hook(request, body)
    if not agent_id:
        logger.warning("hook_agent_stop: no agent match (pane=%s sid=%s)",
                       request.headers.get("X-Tmux-Pane", ""),
                       (body.get("session_id") if isinstance(body, dict) else "") or "")
        return {}

    # Guard: ignore hooks from subprocess sessions (Agent tool inherits XY_AGENT_ID)
    hook_sid = body.get("session_id", "") if isinstance(body, dict) else ""
    if _is_subprocess_session(agent_id, hook_sid, request):
        logger.info("hook_agent_stop: ignoring subprocess session %s for agent %s",
                    hook_sid[:12], agent_id[:8])
        return {}

    # A turn ended — sync flips IDLE from its stop_hook_summary.
    _note_turn_hook(agent_id, "Stop")

    # Notify dropbox sync of agent turn end
    asyncio.ensure_future(_note_dropbox_activity(agent_id, "stop"))

    # All stop-hook operations (_stop_generating, unread, notify, dispatch
    # pending, slash-command completion) are handled by the sync engine when
    # it imports the stop_hook_summary entry from JSONL.  This handler only
    # needs to wake the sync loop so it picks up the new JSONL content.
    #
    # The wait_for marker is critical: without it, Phase 1's "any grew"
    # judgement returns True on late-flushed pre-Stop entries (typically
    # the assistant text), wakes sync before stop_hook_summary lands,
    # sync misses the summary entry, agent stays EXECUTING until the
    # next hook event happens to catch up.  Production logs (2026-05-12)
    # showed this race firing on ~6% of Stop hooks, with ~1% stuck for
    # >15 minutes.
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad:
        logger.info("hook_agent_stop: waking sync for %s", agent_id[:8])
        ctx = ad._sync_contexts.get(agent_id)
        if ctx:
            if ctx.compact_notified:
                _reset_compact_abandoned(ad, agent_id, "sent Stop")
            else:
                async def _post_stop_sync(_aid):
                    await _await_jsonl_flush(
                        ad, _aid,
                        wait_for=b'"subtype":"stop_hook_summary"',
                    )
                    ad.wake_sync(_aid)
                asyncio.ensure_future(_post_stop_sync(agent_id))
        else:
            logger.debug(
                "hook_agent_stop: no sync context for %s",
                agent_id[:8],
            )
    else:
        logger.warning("hook_agent_stop: no agent_dispatcher on app.state")

    return {}


@router.post("/api/hooks/agent-post-compact")
async def hook_agent_post_compact(request: Request):
    """Receive PostCompact hook — ring the bell, let sync reconcile.

    Hook-owned responsibilities:

      1. Flip `ctx.compact_notified`/`compact_end_emitted` (in-memory sync
         coordination flags — not DB state).
      2. Emit the "Compact end" tool_activity WS event for snappy UI.
      3. Drain + run compact full_scan so /compact mark-completed, boundary
         bubbles, and tool_activity finalization land before the hook
         returns.
      4. Decide the EXECUTING→IDLE transition:
           manual              → user-invoked /compact, turn is over → flip IDLE
           auto, idle hold     → between-turns compaction (CC idle compaction;
                                 agent was IDLE at PreCompact) → flip IDLE
           auto, no hold       → mid-turn context-fill auto-compact, original
                                 task continues → no-op (its Stop ends it)
         The hold lives in `_idle_compact_holds` (module-level), not on
         SyncContext, so it survives the ctx rotation that happens between
         PreCompact and PostCompact.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    agent_id = _resolve_agent_for_hook(request, body)
    if not agent_id:
        logger.warning("hook_agent_post_compact: no agent match (pane=%s sid=%s)",
                       request.headers.get("X-Tmux-Pane", ""),
                       (body.get("session_id") if isinstance(body, dict) else "") or "")
        return {}

    # Guard: ignore hooks from subprocess sessions
    hook_sid = body.get("session_id", "") if isinstance(body, dict) else ""
    if _is_subprocess_session(agent_id, hook_sid, request):
        logger.info("hook_agent_post_compact: ignoring subprocess session %s for agent %s",
                    hook_sid[:12], agent_id[:8])
        return {}

    ad = getattr(request.app.state, "agent_dispatcher", None)
    if not ad:
        logger.warning("hook_agent_post_compact: no agent_dispatcher")
        return {}

    logger.info("hook_agent_post_compact: compact done for %s", agent_id[:8])

    ctx = ad._sync_contexts.get(agent_id)
    if ctx:
        ctx.compact_notified = False
        ctx.compact_notified_at = 0.0
        ctx.compact_end_emitted = True  # prevent duplicate from sync engine
        ctx.compact_detected_at = 0.0

    # "Compact end" tool activity WS event — transient UI signal, not DB.
    from websocket import emit_tool_activity, emit_context_usage
    await emit_tool_activity(agent_id, "Compact", "end",
                             tool_output="context compacted")

    # Synchronously drain JSONL and run compact full_scan BEFORE returning.
    # This guarantees: /compact user msg flips to COMPLETED double-check,
    # the boundary + summary sys bubbles land in DB, the compact tool_activity
    # row is finalized — all visible by the time the hook returns.
    #
    # No _await_jsonl_flush: PostCompact fires AFTER CC has rewritten the
    # JSONL (in-place rotation) and the new boundary + summary entries
    # are already on disk.  Flushing here waited up to 10s for file
    # growth that never comes (manual) or isn't needed (auto — sync
    # loop picks up trailing writes naturally).
    if ctx:
        await ad._drain_session_sync(agent_id, run_compact_full_scan=True)

    # Compact resets the in-session running counter (in-place rotation);
    # push a fresh breakdown so the pill's 5-component view shrinks
    # immediately. Emitted AFTER the drain so the snapshot reflects the
    # post-compact JSONL, not stale pre-compact bytes.
    await emit_context_usage(agent_id)

    # CC writes compact summary entries (isCompactSummary + "Conversation
    # compacted") AFTER firing PostCompact.  If we flip IDLE and dispatch
    # the queued message now, the user bubble lands in the DB before the
    # summary sys bubbles — wrong display order.
    #
    # Solution: defer the EXECUTING→IDLE flip (and the dispatch that
    # follows) until the summary entries arrive.  dispatch_pending_message
    # self-checks agent.status and bails while EXECUTING, so no early
    # dispatch can sneak through.
    #
    # A mid-turn auto-compact stays EXECUTING (original task continues), so
    # only manual and between-turns (idle hold) compactions take this path.
    # For idle compaction CC appends its "Compacted while idle" notice right
    # after this hook returns — the flush wait lands it before the dispatch.
    _trigger = (body.get("trigger") if isinstance(body, dict) else None) or "manual"
    _hold = _idle_compact_holds.get(agent_id)
    if _trigger != "manual" and _hold is None and agent_id not in _last_turn_hook_at:
        # No hold and no turn hook seen by this process: the orchestrator
        # restarted mid-compaction and lost the in-memory hold. Ask the
        # JSONL whether a turn was in flight when the compaction started.
        _ctx_now = ad._sync_contexts.get(agent_id)
        if _ctx_now and _ctx_now.jsonl_path and _jsonl_idle_before_last_compact(_ctx_now.jsonl_path):
            logger.info(
                "PostCompact: no hold for %s (restart?) but JSONL shows the "
                "compaction started between turns", agent_id[:8],
            )
            _take_idle_compact_hold(ad, agent_id)
            _hold = _idle_compact_holds.get(agent_id)
    if _trigger != "manual" and _hold is None:
        logger.info(
            "PostCompact: trigger=%s mid-turn, keep agent %s EXECUTING",
            _trigger, agent_id[:8],
        )
    else:
        if _hold is not None:
            logger.info(
                "PostCompact: trigger=%s between turns (agent %s was idle at "
                "PreCompact), returning it to IDLE",
                _trigger, agent_id[:8],
            )

        async def _post_compact_finalize():
            flushed = await _await_jsonl_flush(ad, agent_id, timeout=8.0)
            if flushed:
                await ad._drain_session_sync(agent_id)

            if _hold is not None:
                if _idle_compact_holds.get(agent_id) is not _hold:
                    # A turn started during the flush wait (UserPromptSubmit
                    # dropped the hold) or a watchdog already released it.
                    logger.info(
                        "PostCompact: idle hold for %s gone before finalize, "
                        "leaving status alone", agent_id[:8],
                    )
                    return
                del _idle_compact_holds[agent_id]

            _flip_executing_to_idle(agent_id, "PostCompact")
            await ad.dispatch_pending_message(agent_id, delay=0)
        asyncio.ensure_future(_post_compact_finalize())

    logger.info("hook_agent_post_compact: agent=%s", agent_id[:8])
    return {}


@router.post("/api/hooks/agent-tool-activity")
async def hook_agent_tool_activity(request: Request):
    """Receive PreToolUse/PostToolUse hooks — broadcast tool activity to frontend.

    Gives users real-time visibility into which tool the agent is running,
    replacing the unreliable JSONL-polling approach that loses tool info
    after the idle threshold (~6s).
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    agent_id = _resolve_agent_for_hook(request, body)
    if not agent_id:
        logger.warning("hook_agent_tool_activity: no agent match (pane=%s sid=%s)",
                       request.headers.get("X-Tmux-Pane", ""),
                       (body.get("session_id") if isinstance(body, dict) else "") or "")
        return {}

    # Guard: ignore hooks from subprocess sessions.
    hook_sid = body.get("session_id", "") if isinstance(body, dict) else ""
    hook_event = body.get("hook_event_name", "")
    if _is_subprocess_session(agent_id, hook_sid, request):
        return {}

    from websocket import emit_tool_activity, _tool_input_summary, _tool_output_summary

    ad = getattr(request.app.state, "agent_dispatcher", None)

    # Compact-abandoned detection: if CC sends a non-PreCompact hook while
    # compact_notified is True, the compact operation failed (e.g. API error)
    # and CC resumed work.  Reset the flag so sync resumes immediately.
    if ad and hook_event != "PreCompact":
        ctx = ad._sync_contexts.get(agent_id)
        if ctx and ctx.compact_notified:
            _reset_compact_abandoned(ad, agent_id, f"sent {hook_event}")

    # Wake sync — import new assistant turns written to JSONL between
    # UserPromptSubmit and Stop.  Bare wake_sync handles the common case
    # where CC has already flushed; the background _await_jsonl_flush
    # covers the case where CC fires the hook before the JSONL write
    # lands on disk (observed with batch-flush behaviour on long tool
    # sequences).
    if ad:
        ad.wake_sync(agent_id)
        async def _tool_flush_then_wake(_aid):
            await _await_jsonl_flush(ad, _aid)
            ad.wake_sync(_aid)
        asyncio.ensure_future(_tool_flush_then_wake(agent_id))

    tool_name = phase = summary = output_summary = ""
    is_error = False
    kind = "tool"

    # --- Tool lifecycle ---
    if hook_event == "PreToolUse":
        tool_name = body.get("tool_name", "")
        phase = "start"
        tool_input = body.get("tool_input")
        summary = _tool_input_summary(tool_name, tool_input) if tool_input else ""
        await emit_tool_activity(agent_id, tool_name, phase, tool_input=tool_input)
        # Interactive cards (AskUserQuestion/ExitPlanMode): wake sync loop
        # immediately so it imports the assistant turn from JSONL. By the
        # time PreToolUse fires, the tool_use block is already in JSONL.
        if tool_name in ("AskUserQuestion", "ExitPlanMode") and ad:
            # Event-driven wake: watch the JSONL file for the CC tool_use
            # flush. Replaces a fixed JSONL_FLUSH_DELAY sleep that missed
            # cases where CC's internal buffer flushed slower than expected.
            #
            # No `wait_for` marker: the tool_use block is written BEFORE
            # PreToolUse fires (CC's order is "write entry → fire hook"),
            # so it's either already in baseline or lands within 150ms.
            # No race with a post-hook write, unlike Stop hook.
            async def _wait_jsonl_then_wake(_aid):
                await _await_jsonl_flush(ad, _aid)
                ad.wake_sync(_aid)
            asyncio.ensure_future(_wait_jsonl_then_wake(agent_id))
    elif hook_event in ("PostToolUse", "PostToolUseFailure"):
        tool_name = body.get("tool_name", "")
        phase = "end"
        is_error = hook_event == "PostToolUseFailure"
        tool_input = body.get("tool_input")
        tool_output = body.get("tool_output") or body.get("tool_error") or None
        summary = _tool_input_summary(tool_name, tool_input) if tool_input else ""
        output_summary = _tool_output_summary(tool_name, tool_output, is_error) if tool_output else ""
        await emit_tool_activity(agent_id, tool_name, phase, tool_input=tool_input,
                                  tool_output=tool_output, is_error=is_error)
        # Notify dropbox sync of write-like tool completions
        if (hook_event == "PostToolUse"
                and not is_error
                and tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")):
            asyncio.ensure_future(_note_dropbox_activity(agent_id, "write"))
        # Backfill interactive card answers from PostToolUse
        if tool_name in ("AskUserQuestion", "ExitPlanMode") and tool_output:
            tool_use_id = body.get("tool_use_id", "")
            if tool_use_id:
                from database import SessionLocal as _SL
                _db = _SL()
                try:
                    # Check if agent has skip_permissions (auto-approval)
                    _ag = _db.get(Agent, agent_id)
                    is_auto = bool(_ag and _ag.skip_permissions) if _ag else False

                    # Find ALL card messages with this tool_use_id and patch any
                    # that still have answer=None.
                    _answer_text = str(tool_output)[:500]
                    _patched_any = False
                    _msgs = _db.query(Message).filter(
                        Message.agent_id == agent_id,
                        Message.tool_use_id == tool_use_id,
                    ).order_by(Message.created_at.desc()).all()
                    for _msg in _msgs:
                        try:
                            _meta = json.loads(_msg.meta_json)
                        except (json.JSONDecodeError, TypeError):
                            logger.debug("Malformed meta_json for message %s", _msg.id)
                            continue
                        _msg_changed = False
                        for _item in _meta.get("interactive", []):
                            if _item.get("tool_use_id") != tool_use_id:
                                continue
                            if _item.get("answer") is not None:
                                # Already answered (e.g. JSONL had tool_result).
                                # Still tag auto_approved if missing.
                                if is_auto and not _item.get("auto_approved"):
                                    _item["auto_approved"] = True
                                    _msg_changed = True
                                continue
                            _item["answer"] = _answer_text
                            if is_auto:
                                _item["auto_approved"] = True
                            from agent_dispatcher import _derive_selected_index
                            _derive_selected_index(_item)
                            _msg_changed = True
                        if _msg_changed:
                            _msg.meta_json = json.dumps(_meta)
                            _patched_any = True
                    if _patched_any:
                        _db.commit()
                        # Re-read and emit for each patched message
                        for _msg in _msgs:
                            try:
                                _meta = json.loads(_msg.meta_json)
                            except (json.JSONDecodeError, TypeError):
                                logger.debug("Malformed meta_json for message %s", _msg.id)
                                continue
                            for _item in _meta.get("interactive", []):
                                if _item.get("tool_use_id") == tool_use_id and _item.get("answer") == _answer_text:
                                    from websocket import emit_metadata_update
                                    await emit_metadata_update(agent_id, _msg.id)
                                    break
                finally:
                    _db.close()
    # --- Subagent lifecycle ---
    elif hook_event == "SubagentStart":
        agent_type = body.get("agent_type", "subagent")
        tool_name = f"Agent:{agent_type}"
        phase = "start"
        kind = "subagent"
        desc = body.get("description", "") or body.get("prompt", "")[:80] or ""
        summary = desc
        await emit_tool_activity(agent_id, tool_name, phase,
                                  tool_input={"description": desc} if desc else None)
        # Create Agent record immediately so UI shows the subagent
        sub_agent_id = body.get("agent_id", "")
        if not sub_agent_id:
            logger.warning("SubagentStart hook: no agent_id in body for parent %s", agent_id[:8])
        elif not ad:
            logger.warning("SubagentStart hook: no agent_dispatcher for parent %s", agent_id[:8])
        if ad and sub_agent_id:
            from database import SessionLocal as _SL
            from models import Agent as _Agent, AgentMode as _AM, AgentStatus as _AS
            from websocket import emit_agent_update as _eau
            _db = _SL()
            try:
                # Look up parent to get project name
                _parent = _db.get(Agent, agent_id)
                _project_name = _parent.project if _parent else ""
                _name = desc[:60] or f"subagent-{sub_agent_id[:8]}"
                _sub = _Agent(
                    project=_project_name,
                    name=_name,
                    mode=_AM.AUTO,
                    status=_AS.IDLE,
                    cli_sync=True,
                    parent_id=agent_id,
                    is_subagent=True,
                    claude_agent_id=sub_agent_id,
                )
                _db.add(_sub)
                _db.commit()
                # Register in known_subagents
                known = ad._known_subagents.setdefault(agent_id, {})
                known[sub_agent_id] = {
                    "agent_id": _sub.id,
                    "last_size": 0,
                    "idle_polls": 0,
                }
                ad._emit(_eau(_sub.id, "IDLE", _project_name))
                logger.info(
                    "SubagentStart hook: created subagent %s (%s) for parent %s",
                    _sub.id, _name, agent_id[:8],
                )
            finally:
                _db.close()
    elif hook_event == "SubagentStop":
        agent_type = body.get("agent_type", "subagent")
        tool_name = f"Agent:{agent_type}"
        phase = "end"
        kind = "subagent"
        output_summary = "done"
        await emit_tool_activity(agent_id, tool_name, phase, tool_output="done")
        # Final import of subagent messages + mark STOPPED
        sub_agent_id = body.get("agent_id", "")
        last_msg = body.get("last_assistant_message", "")
        transcript_path = body.get("agent_transcript_path", "")
        if not sub_agent_id:
            logger.warning("SubagentStop hook: no agent_id in body for parent %s", agent_id[:8])
        elif not ad:
            logger.warning("SubagentStop hook: no agent_dispatcher for parent %s", agent_id[:8])
        if ad and sub_agent_id:
            from database import SessionLocal as _SL
            from agent_dispatcher import _parse_session_turns
            from websocket import emit_agent_update as _eau
            known = ad._known_subagents.get(agent_id, {})
            info = known.get(sub_agent_id)
            if not info:
                logger.warning(
                    "SubagentStop hook: unknown subagent %s for parent %s (known: %s)",
                    sub_agent_id[:12], agent_id[:8], list(known.keys()),
                )
                # The compaction's own summary fork never sends SubagentStart
                # and returns a <summary> block (CC extracts it the same way);
                # other unknown stops (e.g. ~30s agent_summary forks of a
                # background agent) don't. An applied compaction sends
                # PostCompact within ~3s of the fork; a summary precompute
                # never does — release the hold if nothing follows.
                _hold = _idle_compact_holds.get(agent_id)
                if _hold is not None and "<summary>" in (last_msg or ""):
                    asyncio.ensure_future(_idle_compact_watchdog(
                        ad, agent_id, _hold, _IDLE_COMPACT_FORK_GRACE_S,
                        "compaction fork finished, no PostCompact",
                    ))
            if info:
                sub_db_id = info["agent_id"]
                _db = _SL()
                try:
                    # Final parse of subagent JSONL if transcript path available
                    if transcript_path and os.path.isfile(transcript_path):
                        turns = _parse_session_turns(transcript_path)
                        existing_count = _db.query(Message).filter(
                            Message.agent_id == sub_db_id,
                        ).count()
                        if len(turns) > existing_count:
                            ad._import_turns_as_messages_deduped(
                                _db, sub_db_id, turns[existing_count:],
                            )
                    sub_ag = _db.get(Agent, sub_db_id)
                    if sub_ag and sub_ag.status == AgentStatus.IDLE:
                        if last_msg:
                            _preview = str(last_msg)[:200] if isinstance(last_msg, str) else str(last_msg.get("content", ""))[:200]
                            sub_ag.last_message_preview = _preview
                        ad.stop_agent_cleanup(
                            _db, sub_ag, "",
                            kill_tmux=False, add_message=False,
                            cancel_tasks=False,
                        )
                        _db.commit()
                        _project_name = sub_ag.project or ""
                        ad._emit(_eau(sub_db_id, "STOPPED", _project_name))
                        # Flush the subagent's "stopped" sys message to its
                        # display file. flush_agent auto-emits new_message,
                        # so no explicit _enm needed.
                        from display_writer import flush_agent as _sub_flush
                        _sub_flush(sub_db_id)
                        logger.info(
                            "SubagentStop hook: marked subagent %s STOPPED",
                            sub_db_id,
                        )
                finally:
                    _db.close()
    # --- Context compaction ---
    elif hook_event == "PreCompact":
        tool_name = "Compact"
        phase = "start"
        kind = "compact"
        summary = "context compaction"
        await emit_tool_activity(agent_id, tool_name, phase)
        # /compact skips UserPromptSubmit. mark_delivered is deferred until
        # AFTER the drain below so the single-check appears once the old
        # session's final turns land in the DB. PostCompact then flips to
        # double-check when the compact rewrite is fully done.
        # Note: `body["trigger"]` ("manual"|"auto") is also present in the
        # PostCompact payload, so the IDLE-vs-EXECUTING decision is made
        # there directly — no need to stash the trigger here.
        _pc_trigger = body.get("trigger") or "manual"
        logger.info("PreCompact: trigger=%s for %s", _pc_trigger, agent_id[:8])

        # Mark agent EXECUTING for the entire compact window. Compact runs
        # for 30s-2.5min; during that span dispatch_pending_message must NOT
        # fire (the pane is in a non-input state — paste/Enter would land
        # somewhere wrong). PostCompact flips status back: manual → IDLE
        # (the /compact turn is over); auto mid-turn → keep EXECUTING (the
        # user's original task continues); auto between turns → IDLE (CC
        # idle compaction — no Stop will follow, see _idle_compact_holds).
        # Doing this for both triggers is symmetric and defensive: even if
        # CC's `trigger` field is unreliable or future modal-confirmation
        # paths don't fit the manual/auto dichotomy, dispatch is still
        # blocked until PostCompact resolves.
        _between_turns = False
        _since_turn_hook = time.monotonic() - _last_turn_hook_at.get(agent_id, float("-inf"))
        _db_status = SessionLocal()
        try:
            _ag = _db_status.get(Agent, agent_id)
            if _ag and _ag.status not in (AgentStatus.STOPPED, AgentStatus.ERROR):
                if _ag.status != AgentStatus.EXECUTING:
                    _between_turns = _since_turn_hook >= _TURN_HOOK_QUIET_S
                    if not _between_turns:
                        logger.info(
                            "PreCompact: %s reads %s but a turn hook fired %.1fs "
                            "ago — treating as in-turn compaction",
                            agent_id[:8], _ag.status.value, _since_turn_hook,
                        )
                    _ag.status = AgentStatus.EXECUTING
                    _db_status.commit()
                    from websocket import emit_agent_update as _eau
                    asyncio.ensure_future(_eau(agent_id, "EXECUTING", _ag.project))
                    logger.info(
                        "PreCompact: agent %s → EXECUTING for compact window",
                        agent_id[:8],
                    )
                elif agent_id in _idle_compact_holds:
                    # Still EXECUTING from an earlier between-turns hold
                    # (e.g. a summary precompute) — renew it.
                    _between_turns = True
        except Exception:
            _db_status.rollback()
            logger.exception("PreCompact: failed to set EXECUTING for %s", agent_id[:8])
        finally:
            _db_status.close()
        if _between_turns and ad:
            _take_idle_compact_hold(ad, agent_id)
        # Drain the old session's pending JSONL turns into the DB before
        # compact rewrites the file.  Without this, any turn produced in
        # the hook-silent window since the last sync (e.g. final assistant
        # reasoning before /compact) would only appear later with a
        # post-rotation created_at and mis-order against the rotation
        # marker.
        #
        # No `wait_for` marker: generic drain — we want to flush whatever
        # is pending before the file gets rewritten, not a specific entry.
        # Between-turns auto compaction has nothing in flight (the last
        # turn's Stop was already synced), so the flush wait would only
        # stall CC's compaction for its full 10s timeout.
        if ad and ad._sync_contexts.get(agent_id):
            if not (_between_turns and _pc_trigger == "auto"):
                await _await_jsonl_flush(ad, agent_id)
            await ad._drain_session_sync(agent_id)
            # Now pause sync — JSONL is about to be rewritten
            import time as _time
            ad._sync_contexts[agent_id].compact_notified = True
            ad._sync_contexts[agent_id].compact_notified_at = _time.monotonic()
        # Write a SYSTEM "Compacting context..." bubble immediately so the
        # user sees pre-compact feedback. Hook-owned: synthetic uuid keeps
        # sync's UUID-dedup from collision; source="hook" keeps compact
        # full_scan's cli-orphan purge from deleting it. The matching
        # "Conversation compacted" + summary bubbles arrive post-compact
        # via sync_full_scan import of JSONL boundary entries.
        from uuid import uuid4 as _uuid4
        _db_pre = SessionLocal()
        try:
            _pre_msg = Message(
                agent_id=agent_id,
                role=MessageRole.SYSTEM,
                kind="compact_start",
                content="Compacting context...",
                source="hook",
                status=MessageStatus.COMPLETED,
                jsonl_uuid=f"pre-compact-{_uuid4().hex[:12]}",
                created_at=_utcnow(),
                completed_at=_utcnow(),
                delivered_at=_utcnow(),
            )
            _db_pre.add(_pre_msg)
            _db_pre.commit()
            from display_writer import flush_agent as _flush_pre
            _flush_pre(agent_id)
        except Exception:
            logger.exception("PreCompact: failed to write sys bubble for %s", agent_id[:8])
        finally:
            _db_pre.close()
        # Drain finished — mark /compact delivered (single check in UI).
        import slash_commands as _sc
        _sc.mark_delivered(agent_id, "/compact")
    else:
        return {}

    # Wake the JSONL sync loop so new message content is picked up
    # immediately instead of waiting for the next poll cycle.
    if ad:
        ad.wake_sync(agent_id)

    return {}


async def _handle_ask_user_question(request, agent_id: str, tool_input: dict, tool_use_id: str = ""):
    """Block until user answers AskUserQuestion from web UI, return updatedInput.

    Called from hook_agent_permission for ALL agents (both skip_permissions and
    supervised).  Card creation follows the same pattern as permission cards:
    drain pending JSONL → persist card in DB → flush display → notify →
    broadcast → block.  Uses jsonl_uuid="interactive-{tool_use_id}" so the
    later JSONL sync deduplicates naturally.
    """
    from permissions import PermissionManager

    pm: PermissionManager | None = getattr(request.app.state, "permission_manager", None)
    if not pm:
        logger.warning("_handle_ask_user_question: no permission_manager for agent %s", agent_id[:8])
        return {}  # fallback: let TUI handle it

    questions = tool_input.get("questions", [])
    q_summary = questions[0].get("question", "Question") if questions else "Question"

    req = pm.create_request(
        agent_id, "AskUserQuestion", tool_input, q_summary,
        tool_use_id=tool_use_id,
    )

    from database import SessionLocal
    _db = SessionLocal()
    try:
        _ag = _db.get(Agent, agent_id)
        _agent_name = _ag.name if _ag else ""
        _agent_project = _ag.project if _ag else ""
    finally:
        _db.close()

    # Drain pending JSONL so card appears after preceding messages.
    # Note: CC doesn't flush ANY JSONL (not even preceding text) until the
    # hook returns, so drain typically finds delta=0. The drain is still
    # useful for importing turns from earlier sync cycles that haven't
    # been flushed to the display file yet.
    _ad = getattr(request.app.state, "agent_dispatcher", None)
    if _ad:
        try:
            await _ad._drain_session_sync(agent_id)
        except Exception:
            pass

    # Persist interactive card in DB (same pattern as permission cards)
    _card_uuid = f"interactive-{tool_use_id}" if tool_use_id else f"hookauq-{req.id}"
    _meta = {
        "interactive": [{
            "type": "ask_user_question",
            "tool_use_id": tool_use_id or _card_uuid,
            "request_id": req.id,
            "questions": questions,
            "answer": None,
        }],
    }
    _db_card = SessionLocal()
    try:
        _msg = Message(
            agent_id=agent_id,
            role=MessageRole.AGENT,
            kind=None,
            content="",
            source="hook",
            status=MessageStatus.COMPLETED,
            meta_json=json.dumps(_meta),
            tool_use_id=tool_use_id or _card_uuid,
            jsonl_uuid=_card_uuid,
        )
        _db_card.add(_msg)
        _db_card.commit()
        from display_writer import flush_agent
        flush_agent(agent_id)
        if _ad:
            _ad._bump_unread_and_notify_interactive(
                agent_id,
                f"[interactive cards] {q_summary}",
            )
    except Exception:
        logger.exception("_handle_ask_user_question: failed to persist card for agent %s", agent_id[:8])
    finally:
        _db_card.close()

    from websocket import ws_manager
    await ws_manager.broadcast("permission_request", {
        "request_id": req.id,
        "agent_id": agent_id,
        "agent_name": _agent_name,
        "project": _agent_project,
        "tool_name": "AskUserQuestion",
        "tool_input": tool_input,
        "summary": q_summary,
    })

    # Block until user answers (reuse permission timeout)
    _perm_timeout = int(os.getenv("XY_PERMISSION_TIMEOUT") or os.getenv("AHIVE_PERMISSION_TIMEOUT") or "7200")
    try:
        decision, reason, updated_input = await asyncio.wait_for(
            pm.wait_for_decision(req.id), timeout=_perm_timeout,
        )
    except asyncio.TimeoutError:
        pm.respond(req.id, "deny", "Question timed out")
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "AskUserQuestion timed out",
        }}

    if decision == "allow" and updated_input:
        logger.info("AskUserQuestion answered for agent %s: %s", agent_id[:8], list(updated_input.get("answers", {}).keys()))
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason or "Answered from Xylocopa web UI",
            "updatedInput": updated_input,
        }}
    else:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason or "Dismissed by user",
        }}


@router.post("/api/hooks/agent-permission")
async def hook_agent_permission(request: Request):
    """PreToolUse hook for non-skip-permissions agents.

    Blocks until the user approves or denies the tool call from the web UI.
    Auto-allows safe read-only tools (Read, Glob, Grep, etc.) and any tool
    the user has previously marked "always allow" for this agent session.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    agent_id = _resolve_agent_for_hook(request, body)
    if not agent_id:
        logger.warning("hook_agent_permission: no agent match (pane=%s sid=%s)",
                       request.headers.get("X-Tmux-Pane", ""),
                       (body.get("session_id") if isinstance(body, dict) else "") or "")
        return {}

    # Guard: ignore hooks from subprocess sessions
    hook_sid = body.get("session_id", "") if isinstance(body, dict) else ""
    if _is_subprocess_session(agent_id, hook_sid, request):
        return {}

    if body.get("hook_event_name") != "PreToolUse":
        logger.warning("hook_agent_permission: unexpected event %s for agent %s", body.get("hook_event_name"), agent_id[:8])
        return {}

    tool_name = body.get("tool_name", "")
    tool_input = body.get("tool_input") or {}

    # AskUserQuestion: block and return updatedInput for ALL agents (both
    # skip_permissions and supervised).  Must intercept BEFORE skip_permissions
    # check, since skip_permissions agents would otherwise pass through.
    if tool_name == "AskUserQuestion":
        tool_use_id = body.get("tool_use_id", "")
        return await _handle_ask_user_question(request, agent_id, tool_input, tool_use_id)

    from permissions import PermissionManager, SAFE_TOOLS

    # Auto-allow safe read-only tools BEFORE any DB access.
    # This avoids SQLite contention when the parallel tool_activity
    # hook is writing at the same time (both fire for each PreToolUse).
    if tool_name in SAFE_TOOLS:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}

    pm: PermissionManager | None = getattr(request.app.state, "permission_manager", None)
    if not pm:
        logger.warning("hook_agent_permission: no permission_manager on app.state for agent %s", agent_id[:8])
        return {}

    # Check if the agent actually needs permission gating
    from database import SessionLocal
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        if not agent or agent.skip_permissions:
            return {}
        agent_name = agent.name or ""
        agent_project = agent.project or ""
    except Exception:
        logger.exception("hook_agent_permission: DB error for agent %s", agent_id[:8])
        return {}
    finally:
        db.close()

    # Check session "always allow" rules
    if pm.check_always_allow(agent_id, tool_name):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}

    # Create pending request and broadcast to frontend
    from websocket import _tool_input_summary
    summary = _tool_input_summary(tool_name, tool_input) if tool_input else ""
    req = pm.create_request(agent_id, tool_name, tool_input, summary)

    # Drain pending JSONL so card appears after preceding messages
    _ad_drain = getattr(request.app.state, "agent_dispatcher", None)
    if _ad_drain:
        try:
            await _ad_drain._drain_session_sync(agent_id)
        except Exception:
            pass

    # Persist as interactive card in DB so it survives page refresh
    _perm_tool_use_id = f"hookperm-{req.id}"
    _perm_meta = {
        "interactive": [{
            "type": "permission_prompt",
            "tool_use_id": _perm_tool_use_id,
            "request_id": req.id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "summary": summary,
            "questions": [{
                "header": "Permission",
                "question": summary or f"{tool_name} requires permission",
                "options": [
                    {"label": "Allow", "description": "Allow this tool call once", "color": "emerald"},
                    {"label": "Always allow", "description": "Don't ask again for this tool", "color": "amber"},
                    {"label": "Deny", "description": "Block this tool call", "color": "red"},
                ],
            }],
            "answer": None,
        }],
    }
    _db_perm = SessionLocal()
    try:
        _perm_msg = Message(
            agent_id=agent_id,
            role=MessageRole.AGENT,
            kind=None,
            content="",
            source="hook",
            status=MessageStatus.COMPLETED,
            meta_json=json.dumps(_perm_meta),
            tool_use_id=_perm_tool_use_id,
            jsonl_uuid=_perm_tool_use_id,
        )
        _db_perm.add(_perm_msg)
        _db_perm.commit()
        from display_writer import flush_agent as _flush_perm
        _flush_perm(agent_id)
        # Bump unread + push notification for permission card
        _ad = getattr(request.app.state, "agent_dispatcher", None)
        if _ad:
            _ad._bump_unread_and_notify_interactive(
                agent_id,
                f"[interactive cards] {summary or f'{tool_name} requires permission'}",
            )
    except Exception:
        logger.exception("hook_agent_permission: failed to persist permission card for agent %s", agent_id[:8])
    finally:
        _db_perm.close()

    from websocket import ws_manager
    await ws_manager.broadcast("permission_request", {
        "request_id": req.id,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "project": agent_project,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "summary": summary,
    })

    # Block until user responds, with configurable timeout (default 2h)
    _perm_timeout = int(os.getenv("XY_PERMISSION_TIMEOUT") or os.getenv("AHIVE_PERMISSION_TIMEOUT") or "7200")
    try:
        decision, reason, _updated_input = await asyncio.wait_for(
            pm.wait_for_decision(req.id), timeout=_perm_timeout,
        )
    except asyncio.TimeoutError:
        pm.respond(req.id, "deny", "Permission timed out")
        # Patch DB card as timed out
        _db_to = SessionLocal()
        try:
            _m = _db_to.query(Message).filter(
                Message.agent_id == agent_id,
                Message.tool_use_id == f"hookperm-{req.id}",
            ).first()
            if _m:
                _meta = json.loads(_m.meta_json or "{}")
                for _item in _meta.get("interactive", []):
                    if _item.get("request_id") == req.id:
                        _item["answer"] = "Timed out"
                        _item["selected_index"] = 2
                        break
                _m.meta_json = json.dumps(_meta)
                _db_to.commit()
                # Post-delivery metadata patch → update_last (via helper).
                # Pre-sent interactive cards go through pre_sent_update
                # directly; this path is reserved for delivered AGENT cards.
                from display_writer import (
                    flush_agent as _flush_to,
                    update_after_metadata_change as _update_to,
                )
                _flush_to(agent_id)
                _update_to(agent_id, _m.id)
        except Exception:
            logger.exception("hook_agent_permission: failed to patch timeout for agent %s", agent_id[:8])
        finally:
            _db_to.close()
        from notify import notify
        notify("permission", agent_id, "Permission timed out",
               f"{agent_name}: {tool_name} auto-denied after timeout",
               url=f"/agents/{agent_id}")
        return {"hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "Permission request timed out",
        }}

    # Wake sync after permission resolves — agent will proceed with tool use
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad:
        ad.wake_sync(agent_id)

    if decision == "allow":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    else:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason or "Denied by user",
        }}


@router.post("/api/agents/{agent_id}/permission/{request_id}/respond")
async def respond_permission(
    agent_id: str, request_id: str,
    request: Request, db: Session = Depends(get_db),
):
    """User responds to a pending tool permission request."""
    body = await request.json()
    decision = body.get("decision")  # "allow" | "deny" | "allow_always"
    reason = body.get("reason", "")
    updated_input = body.get("updated_input")  # AskUserQuestion answers

    from permissions import PermissionManager
    pm: PermissionManager | None = getattr(request.app.state, "permission_manager", None)
    if not pm:
        raise HTTPException(status_code=500, detail="Permission manager not available")

    actual_decision = "allow" if decision in ("allow", "allow_always") else "deny"

    if decision == "allow_always":
        tool_name = body.get("tool_name", "")
        if tool_name:
            pm.add_always_allow(agent_id, tool_name)

    if not pm.respond(request_id, actual_decision, reason, updated_input=updated_input):
        raise HTTPException(status_code=404, detail="Permission request not found or already resolved")

    # Patch the persisted interactive card with the decision
    _perm_msg = db.query(Message).filter(
        Message.agent_id == agent_id,
        Message.tool_use_id == f"hookperm-{request_id}",
    ).first()
    if _perm_msg:
        try:
            _meta = json.loads(_perm_msg.meta_json or "{}")
            for _item in _meta.get("interactive", []):
                if _item.get("request_id") == request_id:
                    if decision == "allow_always":
                        _item["selected_index"] = 1
                        _item["answer"] = "Always allow"
                    elif actual_decision == "allow":
                        _item["selected_index"] = 0
                        _item["answer"] = "Allow"
                    else:
                        _item["selected_index"] = 2
                        _item["answer"] = "Deny"
                    break
            _perm_msg.meta_json = json.dumps(_meta)
            db.commit()
            from display_writer import update_after_metadata_change as _resp_update
            _resp_update(agent_id, _perm_msg.id)
        except Exception:
            logger.exception("respond_permission: failed to patch card for request %s", request_id)

    # Broadcast resolution so all frontend clients update
    from websocket import ws_manager
    await ws_manager.broadcast("permission_resolved", {
        "request_id": request_id,
        "agent_id": agent_id,
        "decision": actual_decision,
    })

    return {"detail": "ok"}


@router.post("/api/hooks/agent-permission-request")
async def hook_agent_permission_request(request: Request):
    """PermissionRequest hook — auto-allow native CC permission prompts.

    For supervised agents, the user already approved the tool via our PreToolUse
    permission hook.  When CC's own ask rules still trigger a native permission
    prompt, this hook auto-allows it to avoid a "double prompt".

    For skip_permissions agents, this hook never fires (they use
    --dangerously-skip-permissions which bypasses all CC permission checks).
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    agent_id = _resolve_agent_for_hook(request, body)
    if not agent_id:
        logger.warning("hook_agent_permission_request: no agent match (pane=%s sid=%s)",
                       request.headers.get("X-Tmux-Pane", ""),
                       (body.get("session_id") if isinstance(body, dict) else "") or "")
        return {}

    # Guard: ignore hooks from subprocess sessions
    hook_sid = body.get("session_id", "") if isinstance(body, dict) else ""
    if _is_subprocess_session(agent_id, hook_sid, request):
        return {}

    tool_name = body.get("tool_name", "")
    logger.info(
        "PermissionRequest auto-allow for agent %s: %s",
        agent_id[:8], tool_name,
    )

    return {"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow"},
    }}


@router.get("/api/agents/{agent_id}/permissions/pending")
async def get_pending_permissions(agent_id: str, request: Request):
    """Get all pending permission requests for an agent."""
    from permissions import PermissionManager
    pm: PermissionManager | None = getattr(request.app.state, "permission_manager", None)
    if not pm:
        return []
    return pm.get_pending(agent_id)


def _internal_worker_lineage(session_id: str) -> str | None:
    """Classify a pane-less session as Claude-internal rather than user-run.

    Claude Code's daemon architecture (``bg-pty-host`` workers,
    ``--fork-session`` compact/summary workers, Agent-tool subagents) runs
    real claude processes with no tmux pane and no agent header, so their
    SessionStart hooks look exactly like a user CLI started outside tmux —
    and used to surface as adoptable "not in tmux" entries. Adopting one
    would kill a live internal worker and resume its session in a fresh
    tmux session (phantom sessions "out of nowhere").

    Walk /proc for the live process carrying this session id, then inspect
    it and its ancestors: XY_AGENT_ID in the environment means the tree
    descends from a managed agent; a ``claude daemon`` / ``bg-pty-host`` /
    ``--bg-spare`` ancestor means a daemon worker. Neither is adoptable.

    Returns ``{"reason": str, "agent_id": str | None,
    "resumed_session": str | None}`` — *agent_id* is the XY_AGENT_ID found
    in the lineage, *resumed_session* the session id this process resumes
    (``--fork-session --resume <jsonl>``: daemon-side auto-compact forks the
    agent's own conversation onto a new id). Returns None when nothing
    internal is found (any /proc read race fails open to the old behavior).
    """
    sid = session_id.strip()
    if not sid:
        return None
    try:
        pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None
    start = None
    start_argv: list[bytes] = []
    sid_b = sid.encode()
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        # Exact argv-token match only: a claude binary invoked with this
        # session id as its own argument. Substring matching would catch
        # unrelated processes that merely mention the id (e.g. the hook
        # sender's own shell), whose environment then misclassifies.
        if argv and b"claude" in argv[0] and sid_b in argv:
            start = pid
            start_argv = argv
            break
    if start is None:
        return None

    resumed = None
    if b"--fork-session" in start_argv and b"--resume" in start_argv:
        try:
            target = start_argv[start_argv.index(b"--resume") + 1].decode()
            base = os.path.basename(target)
            resumed = base[:-6] if base.endswith(".jsonl") else base
        except (IndexError, UnicodeDecodeError):
            pass

    def _result(reason: str, agent_id: str | None) -> dict:
        return {"reason": reason, "agent_id": agent_id, "resumed_session": resumed}

    pid = start
    for _ in range(20):  # bounded ancestor walk
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                for entry in f.read().split(b"\0"):
                    if entry.startswith(b"XY_AGENT_ID="):
                        agent = entry[len(b"XY_AGENT_ID="):].decode("utf-8", "ignore").strip()
                        return _result(f"managed-agent lineage via pid {start}", agent or None)
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ")
        except OSError:
            cmd = b""
        if b"bg-pty-host" in cmd or b"daemon run" in cmd or b"--bg-spare" in cmd:
            return _result(f"claude-daemon worker via pid {pid}", None)
        try:
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(") ", 1)[-1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        pid = ppid
    return None


@router.post("/api/hooks/agent-session-start")
async def hook_agent_session_start(request: Request):
    """Receive SessionStart hook from Claude Code agents.

    Managed agents (pane resolves to a known agent in DB): writes a
    signal file for _detect_successor() to track session rotation.

    Unmanaged sessions (pane not in DB): creates an unlinked session entry
    so the user can confirm (adopt) it in the UI.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        logger.debug("SessionStart hook: failed to parse body")
        return {}
    agent_id = _resolve_agent_for_hook(request, body)

    # Claude Code sends session info — extract session_id
    session_id = ""
    if isinstance(body, dict):
        session_id = body.get("session_id", "") or ""
        if not session_id:
            session = body.get("session") or {}
            if isinstance(session, dict):
                session_id = session.get("session_id", "") or session.get("id", "") or ""

    if not session_id:
        logger.warning("SessionStart hook: no session_id in body (agent=%s)", agent_id[:8] if agent_id else "(none)")
        return {}

    source = ""
    if isinstance(body, dict):
        source = body.get("source", "") or ""

    logger.info("SessionStart hook: agent=%s session=%s source=%r",
                agent_id[:8] if agent_id else "(none)", session_id[:12], source)

    if agent_id:
        # Emitting "end" prematurely here caused a false "compact done" in
        # the UI while the sync engine hadn't processed the new state yet.
        if source == "compact":
            # Compact creates a new session — rotate immediately so the
            # sync loop starts tailing the new JSONL (imports the
            # "continued from" system message) without waiting for idle
            # poll detection (~60s).
            ad = getattr(request.app.state, "agent_dispatcher", None)
            if ad:
                ctx = ad._sync_contexts.get(agent_id)
                if ctx:
                    ctx.compact_notified = False
                    ctx.compact_notified_at = 0.0

                # Look up project_path + worktree for rotation
                _proj_path = None
                _worktree = None
                _db_ss = SessionLocal()
                try:
                    _ag_ss = _db_ss.get(Agent, agent_id)
                    if _ag_ss:
                        _worktree = _ag_ss.worktree
                        _proj_ss = _db_ss.get(Project, _ag_ss.project) if _ag_ss.project else None
                        _proj_path = _proj_ss.path if _proj_ss else None
                finally:
                    _db_ss.close()

                if _proj_path:
                    # Tmux agent: rotate session in-place and start fresh
                    # sync loop with the new JSONL.
                    rotated = ad._rotate_agent_session(
                        agent_id, session_id, _proj_path,
                        worktree=_worktree,
                    )
                    if rotated:
                        # Wake the new sync loop so it imports immediately
                        ad.wake_sync(agent_id)
                        logger.info(
                            "SessionStart hook: agent=%s compact rotation to %s",
                            agent_id[:8], session_id[:12],
                        )
                    else:
                        logger.warning(
                            "SessionStart hook: compact rotation failed for %s",
                            agent_id[:8],
                        )
                else:
                    # Fallback: write signal file for poll-based detection
                    from route_helpers import session_signal_path
                    signal_path = session_signal_path(agent_id)
                    try:
                        with open(signal_path, "w") as f:
                            f.write(session_id)
                    except OSError as e:
                        logger.warning("SessionStart hook: failed to write rotation signal %s: %s", signal_path, e)
                    logger.info(
                        "SessionStart hook: agent=%s compact fallback signal for %s",
                        agent_id[:8], session_id[:12],
                    )

            return {}

        # Confirm /clear command execution — no Stop hook follows,
        # so mark both delivered and completed here.
        if source == "clear":
            import slash_commands as _sc
            _sc.mark_delivered_and_completed(agent_id, "/clear")
            # Write a SYSTEM "Context cleared" bubble so the chat shows a
            # visible boundary between the old and new session. Hook-owned:
            # synthetic uuid keeps sync's UUID-dedup from collision;
            # source="hook" exempts it from cli-orphan purge.
            from uuid import uuid4 as _uuid4
            _db_clear = SessionLocal()
            try:
                _clear_msg = Message(
                    agent_id=agent_id,
                    role=MessageRole.SYSTEM,
                    kind="clear",
                    content="Context cleared — new session",
                    source="hook",
                    status=MessageStatus.COMPLETED,
                    jsonl_uuid=f"clear-{_uuid4().hex[:12]}",
                    created_at=_utcnow(),
                    completed_at=_utcnow(),
                    delivered_at=_utcnow(),
                )
                _db_clear.add(_clear_msg)
                _db_clear.commit()
                from display_writer import flush_agent as _flush_clear
                _flush_clear(agent_id)
            except Exception:
                logger.exception("SessionStart(clear): failed to write sys bubble for %s", agent_id[:8])
            finally:
                _db_clear.close()
            # Flip agent.status to IDLE. /clear has no Stop hook and the
            # post-clear JSONL is empty (no signals for sync_engine to
            # infer from), so this hook is the authoritative path. Without
            # it, agent stays EXECUTING from the USP hook that fired when
            # the user sent /clear, until the next user message.
            _flipped_to_idle = False
            _db_status_clear = SessionLocal()
            try:
                _ag_clear = _db_status_clear.get(Agent, agent_id)
                if (_ag_clear and _ag_clear.status not in
                        (AgentStatus.STOPPED, AgentStatus.ERROR)):
                    if _ag_clear.status != AgentStatus.IDLE:
                        _ag_clear.status = AgentStatus.IDLE
                        _ag_clear.generating_msg_id = None
                        _db_status_clear.commit()
                        _flipped_to_idle = True
                        from websocket import emit_agent_update as _eau_clear
                        asyncio.ensure_future(_eau_clear(
                            agent_id, "IDLE", _ag_clear.project,
                        ))
                        logger.info(
                            "SessionStart(clear): agent %s → IDLE",
                            agent_id[:8],
                        )
            except Exception:
                _db_status_clear.rollback()
                logger.exception(
                    "SessionStart(clear): failed to flip status for %s",
                    agent_id[:8],
                )
            finally:
                _db_status_clear.close()
            # Drain any pre-sent queued messages typed in the brief window
            # between /clear's USP (status=EXECUTING) and now (status=IDLE).
            # /clear has no Stop hook, so without an explicit kick these
            # messages would sit in the queue with no natural trigger.
            if _flipped_to_idle:
                ad_dispatch = getattr(request.app.state, "agent_dispatcher", None)
                if ad_dispatch:
                    asyncio.ensure_future(
                        ad_dispatch.dispatch_pending_message(agent_id, delay=0)
                    )
            # Sleep + drain (mirrors PostCompact pattern). The new session
            # is brand-new and typically empty, so drain is mostly defensive
            # — but it gives the rotation-signal pipeline time to install
            # the new sync ctx and ensures emit_context_usage below sees
            # the post-clear breakdown, not the stale pre-clear one.
            #
            # No `wait_for` marker: defensive drain on a typically-empty
            # post-/clear session — "any growth" is correct; we don't
            # have a specific entry we're waiting for.
            ad_clear = getattr(request.app.state, "agent_dispatcher", None)
            if ad_clear:
                await _await_jsonl_flush(ad_clear, agent_id)
                await ad_clear._drain_session_sync(agent_id)
            # /clear resets the in-session running counter to 0; push a fresh
            # breakdown so the pill shrinks immediately.
            from websocket import emit_context_usage as _emit_ctx
            await _emit_ctx(agent_id)

        # Guard: ignore SessionStart from subprocesses (Agent tool inherits
        # XY_AGENT_ID).  Accept if awaiting_rotation (set by SessionEnd)
        # or if this is a /clear rotation.
        if source != "clear":
            ad_check = getattr(request.app.state, "agent_dispatcher", None)
            if ad_check:
                ctx = ad_check._sync_contexts.get(agent_id)
                if ctx and ctx.session_id and ctx.session_id != session_id:
                    if not ctx.awaiting_rotation:
                        logger.info(
                            "SessionStart hook: agent=%s has active session %s, "
                            "ignoring subprocess session %s",
                            agent_id[:8], ctx.session_id[:12], session_id[:12],
                        )
                        return {}
                    else:
                        ctx.awaiting_rotation = False

        # Managed agent — session rotation signal
        from route_helpers import session_signal_path
        signal_path = session_signal_path(agent_id)
        try:
            with open(signal_path, "w") as f:
                f.write(session_id)
            logger.info("SessionStart hook: agent=%s session=%s (source=%s)",
                        agent_id[:8], session_id[:12], source or "unknown")
        except OSError as e:
            logger.warning("SessionStart hook: failed to write signal %s: %s", signal_path, e)

        # Wake sync loop — new session means new JSONL content
        ad = getattr(request.app.state, "agent_dispatcher", None)
        if ad:
            # If a launch is in flight for this agent, hand the session_id
            # off directly so the launch task can skip JSONL polling.
            fut = ad._launch_session_futures.get(agent_id)
            if fut and not fut.done():
                fut.set_result(session_id)

            # Wait for the launch task to install the sync ctx (~10-20ms
            # typical, handled by _await_jsonl_flush's ctx poll), then wait
            # for CC to flush the user's first turn — same event-driven
            # pattern as the other hooks. Wakes the freshly registered
            # sync loop so it imports without waiting for the next hook.
            #
            # No `wait_for` marker: generic wait-for-any-write at session
            # start.  The first turn entry (whatever it is — could be user,
            # could be system continuation marker for resumed sessions)
            # is what causes growth, so "any" is the right semantics here.
            async def _delayed_wake(_aid: str):
                await _await_jsonl_flush(ad, _aid)
                ad.wake_sync(_aid)
            asyncio.ensure_future(_delayed_wake(agent_id))

        return {}

    # --- Unmanaged session: create unlinked entry for user confirmation ---
    cwd = request.headers.get("X-Session-Cwd", "").strip()
    tmux_pane = request.headers.get("X-Tmux-Pane", "").strip()

    if not cwd:
        logger.info("SessionStart hook: unmanaged session %s missing cwd — skipping",
                     session_id[:12])
        return {}

    # Match CWD to a project + check session not already owned BEFORE the
    # pane check, so we can emit a visible "rejected" marker when the user
    # is in a project dir but ran claude outside tmux (instead of silently
    # dropping). Adopt itself still requires a pane.
    from database import SessionLocal as _SL
    _db = _SL()
    try:
        cwd_real = os.path.realpath(cwd)
        from routers.projects import active_projects
        projects = active_projects(_db)
        matched_proj = None
        for p in projects:
            proj_real = os.path.realpath(p.path)
            if cwd_real == proj_real or cwd_real.startswith(proj_real + "/"):
                matched_proj = p
                break
        if not matched_proj:
            logger.info("SessionStart hook: session %s cwd %s doesn't match any project",
                        session_id[:12], cwd)
            return {}

        existing = _db.query(Agent).filter(Agent.session_id == session_id).first()
        if existing:
            logger.info("SessionStart hook: session %s already owned by agent %s",
                        session_id[:12], existing.id[:8])
            return {}

        # cwd matches a project but claude isn't in tmux — record a
        # rejected entry so the UI can tell the user we saw it but can't
        # adopt it (the design requires claude to be in a tmux pane on
        # this orchestrator's tmux server).
        if not tmux_pane:
            # Claude-internal workers (daemon bg-pty hosts, fork-session
            # compact workers, subagents) are pane-less by construction —
            # never offer them for adoption.
            internal = _internal_worker_lineage(session_id)
            if internal is None and source == "fork":
                internal = {"reason": "fork-session", "agent_id": None,
                            "resumed_session": None}
            if internal:
                # Daemon-side auto-compact forks a managed agent's OWN
                # conversation onto a new session id (--fork-session
                # --resume <current jsonl>) with no agent header. That fork
                # IS the agent's continuation — rotate the agent onto it so
                # chat sync follows the live JSONL instead of tailing the
                # frozen pre-compact file forever.
                rot_agent_id = internal.get("agent_id")
                resumed = internal.get("resumed_session")
                if rot_agent_id and resumed and session_id != resumed:
                    rot_agent = _db.get(Agent, rot_agent_id)
                    if rot_agent and rot_agent.session_id == resumed:
                        ad_rot = getattr(request.app.state, "agent_dispatcher", None)
                        if ad_rot:
                            rotated = ad_rot._rotate_agent_session(
                                rot_agent_id, session_id, matched_proj.path,
                                worktree=rot_agent.worktree,
                            )
                            if rotated:
                                ad_rot.wake_sync(rot_agent_id)
                                logger.info(
                                    "SessionStart hook: fork of agent %s current session %s — rotated to %s (%s)",
                                    rot_agent_id[:8], resumed[:12],
                                    session_id[:12], internal.get("reason"),
                                )
                                return {}
                            logger.warning(
                                "SessionStart hook: fork rotation failed for agent %s → %s",
                                rot_agent_id[:8], session_id[:12],
                            )
                logger.info(
                    "SessionStart hook: session %s in project %s is Claude-internal (%s) — not adoptable, skipping rejected entry",
                    session_id[:12], matched_proj.name, internal.get("reason"),
                )
                return {}
            from agent_dispatcher import _write_rejected_unlinked_entry
            _write_rejected_unlinked_entry(
                session_id=session_id,
                cwd=cwd_real,
                project_name=matched_proj.name,
                reason="missing_tmux_pane",
            )
            logger.info(
                "SessionStart hook: session %s in project %s not in tmux — rejected entry recorded",
                session_id[:12], matched_proj.name,
            )
            return {}

        # If pane already owned by active agent → rotation signal
        pane_owner = _db.query(Agent).filter(
            Agent.tmux_pane == tmux_pane,
            Agent.status.notin_([AgentStatus.STOPPED, AgentStatus.ERROR]),
        ).first()
        if pane_owner:
            from route_helpers import session_signal_path
            signal_path = session_signal_path(pane_owner.id)
            try:
                with open(signal_path, "w") as f:
                    f.write(session_id)
                logger.info("SessionStart hook: pane %s owned by %s — rotation signal for %s",
                            tmux_pane, pane_owner.id[:8], session_id[:12])
            except OSError as e:
                logger.warning("SessionStart hook: rotation signal failed: %s", e)
            return {}
    finally:
        _db.close()

    # Resolve tmux session name
    tmux_session_name = None
    try:
        tmux_session_name = subprocess.check_output(
            ["tmux", "display-message", "-t", tmux_pane, "-p", "#{session_name}"],
            timeout=2, text=True,
        ).strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    # Create unlinked entry with session_id — adopt uses it directly
    from agent_dispatcher import _write_unlinked_entry
    _write_unlinked_entry(
        session_id=session_id,
        cwd=cwd_real,
        tmux_pane=tmux_pane or None,
        tmux_session=tmux_session_name,
        project_name=matched_proj.name,
    )
    logger.info("SessionStart hook: unmanaged session %s → unlinked entry (project=%s, pane=%s)",
                session_id[:12], matched_proj.name, tmux_pane)
    return {}


@router.post("/api/hooks/emit-message")
async def hook_emit_message(request: Request, db: Session = Depends(get_db)):
    """Re-emit the new_message WS signal for an MCP-inserted message.

    The MCP server runs as a stdio subprocess outside this process, so it
    writes Message rows directly to the DB and then pings this endpoint for
    an instant UI update (the 5s poll covers the failure case). Restricted
    to kind="webapp" rows, so the unauthenticated hook surface can at worst
    flush a display file and trigger a redundant refetch.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    message_id = (body.get("message_id") or "").strip() if isinstance(body, dict) else ""
    if not message_id:
        return {}
    msg = db.get(Message, message_id)
    if msg is None or msg.kind != "webapp":
        return {}
    agent = db.get(Agent, msg.agent_id)
    # Flush to the display file before emitting — DB rows are invisible to
    # the frontend until display_writer writes them, and idle/stopped agents
    # have no sync loop running to do it for them.
    from display_writer import flush_agent
    try:
        flush_agent(msg.agent_id)
    except Exception:  # noqa: BLE001 — the WS signal is still worth sending
        logger.exception("emit-message: flush_agent failed for %s", msg.agent_id[:8])
    from websocket import emit_new_message
    await emit_new_message(msg.agent_id, msg.id,
                           agent.name if agent else "",
                           agent.project if agent else "")
    # webapp_present (MCP subprocess) just wrote a WebApp row + card: index
    # it here, in the process that owns the WS clients.
    try:
        import asset_index
        asset_index.schedule_catch_up()
    except Exception:  # noqa: BLE001
        logger.debug("asset_index schedule failed", exc_info=True)
    return {"ok": True}
