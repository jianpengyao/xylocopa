"""Tests for routers/dropbox.py — ASGI client tests."""

import json
import os
import time

import httpx
import pytest

from tests.dropbox_fake import FakeDropboxServer
from dropbox_sync.auth import TokenStore
from dropbox_sync.client import DropboxClient, Throttle


# ── Helpers ──────────────────────────────────────────────────────────


def _seed_token(sync_dir: str) -> None:
    """Write a valid token.json into the sync dir."""
    token_path = os.path.join(sync_dir, "token.json")
    os.makedirs(sync_dir, mode=0o700, exist_ok=True)
    data = {
        "app_key": "testkey1234",
        "refresh_token": "rt_test",
        "access_token": "sl.test_access",
        "expires_at": time.time() + 36000,
        "account_id": "dbid:test123",
        "account_name": "Test User",
        "email": "test@example.com",
        "scope": "files.content.write",
        "linked_at": "2026-01-01T00:00:00Z",
    }
    with open(token_path, "w") as f:
        json.dump(data, f)
    os.chmod(token_path, 0o600)


async def _instant_sleep(delay):
    pass


def _make_client(fake: FakeDropboxServer) -> DropboxClient:
    http = httpx.AsyncClient(transport=fake.transport())

    class _FakeTP:
        async def get_access_token(self, force_refresh=False):
            return "sl.fake"

    return DropboxClient(_FakeTP(), http=http, throttle=Throttle(0), sleep=_instant_sleep)


@pytest.fixture()
def sync_dir(tmp_path):
    """Create a fresh sync dir and reset the engine."""
    sd = str(tmp_path / "dropbox")
    os.makedirs(sd, mode=0o700, exist_ok=True)
    from dropbox_sync import engine
    engine.reset_for_tests(sd)
    yield sd
    engine.reset_for_tests(sd)


