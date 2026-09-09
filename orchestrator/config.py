"""Xylocopa configuration — loaded from environment variables."""

import os

# Project root: one level up from this file (cc-orchestrator/)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load .env so the backend works regardless of how it's started
# (run.sh, bare uvicorn, systemd, etc.)
from dotenv import load_dotenv

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)

# run.sh maps HOST_PROJECTS_DIR → PROJECTS_DIR; replicate that here
# so direct starts work identically.
if not os.getenv("PROJECTS_DIR") and os.getenv("HOST_PROJECTS_DIR"):
    os.environ["PROJECTS_DIR"] = os.environ["HOST_PROJECTS_DIR"]


def _resolve(path: str) -> str:
    """Resolve a path: absolute paths stay as-is, relative paths are
    resolved against the project root (not the CWD)."""
    if os.path.isabs(path):
        return path
    return os.path.join(_PROJECT_ROOT, path)


# HTTP server port — shared by the FastAPI bind (main.py) and hook URL
# construction (routers/agents.py).  mcp_server.py reads the PORT env var
# itself by design (it avoids importing config.py).
PORT = int(os.getenv("PORT", "8080"))

# Worker config
MAX_CONCURRENT_WORKERS = int(os.getenv("MAX_CONCURRENT_WORKERS", "5"))
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "1800"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
MAX_IDLE_AGENTS = int(os.getenv("MAX_IDLE_AGENTS", "20"))

# Default Claude model — single source of truth; also the
# Project.default_model column default (models.py) and the project
# schema default (schemas.py).
DEFAULT_CLAUDE_MODEL = "claude-opus-5"
CC_MODEL = os.getenv("CC_MODEL", DEFAULT_CLAUDE_MODEL)

# Model for internal claude -p summarization jobs (agent insights, retry
# summaries, PROGRESS.md summaries, insight dedup). Opus by explicit user
# preference (2026-09-01): summary quality over sonnet's small per-job
# saving. Keep an EXPLICIT model id here — never the bare "sonnet"/"opus"
# CLI alias: aliases resolve through the inherited ANTHROPIC_DEFAULT_*_MODEL
# env vars, which once mis-resolved to a 1M-context variant and failed
# insight generation with "Usage credits required for 1M context".
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "claude-opus-5")

# Attention assistant's interactive turns (bubble chat + compile) — its own
# knob because the user is watching a spinner here, unlike the background
# digest jobs (run_prompt action) which stay on SUMMARY_MODEL. Sonnet 5 by
# MEASUREMENT, not tier intuition: on the real chat prompt (9KB, registry +
# context) sonnet-5 ran 4.1s/5.2s vs haiku-4.5's 11.0s/23.6s — the newer
# serving stack beats the smaller model, and haiku pads output with
# markdown fences. Re-benchmark before "downgrading for speed" again.
ATTENTION_MODEL = os.getenv("ATTENTION_MODEL", "claude-sonnet-5")

# One-off orb character designs (user-invoked, quality over latency) —
# a strong model per explicit request; the fable id also works here.
ATTENTION_CHARGEN_MODEL = os.getenv("ATTENTION_CHARGEN_MODEL", "claude-opus-5")

# Valid model names — keep in sync with frontend MODEL_OPTIONS
VALID_MODELS = {
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
}

# Claude CLI binary
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")

# Claude home directory (single source of truth for all ~/.claude paths)
CLAUDE_HOME = os.path.expanduser(os.getenv("CLAUDE_HOME", "~/.claude"))

# Claude history file (all past conversations)
CLAUDE_HISTORY_PATH = os.getenv("CLAUDE_HISTORY_PATH", os.path.join(CLAUDE_HOME, "history.jsonl"))

# Claude credentials file (for OAuth token usage queries)
CLAUDE_CREDENTIALS_PATH = os.path.expanduser(
    os.getenv("CLAUDE_CREDENTIALS_PATH", os.path.join(CLAUDE_HOME, ".credentials.json"))
)

# Projects directory (host path)
PROJECTS_DIR = os.getenv("PROJECTS_DIR", os.getenv("HOST_PROJECTS_DIR", ""))

# Voice
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
VOICE_REFINE_MODEL = os.getenv("VOICE_REFINE_MODEL", "gpt-4o-mini")

# Backup
BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "0").strip().lower() in ("1", "true", "yes")
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
MAX_BACKUPS = int(os.getenv("MAX_BACKUPS", "30"))

# Dropbox sync — token.json (0600), config.json and state.db live here.
DROPBOX_SYNC_DIR = _resolve(os.getenv("DROPBOX_SYNC_DIR", "data/dropbox"))

# Project-registered Dropbox app (PKCE public client; the key is not a secret).
# Filled in once the "xylocopa" app exists; empty means "not configured" until then.
DROPBOX_DEFAULT_APP_KEY = "zw5krsh35r7bxpr"
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "") or DROPBOX_DEFAULT_APP_KEY
# Fixed return page registered on the project app. Users who bring their own app can
# self-host a copy and point this at it, or leave it empty to use direct redirects.
DROPBOX_RELAY_URL = os.getenv("DROPBOX_RELAY_URL", "https://jianpengyao.github.io/xylocopa/oauth/dropbox/")
DROPBOX_USING_DEFAULT_APP = not os.getenv("DROPBOX_APP_KEY", "")

# Auth
AUTH_TIMEOUT_MINUTES = int(os.getenv("AUTH_TIMEOUT_MINUTES", "30"))

# Database
DB_PATH = _resolve(os.getenv("DB_PATH", "data/orchestrator.db"))

# Logs and backups directories
LOG_DIR = _resolve(os.getenv("LOG_DIR", "logs"))
BACKUP_DIR = _resolve(os.getenv("BACKUP_DIR", "backups"))  # also configurable via .env BACKUP_DIR=/your/path

# Display files (per-agent JSONL for Phase 2 message sync)
DISPLAY_DIR = _resolve(os.getenv("DISPLAY_DIR", "data/display"))

# Delay (seconds) before waking sync after a hook returns, giving Claude
# time to flush the JSONL entry to disk.
JSONL_FLUSH_DELAY = float(os.getenv("JSONL_FLUSH_DELAY", "0.15"))

# Session cache
SESSION_CACHE_INTERVAL = int(os.getenv("SESSION_CACHE_INTERVAL", "30"))

# Project configs
PROJECT_CONFIGS_PATH = _resolve(os.getenv("PROJECT_CONFIGS_PATH", "project-configs"))

# VAPID (Web Push)
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:xylocopa@example.com")

# Uploads — startup migration moves legacy ~/.agenthive/uploads to the new
# location (see migrate_legacy_paths() in main.py).
UPLOADS_DIR = os.path.expanduser(os.getenv("UPLOADS_DIR", "~/.xylocopa/uploads"))

# CORS
CORS_ORIGINS = [
    o.strip() for o in
    os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8080").split(",")
    if o.strip()
]
