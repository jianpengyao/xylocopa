"""sync_full_scan(reason="compact") must not purge rows it cannot see.

parse_session_turns tail-caps oversized JSONL files at MAX_AUDIT_FILE_SIZE,
so every turn before the window is absent from the scan. Treating those as
orphans deleted ~3.2k agent/system rows from a 151 MB session on
2026-09-21/22. The purge is only valid when the whole file was scanned.
"""

import json
import os
import uuid

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


def _user(uid: str, text: str) -> dict:
    return {"type": "user", "uuid": uid, "timestamp": "2026-09-22T10:00:00.000Z",
            "message": {"role": "user", "content": text}, "sessionId": "s1"}


def _assistant(uid: str, text: str) -> dict:
    return {"type": "assistant", "uuid": uid, "timestamp": "2026-09-22T10:00:01.000Z",
            "message": {"id": f"msg_{uid}", "content": [{"type": "text", "text": text}]},
            "sessionId": "s1"}


@pytest.fixture()
def scan_env(db_engine, monkeypatch, tmp_path):
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("database.SessionLocal", Session)
    monkeypatch.setattr("display_writer.SessionLocal", Session)
    monkeypatch.setattr("sync_engine.SessionLocal", Session)

    async def _noop_broadcast(*args, **kwargs):
        return 0
    monkeypatch.setattr("websocket.ws_manager.broadcast", _noop_broadcast)

    agent_id = _fresh()
    db = Session()
    try:
        db.add(Project(name="scan-proj", display_name="SP", path=str(tmp_path)))
        db.flush()
        db.add(Agent(id=agent_id, project="scan-proj", name="Scan Agent",
                     mode=AgentMode.AUTO, status=AgentStatus.IDLE,
                     model="claude-opus-4-7"))
        db.commit()
    finally:
        db.close()
    return {"Session": Session, "agent_id": agent_id, "tmp_path": tmp_path}


def _write(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _add_cli_agent_row(Session, agent_id, jsonl_uuid):
    db = Session()
    try:
        db.add(Message(id=_fresh(), agent_id=agent_id, role=MessageRole.AGENT,
                       content="an older assistant reply", status=MessageStatus.COMPLETED,
                       source="cli", jsonl_uuid=jsonl_uuid, session_seq=1))
        db.commit()
    finally:
        db.close()


def _count_rows(Session, agent_id, jsonl_uuid):
    db = Session()
    try:
        return db.query(Message).filter(
            Message.agent_id == agent_id, Message.jsonl_uuid == jsonl_uuid,
        ).count()
    finally:
        db.close()


def _ctx(agent_id, jsonl_path, tmp_path):
    from sync_engine import SyncContext
    return SyncContext(agent_id=agent_id, session_id="s1", project_path=str(tmp_path),
                       worktree=None, agent_name="Scan Agent", agent_project="scan-proj",
                       jsonl_path=str(jsonl_path))


@pytest.mark.anyio
async def test_compact_purge_skipped_when_scan_is_truncated(scan_env, monkeypatch):
    """Row whose uuid sits BEFORE the scanned tail window must survive."""
    import sync_engine
    from sync_engine import sync_full_scan

    Session, agent_id, tmp_path = scan_env["Session"], scan_env["agent_id"], scan_env["tmp_path"]
    old_uuid = "old-" + _fresh()
    jsonl = tmp_path / "s1.jsonl"
    # First line holds the old uuid; pad with big later turns so the file
    # exceeds the (monkeypatched) cap and the first line falls outside it.
    entries = [_assistant(old_uuid, "early reply")]
    entries += [_assistant("late-" + _fresh(), "x" * 500) for _ in range(6)]
    _write(jsonl, entries)
    size = os.path.getsize(jsonl)
    monkeypatch.setattr(sync_engine, "MAX_AUDIT_FILE_SIZE", size // 2)

    _add_cli_agent_row(Session, agent_id, old_uuid)
    assert _count_rows(Session, agent_id, old_uuid) == 1

    await sync_full_scan(None, _ctx(agent_id, jsonl, tmp_path), reason="compact")

    assert _count_rows(Session, agent_id, old_uuid) == 1, \
        "truncated compact scan must not purge rows outside its window"


@pytest.mark.anyio
async def test_compact_purge_still_runs_when_whole_file_scanned(scan_env, monkeypatch):
    """Control: a genuinely orphaned cli row is purged when the scan saw everything."""
    import sync_engine
    from sync_engine import sync_full_scan

    Session, agent_id, tmp_path = scan_env["Session"], scan_env["agent_id"], scan_env["tmp_path"]
    gone_uuid = "gone-" + _fresh()
    jsonl = tmp_path / "s1.jsonl"
    _write(jsonl, [_user("u-" + _fresh(), "hello"), _assistant("a-" + _fresh(), "reply")])
    monkeypatch.setattr(sync_engine, "MAX_AUDIT_FILE_SIZE", 50 * 1024 * 1024)

    _add_cli_agent_row(Session, agent_id, gone_uuid)
    assert _count_rows(Session, agent_id, gone_uuid) == 1

    await sync_full_scan(None, _ctx(agent_id, jsonl, tmp_path), reason="compact")

    assert _count_rows(Session, agent_id, gone_uuid) == 0
