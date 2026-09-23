"""Comprehensive tests for Agent API endpoints — listing, detail, update, stop, delete, read."""

from datetime import timedelta

import pytest

from models import Agent, AgentMode, AgentStatus, Message, MessageRole, MessageStatus, Project
from schemas import AgentOut
from utils import utcnow as _utcnow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(db_engine):
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    return Session()


def _seed_project(db, name="proj-full", display_name="Full", path="/tmp/full"):
    proj = Project(name=name, display_name=display_name, path=path)
    db.add(proj)
    db.commit()
    return proj


def _seed_agent(db, agent_id, project="proj-full", **kwargs):
    defaults = dict(
        name="Agent " + agent_id[:4],
        status=AgentStatus.IDLE,
        mode=AgentMode.AUTO,
    )
    defaults.update(kwargs)
    agent = Agent(id=agent_id, project=project, **defaults)
    db.add(agent)
    db.commit()
    return agent


# ===========================================================================
# Agent listing
# ===========================================================================

@pytest.mark.anyio
async def test_list_agents_filter_by_status(client, db_engine):
    """Filter agents by status query param (IDLE / EXECUTING)."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "idle11111111", status=AgentStatus.IDLE)
    _seed_agent(db, "exec11111111", status=AgentStatus.EXECUTING)
    _seed_agent(db, "idle22222222", status=AgentStatus.IDLE)
    db.close()

    resp = await client.get("/api/agents?status=IDLE")
    assert resp.status_code == 200
    data = resp.json()
    ids = [a["id"] for a in data]
    assert "idle11111111" in ids
    assert "idle22222222" in ids
    assert "exec11111111" not in ids

    resp2 = await client.get("/api/agents?status=EXECUTING")
    assert resp2.status_code == 200
    data2 = resp2.json()
    ids2 = [a["id"] for a in data2]
    assert "exec11111111" in ids2
    assert "idle11111111" not in ids2


@pytest.mark.anyio
async def test_list_agents_limit(client, db_engine):
    """Limit param should restrict number of returned agents."""
    db = _make_session(db_engine)
    _seed_project(db)
    for i in range(5):
        _seed_agent(db, f"lim{i:08d}", status=AgentStatus.IDLE)
    db.close()

    resp = await client.get("/api/agents?limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


@pytest.mark.anyio
async def test_list_agents_order_by_last_message(client, db_engine):
    """Agents with more recent last_message_at should appear first."""
    db = _make_session(db_engine)
    _seed_project(db)
    now = _utcnow()
    _seed_agent(db, "old111111111", status=AgentStatus.IDLE, last_message_at=now - timedelta(hours=2))
    _seed_agent(db, "new111111111", status=AgentStatus.IDLE, last_message_at=now)
    _seed_agent(db, "mid111111111", status=AgentStatus.IDLE, last_message_at=now - timedelta(hours=1))
    db.close()

    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    data = resp.json()
    ids = [a["id"] for a in data]
    assert ids.index("new111111111") < ids.index("mid111111111")
    assert ids.index("mid111111111") < ids.index("old111111111")


# ===========================================================================
# Agent detail
# ===========================================================================

@pytest.mark.anyio
async def test_get_agent_includes_subagents(client, db_engine):
    """Agent detail should include child subagents."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "parent111111", status=AgentStatus.IDLE)
    _seed_agent(db, "child1111111", status=AgentStatus.IDLE, parent_id="parent111111", is_subagent=True)
    _seed_agent(db, "child2222222", status=AgentStatus.IDLE, parent_id="parent111111", is_subagent=True)
    db.close()

    resp = await client.get("/api/agents/parent111111")
    assert resp.status_code == 200
    data = resp.json()
    assert data["subagents"] is not None
    sub_ids = [s["id"] for s in data["subagents"]]
    assert "child1111111" in sub_ids
    assert "child2222222" in sub_ids


