"""Device ids: one session per device, and pushes that follow sign-in.

The client sends a random device id (kept across logout and expiry) with
every login and push registration. Covers: a login retiring the same
device's sessions (and, heuristically, idle ones with the same label from
the same IP), push subscriptions deleted when their device is signed out
but kept across a re-login or an idle lock, the per-device notification
state in the devices list, /subscribe keeping one endpoint per device, the
dead-subscription prune, and both schema migrations.
"""

import time
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

import auth_store
import database
import push
from models import PushSubscription
from tests.auth_helpers import LAN_IP, PASSWORD, call, enable_auth, make_auth_db
from tests.test_push_devices import _add_sub, _legacy_db, sent  # noqa: F401 (fixture)

IP_B = "100.64.0.11"
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Mobile/15E148 Safari/604.1")
DEV_A = "device-aaaaaaaa"
DEV_B = "device-bbbbbbbb"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    Session, engine = make_auth_db(tmp_path)
    enable_auth(monkeypatch, Session)
    yield Session
    engine.dispose()


@pytest.fixture()
def clock(monkeypatch):
    c = {"now": time.time()}
    monkeypatch.setattr(auth_store, "time", types.SimpleNamespace(
        time=lambda: c["now"], monotonic=time.monotonic))
    return c


def _dev(device_id):
    return [("x-device-id", device_id)] if device_id else []


async def _login(device_id=None, *, ip=LAN_IP, ua=IPHONE_UA, token=None):
    status, _, out = await call("POST", "/api/auth/login", json={"password": PASSWORD}, ip=ip,
                                token=token, headers=[("user-agent", ua), *_dev(device_id)])
    assert status == 200, out
    return out


def _row(Session, sid):
    db = Session()
    try:
        return db.get(auth_store.AuthSession, sid)
    finally:
        db.close()


def _subs(Session):
    db = Session()
    try:
        return {s.endpoint: s for s in db.query(PushSubscription).all()}
    finally:
        db.close()


def _sub_for(Session, device_id, endpoint=None, **kw):
    db = Session()
    try:
        return _add_sub(db, endpoint or f"https://push.example/{device_id}", device_id=device_id, **kw)
    finally:
        db.close()


async def _live_ids(token):
    status, _, listing = await call("GET", "/api/auth/sessions", token=token)
    assert status == 200
    return {r["id"] for r in listing["sessions"]}


# ---- one session per device ----

@pytest.mark.anyio
async def test_login_with_same_device_id_replaces_without_a_token(store):
    """The token was cleared (a 401, a sign-out) but the storage kept the id."""
    first = await _login(DEV_A)
    second = await _login(DEV_A)
    assert _row(store, first["session_id"]).revoked_reason == "replaced"
    assert _row(store, second["session_id"]).device_id == DEV_A
    assert await _live_ids(second["token"]) == {second["session_id"]}


@pytest.mark.anyio
async def test_other_devices_are_left_alone(store):
    a = await _login(DEV_A)
    b = await _login(DEV_B)  # same IP, same label, both active
    c = await _login()       # an older client without a device id
    assert await _live_ids(c["token"]) == {a["session_id"], b["session_id"], c["session_id"]}


@pytest.mark.anyio
async def test_wiped_storage_retires_the_idle_session_of_that_device(store, clock):
    """Same label from the same IP, and the old session is locked: the
    device's previous storage (e.g. the home-screen icon was re-added)."""
    old = await _login(DEV_A)
    _sub_for(store, DEV_A)
    clock["now"] += 31 * 60  # idle-locked
    new = await _login(DEV_B)
    assert _row(store, old["session_id"]).revoked_reason == "replaced"
    assert await _live_ids(new["token"]) == {new["session_id"]}
    # A heuristic match: its push subscription is left to the push service.
    assert "https://push.example/device-aaaaaaaa" in _subs(store)


@pytest.mark.anyio
async def test_heuristic_needs_idle_same_ip_and_same_label(store, clock):
    same = await _login(DEV_A)
    other_ip = await _login("device-cccccccc", ip=IP_B)
    other_label = await _login("device-dddddddd", ua="Mozilla/5.0 (Macintosh) Safari/605.1.15")
    clock["now"] += 31 * 60  # all three idle
    new = await _login(DEV_B, ip=LAN_IP)
    assert _row(store, same["session_id"]).revoked_reason == "replaced"
    assert _row(store, other_ip["session_id"]).revoked_at is None
    assert _row(store, other_label["session_id"]).revoked_at is None
    assert new["session_id"] in await _live_ids(new["token"])


