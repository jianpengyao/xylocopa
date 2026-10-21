import { useEffect, useRef } from "react";
import { authCheck, expireSession, getAuthToken, pingActivity } from "../lib/api";
import { installGestureListeners, lastGestureAt, onGesture } from "../lib/activity";

// While gestures continue, tell the server at most this often that the user
// is here — covers reading without triggering any request.
export const ACTIVITY_PING_INTERVAL_MS = 60 * 1000;

/**
 * Activity signal for the server-enforced idle lock (replaces the old
 * client-side useIdleLock). The server is the source of truth: a session idle
 * past `auth_idle_timeout_minutes` answers 401 `idle`, and api.js routes that
 * to the lock screen. This hook only
 *   - installs the gesture listeners behind the `X-User-Activity` header,
 *   - pings POST /api/auth/activity on a gesture, at most once a minute,
 *   - as a hint, re-checks the session when the page becomes visible after
 *     longer than the timeout without a local gesture (an iPhone PWA resumed
 *     from the background), so the lock shows at once instead of on the next
 *     poll.
 * It never clears the token itself. `enabled` is false under DISABLE_AUTH
 * and before the boot auth check has passed.
 */
export default function useActivitySignal({ enabled, idleTimeoutMinutes }) {
  const timeoutRef = useRef(idleTimeoutMinutes);
  useEffect(() => { timeoutRef.current = idleTimeoutMinutes; }, [idleTimeoutMinutes]);

  useEffect(() => { installGestureListeners(); }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    // The guard's auth check just confirmed the session: count from here.
    const since = Date.now();
    let lastPingAt = 0;
    const offGesture = onGesture((at) => {
      if (at - lastPingAt < ACTIVITY_PING_INTERVAL_MS) return;
      lastPingAt = at;
      // A 401 is already routed through expireSession by api.js; a network
      // error just waits for the next gesture.
      pingActivity().catch(() => {});
    });

    let checking = false;
    const onVisibility = () => {
      if (document.visibilityState !== "visible" || checking) return;
      const minutes = timeoutRef.current;
      if (!(minutes > 0)) return;
      const lastLocal = Math.max(lastGestureAt(), since);
      if (Date.now() - lastLocal <= minutes * 60 * 1000) return;
      checking = true;
      const sent = getAuthToken();
      // Never counts as activity: becoming visible is not a gesture.
      authCheck({ activity: false })
        .then((r) => {
          if (!r.authenticated && r.reason) {
            return expireSession(r.reason, { requestToken: sent, source: "visible-hint" });
          }
          return undefined;
        })
        .catch(() => {})
        .finally(() => { checking = false; });
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      offGesture();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled]);
}
