// Theme palette registry + custom-theme plumbing.
//
// A palette = a `theme-<id>` class on <html> layered over a light/dark base
// (dark palettes also carry `.dark` so Tailwind `dark:` variants keep
// working). Preset token values live in index.css; this module only knows
// each preset's id, base, and preview swatches. The "custom" palette is
// user-authored: 6 core colors + base are stored in localStorage, the full
// ~20-token set is derived here, compiled to CSS, and injected as a
// <style id="xy-custom-theme"> that sits BEFORE the bundled stylesheet
// (html.theme-custom at (0,1,1) beats .dark/:root by specificity, while
// html.eink / html.no-glass — same specificity, later source — still win).
//
// Storage:
//   xylocopa-theme        — base mode "light" | "dark" (pre-existing key)
//   xy:palette-light      — palette id chosen for the light base
//   xy:palette-dark       — palette id chosen for the dark base
//   xy:custom-theme       — JSON {base, colors:{page,surface,heading,body,edge}}
//   xy:custom-theme-css   — derived CSS, injected pre-paint by index.html
//   xy:themecolor-<base>  — active page color, for the pre-paint meta tag
//
// The sun/moon toggle keeps its meaning: it flips between the palette you
// chose for light and the one you chose for dark.

const PALETTE_KEY = (base) => `xy:palette-${base}`;
const CUSTOM_KEY = "xy:custom-theme";
const CUSTOM_CSS_KEY = "xy:custom-theme-css";
const META_COLOR_KEY = (base) => `xy:themecolor-${base}`;
export const THEME_EVENT = "xy:theme-changed";

// `core` mirrors the preset's page/surface/heading/body/edge tokens in
// index.css — it seeds the custom editor when starting from that preset.
// Grouped light-first, then dark, for the picker grid.
export const PRESETS = [
  // "light" is the bare-:root palette (no theme-* class) — since the Light
  // preset was retired, :root carries the original vivid stock look.
  { id: "light", name: "Vivid", base: "light",
    preview: { page: "#ffffff", surface: "#f6f7f8", text: "#06b6d4" },
    core: { page: "#ffffff", surface: "#f6f7f8", heading: "#222222", body: "#374151", edge: "#d1d5db", bubble: "#0891b2" } },
  { id: "ash", name: "Ash", base: "light",
    preview: { page: "#f2f3f5", surface: "#ffffff", text: "#1c1e21" },
    core: { page: "#f2f3f5", surface: "#ffffff", heading: "#1c1e21", body: "#3f4247", edge: "#d9dce0", bubble: "#135b84" } },
  { id: "silver", name: "Silver", base: "light",
    preview: { page: "#c2c4c7", surface: "#d6d8db", text: "#202124" },
    core: { page: "#c2c4c7", surface: "#d6d8db", heading: "#202124", body: "#383b3e", edge: "#a5a7ab", bubble: "#45484d" } },
  { id: "clay", name: "Clay", base: "light",
    preview: { page: "#cac5be", surface: "#ddd9d2", text: "#26221f" },
    core: { page: "#cac5be", surface: "#ddd9d2", heading: "#26221f", body: "#403a35", edge: "#aaa49c", bubble: "#6f4e37" } },
  { id: "solarized-light", name: "Solarized Light", base: "light",
    preview: { page: "#fdf6e3", surface: "#f3ecd9", text: "#073642" },
    core: { page: "#fdf6e3", surface: "#f3ecd9", heading: "#073642", body: "#586e75", edge: "#d5cdb4", bubble: "#1f74b0" } },
  { id: "nord-light", name: "Nord Light", base: "light",
    preview: { page: "#e9eef6", surface: "#e0e7f1", text: "#2e3440" },
    core: { page: "#e9eef6", surface: "#e0e7f1", heading: "#2e3440", body: "#434c5e", edge: "#bcc8d9", bubble: "#527099" } },
  { id: "dark", name: "Dark", base: "dark",
    preview: { page: "#030712", surface: "#111827", text: "#f3f4f6" },
    core: { page: "#030712", surface: "#111827", heading: "#f3f4f6", body: "#d1d5db", edge: "#374151", bubble: "#155e75" } },
  { id: "graphite", name: "Graphite", base: "dark",
    preview: { page: "#22272e", surface: "#2d333b", text: "#adbac7" },
    core: { page: "#22272e", surface: "#2d333b", heading: "#c5d1de", body: "#adbac7", edge: "#444c56", bubble: "#135b84" } },
  { id: "soft-dark", name: "Soft Dark", base: "dark",
    preview: { page: "#17181c", surface: "#1e2024", text: "#e8e6e3" },
    core: { page: "#17181c", surface: "#1e2024", heading: "#e8e6e3", body: "#c9c7c3", edge: "#383b41", bubble: "#375663" } },
  { id: "solarized-dark", name: "Solarized Dark", base: "dark",
    preview: { page: "#002b36", surface: "#073642", text: "#aebcba" },
    core: { page: "#002b36", surface: "#073642", heading: "#aebcba", body: "#90a2a4", edge: "#29525e", bubble: "#135b84" } },
  { id: "nord", name: "Nord", base: "dark",
    preview: { page: "#2e3440", surface: "#3b4252", text: "#eceff4" },
    core: { page: "#2e3440", surface: "#3b4252", heading: "#eceff4", body: "#d8dee9", edge: "#4c566a", bubble: "#4a6485" } },
  { id: "everforest", name: "Everforest", base: "dark",
    preview: { page: "#272e33", surface: "#2d353b", text: "#d3c6aa" },
    core: { page: "#272e33", surface: "#2d353b", heading: "#d3c6aa", body: "#b9ad93", edge: "#4a555b", bubble: "#3a515d" } },
];

