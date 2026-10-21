import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { WebSocketProvider } from "./WebSocketContext";
import { AuthProvider } from "./AuthContext";
import {
  RESUME_PROBE_DELAY_MS,
  WS_PING_INTERVAL_MS as PING_INTERVAL_MS,
  WS_PONG_TIMEOUT_MS as PONG_TIMEOUT_MS,
  WS_HANDSHAKE_TIMEOUT_MS as HANDSHAKE_TIMEOUT_MS,
  WS_INITIAL_RECONNECT_MS as INITIAL_RECONNECT_MS,
} from "../lib/reconnect";

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.CONNECTING;
    this.sent = [];
    MockWebSocket.instances.push(this);
  }
  send(data) { this.sent.push(data); }
  close() {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({});
  }
  // test helpers
  _open() { this.readyState = MockWebSocket.OPEN; this.onopen?.(); }
  _message(obj) { this.onmessage?.({ data: JSON.stringify(obj) }); }
  // server-side drop: fires onclose without the client calling close();
  // `code`/`reason` model a server close frame (e.g. 4001 "idle")
  _drop(code, reason) { this.readyState = MockWebSocket.CLOSED; this.onclose?.({ code, reason }); }
}

const sockets = () => MockWebSocket.instances;
// Note: a 0ms timer created while the fake clock is ticking is due at now+1,
// so "run the reconnect scheduled by the previous step" is advance(1).
const advance = (ms) => act(() => { vi.advanceTimersByTime(ms); });

describe("WebSocketProvider reconnect policy", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    delete document.visibilityState;
  });

  it("replaces a half-open socket when a ping gets no reply", () => {
    render(<WebSocketProvider><div /></WebSocketProvider>);
    expect(sockets()).toHaveLength(1);
    act(() => sockets()[0]._open());

    advance(PING_INTERVAL_MS);
    expect(sockets()[0].sent).toContain("ping");
    // No frame comes back → socket is dropped and a new one opened at once.
    advance(PONG_TIMEOUT_MS);
    expect(sockets()[0].readyState).toBe(MockWebSocket.CLOSED);
    advance(1);
    expect(sockets()).toHaveLength(2);
    // The dropped socket's own onclose must not have queued another reconnect.
    advance(INITIAL_RECONNECT_MS * 5);
    expect(sockets()).toHaveLength(2);
  });

  it("keeps the socket when the ping is answered", () => {
    render(<WebSocketProvider><div /></WebSocketProvider>);
    act(() => sockets()[0]._open());
    advance(PING_INTERVAL_MS);
    act(() => sockets()[0]._message({ type: "pong" }));
    advance(PONG_TIMEOUT_MS + 1);
    expect(sockets()).toHaveLength(1);
    expect(sockets()[0].readyState).toBe(MockWebSocket.OPEN);
  });

  it("abandons a handshake that never completes", () => {
    render(<WebSocketProvider><div /></WebSocketProvider>);
    expect(sockets()[0].readyState).toBe(MockWebSocket.CONNECTING);
    advance(HANDSHAKE_TIMEOUT_MS);
    expect(sockets()[0].readyState).toBe(MockWebSocket.CLOSED);
    advance(INITIAL_RECONNECT_MS);
    expect(sockets()).toHaveLength(2);
  });

  it("reconnects right away when the network comes back instead of waiting out the backoff", () => {
    render(<WebSocketProvider><div /></WebSocketProvider>);
    act(() => sockets()[0]._open());
    // Several server-side drops grow the backoff well past 1s.
    for (let i = 0; i < 6; i++) {
      const ws = sockets()[sockets().length - 1];
      act(() => ws._drop());
      advance(30000);
    }
    const before = sockets().length;
    const last = sockets()[before - 1];
    act(() => last._drop()); // now waiting on a long backoff (~11s)
    advance(1000);
    expect(sockets()).toHaveLength(before);

    act(() => { window.dispatchEvent(new Event("online")); });
    advance(RESUME_PROBE_DELAY_MS + 1);
    expect(sockets()).toHaveLength(before + 1);
  });

  it("probes an open socket on foreground and replaces it if silent", () => {
    render(<WebSocketProvider><div /></WebSocketProvider>);
    act(() => sockets()[0]._open());
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    advance(RESUME_PROBE_DELAY_MS);
    expect(sockets()[0].sent.filter((m) => m === "ping")).toHaveLength(1);
    advance(PONG_TIMEOUT_MS);
    expect(sockets()[0].readyState).toBe(MockWebSocket.CLOSED);
    advance(1);
    expect(sockets()).toHaveLength(2);
  });

  it("never gives up: keeps retrying after many failures", () => {
    render(<WebSocketProvider><div /></WebSocketProvider>);
    for (let i = 0; i < 40; i++) {
      const ws = sockets()[sockets().length - 1];
      act(() => ws._drop());
      advance(30000);
    }
    expect(sockets().length).toBeGreaterThan(40);
  });
});

