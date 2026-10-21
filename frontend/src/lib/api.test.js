import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// api.js seeds its login-grace timestamp from localStorage at import, and
// activity.js keeps the last gesture in module state: every test loads a
// fresh module graph.

const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});

const LOGIN_RESPONSE = {
  token: "xys_new",
  session_id: "s2",
  expires_at: 1e10,
  idle_timeout_minutes: 30,
  expires_minutes: 30,
  media_token: "m.new",
  media_expires_at: Date.now() / 1000 + 12 * 3600,
};

let calls;
function stubFetch(handler) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url, opts = {}) => {
    calls.push({ url, opts });
    // clearAuthToken() fires a best-effort diagnostics beacon.
    if (url === "/api/debug/auth-diag") return res(200, { ok: true });
    return handler(url, opts);
  }));
}
const apiCalls = () => calls.filter((c) => c.url !== "/api/debug/auth-diag");
const header = (call, name) => call.opts.headers?.[name];

async function load({ token, loginAgoMs = 60_000, path = "/agents", media, locked } = {}) {
  vi.resetModules();
  localStorage.clear();
  if (token) {
    localStorage.setItem("cc-auth-token", token);
    localStorage.setItem("cc-login-ts", String(Date.now() - loginAgoMs));
  }
  if (media) localStorage.setItem("cc-media-token", JSON.stringify(media));
  if (locked) localStorage.setItem("cc-auth-locked", locked);
  window.history.replaceState({}, "", path);
  const api = await import("./api");
  const activity = await import("./activity");
  return { api, activity };
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
  vi.useRealTimers();
  window.history.replaceState({}, "", "/");
});

