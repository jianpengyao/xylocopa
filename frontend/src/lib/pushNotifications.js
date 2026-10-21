/** Web Push for Xylocopa — this device's subscription and its switches.
 *
 * The server owns each device's switches (its push_subscriptions row,
 * keyed by endpoint); localStorage only caches the last answer so the bell
 * can render before the first sync. The bell controls this device only.
 *
 * Permission is requested only from a tap on the bell
 * (enablePushOnThisDevice): iOS standalone PWAs ignore
 * Notification.requestPermission() outside a user gesture, so startup
 * (syncPushDevice) never prompts.
 */

import { authedFetch } from "./api";

const DEVICE_KEY = "xylocopa-push-device";

// How long registration waits for the app's own service worker
// (registered by main.jsx on window 'load') before registering the
// standalone push-handler.js fallback.
const SW_READY_WAIT_MS = 5000;

export function isPushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

/**
 * Bell state for this device:
 *   unsupported — no Push API (e.g. iOS Safari tab, not the Home Screen app)
 *   denied      — the user blocked notifications for this site
 *   syncing     — permission granted, switches unknown, sync in flight
 *                 (e.g. first load with an empty cache); not "off-device",
 *                 so a tap can't unmute a device that is really muted
 *   off-device  — permission not granted yet, or no server-side subscription
 *   on / muted  — subscribed; the server's agents_enabled for this device
 */
export function deviceNotifState({ supported, permission, device, syncing = false }) {
  if (!supported) return "unsupported";
  if (permission === "denied") return "denied";
  if (permission === "granted" && !device && syncing) return "syncing";
  if (permission !== "granted" || !device) return "off-device";
  return device.agents_enabled ? "on" : "muted";
}

// --- This device's server-side settings (cached in localStorage) ---

function readCachedDevice() {
  try {
    const v = localStorage.getItem(DEVICE_KEY);
    return v ? JSON.parse(v) : null;
  } catch {
    return null;
  }
}

let _device = readCachedDevice();
const _listeners = new Set();

function emitChange() {
  for (const fn of _listeners) fn();
}

function setDevice(device) {
  _device = device || null;
  try {
    if (_device) localStorage.setItem(DEVICE_KEY, JSON.stringify(_device));
    else localStorage.removeItem(DEVICE_KEY);
  } catch { /* storage unavailable */ }
  emitChange();
}

export function getPushDeviceState() {
  const supported = isPushSupported();
  const permission = supported ? Notification.permission : "unsupported";
  const syncing = !!_syncing;
  return { state: deviceNotifState({ supported, permission, device: _device, syncing }), device: _device, permission };
}

/** Subscribe to device-state changes; returns the unsubscribe function. */
export function onPushDeviceChange(fn) {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}

// --- VAPID key, fetched ahead so a bell tap has nothing to await ---

let _vapidKey = null;
let _vapidKeyPromise = null;

export function prefetchVapidKey() {
  if (_vapidKey) return Promise.resolve(_vapidKey);
  if (!_vapidKeyPromise) {
    _vapidKeyPromise = authedFetch("/api/push/vapid-public-key")
      .then((res) => {
        if (!res.ok) throw new Error(`VAPID key fetch failed: ${res.status}`);
        return res.json();
      })
      .then(({ publicKey }) => {
        _vapidKey = urlBase64ToUint8Array(publicKey);
        return _vapidKey;
      })
      .finally(() => { _vapidKeyPromise = null; });
  }
  return _vapidKeyPromise;
}

async function getRegistration() {
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
  return navigator.serviceWorker.ready;
}

function postJson(url, body) {
  return authedFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function subscribeFresh(reg) {
  return reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: _vapidKey || await prefetchVapidKey(),
  });
}

/** POST /api/push/subscribe; resolves to {created, device}. */
async function postSubscribe(subscription) {
  const sub = subscription.toJSON();
  // This device's last known switches: they seed the row only if the
  // server creates one (new endpoint), so a mute survives a new endpoint.
  const carried = {};
  for (const k of ["agents_enabled", "tasks_enabled"]) {
    if (typeof _device?.[k] === "boolean") carried[k] = _device[k];
  }
  const res = await postJson("/api/push/subscribe", { endpoint: sub.endpoint, keys: sub.keys, ...carried });
  if (!res.ok) throw new Error(`backend rejected subscription: ${res.status}`);
  return res.json();
}

/**
 * Ensure a browser subscription exists (permission must already be
 * granted — this never prompts) and upsert it on the server. Always sent:
 * re-registers after the server dropped this device or the endpoint
 * rotated. The server's answer replaces the cached device settings.
 */
