// Status colors: EXECUTING follows the theme accent token, and the green/red
// families ride the per-theme ok/danger tokens (branded palettes supply their
// own native hues — Nord aurora, Solarized, Everforest, GitHub-Dimmed). The
// rarer fixed hues (orange/violet/blue/…) keep the paired 600/dark:400
// convention — the bare 400 grades washed out to 1.7-2.8:1 on light bases.
export const STATUS_COLORS = {
  PENDING: "bg-gray-500",
  IDLE: "bg-ok",
  EXECUTING: "bg-accent",
  COMPLETED: "bg-ok",
  FAILED: "bg-danger",
  TIMEOUT: "bg-orange-500",
  CANCELLED: "bg-gray-600",
};

export const STATUS_TEXT_COLORS = {
  PENDING: "text-dim",
  IDLE: "text-ok",
  EXECUTING: "text-accent",
  COMPLETED: "text-ok",
  FAILED: "text-danger",
  TIMEOUT: "text-orange-600 dark:text-orange-400",
  CANCELLED: "text-faint",
};

export const AGENT_STATUS_COLORS = {
  STARTING: "bg-gray-500",
  IDLE: "bg-ok",
  EXECUTING: "bg-accent",
  ERROR: "bg-danger",
  STOPPED: "bg-gray-600",
};

export const AGENT_STATUS_TEXT_COLORS = {
  STARTING: "text-dim",
  IDLE: "text-ok",
  EXECUTING: "text-accent",
  ERROR: "text-danger",
  STOPPED: "text-faint",
};

export const MODE_COLORS = {
  INTERVIEW: "bg-violet-500/20 text-violet-600 dark:text-violet-400 border border-violet-500/40",
  AUTO: "ok-tint-20 text-ok border ok-edge-40",
};

export const AGENT_MODES = [
  { value: "AUTO", label: "Auto" },
];

export const MODEL_OPTIONS = [
  { value: "claude-fable-5-1", label: "Fable 5.1" },
  { value: "claude-fable-5", label: "Fable 5" },
  { value: "claude-opus-5-5", label: "Opus 5.5" },
  { value: "claude-opus-5", label: "Opus 5" },
  { value: "claude-opus-4-6", label: "Opus 4.6" },
  { value: "claude-sonnet-5", label: "Sonnet 5" },
  { value: "claude-haiku-4-5-20251001", label: "Haiku 4.5" },
];

// Default model for new tasks — the latest Opus, deliberately NOT
// MODEL_OPTIONS[0]: the Fable tier leads the picker visually but is opt-in
// (~2x cost), so new tasks default to Opus 5.5 unless the user picks Fable.
export const DEFAULT_MODEL = "claude-opus-5-5";

// Models no longer offered in the picker but still valid on existing
// agents/tasks — keeps their tags rendering with proper labels.
const LEGACY_MODEL_LABELS = {
  "claude-opus-4-8": "Opus 4.8",
  "claude-opus-4-7": "Opus 4.7",
};

/** Map full model ID to short display name. */
export function modelDisplayName(modelId) {
  if (!modelId) return null;
  const opt = MODEL_OPTIONS.find((m) => m.value === modelId);
  if (opt) return opt.label;
  if (LEGACY_MODEL_LABELS[modelId]) return LEGACY_MODEL_LABELS[modelId];
  // Fallback: strip "claude-" prefix and date suffixes
  return modelId
    .replace(/^claude-/, "")
    .replace(/-\d{8}$/, "")
    .split("-")
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join(" ");
}

/** Project badge classes — the shared accent chip.
 *
 * Used to hash the name into a fixed Tailwind palette, which made the same
 * project change hue between routes (accent chip on the agents list, hashed
 * violet on task-detail) and picked colors foreign to every non-stock
 * palette. One project = one look = the theme's accent chip. If per-project
 * identity colors ever return, they need per-theme mapping, not literals.
 */
export function projectBadgeColor() {
  return "accent-tint-15 text-accent";
}

export const POLL_INTERVAL = 5000;

// ---- Timing constants (ms) ----

/** Polling interval when agent is active (EXECUTING/IDLE). */
export const POLL_ACTIVE_INTERVAL = 3000;