// Seed values for the custom editor, per base (= the default palettes).
export const CUSTOM_SEEDS = {
  light: { page: "#ffffff", surface: "#f6f7f8", heading: "#222222", body: "#374151", edge: "#d1d5db", bubble: "#0891b2" },
  dark:  { page: "#030712", surface: "#111827", heading: "#f3f4f6", body: "#d1d5db", edge: "#374151", bubble: "#155e75" },
};

export const CUSTOM_COLOR_FIELDS = [
  { key: "page", label: "Background" },
  { key: "surface", label: "Card" },
  { key: "heading", label: "Heading text" },
  { key: "body", label: "Body text" },
  { key: "edge", label: "Border" },
  { key: "bubble", label: "User bubble" },
];

/* ── color math ── */

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex([r, g, b]) {
  return "#" + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
}

// Linear mix of two hex colors; t=0 → a, t=1 → b.
function mix(a, b, t) {
  const ra = hexToRgb(a), rb = hexToRgb(b);
  if (!ra || !rb) return a;
  return rgbToHex(ra.map((v, i) => v + (rb[i] - v) * t));
}

function rgba(hex, alpha) {
  const c = hexToRgb(hex);
  return c ? `rgba(${c[0]},${c[1]},${c[2]},${alpha})` : hex;
}

// Space-separated channels for rgb(var(--x-rgb) / a) token consumers.
function channels(hex) {
  const c = hexToRgb(hex);
  return c ? `${c[0]} ${c[1]} ${c[2]}` : "0 0 0";
}

// Perceived brightness (YIQ); > 150 means dark ink reads better on it.
function isLightColor(hex) {
  const c = hexToRgb(hex);
  return c ? (c[0] * 299 + c[1] * 587 + c[2] * 114) / 1000 > 150 : false;
}

/* ── custom theme ── */

export function hasCustomConfig() {
  try {
    return localStorage.getItem(CUSTOM_KEY) !== null;
  } catch {
    return false;
  }
}

// Custom config seeded from a preset's core colors — the editor starts
// from what the user is currently looking at, not from the stock palette.
export function customConfigFromPreset(id) {
  const p = PRESETS.find((x) => x.id === id) || PRESETS[0];
  return { base: p.base, colors: { ...p.core } };
}

