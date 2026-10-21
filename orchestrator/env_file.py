"""Read and rewrite single KEY=VALUE lines of the project .env (stdlib only).

The Monitor > Security password-protection switch keeps DISABLE_AUTH in
.env as its one source of truth, so a switch survives a restart:
ecosystem.config.cjs injects .env into the pm2 env, and config.py loads it
with python-dotenv (override=False).

Parsing matches both loaders for simple lines: blank lines and lines whose
first non-blank character is "#" are skipped; KEY is the text before the
first "=" (an optional "export " prefix is allowed); the value is stripped,
and surrounding matching quotes are removed. An unquoted value ends at an
inline " #" comment (python-dotenv). A repeated key: the last line wins.

set_key edits matching lines in place and keeps every other byte (other
lines, comments, blank lines, order, line endings, trailing newline). The
write is atomic: temp file in the same directory, original mode, fsync,
os.replace. File contents are never logged.

ENV_PATH is looked up at call time by callers (``env_file.ENV_PATH``), so
tests point it at a temp file and never touch the real .env.
"""

import os
import re
import stat
import tempfile

# Project root resolved the way config.py does: one level above this file.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")

_NEW_FILE_MODE = 0o600
_LINE_RE = re.compile(r"^(\s*(?:export\s+)?)([^=\s#][^=]*?)(\s*=\s*)(.*)$")
_UNQUOTED_RE = re.compile(r"^(.*?)(\s+#.*|\s*)$")
_AUTH_HEADER_RE = re.compile(r"^\s*#\s*=+\s*auth\s*=+\s*$", re.IGNORECASE)


def _split_lines(text: str) -> list[tuple[str, str]]:
    """[(body, eol)] where eol is "\\n", "\\r\\n" or "" (last line, no newline)."""
    out = []
    parts = text.split("\n")
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            if part:
                out.append((part, ""))
            break
        if part.endswith("\r"):
            out.append((part[:-1], "\r\n"))
        else:
            out.append((part, "\n"))
    return out


def _parse(body: str, name: str):
    """(prefix, quote, value, suffix) when body is an uncommented NAME= line,
    else None. prefix runs through the "=" and its spacing; suffix is any
    closing quote's trailer or an unquoted value's inline comment/whitespace."""
    m = _LINE_RE.match(body)
    if not m or m.group(2).strip() != name:
        return None
    prefix = m.group(1) + m.group(2) + m.group(3)
    rest = m.group(4)
    if rest[:1] in ("'", '"'):
        end = rest.find(rest[0], 1)
        if end > 0:
            return prefix, rest[0], rest[1:end], rest[end + 1:]
    u = _UNQUOTED_RE.match(rest)
    return prefix, "", u.group(1), u.group(2)


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "surrogateescape")


def read_key(path: str, name: str) -> str | None:
    """Value of NAME in the .env at path (last uncommented line wins), or
    None when the file is missing/unreadable or has no such line."""
    try:
        text = _read_text(path)
    except (OSError, ValueError):
        return None
    value = None
    for body, _eol in _split_lines(text):
        parsed = _parse(body, name)
        if parsed is not None:
            value = parsed[2]
    return value


def _render(text: str, name: str, value: str) -> str:
    lines = _split_lines(text)
    found = False
    out = []
    for body, eol in lines:
        parsed = _parse(body, name)
        if parsed is not None:
            prefix, quote, _old, suffix = parsed
            body = f"{prefix}{quote}{value}{quote}{suffix}"
            found = True
        out.append((body, eol))
    if found:
        return "".join(b + e for b, e in out)

    new_line = f"{name}={value}"
    crlf = any(e == "\r\n" for _, e in lines)
    nl = "\r\n" if crlf else "\n"
    for i, (body, eol) in enumerate(lines):
        if _AUTH_HEADER_RE.match(body):
            if not eol:  # header is the last line, without a newline
                lines[i] = (body, nl)
                lines.insert(i + 1, (new_line, ""))
            else:
                lines.insert(i + 1, (new_line, eol))
            return "".join(b + e for b, e in lines)
    if not lines:
        return new_line + "\n"
    if lines[-1][1]:
        return text + new_line + nl
    # No trailing newline: keep that style.
    return text + nl + new_line


def _atomic_write(path: str, data: bytes) -> None:
    target = os.path.realpath(path)
    directory = os.path.dirname(target) or "."
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = _NEW_FILE_MODE
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def set_key(path: str, name: str, value: str) -> None:
    """Set NAME=value in the .env at path, atomically.

    Every uncommented NAME= line gets the new value in place (indentation,
    "export ", spacing, quotes and inline comments kept). Commented lines
    such as "# NAME=1" are left alone. With no such line, one is inserted
    right after a "# === Auth ===" header, else appended at the end; a
    missing file is created (mode 0600). Raises OSError on failure, leaving
    the file as it was."""
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")
    try:
        text = _read_text(path)
    except FileNotFoundError:
        text = ""
    new_text = _render(text, name, value)
    if new_text == text and os.path.exists(path):
        return
    _atomic_write(path, new_text.encode("utf-8", "surrogateescape"))
