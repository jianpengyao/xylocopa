import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
/* global process */
import fs from "node:fs";
import path from "node:path";
import { ENDPOINT, installPushEnv, uninstallPushEnv, setVisibility, settingsRequests } from "./pushEnv.testutil";

const ENDPOINT_2 = "https://push.example/device-2";

// The module keeps per-page state (device cache, VAPID key, visibility
// hook), so every test imports a fresh copy.
async function freshModule() {
  vi.resetModules();
  return import("./pushNotifications");
}

const flush = () => new Promise((r) => setTimeout(r, 0));

let visibilityHandlers;
let storageHandlers;

beforeEach(() => {
  vi.spyOn(console, "debug").mockImplementation(() => {});
  localStorage.clear();
  setVisibility("visible");
  visibilityHandlers = [];
  storageHandlers = [];
  const add = document.addEventListener.bind(document);
  vi.spyOn(document, "addEventListener").mockImplementation((type, fn, opts) => {
    if (type === "visibilitychange") visibilityHandlers.push(fn);
    return add(type, fn, opts);
  });
  const addWin = window.addEventListener.bind(window);
  vi.spyOn(window, "addEventListener").mockImplementation((type, fn, opts) => {
    if (type === "storage") storageHandlers.push(fn);
    return addWin(type, fn, opts);
  });
});

afterEach(() => {
  // Spec: the UI never touches the legacy global settings endpoint.
  expect(settingsRequests()).toEqual([]);
  for (const fn of visibilityHandlers) document.removeEventListener("visibilitychange", fn);
  for (const fn of storageHandlers) window.removeEventListener("storage", fn);
  delete navigator.permissions;
  vi.restoreAllMocks();
  uninstallPushEnv();
});

function subscribeBodies(env) {
  return env.requests.filter((r) => r.url === "/api/push/subscribe").map((r) => r.body);
}

describe("deviceNotifState", () => {
  it.each([
    [{ supported: false, permission: "unsupported", device: null }, "unsupported"],
    [{ supported: true, permission: "denied", device: { agents_enabled: true } }, "denied"],
    [{ supported: true, permission: "default", device: null }, "off-device"],
    [{ supported: true, permission: "default", device: { agents_enabled: true } }, "off-device"],
    [{ supported: true, permission: "granted", device: null }, "off-device"],
    [{ supported: true, permission: "granted", device: { agents_enabled: true } }, "on"],
    [{ supported: true, permission: "granted", device: { agents_enabled: false } }, "muted"],
  ])("%j → %s", async (input, expected) => {
    const { deviceNotifState } = await freshModule();
    expect(deviceNotifState(input)).toBe(expected);
  });
});