describe("401 handling", () => {
  it("a 401 with X-Auth-Reason clears the session and dispatches auth-expired once", async () => {
    const { api } = await load({ token: "xys_a", media: { token: "m", exp: Date.now() / 1000 + 3600 } });
    stubFetch(() => res(401, { detail: "Session revoked" }, { "X-Auth-Reason": "revoked" }));
    const err = await api.fetchProjects().catch((e) => e);
    expect(err).toBeInstanceOf(api.AuthError);
    expect(err.reason).toBe("revoked");
    expect(err.message).toBe("Not authenticated");
    expect(expired).toEqual([{ reason: "revoked" }]);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    expect(localStorage.getItem("cc-media-token")).toBeNull();
  });

  it("a 401 without X-Auth-Reason is a plain error and never logs out", async () => {
    const { api } = await load({ token: "xys_a" });
    stubFetch(() => res(401, { detail: "Invalid preview token" }));
    const err = await api.mintPreviewToken("proj").catch((e) => e);
    expect(err).not.toBeInstanceOf(api.AuthError);
    expect(err.message).toBe("Invalid preview token");
    expect(err.status).toBe(401);
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
  });

  it("a wrong current password on change-password (403) keeps the session", async () => {
    const { api } = await load({ token: "xys_a" });
    stubFetch(() => res(403, { detail: "Current password is wrong" }));
    const err = await api.authChangePassword("bad", "new-password-1").catch((e) => e);
    expect(err.message).toBe("Current password is wrong");
    expect(err.status).toBe(403);
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
  });

  it("a clear refused by the post-login grace window does not dispatch", async () => {
    const { api } = await load({ token: "xys_a", loginAgoMs: 1000 });
    stubFetch(() => res(401, { detail: "Session revoked" }, { "X-Auth-Reason": "revoked" }));
    await expect(api.fetchProjects()).rejects.toBeInstanceOf(api.AuthError);
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
  });

  it("clearAuthToken(reason, {force:true}) ignores the grace window (deliberate sign-out)", async () => {
    const { api } = await load({ token: "xys_a", loginAgoMs: 500, media: { token: "m", exp: Date.now() / 1000 + 3600 } });
    stubFetch(() => res(200, {}));
    expect(api.clearAuthToken("logout")).toBe(false);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
    expect(api.clearAuthToken("logout", { force: true })).toBe(true);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    expect(localStorage.getItem("cc-media-token")).toBeNull();
    expect(localStorage.getItem("cc-login-ts")).toBeNull();
  });

  it("idle keeps the token, flags the lock and dispatches with the reason", async () => {
    const { api } = await load({ token: "xys_a" });
    stubFetch(() => res(401, { detail: "Locked after inactivity" }, { "X-Auth-Reason": "idle" }));
    const err = await api.fetchProjects().catch((e) => e);
    expect(err.reason).toBe("idle");
    expect(expired).toEqual([{ reason: "idle" }]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
    expect(localStorage.getItem("cc-auth-locked")).toBe("idle");
    expect(api.getAuthLockReason()).toBe("idle");
  });

  it("a 401 for a token replaced while the request was in flight is a no-op", async () => {
    const { api } = await load({ token: "xys_old" });
    stubFetch(() => {
      // A login completes while this request is on the wire.
      localStorage.setItem("cc-auth-token", "xys_new");
      return res(401, { detail: "Session revoked" }, { "X-Auth-Reason": "revoked" });
    });
    await expect(api.fetchProjects()).rejects.toBeInstanceOf(api.AuthError);
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_new");
  });

  it("does not dispatch while already on /login, but still clears", async () => {
    const { api } = await load({ token: "xys_a", path: "/login" });
    stubFetch(() => res(401, { detail: "Session expired" }, { "X-Auth-Reason": "expired" }));
    await expect(api.fetchProjects()).rejects.toBeInstanceOf(api.AuthError);
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
  });

  it("uploads (formDataRequest) route a session 401 the same way", async () => {
    const { api } = await load({ token: "xys_a" });
    stubFetch(() => res(401, { detail: "Session revoked" }, { "X-Auth-Reason": "revoked" }));
    await expect(api.uploadFile(new Blob(["x"]))).rejects.toBeInstanceOf(api.AuthError);
    expect(expired).toEqual([{ reason: "revoked" }]);
  });

  it("expireSession without a request token acts on the current token (WebSocket close)", async () => {
    const { api } = await load({ token: "xys_a" });
    stubFetch(() => res(200));
    await expect(api.expireSession("revoked", { source: "ws-status" })).rejects.toMatchObject({ reason: "revoked" });
    expect(expired).toEqual([{ reason: "revoked" }]);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
  });
});

describe("X-User-Activity", () => {
  it("rides along only within 5 s of a user gesture", async () => {
    const { api, activity } = await load({ token: "xys_a" });
    stubFetch(() => res(200, {}));
    await api.fetchProjects();
    expect(header(apiCalls()[0], "X-User-Activity")).toBeUndefined();

    // Pin the clock before the gesture: a real millisecond between reading
    // t0 and marking would put t0 + 5001 back inside the window.
    const t0 = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(t0);
    activity.markUserGesture();
    await api.fetchProjects();
    expect(header(apiCalls()[1], "X-User-Activity")).toBe("1");

    clock.mockReturnValue(t0 + 5001);
    await api.fetchProjects();
    expect(header(apiCalls()[2], "X-User-Activity")).toBeUndefined();
  });

  it("authedFetch follows the gesture window; uploads and the ping always send it", async () => {
    const { api, activity } = await load({ token: "xys_a" });
    stubFetch(() => res(200, {}));
    await api.authedFetch("/api/push/subscribe", { method: "POST" });
    expect(header(apiCalls()[0], "X-User-Activity")).toBeUndefined();
    activity.markUserGesture();
    await api.authedFetch("/api/push/subscribe", { method: "POST" });
    expect(header(apiCalls()[1], "X-User-Activity")).toBe("1");

    const { api: fresh } = await load({ token: "xys_a" });
    stubFetch(() => res(200, {}));
    await fresh.uploadFile(new Blob(["x"]));
    await fresh.pingActivity();
    expect(apiCalls().map((c) => header(c, "X-User-Activity"))).toEqual(["1", "1"]);
  });

  it("authCheck({activity:false}) never sends it, even right after a gesture", async () => {
    const { api, activity } = await load({ token: "xys_a" });
    stubFetch(() => res(200, { authenticated: true }));
    activity.markUserGesture();
    await api.authCheck({ activity: false });
    expect(header(apiCalls()[0], "X-User-Activity")).toBeUndefined();
  });

  it("capture-phase listeners count keystrokes a component swallows (xterm)", async () => {
    const { activity } = await load();
    activity.installGestureListeners();
    activity.installGestureListeners(); // idempotent
    const box = document.createElement("div");
    box.addEventListener("keydown", (e) => e.stopPropagation());
    document.body.appendChild(box);
    const seen = vi.fn();
    const off = activity.onGesture(seen);
    expect(activity.isUserActive()).toBe(false);
    box.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true }));
    expect(activity.isUserActive()).toBe(true);
    expect(seen).toHaveBeenCalledTimes(1);
    off();
    box.remove();
  });
});

