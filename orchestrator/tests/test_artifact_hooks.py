"""PostToolUse for the Artifact tool re-indexes project assets
(routers/hooks.py hook_agent_tool_activity → asset_index.schedule_catch_up):
once at once, and once more after the existing JSONL flush wait."""

import asyncio
from types import SimpleNamespace

import pytest

AGENT_ID = "aaaa-bbbb-0001"


@pytest.fixture()
def hook_env(monkeypatch):
    import asset_index
    import main
    from routers import hooks

    monkeypatch.setattr(hooks, "_resolve_agent_for_hook", lambda req, body: AGENT_ID)
    monkeypatch.setattr(hooks, "_is_subprocess_session", lambda *a, **kw: False)

    async def _flushed(ad, agent_id, *a, **kw):
        return True

    monkeypatch.setattr(hooks, "_await_jsonl_flush", _flushed)

    async def _no_dropbox(aid, reason):
        return None

    monkeypatch.setattr(hooks, "_note_dropbox_activity", _no_dropbox)
    calls = []
    monkeypatch.setattr(asset_index, "schedule_catch_up", lambda: calls.append(1))
    ad = SimpleNamespace(wake_sync=lambda aid: None, _sync_contexts={}, _known_subagents={})
    monkeypatch.setattr(main.app.state, "agent_dispatcher", ad, raising=False)
    return SimpleNamespace(calls=calls, app=main.app)


async def _post(client, event, tool):
    resp = await client.post(
        "/api/hooks/agent-tool-activity",
        json={"hook_event_name": event, "tool_name": tool, "session_id": "sess-001",
              "tool_input": {"file_path": "out/page.html"}},
        headers={"X-Tmux-Pane": "%201"},
    )
    assert resp.status_code == 200
    await asyncio.sleep(0.05)  # let the background flush-then-wake task run


@pytest.mark.anyio
async def test_post_tool_use_artifact_schedules_twice(client, hook_env):
    await _post(client, "PostToolUse", "Artifact")
    assert hook_env.calls == [1, 1]  # immediate + after the flush wait


@pytest.mark.anyio
async def test_other_tools_and_failures_do_not_schedule(client, hook_env):
    await _post(client, "PostToolUse", "Read")
    await _post(client, "PostToolUseFailure", "Artifact")
    await _post(client, "PreToolUse", "Artifact")
    assert hook_env.calls == []


@pytest.mark.anyio
async def test_post_tool_use_artifact_without_dispatcher(client, hook_env, monkeypatch):
    monkeypatch.setattr(hook_env.app.state, "agent_dispatcher", None, raising=False)
    await _post(client, "PostToolUse", "Artifact")
    assert hook_env.calls == [1]
