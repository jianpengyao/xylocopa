"""Logging configuration — file + console with size-bounded rotation."""

import logging
import os
import re
from logging.handlers import RotatingFileHandler

# Resolve LOG_DIR relative to project root (one level up from this file)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_raw_log_dir = os.getenv("LOG_DIR", "logs")
LOG_DIR = _raw_log_dir if os.path.isabs(_raw_log_dir) else os.path.join(_PROJECT_ROOT, _raw_log_dir)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Size-bounded rotation: 50 MB per file × 4 retained = ~200 MB total on disk.
# Replaces the old daily-time rotation which had no size cap — chatty days
# (mass-stop events, hook-tool storms) used to push the active log to 50 MB+
# and the kept-7-days backlog to half a GB.
_LOG_MAX_BYTES = 50 * 1024 * 1024
_LOG_BACKUP_COUNT = 4


# Credentials that ride in URLs and would otherwise land in uvicorn's access
# and WebSocket lines: media tokens / WS tickets (?token=, ?ticket=), preview
# tokens and port-proxy signatures (/api/preview/t|p/<seg>), probe trigger
# tokens, and the Dropbox OAuth code/state on its callback.
_REDACT_PATTERNS = (
    re.compile(r'([?&](?:token|ticket)=)[^&\s"]+'),
    re.compile(r'(/api/preview/[tp]/)[^/\s"?]+'),
    re.compile(r'(/api/probe-trigger/)[^/\s"?]+'),
)
_DROPBOX_CALLBACK = "/api/dropbox/callback"
_DROPBOX_PARAMS = re.compile(r'([?&](?:code|state)=)[^&\s"]+')


def redact_secrets(text: str) -> str:
    """Replace URL-borne credentials in a log string with <redacted>."""
    for rx in _REDACT_PATTERNS:
        text = rx.sub(r"\1<redacted>", text)
    if _DROPBOX_CALLBACK in text:
        text = _DROPBOX_PARAMS.sub(r"\1<redacted>", text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Rewrites str args (or the bare message) of a record. Never drops one."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(redact_secrets(a) if isinstance(a, str) else a
                                    for a in record.args)
            elif not record.args and isinstance(record.msg, str):
                record.msg = redact_secrets(record.msg)
        except Exception:
            pass
        return True


# uvicorn logs request lines on these two (access: HTTP; error: WebSocket
# handshakes). Its dictConfig runs before main is imported, so attaching here
# survives it.
_REDACTED_LOGGERS = ("uvicorn.access", "uvicorn.error")


def install_redaction() -> None:
    for name in _REDACTED_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactSecretsFilter) for f in lg.filters):
            lg.addFilter(RedactSecretsFilter())


def setup_logging():
    """Configure root logger with console + size-rotated file handlers."""
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # File handler — size-based rotation, 4 backups (50 MB each = ~200 MB cap)
    log_file = os.path.join(LOG_DIR, "orchestrator.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    install_redaction()

    logging.info("Logging configured: level=%s, file=%s, max=%dMB×%d",
                 LOG_LEVEL, log_file,
                 _LOG_MAX_BYTES // (1024 * 1024), _LOG_BACKUP_COUNT + 1)


def save_worker_log(task_id: str, log_content: str):
    """Persist a worker's stream log to the logs volume."""
    worker_log_dir = os.path.join(LOG_DIR, "workers")
    os.makedirs(worker_log_dir, exist_ok=True)
    path = os.path.join(worker_log_dir, f"worker-{task_id}.json")
    with open(path, "w") as f:
        f.write(log_content)


def get_recent_logs(level: str = "", limit: int = 100) -> list[str]:
    """Read recent log lines from the orchestrator log file, optionally filtered by level.

    Tail-reads up to a 2 MB window from the end of the file so a polled
    /api/logs endpoint doesn't slurp the whole 50 MB rotated log into RAM
    every request.
    """
    log_file = os.path.join(LOG_DIR, "orchestrator.log")
    if not os.path.isfile(log_file):
        return []

    _TAIL_WINDOW = 2 * 1024 * 1024  # 2 MB tail — enough for the most recent ~thousands of lines
    size = os.path.getsize(log_file)
    with open(log_file, "rb") as f:
        if size > _TAIL_WINDOW:
            f.seek(size - _TAIL_WINDOW)
            f.readline()  # drop the partial first line
        raw = f.read().decode("utf-8", errors="replace")
    lines = raw.splitlines()

    if level:
        level_upper = level.upper()
        lines = [l for l in lines if f"[{level_upper}]" in l]

    return [l.rstrip() for l in lines[-limit:]]