// --- Auth: one-time tickets and the 4001 close code -----------------------

const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});
const AUTH_ON = { authReady: true, authDisabled: false, session: null, idleTimeoutMinutes: 30, refresh: async () => null };
const AUTH_OFF = { ...AUTH_ON, authDisabled: true };

// ticket fetches answer from `ticketQueue` (one handler per call; default:
// a fresh numbered ticket). Every call is recorded.
let ticketCalls;
let ticketQueue;
let ticketNo;
function stubTicketFetch() {
  ticketCalls = [];
  ticketQueue = [];
  ticketNo = 0;
  vi.stubGlobal("fetch", vi.fn((url, opts = {}) => {
    if (url === "/api/debug/auth-diag") return Promise.resolve(res(200, { ok: true }));
    if (url !== "/api/auth/ws-ticket") return Promise.resolve(res(404, { detail: "unexpected" }));
    ticketCalls.push({ url, opts });
    const handler = ticketQueue.shift() || (() => res(200, { ticket: `xyt_${++ticketNo}`, expires_in: 30 }));
    return new Promise((resolve, reject) => {
      opts.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      Promise.resolve().then(handler).then(resolve, reject);
    });
  }));
}
// Let pending promise callbacks (the ticket fetch chain) run.
const flush = () => act(async () => { await vi.advanceTimersByTimeAsync(0); });
const renderWithAuth = (auth = AUTH_ON) =>
  render(<AuthProvider value={auth}><WebSocketProvider><div /></WebSocketProvider></AuthProvider>);

