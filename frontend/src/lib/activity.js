// User-gesture tracking for the server-side idle lock.
//
// The server decides when a session is idle (auth_idle_timeout_minutes);
// only requests flagged `X-User-Activity: 1` move its activity clock. The
// API wrappers (lib/api.js) attach that header when a real gesture happened
// within the last few seconds, so background polls fired on their own never
// keep a session alive. useActivitySignal adds a throttled ping for users
// who read without triggering requests.
//
// Listeners run in the capture phase on window, so components that stop
// propagation (xterm swallows keydown) still count as activity. Scroll is
// deliberately excluded: programmatic scrolls (auto-scroll, microScroll)
// would fake activity; "wheel" covers real user scrolling.

const GESTURE_EVENTS = ["pointerdown", "keydown", "wheel", "touchstart"];
const DEFAULT_WINDOW_MS = 5000;

let _lastGestureAt = 0;
let _installed = false;
const _subscribers = new Set();

/** Record a user gesture now and notify onGesture() subscribers. */
export function markUserGesture() {
  _lastGestureAt = Date.now();
  for (const cb of _subscribers) {
    try { cb(_lastGestureAt); } catch { /* a subscriber must not break the others */ }
  }
}

/** Epoch ms of the last recorded gesture in this page, or 0. */
export function lastGestureAt() {
  return _lastGestureAt;
}

/** True when a gesture happened within the last `windowMs`. */
export function isUserActive(windowMs = DEFAULT_WINDOW_MS) {
  return _lastGestureAt > 0 && Date.now() - _lastGestureAt <= windowMs;
}

/** Install the window-level gesture listeners once per page. */
export function installGestureListeners() {
  if (_installed || typeof window === "undefined") return;
  _installed = true;
  for (const type of GESTURE_EVENTS) {
    window.addEventListener(type, markUserGesture, { capture: true, passive: true });
  }
}

/** Subscribe to gestures. Returns an unsubscribe function. */
export function onGesture(cb) {
  _subscribers.add(cb);
  return () => { _subscribers.delete(cb); };
}
