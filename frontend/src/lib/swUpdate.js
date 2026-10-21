/**
 * Service-worker registration and update reloads.
 *
 * Registered with the plain navigator.serviceWorker API instead of
 * vite-plugin-pwa's virtual:pwa-register (vite.config.js sets
 * injectRegister: false). Its autoUpdate mode reloads the page the moment an
 * updated worker activates, and its prompt mode still reloads on
 * `controlling` for a worker installed by another tab
 * (node_modules/vite-plugin-pwa/dist/client/build/register.js) — mid-password
 * on /login, or mid-sentence in the composer.
 *
 * Here an update that takes control of this page (skipWaiting + clientsClaim
 * → controllerchange) asks for a reload, and the reload waits while the user
 * could lose input: on /login, or while a text field has focus. A pending
 * reload runs on focusout (once nothing editable keeps focus), when the page
 * is hidden, or on a 1 s poll — each time only if nothing defers it any more.
 * While App shows its full-screen update gate nothing on screen can be lost,
 * so the reload is immediate there.
 */

const UPDATE_CHECK_INTERVAL_MS = 30 * 60 * 1000;
const PENDING_POLL_MS = 1000;
// Inputs that take no typing: focusing one never defers a reload.
const NON_TEXT_INPUTS = new Set([
  "button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit",
]);

let _updateScreen = false;
let _pendingReason = null;
let _pollTimer = null;
let _reloadPage = () => window.location.reload();

/** Does `el` take typed input (text-like input, textarea, contentEditable)? */
export function isEditable(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === "TEXTAREA") return true;
  if (tag === "INPUT") return !NON_TEXT_INPUTS.has((el.type || "text").toLowerCase());
  return false;
}

/** True while a reload would cost the user input: /login, or typing. */
export function shouldDeferReload() {
  if (_updateScreen) return false;
  if (window.location.pathname === "/login") return true;
  return isEditable(document.activeElement);
}

/** App's SW update gate ("Updating to latest version…") is (not) on screen. */
export function setUpdateScreen(active) {
  _updateScreen = !!active;
  if (_updateScreen) flushPendingReload();
}

/** A reload is waiting for the user to finish typing / leave /login. */
export function isReloadPending() {
  return _pendingReason !== null;
}

function _onFocusOut() {
  // During focusout activeElement is still the old field or <body>; judge
  // where focus lands (a tab to the next field keeps deferring).
  setTimeout(flushPendingReload, 0);
}

function _onVisibility() {
  if (document.visibilityState === "hidden") flushPendingReload();
}

function _stopWaiting() {
  clearInterval(_pollTimer);
  _pollTimer = null;
  document.removeEventListener("focusout", _onFocusOut, true);
  document.removeEventListener("visibilitychange", _onVisibility);
}

function _reload(reason) {
  _stopWaiting();
  _pendingReason = null;
  console.info("[sw-update] reloading (%s)", reason);
  _reloadPage();
}

/** Reload now, unless that would cost input: then once it no longer would. */
export function requestReload(reason = "sw-update") {
  if (!shouldDeferReload()) {
    _reload(reason);
    return true;
  }
  if (_pendingReason !== null) return false;
  _pendingReason = reason;
  console.info("[sw-update] reload deferred (%s)", reason);
  document.addEventListener("focusout", _onFocusOut, true);
  document.addEventListener("visibilitychange", _onVisibility);
  _pollTimer = setInterval(flushPendingReload, PENDING_POLL_MS);
  return false;
}

/** Run a pending reload if nothing defers it any more. */
export function flushPendingReload() {
  if (_pendingReason !== null && !shouldDeferReload()) _reload(_pendingReason);
}

/**
 * Register the app's service worker (after window load, like the plugin's
 * own register code) and keep it fresh: an update check every 30 min and on
 * every foreground, skipped while a reload would have to wait anyway.
 * `swUrl`: "/sw.js", or "/dev-sw.js?dev-sw" from the dev server
 * (vite-plugin-pwa devOptions; classic type for generateSW).
 */
export function registerServiceWorker({ swUrl, onRegistered } = {}) {
  if (!("serviceWorker" in navigator)) return;
  const sw = navigator.serviceWorker;
  // Controlled at load: a later controllerchange is an updated worker
  // taking over, whose precache may no longer hold the chunks this page
  // still references. Uncontrolled (first install): clients.claim() also
  // fires controllerchange, but this page already runs current code.
  let hadController = !!sw.controller;
  sw.addEventListener("controllerchange", () => {
    if (hadController) requestReload("sw-update");
    hadController = true;
  });
  const register = () => {
    sw.register(swUrl, { scope: "/" })
      .then((registration) => {
        const check = () => {
          if (shouldDeferReload()) return;
          registration.update().catch(() => { /* offline: next check */ });
        };
        setInterval(check, UPDATE_CHECK_INTERVAL_MS);
        // Every foreground too — catches rebuilds as soon as the user
        // switches back to the app (mobile PWA, iPad, etc.)
        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState === "visible") check();
        });
        onRegistered?.(registration);
      })
      .catch((err) => console.warn("Service worker registration failed:", err));
  };
  if (document.readyState === "complete") register();
  else window.addEventListener("load", register, { once: true });
}

/** Test seam: replace window.location.reload and reset module state. */
export function _resetSwUpdateForTests({ reload } = {}) {
  _stopWaiting();
  _pendingReason = null;
  _updateScreen = false;
  _reloadPage = reload || (() => window.location.reload());
}
