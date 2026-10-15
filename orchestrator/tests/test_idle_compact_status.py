"""Between-turns compaction (CC >= 2.1.300 idle compaction) must not strand
agents EXECUTING.

After ~54 min idle Claude Code compacts in the background with
trigger="auto" and no user turn: PreCompact(auto) -> SubagentStop(compaction
fork) -> SessionStart(source=compact) -> PostCompact(auto), and no Stop
follows. Its summary precompute fires PreCompact(auto) + the fork's
SubagentStop and nothing else. PreCompact flips the agent EXECUTING; the
idle hold in routers.hooks is what brings it back.

Contract under test:
  - IDLE before PreCompact(auto)      -> IDLE after PostCompact(auto), queued
                                         messages dispatched once IDLE
  - EXECUTING before PreCompact(auto) -> EXECUTING after PostCompact(auto)
    (mid-turn context-fill auto-compact; the turn's own Stop ends it)
  - manual /compact                   -> IDLE (unchanged)
  - a turn that starts during the window keeps EXECUTING
  - a PreCompact right after a turn hook is in-turn even if the DB says IDLE
  - no PostCompact (precompute / failed compaction) -> watchdog -> IDLE;
    only the compaction fork's stop (carries <summary>) shortens the wait
  - orchestrator restart mid-compaction -> JSONL decides
"""

import asyncio
import json
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from models import Agent, AgentMode, AgentStatus, Project

PANE = "%9701"
SID = "sess-idle-compact-0001"
FORK_ID = "ae5cc030fe27"  # compaction fork: never announced by SubagentStart
FORK_SUMMARY = "<analysis>...</analysis>\n<summary>\n1. Primary Request...\n</summary>"


