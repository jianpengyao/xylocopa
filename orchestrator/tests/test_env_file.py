"""env_file.py: read_key / set_key on a temp .env (never the real one).

set_key must keep every byte it doesn't own (other lines, comments, blank
lines, order, line endings, trailing newline) and the file mode, leave
commented lines alone, and write atomically (a failed write leaves the file
as it was, with no temp file behind).
"""

import os
import stat

import pytest

import config
import env_file

SAMPLE = (
    "# === General ===\n"
    "PORT=8080\n"
    "\n"
    "# DISABLE_AUTH=1\n"
    "CORS_ORIGINS=https://localhost:3000\n"
    "DISABLE_AUTH=1\n"
    "  # trailing comment block\n"
    "VAPID_SUBJECT=mailto:xylocopa@example.com\n"
)


def _write(path, data: bytes, mode: int = 0o600):
    path.write_bytes(data)
    os.chmod(path, mode)
    return str(path)


def test_default_path_is_project_root_env():
    assert env_file.ENV_PATH == os.path.join(config._PROJECT_ROOT, ".env")


def test_edits_the_uncommented_line_in_place_and_keeps_every_other_byte(tmp_path):
    p = _write(tmp_path / ".env", SAMPLE.encode(), 0o640)
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert (tmp_path / ".env").read_bytes() == SAMPLE.replace("\nDISABLE_AUTH=1\n", "\nDISABLE_AUTH=0\n").encode()
    assert "# DISABLE_AUTH=1\n" in (tmp_path / ".env").read_text()  # commented line untouched
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640
    assert env_file.read_key(p, "DISABLE_AUTH") == "0"
    assert os.listdir(tmp_path) == [".env"]  # no temp file left


def test_crlf_and_no_trailing_newline_are_kept(tmp_path):
    raw = b"A=1\r\nDISABLE_AUTH=0\r\nB=2"
    p = _write(tmp_path / ".env", raw)
    env_file.set_key(p, "DISABLE_AUTH", "1")
    assert (tmp_path / ".env").read_bytes() == b"A=1\r\nDISABLE_AUTH=1\r\nB=2"


@pytest.mark.parametrize("line, expected", [
    ('DISABLE_AUTH="1"', 'DISABLE_AUTH="0"'),
    ("DISABLE_AUTH='1'", "DISABLE_AUTH='0'"),
    ("DISABLE_AUTH = 1", "DISABLE_AUTH = 0"),
    ("  DISABLE_AUTH=1", "  DISABLE_AUTH=0"),
    ("export DISABLE_AUTH=1", "export DISABLE_AUTH=0"),
    ("DISABLE_AUTH=1  # dev box", "DISABLE_AUTH=0  # dev box"),
    ("DISABLE_AUTH=", "DISABLE_AUTH=0"),
    ("DISABLE_AUTH=true", "DISABLE_AUTH=0"),
])
def test_value_forms_are_rewritten_in_their_own_style(tmp_path, line, expected):
    p = _write(tmp_path / ".env", f"X=1\n{line}\nY=2\n".encode())
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert (tmp_path / ".env").read_text() == f"X=1\n{expected}\nY=2\n"
    assert env_file.read_key(p, "DISABLE_AUTH") == "0"


def test_similar_keys_are_not_matched(tmp_path):
    raw = "DISABLE_AUTH_X=1\nMY_DISABLE_AUTH=1\n#DISABLE_AUTH=1\n"
    p = _write(tmp_path / ".env", raw.encode())
    assert env_file.read_key(p, "DISABLE_AUTH") is None
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert (tmp_path / ".env").read_text() == raw + "DISABLE_AUTH=0\n"


def test_insert_right_after_the_auth_header(tmp_path):
    raw = "PORT=8080\n\n# === Auth ===\n# DISABLE_AUTH=1\nAUTH_TIMEOUT_MINUTES=30\n\n# === Other ===\nX=1\n"
    p = _write(tmp_path / ".env", raw.encode())
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert (tmp_path / ".env").read_text() == raw.replace(
        "# === Auth ===\n", "# === Auth ===\nDISABLE_AUTH=0\n")


def test_insert_after_auth_header_on_last_line_without_newline(tmp_path):
    p = _write(tmp_path / ".env", b"X=1\r\n# === Auth ===")
    env_file.set_key(p, "DISABLE_AUTH", "1")
    assert (tmp_path / ".env").read_bytes() == b"X=1\r\n# === Auth ===\r\nDISABLE_AUTH=1"