describe("startup sync", () => {
  it.each(["default", "denied"])("never prompts or subscribes when permission is %s", async (permission) => {
    const env = installPushEnv({ permission });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    expect(env.requestPermission).not.toHaveBeenCalled();
    expect(env.pushManager.subscribe).not.toHaveBeenCalled();
    expect(env.posts()).toEqual([]);
    expect(getPushDeviceState().state).toBe(permission === "denied" ? "denied" : "off-device");
  });

  it("with permission granted, subscribes without prompting and registers", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: false });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    expect(env.requestPermission).not.toHaveBeenCalled();
    expect(env.pushManager.subscribe).toHaveBeenCalledWith(
      expect.objectContaining({ userVisibleOnly: true }));
    expect(env.posts()).toEqual(["/api/push/subscribe"]);
    expect(env.requests.at(-1).body).toEqual({
      endpoint: ENDPOINT, keys: { p256dh: "p256dh-key", auth: "auth-key" } });
    expect(getPushDeviceState().state).toBe("on");
  });

  it("server state wins over the localStorage cache", async () => {
    localStorage.setItem("xylocopa-push-device", JSON.stringify({ agents_enabled: false }));
    installPushEnv({ permission: "granted", subscribed: true, device: { agents_enabled: true } });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    expect(getPushDeviceState().state).toBe("muted"); // cache, before the first answer
    await startPushDeviceSync();
    expect(getPushDeviceState().state).toBe("on");
    expect(JSON.parse(localStorage.getItem("xylocopa-push-device")).agents_enabled).toBe(true);
  });

  it("re-syncs when the app returns to the foreground", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: true });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    expect(env.posts()).toEqual(["/api/push/subscribe"]);

    // The server's answer changed while we were away; the foreground sync
    // re-registers and adopts it.
    env.device.agents_enabled = false;
    setVisibility("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    await flush();
    expect(env.posts()).toEqual(["/api/push/subscribe", "/api/push/subscribe"]);
    expect(getPushDeviceState().state).toBe("muted");
    expect(env.requestPermission).not.toHaveBeenCalled();
  });

  it("granted with no cached device: 'syncing' until the server answers, never 'off-device'", async () => {
    installPushEnv({ permission: "granted", subscribed: true, device: { agents_enabled: false } });
    const { startPushDeviceSync, getPushDeviceState, onPushDeviceChange } = await freshModule();
    const seen = [getPushDeviceState().state];
    onPushDeviceChange(() => seen.push(getPushDeviceState().state));
    const pending = startPushDeviceSync();
    expect(getPushDeviceState().state).toBe("syncing");
    await pending;
    expect(getPushDeviceState().state).toBe("muted");
    expect(seen.slice(1)).not.toContain("off-device");
  });

  it("a failed sync does not leave the bell stuck in 'syncing'", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    installPushEnv({ permission: "granted", subscribed: true });
    vi.mocked(fetch).mockImplementationOnce(async () => ({ ok: false, status: 502, json: async () => ({}) }));
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    expect(getPushDeviceState().state).toBe("off-device");
  });

  it("another tab's change reaches this tab through the storage event", async () => {
    installPushEnv({ permission: "granted", subscribed: true });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    expect(getPushDeviceState().state).toBe("on");

    localStorage.setItem("xylocopa-push-device", JSON.stringify({ agents_enabled: false, tasks_enabled: true }));
    window.dispatchEvent(new StorageEvent("storage", { key: "xylocopa-push-device" }));
    expect(getPushDeviceState().state).toBe("muted");
  });

  it("re-syncs when notification permission changes while the page stays visible", async () => {
    const status = { onchange: null };
    Object.defineProperty(navigator, "permissions", {
      configurable: true, value: { query: vi.fn(async () => status) } });
    const env = installPushEnv({ permission: "granted", subscribed: true });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    await flush();
    expect(navigator.permissions.query).toHaveBeenCalledWith({ name: "notifications" });
    expect(getPushDeviceState().state).toBe("on");

    env.permission = "denied";
    status.onchange();
    await flush();
    expect(getPushDeviceState().state).toBe("denied");
  });

  it("drops the cached device when permission was revoked", async () => {
    localStorage.setItem("xylocopa-push-device", JSON.stringify({ agents_enabled: true }));
    installPushEnv({ permission: "denied" });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    expect(getPushDeviceState().state).toBe("denied");
    expect(localStorage.getItem("xylocopa-push-device")).toBeNull();
  });
});

describe("a subscription the server dropped (push service 404/410)", () => {
  it("startup replaces it with a fresh one instead of re-registering the dead endpoint", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: true, known: false });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    expect(env.initialSubscription.unsubscribe).toHaveBeenCalledTimes(1);
    expect(env.pushManager.subscribe).toHaveBeenCalledTimes(1);
    expect(subscribeBodies(env).map((b) => b.endpoint)).toEqual([ENDPOINT, ENDPOINT_2]);
    expect(env.requests.find((r) => r.url === "/api/push/unsubscribe").body).toEqual({ endpoint: ENDPOINT });
    expect([...env.knownEndpoints]).toEqual([ENDPOINT_2]);
    expect(getPushDeviceState().state).toBe("on");
    expect(env.requestPermission).not.toHaveBeenCalled();
  });

  it("the fresh subscription keeps this device's last known mute", async () => {
    localStorage.setItem("xylocopa-push-device",
      JSON.stringify({ agents_enabled: false, tasks_enabled: true, device_label: "Safari on Mac" }));
    const env = installPushEnv({ permission: "granted", subscribed: true, known: false });
    const { startPushDeviceSync, getPushDeviceState } = await freshModule();
    await startPushDeviceSync();
    expect(subscribeBodies(env).at(-1)).toMatchObject({
      endpoint: ENDPOINT_2, agents_enabled: false, tasks_enabled: true });
    expect(env.device.agents_enabled).toBe(false);
    expect(getPushDeviceState().state).toBe("muted");
  });

  it("a subscription the server still knows is kept", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: true });
    const { startPushDeviceSync } = await freshModule();
    await startPushDeviceSync();
    expect(env.initialSubscription.unsubscribe).not.toHaveBeenCalled();
    expect(env.pushManager.subscribe).not.toHaveBeenCalled();
    expect(env.posts()).toEqual(["/api/push/subscribe"]);
  });
});

