import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";

// xterm needs a real canvas/layout; a stub is enough for the socket logic.
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    constructor() { this.cols = 80; this.rows = 24; this.options = {}; }
    loadAddon() {}
    open() {}
    onData() {}
    write() {}
    reset() {}
    focus() {}
    dispose() {}
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

import TerminalOverlay from "./TerminalOverlay";
import { AuthProvider } from "../contexts/AuthContext";

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
    this.onclose?.({ code: 1005, reason: "" });
  }
  _open() { this.readyState = MockWebSocket.OPEN; this.onopen?.(); }
  _drop(code, reason) { this.readyState = MockWebSocket.CLOSED; this.onclose?.({ code, reason }); }
}
const sockets = () => MockWebSocket.instances;

const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});
const AUTH_ON = { authReady: true, authDisabled: false, session: null, idleTimeoutMinutes: 30, refresh: async () => null };

let ticketCalls;
let ticketQueue;
let ticketNo;
function stubTicketFetch() {
  ticketCalls = [];
  ticketQueue = [];
  ticketNo = 0;
  vi.stubGlobal("fetch", vi.fn((url, opts = {}) => {
    if (url === "/api/debug/auth-diag") return Promise.resolve(res(200, { ok: true }));
    if (url !== "/api/auth/ws-ticket") return Promise.resolve(res(404, {}));
    ticketCalls.push({ url, opts });
    const handler = ticketQueue.shift() || (() => res(200, { ticket: `xyt_${++ticketNo}`, expires_in: 30 }));
    return Promise.resolve().then(handler);
  }));
}
const flush = () => act(async () => { await vi.advanceTimersByTimeAsync(0); });
const advance = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const renderOverlay = (auth = AUTH_ON) =>
  render(
    <AuthProvider value={auth}>
      <TerminalOverlay agentId="agent0001" agentName="a1" onClose={() => {}} />
    </AuthProvider>,
  );

describe("TerminalOverlay auth (tickets, 4001)", () => {
  let expired;
  let hidden;
  const onExpired = (e) => expired.push(e.detail);
  beforeEach(() => {
    vi.useFakeTimers();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    // lib/themes reads the colour scheme
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    hidden = false;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
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
    delete document.hidden;
    localStorage.clear();
  });

  it("connects with a one-time ticket, never the session token", async () => {
    renderOverlay();
    expect(sockets()).toHaveLength(0);
    await flush();
    expect(ticketCalls).toHaveLength(1);
    expect(ticketCalls[0].opts.headers.Authorization).toBe("Bearer xys_tab");
    expect(sockets()).toHaveLength(1);
    const url = new URL(sockets()[0].url);
    expect(url.pathname).toBe("/ws/terminal/agent0001");
    expect(url.searchParams.get("ticket")).toBe("xyt_1");
    expect(sockets()[0].url).not.toContain("token=");
  });

  it("4001 idle: shows the locked message, expires the session, never retries", async () => {
    renderOverlay();
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(4001, "idle"));
    await flush();
    expect(screen.getByText("Session locked — sign in again")).toBeInTheDocument();
    expect(expired).toEqual([{ reason: "idle" }]);
    expect(localStorage.getItem("cc-auth-locked")).toBe("idle");
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_tab");
    await advance(10000);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await flush();
    expect(sockets()).toHaveLength(1);
    expect(ticketCalls).toHaveLength(1);
  });

  it("4001 revoked: clears the session, no retries", async () => {
    renderOverlay();
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(4001, "revoked"));
    await flush();
    expect(screen.getByText("Session locked — sign in again")).toBeInTheDocument();
    expect(expired).toEqual([{ reason: "revoked" }]);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    await advance(10000);
    expect(sockets()).toHaveLength(1);
  });

  it("4001 invalid: retries with a fresh ticket, no logout", async () => {
    renderOverlay();
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(4001, "invalid"));
    await advance(800);
    expect(ticketCalls).toHaveLength(2);
    expect(sockets()).toHaveLength(2);
    expect(sockets()[1].url).toContain("ticket=xyt_2");
    expect(expired).toEqual([]);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_tab");
  });

  it.each([
    [4001, "invalid"],
    [4001, "missing"],
    [1011, "Auth store unavailable"],
  ])("accepted then closed %i %s every time: bounded retries, then Connection lost", async (code, reason) => {
    renderOverlay();
    await flush();
    // The server accepts before its auth check, so onopen fires each time.
    for (let i = 0; i < 10; i++) {
      const ws = sockets()[sockets().length - 1];
      if (ws.readyState === MockWebSocket.CLOSED) break;
      act(() => ws._open());
      act(() => ws._drop(code, reason));
      await advance(5000);
    }
    expect(sockets()).toHaveLength(4); // first attempt + 3 retries
    expect(ticketCalls).toHaveLength(4);
    expect(screen.getByText("Connection lost")).toBeInTheDocument();
    expect(expired).toEqual([]);
    await advance(60000);
    expect(sockets()).toHaveLength(4);
  });

  it("a frame from the server proves the attach and resets the retry budget", async () => {
    renderOverlay();
    await flush();
    for (let i = 0; i < 6; i++) {
      const ws = sockets()[sockets().length - 1];
      act(() => ws._open());
      act(() => ws.onmessage?.({ data: new Uint8Array([104, 105]).buffer }));
      act(() => ws._drop(1006, ""));
      await advance(5000);
    }
    expect(sockets()).toHaveLength(7);
    expect(screen.queryByText("Connection lost")).toBeNull();
  });

  it("a ticket 401 shows the locked message and does not retry", async () => {
    ticketQueue.push(() => res(401, { detail: "Locked after inactivity" }, { "X-Auth-Reason": "idle" }));
    renderOverlay();
    await flush();
    expect(sockets()).toHaveLength(0);
    expect(screen.getByText("Session locked — sign in again")).toBeInTheDocument();
    expect(expired).toEqual([{ reason: "idle" }]);
    await advance(10000);
    expect(ticketCalls).toHaveLength(1);
  });

  it("resume during a ticket fetch does not start a second connect", async () => {
    let release;
    ticketQueue.push(() => new Promise((r) => { release = () => r(res(200, { ticket: "xyt_slow", expires_in: 30 })); }));
    renderOverlay();
    await flush();
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await flush();
    expect(ticketCalls).toHaveLength(1);
    release();
    await flush();
    expect(sockets()).toHaveLength(1);
    expect(sockets()[0].url).toContain("ticket=xyt_slow");
  });

  it("4001 on a bare socket (auth was off) re-runs the auth check; a ticket socket's does not", async () => {
    const refresh = vi.fn(async () => null);
    renderOverlay({ ...AUTH_ON, authDisabled: true, refresh });
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(1006, ""));
    await flush();
    expect(refresh).not.toHaveBeenCalled();
    await advance(800);
    act(() => sockets()[1]._open());
    act(() => sockets()[1]._drop(4001, "missing"));
    await flush();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(expired).toEqual([]);
  });

  it("4001 invalid on a ticket socket does not re-run the auth check", async () => {
    const refresh = vi.fn(async () => null);
    renderOverlay({ ...AUTH_ON, refresh });
    await flush();
    act(() => sockets()[0]._open());
    act(() => sockets()[0]._drop(4001, "invalid"));
    await flush();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("authDisabled: bare URL, no ticket fetch", () => {
    renderOverlay({ ...AUTH_ON, authDisabled: true });
    expect(ticketCalls).toHaveLength(0);
    expect(sockets()).toHaveLength(1);
    expect(sockets()[0].url).toMatch(/\/ws\/terminal\/agent0001$/);
  });
});
