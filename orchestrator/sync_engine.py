"""Sync engine — pointer-based JSONL-to-DB synchronization.

Design principles:
1. Pointer (last_turn_count) tracks sync position
2. Hooks wake syncing (never create messages)
3. sync_import_new_turns is the SOLE message creation path
4. sync_full_scan is read-only audit (never creates/updates messages from JSONL)
5. Compact/clear/new → sync_full_scan resets pointer

All functions take (ad, ctx) where ad is AgentDispatcher, ctx is SyncContext.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_ as _or
from sqlalchemy.exc import DatabaseError, IntegrityError

from database import SessionLocal
from models import (
    Agent,
    AgentStatus,
    Message,
    MessageRole,
    MessageStatus,
)
from utils import utcnow as _utcnow, is_interrupt_message


def _parse_jsonl_ts(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp from JSONL into a datetime, or None."""
    if not ts:
        return None
    try:
        # Handle "2026-03-24T17:02:44.544Z" format
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.debug("Failed to parse JSONL timestamp: %s", ts)
        return None

logger = logging.getLogger("orchestrator.sync_engine")

MAX_AUDIT_FILE_SIZE = 50 * 1024 * 1024  # 50MB — protect sync_full_scan


# ---------------------------------------------------------------------------
# SyncContext dataclass — holds all per-agent sync state
# ---------------------------------------------------------------------------

@dataclass
class SyncContext:
    agent_id: str
    session_id: str
    project_path: str
    worktree: str | None = None
    agent_name: str = ""
    agent_project: str = ""
    jsonl_path: str = ""

    # Sync pointer — the only 3 state fields that matter
    last_offset: int = 0           # file byte size — change detection only
    last_turn_count: int = 0       # THE pointer: number of turns processed
    last_content_hash: str = ""    # hash of last turn content — streaming detection

    # Agent operational state
    compact_notified: bool = False
    compact_notified_at: float = 0.0  # monotonic timestamp for abandoned-compact safety net
    compact_end_emitted: bool = False
    compact_detected_at: float = 0.0
    idle_polls: int = 0
    getsize_error_count: int = 0
    awaiting_rotation: bool = False     # set by SessionEnd, consumed by SessionStart


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_tool_use_id(meta: dict | None) -> str | None:
    """Extract primary tool_use_id from parsed interactive metadata."""
    if not meta or not isinstance(meta, dict):
        return None
    items = meta.get("interactive", [])
    if items and isinstance(items, list):
        return items[0].get("tool_use_id")
    return None


