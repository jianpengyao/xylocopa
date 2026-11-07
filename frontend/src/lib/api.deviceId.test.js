import { describe, it, expect, vi, afterEach } from "vitest";

// deviceId(): a random id per browser storage, kept across sign-out and
// sent as X-Device-Id, so the server can tie sessions and push
// registrations to the device (orchestrator/auth_store.py).

const LOGIN_RESPONSE = {
  token: "xys_new", session_id: "s2", expires_at: Date.now() / 1000 + 86400,
  idle_timeout_minutes: 30, expires_minutes: 30, media_token: "m.new",
  media_expires_at: Date.now() / 1000 + 3600,
};

let calls;
function stubFetch(body) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url, opts = {}) => {
    calls.push({ url, opts });
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}
const apiCalls = () => calls.filter((c) => String(c.url).startsWith("/api/") && !String(c.url).includes("/debug/"));

async function load(stored) {
  vi.resetModules();
  localStorage.clear();
  if (stored !== undefined) localStorage.setItem("xy:device-id", stored);
  return import("./api");
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("deviceId", () => {
  it("creates one id and keeps it in localStorage", async () => {
    const api = await load();
    const id = api.deviceId();
    expect(id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(api.deviceId()).toBe(id);
    expect(localStorage.getItem("xy:device-id")).toBe(id);
  });

  it("reuses the stored id after a reload", async () => {
    const api = await load("stored-device-1234");
    expect(api.deviceId()).toBe("stored-device-1234");
  });

  it("replaces a malformed stored id", async () => {
    const api = await load("bad id!");
    const id = api.deviceId();
    expect(id).not.toBe("bad id!");
    expect(localStorage.getItem("xy:device-id")).toBe(id);
  });

  it("survives signing out", async () => {
    const api = await load("stored-device-1234");
    vi.spyOn(console, "warn").mockImplementation(() => {});
    stubFetch({});
    localStorage.setItem("cc-auth-token", "xys_a");
    expect(api.clearAuthToken("test", { force: true })).toBe(true);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    expect(localStorage.getItem("xy:device-id")).toBe("stored-device-1234");
  });
});

describe("X-Device-Id", () => {
  it("goes with a login, even with no token stored", async () => {
    const api = await load("stored-device-1234");
    stubFetch(LOGIN_RESPONSE);
    await api.authLogin("pw-123456");
    const [call] = apiCalls();
    expect(call.url).toBe("/api/auth/login");
    expect(call.opts.headers["X-Device-Id"]).toBe("stored-device-1234");
    expect(JSON.parse(call.opts.body)).toEqual({ password: "pw-123456" });
  });

  it("goes with every API request and authedFetch, next to their own headers", async () => {
    const api = await load("stored-device-1234");
    stubFetch({ sessions: [] });
    await api.fetchAuthSessions();
    await api.authedFetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const [list, subscribe] = apiCalls();
    expect(list.opts.headers["X-Device-Id"]).toBe("stored-device-1234");
    expect(subscribe.opts.headers["X-Device-Id"]).toBe("stored-device-1234");
    expect(subscribe.opts.headers["Content-Type"]).toBe("application/json");
  });
});
