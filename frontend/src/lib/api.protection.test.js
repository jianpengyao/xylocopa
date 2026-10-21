import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// setAuthProtection(): POST /api/auth/protection, which rewrites DISABLE_AUTH
// in the server's .env. api.js keeps module state (login grace timestamp,
// the turn-on counter): every test loads a fresh module graph.

const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});

const SETTINGS_ON = {
  idle_timeout_minutes: 30, choices: [0, 30], default_idle_timeout_minutes: 30, session_max_days: 90,
  auth_disabled: false, password_set: true, env_file_disable_auth: false, env_file_in_sync: true,
};
const SETTINGS_OFF = { ...SETTINGS_ON, auth_disabled: true, env_file_disable_auth: true };
const TURNED_ON = {
  token: "xys_fresh",
  session_id: "s9",
  expires_at: 1e10,
  idle_timeout_minutes: 30,
  expires_minutes: 30,
  media_token: "m.fresh",
  media_expires_at: Date.now() / 1000 + 12 * 3600,
  settings: SETTINGS_ON,
};

let calls;
function stubFetch(handler) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url, opts = {}) => {
    calls.push({ url, opts });
    if (url === "/api/debug/auth-diag") return res(200, { ok: true });
    return handler(url, opts);
  }));
}
const apiCalls = () => calls.filter((c) => c.url !== "/api/debug/auth-diag");
const bodyOf = (call) => JSON.parse(call.opts.body);

async function load({ token, loginAgoMs = 60_000, path = "/monitor" } = {}) {
  vi.resetModules();
  localStorage.clear();
  if (token) {
    localStorage.setItem("cc-auth-token", token);
    localStorage.setItem("cc-login-ts", String(Date.now() - loginAgoMs));
  }
  window.history.replaceState({}, "", path);
  return import("./api");
}

let expired;
const onExpired = (e) => expired.push(e.detail);

beforeEach(() => {
  expired = [];
  window.addEventListener("auth-expired", onExpired);
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  window.removeEventListener("auth-expired", onExpired);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
  localStorage.clear();
});

describe("setAuthProtection", () => {
  it("turning on posts the password + device label and stores the new session", async () => {
    const api = await load();
    stubFetch(() => res(200, TURNED_ON));
    const r = await api.setAuthProtection({ enabled: true, password: "pw-123456", device_label: "Mac · Chrome" });
    expect(apiCalls()).toHaveLength(1);
    expect(apiCalls()[0].url).toBe("/api/auth/protection");
    expect(apiCalls()[0].opts.method).toBe("POST");
    expect(bodyOf(apiCalls()[0])).toEqual({ enabled: true, password: "pw-123456", device_label: "Mac · Chrome" });
    expect(r.settings).toEqual(SETTINGS_ON);
    // Stored like a login: token, media token, login timestamp (grace window).
    expect(api.getAuthToken()).toBe("xys_fresh");
    expect(api.getMediaToken()).toBe("m.fresh");
    expect(api.getLoginTs()).toBeGreaterThan(Date.now() - 5000);
  });

  it("with no password set yet it sends new_password", async () => {
    const api = await load();
    stubFetch(() => res(200, TURNED_ON));
    await api.setAuthProtection({ enabled: true, password: "", new_password: "long-enough-1", device_label: "iPhone · Safari" });
    expect(bodyOf(apiCalls()[0])).toEqual({
      enabled: true, password: "", new_password: "long-enough-1", device_label: "iPhone · Safari",
    });
  });

  it("turning off sends the password with the session as Bearer and keeps that session", async () => {
    const api = await load({ token: "xys_me" });
    stubFetch(() => res(200, { settings: SETTINGS_OFF }));
    const r = await api.setAuthProtection({ enabled: false, password: "pw-123456", device_label: "ignored" });
    expect(bodyOf(apiCalls()[0])).toEqual({ enabled: false, password: "pw-123456" });
    expect(apiCalls()[0].opts.headers.Authorization).toBe("Bearer xys_me");
    expect(r).toEqual({ settings: SETTINGS_OFF });
    expect(api.getAuthToken()).toBe("xys_me");
  });

  it.each([
    [403, "Password is wrong"],
    [429, "Too many failed attempts. Try again in 42s."],
    [500, "Could not update .env: Permission denied"],
  ])("a %i rejects with the server detail and never logs anyone out", async (status, detail) => {
    const api = await load({ token: "xys_me" });
    stubFetch(() => res(status, { detail }));
    const err = await api.setAuthProtection({ enabled: false, password: "nope" }).catch((e) => e);
    expect(err).not.toBeInstanceOf(api.AuthError);
    expect(err.message).toBe(detail);
    expect(err.status).toBe(status);
    expect(expired).toEqual([]);
    expect(api.getAuthToken()).toBe("xys_me");
  });

  it("a failed turn-on stores nothing", async () => {
    const api = await load();
    stubFetch(() => res(403, { detail: "Password is wrong" }));
    await expect(api.setAuthProtection({ enabled: true, password: "nope" })).rejects.toMatchObject({ status: 403 });
    expect(api.getAuthToken()).toBeNull();
    expect(expired).toEqual([]);
  });
});