async function registerWithServer(reg) {
  let subscription = await reg.pushManager.getSubscription();
  const hadExisting = !!subscription;
  if (!subscription) subscription = await subscribeFresh(reg);
  console.debug(
    "[push] %s subscription → sending to backend (endpoint=%s…)",
    hadExisting ? "existing" : "new",
    subscription.endpoint?.slice(0, 60),
  );
  let { created, device } = await postSubscribe(subscription);
  if (hadExisting && created) {
    // The server had no row for a subscription this browser still holds.
    // It drops rows only when the push service answers 404/410 (or on an
    // explicit unsubscribe/reset), so this endpoint can't deliver:
    // re-registering it would loop (row re-created, next push 410s, row
    // deleted). Get a fresh endpoint and forget the dead one.
    const stale = subscription.endpoint;
    console.debug("[push] server had dropped this subscription → renewing");
    try { await subscription.unsubscribe(); } catch { /* already gone */ }
    subscription = await subscribeFresh(reg);
    ({ device } = await postSubscribe(subscription));
    await postJson("/api/push/unsubscribe", { endpoint: stale }).catch(() => {});
  }
  setDevice(device);
  return { endpoint: subscription.endpoint, device };
}

/** POST /api/push/device; resolves to the device settings, or null on 404. */
async function postDevice(endpoint, patch) {
  const res = await postJson("/api/push/device", { endpoint, ...patch });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`device update failed: ${res.status}`);
  const { device } = await res.json();
  setDevice(device);
  return device;
}

let _syncing = null;

/**
 * Startup / foreground sync. With permission granted: make sure this
 * device is subscribed and registered, and adopt the server's switches.
 * Otherwise: never prompt — the bell shows the enable state.
 */
export function syncPushDevice() {
  if (_syncing) return _syncing;
  _syncing = (async () => {
    if (!isPushSupported()) return null;
    if (Notification.permission !== "granted") {
      setDevice(null);
      prefetchVapidKey().catch(() => {});
      return null;
    }
    try {
      const reg = await getRegistration();
      return (await registerWithServer(reg)).device;
    } catch (err) {
      // Keep the cached state; the next foreground retries.
      console.warn("[push] sync failed:", err);
      return _device;
    }
  })().finally(() => {
    _syncing = null;
    emitChange();
  });
  emitChange(); // "syncing" while this device's switches are unknown
  return _syncing;
}

let _hooked = false;

/**
 * Sync now and on every return to the foreground (once per page); also
 * follow other tabs' changes and permission changes made while visible.
 */
export function startPushDeviceSync() {
  if (!_hooked) {
    _hooked = true;
    // Re-registers if the server lost this device, and picks up a
    // permission change made in system settings while we were away.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") syncPushDevice();
    });
    // Another tab on this device muted/unmuted or re-synced.
    window.addEventListener("storage", (e) => {
      if (e.key !== DEVICE_KEY && e.key !== null) return;
      _device = readCachedDevice();
      emitChange();
    });
    // Permission changed in site settings while this page stayed visible.
    try {
      navigator.permissions?.query({ name: "notifications" })
        .then((status) => { status.onchange = () => syncPushDevice(); })
        .catch(() => { /* not queryable (older Safari) */ });
    } catch { /* Permissions API unavailable */ }
  }
  return syncPushDevice();
}

/**
 * Bell tap in the off-device state: ask for permission, subscribe,
 * register, make sure this device is unmuted, then send a test push.
 *
 * MUST be called synchronously from the click handler: requestPermission()
 * runs before the first await because iOS standalone PWAs only honour it
 * inside a user gesture. Resolves to {ok, permission, device?}.
 */
export function enablePushOnThisDevice() {
  const permissionPromise = Notification.requestPermission();
  return (async () => {
    const permission = await permissionPromise;
    if (permission !== "granted") {
      emitChange();
      return { ok: false, permission };
    }
    try {
      const reg = await getRegistration();
      const registered = await registerWithServer(reg);
      const { endpoint } = registered;
      let device = registered.device;
      if (!device?.agents_enabled) {
        device = await postDevice(endpoint, { agents_enabled: true });
        if (!device) throw new Error("server lost the new subscription");
      }
      try {
        const res = await postJson("/api/push/test", { endpoint });
        if (!res.ok) console.warn("[push] test push failed: %d", res.status);
      } catch (err) {
        console.warn("[push] test push failed:", err);
      }
      return { ok: true, permission, device };
    } catch (err) {
      console.warn("[push] enable failed:", err);
      emitChange();
      return { ok: false, permission };
    }
  })();
}

/** Mute (false) or unmute (true) agent notifications on this device only. */
export async function setThisDeviceAgentsEnabled(enabled) {
  const reg = await navigator.serviceWorker.getRegistration();
  const subscription = reg && await reg.pushManager.getSubscription();
  if (!subscription) {
    setDevice(null);
    throw new Error("no push subscription on this device");
  }
  let device = await postDevice(subscription.endpoint, { agents_enabled: enabled });
  if (!device) {
    // The server dropped this device (404/410 from the push service):
    // register again — on a fresh endpoint, see registerWithServer — then
    // apply the switch to whatever endpoint is now registered.
    const { endpoint } = await registerWithServer(reg);
    device = await postDevice(endpoint, { agents_enabled: enabled });
    if (!device) throw new Error("server does not know this device");
  }
  return device;
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
  setDevice(null);
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
