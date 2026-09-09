"""Tests for the legacy AgentHive → Xylocopa git identity migration.

Pre-rebrand orchestrators wrote ``user.name=AgentHive`` /
``user.email=agenthive@localhost`` into each managed repo's ``.git/config``.
The migration must rewrite exactly that identity and nothing else.
"""

import subprocess

import pytest

from git_manager import GitManager, git_identity


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=False,
    ).stdout.strip()


def _repo(tmp_path, name, *, user_name=None, user_email=None):
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if user_name is not None:
        _git(path, "config", "user.name", user_name)
    if user_email is not None:
        _git(path, "config", "user.email", user_email)
    return path


@pytest.fixture(autouse=True)
def _default_identity(monkeypatch):
    monkeypatch.delenv("GIT_USER_NAME", raising=False)
    monkeypatch.delenv("GIT_USER_EMAIL", raising=False)


def test_default_identity_is_xylocopa():
    assert git_identity() == ("Xylocopa", "xylocopa@localhost")


def test_identity_env_override(monkeypatch):
    monkeypatch.setenv("GIT_USER_NAME", "my-bot")
    monkeypatch.setenv("GIT_USER_EMAIL", "bot@example.com")
    assert git_identity() == ("my-bot", "bot@example.com")


def test_migrates_legacy_agenthive_identity(tmp_path):
    repo = _repo(tmp_path, "legacy", user_name="AgentHive", user_email="agenthive@localhost")
    gm = GitManager()

    assert gm.migrate_legacy_identity(str(repo)) is True
    assert _git(repo, "config", "--local", "user.name") == "Xylocopa"
    assert _git(repo, "config", "--local", "user.email") == "xylocopa@localhost"
    # The effective author for a future commit is the new identity.
    assert _git(repo, "var", "GIT_AUTHOR_IDENT").startswith("Xylocopa <xylocopa@localhost>")

    # Idempotent: second run is a no-op.
    assert gm.migrate_legacy_identity(str(repo)) is False


def test_migrates_name_only_when_email_custom(tmp_path):
    repo = _repo(tmp_path, "mixed", user_name="AgentHive", user_email="me@example.com")
    assert GitManager().migrate_legacy_identity(str(repo)) is True
    assert _git(repo, "config", "--local", "user.name") == "Xylocopa"
    assert _git(repo, "config", "--local", "user.email") == "me@example.com"


def test_leaves_user_set_identity_alone(tmp_path):
    repo = _repo(tmp_path, "custom", user_name="jianpengyao and Claude", user_email="me@example.com")
    assert GitManager().migrate_legacy_identity(str(repo)) is False
    assert _git(repo, "config", "--local", "user.name") == "jianpengyao and Claude"
    assert _git(repo, "config", "--local", "user.email") == "me@example.com"


def test_leaves_unset_identity_alone(tmp_path):
    repo = _repo(tmp_path, "unset")
    assert GitManager().migrate_legacy_identity(str(repo)) is False
    assert _git(repo, "config", "--local", "--get", "user.name") == ""


def test_non_git_dir_is_skipped(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert GitManager().migrate_legacy_identity(str(plain)) is False


def test_missing_dir_is_skipped(tmp_path):
    assert GitManager().migrate_legacy_identity(str(tmp_path / "nope")) is False


def test_migration_respects_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_USER_NAME", "my-bot")
    monkeypatch.setenv("GIT_USER_EMAIL", "bot@example.com")
    repo = _repo(tmp_path, "env", user_name="AgentHive", user_email="agenthive@localhost")
    assert GitManager().migrate_legacy_identity(str(repo)) is True
    assert _git(repo, "config", "--local", "user.name") == "my-bot"
    assert _git(repo, "config", "--local", "user.email") == "bot@example.com"


def test_linked_worktree_inherits_migrated_identity(tmp_path):
    """Agent worktrees share the parent's .git/config, so fixing the parent fixes them."""
    repo = _repo(tmp_path, "parent", user_name="AgentHive", user_email="agenthive@localhost")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "-b", "agent"], check=True)
    assert _git(wt, "var", "GIT_AUTHOR_IDENT").startswith("AgentHive <agenthive@localhost>")

    assert GitManager().migrate_legacy_identity(str(repo)) is True
    assert _git(wt, "var", "GIT_AUTHOR_IDENT").startswith("Xylocopa <xylocopa@localhost>")


def test_set_identity_used_by_merge(tmp_path):
    repo = _repo(tmp_path, "merge", user_name="AgentHive", user_email="agenthive@localhost")
    GitManager().set_identity(str(repo))
    assert _git(repo, "config", "--local", "user.name") == "Xylocopa"
    assert _git(repo, "config", "--local", "user.email") == "xylocopa@localhost"


def test_startup_migration_walks_db_projects(tmp_path, db_engine, db_session, monkeypatch):
    """main._migrate_legacy_git_identity rewrites every DB project repo (incl. archived)."""
    from sqlalchemy.orm import sessionmaker
    from models import Project
    import main as main_mod

    legacy = _repo(tmp_path, "legacy", user_name="AgentHive", user_email="agenthive@localhost")
    archived = _repo(tmp_path, "archived", user_name="AgentHive", user_email="agenthive@localhost")
    custom = _repo(tmp_path, "custom", user_name="me", user_email="me@example.com")
    for name, path, arch in [("legacy", legacy, False), ("archived", archived, True), ("custom", custom, False)]:
        db_session.add(Project(name=name, display_name=name, path=str(path), archived=arch))
    db_session.add(Project(name="gone", display_name="gone", path=str(tmp_path / "gone")))
    db_session.commit()

    monkeypatch.setattr(main_mod, "SessionLocal", sessionmaker(bind=db_engine))
    assert main_mod._migrate_legacy_git_identity(GitManager()) == 2
    assert _git(legacy, "config", "--local", "user.name") == "Xylocopa"
    assert _git(archived, "config", "--local", "user.name") == "Xylocopa"
    assert _git(custom, "config", "--local", "user.name") == "me"
    # second run: nothing left to do
    assert main_mod._migrate_legacy_git_identity(GitManager()) == 0