def _fresh(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


class _FakeAd:
    """Only the AgentDispatcher surface the compact hooks touch."""

    def __init__(self):
        self._sync_contexts = {}
        self._known_subagents = {}
        self._drain_session_sync = AsyncMock(return_value=True)
        self.dispatch_pending_message = AsyncMock()
        self.wake_sync = MagicMock(return_value=True)
        self._ack_promote_on_user_prompt = MagicMock()
        self._rotate_agent_session = MagicMock(side_effect=self._rotate)

    def _emit(self, coro_or_dict):
        if hasattr(coro_or_dict, "close"):
            coro_or_dict.close()

    def _rotate(self, agent_id, new_sid, project_path, worktree=None):
        # Real rotation installs a FRESH SyncContext mid-window
        # (agent_dispatcher._sync_session_loop), so nothing stashed on the
        # old ctx at PreCompact survives to PostCompact. Mirror that; the
        # in-place rotation keeps the same JSONL file.
        from sync_engine import SyncContext
        old = self._sync_contexts.get(agent_id)
        self._sync_contexts[agent_id] = SyncContext(
            agent_id=agent_id, session_id=new_sid, project_path=project_path,
            jsonl_path=old.jsonl_path if old else "",
        )
        return True


@pytest.fixture()
async def _cancel_watchdogs():
    """Don't leave hold watchdogs sleeping on the loop after the test."""
    yield
    for task in asyncio.all_tasks():
        if task.get_coro().__qualname__ == "_idle_compact_watchdog":
            task.cancel()
    await asyncio.sleep(0)


@pytest.fixture()
def compact_env(db_engine, monkeypatch, tmp_path, _cancel_watchdogs):
    from main import app
    from sync_engine import SyncContext

    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    # hooks.py binds SessionLocal at import time -> patch the module attr.
    monkeypatch.setattr("routers.hooks.SessionLocal", Session)
    # Runtime `from database import SessionLocal` users:
    # _resolve_agent_for_hook, slash_commands.mark_delivered, emit_agent_update.
    monkeypatch.setattr("database.SessionLocal", Session)
    monkeypatch.setattr("display_writer.SessionLocal", Session)
    # PreCompact flushes the "Compacting context..." bubble to data/display.
    monkeypatch.setattr("display_writer.flush_agent", lambda _aid: None)

    async def _noop_broadcast(*args, **kwargs):
        return 0
    monkeypatch.setattr("websocket.ws_manager.broadcast", _noop_broadcast)

    async def _noop_context_usage(_aid):
        return None
    monkeypatch.setattr("websocket.emit_context_usage", _noop_context_usage)

    # The real flush wait times out at 10s; return instantly and record who
    # awaited it. "hook_agent_tool_activity" = the blocking wait inside the
    # PreCompact branch (stalls CC's hook); "_tool_flush_then_wake" = the
    # prelude's background wake that every tool-activity event schedules.
    # A test can set env["finalize_gate"] to park PostCompact's finalize.
    flush_calls = []
    env = {}

    async def _no_flush(_ad, aid, **kw):
        caller = sys._getframe(1).f_code.co_name
        flush_calls.append(caller)
        gate = env.get("finalize_gate")
        if gate is not None and caller == "_post_compact_finalize":
            await gate.wait()
        return False
    monkeypatch.setattr("routers.hooks._await_jsonl_flush", _no_flush)

    # Watchdogs: long by default so they never fire mid-test; the
    # no-PostCompact tests shorten them explicitly.
    monkeypatch.setattr("routers.hooks._IDLE_COMPACT_MAX_S", 30.0)
    monkeypatch.setattr("routers.hooks._IDLE_COMPACT_FORK_GRACE_S", 30.0)

    ad = _FakeAd()
    # ASGITransport does not run lifespan, so inject the dispatcher.
    monkeypatch.setattr(app.state, "agent_dispatcher", ad, raising=False)

    def status_of(aid: str) -> AgentStatus:
        db = Session()
        try:
            return db.get(Agent, aid).status
        finally:
            db.close()

    # Record the status each dispatch saw: dispatching while still
    # EXECUTING bails in production and strands the queued message.
    dispatched_at = []
    ad.dispatch_pending_message.side_effect = (
        lambda aid, delay=0: dispatched_at.append(status_of(aid)))

    def mk_agent(status: AgentStatus, jsonl_path: str = "") -> str:
        aid = _fresh()
        db = Session()
        try:
            db.add(Project(name="ic-proj", display_name="IC", path=str(tmp_path)))
            db.flush()
            db.add(Agent(
                id=aid, project="ic-proj", name="Idle Compact Agent",
                mode=AgentMode.AUTO, status=status, tmux_pane=PANE,
                session_id=SID, model="claude-opus-4-7",
            ))
            db.commit()
        finally:
            db.close()
        ad._sync_contexts[aid] = SyncContext(
            agent_id=aid, session_id=SID, project_path=str(tmp_path),
            jsonl_path=jsonl_path,
        )
        return aid

    env.update({"ad": ad, "mk_agent": mk_agent, "status_of": status_of,
                "flush_calls": flush_calls, "dispatched_at": dispatched_at})
    return env


async def _hook(client, path: str, body: dict):
    resp = await client.post(
        path, json={"session_id": SID, **body}, headers={"X-Tmux-Pane": PANE},
    )
    assert resp.status_code == 200
    return resp


async def _pre_compact(client, trigger: str):
    await _hook(client, "/api/hooks/agent-tool-activity", {
        "hook_event_name": "PreCompact", "trigger": trigger,
        "custom_instructions": None,
    })


async def _fork_subagent_stop(client):
    await _hook(client, "/api/hooks/agent-tool-activity", {
        "hook_event_name": "SubagentStop", "agent_id": FORK_ID,
        "agent_type": "", "last_assistant_message": FORK_SUMMARY,
    })


async def _agent_summary_stop(client):
    # CC's ~30s agent_summary fork for a running background agent.
    await _hook(client, "/api/hooks/agent-tool-activity", {
        "hook_event_name": "SubagentStop", "agent_id": "a1b2c3d4e5f60718",
        "agent_type": "", "last_assistant_message": "Reading test fixtures",
    })


async def _session_start_compact(client):
    await _hook(client, "/api/hooks/agent-session-start", {"source": "compact"})


async def _post_compact(client, trigger: str):
    await _hook(client, "/api/hooks/agent-post-compact", {
        "hook_event_name": "PostCompact", "trigger": trigger,
        "compact_summary": "summary",
    })


async def _user_prompt(client):
    await _hook(client, "/api/hooks/agent-user-prompt", {
        "hook_event_name": "UserPromptSubmit", "prompt": "hi",
    })


async def _stop(client):
    await _hook(client, "/api/hooks/agent-stop", {
        "hook_event_name": "Stop", "last_assistant_message": "done",
    })


async def _settle(seconds: float = 0.2):
    # PostCompact finalize and the watchdogs run in ensure_future'd tasks.
    for _ in range(int(seconds / 0.01)):
        await asyncio.sleep(0.01)


def _age_turn_hooks(aid: str):
    """Pretend the last turn hook fired long ago (idle compaction fires
    ~54 min after the turn ends)."""
    from routers import hooks
    if aid in hooks._last_turn_hook_at:
        hooks._last_turn_hook_at[aid] -= 3600


def _write_jsonl(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return str(path)


_BOUNDARY = {"type": "system", "subtype": "compact_boundary", "isSidechain": False,
             "content": "Conversation compacted", "compactMetadata": {"trigger": "auto"}}
_SUMMARY = {"type": "user", "isCompactSummary": True, "isSidechain": False,
            "message": {"role": "user", "content": "This session is being continued..."}}
_META = [{"type": "last-prompt"}, {"type": "ai-title"}, {"type": "mode"},
         {"type": "permission-mode"}]
_TURN_END = [
    {"type": "assistant", "isSidechain": False,
     "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
    {"type": "system", "subtype": "stop_hook_summary", "isSidechain": False},
    {"type": "system", "subtype": "turn_duration", "isSidechain": False},
]
_MID_TURN = [
    {"type": "user", "isSidechain": False,
     "message": {"role": "user", "content": "run the tests"}},
    {"type": "assistant", "isSidechain": False,
     "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]}},
    {"type": "user", "isSidechain": False,
     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
]


# ---- Hook sequences ----

@pytest.mark.anyio
async def test_idle_compaction_returns_agent_to_idle(client, compact_env):
    """Bug repro: IDLE -> idle compaction (real CC hook order) -> IDLE."""
    from routers import hooks
    aid = compact_env["mk_agent"](AgentStatus.IDLE)

    await _pre_compact(client, "auto")
    # Dispatch stays blocked while the pane is compacting.
    assert compact_env["status_of"](aid) == AgentStatus.EXECUTING
    assert aid in hooks._idle_compact_holds
    # Nothing in flight between turns: PreCompact must not stall CC on the
    # 10s generic flush wait.
    assert "hook_agent_tool_activity" not in compact_env["flush_calls"]

    await _fork_subagent_stop(client)
    await _session_start_compact(client)
    await _post_compact(client, "auto")
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.IDLE
    assert aid not in hooks._idle_compact_holds
    assert compact_env["dispatched_at"] == [AgentStatus.IDLE]


@pytest.mark.anyio
async def test_idle_compaction_after_earlier_turns(client, compact_env):
    """Same, for an agent whose last Stop is ~an hour old (the real case)."""
    aid = compact_env["mk_agent"](AgentStatus.EXECUTING)
    await _user_prompt(client)
    await _stop(client)
    db_status = compact_env["status_of"]
    # Sync flips IDLE from stop_hook_summary in production; do it directly.
    from routers import hooks
    hooks._flip_executing_to_idle(aid, "test: stop_hook_summary synced")
    assert db_status(aid) == AgentStatus.IDLE
    _age_turn_hooks(aid)

    await _pre_compact(client, "auto")
    await _fork_subagent_stop(client)
    await _session_start_compact(client)
    await _post_compact(client, "auto")
    await _settle()

    assert db_status(aid) == AgentStatus.IDLE
    assert compact_env["dispatched_at"] == [AgentStatus.IDLE]


@pytest.mark.anyio
async def test_midturn_auto_compact_keeps_executing(client, compact_env):
    """Context-fill auto-compact mid-turn: the original turn's Stop ends it."""
    from routers import hooks
    aid = compact_env["mk_agent"](AgentStatus.EXECUTING)

    await _pre_compact(client, "auto")
    assert aid not in hooks._idle_compact_holds
    # Mid-turn there may be un-synced turns: the blocking drain still runs.
    assert "hook_agent_tool_activity" in compact_env["flush_calls"]
    await _fork_subagent_stop(client)
    await _session_start_compact(client)
    await _post_compact(client, "auto")
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.EXECUTING
    assert compact_env["dispatched_at"] == []


@pytest.mark.anyio
async def test_manual_compact_returns_to_idle(client, compact_env):
    """Control: /compact from IDLE ends IDLE (manual path unchanged)."""
    from routers import hooks
    aid = compact_env["mk_agent"](AgentStatus.IDLE)

    await _pre_compact(client, "manual")
    await _post_compact(client, "manual")
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.IDLE
    assert aid not in hooks._idle_compact_holds
    assert compact_env["dispatched_at"] == [AgentStatus.IDLE]


@pytest.mark.anyio
async def test_compaction_right_after_turn_hook_is_in_turn(client, compact_env):
    """DB can read IDLE mid-turn (back-to-back turns, blocked-Stop
    continuation). A PreCompact seconds after a turn hook is in-turn: no
    hold, PostCompact(auto) keeps EXECUTING, the turn's Stop ends it."""
    from routers import hooks
    aid = compact_env["mk_agent"](AgentStatus.EXECUTING)
    await _stop(client)                      # blocked Stop: turn continues
    hooks._flip_executing_to_idle(aid, "test: stop_hook_summary synced")

    await _pre_compact(client, "auto")
    assert aid not in hooks._idle_compact_holds
    await _post_compact(client, "auto")
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.EXECUTING
    assert compact_env["dispatched_at"] == []


@pytest.mark.anyio
async def test_turn_started_during_idle_compaction_stays_executing(client, compact_env):
    """A prompt submitted during the window owns EXECUTING; PostCompact must
    not flip a live turn IDLE (dispatch would paste into a busy pane)."""
    from routers import hooks
    aid = compact_env["mk_agent"](AgentStatus.IDLE)

    await _pre_compact(client, "auto")
    await _user_prompt(client)
    assert aid not in hooks._idle_compact_holds
    await _post_compact(client, "auto")
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.EXECUTING
    assert compact_env["dispatched_at"] == []


@pytest.mark.anyio
async def test_turn_started_during_finalize_flush_stays_executing(client, compact_env):
    """USP lands while PostCompact's finalize waits on its JSONL flush (CC
    starts a prompt typed during compaction right after it ends)."""
    aid = compact_env["mk_agent"](AgentStatus.IDLE)
    gate = asyncio.Event()
    compact_env["finalize_gate"] = gate

    await _pre_compact(client, "auto")
    await _fork_subagent_stop(client)
    await _post_compact(client, "auto")
    await _settle(0.05)                 # finalize is now parked on the gate
    await _user_prompt(client)
    gate.set()
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.EXECUTING
    assert compact_env["dispatched_at"] == []


# ---- No PostCompact: watchdogs ----

@pytest.mark.anyio
async def test_precompute_without_post_compact_released_after_fork_grace(
        client, compact_env, monkeypatch):
    """Summary precompute: PreCompact + fork SubagentStop, no PostCompact."""
    from routers import hooks
    monkeypatch.setattr("routers.hooks._IDLE_COMPACT_FORK_GRACE_S", 0.05)
    aid = compact_env["mk_agent"](AgentStatus.IDLE)

    await _pre_compact(client, "auto")
    await _fork_subagent_stop(client)
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.IDLE
    assert aid not in hooks._idle_compact_holds
    assert compact_env["dispatched_at"] == [AgentStatus.IDLE]


@pytest.mark.anyio
async def test_unrelated_subagent_stop_does_not_release_hold(
        client, compact_env, monkeypatch):
    """A background agent's ~30s agent_summary fork stop is not the
    compaction fork: the hold must survive until PostCompact."""
    from routers import hooks
    monkeypatch.setattr("routers.hooks._IDLE_COMPACT_FORK_GRACE_S", 0.05)
    aid = compact_env["mk_agent"](AgentStatus.IDLE)

    await _pre_compact(client, "auto")
    await _agent_summary_stop(client)
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.EXECUTING
    assert aid in hooks._idle_compact_holds
    assert compact_env["dispatched_at"] == []

    await _fork_subagent_stop(client)
    await _session_start_compact(client)
    await _post_compact(client, "auto")
    await _settle()
    assert compact_env["status_of"](aid) == AgentStatus.IDLE
    assert compact_env["dispatched_at"] == [AgentStatus.IDLE]


@pytest.mark.anyio
async def test_compaction_without_any_follow_up_released_by_max_watchdog(
        client, compact_env, monkeypatch):
    """Blocked/failed compaction: only PreCompact arrives."""
    from routers import hooks
    monkeypatch.setattr("routers.hooks._IDLE_COMPACT_MAX_S", 0.05)
    aid = compact_env["mk_agent"](AgentStatus.IDLE)

    await _pre_compact(client, "auto")
    assert compact_env["status_of"](aid) == AgentStatus.EXECUTING
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.IDLE
    assert aid not in hooks._idle_compact_holds
    assert compact_env["dispatched_at"] == [AgentStatus.IDLE]


@pytest.mark.anyio
async def test_watchdog_leaves_turn_alone(client, compact_env, monkeypatch):
    """Watchdogs must not flip IDLE once a real turn took over."""
    monkeypatch.setattr("routers.hooks._IDLE_COMPACT_MAX_S", 0.05)
    monkeypatch.setattr("routers.hooks._IDLE_COMPACT_FORK_GRACE_S", 0.05)
    aid = compact_env["mk_agent"](AgentStatus.IDLE)

    await _pre_compact(client, "auto")
    await _fork_subagent_stop(client)
    await _user_prompt(client)
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.EXECUTING
    assert compact_env["dispatched_at"] == []


@pytest.mark.anyio
async def test_late_post_compact_after_watchdog_release_is_noop(
        client, compact_env, monkeypatch):
    """Watchdog released first; a late PostCompact(auto) must not re-flip or
    re-dispatch, and the agent stays IDLE (not stranded EXECUTING)."""
    monkeypatch.setattr("routers.hooks._IDLE_COMPACT_FORK_GRACE_S", 0.05)
    aid = compact_env["mk_agent"](AgentStatus.IDLE)

    await _pre_compact(client, "auto")
    await _fork_subagent_stop(client)
    await _settle()
    assert compact_env["status_of"](aid) == AgentStatus.IDLE
    assert compact_env["dispatched_at"] == [AgentStatus.IDLE]

    await _post_compact(client, "auto")
    await _settle()

    assert compact_env["status_of"](aid) == AgentStatus.IDLE
    assert compact_env["dispatched_at"] == [AgentStatus.IDLE]


# ---- Orchestrator restart mid-compaction: the JSONL decides ----

@pytest.mark.anyio
@pytest.mark.parametrize("before_boundary, expected", [
    (_TURN_END + _META, AgentStatus.IDLE),
    (_MID_TURN, AgentStatus.EXECUTING),
])
async def test_restart_mid_compaction_falls_back_to_jsonl(
        client, compact_env, tmp_path, before_boundary, expected):
    from routers import hooks
    path = _write_jsonl(tmp_path / "s.jsonl", before_boundary + [_BOUNDARY, _SUMMARY])
    start = AgentStatus.IDLE if expected == AgentStatus.IDLE else AgentStatus.EXECUTING
    aid = compact_env["mk_agent"](start, jsonl_path=path)

    await _pre_compact(client, "auto")
    hooks._idle_compact_holds.pop(aid, None)   # restart: in-memory hold lost
    assert aid not in hooks._last_turn_hook_at
    await _session_start_compact(client)
    await _post_compact(client, "auto")
    await _settle()

    assert compact_env["status_of"](aid) == expected


def test_jsonl_classifier(tmp_path):
    from routers.hooks import _jsonl_idle_before_last_compact as classify

    def run(entries):
        return classify(_write_jsonl(tmp_path / f"{_fresh()}.jsonl", entries))

    assert run(_TURN_END + _META + [_BOUNDARY, _SUMMARY]) is True
    assert run(_MID_TURN + [_BOUNDARY, _SUMMARY]) is False
    # A queued prompt (task notification, probe) after the last turn end:
    # the compaction opened the next turn.
    assert run(_TURN_END + [{"type": "queue-operation", "operation": "enqueue"},
                            _BOUNDARY, _SUMMARY]) is False
    # Only the latest boundary counts.
    assert run(_TURN_END + [_BOUNDARY, _SUMMARY] + _MID_TURN + [_BOUNDARY]) is False
    assert run(_TURN_END) is None
    assert classify(str(tmp_path / "missing.jsonl")) is None
