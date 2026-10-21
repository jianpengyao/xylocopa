"""Per-agent hook nonce (hook_auth.py): header injection, the launch hand-off
through a 0600 file, the check in _resolve_agent_for_hook, and fail-open.

DB state goes to a temp DB (tests/auth_helpers.make_auth_db); nonce files go
to a temp XDG_RUNTIME_DIR. tmux is never called: route_helpers._sp.run is
replaced by a recorder.
"""

import hashlib
import http.server
import json
import logging
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

import auth_store
import config
import database
import hook_auth
import main
import route_helpers
from models import Agent, AgentMode, AgentStatus, Project
from routers import agents as agents_router
from tests.auth_helpers import call, enable_auth, make_auth_db

PANE = "%4242"
AGENT_ID = "nonce0000agent"
NONCE = "n0nce-value-for-tests"


@pytest.fixture
def Session(tmp_path, monkeypatch):
    Session, _engine = make_auth_db(tmp_path)
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr("routers.hooks.SessionLocal", Session)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    os.makedirs(tmp_path / "run", mode=0o700)
    monkeypatch.setattr(config, "XY_HOOK_NONCE_MODE", "enforce")
    hook_auth._last_logged.clear()
    return Session


def _store(Session, agent_id: str = AGENT_ID, nonce: str = NONCE) -> None:
    db = Session()
    try:
        hook_auth._upsert(db, agent_id, hashlib.sha256(nonce.encode()).hexdigest())
    finally:
        db.close()


def _row(Session, agent_id: str = AGENT_ID):
    db = Session()
    try:
        auth_store._ready(db)
        return db.get(auth_store.AgentHookNonce, agent_id)
    finally:
        db.close()


def _req(headers: dict | None = None, path: str = "/api/hooks/agent-stop"):
    return SimpleNamespace(headers=headers or {}, url=SimpleNamespace(path=path))


# ---- inject_header ----

def _http_hooks(hooks: dict):
    for entries in hooks.values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                if hook.get("type") == "http":
                    yield hook


def test_inject_header_on_bundled_default():
    raw = agents_router._DEFAULT_AGENT_HOOKS_JSON
    hooks = json.loads(raw.replace("{{BASE_URL}}", "http://localhost:8080/api/hooks")
                       .replace("{{HOOK_SCRIPT}}", "/x/pretooluse-safety.py"))
    before = json.loads(json.dumps(hooks))
    out = hook_auth.inject_header(hooks)
    assert hooks == before  # pure
    http = list(_http_hooks(out))
    assert len(http) == len(list(_http_hooks(before))) > 10
    for hook in http:
        assert hook["headers"]["X-Xy-Hook-Nonce"] == "$XY_HOOK_NONCE"
        assert hook["headers"]["X-Agent-Id"] == "$XY_AGENT_ID"
        assert hook["allowedEnvVars"] == ["XY_AGENT_ID", "AHIVE_AGENT_ID", "XY_HOOK_NONCE"]
    # Command hooks and other fields untouched; idempotent.
    assert out["PreToolUse"][0] == before["PreToolUse"][0]
    perm = out["PreToolUse"][2]["hooks"][0]
    assert perm["timeout"] == 86400
    assert hook_auth.inject_header(out) == out


