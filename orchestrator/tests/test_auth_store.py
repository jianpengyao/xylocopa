"""auth_store: server-side sessions, legacy JWT window, media tokens, WS tickets.

Every call takes an injected clock (now=), so idle / expiry / throttling are
tested without sleeping. The auth tables are never created up front: the
store must create them lazily on whatever engine it is handed.
"""

import ast
import hashlib
import pathlib
import re
import time

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth_store
import config
from auth import create_token, get_config, get_jwt_secret, set_config
from auth_store import AuthResult, AuthSession

T0 = 1_800_000_000.0  # fake wall clock (2027-01-15)


def _engine():
    return create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                         poolclass=StaticPool)


@pytest.fixture()
def db(db_engine):
    """conftest's in-memory DB (models tables only) — no auth tables yet."""
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def updates(db_engine):
    """Count UPDATE statements against auth_sessions."""
    seen = []

    @event.listens_for(db_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE AUTH_SESSIONS"):
            seen.append(statement)

    yield seen
    event.remove(db_engine, "before_cursor_execute", _count)


def _session(db, now=T0, **kw):
    return auth_store.create_session(db, user_agent=kw.pop("ua", "pytest"), now=now, **kw)


def _fresh(db, session_id):
    """The row as stored (the store writes with core UPDATEs)."""
    db.expire_all()
    return db.get(AuthSession, session_id)


# ---- tokens and schema ----

def test_token_format_and_only_hash_stored(db):
    token, row = _session(db)
    assert re.fullmatch(r"xys_[A-Za-z0-9_-]{43}", token)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest() == auth_store.hash_token(token)
    assert re.fullmatch(r"[0-9a-f]{16}", row.id)
    dump = " ".join(str(v) for v in db.execute(text("SELECT * FROM auth_sessions")).one())
    assert token not in dump and token[4:] not in dump
    assert row.expires_at == T0 + config.AUTH_SESSION_MAX_DAYS * 86400
    assert row.created_at == row.last_seen_at == row.last_active_at == T0


def test_lazy_schema_on_bare_engine():
    engine = _engine()  # no tables at all — not even system_config
    s = sessionmaker(bind=engine)()
    try:
        token, row = auth_store.create_session(s, now=T0)
        names = set(inspect(engine).get_table_names())
        assert {"auth_sessions", "agent_hook_nonces", "system_config"} <= names
        assert {ix["name"] for ix in inspect(engine).get_indexes("auth_sessions")} >= {"ix_auth_sessions_live"}
        epoch = float(get_config(s, "auth_sessions_epoch"))
        assert abs(epoch - time.time()) < 60
        assert auth_store.authenticate(s, token, now=T0 + 1).ok
        # Ensured once per engine: a second ensure neither recreates nor
        # re-stamps the epoch.
        auth_store.ensure_auth_schema(engine)
        assert float(get_config(s, "auth_sessions_epoch")) == epoch
    finally:
        s.close()
        engine.dispose()


def test_ensure_keeps_existing_epoch():
    engine = _engine()
    s = sessionmaker(bind=engine)()
    try:
        from models import SystemConfig
        SystemConfig.__table__.create(bind=engine)
        set_config(s, "auth_sessions_epoch", "123.5")
        auth_store.ensure_auth_schema(engine)
        assert get_config(s, "auth_sessions_epoch") == "123.5"
    finally:
        s.close()
        engine.dispose()


def test_device_label():
    iphone = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
    mac_chrome = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
    win_edge = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0")
    linux_ff = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"
    android = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36")
    assert auth_store.derive_device_label(iphone) == "iPhone · Safari"
    assert auth_store.derive_device_label(mac_chrome) == "Mac · Chrome"
    assert auth_store.derive_device_label(win_edge) == "Windows · Edge"
    assert auth_store.derive_device_label(linux_ff) == "Linux · Firefox"
    assert auth_store.derive_device_label(android) == "Android · Chrome"
    assert auth_store.derive_device_label("") == "Unknown device"
    assert auth_store.clean_device_label("My\x00 iPhone\n‮") == "My iPhone"
    assert auth_store.clean_device_label("x" * 200) == "x" * 80
    assert auth_store.clean_device_label("\x07 ") is None
    assert auth_store.clean_device_label(42) is None


def test_device_label_from_client_or_user_agent(db):
    _, row = _session(db, device_label="Kitchen\tiPad")
    assert row.device_label == "KitcheniPad"
    _, row = _session(db, device_label=None, ua="Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) Safari/604.1")
    assert row.device_label == "iPad · Safari"
    _, row = _session(db, ua="x" * 1000)
    assert len(row.user_agent) == 300


# ---- session states ----

def test_authenticate_states(db):
    token, row = _session(db, ip="100.64.0.10")
    ok = auth_store.authenticate(db, token, now=T0 + 1)
    assert ok.ok and ok.kind == "session" and ok.session_id == row.id and ok.reason is None
    assert ok.token_hash == row.token_hash and ok.expires_at == row.expires_at

    assert auth_store.authenticate(db, None).reason == "missing"
    assert auth_store.authenticate(db, "").reason == "missing"
    assert auth_store.authenticate(db, "xys_" + "A" * 43).reason == "invalid"
    assert auth_store.authenticate(db, "garbage").reason == "invalid"
    assert auth_store.authenticate(db, "a.b").reason == "invalid"

    # expired: absolute cap, idle off so only expiry can fire
    auth_store.set_idle_timeout(db, 0)
    assert auth_store.authenticate(db, token, now=row.expires_at - 1).ok
    assert auth_store.authenticate(db, token, now=row.expires_at).reason == "expired"

    assert auth_store.revoke_session(db, row.id, "revoked", now=T0 + 2) is True
    assert auth_store.authenticate(db, token, now=T0 + 3).reason == "revoked"
    assert auth_store.revoke_session(db, row.id, "revoked") is False  # already revoked
    assert auth_store.revoke_session(db, "nope", "revoked") is False
    assert _fresh(db, row.id).revoked_reason == "revoked"


def test_idle_lock_and_activity(db):
    token, row = _session(db)
    timeout = auth_store.get_idle_timeout(db)
    assert timeout == config.AUTH_TIMEOUT_MINUTES == 30
    assert auth_store.authenticate(db, token, now=T0 + timeout * 60).ok  # boundary: not yet
    # Background calls (no activity) never extend the session.
    assert auth_store.authenticate(db, token, now=T0 + timeout * 60 + 1).reason == "idle"
    # Idle keeps the row: the session is locked, not revoked.
    assert _fresh(db, row.id).revoked_at is None
    assert auth_store.session_state(db, row.id, now=T0 + timeout * 60 + 1) == "idle"

    token2, row2 = _session(db)
    # Activity only counts on an authenticated, not-yet-idle request.
    t = T0 + 20 * 60
    assert auth_store.authenticate(db, token2, activity=True, now=t).ok
    assert auth_store.authenticate(db, token2, now=t + timeout * 60).ok
    assert auth_store.authenticate(db, token2, now=t + timeout * 60 + 1).reason == "idle"
    # An activity flag on an idle session does not unlock it.
    assert auth_store.authenticate(db, token2, activity=True, now=t + timeout * 60 + 2).reason == "idle"
    assert auth_store.authenticate(db, token2, now=t + timeout * 60 + 3).reason == "idle"


def test_idle_uses_pending_activity_between_throttled_writes(db, updates):
    token, row = _session(db)
    # Within ACTIVITY_WRITE_S of the session's creation: kept in memory only.
    assert auth_store.authenticate(db, token, activity=True, now=T0 + 10).ok
    assert updates == []
    assert _fresh(db, row.id).last_active_at == T0
    # The idle decision still sees the in-memory activity.
    assert auth_store.authenticate(db, token, now=T0 + 10 + 30 * 60).ok
    assert auth_store.authenticate(db, token, now=T0 + 10 + 30 * 60 + 1).reason == "idle"


def test_throttled_writes(db, updates):
    token, row = _session(db, ip="100.64.0.10")
    act = lambda t, **kw: auth_store.authenticate(db, token, activity=True, now=t, **kw)  # noqa: E731
    assert act(T0 + 5).ok and act(T0 + 14).ok
    assert updates == []  # creation counts as the last write of both
    assert act(T0 + 16).ok  # > ACTIVITY_WRITE_S since the last write
    assert len(updates) == 1
    assert _fresh(db, row.id).last_active_at == T0 + 16
    assert act(T0 + 20).ok and act(T0 + 30).ok
    assert len(updates) == 1
    assert act(T0 + 31).ok
    assert len(updates) == 2
    # last_seen / last_ip: at most every SEEN_WRITE_S, also without activity.
    assert auth_store.authenticate(db, token, ip="100.64.0.11", now=T0 + 50).ok
    assert len(updates) == 2
    assert auth_store.authenticate(db, token, ip="100.64.0.11", now=T0 + 61).ok
    assert len(updates) == 3
    fresh = _fresh(db, row.id)
    assert fresh.last_seen_at == T0 + 61 and fresh.last_ip == "100.64.0.11"
    assert fresh.last_active_at == T0 + 31  # a seen-only write never moves activity
    # Many background polls inside the window: no writes at all.
    for i in range(50):
        assert auth_store.authenticate(db, token, now=T0 + 62 + i * 0.5).ok
    assert len(updates) == 3


def test_reads_are_fresh_across_sessions(db_engine):
    """A revoke written by another DB session (the reset CLI, another
    request) applies to the very next authenticate."""
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    a, b = Session(), Session()
    try:
        token, row = _session(a)
        assert auth_store.authenticate(a, token, now=T0 + 1).ok
        auth_store.revoke_all(b, reason="reset", now=T0 + 2)
        assert auth_store.authenticate(a, token, now=T0 + 3).reason == "revoked"
    finally:
        a.close()
        b.close()


def test_clock_going_backwards_never_idle(db):
    token, row = _session(db)
    assert auth_store.authenticate(db, token, activity=True, now=T0 + 600).ok
    # Wall clock steps back an hour: now < last_active is never idle.
    assert auth_store.authenticate(db, token, now=T0 - 3600).ok
    assert auth_store.idle_remaining(db, row.id, now=T0 - 3600) == 30 * 60


def test_idle_off_and_settings_validation(db):
    token, row = _session(db)
    assert auth_store.set_idle_timeout(db, 0) == 0
    assert auth_store.get_idle_timeout(db) == 0  # cache refreshed at once
    assert auth_store.authenticate(db, token, now=T0 + 30 * 86400).ok
    assert auth_store.idle_remaining(db, row.id, now=T0 + 99) is None
    for bad in (7, -1, "30", 30.0, True, None):
        with pytest.raises(ValueError, match=r"must be one of \[0, 5, 15, 30, 60, 120, 240, 480, 1440\]"):
            auth_store.set_idle_timeout(db, bad)
    assert auth_store.set_idle_timeout(db, 5) == 5
    assert auth_store.idle_remaining(db, row.id, now=T0 + 60) == 240
    assert auth_store.authenticate(db, token, now=T0 + 5 * 60 + 1).reason == "idle"
    assert get_config(db, "auth_idle_timeout_minutes") == "5"


def test_idle_timeout_cache(db):
    assert auth_store.get_idle_timeout(db) == 30
    # A write behind the cache's back (the reset CLI) shows up within 5 s.
    set_config(db, "auth_idle_timeout_minutes", "60")
    assert auth_store.get_idle_timeout(db) == 30
    engine = db.get_bind()
    value, fetched = auth_store._idle_cache[engine]
    auth_store._idle_cache[engine] = (value, fetched - 6)
    assert auth_store.get_idle_timeout(db) == 60
    set_config(db, "auth_idle_timeout_minutes", "junk")
    auth_store._idle_cache.pop(engine)
    assert auth_store.get_idle_timeout(db) == config.AUTH_TIMEOUT_MINUTES


# ---- revoke / list ----

def test_revoke_all_and_list(db):
    tok_a, a = _session(db, now=T0, device_label="A")
    tok_b, b = _session(db, now=T0 + 1, device_label="B")
    tok_c, c = _session(db, now=T0 + 2, device_label="C")
    assert auth_store.authenticate(db, tok_a, now=T0 + 100).ok  # last_seen written (>60 s)
    rows = auth_store.list_sessions(db, current_id=b.id, now=T0 + 101)
    assert [r["id"] for r in rows] == [a.id, c.id, b.id]  # last_seen_at desc
    assert [r["current"] for r in rows] == [False, False, True]
    assert set(rows[0]) == {"id", "device_label", "user_agent", "created_at", "last_seen_at",
                            "last_active_at", "last_ip", "expires_at", "state", "current"}
    assert {r["state"] for r in rows} == {"active"}
    assert auth_store.list_sessions(db, now=T0 + 31 * 60)[0]["state"] == "idle"

    assert auth_store.revoke_all(db, except_id=b.id, reason="revoke-others", now=T0 + 200) == 2
    assert auth_store.authenticate(db, tok_a, now=T0 + 201).reason == "revoked"
    assert auth_store.authenticate(db, tok_b, now=T0 + 201).ok
    assert [r["id"] for r in auth_store.list_sessions(db, now=T0 + 201)] == [b.id]
    assert _fresh(db, a.id).revoked_reason == "revoke-others"
    assert auth_store.revoke_all(db, reason="password-change", now=T0 + 202) == 1
    assert auth_store.list_sessions(db, now=T0 + 203) == []


def test_revoke_token_and_lookup(db):
    token, row = _session(db)
    assert auth_store.lookup_session_id(db, token) == row.id
    assert auth_store.lookup_session_id(db, "xys_nope") is None
    assert auth_store.lookup_session_id(db, None) is None
    assert auth_store.revoke_token(db, token, "logout") is True
    assert auth_store.revoke_token(db, token, "logout") is False
    assert auth_store.revoke_token(db, "a.b", "logout") is False


def test_stale_rows_purged_on_new_session(db):
    _, old = _session(db, now=T0)
    auth_store.revoke_session(db, old.id, "logout", now=T0)
    _, kept = _session(db, now=T0 + 10)
    auth_store.revoke_session(db, kept.id, "logout", now=T0 + 10 * 86400)
    _session(db, now=T0 + 31 * 86400)
    ids = {r[0] for r in db.execute(text("SELECT id FROM auth_sessions"))}
    assert old.id not in ids and kept.id in ids


# ---- legacy JWTs ----

@pytest.fixture()
def legacy(db):
    """jwt secret + epoch anchored at T0 (the "first boot" of Step 3)."""
    auth_store.ensure_auth_schema(db.get_bind())
    set_config(db, "auth_sessions_epoch", repr(T0))
    return get_jwt_secret(db)


def test_legacy_jwt_window(db, legacy):
    tok = create_token(legacy, now=T0 - 3600)  # issued an hour before the epoch
    r = auth_store.authenticate(db, tok, now=T0)
    assert r.ok and r.kind == "legacy" and r.session_id is None
    assert r.legacy_exp == T0 - 3600 + 86400
    # Legacy sessions have no row: no idle check.
    assert auth_store.authenticate(db, tok, now=T0 + 20 * 3600).ok
    assert auth_store.authenticate(db, tok, now=T0 - 3600 + 86400 + 1).reason == "expired"

    # Issued at/after the epoch: new code never mints these → forged.
    assert auth_store.authenticate(db, create_token(legacy, now=T0), now=T0 + 1).reason == "invalid"
    # Lifetime reaching past the sunset window → invalid.
    long_lived = create_token(legacy, expires_minutes=24 * 60 + 5, now=T0 - 60)
    assert auth_store.authenticate(db, long_lived, now=T0).reason == "invalid"
    # Scoped tokens are never sessions.
    for scope in ("media", "preview"):
        scoped = create_token(legacy, extra_claims={"scope": scope}, now=T0 - 60)
        assert auth_store.authenticate(db, scoped, now=T0).reason == "invalid"
    # Wrong secret / garbage.
    assert auth_store.authenticate(db, create_token("other", now=T0 - 60), now=T0).reason == "invalid"
    assert auth_store.authenticate(db, tok[:-2] + "xx", now=T0).reason == "invalid"


def test_legacy_not_before(db, legacy):
    tok = create_token(legacy, now=T0 - 3600)
    assert auth_store.authenticate(db, tok, now=T0).ok
    auth_store.bump_not_before(db, now=T0 - 1800)
    assert auth_store.get_not_before(db) == T0 - 1800
    assert auth_store.authenticate(db, tok, now=T0).reason == "revoked"
    # Issued after the cut-off (but before the epoch): still fine.
    assert auth_store.authenticate(db, create_token(legacy, now=T0 - 900), now=T0).ok


def test_no_backend_code_mints_unscoped_tokens():
    """create_token without a scope would mint a legacy session JWT, which
    is only accepted inside the sunset window — new code must not."""
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] in ("tests", ".venv") or "site-packages" in rel.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "create_token":
                continue
            has_claims = len(node.args) >= 3 or any(k.arg == "extra_claims" for k in node.keywords)
            if not has_claims:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"create_token() without extra_claims: {offenders}"


