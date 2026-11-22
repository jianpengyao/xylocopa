"""Progress updates under thinking display "updates" become agent text rows.

With `thinking: {display: "updates"}` the reasoning stays an empty signed
thinking block and the model's short user-facing progress note arrives as a
separate non-empty thinking block (entry field thinkingDisplay="updates").
Those notes used to be text blocks; the parser now emits them as text turns
so the chat shows them as bubbles again. Sync imports only live ones —
history older than the newest imported agent row is skipped, since display
order is append-only and it would land at the bottom of the chat.
"""

import json
import os
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from models import (
    Agent,
    AgentMode,
    AgentStatus,
    Message,
    MessageRole,
)


def _fresh(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


def _asst(uid, block, ts, display="updates"):
    e = {"type": "assistant", "uuid": uid, "timestamp": ts, "sessionId": "s1",
         "message": {"id": f"msg_{uid}", "content": [block]}}
    if display:
        e["thinkingDisplay"] = display
    return e


def _think(text):
    return {"type": "thinking", "thinking": text, "signature": "sig"}


def _tool(tid, cmd="ls"):
    return {"type": "tool_use", "id": tid, "name": "Bash",
            "input": {"command": cmd, "description": f"run {cmd}"}}


def _text(text):
    return {"type": "text", "text": text}


def _result(uid, tid, ts):
    return {"type": "user", "uuid": uid, "timestamp": ts, "sessionId": "s1",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def _parse(entries):
    from jsonl_parser import parse_session_turns_from_lines
    return parse_session_turns_from_lines([json.dumps(e) + "\n" for e in entries])


# ---------------------------------------------------------------- parser


def test_update_block_becomes_text_turn_in_order():
    entries = [
        _asst("a1", _think(""), "2026-11-22T08:00:00.000Z"),
        _asst("a2", _think("Checking the queue next.\n\n"), "2026-11-22T08:00:01.000Z"),
        _asst("a3", _tool("t1"), "2026-11-22T08:00:02.000Z"),
        _result("r1", "t1", "2026-11-22T08:00:03.000Z"),
        _asst("a4", _text("Done."), "2026-11-22T08:00:04.000Z"),
    ]
    turns = _parse(entries)
    assert [(t[0], t[4]) for t in turns] == [
        ("assistant", "text"), ("assistant", "tool_use"), ("assistant", "text")]
    upd = turns[0]
    assert upd[1] == "Checking the queue next."
    assert upd[2] == {"thinking_update": True}
    assert upd[3] == "update-a2"
    assert upd[5] == "2026-11-22T08:00:01.000Z"
    assert turns[2][1] == "Done." and turns[2][2] is None


def test_text_before_update_is_flushed_as_its_own_turn():
    entries = [
        _asst("a1", _text("First part."), "2026-11-22T08:00:00.000Z"),
        _asst("a2", _think("Now running the tests."), "2026-11-22T08:00:01.000Z"),
        _asst("a3", _tool("t1"), "2026-11-22T08:00:02.000Z"),
    ]
    turns = _parse(entries)
    assert [t[1] for t in turns[:2]] == ["First part.", "Now running the tests."]
    assert turns[0][3] == "a1" and turns[1][3] == "update-a2"


def test_reasoning_thinking_is_not_shown():
    entries = [
        # Empty signed thinking under "updates": hidden reasoning.
        _asst("a1", _think(""), "2026-11-22T08:00:00.000Z"),
        # Non-empty thinking without the updates display (older sessions):
        # raw reasoning, must stay hidden.
        _asst("a2", _think("The user wants a changelog. Let me look."),
              "2026-05-20T19:54:05.000Z", display=None),
        _asst("a3", _tool("t1"), "2026-11-22T08:00:02.000Z"),
    ]
    turns = _parse(entries)
    assert [t[4] for t in turns] == ["tool_use"]


# ---------------------------------------------------------------- sync


@pytest.fixture()
def env(db_engine, monkeypatch, tmp_path):
    from display_writer import DISPLAY_DIR, _display_path
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("database.SessionLocal", Session)
    monkeypatch.setattr("display_writer.SessionLocal", Session)
    monkeypatch.setattr("sync_engine.SessionLocal", Session)

    async def _noop(*args, **kwargs):
        return 0
    monkeypatch.setattr("websocket.ws_manager.broadcast", _noop)
    os.makedirs(DISPLAY_DIR, exist_ok=True)

    from models import Project
    agent_id = _fresh()
    db = Session()
    try:
        db.add(Project(name="upd-proj", display_name="UP", path=str(tmp_path)))
        db.flush()
        db.add(Agent(id=agent_id, project="upd-proj", name="Upd Agent",
                     mode=AgentMode.AUTO, status=AgentStatus.EXECUTING,
                     model="claude-fable-5-2"))
        db.commit()
    finally:
        db.close()
    yield {"Session": Session, "agent_id": agent_id, "tmp_path": tmp_path}
    try:
        os.unlink(_display_path(agent_id))
    except FileNotFoundError:
        pass


def _ad():
    ad = MagicMock()
    ad._emit = lambda c: c.close() if hasattr(c, "close") else None
    ad._is_agent_in_use.return_value = True
    return ad


def _ctx(agent_id, jsonl, tmp_path):
    from sync_engine import SyncContext
    return SyncContext(agent_id=agent_id, session_id="s1", project_path=str(tmp_path),
                       worktree=None, agent_name="Upd Agent", agent_project="upd-proj",
                       jsonl_path=str(jsonl))


def _write(path, entries, mode="w"):
    with open(path, mode) as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _agent_rows(Session, agent_id):
    db = Session()
    try:
        return (db.query(Message)
                .filter(Message.agent_id == agent_id, Message.role == MessageRole.AGENT)
                .order_by(Message.display_seq).all())
    finally:
        db.close()


@pytest.mark.anyio
async def test_live_update_is_imported_as_text_bubble(env):
    from display_writer import _display_path
    from sync_engine import sync_import_new_turns

    Session, agent_id, tmp_path = env["Session"], env["agent_id"], env["tmp_path"]
    jsonl = tmp_path / "s1.jsonl"
    _write(jsonl, [
        _asst("a1", _think(""), "2026-11-22T08:00:00.000Z"),
        _asst("a2", _think("Submitting the smoke run."), "2026-11-22T08:00:01.000Z"),
        _asst("a3", _tool("t1"), "2026-11-22T08:00:02.000Z"),
    ])
    await sync_import_new_turns(_ad(), _ctx(agent_id, jsonl, tmp_path))

    rows = _agent_rows(Session, agent_id)
    assert [(r.kind, r.content) for r in rows][0] == ("text", "Submitting the smoke run.")
    assert rows[0].jsonl_uuid == "update-a2"
    assert rows[0].meta_json and json.loads(rows[0].meta_json) == {"thinking_update": True}
    assert rows[1].kind == "tool_use"
    with open(_display_path(agent_id)) as f:
        shown = [json.loads(l) for l in f if l.strip()]
    assert [l["content"] for l in shown][0] == "Submitting the smoke run."


@pytest.mark.anyio
async def test_history_update_skipped_live_one_imported_no_drift(env):
    """Upgrade path: rows were imported by the old parser (no update rows).
    The re-sliced old update is skipped; a new one after it is imported last;
    the full scan does not count the skipped one as drift."""
    from sync_engine import sync_full_scan, sync_import_new_turns

    Session, agent_id, tmp_path = env["Session"], env["agent_id"], env["tmp_path"]
    jsonl = tmp_path / "s1.jsonl"
    history = [
        _asst("a1", _think("Old progress note."), "2026-11-22T07:00:00.000Z"),
        _asst("a2", _tool("t1"), "2026-11-22T07:00:01.000Z"),
        _result("r1", "t1", "2026-11-22T07:00:02.000Z"),
        _asst("a3", _tool("t2", "pwd"), "2026-11-22T07:00:03.000Z"),
    ]
    _write(jsonl, history)
    ctx = _ctx(agent_id, jsonl, tmp_path)
    # Old parser's view: two tool rows already imported, pointer past them.
    db = Session()
    try:
        from sync_engine import _create_agent_msg
        from jsonl_parser import parse_session_turns
        for seq, t in enumerate(parse_session_turns(str(jsonl))):
            if t[4] == "tool_use":
                db.add(_create_agent_msg(db, ctx, t[1], t[3], seq, t[2],
                                         json.dumps(t[2]), t[4], t[5]))
        db.commit()
    finally:
        db.close()
    from display_writer import flush_agent
    flush_agent(agent_id)

    # Startup scan: the old update is missing from DB but exempt → no rewind.
    await sync_full_scan(_ad(), ctx, reason="startup")
    assert ctx.last_turn_count == 3

    # Simulate a pointer re-slice over history (oversized-JSONL tail scan).
    ctx.last_turn_count = 0
    _write(jsonl, [
        _result("r2", "t2", "2026-11-22T07:00:04.000Z"),
        _asst("a4", _think("New progress note."), "2026-11-22T07:00:05.000Z"),
        _asst("a5", _tool("t3", "date"), "2026-11-22T07:00:06.000Z"),
    ], mode="a")
    await sync_import_new_turns(_ad(), ctx)

    rows = _agent_rows(Session, agent_id)
    assert [r.content for r in rows if r.kind == "text"] == ["New progress note."]
    assert rows[-2].content == "New progress note."  # appended in order, before t3
    assert rows[-1].jsonl_uuid == "tool-t3"

    # Converged: a second scan finds nothing missing and moves to the end.
    await sync_full_scan(_ad(), ctx, reason="startup")
    assert ctx.last_turn_count == 5
