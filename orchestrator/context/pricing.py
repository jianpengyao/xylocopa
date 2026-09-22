"""Model pricing lookup and cost math.

USD per 1M tokens, Anthropic published rates (subject to change).
Last verified on 2026-09-22 against the Claude Code CLI's own baked-in
model table (v2.1.280, `pricing_tiers`), which is what actually bills.

Cache create has TWO sub-rates because Anthropic charges 1h ephemeral
cache writes at a higher rate than 5min:
  cache_create_5m = 1.25× input
  cache_create_1h = 2.00× input
JSONL `usage.cache_creation` exposes the split:
  ephemeral_5m_input_tokens, ephemeral_1h_input_tokens

Cache read price is 0.10× input regardless of which TTL was used to write,
with two exceptions that carry their own rate: Fable 5.1 reads at
$0.25/MTok (0.025×) and Opus 5.5 at $0.20/MTok (0.05×).
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Pricing — USD per 1M tokens.
# ---------------------------------------------------------------------------
PRICING: dict[str, dict[str, float]] = {
    # --- Fable 5.1 / 5 ($10/$50 — flagship) ---
    # 5.1 must stay an explicit entry: resolve_pricing's suffix-strip fallback
    # would hand it Fable 5's $1.00 cache_read, but 5.1 reads at $0.25/MTok.
    "claude-fable-5-1":  {"input": 10.00, "cache_create_5m": 12.50, "cache_create_1h": 20.00, "cache_read": 0.25, "output": 50.00},
    "claude-fable-5":    {"input": 10.00, "cache_create_5m": 12.50, "cache_create_1h": 20.00, "cache_read": 1.00, "output": 50.00},
    # --- Opus 5.5 ($4/$20 — cheapest Opus tier, cache read $0.20) ---
    # Must stay an explicit entry: resolve_pricing's suffix-strip fallback
    # would hand it Opus 5's $5/$25 rates and a 2.5x cache_read.
    "claude-opus-5-5":   {"input": 4.00,  "cache_create_5m": 5.00,  "cache_create_1h":  8.00, "cache_read": 0.20, "output": 20.00},
    # --- Opus 4.5+ / Opus 5 ($5/$25 — previous generation) ---
    "claude-opus-5":     {"input": 5.00,  "cache_create_5m": 6.25,  "cache_create_1h": 10.00, "cache_read": 0.50, "output": 25.00},
    "claude-opus-4-8":   {"input": 5.00,  "cache_create_5m": 6.25,  "cache_create_1h": 10.00, "cache_read": 0.50, "output": 25.00},
    "claude-opus-4-7":   {"input": 5.00,  "cache_create_5m": 6.25,  "cache_create_1h": 10.00, "cache_read": 0.50, "output": 25.00},
    "claude-opus-4-6":   {"input": 5.00,  "cache_create_5m": 6.25,  "cache_create_1h": 10.00, "cache_read": 0.50, "output": 25.00},
    "claude-opus-4-5":   {"input": 5.00,  "cache_create_5m": 6.25,  "cache_create_1h": 10.00, "cache_read": 0.50, "output": 25.00},
    # --- Opus 4 / 4.1 (legacy $15/$75) ---
    "claude-opus-4-1":   {"input": 15.00, "cache_create_5m": 18.75, "cache_create_1h": 30.00, "cache_read": 1.50, "output": 75.00},
    "claude-opus-4":     {"input": 15.00, "cache_create_5m": 18.75, "cache_create_1h": 30.00, "cache_read": 1.50, "output": 75.00},
    # --- Sonnet 5 ($2/$10 — its own tier, cache read $0.20) ---
    "claude-sonnet-5":   {"input": 2.00,  "cache_create_5m": 2.50,  "cache_create_1h":  4.00, "cache_read": 0.20, "output": 10.00},
    # --- Sonnet 4.x ($3/$15) ---
    "claude-sonnet-4-6": {"input": 3.00,  "cache_create_5m": 3.75,  "cache_create_1h":  6.00, "cache_read": 0.30, "output": 15.00},
    "claude-sonnet-4-5": {"input": 3.00,  "cache_create_5m": 3.75,  "cache_create_1h":  6.00, "cache_read": 0.30, "output": 15.00},
    "claude-sonnet-4":   {"input": 3.00,  "cache_create_5m": 3.75,  "cache_create_1h":  6.00, "cache_read": 0.30, "output": 15.00},
    # --- Haiku 4.5 ($1/$5) ---
    "claude-haiku-4-5":  {"input": 1.00,  "cache_create_5m": 1.25,  "cache_create_1h":  2.00, "cache_read": 0.10, "output":  5.00},
    # --- Haiku 3.5 ---
    "claude-haiku-3-5":  {"input": 0.80,  "cache_create_5m": 1.00,  "cache_create_1h":  1.60, "cache_read": 0.08, "output":  4.00},
}
DEFAULT_PRICING = {"input": 3.00, "cache_create_5m": 3.75, "cache_create_1h": 6.00, "cache_read": 0.30, "output": 15.00}


def resolve_pricing(model: str | None) -> dict[str, float]:
    """Return per-million-token rates for `model`, falling back to defaults.

    Handles version-suffixed IDs by stripping the trailing `-YYYYMMDD` and
    retrying. Unknown models get DEFAULT_PRICING.
    """
    if not model:
        return DEFAULT_PRICING
    if model in PRICING:
        return PRICING[model]
    base = model.rsplit("-", 1)[0]
    return PRICING.get(base, DEFAULT_PRICING)


def compute_cost(usage: dict[str, int], model: str | None) -> float:
    """Compute USD cost for a single Anthropic `usage` block.

    `usage` may carry the canonical fields:
      input_tokens, output_tokens, cache_read_input_tokens
    Plus EITHER the split fields (preferred):
      cache_creation_5m_tokens, cache_creation_1h_tokens
    OR the legacy rollup:
      cache_creation_input_tokens   (treated as all-5m for back-compat)

    Missing keys are treated as 0. Result is total USD (NOT rounded).
    """
    p = resolve_pricing(model)
    # Prefer the explicit 5m/1h split when available; fall back to the
    # legacy rollup (assume all-5m, the cheaper rate — preserves prior
    # cost numbers for records produced before the split was tracked).
    five_m = usage.get("cache_creation_5m_tokens")
    one_h = usage.get("cache_creation_1h_tokens")
    if five_m is None and one_h is None:
        five_m = usage.get("cache_creation_input_tokens", 0)
        one_h = 0
    five_m = five_m or 0
    one_h = one_h or 0
    return (
        usage.get("input_tokens", 0) * p["input"]
        + five_m * p["cache_create_5m"]
        + one_h * p["cache_create_1h"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
        + usage.get("output_tokens", 0) * p["output"]
    ) / 1_000_000