# ---- media tokens ----

def test_media_token_follows_its_session(db, legacy):
    token, row = _session(db)
    r = auth_store.authenticate(db, token, now=T0 + 1)
    media, exp = auth_store.mint_media_token(db, r, now=T0 + 1)
    assert exp == T0 + 1 + config.MEDIA_TOKEN_TTL_MINUTES * 60
    m = auth_store.authenticate_media(db, media, now=T0 + 2)
    assert m.ok and m.kind == "media" and m.session_id == row.id
    # Never a Bearer credential.
    assert auth_store.authenticate(db, media, now=T0 + 2).reason == "invalid"
    # Session tokens / other scopes are not media tokens.
    assert auth_store.authenticate_media(db, token, now=T0 + 2).reason == "invalid"
    preview = create_token(legacy, extra_claims={"scope": "preview"}, now=T0)
    assert auth_store.authenticate_media(db, preview, now=T0 + 2).reason == "invalid"
    assert auth_store.authenticate_media(db, None).reason == "missing"
    # Idle session → media refused too, and checking media never bumps it.
    for i in range(10):
        assert auth_store.authenticate_media(db, media, now=T0 + 60 + i * 60).ok
    assert auth_store.authenticate_media(db, media, now=T0 + 1 + 30 * 60 + 1).reason == "idle"
    assert auth_store.authenticate(db, token, now=T0 + 1 + 30 * 60 + 1).reason == "idle"