@pytest.mark.parametrize("raw, expected", [
    (b"PORT=8080\n# DISABLE_AUTH=1\n", b"PORT=8080\n# DISABLE_AUTH=1\nDISABLE_AUTH=0\n"),
    (b"PORT=8080", b"PORT=8080\nDISABLE_AUTH=0"),
    (b"A=1\r\nB=2\r\n", b"A=1\r\nB=2\r\nDISABLE_AUTH=0\r\n"),
    (b"", b"DISABLE_AUTH=0\n"),
])
def test_append_when_no_line_and_no_header(tmp_path, raw, expected):
    p = _write(tmp_path / ".env", raw)
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert (tmp_path / ".env").read_bytes() == expected


def test_missing_file_is_created_0600(tmp_path):
    p = str(tmp_path / ".env")
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert (tmp_path / ".env").read_text() == "DISABLE_AUTH=0\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_every_uncommented_duplicate_is_set(tmp_path):
    # Both loaders let the last line win: setting only the first would leave
    # the old value in force.
    p = _write(tmp_path / ".env", b"DISABLE_AUTH=1\nX=1\nDISABLE_AUTH=1\n")
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert (tmp_path / ".env").read_bytes() == b"DISABLE_AUTH=0\nX=1\nDISABLE_AUTH=0\n"


def test_unchanged_value_does_not_rewrite(tmp_path):
    p = _write(tmp_path / ".env", b"DISABLE_AUTH=0\n")
    before = os.stat(p).st_ino
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert os.stat(p).st_ino == before


def test_symlinked_env_writes_the_target(tmp_path):
    real = tmp_path / "real.env"
    _write(real, b"DISABLE_AUTH=1\n", 0o640)
    link = tmp_path / ".env"
    link.symlink_to(real)
    env_file.set_key(str(link), "DISABLE_AUTH", "0")
    assert link.is_symlink() and real.read_bytes() == b"DISABLE_AUTH=0\n"
    assert stat.S_IMODE(os.stat(real).st_mode) == 0o640


def test_non_utf8_bytes_survive(tmp_path):
    raw = b"NOTE=caf\xe9\nDISABLE_AUTH=1\n"
    p = _write(tmp_path / ".env", raw)
    env_file.set_key(p, "DISABLE_AUTH", "0")
    assert (tmp_path / ".env").read_bytes() == b"NOTE=caf\xe9\nDISABLE_AUTH=0\n"


@pytest.mark.parametrize("fail", ["replace", "fsync"])
def test_failed_write_leaves_the_file_intact(tmp_path, monkeypatch, fail):
    p = _write(tmp_path / ".env", SAMPLE.encode(), 0o640)

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(env_file.os, fail, boom)
    with pytest.raises(OSError):
        env_file.set_key(p, "DISABLE_AUTH", "0")
    monkeypatch.undo()
    assert (tmp_path / ".env").read_bytes() == SAMPLE.encode()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640
    assert os.listdir(tmp_path) == [".env"]


def test_unwritable_directory_raises_permission_error(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    p = _write(d / ".env", b"DISABLE_AUTH=1\n")
    os.chmod(d, 0o500)
    try:
        if os.access(d, os.W_OK):
            pytest.skip("running with privileges that ignore directory modes")
        with pytest.raises(PermissionError):
            env_file.set_key(p, "DISABLE_AUTH", "0")
    finally:
        os.chmod(d, 0o700)
    assert (d / ".env").read_bytes() == b"DISABLE_AUTH=1\n"


def test_multiline_value_refused(tmp_path):
    p = _write(tmp_path / ".env", b"DISABLE_AUTH=1\n")
    with pytest.raises(ValueError):
        env_file.set_key(p, "DISABLE_AUTH", "0\nEVIL=1")


@pytest.mark.parametrize("raw, expected", [
    ("DISABLE_AUTH=1\n", "1"),
    ('DISABLE_AUTH="yes"\n', "yes"),
    ("DISABLE_AUTH='0'\n", "0"),
    ("DISABLE_AUTH = true \n", "true"),
    ("DISABLE_AUTH=1 # inline\n", "1"),
    ("DISABLE_AUTH=\n", ""),
    ("export DISABLE_AUTH=1\n", "1"),
    ("DISABLE_AUTH=1\r\n", "1"),
    ("DISABLE_AUTH=1\nDISABLE_AUTH=0\n", "0"),  # last wins, like both loaders
    ("# DISABLE_AUTH=1\n", None),
    ("   #DISABLE_AUTH=1\n", None),
    ("OTHER=1\n", None),
])
def test_read_key(tmp_path, raw, expected):
    p = _write(tmp_path / ".env", raw.encode())
    assert env_file.read_key(p, "DISABLE_AUTH") == expected


def test_read_key_missing_or_unreadable_file(tmp_path):
    assert env_file.read_key(str(tmp_path / "nope.env"), "DISABLE_AUTH") is None
    assert env_file.read_key(str(tmp_path), "DISABLE_AUTH") is None  # a directory