@pytest.mark.anyio
async def test_get_agent_can_exclude_subagents(client, db_engine):
    """include_subagents=false omits the child list (chat-page poll payload)."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "parent222222", status=AgentStatus.IDLE)
    _seed_agent(db, "child3333333", status=AgentStatus.IDLE, parent_id="parent222222", is_subagent=True)
    db.close()

    resp = await client.get("/api/agents/parent222222?include_subagents=false")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "parent222222"
    assert data["subagents"] is None

    # Default is unchanged.
    resp = await client.get("/api/agents/parent222222")
    assert [s["id"] for s in resp.json()["subagents"]] == ["child3333333"]


@pytest.mark.anyio
async def test_get_agent_successor_id(client, db_engine):
    """Agent with a non-subagent child (successor) should show successor_id."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "predec111111", status=AgentStatus.STOPPED)
    _seed_agent(db, "succes111111", status=AgentStatus.IDLE, parent_id="predec111111", is_subagent=False)
    db.close()

    resp = await client.get("/api/agents/predec111111")
    assert resp.status_code == 200
    data = resp.json()
    assert data["successor_id"] == "succes111111"


# ===========================================================================
# Agent update
# ===========================================================================

@pytest.mark.anyio
async def test_update_agent_name(client, db_engine):
    """PUT /api/agents/{id} with name should update the agent name."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "upd111111111", name="Old Name")
    db.close()

    resp = await client.put("/api/agents/upd111111111", json={"name": "New Name"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "New Name"


@pytest.mark.anyio
async def test_update_agent_name_empty_rejected(client, db_engine):
    """PUT with empty name should return 400."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "upd222222222")
    db.close()

    resp = await client.put("/api/agents/upd222222222", json={"name": ""})
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_agent_name_too_long(client, db_engine):
    """PUT with name > 200 chars should return 400."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "upd333333333")
    db.close()

    long_name = "A" * 201
    resp = await client.put("/api/agents/upd333333333", json={"name": long_name})
    assert resp.status_code == 400
    assert "long" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_update_agent_muted(client, db_engine):
    """PUT with muted=true should update the muted flag."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "upd444444444", muted=False)
    db.close()

    resp = await client.put("/api/agents/upd444444444", json={"muted": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["muted"] is True

    # Verify toggling back works
    resp2 = await client.put("/api/agents/upd444444444", json={"muted": False})
    assert resp2.status_code == 200
    assert resp2.json()["muted"] is False


@pytest.mark.anyio
async def test_update_agent_not_found(client):
    """PUT on non-existent agent should return 404."""
    resp = await client.put("/api/agents/doesntexist1", json={"name": "X"})
    assert resp.status_code == 404


# ===========================================================================
# Agent stop (DELETE)
# ===========================================================================

@pytest.mark.anyio
async def test_stop_agent_success(client, db_engine):
    """DELETE /api/agents/{id} should mark agent as STOPPED."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "stop11111111", status=AgentStatus.IDLE)
    db.close()

    resp = await client.delete("/api/agents/stop11111111")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "STOPPED"
    assert data["tmux_pane"] is None


@pytest.mark.anyio
async def test_stop_agent_already_stopped(client, db_engine):
    """DELETE on already STOPPED agent should return 400."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "stop22222222", status=AgentStatus.STOPPED)
    db.close()

    resp = await client.delete("/api/agents/stop22222222")
    assert resp.status_code == 400
    assert "already stopped" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_stop_agent_cascades_messages(client, db_engine):
    """EXECUTING messages should become FAILED when agent is stopped."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "stop33333333", status=AgentStatus.EXECUTING)
    db.add(Message(
        id="execmsg11111",
        agent_id="stop33333333",
        role=MessageRole.USER,
        content="Running task",
        status=MessageStatus.EXECUTING,
    ))
    db.add(Message(
        id="compmsg11111",
        agent_id="stop33333333",
        role=MessageRole.AGENT,
        content="Done earlier",
        status=MessageStatus.COMPLETED,
    ))
    db.commit()
    db.close()

    resp = await client.delete("/api/agents/stop33333333")
    assert resp.status_code == 200

    # Verify the EXECUTING message is now FAILED
    db = _make_session(db_engine)
    exec_msg = db.get(Message, "execmsg11111")
    assert exec_msg.status == MessageStatus.FAILED
    assert exec_msg.error_message == "Agent stopped by user"
    # COMPLETED message should be unchanged
    comp_msg = db.get(Message, "compmsg11111")
    assert comp_msg.status == MessageStatus.COMPLETED
    db.close()


@pytest.mark.anyio
async def test_stop_agent_creates_system_message(client, db_engine):
    """Stopping an agent should create a system message 'Agent stopped'."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "stop44444444", status=AgentStatus.IDLE)
    db.close()

    resp = await client.delete("/api/agents/stop44444444")
    assert resp.status_code == 200

    db = _make_session(db_engine)
    system_msgs = (
        db.query(Message)
        .filter(
            Message.agent_id == "stop44444444",
            Message.role == MessageRole.SYSTEM,
        )
        .all()
    )
    assert len(system_msgs) >= 1
    assert any(m.content == "Agent stopped" for m in system_msgs)
    db.close()


@pytest.mark.anyio
async def test_stop_agent_not_found(client):
    """DELETE on non-existent agent should return 404."""
    resp = await client.delete("/api/agents/doesntexist1")
    assert resp.status_code == 404


# ===========================================================================
# Agent permanent delete
# ===========================================================================

@pytest.mark.anyio
async def test_permanent_delete_agent_not_stopped(client, db_engine):
    """Permanent delete should return 400 if agent is not STOPPED or ERROR."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "perm11111111", status=AgentStatus.IDLE)
    db.close()

    resp = await client.delete("/api/agents/perm11111111/permanent")
    assert resp.status_code == 400
    assert "stopped" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_permanent_delete_success(client, db_engine):
    """Permanent delete of a STOPPED agent should remove agent and messages."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "perm22222222", status=AgentStatus.STOPPED)
    for i in range(3):
        db.add(Message(
            agent_id="perm22222222",
            role=MessageRole.AGENT,
            content=f"Message {i}",
            status=MessageStatus.COMPLETED,
        ))
    db.commit()
    db.close()

    resp = await client.delete("/api/agents/perm22222222/permanent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["detail"] == "ok"
    assert data["deleted_messages"] == 3

    # Verify agent is gone
    db = _make_session(db_engine)
    assert db.get(Agent, "perm22222222") is None
    remaining = db.query(Message).filter(Message.agent_id == "perm22222222").count()
    assert remaining == 0
    db.close()


@pytest.mark.anyio
async def test_permanent_delete_not_found(client):
    """Permanent delete on non-existent agent should return 404."""
    resp = await client.delete("/api/agents/doesntexist1/permanent")
    assert resp.status_code == 404


# ===========================================================================
# Read operations
# ===========================================================================

@pytest.mark.anyio
async def test_mark_agent_read(client, db_engine):
    """PUT /api/agents/{id}/read should reset unread_count to 0."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "read11111111", status=AgentStatus.IDLE, unread_count=5)
    db.close()

    resp = await client.put("/api/agents/read11111111/read")
    assert resp.status_code == 200
    assert resp.json()["detail"] == "ok"

    # Verify in DB
    db = _make_session(db_engine)
    agent = db.get(Agent, "read11111111")
    assert agent.unread_count == 0
    db.close()


@pytest.mark.anyio
async def test_mark_all_agents_read(client, db_engine):
    """PUT /api/agents/read-all should reset unread_count for all agents."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "rall11111111", status=AgentStatus.IDLE, unread_count=3)
    _seed_agent(db, "rall22222222", status=AgentStatus.IDLE, unread_count=7)
    _seed_agent(db, "rall33333333", status=AgentStatus.IDLE, unread_count=0)
    db.close()

    resp = await client.put("/api/agents/read-all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["detail"] == "ok"
    assert data["updated"] == 2  # only agents with unread_count > 0

    # Verify all zeroed
    db = _make_session(db_engine)
    for aid in ("rall11111111", "rall22222222", "rall33333333"):
        assert db.get(Agent, aid).unread_count == 0
    db.close()


# ===========================================================================
# Unread count
# ===========================================================================

@pytest.mark.anyio
async def test_agents_unread_count(client, db_engine):
    """GET /api/agents/unread should return correct total unread count."""
    db = _make_session(db_engine)
    _seed_project(db)
    _seed_agent(db, "unrd11111111", status=AgentStatus.IDLE, unread_count=4)
    _seed_agent(db, "unrd22222222", status=AgentStatus.IDLE, unread_count=6)
    _seed_agent(db, "unrd33333333", status=AgentStatus.IDLE, unread_count=0)
    # Subagent unread should NOT be counted (endpoint filters is_subagent=False)
    _seed_agent(db, "unrdsub11111", status=AgentStatus.IDLE, unread_count=100, is_subagent=True, parent_id="unrd11111111")
    db.close()

    resp = await client.get("/api/agents/unread")
    assert resp.status_code == 200
    data = resp.json()
    # Only top-level agents: 4 + 6 + 0 = 10
    assert data["unread"] == 10