def test_media_token_rejected_after_revoke_and_expiry(db, legacy):
    token, row = _session(db)
    r = auth_store.authenticate(db, token, now=T0)
    media, exp = auth_store.mint_media_token(db, r, now=T0)
    auth_store.set_idle_timeout(db, 0)
    assert auth_store.authenticate_media(db, media, now=exp - 1).ok
    assert auth_store.authenticate_media(db, media, now=exp + 1).reason == "expired"
    auth_store.revoke_session(db, row.id, "revoked", now=T0 + 5)
    assert auth_store.authenticate_media(db, media, now=T0 + 6).reason == "revoked"


def test_media_token_survives_not_before_while_its_session_lives(db, legacy):
    """revoke-others bumps auth_jwt_not_before to cut off legacy JWTs; the
    caller's own session-bound media token must keep working."""
    token, row = _session(db)
    media, _ = auth_store.mint_media_token(db, auth_store.authenticate(db, token, now=T0), now=T0)
    auth_store.bump_not_before(db, now=T0 + 10)
    assert auth_store.authenticate_media(db, media, now=T0 + 11).ok


def test_legacy_born_media_token(db, legacy):
    tok = create_token(legacy, now=T0 - 23 * 3600)  # one hour of life left
    r = auth_store.authenticate(db, tok, now=T0)
    media, exp = auth_store.mint_media_token(db, r, now=T0)
    assert exp == r.legacy_exp == T0 + 3600  # min(12 h, legacy exp)
    m = auth_store.authenticate_media(db, media, now=T0 + 1)
    assert m.ok and m.session_id is None
    assert auth_store.authenticate_media(db, media, now=T0 + 3601).reason == "expired"
    auth_store.bump_not_before(db, now=T0 + 2)
    assert auth_store.authenticate_media(db, media, now=T0 + 3).reason == "revoked"