/** Polling interval when agent is idle. */
export const POLL_IDLE_INTERVAL = 10000;

/** Duration to show "Copied" toast. */
export const COPY_TOAST_DURATION = 1500;

/** Duration to show transient error toasts. */
export const ERROR_TOAST_DURATION = 4000;

/** Duration to show success/info toasts. */
export const TOAST_DURATION = 3000;

/** Escape key cooldown to match backend rate limit. */
export const ESCAPE_COOLDOWN = 2500;

/** Long-press duration for touch actions. */
export const LONG_PRESS_DELAY = 500;

/** Double-tap detection window. */
export const DOUBLE_TAP_WINDOW = 350;

/** Scroll-save debounce delay. */
export const SCROLL_SAVE_DEBOUNCE = 200;

/**
 * Delay before re-reading the DB after triggering wake-sync when we
 * know only a small burst of JSONL was just written (e.g. ESC → CLI
 * writes a "Request interrupted" marker). Mirrors the backend
 * JSONL_FLUSH_DELAY env var (default 0.15s).
 */
export const JSONL_FLUSH_DELAY_MS = 150;

/**
 * Delay before re-reading the DB after a user-initiated manual refresh
 * (the in-chat sync button, pull-to-refresh). Larger than the flush
 * delay because the sync loop may be importing a long tail of JSONL.
 */
export const SYNC_SETTLE_DELAY = 800;

/**
 * Delay before reloading the agent list after a global wake-sync-all +
 * scan. Bigger still because every agent's sync loop is competing.
 */
export const SYNC_SETTLE_DELAY_GLOBAL = 1000;

/** Lightbox swipe navigation threshold (px). */
export const SWIPE_THRESHOLD = 80;

/** Lightbox dismiss swipe threshold (px). */
export const DISMISS_THRESHOLD = 100;

/** Lightbox double-tap detection window (ms). */
export const LIGHTBOX_DOUBLE_TAP_WINDOW = 300;

/** Lightbox double-tap max distance (px). */
export const LIGHTBOX_DOUBLE_TAP_DIST = 30;

/** Max zoom scale for lightbox. */
export const MAX_ZOOM_SCALE = 5;

// ---- Task v2 ----

export const TASK_STATUS_COLORS = {
  INBOX: "bg-blue-500",
  PLANNING: "bg-violet-500",
  PENDING: "bg-gray-500",
  EXECUTING: "bg-accent animate-pulse",
  REVIEW: "bg-amber-500",
  MERGING: "bg-purple-500",
  CONFLICT: "bg-danger",
  COMPLETE: "bg-ok",
  REJECTED: "bg-orange-500",
  CANCELLED: "bg-gray-600",
  FAILED: "bg-danger",
  TIMEOUT: "bg-orange-500",
};

export const TASK_STATUS_TEXT_COLORS = {
  INBOX: "text-blue-600 dark:text-blue-400",
  PLANNING: "text-violet-600 dark:text-violet-400",
  PENDING: "text-dim",
  EXECUTING: "text-accent",
  REVIEW: "text-amber-600 dark:text-amber-400",
  MERGING: "text-purple-600 dark:text-purple-400",
  CONFLICT: "text-danger",
  COMPLETE: "text-ok",
  REJECTED: "text-orange-600 dark:text-orange-400",
  CANCELLED: "text-faint",
  FAILED: "text-danger",
  TIMEOUT: "text-orange-600 dark:text-orange-400",
};

export const TASK_PERSPECTIVE_TABS = [
  { key: "INBOX", label: "Inbox" },
  { key: "PLANNING", label: "Planning" },
  { key: "EXECUTING", label: "Executing" },
  { key: "REVIEW", label: "Review" },
  { key: "DONE", label: "Done" },
];

// ---- Agent helpers ----

/** Map agent status to BotIcon visual state. */
export function agentBotState(status) {
  if (status === "EXECUTING") return "running";
  if (status === "SYNCING") return "running";
  if (status === "ERROR") return "error";
  if (status === "IDLE") return "completed";
  if (status === "STOPPED") return "idle";
  return "idle";
}

/** Check if system health object indicates all systems OK. */
export function isSystemHealthy(health) {
  return health && health.status === "ok" && health.db === "ok" && health.claude_cli === "ok";
}