def test_inject_header_on_live_template_shape():
    """The on-disk template adds a Notification hook and may lack headers."""
    live = {
        "Notification": [{"matcher": "permission_prompt", "hooks": [
            {"type": "http", "url": "http://localhost:8080/api/hooks/agent-permission-request",
             "headers": {"X-Agent-Id": "$XY_AGENT_ID"}, "allowedEnvVars": ["XY_AGENT_ID"]}]}],
        "Stop": [{"hooks": [{"type": "http", "url": "http://localhost:8080/api/hooks/agent-stop"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}],
        "weird": "not-a-list",
    }
    out = hook_auth.inject_header(live)
    assert out["Notification"][0]["hooks"][0]["headers"] == {
        "X-Agent-Id": "$XY_AGENT_ID", "X-Xy-Hook-Nonce": "$XY_HOOK_NONCE"}
    assert out["Stop"][0]["hooks"][0]["headers"] == {"X-Xy-Hook-Nonce": "$XY_HOOK_NONCE"}
    assert out["Stop"][0]["hooks"][0]["allowedEnvVars"] == ["XY_HOOK_NONCE"]
    assert out["PreToolUse"] == live["PreToolUse"] and out["weird"] == "not-a-list"


def test_written_project_hooks_carry_the_header(tmp_path, monkeypatch):
    monkeypatch.setattr(agents_router, "_load_agent_hooks_template",
                        lambda base, script: {"Stop": [{"hooks": [
                            {"type": "http", "url": f"{base}/agent-stop"}]}]})
    agents_router._write_agent_hooks_config(str(tmp_path))
    written = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    hook = written["hooks"]["Stop"][0]["hooks"][0]
    assert hook["headers"]["X-Xy-Hook-Nonce"] == "$XY_HOOK_NONCE"
    assert "XY_HOOK_NONCE" in hook["allowedEnvVars"]


# ---- launch hand-off ----

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = "%77\n" if argv[:2] == ["tmux", "display-message"] else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def _launch(monkeypatch, agent_id=AGENT_ID):
    rec = _Recorder()
    monkeypatch.setattr(route_helpers._sp, "run", rec)
    pane = route_helpers.create_tmux_claude_session("xy-nonce000", "/tmp", "claude", agent_id=agent_id)
    assert pane == "%77"
    setup = [c for c in rec.calls if c[:2] == ["tmux", "send-keys"]][0][4]
    return rec, setup


def _file_from(setup: str) -> str:
    return setup.split("$(cat ", 1)[1].split(")", 1)[0].strip("'")


def test_launch_hands_nonce_over_through_a_0600_file(Session, monkeypatch):
    monkeypatch.setattr(hook_auth, "_CONSUME_TIMEOUT_S", 60.0)
    rec, setup = _launch(monkeypatch)
    assert setup.startswith(" unset CLAUDECODE")  # leading space: no history
    path = _file_from(setup)
    assert setup.endswith(f' && export XY_HOOK_NONCE="$(cat {path})" && rm -f {path}')
    nonce = open(path).read()
    assert len(nonce) >= 32
    # The secret itself is in no tmux argv.
    assert not any(nonce in arg for argv in rec.calls for arg in argv)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert os.path.dirname(path) == os.path.join(os.environ["XDG_RUNTIME_DIR"], "xylocopa")
    assert _row(Session).nonce_hash == hashlib.sha256(nonce.encode()).hexdigest()
    os.unlink(path)


def test_launch_without_agent_id_registers_nothing(Session, monkeypatch):
    rec, setup = _launch(monkeypatch, agent_id=None)
    assert "XY_HOOK_NONCE" not in setup
    assert _row(Session) is None


def test_unconsumed_nonce_fails_open(Session, monkeypatch):
    monkeypatch.setattr(hook_auth, "_CONSUME_TIMEOUT_S", 0.2)
    _rec, setup = _launch(monkeypatch)
    path = _file_from(setup)
    assert os.path.exists(path) and _row(Session) is not None
    deadline = time.time() + 5
    while (os.path.exists(path) or _row(Session) is not None) and time.time() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(path)
    assert _row(Session) is None  # legacy: hooks without a nonce pass


def test_consumed_nonce_survives_the_timer(Session, monkeypatch):
    monkeypatch.setattr(hook_auth, "_CONSUME_TIMEOUT_S", 0.2)
    _rec, setup = _launch(monkeypatch)
    os.unlink(_file_from(setup))  # what the pane's shell does
    time.sleep(0.5)
    assert _row(Session) is not None


def test_timer_never_drops_a_newer_launch_row(Session, monkeypatch):
    monkeypatch.setattr(hook_auth, "_CONSUME_TIMEOUT_S", 60.0)
    first = hook_auth.register_nonce(AGENT_ID)
    second = hook_auth.register_nonce(AGENT_ID)
    os.unlink(second[1])
    hook_auth._verify_consumed(AGENT_ID, first[1], hashlib.sha256(first[0].encode()).hexdigest())
    assert not os.path.exists(first[1])
    assert _row(Session).nonce_hash == hashlib.sha256(second[0].encode()).hexdigest()


def test_register_fails_open_on_file_or_db_error(Session, monkeypatch, tmp_path):
    def _no_dir():
        raise OSError("read-only")
    with monkeypatch.context() as m:
        m.setattr(hook_auth, "nonce_dir", _no_dir)
        assert hook_auth.register_nonce(AGENT_ID) is None
    assert _row(Session) is None

    def _locked():
        raise OperationalError("INSERT", {}, Exception("database is locked"))
    monkeypatch.setattr(hook_auth, "_session_local", _locked)
    assert hook_auth.register_nonce(AGENT_ID) is None
    assert os.listdir(tmp_path / "run" / "xylocopa") == []  # file removed again


def test_nonce_dir_refuses_a_symlink(tmp_path, monkeypatch):
    run = tmp_path / "run2"
    run.mkdir(mode=0o700)
    (run / "xylocopa").symlink_to(tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    with pytest.raises(OSError):
        hook_auth.nonce_dir()


def test_mode_defaults_to_enforce():
    # Spike A0's result; .env is kept out so only the code default counts.
    env = {k: v for k, v in os.environ.items() if k != "XY_HOOK_NONCE_MODE"}
    out = subprocess.run(
        [sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: None; "
                               "import config; print(config.XY_HOOK_NONCE_MODE)"],
        cwd=os.path.dirname(config.__file__), env=env, capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "enforce", out.stderr


def test_nonce_is_stripped_from_subprocess_env():
    assert "XY_HOOK_NONCE" in route_helpers.SUBPROCESS_STRIP_VARS


def _run_session_start(tmp_path, nonce: str | None):
    """Run hooks/session-start.sh against a stub server with the real curl
    behind a wrapper that records curl's argv. → (request headers, argv)."""
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["headers"] = dict(self.headers)
            seen["body"] = self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_curl = shutil.which("curl")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_file = tmp_path / "curl.argv"
    (bindir / "curl").write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$@" > {shlex.quote(str(argv_file))}\nexec {real_curl} "$@"\n')
    (bindir / "curl").chmod(0o755)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "XY_PORT": str(server.server_port), "XY_AGENT_ID": AGENT_ID, "TMUX_PANE": PANE}
    if nonce is not None:
        env["XY_HOOK_NONCE"] = nonce
    script = os.path.join(os.path.dirname(route_helpers.__file__), "hooks", "session-start.sh")
    try:
        subprocess.run(["bash", script], input=b'{"session_id":"s-1","source":"startup"}',
                       env=env, cwd=str(tmp_path), timeout=20, check=True)
    finally:
        server.shutdown()
        server.server_close()
        # A non-200 would have left the offline fallback signal file.
        if os.path.exists(f"/tmp/xy-{AGENT_ID}.newsession"):
            os.unlink(f"/tmp/xy-{AGENT_ID}.newsession")
    return seen.get("headers", {}), argv_file.read_text()


def test_session_start_sends_the_nonce_but_never_in_argv(tmp_path):
    headers, argv = _run_session_start(tmp_path, NONCE)
    assert headers["X-Xy-Hook-Nonce"] == NONCE
    assert headers["X-Agent-Id"] == AGENT_ID and headers["X-Tmux-Pane"] == PANE
    # /proc/<curl pid>/cmdline is world-readable: the secret must not be there.
    assert NONCE not in argv and "X-Agent-Id: " + AGENT_ID in argv


def test_session_start_without_a_nonce_sends_no_header(tmp_path):
    headers, _ = _run_session_start(tmp_path, None)
    assert "X-Xy-Hook-Nonce" not in headers
    assert headers["X-Agent-Id"] == AGENT_ID


# ---- check() ----

def test_no_row_passes(Session, monkeypatch):
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    hook_auth.check(_req(), AGENT_ID)


def test_matching_header_passes(Session, monkeypatch):
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    _store(Session)
    hook_auth.check(_req({"X-Xy-Hook-Nonce": NONCE}), AGENT_ID)


@pytest.mark.parametrize("value", [None, "", "$XY_HOOK_NONCE", "${XY_HOOK_NONCE}", "wrong-value"])
def test_mismatch_enforced_with_auth_on(Session, monkeypatch, caplog, value):
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    _store(Session)
    headers = {} if value is None else {"X-Xy-Hook-Nonce": value}
    caplog.set_level(logging.WARNING, logger="orchestrator.auth")
    with pytest.raises(HTTPException) as exc:
        hook_auth.check(_req(headers), AGENT_ID)
    assert exc.value.status_code == 403 and exc.value.detail == "Hook nonce mismatch"
    kind = "wrong" if value == "wrong-value" else "missing"
    assert f"HOOK_NONCE_REJECT agent=nonce000 kind={kind} hook=agent-stop" in caplog.text
    if value:
        assert value not in caplog.text.replace("agent-stop", "")


def test_mismatch_logged_only_under_disable_auth(Session, monkeypatch, caplog):
    monkeypatch.setenv("DISABLE_AUTH", "1")
    _store(Session)
    caplog.set_level(logging.WARNING, logger="orchestrator.auth")
    hook_auth.check(_req({"X-Xy-Hook-Nonce": "$XY_HOOK_NONCE"}), AGENT_ID)
    hook_auth.check(_req(), AGENT_ID)  # throttled: once per agent per 10 min
    assert caplog.text.count("HOOK_NONCE_MISMATCH agent=nonce000 kind=missing") == 1


def test_log_mode_passes_with_auth_on(Session, monkeypatch, caplog):
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    monkeypatch.setattr(config, "XY_HOOK_NONCE_MODE", "log")
    _store(Session)
    caplog.set_level(logging.WARNING, logger="orchestrator.auth")
    hook_auth.check(_req({"X-Xy-Hook-Nonce": "nope"}), AGENT_ID)
    assert "HOOK_NONCE_MISMATCH agent=nonce000 kind=wrong" in caplog.text


def test_unknown_mode_is_log_only(Session, monkeypatch):
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    monkeypatch.setattr(config, "XY_HOOK_NONCE_MODE", "enforced-typo")
    assert hook_auth.mode() == "log"
    _store(Session)
    hook_auth.check(_req(), AGENT_ID)


def test_mode_off_skips_the_lookup(Session, monkeypatch):
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    monkeypatch.setattr(config, "XY_HOOK_NONCE_MODE", "off")
    monkeypatch.setattr(hook_auth, "_session_local", lambda: pytest.fail("DB read in mode off"))
    hook_auth.check(_req(), AGENT_ID)


def test_lookup_error_fails_open(Session, monkeypatch):
    monkeypatch.delenv("DISABLE_AUTH", raising=False)

    def _locked():
        raise OperationalError("SELECT", {}, Exception("database is locked"))
    monkeypatch.setattr(hook_auth, "_session_local", _locked)
    hook_auth.check(_req(), AGENT_ID)


def test_forget_nonce(Session):
    _store(Session)
    db = Session()
    try:
        hook_auth.forget_nonce(AGENT_ID, db)
        db.commit()
    finally:
        db.close()
    assert _row(Session) is None


# ---- through the real hook route ----

@pytest.fixture
def hook_env(Session, monkeypatch):
    enable_auth(monkeypatch, Session)
    monkeypatch.setattr(main.app.state, "agent_dispatcher", None, raising=False)
    db = Session()
    try:
        db.add(Project(name="nonce-proj", display_name="N", path="/tmp/nonce-proj"))
        db.flush()
        db.add(Agent(id=AGENT_ID, project="nonce-proj", name="nonce agent", mode=AgentMode.AUTO,
                     status=AgentStatus.IDLE, tmux_pane=PANE, model="claude-opus-4-7"))
        db.add(Agent(id="legacy000agent", project="nonce-proj", name="legacy agent",
                     mode=AgentMode.AUTO, status=AgentStatus.IDLE, tmux_pane="%4343",
                     model="claude-opus-4-7"))
        db.commit()
    finally:
        db.close()
    _store(Session)
    return Session


async def _hook(pane: str, nonce: str | None = None, content_type: str = "application/json"):
    headers = [("x-tmux-pane", pane)]
    if nonce is not None:
        headers.append(("x-xy-hook-nonce", nonce))
    return await call("POST", "/api/hooks/agent-session-end", ip="127.0.0.1", headers=headers,
                      body=b'{"session_id": "s-1", "reason": "other"}', content_type=content_type)


@pytest.mark.anyio
async def test_route_enforce_with_auth_on(hook_env):
    assert (await _hook(PANE, NONCE))[0] == 200
    status, _, body = await _hook(PANE)
    assert (status, body) == (403, {"detail": "Hook nonce mismatch"})
    assert (await _hook(PANE, "wrong"))[0] == 403
    assert (await _hook("%4343"))[0] == 200  # legacy agent: no row
    assert (await _hook(PANE, NONCE, content_type="text/plain"))[0] == 415


@pytest.mark.anyio
async def test_route_log_only_under_disable_auth(hook_env, monkeypatch, caplog):
    monkeypatch.setenv("DISABLE_AUTH", "1")
    caplog.set_level(logging.WARNING, logger="orchestrator.auth")
    assert (await _hook(PANE))[0] == 200
    assert (await _hook(PANE, "wrong"))[0] == 200
    assert "HOOK_NONCE_MISMATCH agent=nonce000 kind=missing hook=agent-session-end" in caplog.text


@pytest.mark.anyio
async def test_route_enforce_on_session_id_resolution(hook_env):
    """HTTP hooks send no X-Tmux-Pane; they resolve by session_id (cli_sync)."""
    db = hook_env()
    try:
        agent = db.get(Agent, AGENT_ID)
        agent.tmux_pane, agent.session_id, agent.cli_sync = None, "sid-nonce-1", True
        db.commit()
    finally:
        db.close()

    async def _by_sid(nonce=None):
        headers = [] if nonce is None else [("x-xy-hook-nonce", nonce)]
        return (await call("POST", "/api/hooks/agent-session-end", ip="127.0.0.1", headers=headers,
                           json={"session_id": "sid-nonce-1", "reason": "other"}))[0]

    assert await _by_sid() == 403
    assert await _by_sid(NONCE) == 200


@pytest.mark.anyio
async def test_adopt_into_existing_agent_drops_its_nonce(client, db_engine, tmp_path, monkeypatch):
    """Binding a user-launched pane (no nonce) to an existing agent must not
    leave that agent's stored nonce behind, or its hooks would be refused."""
    from unittest.mock import MagicMock

    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    db = Session()
    try:
        db.add(Project(name="adopt-proj", display_name="A", path=str(tmp_path)))
        db.flush()
        db.add(Agent(id=AGENT_ID, project="adopt-proj", name="stopped agent", mode=AgentMode.AUTO,
                     status=AgentStatus.STOPPED, model="claude-opus-4-7"))
        db.commit()
    finally:
        db.close()
    _store(Session)
    assert _row(Session) is not None

    udir = tmp_path / "unlinked"
    udir.mkdir()
    (udir / "pane-9.json").write_text(json.dumps(
        {"session_id": "sid-adopt-1", "cwd": str(tmp_path), "tmux_pane": "%9"}))
    monkeypatch.setattr(agents_router, "_UNLINKED_DIR", str(udir))
    monkeypatch.setattr(main.app.state, "agent_dispatcher", MagicMock(), raising=False)

    async def _no_emit(*args, **kwargs):
        return None
    monkeypatch.setattr("routers.agents.emit_agent_update", _no_emit)

    resp = await client.post("/api/unlinked-sessions/pane-9/adopt",
                             json={"project": "adopt-proj", "agent_id": AGENT_ID})
    assert resp.status_code == 200, resp.text
    assert _row(Session) is None