def _content_hash(content: str) -> str:
    """Fast hash of content for change detection."""
    import hashlib
    return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _end_compact_activity(db, agent_id: str, session_id: str):
    """Mark the most recent unfinished Compact tool_activity Message as ended.

    Returns the message id (str) if found, else None — caller should
    update_last() after commit to push the status change to the display file.
    """
    existing = (
        db.query(Message)
        .filter(
            Message.agent_id == agent_id,
            Message.kind == "tool_activity",
            Message.status == MessageStatus.EXECUTING,
            Message.meta_json.contains('"tool_kind":"compact"'),
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    if existing:
        import json as _json
        existing.completed_at = _utcnow()
        existing.status = MessageStatus.COMPLETED
        _meta = _json.loads(existing.meta_json or "{}")
        _meta["phase"] = "end"
        _meta["output_summary"] = "context compacted"
        existing.meta_json = _json.dumps(_meta)
        return existing.id
    return None


def _notify_interactive(ad, agent, new_turns):
    """Bump unread + send push for unanswered interactive items."""
    _plan_content: str | None = None
    _question_content: str | None = None
    for _r, _c, *_rest in new_turns:
        if _r == "assistant" and _rest:
            _meta = _rest[0] if _rest else None
            if isinstance(_meta, dict):
                for _item in _meta.get("interactive", []):
                    if _item.get("answer") is not None:
                        continue
                    _t = _item.get("type", "")
                    if _t == "exit_plan_mode" and _plan_content is None:
                        _plan_content = (_item.get("plan") or "").strip()
                    elif _t == "ask_user_question" and _question_content is None:
                        _qs = _item.get("questions") or []
                        if _qs:
                            _question_content = _qs[0].get("question", "").strip()

    # Plan takes precedence (preserves original elif ordering).
    if _plan_content is not None:
        body = f"[interactive cards] {_plan_content}" if _plan_content else "[interactive cards] plan ready"
        ad._bump_unread_and_notify_interactive(agent.id, body)
    elif _question_content is not None:
        body = f"[interactive cards] {_question_content}" if _question_content else "[interactive cards] question"
        ad._bump_unread_and_notify_interactive(agent.id, body)


def _is_unimported_turn(role: str, kind: str | None) -> bool:
    """True for JSONL turns the import loop intentionally never writes to
    the DB (currently: slash_signal user turns — CC's /command markers,
    skipped since 93ca3330 to avoid duplicate "/compact" bubbles).

    Every drift audit (sync_full_scan's missing_in_db, the realtime drift
    snapshot in sync_import_new_turns) MUST exclude these turns via this
    same predicate. An asymmetry leaves them permanently "missing in DB",
    so every full scan treats them as real drift and rewinds the pointer —
    replaying history through the status-signal accumulator (the
    auto-compact → spurious IDLE incident, 2026-06-03).
    """
    return role == "user" and kind == "slash_signal"


def _infer_status_from_signals(
    db,
    ctx: SyncContext,
    *,
    saw_user_turn: bool,
    saw_assistant_turn: bool,
    saw_stop_hook: bool,
    saw_rate_limit: bool,
    saw_interrupt: bool,
) -> str | None:
    """Derive the agent.status transition implied by JSONL signals seen this
    sync cycle. The single writer of EXECUTING/IDLE based on JSONL truth.

    Returns the new status value as a string ("EXECUTING" | "IDLE") if a
    DB write happened (caller should emit), else None (no change).

    Caller MUST already have committed message inserts so this runs
    against a fresh agent row.

    Rules:
      saw_stop_hook | saw_rate_limit | saw_interrupt → IDLE (only if not
        already IDLE; clears generating_msg_id)
      saw_user_turn | saw_assistant_turn (and no stop signal) → EXECUTING
        (only if status is IDLE/STARTING — never overrides STOPPED/ERROR)

    EXECUTING vs IDLE in the same cycle: stop signals always come at the
    end of a Claude turn, so any cycle that contains a stop signal ends
    in IDLE regardless of preceding user/assistant turns.

    saw_assistant_turn covers the recovery case where user_prompt hook
    was missed (network/config/restart) but Claude is clearly working
    because new assistant turns are streaming into JSONL — this lets
    sync flip IDLE→EXECUTING from JSONL truth alone, without depending
    on the hook chain.
    """
    agent = db.get(Agent, ctx.agent_id)
    if not agent or agent.status in (AgentStatus.STOPPED, AgentStatus.ERROR):
        return None

    if saw_stop_hook or saw_rate_limit or saw_interrupt:
        if agent.status != AgentStatus.IDLE or agent.generating_msg_id is not None:
            agent.status = AgentStatus.IDLE
            agent.generating_msg_id = None
            db.commit()
            return "IDLE"
        return None

    if (saw_user_turn or saw_assistant_turn) and agent.status in (AgentStatus.IDLE, AgentStatus.STARTING):
        agent.status = AgentStatus.EXECUTING
        db.commit()
        return "EXECUTING"

    return None


# ---------------------------------------------------------------------------
# User message promotion — single path (Phase 3a)
# ---------------------------------------------------------------------------

def _promote_or_create_user_msg(db, ctx: SyncContext, content, jsonl_uuid, seq, meta, kind, jsonl_ts=None,
                                 deferred_updates: list | None = None):
    """Match a JSONL user turn to a sent-state DB row, or create a CLI message.

    Strategy (pre-sent refactor):
    1. UUID dedup — skip if already imported.
    2. Content-match against sent-state rows — messages that were promoted
       from the pre-sent file on tmux send (status=QUEUED, jsonl_uuid
       NULL, delivered_at NULL) but not yet confirmed by UserPromptSubmit.
    3. No match → genuine CLI-typed user input, create a fresh row.

    Returns Message to insert, or None if already handled (dedup or
    sent->delivered update).

    When a sent row is matched, the id is appended to ``deferred_updates``
    (if provided) so the caller calls `update_last` AFTER db.commit() —
    writing the `_replace` line with status='delivered'.
    """
    from content_matcher import ContentMatcher

    # 1. UUID dedup (fastest — covers restarts/re-reads)
    if jsonl_uuid:
        existing = db.query(Message).filter(
            Message.agent_id == ctx.agent_id,
            Message.jsonl_uuid == jsonl_uuid,
        ).first()
        if existing:
            if existing.session_seq != seq:
                existing.session_seq = seq
            logger.debug("Agent %s: dedup skip uuid=%s", ctx.agent_id[:8], jsonl_uuid)
            return None

    # 2. Fetch unpromoted candidates.
    # Two shapes end up here:
    #   (a) pre-sent → sent rows: status=QUEUED, delivered_at=NULL,
    #       jsonl_uuid=NULL (web/plan_continue path).
    #   (b) task-launched rows from _dispatch_task_tmux: status=COMPLETED
    #       synchronously, but still delivered_at=NULL, jsonl_uuid=NULL
    #       until the JSONL echo arrives.
    # Both are "not yet confirmed by JSONL"; filtering only by QUEUED
    # (case a) would orphan task-launched rows and create a duplicate
    # `source=cli` row on JSONL import — losing the insights metadata
    # that the task row carries.
    # Source whitelist — DO NOT REMOVE. ContentMatcher.match() (in
    # content_matcher.py) is purely content-based with no source-awareness:
    # any candidate that happens to share content with an incoming JSONL
    # turn will be promoted, regardless of source. The (jsonl_uuid IS NULL,
    # delivered_at IS NULL) pair narrows the pool to "unconfirmed", but
    # that is NOT sufficient — we have several creation paths that briefly
    # leave both fields NULL with sources outside this whitelist (e.g.
    # certain hook + retry paths, future internal flows). The whitelist
    # is the primary gate, the NULL checks are secondary.
    #
    # When adding a NEW source that creates pre_sent → SENT rows (i.e. rows
    # that legitimately need JSONL match-promotion), add it here. Forgetting
    # was the root cause of duplicate bubbles when probe was first added.
    # Tests in test_sync_sent_to_delivered.py guard against future regressions.
    # NOTE: probe was folded into source="web" — probe rows go through the
    # standard web message path with metadata.probe_id for identification, so
    # the whitelist needs no probe-specific entry.
    candidates = (
        db.query(Message)
        .filter(
            Message.agent_id == ctx.agent_id,
            Message.role == MessageRole.USER,
            Message.status != MessageStatus.CANCELLED,
            _or(
                Message.source == "web",
                Message.source == "plan_continue",
                Message.source == "task",
            ),
            Message.jsonl_uuid.is_(None),
            Message.delivered_at.is_(None),
        )
        .order_by(Message.created_at.asc())
        .all()
    )

    web_msg, method = ContentMatcher.match(content, candidates)

    if web_msg:
        if jsonl_uuid:
            web_msg.jsonl_uuid = jsonl_uuid
        web_msg.session_seq = seq
        web_msg.delivered_at = _parse_jsonl_ts(jsonl_ts) or _utcnow()
        web_msg.status = MessageStatus.COMPLETED
        web_msg.completed_at = web_msg.delivered_at
        logger.info("Agent %s: sent → delivered for msg %s → uuid=%s (method=%s)",
                    ctx.agent_id[:8], web_msg.id, jsonl_uuid, method)

        if deferred_updates is not None:
            deferred_updates.append(web_msg.id)

        if web_msg.delivered_at:
            from websocket import emit_message_delivered
            asyncio.ensure_future(emit_message_delivered(
                ctx.agent_id, web_msg.id,
            ))
        return None

    # 3. No promotable sent row — genuine CLI-typed input
    # (includes slash_signal: a /cmd typed directly in the tmux terminal
    # — or replayed from an externally-adopted session — has no dispatched
    # SENT row to promote, so we record it as a CLI-source bubble. Web-UI
    # dispatched slash commands hit the ContentMatcher branch above and
    # never reach here, so there's no duplicate-bubble risk in the common
    # case. If ContentMatcher does miss for a real web dispatch the user
    # will see one extra cli-source row, which is louder than the previous
    # silent-drop behaviour — by design: silent drops mask real bugs.)
    _ts = _parse_jsonl_ts(jsonl_ts) or _utcnow()
    return Message(
        agent_id=ctx.agent_id,
        role=MessageRole.USER,
        content=content,
        status=MessageStatus.COMPLETED,
        source="cli",
        jsonl_uuid=jsonl_uuid,
        created_at=_ts,
        completed_at=_ts,
        delivered_at=_ts,
        tool_use_id=_extract_tool_use_id(meta),
        session_seq=seq,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Assistant/system message creation (Phase 3b)
# ---------------------------------------------------------------------------

def _create_agent_msg(db, ctx: SyncContext, content, jsonl_uuid, seq, meta, meta_json, kind, jsonl_ts=None):
    """UUID dedup, then create AGENT message. Returns Message or None."""
    if jsonl_uuid:
        existing = db.query(Message.id).filter(
            Message.agent_id == ctx.agent_id,
            Message.jsonl_uuid == jsonl_uuid,
        ).first()
        if existing:
            logger.debug("Agent %s: dedup skip uuid=%s", ctx.agent_id[:8], jsonl_uuid)
            return None

    logger.debug("Agent %s: creating message role=assistant kind=%s uuid=%s seq=%d",
                 ctx.agent_id[:8], kind, jsonl_uuid, seq)
    _now = _parse_jsonl_ts(jsonl_ts) or _utcnow()
    _tid = (meta.get("tool_use_id") if kind == "tool_use" and meta
            else _extract_tool_use_id(meta))
    return Message(
        agent_id=ctx.agent_id,
        role=MessageRole.AGENT,
        content=content,
        status=MessageStatus.COMPLETED,
        source="cli",
        meta_json=meta_json,
        jsonl_uuid=jsonl_uuid,
        created_at=_now,
        completed_at=_now,
        delivered_at=_now,
        tool_use_id=_tid,
        session_seq=seq,
        kind=kind,
    )


_DISMISS_PREFIXES = (
    "The user doesn't want to proceed",
    "User declined",
    "Tool use rejected",
)


def _is_dismiss_answer(s) -> bool:
    return isinstance(s, str) and any(s.startswith(p) for p in _DISMISS_PREFIXES)


def _detect_interactive_mismatches(db, ctx: "SyncContext", turns) -> None:
    """Side-channel: emit a SystemBubble when DB has a UI-written answer
    but JSONL's tool_result for the same interactive item is dismiss-pattern.

    Pure read on existing interactive metadata — never mutates the card.
    Dedup is per tool_use_id (synthetic jsonl_uuid="mismatch-{tid}"), so
    a second sync that re-observes the same condition won't duplicate.
    """
    for t in turns:
        if t[0] != "assistant":
            continue
        meta = t[2] if len(t) > 2 else None
        if not isinstance(meta, dict):
            continue
        for jsonl_item in meta.get("interactive") or []:
            tid = jsonl_item.get("tool_use_id")
            if not tid:
                continue
            jsonl_ans = jsonl_item.get("answer")
            if not _is_dismiss_answer(jsonl_ans):
                continue

            db_msg = (
                db.query(Message)
                .filter(
                    Message.agent_id == ctx.agent_id,
                    Message.tool_use_id == tid,
                )
                .order_by(Message.created_at.desc())
                .first()
            )
            if not db_msg or not db_msg.meta_json:
                continue
            try:
                db_meta = json.loads(db_msg.meta_json)
            except (json.JSONDecodeError, TypeError):
                continue
            db_ans = None
            for db_item in db_meta.get("interactive") or []:
                if db_item.get("tool_use_id") == tid:
                    db_ans = db_item.get("answer")
                    break
            if db_ans is None or _is_dismiss_answer(db_ans):
                continue

            bubble_uuid = f"mismatch-{tid}"
            already_emitted = (
                db.query(Message.id)
                .filter(
                    Message.agent_id == ctx.agent_id,
                    Message.jsonl_uuid == bubble_uuid,
                )
                .first()
            )
            if already_emitted:
                continue

            short = (jsonl_ans or "")[:60]
            content = f'Selection didn\'t reach Claude (got "{short}")'
            db.add(Message(
                agent_id=ctx.agent_id,
                role=MessageRole.SYSTEM,
                kind="interactive_mismatch",
                content=content,
                source="cli",
                status=MessageStatus.COMPLETED,
                jsonl_uuid=bubble_uuid,
                created_at=_utcnow(),
                completed_at=_utcnow(),
                delivered_at=_utcnow(),
            ))
            logger.info(
                "interactive_mismatch: agent=%s tid=%s ui_answer=%r jsonl_answer=%r",
                ctx.agent_id[:8], tid[:12], (db_ans or "")[:60], short,
            )


def _create_system_msg(db, ctx: SyncContext, content, jsonl_uuid, seq, kind, jsonl_ts=None):
    """UUID dedup, then create SYSTEM message. Returns Message or None."""
    if jsonl_uuid:
        existing = db.query(Message.id).filter(
            Message.agent_id == ctx.agent_id,
            Message.jsonl_uuid == jsonl_uuid,
        ).first()
        if existing:
            logger.debug("Agent %s: dedup skip uuid=%s", ctx.agent_id[:8], jsonl_uuid)
            return None

    logger.debug("Agent %s: creating message role=system kind=%s uuid=%s seq=%d",
                 ctx.agent_id[:8], kind, jsonl_uuid, seq)
    _now = _parse_jsonl_ts(jsonl_ts) or _utcnow()
    return Message(
        agent_id=ctx.agent_id,
        role=MessageRole.SYSTEM,
        content=content,
        status=MessageStatus.COMPLETED,
        source="cli",
        jsonl_uuid=jsonl_uuid,
        created_at=_now,
        completed_at=_now,
        delivered_at=_now,
        session_seq=seq,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Streaming update helper (Phase 3c)
# ---------------------------------------------------------------------------

def _handle_streaming_update(ad, ctx: SyncContext, turns, current_size) -> str:
    """Update last assistant message content if it grew (streaming).

    Only applies to text turns (tool_use turns don't stream).
    Returns "turn_updated", "exit", or "no_change".
    """
    from jsonl_parser import merge_interactive_meta as _merge_interactive_meta

    last_turn = turns[-1]
    last_kind = last_turn[4] if len(last_turn) > 4 else None
    new_hash = _content_hash(last_turn[1])

    if (new_hash == ctx.last_content_hash
            or last_turn[0] != "assistant"
            or last_kind not in ("text", None)):
        ctx.last_offset = current_size
        return "no_change"

    db = SessionLocal()
    try:
        agent = db.get(Agent, ctx.agent_id)
        if not agent or agent.status in (AgentStatus.STOPPED, AgentStatus.ERROR):
            return "exit"

        last_msg = db.query(Message).filter(
            Message.agent_id == ctx.agent_id,
            Message.role == MessageRole.AGENT,
        ).order_by(Message.created_at.desc()).first()

        if not last_msg:
            ctx.last_offset = current_size
            return "no_change"

        _role, _content, *_rest = last_turn
        _meta = _rest[0] if _rest else None
        _uuid = _rest[1] if len(_rest) > 1 else None

        logger.debug("Agent %s: streaming update msg=%s new_len=%d",
                     ctx.agent_id[:8], last_msg.id, len(_content))

        last_msg.content = _content
        last_msg.completed_at = _utcnow()
        last_msg.session_seq = last_msg.session_seq or (len(turns) - 1)
        if _uuid and not last_msg.jsonl_uuid:
            last_msg.jsonl_uuid = _uuid
        if _meta is not None:
            last_msg.meta_json = _merge_interactive_meta(
                last_msg.meta_json, _meta,
            )
        if (_content or "").strip():
            agent.last_message_preview = _content[:200]
        agent.last_message_at = _utcnow()
        db.commit()

        # Update display file with replaced content. update_last auto-emits
        # the WS new_message signal, so no explicit emit needed here.
        from display_writer import update_last as _update_display
        _update_display(ctx.agent_id, last_msg.id)

        ctx.last_content_hash = new_hash
        ctx.last_offset = current_size
        logger.info(
            "Updated last turn content for agent %s (%d chars)",
            ctx.agent_id, len(_content),
        )
    finally:
        db.close()
    return "turn_updated"


# ---------------------------------------------------------------------------
# sync_import_new_turns — SOLE message creation path
# ---------------------------------------------------------------------------

async def sync_import_new_turns(ad, ctx: SyncContext):
    """Full-parse JSONL, import new turns via pointer.

    This is the SOLE path that creates Message rows from JSONL.
    Returns: "new_turns", "turn_updated", "no_change", "compact", "exit",
             "commit_error"
    """
    from jsonl_parser import (
        parse_session_turns as _parse_session_turns,
        merge_interactive_meta as _merge_interactive_meta,
    )
    from websocket import emit_agent_update
    from thumbnails import generate_thumbnails_for_message

    # 1. Check file size for change detection
    try:
        current_size = os.path.getsize(ctx.jsonl_path)
    except OSError as e:
        logger.warning("Cannot stat JSONL %s: %s", ctx.jsonl_path, e)
        return "no_change"

    if current_size < ctx.last_offset:
        return "compact"  # caller handles via sync_full_scan

    if current_size == ctx.last_offset:
        return "no_change"

    # 2. Full parse (in thread — large files block the event loop)
    turns = await asyncio.to_thread(_parse_session_turns, ctx.jsonl_path)

    logger.debug("Agent %s: parsed %d total turns, pointer at %d, new_turns=%d",
                 ctx.agent_id[:8], len(turns), ctx.last_turn_count,
                 max(0, len(turns) - ctx.last_turn_count))

    # 3. Detect turn count decrease (compact with longer summary)
    if len(turns) < ctx.last_turn_count:
        return "compact"

    # 4. Slice new turns using the pointer
    new_turns = turns[ctx.last_turn_count:]

    # DRIFT_INSTRUMENT: log entry-level breakdown of new_turns so we can see
    # whether multi-block JSONL entries (text + tool_use_A + tool_use_B) are
    # being correctly expanded into separate parser turns.
    if new_turns:
        from collections import Counter as _Counter
        _kind_counter = _Counter(
            (t[0], t[4] if len(t) > 4 else None) for t in new_turns
        )
        logger.info(
            "DRIFT_INSTRUMENT sync_start agent=%s pointer=%d total_turns=%d "
            "new_turns=%d breakdown=%s",
            ctx.agent_id[:8], ctx.last_turn_count, len(turns),
            len(new_turns), dict(_kind_counter),
        )

    # 5. Streaming update — last turn content changed but no new turns
    if not new_turns and turns:
        return _handle_streaming_update(ad, ctx, turns, current_size)

    if not new_turns:
        ctx.last_offset = current_size
        return "no_change"

    # 6. Import new turns
    db = SessionLocal()
    try:
        agent = db.get(Agent, ctx.agent_id)
        if not agent or agent.status in (AgentStatus.STOPPED, AgentStatus.ERROR):
            return "exit"

        # Sanity check: previous turn content should not grow between syncs.
        # Claude Code buffers responses and flushes to JSONL on completion,
        # so content should be final when hook-driven sync imports it.
        if ctx.last_turn_count > 0:
            prev_role, prev_content, *prev_rest = turns[ctx.last_turn_count - 1]
            if prev_role == "assistant":
                last_agent_msg = db.query(Message).filter(
                    Message.agent_id == ctx.agent_id,
                    Message.role == MessageRole.AGENT,
                ).order_by(Message.created_at.desc()).first()
                if (last_agent_msg
                        and len(last_agent_msg.content or "") < len(prev_content)):
                    logger.warning(
                        "Agent %s: previous turn content grew between syncs "
                        "(db_len=%d, jsonl_len=%d, msg_id=%s, jsonl_uuid=%s). "
                        "DB message may have stale content.",
                        ctx.agent_id[:8],
                        len(last_agent_msg.content or ""),
                        len(prev_content),
                        last_agent_msg.id,
                        last_agent_msg.jsonl_uuid,
                    )

        _actually_inserted = 0
        # DRIFT_INSTRUMENT: track per-turn outcomes for end-of-loop reconciliation
        _drift_skipped_dedup: list[tuple[int, str, str | None, str | None]] = []
        _drift_skipped_integrity: list[tuple[int, str, str | None, str | None, bool, str]] = []
        _drift_skipped_other_role: list[tuple[int, str]] = []
        # State signals are accumulated across new_turns (see derivation
        # block after the import loop). The historical-replay risk that
        # last-only was guarding against is now bounded by 93fbd00's
        # benign-vs-real drift split, so accumulator's robustness wins.
        # Accumulate message_ids updated (sent→delivered) this cycle so we
        # can call update_last AFTER db.commit(). Writing inline would
        # violate the display_writer "commit → then flush" contract (the
        # function opens its own session, which would see stale state).
        _deferred_updates: list[str] = []
        _pending_inserts: list[Message] = []
        _queued_uuids: set[str] = set()
        # Batch indices of turns that were UUID-dedup'd against rows already
        # in the DB — i.e. replayed history (pointer rewind / full-scan drift
        # repair), not live events. The status-signal accumulator below must
        # skip these: counting a replayed stop_hook flipped EXECUTING→IDLE
        # mid-task and re-fired stop-hook ops (the auto-compact spurious-IDLE
        # incident). Indexed by batch position, not uuid, so a live turn that
        # shares a uuid with a same-batch duplicate is still counted.
        _replayed_idx: set[int] = set()
        for i, (role, content, *rest) in enumerate(new_turns):
            seq = ctx.last_turn_count + i
            meta = rest[0] if rest else None
            jsonl_uuid = rest[1] if len(rest) > 1 else None
            kind = rest[2] if len(rest) > 2 else None
            jsonl_ts = rest[3] if len(rest) > 3 else None
            meta_json = json.dumps(meta) if meta else None

            logger.debug("Agent %s: processing turn %d: role=%s kind=%s uuid=%s content_len=%d",
                         ctx.agent_id[:8], seq, role, kind, jsonl_uuid, len(content or ""))

            if _is_unimported_turn(role, kind):
                _drift_skipped_other_role.append((seq, role))
                continue

            # In-memory dedup for same-batch duplicates — the first
            # occurrence carries the signal, the duplicate doesn't.
            if jsonl_uuid and jsonl_uuid in _queued_uuids:
                _drift_skipped_dedup.append((seq, role, kind, jsonl_uuid))
                _replayed_idx.add(i)
                continue

            if role == "user":
                _deferred_before = len(_deferred_updates)
                msg = _promote_or_create_user_msg(
                    db, ctx, content, jsonl_uuid, seq, meta, kind, jsonl_ts,
                    deferred_updates=_deferred_updates,
                )
                if msg is None:
                    _drift_skipped_dedup.append((seq, role, kind, jsonl_uuid))
                    # A sent→delivered promotion (deferred_updates grew) is a
                    # live delivery event and keeps its signal; a plain UUID
                    # dedup is replayed history.
                    if len(_deferred_updates) == _deferred_before:
                        _replayed_idx.add(i)
                    continue

            elif role == "assistant":
                msg = _create_agent_msg(
                    db, ctx, content, jsonl_uuid, seq, meta, meta_json, kind, jsonl_ts,
                )
                if msg is None:
                    _drift_skipped_dedup.append((seq, role, kind, jsonl_uuid))
                    _replayed_idx.add(i)
                    continue

            elif role == "system":
                msg = _create_system_msg(
                    db, ctx, content, jsonl_uuid, seq, kind, jsonl_ts,
                )
                if msg is None:
                    _drift_skipped_dedup.append((seq, role, kind, jsonl_uuid))
                    _replayed_idx.add(i)
                    continue

            else:
                _drift_skipped_other_role.append((seq, role))
                continue

            if msg.jsonl_uuid:
                _queued_uuids.add(msg.jsonl_uuid)
            _pending_inserts.append(msg)
            _actually_inserted += 1

        # Side-channel: detect UI/JSONL answer mismatches and emit a
        # SystemBubble. Doesn't touch existing card metadata.
        _detect_interactive_mismatches(db, ctx, turns)

        # Batch write — write lock only held during add_all + commit
        for _msg in _pending_inserts:
            db.add(_msg)

        if _actually_inserted:
            # Pick the most recent meaningful turn for the preview.
            # Skip stop_hook (signal, not content); for empty-content turns,
            # synthesize a preview from interactive metadata so AskUserQuestion
            # / ExitPlanMode show on the card instead of falling through to
            # "No messages yet".  Otherwise the previous (still-meaningful)
            # preview is preserved.
            _preview_text: str | None = None
            for _t in reversed(new_turns):
                _kind = _t[4] if len(_t) > 4 else None
                if _kind == "stop_hook":
                    continue
                _content = _t[1] or ""
                if _content.strip():
                    _preview_text = _content
                    break
                _meta = _t[2] if len(_t) > 2 else None
                if isinstance(_meta, dict):
                    _items = _meta.get("interactive") or []
                    if _items:
                        _first = _items[0]
                        if _first.get("type") == "ask_user_question":
                            _qs = _first.get("questions") or []
                            if _qs:
                                _q = _qs[0].get("question", "").strip()
                                _preview_text = f"[interactive cards] {_q}" if _q else "[interactive cards] question"
                                break
                        elif _first.get("type") == "exit_plan_mode":
                            _plan = (_first.get("plan") or "").strip()
                            _preview_text = f"[interactive cards] {_plan}" if _plan else "[interactive cards] plan ready"
                            break
            if _preview_text is not None:
                agent.last_message_preview = _preview_text[:200]
            agent.last_message_at = _utcnow()

        try:
            db.commit()
        except (DatabaseError, IntegrityError) as exc:
            db.rollback()
            logger.warning(
                "Commit failed for agent %s, will retry next cycle: %s",
                ctx.agent_id[:8], exc,
            )
            # Drop deferred updates — the rows never committed, so the
            # display file must not reflect them.
            _deferred_updates.clear()
            # DO NOT advance pointer — next cycle retries, UUID dedup
            # skips already-committed turns
            return "commit_error"

        # DRIFT_INSTRUMENT: log per-sync reconciliation summary + real-time
        # drift snapshot. This is the smoking-gun log for Bug B — if the JSONL
        # has any UUID that doesn't exist in DB after this sync's commit, we
        # log it immediately (instead of waiting for startup full_scan).
        _expected = len(new_turns)
        _skipped_total = (
            len(_drift_skipped_dedup)
            + len(_drift_skipped_integrity)
            + len(_drift_skipped_other_role)
        )
        if _actually_inserted + _skipped_total != _expected:
            logger.error(
                "DRIFT_INSTRUMENT count_mismatch agent=%s "
                "expected=%d inserted=%d skipped_dedup=%d "
                "skipped_integrity=%d skipped_other_role=%d",
                ctx.agent_id[:8], _expected, _actually_inserted,
                len(_drift_skipped_dedup), len(_drift_skipped_integrity),
                len(_drift_skipped_other_role),
            )

        # Real-time drift snapshot: compare full parser-emitted UUID set
        # against DB UUID set for this agent. Anything in JSONL but not in
        # DB is drift. Cheap (one indexed query, ~1ms for typical sessions).
        # Intentionally-unimported turns are excluded — they never get DB
        # rows, so counting them produced a permanent drift_detected error
        # on every cycle.
        _parser_uuids = {
            t[3] for t in turns
            if len(t) > 3 and t[3]
            and not _is_unimported_turn(t[0], t[4] if len(t) > 4 else None)
        }
        if _parser_uuids:
            _db_uuids_now = set(
                _r[0] for _r in
                db.query(Message.jsonl_uuid)
                  .filter(Message.agent_id == ctx.agent_id,
                          Message.jsonl_uuid.isnot(None))
                  .all()
            )
            _drift_uuids = _parser_uuids - _db_uuids_now
            if _drift_uuids:
                # Find the offending turns' positions + kinds for diagnosis
                _drift_details = []
                for _t in turns:
                    if len(_t) > 3 and _t[3] in _drift_uuids:
                        _drift_details.append({
                            "uuid": _t[3],
                            "role": _t[0],
                            "kind": _t[4] if len(_t) > 4 else None,
                            "ts": _t[5] if len(_t) > 5 else None,
                        })
                logger.error(
                    "DRIFT_INSTRUMENT drift_detected agent=%s "
                    "drift_count=%d parser_uuids=%d db_uuids=%d details=%s",
                    ctx.agent_id[:8], len(_drift_uuids),
                    len(_parser_uuids), len(_db_uuids_now),
                    _drift_details[:10],  # cap to avoid log explosion
                )

        # DRIFT_INSTRUMENT: lifecycle summary line for every sync that imported
        # at least one turn. INFO-level so it shows up in default log; one line.
        if _actually_inserted or _skipped_total:
            logger.info(
                "DRIFT_INSTRUMENT sync_done agent=%s expected=%d inserted=%d "
                "skipped_dedup=%d skipped_integrity=%d new_pointer=%d",
                ctx.agent_id[:8], _expected, _actually_inserted,
                len(_drift_skipped_dedup), len(_drift_skipped_integrity),
                len(turns),
            )

        # Advance pointer ONLY on successful commit
        ctx.last_turn_count = len(turns)
        ctx.last_offset = current_size
        ctx.last_content_hash = _content_hash(turns[-1][1]) if turns else ""

        # Persist pointer to DB so restart resumes from this exact spot.
        # Without this, ctx is rebuilt with all-zero pointer and the next
        # sync re-traverses the entire JSONL — re-firing every historical
        # signal (push notify, status inference, dispatch, etc).
        _agent_pointer = db.get(Agent, ctx.agent_id)
        if _agent_pointer:
            _agent_pointer.sync_last_offset = ctx.last_offset
            _agent_pointer.sync_last_turn_count = ctx.last_turn_count
            _agent_pointer.sync_last_content_hash = ctx.last_content_hash
            try:
                db.commit()
            except (DatabaseError, IntegrityError):
                db.rollback()  # pointer write is non-critical; will retry next cycle

        # cc_sessions live-event bookkeeping. Best-effort — wrapped in a
        # broad try/except since this is a side-table. Skipped silently
        # if the writer module isn't importable yet.
        if _actually_inserted > 0:
            try:
                import cc_session_writer as _ccw
                from session_history import sum_jsonl_usage as _sum_usage

                # Pull current totals from the JSONL we just imported
                # from. The file was already read by the parser so the
                # OS page cache is hot — this scan is cheap.
                _cc_totals = _sum_usage(ctx.jsonl_path)

                # Determine started_at from the first turn's timestamp
                # (only meaningful on insert; upsert ignores None on
                # update so passing it on every batch is safe).
                _started_at = None
                if turns:
                    _ts0 = turns[0][5] if len(turns[0]) > 5 else None
                    _started_at = _parse_jsonl_ts(_ts0)

                # Pull worktree for this agent — needed for the row
                # (read once per batch).
                _agent_for_cc = db.get(Agent, ctx.agent_id)
                _worktree_for_cc = _agent_for_cc.worktree if _agent_for_cc else None
                _model_for_cc = _agent_for_cc.model if _agent_for_cc else None

                _ccw.upsert_cc_session(
                    session_id=ctx.session_id,
                    agent_id=ctx.agent_id,
                    project_path=ctx.project_path,
                    worktree=_worktree_for_cc,
                    started_at=_started_at,
                    end_reason="active",
                    model=_model_for_cc,
                    totals=_cc_totals,
                )

                # Sub-session detection: scan once per sync batch (NOT
                # per-turn). Triggered when this batch contains any
                # tool_result OR a Task tool_use — both indicate a Task
                # call may have just completed and emitted a JSONL of
                # its own.
                _saw_task_tool = any(
                    (len(_t) > 4 and _t[4] in ("tool_use", "tool_result"))
                    and (
                        # Look for Task tool by name in metadata
                        isinstance(_t[2], dict)
                        and _t[2].get("tool_name") == "Task"
                    )
                    for _t in new_turns
                )
                # Fallback: any tool_result also triggers a scan, since
                # the sub-session JSONL is most reliably present after
                # the result lands.
                _saw_any_tool_result = any(
                    (len(_t) > 4 and _t[4] == "tool_result")
                    for _t in new_turns
                )
                if _saw_task_tool or _saw_any_tool_result:
                    try:
                        _ccw.detect_and_record_subsessions(
                            ctx.jsonl_path, ctx.session_id, ctx.agent_id,
                            project_path=ctx.project_path,
                            worktree=_worktree_for_cc,
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "cc_session_writer.detect_and_record_subsessions failed",
                            exc_info=True,
                        )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "cc_session bookkeeping failed for agent %s",
                    ctx.agent_id[:8], exc_info=True,
                )

        # Sent rows already carry display_seq from the promote-to-sent
        # step. update_last appends a _replace line reflecting the new
        # status (delivered/completed), so the display file transitions
        # the bubble without a new seq slot.
        if _deferred_updates:
            from display_writer import update_last as _update_last
            for _updated_id in _deferred_updates:
                _update_last(ctx.agent_id, _updated_id)

        # Flush remaining undisplayed messages (AGENT/SYSTEM and any USER
        # turns not matched to a sent row — i.e. genuine CLI input).
        from display_writer import flush_agent as _flush_display
        _flush_display(ctx.agent_id)

        # Derive state signals by accumulating across new_turns. Last-only
        # was structurally fragile: Claude Code's JSONL writer occasionally
        # places a trailing assistant entry after stop_hook_summary (with an
        # earlier timestamp), which left agents stuck EXECUTING.
        #
        # Replayed turns (_replayed_idx — UUID-dedup'd against rows already
        # in the DB) are excluded: they are history, not live signals. A
        # pointer rewind re-slices old turns, and counting their stop_hooks
        # flipped EXECUTING→IDLE mid-task and re-fired stop-hook operations
        # (push notify, unread bump, dispatch). Genuinely-new turns are
        # always inserted (or promoted sent→delivered), so live signals —
        # including the trailing-assistant case above — survive the gate.
        _saw_user_turn = False
        _saw_assistant_turn = False
        _saw_stop_hook = False
        _saw_interrupt = False
        _saw_rate_limit = False
        for _i, _t in enumerate(new_turns):
            if _i in _replayed_idx:
                continue
            _r = _t[0]
            _k = _t[4] if len(_t) > 4 else None
            if _r == "user":
                if _k != "slash_signal":
                    _saw_user_turn = True
            elif _r == "assistant":
                _saw_assistant_turn = True
            elif _r == "system":
                if _k == "stop_hook":
                    _saw_stop_hook = True
                elif _k == "interrupt":
                    _saw_interrupt = True
                elif _k == "rate_limit":
                    _saw_rate_limit = True

        # Status inference from accumulated signals — sync_engine is the
        # truth writer for EXECUTING/IDLE.
        _inferred_status = _infer_status_from_signals(
            db, ctx,
            saw_user_turn=_saw_user_turn,
            saw_assistant_turn=_saw_assistant_turn,
            saw_stop_hook=_saw_stop_hook,
            saw_rate_limit=_saw_rate_limit,
            saw_interrupt=_saw_interrupt,
        )
        if _inferred_status:
            from websocket import emit_agent_update as _emit_agent_update
            _agent_for_emit = db.get(Agent, ctx.agent_id)
            ad._emit(_emit_agent_update(
                ctx.agent_id, _inferred_status,
                _agent_for_emit.project if _agent_for_emit else "",
            ))

        # Rate limit detected: transition to IDLE but do NOT dispatch queued
        # messages — the agent cannot process them while rate-limited.  The
        # /rate-limit-options menu Claude Code shows is left for the user to
        # dismiss manually (auto-dismiss removed for thread-safety).
        if _saw_rate_limit:
            logger.info(
                "sync: rate_limit detected for agent %s — "
                "transitioning to IDLE (no dispatch)",
                ctx.agent_id[:8],
            )
            ad._stop_generating(ctx.agent_id)

        # Interrupt detected in JSONL: stop generating and dispatch PENDING
        # messages.  _stop_generating is called HERE (after db.commit) rather
        # than inside the turn loop to avoid a self-deadlock: the loop holds
        # uncommitted SAVEPOINTs on one connection while _stop_generating
        # opens a second connection that tries to write — same async task,
        # two connections, SQLite single-writer lock → 5s timeout → crash.
        if _saw_interrupt:
            ad._stop_generating(ctx.agent_id)
            if not _saw_rate_limit:
                asyncio.ensure_future(ad.dispatch_pending_message(ctx.agent_id, delay=0))
            # Dismiss any unanswered interactive cards — the interrupt killed
            # the tool_use before a tool_result could be written, so the
            # PostToolUse backfill path will never fire.
            from routers.agents import _dismiss_pending_interactive_cards
            _dismissed = _dismiss_pending_interactive_cards(db, ctx.agent_id)
            if _dismissed:
                from websocket import emit_metadata_update
                for _d in _dismissed:
                    ad._emit(emit_metadata_update(
                        ctx.agent_id, _d["message_id"],
                    ))
                logger.info(
                    "sync: dismissed %d interactive card(s) for agent %s on interrupt",
                    len(_dismissed), ctx.agent_id[:8],
                )

        # stop_hook_summary in JSONL: this is the authoritative signal that
        # the agent finished a turn.  Perform all stop-hook operations here
        # (the HTTP handler only wakes sync; this is the sole executor).
        # _stop_generating is idempotent; unread/notify/dispatch run once
        # because the HTTP handler no longer does them.
        if _saw_stop_hook:
            logger.info(
                "sync: stop_hook_summary detected for agent %s — "
                "executing stop-hook operations",
                ctx.agent_id[:8],
            )
            ad._stop_generating(ctx.agent_id)
            # unread + notify
            _sh_db = SessionLocal()
            _sh_project = None
            _sh_status = None
            _sh_bumped = False
            try:
                _sh_agent = _sh_db.get(Agent, ctx.agent_id)
                if _sh_agent:
                    _is_sub = _sh_agent.is_subagent or _sh_agent.parent_id
                    if not _is_sub and not ad._is_agent_in_use(
                        _sh_agent.id, _sh_agent.tmux_pane
                    ):
                        _sh_agent.unread_count += 1
                        _sh_bumped = True
                    _sh_db.commit()
                    _sh_project = _sh_agent.project
                    _sh_status = _sh_agent.status.value
                    if not _is_sub:
                        ad._maybe_notify_message(_sh_agent)
            finally:
                _sh_db.close()
            # Broadcast immediately so frontend subscribers (e.g. the FAB
            # unread badge) update in sync with the APNs push, instead of
            # waiting for the per-turn emit at the end of sync import (which
            # can lag 2-3s when importing large deltas / thumbnails).
            if _sh_bumped and _sh_project is not None:
                from websocket import emit_agent_update
                ad._emit(emit_agent_update(
                    ctx.agent_id, _sh_status or "IDLE", _sh_project,
                ))
            # Dismiss stale interactive cards — if the agent continued past
            # an AskUserQuestion (e.g. timeout), the tool_result lands in a
            # later sync batch than the tool_use, so the pointer-based import
            # never retroactively updates the card's answer in the DB/display.
            from routers.agents import _dismiss_pending_interactive_cards
            _sh_dismissed = _dismiss_pending_interactive_cards(db, ctx.agent_id)
            if _sh_dismissed:
                from websocket import emit_metadata_update
                for _d in _sh_dismissed:
                    ad._emit(emit_metadata_update(
                        ctx.agent_id, _d["message_id"],
                    ))
                logger.info(
                    "sync: dismissed %d interactive card(s) for agent %s on stop_hook",
                    len(_sh_dismissed), ctx.agent_id[:8],
                )
            # mark slash commands completed
            import slash_commands as _sc
            _sc.mark_completed(ctx.agent_id)
            # dispatch pending (no delay — response already imported above)
            # Skip dispatch if rate-limited — agent can't process messages
            if not _saw_rate_limit:
                asyncio.ensure_future(
                    ad.dispatch_pending_message(ctx.agent_id, delay=0)
                )
            # Fire-and-forget resume hint refresh — summarize this agent's
            # last few turns into the project's recap.  Stop hook is the
            # authoritative "turn is done" signal.
            if _sh_project and _sh_agent and not _sh_agent.is_subagent:
                from routers.projects import _refresh_resume_hint
                asyncio.ensure_future(_refresh_resume_hint(ctx.agent_id))

        logger.debug("Agent %s: sync_import result=%s, inserted=%d, pointer now=%d",
                     ctx.agent_id[:8], "new_turns", _actually_inserted, ctx.last_turn_count)

        ad._emit(emit_agent_update(
            agent.id, agent.status.value, agent.project,
        ))
        logger.info(
            "Synced %d new turns for agent %s (roles=%s)",
            len(new_turns), ctx.agent_id,
            [r for r, *_ in new_turns],
        )
        # new_message WS signal is now auto-emitted by flush_agent (called
        # at line 855 above) whenever it actually wrote display lines —
        # single source of truth, no explicit emit needed here.
        if _actually_inserted > 0:
            # Push an updated token-budget snapshot whenever a new assistant
            # turn lands — its `usage` block is what context_usage reads.
            if any(r == "assistant" for r, *_ in new_turns):
                from websocket import emit_context_usage as _emit_ctx
                ad._emit(_emit_ctx(agent.id))

        # Notify for unanswered interactive items
        _notify_interactive(ad, agent, new_turns)

        # Generate video thumbnails for new assistant turns. Pre-filter
        # with a cheap substring check so we only spawn a worker thread
        # when the content actually mentions a video extension —
        # otherwise every assistant turn would queue a no-op task whose
        # closure pinned the full content string in memory until the
        # default thread pool drained (a slow leak under load).
        from thumbnails import content_likely_has_video
        for _r, _c, *_ in new_turns:
            if _r == "assistant" and _c and content_likely_has_video(_c):
                asyncio.ensure_future(asyncio.to_thread(
                    generate_thumbnails_for_message, _c, ctx.project_path,
                ))

    finally:
        db.close()

    return "new_turns"


# ---------------------------------------------------------------------------
# sync_full_scan — read-only audit + pointer reset
# ---------------------------------------------------------------------------

async def sync_full_scan(ad, ctx: SyncContext, reason: str = "startup"):
    """Read-only audit + pointer reset.

    Called on: startup, compact, clear, new session, manual trigger.
    - On compact: deletes orphaned DB messages, reassigns session_seq.
    - On mismatch: logs warning, resets pointer for reimport.
    - Always: resets the sync pointer to current state.
    - NEVER creates or updates regular messages from JSONL.
    """
    from jsonl_parser import parse_session_turns as _parse_session_turns

    logger.info("Full scan for agent %s (reason=%s)", ctx.agent_id, reason)

    turns = await asyncio.to_thread(_parse_session_turns, ctx.jsonl_path, max_bytes=MAX_AUDIT_FILE_SIZE)
    try:
        current_size = os.path.getsize(ctx.jsonl_path)
    except OSError as e:
        logger.warning("Cannot stat JSONL %s during audit: %s", ctx.jsonl_path, e)
        current_size = 0

    # Collect all UUIDs from JSONL
    jsonl_uuids = {t[3] for t in turns if len(t) > 3 and t[3]}
    logger.debug("Agent %s: full_scan found %d JSONL UUIDs", ctx.agent_id[:8], len(jsonl_uuids))

    db = SessionLocal()
    try:
        # Get all DB messages that have a jsonl_uuid (includes cli-sourced
        # AND web-sourced messages that were linked via wrapped-prompt matching)
        db_msgs = (
            db.query(Message)
            .filter(
                Message.agent_id == ctx.agent_id,
                Message.jsonl_uuid.isnot(None),
            )
            .all()
        )

        db_by_uuid = {m.jsonl_uuid: m for m in db_msgs}
        logger.debug("Agent %s: full_scan found %d DB messages with UUID", ctx.agent_id[:8], len(db_msgs))

        # Detect drift. Intentionally-unimported turns (slash_signal) are
        # excluded from the missing set: they never get DB rows by design,
        # so counting them as missing rewinds the pointer on EVERY full
        # scan and replays history. They stay in jsonl_uuids on purpose —
        # extra_in_db must still recognize legacy DB rows holding their
        # uuids (pre-93ca3330 imports), otherwise a compact scan would
        # purge those rows as orphans.
        _unimported_uuids = {
            t[3] for t in turns
            if len(t) > 3 and t[3]
            and _is_unimported_turn(t[0], t[4] if len(t) > 4 else None)
        }
        missing_in_db = [
            u for u in jsonl_uuids
            if u not in db_by_uuid and u not in _unimported_uuids
        ]
        extra_in_db = [
            m for m in db_msgs
            if m.jsonl_uuid and m.jsonl_uuid not in jsonl_uuids
        ]
        content_mismatches = []
        for t in turns:
            role, content = t[0], t[1]
            uuid = t[3] if len(t) > 3 else None
            # Skip USER turns: DB stores the user-typed input (what the chat
            # UI shows), JSONL records the dispatcher-wrapped prompt (with
            # project context + "Leave a summary…" tail). The two are
            # intentionally different and not a drift signal.
            if role == "user":
                continue
            if uuid and uuid in db_by_uuid:
                db_msg = db_by_uuid[uuid]
                if abs(len(db_msg.content or "") - len(content)) > 50:
                    content_mismatches.append(uuid)

        logger.debug("Agent %s: missing_in_db=%d extra_in_db=%d mismatches=%d",
                     ctx.agent_id[:8], len(missing_in_db), len(extra_in_db), len(content_mismatches))

        _changes_made = False

        # On compact: delete orphaned cli-sourced messages + reassign session_seq
        _compact_finalized_msg_id: str | None = None
        _compact_activity_id: str | None = None
        # The scan above reads only the LAST MAX_AUDIT_FILE_SIZE bytes of an
        # oversized JSONL (parse_session_turns tail-caps). Every turn before
        # that window is then absent from jsonl_uuids and would be purged
        # as an "orphan" although it is still in the file — observed on
        # 2026-09-21/22 for a 151 MB session: three compactions purged
        # 466 + 1489 + 1264 agent/system rows that the JSONL still held.
        # Orphan detection is only meaningful when the whole file was read.
        _scan_truncated = MAX_AUDIT_FILE_SIZE > 0 and current_size > MAX_AUDIT_FILE_SIZE
        if reason == "compact":
            _cli_orphans = [m for m in extra_in_db if m.source == "cli"]
            if _cli_orphans and _scan_truncated:
                logger.warning(
                    "Agent %s: compact scan read only the last %d of %d bytes — "
                    "skipping orphan purge of %d cli rows (cannot tell orphans "
                    "from turns outside the scanned window)",
                    ctx.agent_id[:8], MAX_AUDIT_FILE_SIZE, current_size, len(_cli_orphans),
                )
                _cli_orphans = []
            if _cli_orphans:
                # DRIFT_INSTRUMENT: log every UUID being purged. If we ever see
                # this fire outside a real /compact event, we have a smoking
                # gun for one possible Bug B mechanism (cleanup mistakes a
                # transient parser shortfall for a true compact).
                _orphan_details = [
                    {"uuid": m.jsonl_uuid, "role": m.role.value if m.role else None,
                     "kind": m.kind, "preview": (m.content or "")[:60]}
                    for m in _cli_orphans[:10]
                ]
                logger.warning(
                    "DRIFT_INSTRUMENT compact_purge agent=%s reason=%s "
                    "purge_count=%d details=%s",
                    ctx.agent_id, reason, len(_cli_orphans), _orphan_details,
                )
                for m in _cli_orphans:
                    db.delete(m)
                logger.info(
                    "Purged %d orphaned messages for agent %s after compact",
                    len(_cli_orphans), ctx.agent_id,
                )
                _changes_made = True

            # Reassign session_seq from fresh turn order
            for idx, t in enumerate(turns):
                uuid = t[3] if len(t) > 3 else None
                if uuid and uuid in db_by_uuid:
                    db_by_uuid[uuid].session_seq = idx
            _changes_made = True

            # Finalize the /compact user message. It never lands in
            # post-compact JSONL (CC rewrites the file and drops that turn),
            # so _promote_or_create_user_msg can't match it. This path is
            # the only point the row ever gets its completion tick.
            _compact_msg = (
                db.query(Message)
                .filter(
                    Message.agent_id == ctx.agent_id,
                    Message.role == MessageRole.USER,
                    Message.source == "web",
                    Message.completed_at.is_(None),
                    Message.content.startswith("/compact"),
                )
                .order_by(Message.created_at.desc())
                .first()
            )
            if _compact_msg:
                _now = _utcnow()
                _compact_msg.completed_at = _now
                _compact_msg.status = MessageStatus.COMPLETED
                if not _compact_msg.delivered_at:
                    _compact_msg.delivered_at = _now
                # Fake jsonl_uuid so subsequent UUID-dedup / content-match
                # cycles don't try to re-link this row to a real user turn.
                if not _compact_msg.jsonl_uuid:
                    _compact_msg.jsonl_uuid = f"slash-{_compact_msg.id[:8]}"
                _compact_finalized_msg_id = _compact_msg.id
                _changes_made = True

            # End compact tool activity record
            _compact_activity_id = _end_compact_activity(
                db, ctx.agent_id, ctx.session_id,
            )

            # Handle compact UI signals
            if ctx.compact_end_emitted:
                ctx.compact_end_emitted = False
            else:
                import time as _time
                ctx.compact_detected_at = _time.monotonic()
            ctx.compact_notified = False
            ctx.compact_notified_at = 0.0

            # Status transitions for the compact window are owned by the
            # PreCompact (→EXECUTING) and PostCompact (manual→IDLE,
            # auto→no-op) hook handlers in routers/hooks.py, which read
            # `body["trigger"]` directly from each hook's payload. Sync
            # only reconciles JSONL turns here.

        # Log drift — no UI bubbles, no silent skipping.
        if missing_in_db:
            logger.warning(
                "Agent %s: %d JSONL turns not in DB (%s), resetting pointer for reimport",
                ctx.agent_id, len(missing_in_db), reason,
            )
        if extra_in_db and reason != "compact":
            logger.warning(
                "Agent %s: %d DB messages not in JSONL (%s)",
                ctx.agent_id, len(extra_in_db), reason,
            )
        if content_mismatches:
            logger.warning(
                "Agent %s: %d content mismatches (%s)",
                ctx.agent_id, len(content_mismatches), reason,
            )

        if _changes_made:
            db.commit()

        # Append targeted _replace lines for rows whose status changed in
        # the compact block above. We deliberately do NOT call rebuild_agent
        # here: the display file is an event log of what the user has seen,
        # not a projection of current DB state. Rebuilding would (a) drop
        # SENT-but-undelivered USER orphans like messages eaten by a TUI
        # modal during compact, (b) silently erase cli-orphan rows the user
        # already saw, and (c) re-shuffle display_seq based on the post-
        # compact session_seq even though the visible timeline shouldn't
        # change. update_last keeps the existing entries intact and only
        # appends the status transitions.
        if reason == "compact":
            from display_writer import update_last as _update_last
            if _compact_finalized_msg_id:
                _compact_msg_re = db.get(Message, _compact_finalized_msg_id)
                if _compact_msg_re and _compact_msg_re.completed_at:
                    _update_last(ctx.agent_id, _compact_finalized_msg_id)
                    from websocket import emit_message_executed
                    asyncio.ensure_future(emit_message_executed(
                        ctx.agent_id, _compact_finalized_msg_id,
                    ))
            if _compact_activity_id:
                _update_last(ctx.agent_id, _compact_activity_id)
                from websocket import emit_message_update
                asyncio.ensure_future(emit_message_update(
                    ctx.agent_id, _compact_activity_id,
                ))

        # If turns are missing from DB, decide whether this is real drift
        # (missing turn at an index BEFORE our pointer — sync genuinely
        # dropped it) or a benign timing gap (missing turn at an index
        # AFTER our pointer — incremental sync hasn't run yet for it).
        # Only real drift triggers a partial pointer rewind; benign gaps
        # are left for the next incremental sync to handle.
        _old_count = ctx.last_turn_count
        _missing_set = set(missing_in_db)
        _earliest_missing_idx = None
        if _missing_set:
            for _i, _t in enumerate(turns):
                if len(_t) > 3 and _t[3] in _missing_set:
                    _earliest_missing_idx = _i
                    break

        if _earliest_missing_idx is not None and _earliest_missing_idx < ctx.last_turn_count:
            # Real drift — rewind pointer to the missing turn so
            # incremental sync re-imports from there. UUID dedup in
            # sync_import_new_turns skips rows that did successfully
            # commit between earliest_missing_idx and old pointer, so
            # this only re-creates the genuinely-missing rows.
            ctx.last_turn_count = _earliest_missing_idx
            ctx.last_offset = 0
            ctx.last_content_hash = ""
            logger.warning(
                "Agent %s: real drift, rewinding pointer %d → %d",
                ctx.agent_id[:8], _old_count, _earliest_missing_idx,
            )
        elif _earliest_missing_idx is not None:
            # Missing UUIDs at index >= ctx.last_turn_count — incremental
            # sync would re-import them on next cycle IF we don't advance
            # the pointer past them. The original code unconditionally set
            # ctx.last_turn_count = len(turns), which skipped ALL post-pointer
            # missing UUIDs (Bug B: pm2-reload race + 93fbd00 off-by-one).
            # Leave pointer at the earliest missing position so the next
            # sync_import_new_turns picks it up.
            ctx.last_turn_count = _earliest_missing_idx
            ctx.last_offset = 0
            ctx.last_content_hash = ""
            logger.warning(
                "Agent %s: post-pointer missing turn — leaving pointer at %d "
                "(was %d, len(turns)=%d) so next sync re-imports",
                ctx.agent_id[:8], _earliest_missing_idx, _old_count, len(turns),
            )
        else:
            # No missing turns — safe to advance to current end.
            ctx.last_turn_count = len(turns)
            ctx.last_offset = current_size
            ctx.last_content_hash = _content_hash(turns[-1][1]) if turns else ""
        # Persist to DB so restart resumes from this pointer.
        _agent_for_pointer = db.get(Agent, ctx.agent_id)
        if _agent_for_pointer:
            _agent_for_pointer.sync_last_offset = ctx.last_offset
            _agent_for_pointer.sync_last_turn_count = ctx.last_turn_count
            _agent_for_pointer.sync_last_content_hash = ctx.last_content_hash
            try:
                db.commit()
            except (DatabaseError, IntegrityError):
                db.rollback()
        logger.debug("Agent %s: pointer reset to %d (was %d)",
                     ctx.agent_id[:8], ctx.last_turn_count, _old_count)

        logger.info(
            "Full scan complete for agent %s: %d turns, pointer at %d bytes",
            ctx.agent_id, len(turns), current_size,
        )

        return {
            "turns": len(turns),
            "missing_in_db": len(missing_in_db),
            "extra_in_db": len(extra_in_db),
            "content_mismatches": len(content_mismatches),
        }
    except (DatabaseError, IntegrityError) as e:
        logger.error("Full scan failed for %s: %s", ctx.agent_id, e)
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# trigger_sync — public entry point for hooks
# ---------------------------------------------------------------------------

async def trigger_sync(ad, agent_id: str):
    """Public entry point for hooks to wake the sync loop."""
    ctx = ad._sync_contexts.get(agent_id)
    if not ctx:
        return
    ad.wake_sync(agent_id)