export function getCustomConfig() {
  try {
    const raw = JSON.parse(localStorage.getItem(CUSTOM_KEY) || "null");
    if (raw && (raw.base === "light" || raw.base === "dark") && raw.colors) {
      const colors = { ...CUSTOM_SEEDS[raw.base] };
      for (const f of CUSTOM_COLOR_FIELDS) {
        if (hexToRgb(raw.colors[f.key])) colors[f.key] = raw.colors[f.key].toLowerCase();
      }
      return { base: raw.base, colors };
    }
  } catch { /* corrupt JSON → fall through to seed */ }
  return { base: "dark", colors: { ...CUSTOM_SEEDS.dark } };
}

// Derive the full token set from the 5 core colors. Mixing pole is white on
// dark bases / black on light ones, so "elevated" always moves away from the
// page and text steps always fade toward it.
export function deriveCustomTokens({ base, colors }) {
  const { page, surface, heading, body, edge, bubble } = colors;
  const pole = base === "dark" ? "#ffffff" : "#000000";
  const glass = mix(surface, pole, 0.04);
  const glassNav = mix(surface, pole, 0.08);
  // Messenger convention: light bubbles carry dark ink; dark bubbles on a
  // light base carry pure white (iMessage); dark bubbles on a dark base
  // carry OFF-white — the theme's body color pushed toward white
  // (WhatsApp-dark #E9EDEF-style), brighter than body, softer than #fff.
  const bubbleInk = isLightColor(bubble)
    ? "#1f2937"
    : base === "dark" ? mix(body, "#ffffff", 0.35) : "#ffffff";
  // Accent (tags, links, inline code) rides the bubble hue: lightened on
  // dark bases so it reads as text, used near-directly on light ones.
  const accent = base === "dark" ? mix(bubble, "#ffffff", 0.45)
    : isLightColor(bubble) ? mix(bubble, "#000000", 0.3) : bubble;
  // Ink for FILLED accent surfaces (FAB, filter pills): pale dark-base
  // accents take the page ink, dark light-base accents take white.
  const accentInk = base === "dark" ? page
    : isLightColor(accent) ? "#1f2937" : "#ffffff";
  return {
    page, surface, heading, body, edge, bubble,
    accent,
    accentRgb: channels(accent),
    accentInk,
    bubbleRgb: channels(bubble),
    bubbleInk,
    bubbleInkRgb: channels(bubbleInk),
    bubbleDim: mix(bubble, bubbleInk, 0.62),
    input: mix(surface, pole, 0.05),
    elevated: mix(surface, pole, 0.14),
    hover: mix(surface, pole, 0.22),
    inset: page,
    label: mix(body, page, 0.22),
    dim: mix(body, page, 0.38),
    faint: mix(body, page, 0.56),
    ghost: mix(body, page, 0.7),
    divider: mix(edge, page, 0.45),
    ringHover: mix(edge, pole, 0.15),
    hint: mix(body, page, 0.38),
    shadow: base === "dark" ? "rgba(0,0,0,0.4)" : "rgba(0,0,0,0.08)",
    skel: mix(surface, pole, 0.05),
    glass,
    glassNav,
    glassEdge: base === "dark" ? "rgba(255,255,255,0.18)" : "rgba(255,255,255,0.5)",
    glassRgba: rgba(glass, 0.7),
    glassNavRgba: rgba(glassNav, 0.68),
  };
}

function buildCustomCss(cfg) {
  const t = deriveCustomTokens(cfg);
  return [
    "html.theme-custom{",
    `--color-page:${t.page};--color-surface:${t.surface};--color-input:${t.input};`,
    `--color-elevated:${t.elevated};--color-hover:${t.hover};--color-inset:${t.inset};`,
    `--color-heading:${t.heading};--color-body:${t.body};--color-label:${t.label};`,
    `--color-dim:${t.dim};--color-faint:${t.faint};--color-ghost:${t.ghost};`,
    `--color-divider:${t.divider};--color-edge:${t.edge};--color-ring-hover:${t.ringHover};`,
    `--color-hint:${t.hint};--color-shadow:${t.shadow};--color-skel:${t.skel};`,
    `--color-glass:${t.glass};--color-glass-nav:${t.glassNav};--color-glass-edge:${t.glassEdge};`,
    `--color-bubble:${t.bubble};--color-bubble-rgb:${t.bubbleRgb};`,
    `--color-bubble-ink:${t.bubbleInk};--color-bubble-ink-rgb:${t.bubbleInkRgb};`,
    `--color-bubble-dim:${t.bubbleDim};`,
    `--color-accent:${t.accent};--color-accent-rgb:${t.accentRgb};`,
    `--color-accent-ink:${t.accentInk};`,
    "}",
    "@supports ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))){",
    `html.theme-custom{--color-glass:${t.glassRgba};--color-glass-nav:${t.glassNavRgba};}`,
    "}",
  ].join("");
}