describe("enablePushOnThisDevice", () => {
  it("calls requestPermission synchronously, then subscribe → register → test", async () => {
    const env = installPushEnv({ permission: "default" });
    let answer;
    env.requestPermission.mockImplementation(() => new Promise((r) => { answer = r; }));
    const { enablePushOnThisDevice, prefetchVapidKey, getPushDeviceState } = await freshModule();
    await prefetchVapidKey();

    const pending = enablePushOnThisDevice();
    // Nothing awaited yet: the prompt was asked for inside the caller's turn.
    expect(env.requestPermission).toHaveBeenCalledTimes(1);
    expect(env.posts()).toEqual([]);

    env.permission = "granted";
    answer("granted");
    const result = await pending;
    expect(result).toMatchObject({ ok: true, permission: "granted" });
    expect(env.pushManager.subscribe).toHaveBeenCalledTimes(1);
    expect(env.posts()).toEqual(["/api/push/subscribe", "/api/push/test"]);
    expect(env.requests.at(-1).body).toEqual({ endpoint: ENDPOINT });
    expect(getPushDeviceState().state).toBe("on");
  });

  it("unmutes a device the server had muted before sending the test", async () => {
    const env = installPushEnv({ permission: "default", device: { agents_enabled: false } });
    const { enablePushOnThisDevice, getPushDeviceState } = await freshModule();
    await enablePushOnThisDevice();
    expect(env.posts()).toEqual(["/api/push/subscribe", "/api/push/device", "/api/push/test"]);
    expect(env.requests.find((r) => r.url === "/api/push/device").body)
      .toEqual({ endpoint: ENDPOINT, agents_enabled: true });
    expect(getPushDeviceState().state).toBe("on");
  });

  it("stops when the user blocks the prompt", async () => {
    const env = installPushEnv({ permission: "default", answer: "denied" });
    const { enablePushOnThisDevice, getPushDeviceState } = await freshModule();
    const result = await enablePushOnThisDevice();
    expect(result).toEqual({ ok: false, permission: "denied" });
    expect(env.pushManager.subscribe).not.toHaveBeenCalled();
    expect(env.posts()).toEqual([]);
    expect(getPushDeviceState().state).toBe("denied");
  });
});

describe("setThisDeviceAgentsEnabled", () => {
  it("mutes only this device via /api/push/device, never the global settings", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: true });
    const { syncPushDevice, setThisDeviceAgentsEnabled, getPushDeviceState } = await freshModule();
    await syncPushDevice();
    await setThisDeviceAgentsEnabled(false);
    expect(env.requests.at(-1)).toEqual({
      url: "/api/push/device", method: "POST", body: { endpoint: ENDPOINT, agents_enabled: false } });
    expect(getPushDeviceState().state).toBe("muted");
    expect(env.requests.some((r) => r.url.startsWith("/api/settings"))).toBe(false);
  });

  it("when the server forgot this device: fresh subscription, register, then retry on it", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: true, known: false });
    const { setThisDeviceAgentsEnabled, getPushDeviceState } = await freshModule();
    await setThisDeviceAgentsEnabled(false);
    expect(env.posts()).toEqual([
      "/api/push/device", "/api/push/subscribe", "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/device"]);
    expect(env.initialSubscription.unsubscribe).toHaveBeenCalledTimes(1);
    expect(env.requests.at(-1).body).toEqual({ endpoint: ENDPOINT_2, agents_enabled: false });
    expect(getPushDeviceState().state).toBe("muted");
  });
});

describe("source guards", () => {
  // Only enablePushOnThisDevice (a bell tap) may prompt, and nothing in the
  // UI may use the legacy global settings endpoint. Covers callers with no
  // tests of their own (App.jsx startup, AgentsPage).
  const SRC = path.resolve(process.cwd(), "src");
  const sources = fs.readdirSync(SRC, { recursive: true })
    .filter((f) => /\.(js|jsx)$/.test(f) && !/\.test\.jsx?$|testutil/.test(f))
    .map((f) => [f.split(path.sep).join("/"), fs.readFileSync(path.join(SRC, f), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "")]);

  it("requestPermission() is called only from enablePushOnThisDevice", () => {
    const callers = sources.filter(([, code]) => /requestPermission\s*\(/.test(code)).map(([f]) => f);
    expect(callers).toEqual(["lib/pushNotifications.js"]);
    const code = sources.find(([f]) => f === "lib/pushNotifications.js")[1];
    const fn = code.slice(code.indexOf("export function enablePushOnThisDevice"));
    const body = fn.slice(0, fn.indexOf("\nexport ", 1));
    expect(code.match(/requestPermission\s*\(/g)).toHaveLength(1);
    expect(body).toMatch(/requestPermission\s*\(/);
  });

  it("nothing references the legacy /api/settings/notifications endpoint", () => {
    expect(sources.filter(([, code]) => code.includes("settings/notifications")).map(([f]) => f)).toEqual([]);
  });
});