describe("WebSocketProvider auth (tickets, 4001)", () => {
  let expired;
  const onExpired = (e) => expired.push(e.detail);
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    localStorage.clear();
    localStorage.setItem("cc-auth-token", "xys_tab");
    stubTicketFetch();
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
    delete document.visibilityState;
    localStorage.clear();
  });

  it("fetches a ticket before opening, and puts only the ticket in the URL", async () => {
    let release;
    ticketQueue.push(() => new Promise((r) => { release = () => r(res(200, { ticket: "xyt_1", expires_in: 30 })); }));
    renderWithAuth();
    await flush();
    expect(ticketCalls).toHaveLength(1);
    expect(sockets()).toHaveLength(0);
    expect(ticketCalls[0].opts.method).toBe("POST");
    expect(ticketCalls[0].opts.headers.Authorization).toBe("Bearer xys_tab");
    expect(ticketCalls[0].opts.headers["X-User-Activity"]).toBeUndefined();
    release();
    await flush();
    expect(sockets()).toHaveLength(1);
    const url = new URL(sockets()[0].url);
    expect(url.pathname).toBe("/ws/status");
    expect(url.searchParams.get("ticket")).toBe("xyt_1");
    expect(url.searchParams.has("token")).toBe(false);
    expect(sockets()[0].url).not.toContain("xys_");
  });

  it("starts only one connect while a ticket is in flight", async () => {
    let release;
    ticketQueue.push(() => new Promise((r) => { release = () => r(res(200, { ticket: "xyt_slow", expires_in: 30 })); }));
    renderWithAuth();
    await flush();
    // A resume and an online event during the fetch must not start another.
    act(() => { window.dispatchEvent(new Event("online")); });
    advance(RESUME_PROBE_DELAY_MS + 1);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    advance(RESUME_PROBE_DELAY_MS + 1);
    expect(ticketCalls).toHaveLength(1);
    expect(sockets()).toHaveLength(0);
    release();
    await flush();
    expect(sockets()).toHaveLength(1);
    expect(sockets()[0].url).toContain("ticket=xyt_slow");
    expect(ticketCalls).toHaveLength(1);
  });

  it("4001 idle: locks the session, dispatches auth-expired, never reconnects", async () => {
    renderWithAuth();
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(4001, "idle"));
    await flush();
    expect(expired).toEqual([{ reason: "idle" }]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_tab");
    expect(localStorage.getItem("cc-auth-locked")).toBe("idle");
    advance(120000);
    act(() => { window.dispatchEvent(new Event("online")); });
    advance(RESUME_PROBE_DELAY_MS + 1);
    await flush();
    expect(sockets()).toHaveLength(1);
    expect(ticketCalls).toHaveLength(1);
  });

  it("4001 revoked: clears the session and does not reconnect", async () => {
    renderWithAuth();
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(4001, "revoked"));
    await flush();
    expect(expired).toEqual([{ reason: "revoked" }]);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    advance(120000);
    await flush();
    expect(sockets()).toHaveLength(1);
    expect(ticketCalls).toHaveLength(1);
  });

  it("4001 expired for a session another tab already replaced: no logout, reconnects", async () => {
    renderWithAuth();
    await flush();
    act(() => sockets()[0]._open());
    localStorage.setItem("cc-auth-token", "xys_newer");
    act(() => sockets()[0]._drop(4001, "expired"));
    await flush();
    advance(1);
    await flush();
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_newer");
    expect(ticketCalls).toHaveLength(2);
    expect(ticketCalls[1].opts.headers.Authorization).toBe("Bearer xys_newer");
    expect(sockets()).toHaveLength(2);
  });

  it("4001 invalid / missing: fetches a fresh ticket and reconnects, with growing backoff", async () => {
    renderWithAuth();
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(4001, "invalid"));
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_tab");
    advance(INITIAL_RECONNECT_MS);
    await flush();
    expect(ticketCalls).toHaveLength(2);
    expect(sockets()).toHaveLength(2);
    expect(sockets()[1].url).toContain("ticket=xyt_2");
    // Accepted-then-closed again: onopen fired, yet the delay keeps growing.
    act(() => sockets()[1]._open());
    act(() => sockets()[1]._drop(4001, "missing"));
    advance(INITIAL_RECONNECT_MS);
    await flush();
    expect(sockets()).toHaveLength(2);
    advance(INITIAL_RECONNECT_MS);
    await flush();
    expect(sockets()).toHaveLength(3);
    expect(expired).toEqual([]);
  });

  it("an ordinary drop reconnects with a fresh ticket (tickets are single use)", async () => {
    renderWithAuth();
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(1006, ""));
    advance(INITIAL_RECONNECT_MS);
    await flush();
    expect(sockets()).toHaveLength(2);
    expect(sockets()[1].url).toContain("ticket=xyt_2");
  });

  it("authDisabled: no ticket fetch, bare URL", () => {
    renderWithAuth(AUTH_OFF);
    expect(ticketCalls).toHaveLength(0);
    expect(sockets()).toHaveLength(1);
    expect(sockets()[0].url).toMatch(/\/ws\/status$/);
  });

  it("a null ticket (server has auth off) connects bare", async () => {
    ticketQueue.push(() => res(200, { ticket: null, expires_in: 0 }));
    renderWithAuth();
    await flush();
    expect(sockets()).toHaveLength(1);
    expect(sockets()[0].url).toMatch(/\/ws\/status$/);
  });

  it("ticket fetch 401 with a reason: one auth-expired (from api.js), no socket, no retry", async () => {
    ticketQueue.push(() => res(401, { detail: "Session revoked" }, { "X-Auth-Reason": "revoked" }));
    renderWithAuth();
    await flush();
    expect(expired).toEqual([{ reason: "revoked" }]);
    expect(sockets()).toHaveLength(0);
    advance(120000);
    act(() => { window.dispatchEvent(new Event("online")); });
    advance(RESUME_PROBE_DELAY_MS + 1);
    await flush();
    expect(ticketCalls).toHaveLength(1);
    expect(sockets()).toHaveLength(0);
  });

  it("ticket fetch network error / 5xx: retries with the normal backoff", async () => {
    ticketQueue.push(() => Promise.reject(new TypeError("Failed to fetch")));
    ticketQueue.push(() => res(503, { detail: "Auth store unavailable" }));
    renderWithAuth();
    await flush();
    expect(sockets()).toHaveLength(0);
    advance(INITIAL_RECONNECT_MS);
    await flush();
    expect(ticketCalls).toHaveLength(2);
    expect(sockets()).toHaveLength(0);
    advance(INITIAL_RECONNECT_MS); // second delay is 1.5x
    await flush();
    expect(ticketCalls).toHaveLength(2);
    advance(INITIAL_RECONNECT_MS / 2);
    await flush();
    expect(ticketCalls).toHaveLength(3);
    expect(sockets()).toHaveLength(1);
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_tab");
  });

  it("a hung ticket fetch is abandoned after the handshake timeout and retried", async () => {
    ticketQueue.push(() => new Promise(() => {}));
    renderWithAuth();
    await flush();
    expect(ticketCalls[0].opts.signal.aborted).toBe(false);
    advance(HANDSHAKE_TIMEOUT_MS);
    await flush();
    expect(ticketCalls[0].opts.signal.aborted).toBe(true);
    advance(INITIAL_RECONNECT_MS);
    await flush();
    expect(ticketCalls).toHaveLength(2);
    expect(sockets()).toHaveLength(1);
  });

  it("a 1011 (auth store unavailable) is an ordinary drop", async () => {
    renderWithAuth();
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(1011, "Auth store unavailable"));
    advance(INITIAL_RECONNECT_MS);
    await flush();
    expect(sockets()).toHaveLength(2);
    expect(expired).toEqual([]);
  });

  it("4001 on a socket opened bare (auth was off): re-runs the auth check, then uses tickets", async () => {
    // DISABLE_AUTH flipped off under an open tab: the tab still believes
    // auth is off, so only the server's 4001 can tell it.
    const refresh = vi.fn(async () => null);
    const tree = (auth) => <AuthProvider value={{ ...auth, refresh }}><WebSocketProvider><div /></WebSocketProvider></AuthProvider>;
    const { rerender } = render(tree(AUTH_OFF));
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(1006, ""));
    advance(INITIAL_RECONNECT_MS);
    await flush();
    expect(refresh).not.toHaveBeenCalled(); // an ordinary drop is not a hint
    act(() => sockets()[1]._open());
    act(() => sockets()[1]._drop(4001, "missing"));
    await flush();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(expired).toEqual([]);
    // AuthGuard applied the check: auth is on.
    rerender(tree(AUTH_ON));
    await flush();
    expect(ticketCalls).toHaveLength(1);
    expect(sockets()[sockets().length - 1].url).toContain("ticket=xyt_1");
  });

  it("4001 on a ticket socket does not re-run the auth check", async () => {
    const refresh = vi.fn(async () => null);
    renderWithAuth({ ...AUTH_ON, refresh });
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(4001, "invalid"));
    await flush();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("unmount during a ticket fetch opens no socket", async () => {
    let release;
    ticketQueue.push(() => new Promise((r) => { release = () => r(res(200, { ticket: "xyt_late", expires_in: 30 })); }));
    const { unmount } = renderWithAuth();
    await flush();
    expect(ticketCalls).toHaveLength(1);
    unmount();
    release();
    await flush();
    expect(sockets()).toHaveLength(0);
  });
});