describe("withTimeout / authCheck", () => {
  it("abandons an auth check after 8 s with AuthTimeoutError and aborts the fetch", async () => {
    vi.useFakeTimers();
    const { api } = await load({ token: "xys_a" });
    let signal;
    stubFetch((url, opts) => new Promise((_, reject) => {
      signal = opts.signal;
      opts.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    }));
    const p = api.withTimeout((s) => api.authCheck({ signal: s }), 8000);
    const outcome = p.catch((e) => e);
    await vi.advanceTimersByTimeAsync(7999);
    expect(signal.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const err = await outcome;
    expect(err).toBeInstanceOf(api.AuthTimeoutError);
    expect(signal.aborted).toBe(true);
  });

  it("times out even when the callee ignores the signal", async () => {
    vi.useFakeTimers();
    const { api } = await load();
    const outcome = api.withTimeout(() => new Promise(() => {}), 8000).catch((e) => e);
    await vi.advanceTimersByTimeAsync(8000);
    expect(await outcome).toBeInstanceOf(api.AuthTimeoutError);
  });

  it("an outer abort (Retry) rejects with AbortError, not a timeout", async () => {
    vi.useFakeTimers();
    const { api } = await load();
    const outer = new AbortController();
    let inner;
    const outcome = api.withTimeout((s) => { inner = s; return new Promise(() => {}); }, 8000, { signal: outer.signal })
      .catch((e) => e);
    await vi.advanceTimersByTimeAsync(0);
    outer.abort();
    const err = await outcome;
    expect(err.name).toBe("AbortError");
    expect(inner.aborted).toBe(true);
  });

  it("passes the signal through authCheck to fetch and resolves normally", async () => {
    const { api } = await load({ token: "xys_a" });
    stubFetch(() => res(200, { authenticated: true }));
    const r = await api.withTimeout((s) => api.authCheck({ signal: s }), 8000);
    expect(r).toEqual({ authenticated: true });
    expect(apiCalls()[0].opts.signal).toBeInstanceOf(AbortSignal);
    expect(header(apiCalls()[0], "Authorization")).toBe("Bearer xys_a");
  });
});

describe("media token", () => {
  it("getMediaToken honours expiry", async () => {
    const { api } = await load();
    const now = Date.now() / 1000;
    expect(api.getMediaToken()).toBeNull();
    api.setMediaToken("m1", now + 3600);
    expect(api.getMediaToken()).toBe("m1");
    expect(api.getMediaTokenExp()).toBeCloseTo(now + 3600);
    api.setMediaToken("m2", now - 1);
    expect(api.getMediaToken()).toBeNull();
    // Inside the 30 s safety margin counts as expired.
    api.setMediaToken("m3", now + 10);
    expect(api.getMediaToken()).toBeNull();
    api.setMediaToken("m4", null);
    expect(api.getMediaToken()).toBe("m4");
    api.setMediaToken(null);
    expect(localStorage.getItem("cc-media-token")).toBeNull();
  });

  it("ignores a corrupt stored value", async () => {
    const { api } = await load();
    localStorage.setItem("cc-media-token", "{not json");
    expect(api.getMediaToken()).toBeNull();
  });

  it("mintMediaToken stores the new token; a null token (auth off) clears it", async () => {
    const { api } = await load({ token: "xys_a", media: { token: "m.old", exp: Date.now() / 1000 + 60 } });
    const exp = Date.now() / 1000 + 43200;
    stubFetch(() => res(200, { token: "m.fresh", expires_at: exp, expires_in: 43200 }));
    await api.mintMediaToken();
    expect(api.getMediaToken()).toBe("m.fresh");
    stubFetch(() => res(200, { token: null, expires_at: null, expires_in: 0 }));
    await api.mintMediaToken();
    expect(api.getMediaToken()).toBeNull();
  });

  it("subscribeMediaToken hears this tab's setMediaToken and another tab's storage write", async () => {
    const { api } = await load();
    const seen = vi.fn();
    const off = api.subscribeMediaToken(seen);
    api.setMediaToken("m1", Date.now() / 1000 + 3600);
    expect(seen).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new StorageEvent("storage", { key: "cc-media-token" }));
    window.dispatchEvent(new StorageEvent("storage", { key: "unrelated" }));
    expect(seen).toHaveBeenCalledTimes(2);
    off();
    api.setMediaToken(null);
    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("mintMediaToken does not store a token minted for a session that was replaced meanwhile", async () => {
    const { api } = await load({ token: "xys_old", media: { token: "m.keep", exp: Date.now() / 1000 + 3600 } });
    stubFetch(() => {
      localStorage.setItem("cc-auth-token", "xys_new");
      return res(200, { token: "m.stale", expires_at: Date.now() / 1000 + 43200, expires_in: 43200 });
    });
    await api.mintMediaToken();
    expect(api.getMediaToken()).toBe("m.keep");
  });
});

describe("session storage", () => {
  it("setAuthSession stores token + media token and drops the lock flag", async () => {
    const { api } = await load({ token: "xys_old", locked: "idle" });
    api.setAuthSession(LOGIN_RESPONSE);
    expect(api.getAuthToken()).toBe("xys_new");
    expect(api.getMediaToken()).toBe("m.new");
    expect(api.getAuthLockReason()).toBeNull();
    expect(api.getLoginTs()).toBeGreaterThan(Date.now() - 1000);
  });

  it("setAuthToken (bare token) drops a media token of the previous session", async () => {
    const { api } = await load({ token: "xys_old", media: { token: "m.old", exp: Date.now() / 1000 + 3600 } });
    api.setAuthToken("xys_bare");
    expect(api.getAuthToken()).toBe("xys_bare");
    expect(api.getMediaToken()).toBeNull();
  });

  it("clearAuthToken also clears the media token and the lock flag", async () => {
    const { api } = await load({ token: "xys_a", locked: "idle", media: { token: "m", exp: Date.now() / 1000 + 3600 } });
    stubFetch(() => res(200));
    expect(api.clearAuthToken("test")).toBe(true);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    expect(localStorage.getItem("cc-media-token")).toBeNull();
    expect(localStorage.getItem("cc-auth-locked")).toBeNull();
  });
});

describe("login / password wrappers", () => {
  it("login sends the stored (locked) token as Bearer and the device label", async () => {
    const { api } = await load({ token: "xys_locked", locked: "idle" });
    stubFetch(() => res(200, LOGIN_RESPONSE));
    const r = await api.authLogin("pw-123456", { deviceLabel: "iPhone · Safari" });
    expect(r).toEqual(LOGIN_RESPONSE);
    const call = apiCalls()[0];
    expect(call.url).toBe("/api/auth/login");
    expect(header(call, "Authorization")).toBe("Bearer xys_locked");
    expect(header(call, "Content-Type")).toBe("application/json");
    expect(JSON.parse(call.opts.body)).toEqual({ password: "pw-123456", device_label: "iPhone · Safari" });
  });

  it("login without a stored token sends no Authorization and no empty label", async () => {
    const { api } = await load();
    stubFetch(() => res(200, LOGIN_RESPONSE));
    await api.authLogin("pw-123456", { deviceLabel: null });
    expect(header(apiCalls()[0], "Authorization")).toBeUndefined();
    expect(JSON.parse(apiCalls()[0].opts.body)).toEqual({ password: "pw-123456" });
  });

  it("a wrong password or lockout throws the server detail without any logout", async () => {
    const { api } = await load({ token: "xys_a" });
    stubFetch(() => res(401, { detail: "Wrong password" }));
    await expect(api.authLogin("nope")).rejects.toMatchObject({ message: "Wrong password", status: 401 });
    stubFetch(() => res(429, { detail: "Too many failed attempts. Try again in 42s." }, { "Retry-After": "42" }));
    await expect(api.authLogin("nope")).rejects.toMatchObject({ status: 429 });
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
  });

  it("change-password stores the replacement session before resolving", async () => {
    const { api } = await load({ token: "xys_old" });
    stubFetch(() => res(200, { ...LOGIN_RESPONSE, revoked_sessions: 3 }));
    const r = await api.authChangePassword("old-password", "new-password-1");
    expect(r.revoked_sessions).toBe(3);
    expect(api.getAuthToken()).toBe("xys_new");
    expect(api.getMediaToken()).toBe("m.new");
    expect(JSON.parse(apiCalls()[0].opts.body)).toEqual({ current_password: "old-password", new_password: "new-password-1" });
    expect(header(apiCalls()[0], "Authorization")).toBe("Bearer xys_old");
  });

  it("set-password returns the LoginResponse and sends the device label", async () => {
    const { api } = await load();
    stubFetch(() => res(200, LOGIN_RESPONSE));
    const r = await api.authSetPassword("pw-123456", { deviceLabel: "Mac · Chrome" });
    expect(r.token).toBe("xys_new");
    expect(JSON.parse(apiCalls()[0].opts.body)).toEqual({ password: "pw-123456", device_label: "Mac · Chrome" });
  });

  it("session wrappers hit the contract endpoints", async () => {
    const { api } = await load({ token: "xys_a" });
    stubFetch(() => res(200, {}));
    await api.authLogout();
    await api.fetchAuthSessions();
    await api.revokeAuthSession("ab/cd");
    await api.revokeOtherSessions();
    await api.fetchWsTicket();
    await api.fetchAuthSettings();
    await api.updateAuthSettings({ idle_timeout_minutes: 15 });
    expect(apiCalls().map((c) => `${c.opts.method || "GET"} ${c.url}`)).toEqual([
      "POST /api/auth/logout",
      "GET /api/auth/sessions",
      "POST /api/auth/sessions/ab%2Fcd/revoke",
      "POST /api/auth/sessions/revoke-others",
      "POST /api/auth/ws-ticket",
      "GET /api/settings/auth",
      "PUT /api/settings/auth",
    ]);
    expect(JSON.parse(apiCalls()[6].opts.body)).toEqual({ idle_timeout_minutes: 15 });
  });
});

describe("deviceLabel", () => {
  // jsdom has no navigator.maxTouchPoints; define it per case.
  const withUA = (ua, { standalone = false, touch = 0 } = {}) => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(ua);
    Object.defineProperty(navigator, "maxTouchPoints", { value: touch, configurable: true });
    vi.stubGlobal("matchMedia", (q) => ({ matches: standalone && q === "(display-mode: standalone)" }));
  };
  afterEach(() => { delete navigator.maxTouchPoints; });

  it("derives OS · browser from the User-Agent", async () => {
    const { api } = await load();
    withUA("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1");
    expect(api.deviceLabel()).toBe("iPhone · Safari");
    withUA("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36");
    expect(api.deviceLabel()).toBe("Mac · Chrome");
    withUA("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0");
    expect(api.deviceLabel()).toBe("Windows · Edge");
    withUA("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0");
    expect(api.deviceLabel()).toBe("Linux · Firefox");
  });

  it("tells an iPad (desktop UA + touch) and the installed PWA apart", async () => {
    const { api } = await load();
    withUA("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15", { touch: 5 });
    expect(api.deviceLabel()).toBe("iPad · Safari");
    withUA("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148", { standalone: true });
    expect(api.deviceLabel()).toBe("iPhone · Installed app");
  });

  it("returns null when nothing is recognisable", async () => {
    const { api } = await load();
    withUA("curl/8.0");
    expect(api.deviceLabel()).toBeNull();
  });
});
