"""Shared pytest fixtures for Xylocopa backend tests."""

import os
import sys
import tempfile

# Ensure orchestrator package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Disable auth for all tests
os.environ["DISABLE_AUTH"] = "1"
# Use in-memory DB
os.environ["DB_PATH"] = ":memory:"
# Keep Dropbox sync state (token/config/state.db) out of the real data dir.
os.environ["DROPBOX_SYNC_DIR"] = tempfile.mkdtemp(prefix="xylocopa-dropbox-test-")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from models import Agent, AgentMode, AgentStatus, Base, Message, MessageRole, MessageStatus, Project, Task, TaskStatus


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_tmux_sessions():
    """Kill orphan ``xy-*`` tmux sessions spawned by this pytest run.

    Tests hit ``/api/v2/tasks/{id}/dispatch`` through the ASGI client, which
    runs production code that calls real ``tmux new-session``. Claude itself
    fails fast on ``/tmp/`` fixture dirs (not a git repo), but the wrapping
    bash shell + tmux session linger forever. Snapshot existing ``xy-*``
    sessions at startup and only kill ones that appeared during the run, so
    pre-existing production agents are never touched.
    """
    import subprocess as _sp

    def _list_xy_sessions() -> set[str]:
        try:
            result = _sp.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, _sp.SubprocessError):
            return set()
        if result.returncode != 0:
            return set()
        return {
            line.strip() for line in result.stdout.splitlines()
            if line.strip().startswith("xy-")
        }

    pre_existing = _list_xy_sessions()
    yield
    new_sessions = _list_xy_sessions() - pre_existing
    for name in new_sessions:
        _sp.run(["tmux", "kill-session", "-t", name],
                capture_output=True, timeout=5)


@pytest.fixture()
def db_engine():
    """Create an in-memory SQLite engine with all tables."""
    # StaticPool: one shared connection for every thread. Without it each
    # thread gets its own private in-memory database, so a handler that runs
    # in FastAPI's threadpool (plain `def` routes, asyncio.to_thread) sees
    # "no such table". Production uses a file DB and is unaffected.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Provide a transactional DB session scoped to each test."""
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
async def client(db_engine):
    """Create a test HTTP client with the DB dependency overridden.

    Async fixture because the AsyncClient must be closed under the same
    event loop that ran the test.  Without ``await c.aclose()``, the
    client is finalized by the GC later — under a closed event loop —
    which raises RuntimeError on connection cleanup and surfaces as
    spurious failures in tests that run after the affected one
    (observed: ``test_pre_sent_reader::test_pre_sent_endpoint_returns_
    index_snapshot`` in the full-suite run only).
    """
    from httpx import ASGITransport, AsyncClient
    from database import get_db
    from main import app

    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)

    def _override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    c = AsyncClient(transport=transport, base_url="http://test")
    try:
        yield c
    finally:
        await c.aclose()
        app.dependency_overrides.clear()


@pytest.fixture()
def sample_project(db_session):
    """Insert and return a sample project row."""
    proj = Project(
        name="test-project",
        display_name="Test Project",
        path="/tmp/test-project",
        max_concurrent=2,
        default_model="claude-opus-4-7",
    )
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj


@pytest.fixture()
def sample_agent(db_session, sample_project):
    """Insert and return a sample agent row."""
    agent = Agent(
        id="aaaa11112222",
        project=sample_project.name,
        name="Test agent prompt...",
        mode=AgentMode.AUTO,
        status=AgentStatus.IDLE,
        model="claude-opus-4-7",
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


@pytest.fixture()
def sample_task(db_session, sample_project):
    """Insert and return a sample v2 task in INBOX status."""
    task = Task(
        title="Fix the login page",
        description="The login page has a CSS bug",
        project_name=sample_project.name,
        status=TaskStatus.INBOX,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


@pytest.fixture()
def sample_message(db_session, sample_agent):
    """Insert and return a sample message."""
    msg = Message(
        agent_id=sample_agent.id,
        role=MessageRole.USER,
        content="Hello, agent!",
        status=MessageStatus.COMPLETED,
        source="web",
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)
    return msg