// Create/update the injected <style>. Prepend so the bundled stylesheet
// stays later in source order — that's what lets html.eink / html.no-glass
// (same specificity) still beat html.theme-custom.
function ensureCustomStyle(css) {
  if (typeof document === "undefined") return;
  let el = document.getElementById("xy-custom-theme");
  if (!el) {
    el = document.createElement("style");
    el.id = "xy-custom-theme";
    document.head.prepend(el);
  }
  // No arg → rebuild from config rather than trusting the stored CSS: a
  // schema change (e.g. new bubble tokens) would otherwise stick around
  // until the user next edits a color. Keep the pre-paint copy fresh too.
  let text = css;
  if (text == null) {
    text = buildCustomCss(getCustomConfig());
    try { localStorage.setItem(CUSTOM_CSS_KEY, text); } catch { /* blocked */ }
  }
  if (el.textContent !== text) el.textContent = text;
}

export function saveCustomConfig(cfg) {
  const css = buildCustomCss(cfg);
  try {
    localStorage.setItem(CUSTOM_KEY, JSON.stringify(cfg));
    localStorage.setItem(CUSTOM_CSS_KEY, css);
  } catch { /* private mode */ }
  ensureCustomStyle(css);
  // Re-select so a base flip (dark custom → light custom) re-resolves the
  // .dark class, palette bookkeeping, and meta color.
  if (getActivePaletteId() === "custom") selectPalette("custom");
}

/* ── terminal (xterm.js) themes ─────────────────────────────────────────
   One theme object per palette so the attached-terminal overlay follows
   the app theme. Solarized / Nord / Everforest use their canonical ANSI-16
   tables; the stock light/dark use GitHub Light/Dark; soft-dark uses a
   muted One-Dark-family table on the theme's own surfaces. */

const GITHUB_DARK_ANSI = {
  black: "#484f58", red: "#ff7b72", green: "#3fb950", yellow: "#d29922",
  blue: "#58a6ff", magenta: "#bc8cff", cyan: "#39c5cf", white: "#b1bac4",
  brightBlack: "#6e7681", brightRed: "#ffa198", brightGreen: "#56d364",
  brightYellow: "#e3b341", brightBlue: "#79c0ff", brightMagenta: "#d2a8ff",
  brightCyan: "#56d4dd", brightWhite: "#f0f6fc",
};

const GITHUB_LIGHT_ANSI = {
  black: "#24292f", red: "#cf222e", green: "#116329", yellow: "#4d2d00",
  blue: "#0969da", magenta: "#8250df", cyan: "#1b7c83", white: "#6e7781",
  brightBlack: "#57606a", brightRed: "#a40e26", brightGreen: "#1a7f37",
  brightYellow: "#633c01", brightBlue: "#218bff", brightMagenta: "#a475f9",
  brightCyan: "#3192aa", brightWhite: "#8c959f",
};

const SOLARIZED_ANSI = {
  black: "#073642", red: "#dc322f", green: "#859900", yellow: "#b58900",
  blue: "#268bd2", magenta: "#d33682", cyan: "#2aa198", white: "#eee8d5",
  brightBlack: "#586e75", brightRed: "#cb4b16", brightGreen: "#859900",
  brightYellow: "#b58900", brightBlue: "#839496", brightMagenta: "#6c71c4",
  brightCyan: "#93a1a1", brightWhite: "#fdf6e3",
};

