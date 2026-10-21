import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

// Replaces useIdleLock.test.js: the idle lock is now enforced by the server
// (401 `idle`), and this hook only signals activity and hints on resume.

const MIN = 60 * 1000;

const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});

let calls;
let checkBody;
let pingReply;
function stubFetch() {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url, opts = {}) => {
    calls.push({ url, opts });
    if (url === "/api/auth/check") return res(200, checkBody);
    if (url === "/api/auth/activity") return pingReply();
    return res(200, {});
  }));
}
const hits = (url) => calls.filter((c) => c.url === url);

// Fresh module graph per test: activity.js keeps gesture state per page.
async function setup({ enabled = true, idleTimeoutMinutes = 30 } = {}) {
  vi.resetModules();
  const { default: useActivitySignal } = await import("./useActivitySignal");
  const activity = await import("../lib/activity");
  const hook = renderHook((props) => useActivitySignal(props), { initialProps: { enabled, idleTimeoutMinutes } });
  return { activity, ...hook };
}

const setVisibility = (state) => {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
};
const flush = () => act(async () => { await vi.advanceTimersByTimeAsync(0); });

let expired;
const onExpired = (e) => expired.push(e.detail);

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  localStorage.setItem("cc-auth-token", "xys_a");
  localStorage.setItem("cc-login-ts", String(Date.now() - 60 * MIN));
  checkBody = { authenticated: true, reason: null };
  pingReply = () => res(200, { ok: true, idle_timeout_minutes: 30, idle_remaining_s: 1800 });
  stubFetch();
  expired = [];
  window.addEventListener("auth-expired", onExpired);
  window.history.replaceState({}, "", "/agents");
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  window.removeEventListener("auth-expired", onExpired);
  delete document.visibilityState;
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useActivitySignal", () => {
  it("pings on a gesture at most once a minute", async () => {
    await setup();
    fireGesture();
    fireGesture();
    await flush();
    expect(hits("/api/auth/activity")).toHaveLength(1);
    expect(hits("/api/auth/activity")[0].opts.headers["X-User-Activity"]).toBe("1");

    await act(async () => { await vi.advanceTimersByTimeAsync(59 * 1000); });
    fireGesture();
    await flush();
    expect(hits("/api/auth/activity")).toHaveLength(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    fireGesture();
    await flush();
    expect(hits("/api/auth/activity")).toHaveLength(2);
  });

  it("counts keystrokes that a component swallows (capture phase)", async () => {
    await setup();
    const term = document.createElement("div");
    term.addEventListener("keydown", (e) => e.stopPropagation());
    document.body.appendChild(term);
    term.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true }));
    await flush();
    expect(hits("/api/auth/activity")).toHaveLength(1);
    term.remove();
  });

  it("does not ping when disabled (DISABLE_AUTH / not signed in yet)", async () => {
    const { activity } = await setup({ enabled: false });
    fireGesture();
    await flush();
    expect(hits("/api/auth/activity")).toHaveLength(0);
    // Gesture tracking for the X-User-Activity header still works.
    expect(activity.isUserActive()).toBe(true);
  });

  it("the resume hint checks the server only once the timeout has passed without gestures", async () => {
    await setup({ idleTimeoutMinutes: 30 });
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * MIN); });
    setVisibility("visible");
    await flush();
    expect(hits("/api/auth/check")).toHaveLength(0);

    await act(async () => { await vi.advanceTimersByTimeAsync(21 * MIN); });
    setVisibility("hidden");
    setVisibility("visible");
    await flush();
    expect(hits("/api/auth/check")).toHaveLength(1);
    // The hint is never activity itself.
    expect(hits("/api/auth/check")[0].opts.headers["X-User-Activity"]).toBeUndefined();
    expect(expired).toEqual([]);
  });

  it("a recent gesture postpones the hint", async () => {
    await setup({ idleTimeoutMinutes: 30 });
    await act(async () => { await vi.advanceTimersByTimeAsync(25 * MIN); });
    fireGesture();
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * MIN); });
    setVisibility("visible");
    await flush();
    expect(hits("/api/auth/check")).toHaveLength(0);
  });

  it("an idle answer goes through expireSession: token kept, lock flagged, auth-expired", async () => {
    checkBody = { authenticated: false, reason: "idle" };
    await setup({ idleTimeoutMinutes: 5 });
    await act(async () => { await vi.advanceTimersByTimeAsync(6 * MIN); });
    setVisibility("visible");
    await flush();
    expect(expired).toEqual([{ reason: "idle" }]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
    expect(localStorage.getItem("cc-auth-locked")).toBe("idle");
  });

  it("never clears the token itself and never writes cc-last-activity", async () => {
    await setup({ idleTimeoutMinutes: 5 });
    fireGesture();
    await act(async () => { await vi.advanceTimersByTimeAsync(120 * MIN); });
    await flush();
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
    expect(localStorage.getItem("cc-last-activity")).toBeNull();
    expect(expired).toEqual([]);
  });

  it("idle off (0) or unknown timeout: no hint at all", async () => {
    await setup({ idleTimeoutMinutes: 0 });
    await act(async () => { await vi.advanceTimersByTimeAsync(600 * MIN); });
    setVisibility("visible");
    await flush();
    expect(hits("/api/auth/check")).toHaveLength(0);
  });

  it("nothing at all while disabled, even long past the timeout", async () => {
    await setup({ enabled: false, idleTimeoutMinutes: 5 });
    await act(async () => { await vi.advanceTimersByTimeAsync(60 * MIN); });
    setVisibility("visible");
    fireGesture();
    await flush();
    expect(calls).toEqual([]);
  });

  it("stops after it is disabled (sign-out) and on unmount", async () => {
    const { rerender, unmount } = await setup({ idleTimeoutMinutes: 5 });
    rerender({ enabled: false, idleTimeoutMinutes: 5 });
    fireGesture();
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * MIN); });
    setVisibility("visible");
    await flush();
    expect(calls).toEqual([]);
    unmount();
  });

  it("a 401 on the ping is routed by api.js (here: revoked → token cleared, auth-expired)", async () => {
    pingReply = () => res(401, { detail: "Session revoked" }, { "X-Auth-Reason": "revoked" });
    await setup();
    fireGesture();
    await flush();
    expect(expired).toEqual([{ reason: "revoked" }]);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
  });
});

function fireGesture() {
  window.dispatchEvent(new Event("pointerdown"));
}
