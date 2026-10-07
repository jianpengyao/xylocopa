import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { WebSocketProvider } from "./WebSocketContext";
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
  // server-side drop: fires onclose without the client calling close()
  _drop() { this.readyState = MockWebSocket.CLOSED; this.onclose?.({}); }
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