# ---- WebSocket tickets ----

def test_ws_ticket_single_use_and_expiry(db):
    token, row = _session(db)
    r = auth_store.authenticate(db, token, now=T0)
    ticket, ttl = auth_store.issue_ws_ticket(r, now=T0)
    assert re.fullmatch(r"xyt_[A-Za-z0-9_-]{32}", ticket) and ttl == config.WS_TICKET_TTL_S == 30
    ctx = auth_store.consume_ws_ticket(ticket, now=T0 + 29)
    assert ctx is not None and ctx.kind == "session" and ctx.session_id == row.id
    assert auth_store.consume_ws_ticket(ticket, now=T0 + 29) is None  # single use
    late, _ = auth_store.issue_ws_ticket(r, now=T0)
    assert auth_store.consume_ws_ticket(late, now=T0 + 30) is None  # 30 s expiry
    assert auth_store.consume_ws_ticket(late, now=T0 + 1) is None  # popped even when late
    assert auth_store.consume_ws_ticket("xyt_unknown") is None
    assert auth_store.consume_ws_ticket("") is None


def test_ws_tickets_bounded_and_purged(db):
    token, _ = _session(db)
    r = auth_store.authenticate(db, token, now=T0)
    auth_store._ws_tickets.clear()
    first, _ = auth_store.issue_ws_ticket(r, now=T0)
    for _ in range(1000):
        auth_store.issue_ws_ticket(r, now=T0)
    assert len(auth_store._ws_tickets) == 1000
    assert auth_store.consume_ws_ticket(first, now=T0 + 1) is None  # oldest evicted
    auth_store.issue_ws_ticket(r, now=T0 + 31)  # expired ones purged on issue
    assert len(auth_store._ws_tickets) == 1
    auth_store._ws_tickets.clear()