const TERMINAL_THEMES = {
  dark: {
    background: "#0d1117", foreground: "#c9d1d9", cursor: "#58a6ff",
    cursorAccent: "#0d1117", selectionBackground: "#264f78",
    ...GITHUB_DARK_ANSI,
  },
  light: {
    background: "#ffffff", foreground: "#24292f", cursor: "#06b6d4",
    cursorAccent: "#ffffff", selectionBackground: "#b6d7ff",
    ...GITHUB_LIGHT_ANSI,
  },
  "soft-dark": {
    background: "#17181c", foreground: "#c9c7c3", cursor: "#8ab4c4",
    cursorAccent: "#17181c", selectionBackground: "#3e424a",
    black: "#45464a", red: "#e06c75", green: "#98c379", yellow: "#e5c07b",
    blue: "#61afef", magenta: "#c678dd", cyan: "#56b6c2", white: "#c9c7c3",
    brightBlack: "#5f5e5b", brightRed: "#e8858c", brightGreen: "#aad094",
    brightYellow: "#edd09a", brightBlue: "#81c0f5", brightMagenta: "#d48fe6",
    brightCyan: "#77c5cf", brightWhite: "#e8e6e3",
  },
  "solarized-dark": {
    background: "#002b36", foreground: "#839496", cursor: "#93a1a1",
    cursorAccent: "#002b36", selectionBackground: "#073642",
    ...SOLARIZED_ANSI,
  },
  "solarized-light": {
    background: "#fdf6e3", foreground: "#657b83", cursor: "#586e75",
    cursorAccent: "#fdf6e3", selectionBackground: "#eee8d5",
    ...SOLARIZED_ANSI,
  },
  nord: {
    background: "#2e3440", foreground: "#d8dee9", cursor: "#d8dee9",
    cursorAccent: "#2e3440", selectionBackground: "#434c5e",
    black: "#3b4252", red: "#bf616a", green: "#a3be8c", yellow: "#ebcb8b",
    blue: "#81a1c1", magenta: "#b48ead", cyan: "#88c0d0", white: "#e5e9f0",
    brightBlack: "#4c566a", brightRed: "#bf616a", brightGreen: "#a3be8c",
    brightYellow: "#ebcb8b", brightBlue: "#81a1c1", brightMagenta: "#b48ead",
    brightCyan: "#8fbcbb", brightWhite: "#eceff4",
  },
  everforest: {
    background: "#2d353b", foreground: "#d3c6aa", cursor: "#d3c6aa",
    cursorAccent: "#2d353b", selectionBackground: "#475258",
    black: "#475258", red: "#e67e80", green: "#a7c080", yellow: "#dbbc7f",
    blue: "#7fbbb3", magenta: "#d699b6", cyan: "#83c092", white: "#d3c6aa",
    brightBlack: "#859289", brightRed: "#e67e80", brightGreen: "#a7c080",
    brightYellow: "#dbbc7f", brightBlue: "#7fbbb3", brightMagenta: "#d699b6",
    brightCyan: "#83c092", brightWhite: "#fdf1c7",
  },
  ash: {
    background: "#f2f3f5", foreground: "#3f4247", cursor: "#135b84",
    cursorAccent: "#f2f3f5", selectionBackground: "#d7dade",
    ...GITHUB_LIGHT_ANSI,
  },
  silver: {
    background: "#c2c4c7", foreground: "#26282b", cursor: "#45484d",
    cursorAccent: "#c2c4c7", selectionBackground: "#dadcdf",
    ...GITHUB_LIGHT_ANSI,
  },
  clay: {
    background: "#cac5be", foreground: "#2b2723", cursor: "#6f4e37",
    cursorAccent: "#cac5be", selectionBackground: "#e0dbd4",
    ...GITHUB_LIGHT_ANSI,
  },
  // GitHub Dark Dimmed ANSI table.
  graphite: {
    background: "#22272e", foreground: "#adbac7", cursor: "#539bf5",
    cursorAccent: "#22272e", selectionBackground: "#444c56",
    black: "#545d68", red: "#f47067", green: "#57ab5a", yellow: "#c69026",
    blue: "#539bf5", magenta: "#b083f0", cyan: "#39c5cf", white: "#909dab",
    brightBlack: "#636e7b", brightRed: "#ff938a", brightGreen: "#6bc46d",
    brightYellow: "#daaa3f", brightBlue: "#6cb6ff", brightMagenta: "#dcbdfb",
    brightCyan: "#56d4dd", brightWhite: "#cdd9e5",
  },
  // Snow Storm ground, Polar Night ink, nord's own accent set.
  "nord-light": {
    background: "#e9eef6", foreground: "#2e3440", cursor: "#2e3440",
    cursorAccent: "#e9eef6", selectionBackground: "#d3dce9",
    black: "#3b4252", red: "#bf616a", green: "#7a9556", yellow: "#c48e2c",
    blue: "#5e81ac", magenta: "#b48ead", cyan: "#6f9fae", white: "#4c566a",
    brightBlack: "#4c566a", brightRed: "#bf616a", brightGreen: "#7a9556",
    brightYellow: "#c48e2c", brightBlue: "#5e81ac", brightMagenta: "#b48ead",
    brightCyan: "#6f9fae", brightWhite: "#2e3440",
  },
};

