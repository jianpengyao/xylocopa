// Fake browser push environment + backend for the push tests. Nothing here
// touches a real service worker, push service or server.
import { vi } from "vitest";

export const ENDPOINT = "https://push.example/device-1";
// base64url of "test-vapid-key" — any decodable string will do.
const VAPID_KEY = "dGVzdC12YXBpZC1rZXk";

let _current = null;

/**
 * permission:  Notification.permission at start ("default" | "granted" | "denied")
 * answer:      what the permission prompt resolves to
 * subscribed:  whether the browser already holds a push subscription (ENDPOINT)
 * supported:   false = no Push API (e.g. iOS Safari tab)
 * device:      this device's server-side settings
 * known:       false = the server has no row for ENDPOINT (404 on /api/push/device,
 *              /subscribe answers created: true)
 *
 * Each pushManager.subscribe() hands out a new endpoint (device-1, device-2, …),
 * like a browser after unsubscribe().
 */
export function installPushEnv({
  permission = "default",
  answer = "granted",
  subscribed = false,
  supported = true,
  device = {},
  known = true,
} = {}) {
  let nextId = subscribed ? 2 : 1;
  const env = {
    permission,
    answer,
    subscription: null,
    device: { agents_enabled: true, tasks_enabled: true, device_label: "Chrome on Linux", ...device },
    knownEndpoints: new Set(known ? [ENDPOINT] : []),
    requests: [],
  };

  const makeSubscription = (endpoint) => {
    const sub = {
      endpoint,
      toJSON: () => ({ endpoint, keys: { p256dh: "p256dh-key", auth: "auth-key" } }),
      unsubscribe: vi.fn(async () => {
        if (env.subscription === sub) env.subscription = null;
        return true;
      }),
    };
    return sub;
  };
  if (subscribed) env.subscription = makeSubscription(ENDPOINT);
  env.initialSubscription = env.subscription;

  env.requestPermission = vi.fn(() => {
    env.permission = env.answer;
    return Promise.resolve(env.answer);
  });

  const pushManager = {
    getSubscription: vi.fn(async () => env.subscription),
    subscribe: vi.fn(async () => {
      if (env.permission !== "granted") throw new Error("NotAllowedError");
      env.subscription = makeSubscription(`https://push.example/device-${nextId++}`);
      return env.subscription;
    }),
  };
  const reg = { pushManager };
  env.pushManager = pushManager;

  if (supported) {
    vi.stubGlobal("Notification", {
      get permission() { return env.permission; },
      requestPermission: env.requestPermission,
    });
    vi.stubGlobal("PushManager", function PushManager() {});
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: { getRegistration: vi.fn(async () => reg), ready: Promise.resolve(reg), register: vi.fn() },
    });
  }

  const reply = (status, body) => ({ ok: status < 400, status, json: async () => body });
  vi.stubGlobal("fetch", vi.fn(async (url, opts = {}) => {
    const body = opts.body ? JSON.parse(opts.body) : undefined;
    env.requests.push({ url, method: opts.method || "GET", body });
    switch (url) {
      case "/api/push/vapid-public-key":
        return reply(200, { publicKey: VAPID_KEY });
      case "/api/push/subscribe": {
        const created = !env.knownEndpoints.has(body.endpoint);
        env.knownEndpoints.add(body.endpoint);
        if (created) {
          // A new row: defaults, seeded by any switches the client carried.
          env.device = { ...env.device, agents_enabled: true, tasks_enabled: true };
          for (const k of ["agents_enabled", "tasks_enabled"]) {
            if (typeof body[k] === "boolean") env.device[k] = body[k];
          }
        }
        return reply(200, { status: "subscribed", created, device: { ...env.device } });
      }
      case "/api/push/device":
        if (!env.knownEndpoints.has(body.endpoint)) return reply(404, { detail: "Unknown push subscription" });
        for (const k of ["agents_enabled", "tasks_enabled"]) if (k in body) env.device[k] = body[k];
        return reply(200, { status: "updated", device: { ...env.device } });
      case "/api/push/unsubscribe":
        env.knownEndpoints.delete(body.endpoint);
        return reply(200, { status: "unsubscribed" });
      case "/api/push/test":
        return reply(200, { status: "sent" });
      default:
        return reply(404, {});
    }
  }));

  /** POST paths in order, e.g. ["/api/push/subscribe", "/api/push/test"]. */
  env.posts = () => env.requests.filter((r) => r.method === "POST").map((r) => r.url);
  _current = env;
  return env;
}

/**
 * Requests (any method) the installed env saw to the legacy global
 * settings endpoint. The UI must never call it — tests assert this is
 * empty after every case.
 */
export function settingsRequests() {
  return (_current?.requests || []).filter((r) => r.url.startsWith("/api/settings"));
}

export function uninstallPushEnv() {
  vi.unstubAllGlobals();
  delete navigator.serviceWorker;
  _current = null;
}

export function setVisibility(state) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
}
