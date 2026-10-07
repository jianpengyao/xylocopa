/** Web Push notification helpers for Xylocopa. */

import { authedFetch } from "./api";

const STORAGE_KEY = "xylocopa-push-enabled";

// One-time migration of legacy key
try {
  const legacy = localStorage.getItem("agenthive-push-enabled");
  if (legacy !== null) {
    if (localStorage.getItem(STORAGE_KEY) === null) localStorage.setItem(STORAGE_KEY, legacy);
    localStorage.removeItem("agenthive-push-enabled");
  }
} catch {}

export function isPushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window;
}

export function isPushEnabled() {
  return localStorage.getItem(STORAGE_KEY) === "true";
}

function setPushEnabled(v) {
  localStorage.setItem(STORAGE_KEY, v ? "true" : "false");
}

/**
 * Register service worker, subscribe to push, and send subscription to backend.
 * Returns true on success, false on failure.
 */
// How long setupPushNotifications waits for the app's own service worker
// (registered by main.jsx on window 'load') before registering the
// standalone push-handler.js fallback.
const SW_READY_WAIT_MS = 5000;

export async function setupPushNotifications() {
  if (!isPushSupported()) {
    console.debug("[push] setup skipped: PushManager not supported");
    return false;
  }

  try {
    // Request notification permission
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      console.debug("[push] setup skipped: permission=%s", permission);
      return false;
    }

    // Fallback: if no SW is registered yet (e.g. first load before VitePWA
    // SW installs), register push-handler.js as a temporary standalone SW.
    // Once VitePWA's SW activates (includes push-handler.js via importScripts),
    // this standalone registration gets replaced automatically.
    //
    // Give the real SW a moment first: main.jsx registers it on window
    // 'load', which can land after this auth-gated setup. Two register()
    // calls at the same scope race, and if push-handler.js won the app
    // would run without its precache worker until the next update check.
    let existing = await navigator.serviceWorker.getRegistration();
    if (!existing) {
      existing = await Promise.race([
        navigator.serviceWorker.ready,
        new Promise((resolve) => setTimeout(() => resolve(null), SW_READY_WAIT_MS)),
      ]);
    }
    if (!existing) {
      console.debug("[push] no SW yet, registering push-handler.js as fallback");
      await navigator.serviceWorker.register("/push-handler.js");
    }

    const reg = await navigator.serviceWorker.ready;

    // Fetch VAPID public key from backend
    const res = await authedFetch("/api/push/vapid-public-key");
    if (!res.ok) {
      console.warn("[push] VAPID key fetch failed: %d %s", res.status, res.statusText);
      return false;
    }
    const { publicKey } = await res.json();

    // Convert base64url to Uint8Array
    const applicationServerKey = urlBase64ToUint8Array(publicKey);

    // Always ensure we have a valid subscription and the backend knows about it.
    // Existing subscriptions may have been purged server-side (410 expired)
    // while the browser still holds a stale reference.
    let subscription = await reg.pushManager.getSubscription();
    const hadExisting = !!subscription;
    if (!subscription) {
      subscription = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey,
      });
    }

    // Always send to backend — handles re-registration after server-side
    // expiry cleanup and endpoint rotation.
    const sub = subscription.toJSON();
    console.debug(
      "[push] %s subscription → sending to backend (endpoint=%s…)",
      hadExisting ? "existing" : "new",
      sub.endpoint?.slice(0, 60),
    );
    const postRes = await authedFetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        endpoint: sub.endpoint,
        keys: sub.keys,
      }),
    });

    if (postRes.ok) {
      console.debug("[push] subscription registered with backend");
      setPushEnabled(true);
      return true;
    }
    console.warn("[push] backend rejected subscription:", postRes.status);
    return false;
  } catch (err) {
    console.warn("[push] setup failed:", err);
    return false;
  }
}

/**
 * Unsubscribe from push and notify backend.
 */
export async function teardownPushNotifications() {
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    if (reg) {
      const subscription = await reg.pushManager.getSubscription();
      if (subscription) {
        const endpoint = subscription.endpoint;
        await subscription.unsubscribe();

        await authedFetch("/api/push/unsubscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint }),
        });
      }
    }
  } catch (err) {
    console.warn("Push teardown error:", err);
  }
  setPushEnabled(false);
}

/**
 * Re-send existing push subscription to backend on every page load.
 * Works in dev mode (no SW registration needed) — just upserts whatever
 * the browser already has.  Ensures backend always has a fresh copy even
 * after server-side expiry cleanup or endpoint rotation.
 */
export async function reRegisterExistingSubscription() {
  if (!isPushSupported()) return;
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    if (!reg) {
      console.debug("[push] re-register: no service worker registered");
      return;
    }
    const subscription = await reg.pushManager.getSubscription();
    if (!subscription) {
      console.debug("[push] re-register: no existing subscription");
      return;
    }
    const sub = subscription.toJSON();
    if (!sub.endpoint || !sub.keys?.p256dh || !sub.keys?.auth) {
      console.debug("[push] re-register: subscription missing keys");
      return;
    }
    const res = await authedFetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        endpoint: sub.endpoint,
        keys: sub.keys,
      }),
    });
    if (res.ok) {
      console.debug("[push] re-register: upsert OK (endpoint=%s…)", sub.endpoint?.slice(0, 60));
      setPushEnabled(true);
    } else {
      console.warn("[push] re-register: backend rejected: %d", res.status);
    }
  } catch (err) {
    console.debug("[push] re-register failed: %s", err);
  }
}

/** Convert a base64url-encoded string to a Uint8Array (for applicationServerKey). */
function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) {
    arr[i] = raw.charCodeAt(i);
  }
  return arr;
}
