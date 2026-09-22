"""Shared constants for the context package.

Centralizes per-model context window caps, MCP server token estimates,
the system overhead constant, and the heuristic char/token ratio so that
the rest of the package can stay free of magic numbers.
"""
from __future__ import annotations


# Per-model context window caps. Defaults to 200K for any model not listed.
# Opus 4.7 has a 1M native window — see the CHANGELOG entry "Fixed Opus 4.7
# sessions showing inflated /context percentages".
MODEL_LIMITS: dict[str, int] = {
    "claude-fable-5-1": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-7-20251015": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-5": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}
DEFAULT_LIMIT = 200_000


# Per-server token estimate. Real tool descriptions vary widely — we list
# rough ranges for known servers. Unknown servers get a conservative default.
# Numbers come from observed token costs in CC sessions, not exact lookups.
MCP_SERVER_ESTIMATES: dict[str, int] = {
    "xylocopa": 6_500,    # ~30 tools (project_*, task_*, session_*, agent_*, system_*)
    "filesystem": 4_500,
    "github": 12_000,
    "playwright": 18_000,
    "puppeteer": 14_000,
    "memory": 1_500,
    "fetch": 1_200,
    "sqlite": 2_500,
    "postgres": 3_000,
    "git": 5_000,
    "slack": 8_000,
    "linear": 6_000,
}
MCP_DEFAULT_ESTIMATE = 5_000


# CC's base system prompt (~3k tokens) + ~50 built-in tool descriptions
# (~10k tokens). Roughly constant per CLI version; we treat it as a single
# unsplittable bucket since the user can't optimize it.
SYSTEM_OVERHEAD_BASE = 13_000


# cl100k_base averages ~3.5 chars/token for English/code, ~1.6 for CJK.
# We use a single ratio because static categories (system files, tool
# definitions) are predominantly ASCII/code. Total error gets absorbed by
# the Messages remainder.
CHARS_PER_TOKEN = 3.5
