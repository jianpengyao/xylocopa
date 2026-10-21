"""Break-glass CLI (reset_password.py), always against a temp DB.

config.DB_PATH and database.engine / SessionLocal are pointed at a temp file
for every test; the CLI refuses (exit 3) when the engine and DB_PATH differ,
so a missed patch can never reach another database.
"""

import io
import os
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import auth
import auth_store
import config
import database
import reset_password
from auth import get_config, get_password_hash, verify_password
from models import Base
from tests.auth_helpers import fast_hash

OLD_PASSWORD = "old-password-123"
NEW_PASSWORD = "brand-new-pass-456"


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    path = str(tmp_path / "cli.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)  # what init_db leaves; no auth tables yet
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    auth.set_config(db, "password_hash", fast_hash(OLD_PASSWORD))
    auth.set_config(db, "jwt_secret", "s" * 64)
    db.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", Session)
    # bcrypt cost 12 is the real thing; cost 4 keeps the suite fast.
    monkeypatch.setattr(auth, "hash_password", fast_hash)
    return Session


def _run(*argv, stdin: str = ""):
    out, err = io.StringIO(), io.StringIO()
    code = reset_password.main(list(argv), stdin=io.StringIO(stdin), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _sessions(Session, n: int) -> list[str]:
    db = Session()
    try:
        return [auth_store.create_session(db, user_agent="pytest")[0] for _ in range(n)]
    finally:
        db.close()


def _cfg(Session, key: str):
    db = Session()
    try:
        return get_config(db, key)
    finally:
        db.close()


def test_password_changed_and_sessions_revoked(cli_db):
    tokens = _sessions(cli_db, 2)
    before = time.time()
    code, out, err = _run("--password-stdin", stdin=NEW_PASSWORD + "\n")
    assert code == 0, err
    assert f"Database: {config.DB_PATH}" in out
    assert "Signed out 2 device session(s)" in out
    db = cli_db()
    try:
        stored = get_password_hash(db)
        assert verify_password(NEW_PASSWORD, stored)
        assert not verify_password(OLD_PASSWORD, stored)
        for tok in tokens:
            result = auth_store.authenticate(db, tok)
            assert (result.ok, result.reason) == (False, "revoked")
            row = auth_store.get_session(db, auth_store.lookup_session_id(db, tok))
            assert row.revoked_reason == "reset"
    finally:
        db.close()
    assert float(_cfg(cli_db, "auth_jwt_not_before")) >= before
    assert float(_cfg(cli_db, "auth_limiter_reset_at")) >= before
    assert _cfg(cli_db, "jwt_secret") == "s" * 64  # not rotated by default
    assert _cfg(cli_db, "auth_sessions_epoch") is not None  # schema ensured


def test_reset_unlocks_a_locked_ip(cli_db):
    """G4: the running server's limiter drops entries older than reset_at."""
    limiter = auth.LoginRateLimiter()
    t0 = time.time() - 5
    for i in range(config.AUTH_LIMIT_THRESHOLD):
        limiter.record_failure("100.64.0.10", now=t0 + i * 0.1)
    assert limiter.check("100.64.0.10")[0] is True
    assert _run("--password-stdin", stdin=NEW_PASSWORD + "\n")[0] == 0
    db = cli_db()
    try:
        reset_at = auth_store.get_limiter_reset_at(db)
    finally:
        db.close()
    assert limiter.check("100.64.0.10", reset_at=reset_at) == (False, 0)


def test_rotate_secret(cli_db):
    code, out, _ = _run("--password-stdin", "--rotate-secret", stdin=NEW_PASSWORD + "\n")
    assert code == 0
    secret = _cfg(cli_db, "jwt_secret")
    assert secret != "s" * 64 and len(secret) == 64
    assert "port-preview cards and open previews must be re-presented" in out


def test_keep_sessions(cli_db):
    tokens = _sessions(cli_db, 2)
    code, out, _ = _run("--password-stdin", "--keep-sessions", stdin=NEW_PASSWORD + "\n")
    assert code == 0 and "kept" in out
    db = cli_db()
    try:
        assert all(auth_store.authenticate(db, t).ok for t in tokens)
    finally:
        db.close()
    assert _cfg(cli_db, "auth_jwt_not_before") is None
    assert _cfg(cli_db, "auth_limiter_reset_at") is not None


def test_idle_timeout_option(cli_db):
    assert _run("--password-stdin", "--idle-timeout", "15", stdin=NEW_PASSWORD + "\n")[0] == 0
    assert _cfg(cli_db, "auth_idle_timeout_minutes") == "15"
    code, out, _ = _run("--password-stdin", "--idle-timeout", "off", stdin=NEW_PASSWORD + "\n")
    assert code == 0 and "Idle lock: off." in out
    assert _cfg(cli_db, "auth_idle_timeout_minutes") == "0"


@pytest.mark.parametrize("value", ["7", "abc", "-1"])
def test_bad_idle_timeout_exits_2_and_changes_nothing(cli_db, value):
    code, _, err = _run("--password-stdin", "--idle-timeout", value, stdin=NEW_PASSWORD + "\n")
    assert code == 2 and "--idle-timeout" in err
    db = cli_db()
    try:
        assert verify_password(OLD_PASSWORD, get_password_hash(db))
    finally:
        db.close()


def test_short_password_exits_2_and_changes_nothing(cli_db):
    tokens = _sessions(cli_db, 1)
    code, _, err = _run("--password-stdin", stdin="short\n")
    assert code == 2 and "at least 8" in err
    db = cli_db()
    try:
        assert verify_password(OLD_PASSWORD, get_password_hash(db))
        assert auth_store.authenticate(db, tokens[0]).ok
    finally:
        db.close()


def test_missing_db_exits_3_and_creates_nothing(cli_db, tmp_path, monkeypatch):
    missing = tmp_path / "nowhere" / "orchestrator.db"
    monkeypatch.setattr(config, "DB_PATH", str(missing))
    code, out, err = _run("--password-stdin", stdin=NEW_PASSWORD + "\n")
    assert code == 3 and "database not found" in err
    assert f"Database: {missing}" in out
    assert not missing.exists() and not missing.parent.exists()


def test_engine_db_path_mismatch_exits_3(cli_db, tmp_path, monkeypatch):
    other = tmp_path / "other.db"
    other.write_bytes(b"")
    monkeypatch.setattr(config, "DB_PATH", str(other))
    code, _, err = _run("--password-stdin", stdin=NEW_PASSWORD + "\n")
    assert code == 3 and "not " + str(other) in err
    db = cli_db()
    try:
        assert verify_password(OLD_PASSWORD, get_password_hash(db))
    finally:
        db.close()


def test_generate_prints_the_password_once(cli_db):
    code, out, _ = _run("--generate", "--yes")
    assert code == 0
    lines = [ln for ln in out.splitlines() if ln.startswith("New password: ")]
    assert len(lines) == 1
    password = lines[0].split(": ", 1)[1]
    assert len(password) >= 8 and out.count(password) == 1
    db = cli_db()
    try:
        assert verify_password(password, get_password_hash(db))
    finally:
        db.close()


def test_interactive_without_terminal_exits_2(cli_db):
    code, _, err = _run()
    assert code == 2 and "--password-stdin" in err


def test_interactive_prompts_twice(cli_db, monkeypatch):
    answers = iter([NEW_PASSWORD, NEW_PASSWORD])
    monkeypatch.setattr(reset_password.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    out, err = io.StringIO(), io.StringIO()
    assert reset_password.main([], stdin=_Tty(""), out=out, err=err) == 0, err.getvalue()
    db = cli_db()
    try:
        assert verify_password(NEW_PASSWORD, get_password_hash(db))
    finally:
        db.close()

    answers = iter([NEW_PASSWORD, "something-else"])
    assert reset_password.main([], stdin=_Tty(""), out=io.StringIO(), err=err) == 2
    assert "do not match" in err.getvalue()


def test_disable_auth_note(cli_db, monkeypatch):
    monkeypatch.setenv("DISABLE_AUTH", "1")
    _, out, _ = _run("--password-stdin", stdin=NEW_PASSWORD + "\n")
    assert "DISABLE_AUTH is set" in out
    monkeypatch.delenv("DISABLE_AUTH")
    _, out, _ = _run("--password-stdin", stdin=NEW_PASSWORD + "\n")
    assert "DISABLE_AUTH" not in out


def test_help_and_usage_errors(cli_db, capsys):
    assert reset_password.main(["--help"]) == 0
    assert "reset-password" in capsys.readouterr().out
    assert reset_password.main(["--generate", "--password-stdin"]) == 2
    assert reset_password.main(["--bogus"]) == 2


def test_cli_never_imports_main():
    src = open(reset_password.__file__).read()
    assert "import main" not in src and "from main" not in src
    assert os.path.basename(reset_password.__file__) == "reset_password.py"