def test_ws_ctx_state(db, legacy):
    token, row = _session(db)
    r = auth_store.authenticate(db, token, now=T0)
    ctx = auth_store.consume_ws_ticket(auth_store.issue_ws_ticket(r, now=T0)[0], now=T0)
    assert auth_store.ws_ctx_state(db, ctx, now=T0 + 60) is None
    assert auth_store.ws_ctx_state(db, ctx, now=T0 + 31 * 60) == "idle"
    auth_store.revoke_session(db, row.id, "revoked", now=T0 + 61)
    assert auth_store.ws_ctx_state(db, ctx, now=T0 + 62) == "revoked"

    lr = auth_store.authenticate(db, create_token(legacy, now=T0 - 3600), now=T0)
    lctx = auth_store.consume_ws_ticket(auth_store.issue_ws_ticket(lr, now=T0)[0], now=T0)
    assert lctx.kind == "legacy" and lctx.session_id is None
    assert auth_store.ws_ctx_state(db, lctx, now=T0 + 60) is None
    assert auth_store.ws_ctx_state(db, lctx, now=T0 - 3600 + 86401) == "expired"
    auth_store.bump_not_before(db, now=T0 + 100)
    assert auth_store.ws_ctx_state(db, lctx, now=T0 + 101) == "revoked"
    assert auth_store.ws_ctx_state(db, auth_store.WsAuthCtx(kind="open")) is None


def test_idle_remaining(db):
    token, row = _session(db)
    r = auth_store.authenticate(db, token, now=T0)
    assert auth_store.idle_remaining(db, r, now=T0 + 60) == 30 * 60 - 60
    assert auth_store.idle_remaining(db, row, now=T0 + 31 * 60) == 0
    assert auth_store.idle_remaining(db, AuthResult(ok=True, kind="legacy"), now=T0) is None
    assert auth_store.idle_remaining(db, None) is None