// xterm theme for the active palette. Custom themes get their core colors
// over the base's ANSI table; e-ink gets flat black-on-white.
export function getTerminalTheme() {
  if (typeof document !== "undefined" && document.documentElement.classList.contains("eink")) {
    const dark = document.documentElement.classList.contains("dark");
    const bg = dark ? "#000000" : "#ffffff";
    const fg = dark ? "#ffffff" : "#000000";
    return {
      background: bg, foreground: fg, cursor: fg, cursorAccent: bg,
      selectionBackground: dark ? "#444444" : "#d8d8d8",
      ...(dark ? GITHUB_DARK_ANSI : GITHUB_LIGHT_ANSI),
    };
  }
  const id = getActivePaletteId();
  if (id === "custom") {
    const cfg = getCustomConfig();
    const base = TERMINAL_THEMES[cfg.base];
    return {
      ...base,
      background: cfg.colors.page,
      foreground: cfg.colors.body,
      cursor: cfg.colors.heading,
      cursorAccent: cfg.colors.page,
      selectionBackground: deriveCustomTokens(cfg).elevated,
    };
  }
  return TERMINAL_THEMES[id] || TERMINAL_THEMES.dark;
}

/* ── palette resolution / application ── */

export function getBase() {
  const stored = localStorage.getItem("xylocopa-theme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// Validated palette id for a base; falls back to the base default.
export function getPaletteId(base) {
  let id;
  try { id = localStorage.getItem(PALETTE_KEY(base)); } catch { /* blocked */ }
  if (id === "custom") return getCustomConfig().base === base ? "custom" : base;
  const preset = PRESETS.find((p) => p.id === id);
  return preset && preset.base === base ? id : base;
}

export function getActivePaletteId() {
  return getPaletteId(getBase());
}

function pageColorOf(id) {
  if (id === "custom") return getCustomConfig().colors.page;
  return (PRESETS.find((p) => p.id === id) || PRESETS[0]).preview.page;
}

// Sync <html> classes + theme-color meta to the palette stored for `base`.
// Idempotent; called from useTheme's effect and from selectPalette.
export function applyPalette(base) {
  const root = document.documentElement;
  root.classList.toggle("dark", base === "dark");
  const id = getPaletteId(base);
  for (const c of [...root.classList]) {
    if (c.startsWith("theme-")) root.classList.remove(c);
  }
  if (id !== "light" && id !== "dark") root.classList.add(`theme-${id}`);
  if (id === "custom") ensureCustomStyle();
  const pageColor = pageColorOf(id);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", pageColor);
  try { localStorage.setItem(META_COLOR_KEY(base), pageColor); } catch { /* blocked */ }
}

// User picked a palette: remember it for its base, switch to that base,
// apply, and broadcast so every useTheme instance / settings UI syncs.
export function selectPalette(id) {
  const base = id === "custom"
    ? getCustomConfig().base
    : (PRESETS.find((p) => p.id === id) || PRESETS[0]).base;
  try {
    localStorage.setItem(PALETTE_KEY(base), id);
    localStorage.setItem("xylocopa-theme", base);
  } catch { /* blocked */ }
  applyPalette(base);
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: { base } }));
}
