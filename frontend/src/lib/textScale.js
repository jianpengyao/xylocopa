// Text size (Settings > Text size): a per-device scale for the whole UI.
//
// The chosen scale lands on <html> as --xy-text-scale, which index.css turns
// into the root font-size. Every rem follows: Tailwind's text, spacing and
// radius scale, plus the px utilities that postcss-scalable-px.js rewrites
// to rem. Media-query breakpoints don't move (rem there is the browser
// default), so the layout reflows inside the same screen width instead of
// switching layouts.
//
// Stored per device: the right size differs between a phone and a tablet.
// index.html applies it before first paint; keep the numbers there in sync.
//
// Layout guard: the UI is built down to 320px wide (App root
// min-w-[320px]). Above 1, a scale is capped so the window still holds
// MIN_LAYOUT_WIDTH design px (innerWidth / scale >= 320). The slider's
// maximum follows the screen's short edge; the live cap follows the window
// (iPad split view, a narrow desktop window).

const STORAGE_KEY = "xy:text-scale";
export const TEXT_SCALE_EVENT = "xy:text-scale-change";
export const TEXT_SCALE_MIN = 0.85;
export const TEXT_SCALE_MAX = 1.5;
export const TEXT_SCALE_STEP = 0.05;
export const MIN_LAYOUT_WIDTH = 320;

// Round to the 5% grid; integer percent math avoids 1.1500000000000001.
function toStep(v) {
  return Math.round(Math.round(v * 20) * 5) / 100;
}

export function clampTextScale(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 1;
  return toStep(Math.min(TEXT_SCALE_MAX, Math.max(TEXT_SCALE_MIN, n)));
}

export function getTextScale() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw == null ? 1 : clampTextScale(parseFloat(raw));
  } catch {
    return 1;
  }
}

// Largest scale this screen allows, on the 5% grid, within [1, MAX].
export function getMaxTextScale(scr = typeof window !== "undefined" ? window.screen : null) {
  const short = scr ? Math.min(scr.width || 0, scr.height || 0) : 0;
  if (!short) return TEXT_SCALE_MAX;
  const cap = Math.floor((short / MIN_LAYOUT_WIDTH) * 20 + 1e-9) / 20;
  return Math.min(TEXT_SCALE_MAX, Math.max(1, cap));
}

// Layout-viewport width. documentElement.clientWidth, not innerWidth: iOS
// reports the pinch-zoomed visual viewport in innerWidth, and zooming in
// must not shrink the text.
function layoutWidth() {
  if (typeof document === "undefined") return 0;
  return document.documentElement.clientWidth || window.innerWidth || 0;
}

// The scale actually applied: above 1 it is capped by the window width.
export function effectiveTextScale(chosen, width = layoutWidth()) {
  if (chosen <= 1 || !width) return chosen;
  const capped = Math.max(1, Math.min(chosen, width / MIN_LAYOUT_WIDTH));
  return Math.round(capped * 1000) / 1000;
}

let applied = 1;

export function applyTextScale(chosen = getTextScale()) {
  if (typeof document === "undefined") return 1;
  const eff = effectiveTextScale(chosen);
  const root = document.documentElement;
  if (eff === 1) root.style.removeProperty("--xy-text-scale");
  else root.style.setProperty("--xy-text-scale", String(eff));
  applied = eff;
  return eff;
}

function notify() {
  try { window.dispatchEvent(new Event(TEXT_SCALE_EVENT)); } catch { /* no window */ }
}

export function setTextScale(v) {
  const chosen = clampTextScale(v);
  try {
    if (chosen === 1) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, String(chosen));
  } catch { /* storage blocked: still apply for this page view */ }
  const eff = applyTextScale(chosen);
  notify();
  return eff;
}

// Apply the stored scale and keep the window cap current. Call once at boot.
let listening = false;
export function initTextScale() {
  applyTextScale();
  if (listening || typeof window === "undefined") return;
  listening = true;
  let raf = 0;
  window.addEventListener("resize", () => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const before = applied;
      if (applyTextScale() !== before) notify();
    });
  });
  // Another tab changed it.
  window.addEventListener("storage", (e) => {
    if (e.key !== STORAGE_KEY) return;
    applyTextScale();
    notify();
  });
}

// Rendered size of a design-px value. postcss-scalable-px rewrites px in our
// CSS to rem (16px = 1rem), so JS that mirrors one of those sizes — a picker
// width used for positioning, a textarea autosize cap — scales the same way.
export function designPx(px) {
  if (typeof document === "undefined") return px;
  const rootPx = parseFloat(getComputedStyle(document.documentElement).fontSize);
  return rootPx > 0 ? (px * rootPx) / 16 : px;
}

// For inline styles: a design-px value as a rem string (scales with the root).
export function rem(px) {
  return `${px / 16}rem`;
}
