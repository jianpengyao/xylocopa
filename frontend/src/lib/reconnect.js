// Shared reconnect helpers for the status WebSocket and the AuthGuard
// server-down probe.

/** Next capped exponential backoff delay (ms). */
export function nextBackoff(prev, { factor = 1.5, max = 30000 } = {}) {
  return Math.min(Math.max(prev, 0) * factor, max);
}

// iOS 18 Safari: a fetch issued synchronously inside the visibilitychange
// handler can fail with TypeError "Load failed" 20-40 s later, while the
// same fetch issued a few hundred ms after resume succeeds. Defer the first
// probe after resume by this much.
export const RESUME_PROBE_DELAY_MS = 500;

/**
 * Call `handler` shortly after the page regains the network / foreground:
 *   - document.visibilitychange -> visible (PWA foregrounded)
 *   - window 'online'            (browser noticed the network came back)
 *   - window 'pageshow'          (restored from bfcache / app switcher)
 * Multiple triggers within the delay window coalesce into one call.
 * Returns a cleanup function.
 */
export function onNetworkResume(handler, { delayMs = RESUME_PROBE_DELAY_MS } = {}) {
  let timer = null;
  const fire = () => {
    if (timer) return;
    timer = setTimeout(() => {
      timer = null;
      handler();
    }, delayMs);
  };
  const onVisibility = () => {
    if (document.visibilityState === "visible") fire();
  };
  const onPageShow = (e) => {
    if (e && e.persisted) fire();
  };
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("online", fire);
  window.addEventListener("pageshow", onPageShow);
  return () => {
    if (timer) clearTimeout(timer);
    timer = null;
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("online", fire);
    window.removeEventListener("pageshow", onPageShow);
  };
}

// Status WebSocket policy (contexts/WebSocketContext.jsx).
// Liveness: a client "ping" every WS_PING_INTERVAL_MS; some frame (pong,
// server ping, any event) must arrive within WS_PONG_TIMEOUT_MS or the
// socket is treated as half-open and replaced. Half-open sockets are the
// normal outcome of an iOS PWA resume or a NAT/VPN path change: readyState
// stays OPEN, sends succeed into the buffer, and onclose never fires.
export const WS_PING_INTERVAL_MS = 30000;
export const WS_PONG_TIMEOUT_MS = 10000;
// A handshake that never completes (upstream hung) is abandoned after this.
export const WS_HANDSHAKE_TIMEOUT_MS = 10000;
export const WS_INITIAL_RECONNECT_MS = 1000;
export const WS_MAX_RECONNECT_MS = 30000;

// WebSocket auth close (orchestrator/ws_auth.py): code 4001 with the reason
// as close reason. Only expired / revoked / idle end the session; missing /
// invalid (no ticket, or a spent or stale one) mean "fetch a fresh ticket and
// reconnect". 1011 "Auth store unavailable" is an ordinary drop.
export const WS_AUTH_CLOSE_CODE = 4001;
const WS_SESSION_END_REASONS = new Set(["expired", "revoked", "idle"]);

/** The session-ending reason carried by a WebSocket close event, or null. */
export function wsSessionEndReason(ev) {
  return ev?.code === WS_AUTH_CLOSE_CODE && WS_SESSION_END_REASONS.has(ev.reason)
    ? ev.reason
    : null;
}
