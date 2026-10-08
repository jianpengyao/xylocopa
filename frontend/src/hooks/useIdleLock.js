import { useEffect, useCallback } from "react";
import { clearAuthToken, getLoginTs } from "../lib/api";

const IDLE_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes
const CHECK_INTERVAL_MS = 30 * 1000; // check every 30s
const ACTIVITY_KEY = "cc-last-activity";

/** Record user activity timestamp in localStorage (shared across tabs). */
function touch() {
  localStorage.setItem(ACTIVITY_KEY, String(Date.now()));
}

/**
 * Get milliseconds since last recorded activity.
 * Uses Math.max(lastActivity, lastLogin) so that a recent login always
 * counts as activity — prevents immediate idle-lock after iOS PWA page
 * reloads where the activity key has a stale timestamp but the user just
 * logged in.
 */
function idleMs() {
  const lastActivity = Number(localStorage.getItem(ACTIVITY_KEY) || 0);
  const lastLogin = getLoginTs();
  const last = Math.max(lastActivity, lastLogin);
  return last ? Date.now() - last : 0;
}

/**
 * Hook that monitors user activity and locks (clears token + redirects
 * to /login) after IDLE_TIMEOUT_MS of inactivity.
 *
 * Uses polling (every 30s) instead of a single long setTimeout to handle
 * mobile devices that suspend timers in the background.  Activity is
 * shared across tabs via localStorage.
 *
 * Listens to pointer, keyboard, and touch events (NOT scroll — programmatic
 * scrolls from auto-scroll and microScroll would reset the timer falsely).
 *
 * `enabled: false` (server runs with DISABLE_AUTH, i.e. no password at all)
 * turns the lock off entirely: there is nothing to protect, and locking
 * only bounced the user through /login back to the home page every 30 min.
 */
export default function useIdleLock(navigate, { enabled = true } = {}) {
  const lock = useCallback(() => {
    if (!clearAuthToken("idle-lock")) return; // grace period blocked clear
    localStorage.removeItem(ACTIVITY_KEY);
    navigate("/login", { replace: true });
  }, [navigate]);

  useEffect(() => {
    if (!enabled) return;
    // On mount, check if already idle too long.
    // If lock() returns early (grace period blocked), fall through to
    // normal setup so touch() and event listeners are registered.
    if (idleMs() > IDLE_TIMEOUT_MS) {
      lock();
      if (!localStorage.getItem("cc-auth-token")) return; // actually locked out
    }

    // Initial touch
    touch();

    // User activity events — no 'scroll' (programmatic scrolls cause
    // false resets via microScroll, auto-scroll, etc.).
    // "wheel" captures real user scrolling without the false positives.
    const events = ["pointerdown", "keydown", "wheel", "touchstart"];
    const onActivity = () => touch();
    for (const e of events) {
      window.addEventListener(e, onActivity, { passive: true });
    }

    // Listen for activity changes from other tabs
    const storageHandler = (e) => {
      if (e.key === ACTIVITY_KEY && e.newValue) {
        // Another tab recorded activity — no action needed, the
        // polling check will read the updated timestamp.
      }
    };
    window.addEventListener("storage", storageHandler);

    // Poll-based idle check — reliable on mobile (no suspended timers)
    const checkIdle = () => {
      if (idleMs() > IDLE_TIMEOUT_MS) {
        lock();
      }
    };
    const intervalId = setInterval(checkIdle, CHECK_INTERVAL_MS);

    // Also check on visibility change (immediate response when
    // returning from background)
    const visibilityHandler = () => {
      if (document.visibilityState === "visible") {
        checkIdle();
      }
    };
    document.addEventListener("visibilitychange", visibilityHandler);

    return () => {
      for (const e of events) {
        window.removeEventListener(e, onActivity);
      }
      window.removeEventListener("storage", storageHandler);
      document.removeEventListener("visibilitychange", visibilityHandler);
      clearInterval(intervalId);
    };
  }, [lock, enabled]);
}