describe("token-less requests racing a turn-on", () => {
  // The server switches auth on before its answer (this tab's session)
  // arrives: a poll sent without a token in that window gets 401 missing.
  it("are not a logout while the turn-on is in flight", async () => {
    const api = await load();
    let answer;
    stubFetch((url) => {
      if (url === "/api/auth/protection") return new Promise((r) => { answer = () => r(res(200, TURNED_ON)); });
      return res(401, { detail: "Not authenticated" }, { "X-Auth-Reason": "missing" });
    });
    const turningOn = api.setAuthProtection({ enabled: true, password: "pw-123456" });
    const err = await api.fetchProjects().catch((e) => e);
    expect(err).toBeInstanceOf(api.AuthError);
    expect(expired).toEqual([]);
    answer();
    await turningOn;
    expect(api.getAuthToken()).toBe("xys_fresh");
    expect(expired).toEqual([]);
  });

  it("are a logout again once no turn-on is in flight (failed or done)", async () => {
    const api = await load();
    stubFetch((url) => {
      if (url === "/api/auth/protection") return res(403, { detail: "Password is wrong" });
      return res(401, { detail: "Not authenticated" }, { "X-Auth-Reason": "missing" });
    });
    await api.setAuthProtection({ enabled: true, password: "nope" }).catch(() => {});
    await expect(api.fetchProjects()).rejects.toBeInstanceOf(api.AuthError);
    expect(expired).toEqual([{ reason: "missing" }]);
  });

  it("turning off does not shield token-less failures", async () => {
    const api = await load();
    let answer;
    stubFetch((url) => {
      if (url === "/api/auth/protection") return new Promise((r) => { answer = () => r(res(200, { settings: SETTINGS_OFF })); });
      return res(401, { detail: "Not authenticated" }, { "X-Auth-Reason": "missing" });
    });
    const turningOff = api.setAuthProtection({ enabled: false, password: "pw-123456" });
    await expect(api.fetchProjects()).rejects.toBeInstanceOf(api.AuthError);
    expect(expired).toEqual([{ reason: "missing" }]);
    answer();
    await turningOff;
  });
});

describe("fetchAuthProtection", () => {
  it("GETs the switch's state from /api/auth/protection", async () => {
    const state = { auth_disabled: true, password_set: true, env_file_disable_auth: true, env_file_in_sync: true };
    stubFetch(() => res(200, state));
    const api = await load();
    await expect(api.fetchAuthProtection()).resolves.toEqual(state);
    expect(apiCalls().map((c) => [c.url, c.opts.method || "GET"])).toEqual([["/api/auth/protection", "GET"]]);
  });
});
