"""Tests for sync_engine._promote_or_create_user_msg under the pre-sent
refactor.

The match pool is now restricted to sent-state DB rows (status=QUEUED,
jsonl_uuid IS NULL, delivered_at IS NULL) — the rows created when
dispatch_pending_message promotes a pre-sent entry to sent.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from models import (
    Agent,
    AgentMode,
    AgentStatus,
    Message,
    MessageRole,
    MessageStatus,
    Project,
)


def _fresh(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


@pytest.fixture()
def sync_env(db_engine, monkeypatch):
    """Wire display_writer + sync_engine to the test engine; stub WS."""
    from display_writer import (
        DISPLAY_DIR,
        _display_path,
        _pre_sent_index,
        _pre_sent_index_ready,
        _pre_sent_lock,
    )
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setattr("database.SessionLocal", Session)
    monkeypatch.setattr("display_writer.SessionLocal", Session)

    async def _noop_broadcast(*args, **kwargs):
        return 0
    monkeypatch.setattr("websocket.ws_manager.broadcast", _noop_broadcast)

    os.makedirs(DISPLAY_DIR, exist_ok=True)

    db = Session()
    try:
        db.add(Project(name="sync-proj", display_name="SP", path="/tmp/sp"))
        db.flush()
        agent_id = _fresh()
        db.add(Agent(
            id=agent_id,
            project="sync-proj",
            name="Sync Agent",
            mode=AgentMode.AUTO,
            status=AgentStatus.IDLE,
            model="claude-opus-4-7",
        ))
        db.commit()
    finally:
        db.close()

    ctx_obj = {"Session": Session, "agent_id": agent_id}
    yield ctx_obj

    # Teardown
    try:
        os.unlink(_display_path(agent_id))
    except FileNotFoundError:
        pass
    with _pre_sent_lock:
        _pre_sent_index.pop(agent_id, None)
        _pre_sent_index_ready.discard(agent_id)


def _mk_sync_context(agent_id: str):
    from sync_engine import SyncContext
    return SyncContext(
        agent_id=agent_id,
        session_id="sess-1",
        project_path="/tmp/sp",
        worktree=None,
        agent_name="Sync Agent",
        agent_project="sync-proj",
        jsonl_path="/tmp/sp/fake.jsonl",
    )


# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sync_matches_sent_row_and_marks_delivered(sync_env):
    """A sent-state DB row matching a JSONL user turn transitions to delivered."""
    from sync_engine import _promote_or_create_user_msg

    agent_id = sync_env["agent_id"]
    Session = sync_env["Session"]

    # Create a sent-state row (what dispatch_pending_message would leave behind).
    sent_msg_id = _fresh()
    db = Session()
    try:
        db.add(Message(
            id=sent_msg_id,
            agent_id=agent_id,
            role=MessageRole.USER,
            content="hello from web",
            status=MessageStatus.SENT,   # legacy enum for "sent"
            source="web",
            jsonl_uuid=None,
            delivered_at=None,
            display_seq=1,
            dispatch_seq=1,
        ))
        db.commit()
    finally:
        db.close()

    db = Session()
    try:
        ctx = _mk_sync_context(agent_id)
        deferred: list[str] = []
        result = _promote_or_create_user_msg(
            db, ctx,
            content="hello from web",
            jsonl_uuid="uuid-abc",
            seq=5,
            meta=None,
            kind=None,
            jsonl_ts=None,
            deferred_updates=deferred,
        )
        # Match hit → None returned; deferred list carries the id.
        assert result is None
        assert deferred == [sent_msg_id]
        db.commit()
    finally:
        db.close()

    # Verify post-commit state.
    db = Session()
    try:
        row = db.get(Message, sent_msg_id)
        assert row is not None
        assert row.status == MessageStatus.COMPLETED
        assert row.delivered_at is not None
        assert row.completed_at is not None
        assert row.jsonl_uuid == "uuid-abc"
        assert row.session_seq == 5
        # display_seq preserved (allocated at promote-to-sent time).
        assert row.display_seq == 1
    finally:
        db.close()


@pytest.mark.anyio
async def test_sync_creates_cli_on_no_match(sync_env):
    """With no sent candidates, a JSONL user turn creates a fresh CLI row."""
    from sync_engine import _promote_or_create_user_msg

    agent_id = sync_env["agent_id"]
    Session = sync_env["Session"]

    db = Session()
    try:
        ctx = _mk_sync_context(agent_id)
        deferred: list[str] = []
        msg = _promote_or_create_user_msg(
            db, ctx,
            content="typed in CLI",
            jsonl_uuid="uuid-cli",
            seq=3,
            meta=None,
            kind=None,
            jsonl_ts=None,
            deferred_updates=deferred,
        )
        # No match → returns a new Message instance for caller to insert.
        assert msg is not None
        assert isinstance(msg, Message)
        assert msg.source == "cli"
        assert msg.status == MessageStatus.COMPLETED
        assert msg.jsonl_uuid == "uuid-cli"
        assert msg.session_seq == 3
        assert deferred == []
    finally:
        db.close()


@pytest.mark.anyio
async def test_sync_skips_row_already_delivered(sync_env):
    """Rows with delivered_at already set are excluded from the match pool."""
    from sync_engine import _promote_or_create_user_msg

    agent_id = sync_env["agent_id"]
    Session = sync_env["Session"]

    already_delivered_id = _fresh()
    db = Session()
    try:
        now = datetime.now(timezone.utc)
        db.add(Message(
            id=already_delivered_id,
            agent_id=agent_id,
            role=MessageRole.USER,
            content="previously delivered",
            status=MessageStatus.SENT,
            source="web",
            jsonl_uuid=None,         # no uuid yet
            delivered_at=now,        # but delivered_at set
            display_seq=1,
        ))
        db.commit()
    finally:
        db.close()

    db = Session()
    try:
        ctx = _mk_sync_context(agent_id)
        deferred: list[str] = []
        msg = _promote_or_create_user_msg(
            db, ctx,
            content="previously delivered",
            jsonl_uuid="uuid-d",
            seq=1,
            meta=None,
            kind=None,
            jsonl_ts=None,
            deferred_updates=deferred,
        )
        # Should not match → creates a CLI row instead.
        assert msg is not None
        assert msg.source == "cli"
        assert deferred == []
    finally:
        db.close()


@pytest.mark.anyio
async def test_sync_uuid_dedup_skips_already_imported(sync_env):
    """UUID dedup path: a matching jsonl_uuid returns None with no insert."""
    from sync_engine import _promote_or_create_user_msg

    agent_id = sync_env["agent_id"]
    Session = sync_env["Session"]

    db = Session()
    try:
        db.add(Message(
            id=_fresh(),
            agent_id=agent_id,
            role=MessageRole.USER,
            content="already here",
            status=MessageStatus.COMPLETED,
            source="cli",
            jsonl_uuid="dup-uuid",
            delivered_at=datetime.now(timezone.utc),
        ))
        db.commit()
    finally:
        db.close()

    db = Session()
    try:
        ctx = _mk_sync_context(agent_id)
        deferred: list[str] = []
        result = _promote_or_create_user_msg(
            db, ctx,
            content="something else",
            jsonl_uuid="dup-uuid",
            seq=0,
            meta=None,
            kind=None,
            jsonl_ts=None,
            deferred_updates=deferred,
        )
        assert result is None
        assert deferred == []
    finally:
        db.close()


@pytest.mark.anyio
async def test_sync_rejects_unwhitelisted_source(sync_env):
    """A USER row with source NOT in the dedup whitelist must NOT be promoted.

    The whitelist (web, plan_continue, task) is the primary gate against
    false-positive promotions: ContentMatcher is purely content-based, so
    any unconfirmed candidate that happens to share content with an
    incoming JSONL turn would be promoted regardless of source. The
    `(jsonl_uuid IS NULL, delivered_at IS NULL)` pair narrows the pool
    but is not sufficient — hook + retry edge cases briefly leave both
    NULL with non-whitelisted sources. This test guards against future
    "simplifications" that drop the whitelist.
    """
    from sync_engine import _promote_or_create_user_msg

    agent_id = sync_env["agent_id"]
    Session = sync_env["Session"]

    # Simulate a tampered/buggy row with an unknown source but the same
    # NULL-state fingerprint as a legitimate pre_sent → SENT row.
    bogus_id = _fresh()
    db = Session()
    try:
        db.add(Message(
            id=bogus_id,
            agent_id=agent_id,
            role=MessageRole.USER,
            content="hello from somewhere",
            status=MessageStatus.SENT,
            source="unknown_source",  # NOT in (web, plan_continue, task)
            jsonl_uuid=None,
            delivered_at=None,
            display_seq=1,
            dispatch_seq=1,
        ))
        db.commit()
    finally:
        db.close()

    db = Session()
    try:
        ctx = _mk_sync_context(agent_id)
        deferred: list[str] = []
        msg = _promote_or_create_user_msg(
            db, ctx,
            content="hello from somewhere",   # same content
            jsonl_uuid="uuid-x",
            seq=7,
            meta=None,
            kind=None,
            jsonl_ts=None,
            deferred_updates=deferred,
        )
        # Whitelist must reject it — sync falls through to fresh CLI creation
        # rather than promoting the bogus row.
        assert msg is not None, "expected a fresh CLI row, got match-promote"
        assert msg.source == "cli"
        assert deferred == [], "bogus row must not be in deferred update list"
    finally:
        db.close()

    # And the bogus row must remain untouched (status, NULL fields).
    db = Session()
    try:
        row = db.get(Message, bogus_id)
        assert row.status == MessageStatus.SENT
        assert row.delivered_at is None
        assert row.jsonl_uuid is None
    finally:
        db.close()


@pytest.mark.anyio
async def test_sync_matches_sent_row_through_pasted_content_wrapper(sync_env):
    """CC 2.1.28x wraps tmux pastes in <pasted_content id=…>. The parser
    unwraps before content reaches here, but the matcher must also see
    through the wrapper on its own (raw callers / defence in depth) so the
    SENT row is promoted instead of a duplicate cli row being created."""
    from sync_engine import _promote_or_create_user_msg

    agent_id = sync_env["agent_id"]
    Session = sync_env["Session"]

    sent_msg_id = _fresh()
    db = Session()
    try:
        db.add(Message(
            id=sent_msg_id,
            agent_id=agent_id,
            role=MessageRole.USER,
            content="这个citation似乎不太对，@article{labs2025flux}",
            status=MessageStatus.SENT,
            source="web",
            jsonl_uuid=None,
            delivered_at=None,
            display_seq=1,
            dispatch_seq=1,
        ))
        db.commit()
    finally:
        db.close()

    wrapped = (
        "<pasted_content id=\"9671\">\n"
        "这个citation似乎不太对，@article{labs2025flux}\n"
        "</pasted_content id=\"9671\">"
    )
    db = Session()
    try:
        ctx = _mk_sync_context(agent_id)
        deferred: list[str] = []
        result = _promote_or_create_user_msg(
            db, ctx,
            content=wrapped,
            jsonl_uuid="uuid-wrapped",
            seq=7,
            meta=None,
            kind=None,
            jsonl_ts=None,
            deferred_updates=deferred,
        )
        assert result is None, "wrapped echo must promote, not create a cli duplicate"
        assert deferred == [sent_msg_id]
        db.commit()
    finally:
        db.close()

    db = Session()
    try:
        row = db.get(Message, sent_msg_id)
        assert row.status == MessageStatus.COMPLETED
        assert row.jsonl_uuid == "uuid-wrapped"
        assert row.session_seq == 7
        # No second USER row for this agent.
        n = db.query(Message).filter(
            Message.agent_id == agent_id, Message.role == MessageRole.USER,
        ).count()
        assert n == 1
    finally:
        db.close()