@pytest.mark.anyio
async def test_active_session_with_same_label_and_ip_is_kept(store):
    a = await _login(DEV_A)
    b = await _login(DEV_B)
    assert _row(store, a["session_id"]).revoked_at is None
    assert await _live_ids(b["token"]) == {a["session_id"], b["session_id"]}


# ---- pushes follow sign-in ----

@pytest.mark.anyio
async def test_relogin_after_idle_keeps_the_push_subscription(store, clock):
    s = await _login(DEV_A)
    _sub_for(store, DEV_A)
    clock["now"] += 31 * 60
    # Locked, not signed out: still subscribed.
    assert "https://push.example/device-aaaaaaaa" in _subs(store)
    s2 = await _login(DEV_A, token=s["token"])
    assert _row(store, s["session_id"]).revoked_reason == "replaced"
    assert "https://push.example/device-aaaaaaaa" in _subs(store)
    assert s2["session_id"] != s["session_id"]


@pytest.mark.anyio
async def test_revoke_drops_only_that_devices_push(store):
    a = await _login(DEV_A)
    b = await _login(DEV_B, ip=IP_B)
    _sub_for(store, DEV_A)
    _sub_for(store, DEV_B)
    _sub_for(store, None, endpoint="https://push.example/unlinked")
    status, _, _ = await call("POST", f"/api/auth/sessions/{b['session_id']}/revoke", token=a["token"])
    assert status == 200
    assert set(_subs(store)) == {"https://push.example/device-aaaaaaaa", "https://push.example/unlinked"}


@pytest.mark.anyio
async def test_logout_drops_own_push(store):
    a = await _login(DEV_A)
    _sub_for(store, DEV_A)
    status, _, body = await call("POST", "/api/auth/logout", token=a["token"])
    assert status == 200 and body["revoked"] is True
    assert _subs(store) == {}


@pytest.mark.anyio
async def test_revoke_others_keeps_the_callers_push(store):
    a = await _login(DEV_A)
    await _login(DEV_B, ip=IP_B)
    _sub_for(store, DEV_A)
    _sub_for(store, DEV_B)
    status, _, body = await call("POST", "/api/auth/sessions/revoke-others", token=a["token"])
    assert status == 200 and body["revoked"] == 1
    assert set(_subs(store)) == {"https://push.example/device-aaaaaaaa"}


@pytest.mark.anyio
async def test_change_password_keeps_the_callers_device_and_push(store):
    a = await _login(DEV_A)
    await _login(DEV_B, ip=IP_B)
    _sub_for(store, DEV_A)
    _sub_for(store, DEV_B)
    status, _, body = await call(
        "POST", "/api/auth/change-password", token=a["token"],
        json={"current_password": PASSWORD, "new_password": "new-pass-123"})
    assert status == 200 and body["revoked_sessions"] == 2
    assert _row(store, body["session_id"]).device_id == DEV_A
    assert set(_subs(store)) == {"https://push.example/device-aaaaaaaa"}


# ---- devices list ----

@pytest.mark.anyio
async def test_sessions_list_reports_notification_state(store):
    on = await _login(DEV_A)
    muted = await _login(DEV_B, ip=IP_B)
    off = await _login("device-cccccccc", ip="100.64.0.12")
    legacy = await _login(ip="100.64.0.13")
    _sub_for(store, DEV_A)
    _sub_for(store, DEV_B, agents=False)
    status, _, listing = await call("GET", "/api/auth/sessions", token=on["token"])
    assert status == 200
    state = {r["id"]: r["notifications"] for r in listing["sessions"]}
    assert state == {on["session_id"]: "on", muted["session_id"]: "muted",
                     off["session_id"]: "off", legacy["session_id"]: None}
    assert all("device_id" not in r for r in listing["sessions"])


# ---- /api/push/subscribe ----

async def _subscribe(token, endpoint, device_id=None):
    return await call("POST", "/api/push/subscribe", token=token, headers=_dev(device_id),
                      json={"endpoint": endpoint, "keys": {"p256dh": "p1", "auth": "a1"}})


@pytest.mark.anyio
async def test_subscribe_keeps_one_endpoint_per_device(store):
    s = await _login(DEV_A)
    other = _sub_for(store, DEV_B)
    status, _, _ = await _subscribe(s["token"], "https://push.example/old", DEV_A)
    assert status == 200
    status, _, body = await _subscribe(s["token"], "https://push.example/new", DEV_A)
    assert status == 200 and body["created"] is True
    subs = _subs(store)
    assert set(subs) == {"https://push.example/new", other.endpoint}
    assert subs["https://push.example/new"].device_id == DEV_A


