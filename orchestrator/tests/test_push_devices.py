"""Per-device push notification control.

Each push subscription carries its own agents_enabled / tasks_enabled
switches; a client's bell sets only its own row. Covers the migration that
seeds the switches from the old global flags, per-channel recipient
filtering, the /subscribe, /device and /test routes, and pruning only on a
404/410 from the push service. pywebpush.webpush is always faked — nothing
here talks to a real push service.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import pywebpush
import requests
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import database
import push
from models import PushSubscription, SystemConfig
from notify import notify

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
MAC_SAFARI_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                 "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _Calls(list):
    """Recorded pushes; ``status[endpoint]`` makes that endpoint fail."""

    def __init__(self):
        super().__init__()
        self.status = {}


class _InlinePool:
    """Runs submitted pushes inline so tests can assert on them."""

    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture()
def sent(db_engine, monkeypatch):
    """Route push.py at the test DB and record every (faked) webpush call.

    Yields a list of {"endpoint", "payload"}; set ``sent.status[endpoint]``
    to make the push service answer that HTTP error for an endpoint.
    """
    import json

    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(push, "VAPID_PRIVATE_KEY", "test-private")
    monkeypatch.setattr(push, "VAPID_PUBLIC_KEY", "test-public")
    monkeypatch.setattr(push, "_push_pool", _InlinePool())

    calls = _Calls()

    def fake_webpush(subscription_info, data, **_kw):
        endpoint = subscription_info["endpoint"]
        status = calls.status.get(endpoint)
        if status == "network":
            raise requests.exceptions.ConnectionError("down")
        if status:
            raise pywebpush.WebPushException(f"HTTP {status}", response=_Resp(status))
        calls.append({"endpoint": endpoint, "payload": json.loads(data)})

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    return calls


def _add_sub(db, endpoint, *, agents=True, tasks=True, **kw):
    sub = PushSubscription(endpoint=endpoint, p256dh_key="p", auth_key="a",
                           agents_enabled=agents, tasks_enabled=tasks, **kw)
    db.add(sub)
    db.commit()
    return sub


def _endpoints(calls):
    return sorted(c["endpoint"] for c in calls)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def _legacy_db(tmp_path, monkeypatch, flags: dict):
    """A file DB with the pre-per-device push_subscriptions schema."""
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE push_subscriptions (id VARCHAR(12) PRIMARY KEY, "
            "endpoint VARCHAR(500) NOT NULL UNIQUE, p256dh_key VARCHAR(200) NOT NULL, "
            "auth_key VARCHAR(200) NOT NULL, created_at DATETIME, last_ack_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE system_config (key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL)"
        ))
        for i in (1, 2):
            conn.execute(text(
                "INSERT INTO push_subscriptions (id, endpoint, p256dh_key, auth_key) "
                "VALUES (:id, :ep, 'p', 'a')"
            ), {"id": f"sub{i}", "ep": f"https://push.example/{i}"})
        for key, value in flags.items():
            conn.execute(text("INSERT INTO system_config VALUES (:k, :v)"),
                         {"k": key, "v": value})
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    return engine


def _rows(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT id, agents_enabled, tasks_enabled, device_label, last_seen_at, "
            "unacked_sends FROM push_subscriptions ORDER BY id"
        )).fetchall()


def test_migration_copies_global_flags(tmp_path, monkeypatch):
    """A device muted through the old global switch stays muted."""
    engine = _legacy_db(tmp_path, monkeypatch,
                        {"notifications_agents_enabled": "0",
                         "notifications_tasks_enabled": "1"})
    database.init_db()
    assert [tuple(r) for r in _rows(engine)] == [
        ("sub1", 0, 1, None, None, 0),
        ("sub2", 0, 1, None, None, 0),
    ]


def test_migration_missing_flags_mean_enabled(tmp_path, monkeypatch):
    engine = _legacy_db(tmp_path, monkeypatch, {"notifications_tasks_enabled": "0"})
    database.init_db()
    assert [(r.agents_enabled, r.tasks_enabled) for r in _rows(engine)] == [(1, 0), (1, 0)]


def test_migration_seeds_only_once(tmp_path, monkeypatch):
    """Later startups leave per-device flags alone, whatever the globals say."""
    engine = _legacy_db(tmp_path, monkeypatch, {"notifications_agents_enabled": "0"})
    database.init_db()
    with engine.begin() as conn:
        conn.execute(text("UPDATE push_subscriptions SET agents_enabled = 1 WHERE id = 'sub1'"))
    database.init_db()
    assert [r.agents_enabled for r in _rows(engine)] == [1, 0]


def test_migration_failed_seed_is_retried(tmp_path, monkeypatch):
    """If the first startup dies between ALTER and seed (e.g. SQLITE_BUSY),
    the column must not survive unseeded — the next startup seeds it."""
    engine = _legacy_db(tmp_path, monkeypatch, {"notifications_agents_enabled": "0"})

    def _busy(conn, cursor, statement, *_):
        if statement.startswith("UPDATE push_subscriptions SET agents_enabled"):
            raise sqlite3.OperationalError("database is locked")

    event.listen(engine, "before_cursor_execute", _busy)
    with pytest.raises(Exception):
        database.init_db()
    event.remove(engine, "before_cursor_execute", _busy)
    with engine.connect() as conn:
        cols = database._table_columns(conn, "push_subscriptions")
    assert "agents_enabled" not in cols

    database.init_db()
    assert [r.agents_enabled for r in _rows(engine)] == [0, 0]


# ---------------------------------------------------------------------------
# Per-channel recipients
# ---------------------------------------------------------------------------

@pytest.fixture()
def three_devices(db_session):
    _add_sub(db_session, "https://push.example/all")
    _add_sub(db_session, "https://push.example/no-agents", agents=False)
    _add_sub(db_session, "https://push.example/no-tasks", tasks=False)


def test_message_goes_only_to_agents_enabled(sent, three_devices, caplog):
    caplog.set_level("INFO", logger="orchestrator.push")
    assert notify("message", "agent1", "Agent", "hello") == "SEND"
    assert _endpoints(sent) == ["https://push.example/all", "https://push.example/no-tasks"]
    assert "subs=2 (of 3) channel=message" in caplog.text


def test_task_complete_goes_only_to_tasks_enabled(sent, three_devices):
    notify("task_complete", "agent1", "Task done", "body", "/tasks/1")
    assert _endpoints(sent) == ["https://push.example/all", "https://push.example/no-agents"]


@pytest.mark.parametrize("channel", ["permission", "notify_at", "attention"])
def test_always_channels_reach_every_device(sent, three_devices, channel):
    notify(channel, "agent1", "Title", "body")
    assert len(sent) == 3


def test_global_flags_no_longer_gate(sent, three_devices, db_session):
    for key in ("notifications_agents_enabled", "notifications_tasks_enabled"):
        db_session.add(SystemConfig(key=key, value="0"))
    db_session.commit()
    notify("message", "agent1", "Agent", "hello")
    notify("task_complete", "agent1", "Task done", "body")
    assert len(sent) == 4


def test_message_muted_on_every_device_sends_nothing(sent, db_session, caplog):
    caplog.set_level("INFO", logger="orchestrator.push")
    _add_sub(db_session, "https://push.example/muted", agents=False)
    notify("message", "agent1", "Agent", "hello")
    assert sent == []
    assert "subs=0 (of 1) channel=message" in caplog.text


# ---------------------------------------------------------------------------
# /api/push/subscribe and /api/push/device
# ---------------------------------------------------------------------------

def _sub_body(endpoint="https://push.example/dev", p256dh="p1", auth="a1"):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


@pytest.mark.anyio
async def test_subscribe_returns_device_settings(client, db_session):
    resp = await client.post("/api/push/subscribe", json=_sub_body(),
                             headers={"user-agent": IPHONE_UA})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "subscribed",
        "created": True,
        "device": {"agents_enabled": True, "tasks_enabled": True, "device_label": "iPhone"},
    }
    sub = db_session.query(PushSubscription).one()
    assert sub.last_seen_at is not None

    resp = await client.post("/api/push/subscribe", json=_sub_body(),
                             headers={"user-agent": IPHONE_UA})
    assert resp.json()["created"] is False


@pytest.mark.anyio
async def test_rotated_endpoint_keeps_carried_mute(client, db_session, sent):
    """A muted device that comes back with a new endpoint (browser rotated
    it, or the old one 404/410'd) sends its last known switches; they seed
    the new row, so the mute is not silently lost."""
    await client.post("/api/push/subscribe", json=_sub_body("https://push.example/old"),
                      headers={"user-agent": IPHONE_UA})
    await client.post("/api/push/device", json={
        "endpoint": "https://push.example/old", "agents_enabled": False})
    resp = await client.post("/api/push/subscribe", headers={"user-agent": IPHONE_UA}, json={
        **_sub_body("https://push.example/new"), "agents_enabled": False, "tasks_enabled": True})
    assert resp.json()["created"] is True
    assert resp.json()["device"]["agents_enabled"] is False
    notify("message", "agent1", "Agent", "hello")
    assert sent == []


@pytest.mark.anyio
async def test_carried_flags_never_override_an_existing_row(client, db_session):
    await client.post("/api/push/subscribe", json=_sub_body())
    await client.post("/api/push/device", json={
        "endpoint": "https://push.example/dev", "agents_enabled": False})
    resp = await client.post("/api/push/subscribe", json={
        **_sub_body(), "agents_enabled": True, "tasks_enabled": False})
    assert resp.json()["device"]["agents_enabled"] is False
    assert resp.json()["device"]["tasks_enabled"] is True


@pytest.mark.anyio
async def test_carried_flags_must_be_booleans(client, db_session):
    resp = await client.post("/api/push/subscribe", json={
        **_sub_body(), "agents_enabled": "false", "tasks_enabled": None})
    assert resp.status_code == 200
    assert resp.json()["device"]["agents_enabled"] is True
    assert resp.json()["device"]["tasks_enabled"] is True


@pytest.mark.anyio
async def test_flags_survive_resubscribe(client, db_session):
    await client.post("/api/push/subscribe", json=_sub_body(), headers={"user-agent": MAC_SAFARI_UA})
    resp = await client.post("/api/push/device", json={
        "endpoint": "https://push.example/dev", "agents_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["device"] == {
        "agents_enabled": False, "tasks_enabled": True, "device_label": "Safari on Mac"}

    resp = await client.post("/api/push/subscribe", json=_sub_body(p256dh="p2", auth="a2"),
                             headers={"user-agent": MAC_SAFARI_UA})
    assert resp.json()["device"]["agents_enabled"] is False
    sub = db_session.query(PushSubscription).one()
    assert (sub.p256dh_key, sub.auth_key, sub.agents_enabled) == ("p2", "a2", False)


@pytest.mark.anyio
async def test_device_updates_only_that_device(client, db_session):
    _add_sub(db_session, "https://push.example/other")
    await client.post("/api/push/subscribe", json=_sub_body())
    resp = await client.post("/api/push/device", json={
        "endpoint": "https://push.example/dev", "tasks_enabled": False})
    assert resp.json()["device"]["tasks_enabled"] is False
    assert resp.json()["device"]["agents_enabled"] is True
    db_session.expire_all()
    other = db_session.query(PushSubscription).filter_by(endpoint="https://push.example/other").one()
    assert (other.agents_enabled, other.tasks_enabled) == (True, True)


@pytest.mark.anyio
async def test_device_unknown_endpoint_404(client):
    resp = await client.post("/api/push/device", json={
        "endpoint": "https://push.example/gone", "agents_enabled": False})
    assert resp.status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    {"agents_enabled": False},
    {"endpoint": "", "agents_enabled": False},
    {"endpoint": "https://push.example/dev", "agents_enabled": "false"},
])
async def test_device_rejects_bad_body(client, body):
    await client.post("/api/push/subscribe", json=_sub_body())
    resp = await client.post("/api/push/device", json=body)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/push/test
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_test_push_goes_to_that_device_only(client, db_session, sent):
    _add_sub(db_session, "https://push.example/other")
    _add_sub(db_session, "https://push.example/dev", agents=False, tasks=False)
    resp = await client.post("/api/push/test", json={"endpoint": "https://push.example/dev"})
    assert resp.status_code == 200
    assert _endpoints(sent) == ["https://push.example/dev"]
    assert sent[0]["payload"]["body"] == "Notifications are on for this device"


@pytest.mark.anyio
async def test_test_push_unknown_endpoint_404(client, sent):
    resp = await client.post("/api/push/test", json={"endpoint": "https://push.example/gone"})
    assert resp.status_code == 404
    assert sent == []


@pytest.mark.anyio
async def test_test_push_without_vapid_503(client, db_session, sent, monkeypatch):
    monkeypatch.setattr(push, "VAPID_PRIVATE_KEY", "")
    _add_sub(db_session, "https://push.example/dev")
    resp = await client.post("/api/push/test", json={"endpoint": "https://push.example/dev"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Pruning: only on 404/410 from the push service
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,deleted", [
    (404, True), (410, True), (400, False), (429, False), (500, False), ("network", False),
])
def test_prune_only_on_404_410(sent, db_session, status, deleted):
    _add_sub(db_session, "https://push.example/dev")
    sent.status["https://push.example/dev"] = status
    notify("permission", "agent1", "Title", "body")
    db_session.expire_all()
    remaining = db_session.query(PushSubscription).count()
    assert remaining == (0 if deleted else 1)


@pytest.mark.anyio
async def test_subscribe_never_prunes_silent_devices(client, db_session):
    """A device that hasn't acked for weeks (e.g. muted, so nothing was sent)
    survives other devices subscribing."""
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    _add_sub(db_session, "https://push.example/never-acked", agents=False, created_at=old)
    _add_sub(db_session, "https://push.example/acked-long-ago", created_at=old, last_ack_at=old)
    await client.post("/api/push/subscribe", json=_sub_body())
    assert db_session.query(PushSubscription).count() == 3


@pytest.mark.anyio
async def test_unacked_counter_counts_sends_and_resets_on_ack(client, db_session, sent):
    _add_sub(db_session, "https://push.example/dev")
    notify("permission", "agent1", "Title", "body")
    notify("notify_at", "", "Reminder", "body")
    db_session.expire_all()
    assert db_session.query(PushSubscription).one().unacked_sends == 2

    resp = await client.post("/api/push/ack", json={
        "nid": "abcd1234", "shown": True, "endpoint": "https://push.example/dev"})
    assert resp.status_code == 200
    db_session.expire_all()
    sub = db_session.query(PushSubscription).one()
    assert sub.unacked_sends == 0 and sub.last_ack_at is not None


def test_nothing_else_deletes_push_subscriptions():
    """Structural guard for the removed 14-day zombie prune (startup and
    /subscribe): a muted device gets no pushes, so it never acks, and must
    never be pruned for that.  The only code allowed to delete a
    push_subscriptions row is the 404/410 handler and /unsubscribe; a new
    deleter (e.g. startup housekeeping in main.lifespan, which ASGI tests
    never run) must be added here deliberately."""
    import ast
    import pathlib
    import re

    root = pathlib.Path(push.__file__).resolve().parent
    deleters = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] == "tests" or path.name.startswith("test_"):
            continue
        src = path.read_text()
        if "PushSubscription" not in src and "push_subscriptions" not in src:
            continue
        module = ".".join(rel.with_suffix("").parts)
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if ("PushSubscription" in seg and ".delete(" in seg) or \
                    re.search(r"DELETE\s+FROM\s+push_subscriptions", seg, re.I):
                deleters.add(f"{module}.{node.name}")
    assert deleters == {"push._send_one", "routers.push.push_unsubscribe"}