@pytest.fixture()
def fake():
    return FakeDropboxServer()


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_status_not_linked(client, sync_dir):
    """GET /api/dropbox/status when not linked."""
    resp = await client.get("/api/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["linked"] is False
    assert data["account"] is None
    assert data["space"] is None
    assert isinstance(data["config"], dict)
    assert isinstance(data["queue"], list)


@pytest.mark.anyio
async def test_status_linked(client, sync_dir):
    """GET /api/dropbox/status when linked."""
    _seed_token(sync_dir)
    resp = await client.get("/api/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["linked"] is True
    assert data["account"]["email"] == "test@example.com"


@pytest.mark.anyio
async def test_link_start_validation(client, sync_dir):
    """POST /api/dropbox/link/start with invalid key returns 400."""
    resp = await client.post("/api/dropbox/link/start", json={"app_key": "short"})
    assert resp.status_code == 400

    # An empty app_key means "use the project's shipped key" (relay mode).
    resp2 = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": ""},
        headers={"Origin": "https://localhost:3000"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["mode"] == "relay"


@pytest.mark.anyio
async def test_link_start_409_when_linked(client, sync_dir):
    """POST /api/dropbox/link/start returns 409 when already linked."""
    _seed_token(sync_dir)
    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "abcdefghij1234"},
    )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_link_complete_via_fake(client, sync_dir, fake):
    """Full link flow: start -> complete via FakeDropboxServer."""
    from dropbox_sync import engine

    # Set up fake OAuth
    fake.register_oauth_code("test_auth_code", {
        "access_token": "sl.linked_token",
        "token_type": "bearer",
        "expires_in": 14400,
        "refresh_token": "rt_linked",
        "scope": "files.content.write account_info.read",
    })

    # Override the link flow to use the fake's HTTP transport
    fake_http = httpx.AsyncClient(transport=fake.transport())
    from dropbox_sync.auth import LinkFlow
    store = engine.get_token_store()
    flow = LinkFlow(store, http=fake_http)
    engine._link_flow = flow

    # Start
    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "abcdefghij1234"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "authorize_url" in data
    assert "state" in data

    # Complete
    resp2 = await client.post(
        "/api/dropbox/link/complete",
        json={"code": "test_auth_code"},
    )
    assert resp2.status_code == 200
    result = resp2.json()
    assert result["detail"] == "ok"
    assert result["account"]["email"] == "test@example.com"

    # Now linked
    assert store.is_linked

    await fake_http.aclose()


@pytest.mark.anyio
async def test_config_put_validation(client, sync_dir):
    """PUT /api/dropbox/config validates chunk_mb."""
    # chunk_mb must be multiple of 4
    resp = await client.put("/api/dropbox/config", json={"chunk_mb": 5})
    assert resp.status_code == 400

    resp2 = await client.put("/api/dropbox/config", json={"chunk_mb": 148})
    assert resp2.status_code == 400

    # Valid
    resp3 = await client.put("/api/dropbox/config", json={"chunk_mb": 8})
    assert resp3.status_code == 200
    assert resp3.json()["chunk_mb"] == 8

    # interval_minutes < 0
    resp4 = await client.put("/api/dropbox/config", json={"interval_minutes": -1})
    assert resp4.status_code == 400


@pytest.mark.anyio
async def test_sync_409_not_linked(client, sync_dir):
    """POST /api/dropbox/sync returns 409 when not linked."""
    resp = await client.post("/api/dropbox/sync", json={})
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_sync_404_unknown_project(client, sync_dir):
    """POST /api/dropbox/sync with unknown project returns 404."""
    _seed_token(sync_dir)
    resp = await client.post("/api/dropbox/sync", json={"project": "nonexistent"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_sync_400_not_enabled(client, sync_dir, db_session):
    """POST /api/dropbox/sync with non-enabled project returns 400."""
    _seed_token(sync_dir)

    from models import Project
    proj = Project(
        name="notsync",
        display_name="Not Sync",
        path="/tmp/notsync",
        max_concurrent=2,
        default_model="claude-opus-4-7",
        dropbox_sync=False,
    )
    db_session.add(proj)
    db_session.commit()

    resp = await client.post("/api/dropbox/sync", json={"project": "notsync"})
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_pause_resume(client, sync_dir):
    """POST /api/dropbox/pause and /api/dropbox/resume."""
    resp = await client.post("/api/dropbox/pause")
    assert resp.status_code == 200
    assert resp.json()["paused"] is True

    resp2 = await client.post("/api/dropbox/resume")
    assert resp2.status_code == 200
    assert resp2.json()["paused"] is False


@pytest.mark.anyio
async def test_dry_run_lifecycle(client, sync_dir, db_session):
    """POST dry-run, GET status, DELETE to stop."""
    from models import Project

    proj_dir = os.path.join(str(sync_dir), "proj_dry")
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "hello.txt"), "w") as f:
        f.write("hello")

    proj = Project(
        name="dryproj",
        display_name="Dry Project",
        path=proj_dir,
        max_concurrent=2,
        default_model="claude-opus-4-7",
    )
    db_session.add(proj)
    db_session.commit()

    # Start dry run
    resp = await client.post("/api/dropbox/dry-run", json={"project": "dryproj"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Poll until done
    import asyncio
    for _ in range(50):
        resp2 = await client.get(f"/api/dropbox/dry-run/{job_id}")
        assert resp2.status_code == 200
        data = resp2.json()
        if data["status"] != "running":
            break
        await asyncio.sleep(0.05)

    assert data["status"] in ("complete", "error")

    # Delete
    resp3 = await client.delete(f"/api/dropbox/dry-run/{job_id}")
    assert resp3.status_code == 200

    # Unknown job
    resp4 = await client.get("/api/dropbox/dry-run/nonexistent")
    assert resp4.status_code == 404


@pytest.mark.anyio
async def test_folders_for_project(client, sync_dir, db_session):
    """GET /api/projects/{name}/dropbox/folders."""
    from models import Project

    proj_dir = os.path.join(str(sync_dir), "folderproj")
    os.makedirs(os.path.join(proj_dir, "src"), exist_ok=True)
    with open(os.path.join(proj_dir, "readme.txt"), "w") as f:
        f.write("hello")

    proj = Project(
        name="folderproj",
        display_name="Folder Project",
        path=proj_dir,
        max_concurrent=2,
        default_model="claude-opus-4-7",
    )
    db_session.add(proj)
    db_session.commit()

    resp = await client.get("/api/projects/folderproj/dropbox/folders")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "folderproj"
    assert data["remote_root"] == "/folderproj"
    assert isinstance(data["entries"], list)
    names = [e["name"] for e in data["entries"]]
    assert "src" in names


@pytest.mark.anyio
async def test_project_dropbox_status(client, sync_dir, db_session):
    """GET /api/projects/{name}/dropbox/status."""
    _seed_token(sync_dir)

    from models import Project

    proj_dir = os.path.join(str(sync_dir), "statusproj")
    os.makedirs(proj_dir, exist_ok=True)

    proj = Project(
        name="statusproj",
        display_name="Status Project",
        path=proj_dir,
        max_concurrent=2,
        default_model="claude-opus-4-7",
        dropbox_sync=True,
    )
    db_session.add(proj)
    db_session.commit()

    resp = await client.get("/api/projects/statusproj/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["linked"] is True
    assert data["enabled"] is True
    assert data["account_email"] == "test@example.com"


@pytest.mark.anyio
async def test_settings_patch_dropbox_folders(client, sync_dir, db_session):
    """PATCH project settings with dropbox_folders still works."""
    from models import Project

    proj_dir = os.path.join(str(sync_dir), "patchproj")
    os.makedirs(proj_dir, exist_ok=True)

    proj = Project(
        name="patchproj",
        display_name="Patch Project",
        path=proj_dir,
        max_concurrent=2,
        default_model="claude-opus-4-7",
    )
    db_session.add(proj)
    db_session.commit()

    resp = await client.patch(
        "/api/projects/patchproj/settings",
        json={"dropbox_folders": ["src", "docs"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert json.loads(data["dropbox_folders"]) == ["docs", "src"]  # sorted

    # Clear
    resp2 = await client.patch(
        "/api/projects/patchproj/settings",
        json={"dropbox_folders": None},
    )
    assert resp2.status_code == 200
    assert resp2.json()["dropbox_folders"] is None


@pytest.mark.anyio
async def test_unlink_404_when_not_linked(client, sync_dir):
    """DELETE /api/dropbox/link returns 404 when not linked."""
    resp = await client.delete("/api/dropbox/link")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_unlink_when_linked(client, sync_dir):
    """DELETE /api/dropbox/link deletes token."""
    _seed_token(sync_dir)
    from dropbox_sync import engine
    assert engine.get_token_store().is_linked

    resp = await client.delete("/api/dropbox/link")
    assert resp.status_code == 200
    assert resp.json()["detail"] == "ok"
    assert not engine.get_token_store().is_linked


@pytest.mark.anyio
async def test_status_next_run_at_null_when_unlinked(client, sync_dir):
    """GET /api/dropbox/status returns null next_run_at when not linked."""
    from dropbox_sync import engine
    # Set a non-null _next_run_at
    engine._next_run_at = "2026-09-02T12:00:00Z"

    resp = await client.get("/api/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["linked"] is False
    assert data["next_run_at"] is None


@pytest.mark.anyio
async def test_status_next_run_at_null_when_paused(client, sync_dir):
    """GET /api/dropbox/status returns null next_run_at when paused."""
    _seed_token(sync_dir)
    from dropbox_sync import engine
    engine._cfg.paused = True
    engine._next_run_at = "2026-09-02T12:00:00Z"

    resp = await client.get("/api/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["linked"] is True
    assert data["next_run_at"] is None
    engine._cfg.paused = False


@pytest.mark.anyio
async def test_folders_endpoint_runs_off_loop(client, sync_dir, db_session):
    """GET /api/projects/{name}/dropbox/folders wraps list_folders in to_thread."""
    from models import Project

    proj_dir = os.path.join(str(sync_dir), "threadproj")
    os.makedirs(os.path.join(proj_dir, "src"), exist_ok=True)
    with open(os.path.join(proj_dir, "readme.txt"), "w") as f:
        f.write("hello")

    proj = Project(
        name="threadproj",
        display_name="Thread Project",
        path=proj_dir,
        max_concurrent=2,
        default_model="claude-opus-4-7",
    )
    db_session.add(proj)
    db_session.commit()

    resp = await client.get("/api/projects/threadproj/dropbox/folders")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["entries"], list)


# ── Redirect-based link flow (Amendment B) ──────────────────────────


@pytest.mark.anyio
async def test_link_start_redirect_mode(client, sync_dir):
    """POST /api/dropbox/link/start in redirect/direct mode derives redirect_uri from Origin."""
    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "abcdefghij1234", "mode": "direct"},
        headers={"Origin": "https://localhost:3000"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "direct"
    assert data["redirect_uri"] == "https://localhost:3000/api/dropbox/callback"
    assert "redirect_uri=" in data["authorize_url"]


@pytest.mark.anyio
async def test_link_start_code_mode_no_redirect_uri(client, sync_dir):
    """POST /api/dropbox/link/start in code mode has no redirect_uri."""
    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "abcdefghij1234", "mode": "code"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "code"
    assert data["redirect_uri"] is None
    assert "redirect_uri" not in data["authorize_url"]


@pytest.mark.anyio
async def test_link_start_no_configured_key(client, sync_dir, monkeypatch):
    """POST /api/dropbox/link/start with no configured key and no body key returns 400."""
    import config
    monkeypatch.setattr(config, "DROPBOX_APP_KEY", "")
    resp = await client.post(
        "/api/dropbox/link/start",
        json={"mode": "redirect"},
        headers={"Origin": "https://localhost:3000"},
    )
    assert resp.status_code == 400
    assert "DROPBOX_APP_KEY" in resp.json()["detail"]


@pytest.mark.anyio
async def test_link_start_return_to_open_redirect_guard(client, sync_dir):
    """return_to starting with // is rejected (open-redirect guard)."""
    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "abcdefghij1234", "return_to": "//evil.com"},
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_callback_happy_path(client, sync_dir, fake):
    """GET /api/dropbox/callback with valid code and state -> 302 with dropbox=linked."""
    from dropbox_sync import engine
    from dropbox_sync.auth import LinkFlow

    fake.register_oauth_code("cb_code", {
        "access_token": "sl.cb_token",
        "token_type": "bearer",
        "expires_in": 14400,
        "refresh_token": "rt_cb",
        "scope": "files.content.write account_info.read",
    })

    fake_http = httpx.AsyncClient(transport=fake.transport())
    store = engine.get_token_store()
    flow = LinkFlow(store, http=fake_http)
    engine._link_flow = flow

    # Start the flow to get the state
    result = flow.start("abcdefghij1234", redirect_uri="https://localhost:3000/api/dropbox/callback",
                        return_to="/projects/x")
    state = result["state"]

    # Simulate the callback
    resp = await client.get(
        f"/api/dropbox/callback?code=cb_code&state={state}",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/projects/x" in location
    assert "dropbox=linked" in location

    # Token should be saved
    assert store.is_linked

    await fake_http.aclose()


@pytest.mark.anyio
async def test_callback_wrong_state(client, sync_dir, fake):
    """GET /api/dropbox/callback with wrong state -> 302 with dropbox=error."""
    from dropbox_sync import engine
    from dropbox_sync.auth import LinkFlow

    fake_http = httpx.AsyncClient(transport=fake.transport())
    store = engine.get_token_store()
    flow = LinkFlow(store, http=fake_http)
    engine._link_flow = flow

    flow.start("abcdefghij1234", redirect_uri="https://localhost:3000/api/dropbox/callback",
               return_to="/monitor")

    resp = await client.get(
        "/api/dropbox/callback?code=some_code&state=wrong-state-value",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "dropbox=error" in location
    assert "state+mismatch" in location or "state%20mismatch" in location or "mismatch" in location

    await fake_http.aclose()


@pytest.mark.anyio
async def test_callback_error_from_dropbox(client, sync_dir):
    """GET /api/dropbox/callback with error param -> 302 with dropbox=error."""
    from dropbox_sync import engine
    from dropbox_sync.auth import LinkFlow

    store = engine.get_token_store()
    flow = LinkFlow(store)
    engine._link_flow = flow
    flow.start("abcdefghij1234", return_to="/monitor")

    resp = await client.get(
        "/api/dropbox/callback?error=access_denied&error_description=User+declined",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "dropbox=error" in location
    assert "User" in location or "declined" in location.lower()


@pytest.mark.anyio
async def test_callback_no_pending_flow(client, sync_dir):
    """GET /api/dropbox/callback with no pending flow -> 302 error to /monitor."""
    from dropbox_sync import engine
    engine._link_flow = None  # Reset to ensure a fresh flow with no pending

    resp = await client.get(
        "/api/dropbox/callback?code=abc&state=xyz",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "dropbox=error" in location


@pytest.mark.anyio
async def test_callback_auth_exempt(client, sync_dir):
    """The callback endpoint does not require auth (it's in _AUTH_EXEMPT_PREFIXES)."""
    # This test verifies the endpoint is reachable without auth.
    # Even with no pending flow, it should return a 302, not a 401.
    resp = await client.get(
        "/api/dropbox/callback?error=test",
        follow_redirects=False,
    )
    assert resp.status_code == 302


@pytest.mark.anyio
async def test_link_start_referer_derivation(client, sync_dir):
    """Redirect URI derived from Referer when Origin is absent."""
    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "abcdefghij1234", "mode": "direct"},
        headers={"Referer": "https://myhost:3000/monitor?tab=dropbox"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["redirect_uri"] == "https://myhost:3000/api/dropbox/callback"


# ── Amendment C — relay mode / link_mode resolution ───────────────────


@pytest.mark.anyio
async def test_link_start_auto_resolves_relay_with_default_key(client, sync_dir, monkeypatch):
    """auto mode resolves to relay when using the default app key and DROPBOX_RELAY_URL is set."""
    import config
    from dropbox_sync import engine
    monkeypatch.setattr(config, "DROPBOX_APP_KEY", "defaultkey1234")
    monkeypatch.setattr(config, "DROPBOX_USING_DEFAULT_APP", True)
    monkeypatch.setattr(config, "DROPBOX_RELAY_URL", "https://jianpengyao.github.io/xylocopa/oauth/dropbox/")

    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "defaultkey1234", "mode": "auto"},
        headers={"Origin": "https://localhost:3000"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "relay"
    assert data["redirect_uri"] == "https://jianpengyao.github.io/xylocopa/oauth/dropbox/"
    assert "relay_start_url" in data
    # relay_start_url is relay_url + "#" + urlencode(...)
    assert data["relay_start_url"].startswith("https://jianpengyao.github.io/xylocopa/oauth/dropbox/#")
    # Fragment must contain return= and authorize=, both percent-encoded
    fragment = data["relay_start_url"].split("#", 1)[1]
    assert "return=" in fragment
    assert "authorize=" in fragment
    # The authorize URL in the fragment should be percent-encoded
    assert "https%3A%2F%2Fwww.dropbox.com" in fragment
    # The return origin should be percent-encoded
    assert "https%3A%2F%2Flocalhost%3A3000" in fragment
    # The authorize_url itself should contain the relay URL as redirect_uri
    assert "redirect_uri=" in data["authorize_url"]


@pytest.mark.anyio
async def test_link_start_auto_resolves_direct_with_env_key(client, sync_dir, monkeypatch):
    """auto mode resolves to direct when user set their own DROPBOX_APP_KEY and no relay override."""
    import config
    monkeypatch.setattr(config, "DROPBOX_APP_KEY", "usercustom1234")
    monkeypatch.setattr(config, "DROPBOX_USING_DEFAULT_APP", False)
    monkeypatch.setattr(config, "DROPBOX_RELAY_URL", "")

    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "usercustom1234", "mode": "auto"},
        headers={"Origin": "https://localhost:3000"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "direct"
    assert data["redirect_uri"] == "https://localhost:3000/api/dropbox/callback"
    assert "relay_start_url" not in data or data.get("relay_start_url") is None


@pytest.mark.anyio
async def test_relay_start_url_encoding(client, sync_dir, monkeypatch):
    """relay_start_url encodes & and = inside the authorize URL correctly."""
    import config
    monkeypatch.setattr(config, "DROPBOX_APP_KEY", "relaytest12345")
    monkeypatch.setattr(config, "DROPBOX_USING_DEFAULT_APP", True)
    monkeypatch.setattr(config, "DROPBOX_RELAY_URL", "https://jianpengyao.github.io/xylocopa/oauth/dropbox/")

    resp = await client.post(
        "/api/dropbox/link/start",
        json={"app_key": "relaytest12345", "mode": "relay"},
        headers={"Origin": "https://myhost:3000"},
    )
    assert resp.status_code == 200
    data = resp.json()
    relay_url = data["relay_start_url"]
    assert "#" in relay_url
    fragment = relay_url.split("#", 1)[1]
    # The authorize URL has & and = inside it; they must be percent-encoded in the fragment
    # urlencode with quote_via=quote encodes & as %26 and = as %3D inside values
    from urllib.parse import parse_qs, unquote
    parsed = parse_qs(fragment)
    assert "return" in parsed
    assert "authorize" in parsed
    # The decoded authorize URL should be a valid Dropbox authorize URL
    authorize_decoded = parsed["authorize"][0]
    assert authorize_decoded.startswith("https://www.dropbox.com/oauth2/authorize?")
    # The return origin should match what was sent
    assert parsed["return"][0] == "https://myhost:3000"


@pytest.mark.anyio
async def test_callback_works_with_relay_redirect_uri(client, sync_dir, fake):
    """Callback still works (302 dropbox=linked) when the pending flow's redirect_uri is the relay URL."""
    from dropbox_sync import engine
    from dropbox_sync.auth import LinkFlow

    fake.register_oauth_code("relay_cb_code", {
        "access_token": "sl.relay_token",
        "token_type": "bearer",
        "expires_in": 14400,
        "refresh_token": "rt_relay",
        "scope": "files.content.write account_info.read",
    })

    fake_http = httpx.AsyncClient(transport=fake.transport())
    store = engine.get_token_store()
    flow = LinkFlow(store, http=fake_http)
    engine._link_flow = flow

    # Start the flow with the relay URL as redirect_uri
    relay_url = "https://jianpengyao.github.io/xylocopa/oauth/dropbox/"
    result = flow.start("abcdefghij1234",
                        redirect_uri=relay_url,
                        return_to="/projects/myproj")
    state = result["state"]

    # Simulate the callback (browser arrives via relay page)
    resp = await client.get(
        f"/api/dropbox/callback?code=relay_cb_code&state={state}",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/projects/myproj" in location
    assert "dropbox=linked" in location

    # The fake should have received redirect_uri in the token POST
    assert fake._last_token_form is not None
    from urllib.parse import unquote
    assert unquote(fake._last_token_form.get("redirect_uri", "")) == relay_url

    assert store.is_linked

    await fake_http.aclose()


@pytest.mark.anyio
async def test_status_includes_link_mode_and_relay_url(client, sync_dir, monkeypatch):
    """GET /api/dropbox/status includes link_mode and relay_url."""
    from dropbox_sync import engine
    monkeypatch.setattr(engine, "DROPBOX_APP_KEY", "statuskey12345")
    monkeypatch.setattr(engine, "DROPBOX_USING_DEFAULT_APP", True)
    monkeypatch.setattr(engine, "DROPBOX_RELAY_URL", "https://jianpengyao.github.io/xylocopa/oauth/dropbox/")

    resp = await client.get("/api/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["link_mode"] == "relay"
    assert data["relay_url"] == "https://jianpengyao.github.io/xylocopa/oauth/dropbox/"


@pytest.mark.anyio
async def test_status_link_mode_none_when_no_key(client, sync_dir, monkeypatch):
    """GET /api/dropbox/status returns link_mode=none when no app key is configured."""
    from dropbox_sync import engine
    monkeypatch.setattr(engine, "DROPBOX_APP_KEY", "")

    resp = await client.get("/api/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["link_mode"] == "none"


@pytest.mark.anyio
async def test_status_link_mode_direct_with_env_key(client, sync_dir, monkeypatch):
    """GET /api/dropbox/status returns link_mode=direct when user set own key with no relay."""
    from dropbox_sync import engine
    monkeypatch.setattr(engine, "DROPBOX_APP_KEY", "myownkey123456")
    monkeypatch.setattr(engine, "DROPBOX_USING_DEFAULT_APP", False)
    monkeypatch.setattr(engine, "DROPBOX_RELAY_URL", "")

    resp = await client.get("/api/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["link_mode"] == "direct"
    assert data["relay_url"] is None


@pytest.mark.anyio
async def test_project_status_includes_link_mode(client, sync_dir, db_session, monkeypatch):
    """GET /api/projects/{name}/dropbox/status includes link_mode."""
    _seed_token(sync_dir)
    from dropbox_sync import engine
    monkeypatch.setattr(engine, "DROPBOX_APP_KEY", "projstatuskey1")
    monkeypatch.setattr(engine, "DROPBOX_USING_DEFAULT_APP", True)
    monkeypatch.setattr(engine, "DROPBOX_RELAY_URL", "https://jianpengyao.github.io/xylocopa/oauth/dropbox/")

    from models import Project

    proj_dir = os.path.join(str(sync_dir), "linkmode_proj")
    os.makedirs(proj_dir, exist_ok=True)

    proj = Project(
        name="linkmode_proj",
        display_name="Link Mode Project",
        path=proj_dir,
        max_concurrent=2,
        default_model="claude-opus-4-7",
        dropbox_sync=True,
    )
    db_session.add(proj)
    db_session.commit()

    resp = await client.get("/api/projects/linkmode_proj/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["link_mode"] == "relay"


@pytest.mark.anyio
@pytest.mark.parametrize("any_enabled", [True, False])
async def test_callback_starts_sync_only_when_a_project_is_enabled(client, sync_dir, fake, monkeypatch, any_enabled):
    """Linking must kick off a run for already-enabled projects (and not otherwise)."""
    from dropbox_sync import engine
    from dropbox_sync.auth import LinkFlow

    fake.register_oauth_code("cb_code2", {
        "access_token": "sl.cb_token2", "token_type": "bearer", "expires_in": 14400,
        "refresh_token": "rt_cb2", "scope": "files.content.write account_info.read",
    })
    fake_http = httpx.AsyncClient(transport=fake.transport())
    flow = LinkFlow(engine.get_token_store(), http=fake_http)
    engine._link_flow = flow
    state = flow.start("abcdefghij1234", redirect_uri="https://localhost:3000/api/dropbox/callback",
                       return_to="/projects/x")["state"]

    calls = []
    monkeypatch.setattr(engine, "request_sync", lambda project=None, trigger="manual": calls.append((project, trigger)))
    monkeypatch.setattr(engine, "get_status", lambda: {"projects": [{"name": "p", "enabled": any_enabled}]})

    resp = await client.get(f"/api/dropbox/callback?code=cb_code2&state={state}", follow_redirects=False)
    assert resp.status_code == 302 and "dropbox=linked" in resp.headers["location"]
    assert calls == ([(None, "manual")] if any_enabled else [])


# ── Amendment E — interval_minutes config and status ─────────────────


@pytest.mark.anyio
async def test_config_put_interval_hours_compat(client, sync_dir):
    """PUT /api/dropbox/config with interval_hours converts to interval_minutes."""
    resp = await client.put("/api/dropbox/config", json={"interval_hours": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["interval_minutes"] == 120
    assert "interval_hours" not in data


@pytest.mark.anyio
async def test_config_put_interval_minutes(client, sync_dir):
    """PUT /api/dropbox/config with interval_minutes."""
    resp = await client.put("/api/dropbox/config", json={"interval_minutes": 15})
    assert resp.status_code == 200
    assert resp.json()["interval_minutes"] == 15


@pytest.mark.anyio
async def test_config_put_interval_minutes_bounds(client, sync_dir):
    """PUT /api/dropbox/config validates interval_minutes bounds."""
    # 0 is now valid (event-only mode)
    resp = await client.put("/api/dropbox/config", json={"interval_minutes": 0})
    assert resp.status_code == 200

    # -1 is invalid
    resp1b = await client.put("/api/dropbox/config", json={"interval_minutes": -1})
    assert resp1b.status_code == 400

    resp2 = await client.put("/api/dropbox/config", json={"interval_minutes": 1441})
    assert resp2.status_code == 400


@pytest.mark.anyio
async def test_status_includes_last_check(client, sync_dir):
    """GET /api/dropbox/status includes last_check field."""
    resp = await client.get("/api/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "last_check" in data
    assert "at" in data["last_check"]
    assert "changed" in data["last_check"]


@pytest.mark.anyio
async def test_project_status_includes_up_to_date(client, sync_dir, db_session):
    """GET /api/projects/{name}/dropbox/status includes up_to_date and last_checked_at."""
    _seed_token(sync_dir)

    from models import Project

    proj_dir = os.path.join(str(sync_dir), "utdproj")
    os.makedirs(proj_dir, exist_ok=True)

    proj = Project(
        name="utdproj",
        display_name="UTD Project",
        path=proj_dir,
        max_concurrent=2,
        default_model="claude-opus-4-7",
        dropbox_sync=True,
    )
    db_session.add(proj)
    db_session.commit()

    resp = await client.get("/api/projects/utdproj/dropbox/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "up_to_date" in data
    assert "last_checked_at" in data