@pytest.mark.anyio
async def test_subscribe_rejects_a_malformed_device_id(store):
    s = await _login()
    status, _, _ = await _subscribe(s["token"], "https://push.example/x", "bad id!")
    assert status == 200
    assert _subs(store)["https://push.example/x"].device_id is None


@pytest.mark.anyio
async def test_subscribe_gives_an_older_session_its_device_id(store):
    s = await _login()  # signed in before device ids
    assert _row(store, s["session_id"]).device_id is None
    status, _, _ = await _subscribe(s["token"], "https://push.example/x", DEV_A)
    assert status == 200
    assert _row(store, s["session_id"]).device_id == DEV_A
    # ...but never overwrites one it already has.
    status, _, _ = await _subscribe(s["token"], "https://push.example/y", DEV_B)
    assert _row(store, s["session_id"]).device_id == DEV_A


# ---- dead subscriptions ----

def _utc(days_ago):
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)


def test_prune_needs_many_unacked_sends_and_weeks_of_silence(db_session):
    old = _utc(20)
    _add_sub(db_session, "https://push.example/dead", unacked_sends=25,
             created_at=old, last_ack_at=old, last_seen_at=old)
    _add_sub(db_session, "https://push.example/muted", agents=False, unacked_sends=0,
             created_at=old, last_ack_at=old, last_seen_at=old)
    _add_sub(db_session, "https://push.example/few-sends", unacked_sends=19,
             created_at=old, last_ack_at=old, last_seen_at=old)
    _add_sub(db_session, "https://push.example/in-use", unacked_sends=40,
             created_at=old, last_ack_at=old, last_seen_at=_utc(1))
    _add_sub(db_session, "https://push.example/acked", unacked_sends=40,
             created_at=old, last_ack_at=_utc(2), last_seen_at=old)
    _add_sub(db_session, "https://push.example/never-acked-new", unacked_sends=40,
             created_at=_utc(3))
    assert push.prune_dead_subscriptions(db_session) == 1
    left = {s.endpoint for s in db_session.query(PushSubscription).all()}
    assert left == {"https://push.example/muted", "https://push.example/few-sends",
                    "https://push.example/in-use", "https://push.example/acked",
                    "https://push.example/never-acked-new"}


def test_dead_subscription_is_pruned_before_a_send(sent, db_session):
    from notify import notify

    old = _utc(30)
    _add_sub(db_session, "https://push.example/dead", unacked_sends=30,
             created_at=old, last_ack_at=old, last_seen_at=old)
    _add_sub(db_session, "https://push.example/alive")
    notify("permission", "agent1", "Title", "body")
    assert [c["endpoint"] for c in sent] == ["https://push.example/alive"]
    db_session.expire_all()
    assert [s.endpoint for s in db_session.query(PushSubscription).all()] == ["https://push.example/alive"]


# ---- migrations ----

def test_auth_schema_adds_device_id_to_an_older_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE auth_sessions (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, "
            "device_label TEXT, user_agent TEXT, created_ip TEXT, last_ip TEXT, "
            "created_at REAL NOT NULL, last_seen_at REAL NOT NULL, last_active_at REAL NOT NULL, "
            "expires_at REAL NOT NULL, revoked_at REAL, revoked_reason TEXT)"))
        conn.execute(text(
            "INSERT INTO auth_sessions (id, token_hash, created_at, last_seen_at, last_active_at, "
            "expires_at) VALUES ('s1', 'h1', 1, 1, 1, 9e12)"))
    auth_store.ensure_auth_schema(engine)
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(auth_sessions)"))}
        row = conn.execute(text("SELECT id, device_id FROM auth_sessions")).one()
    assert "device_id" in cols and tuple(row) == ("s1", None)
    engine.dispose()


def test_init_db_adds_device_id_to_push_subscriptions(tmp_path, monkeypatch):
    engine = _legacy_db(tmp_path, monkeypatch, {})
    database.init_db()
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(push_subscriptions)"))}
        indexes = {r[1] for r in conn.execute(text("PRAGMA index_list(push_subscriptions)"))}
    assert "device_id" in cols and "ix_push_subscriptions_device_id" in indexes
    database.init_db()  # idempotent
