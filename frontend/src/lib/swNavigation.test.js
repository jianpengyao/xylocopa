import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
/* global process */
import fs from "node:fs";
import path from "node:path";

// public/sw-navigation.js is a classic worker script (no exports). Evaluate
// it with a fake `self` and capture the fetch listener it registers.
// vitest runs with cwd = frontend/ (see package.json "test"); under the jsdom
// environment import.meta.url is not a file: URL, so resolve from cwd.
const SCRIPT = fs.readFileSync(path.resolve(process.cwd(), "public/sw-navigation.js"), "utf8");

function loadWorker({ fetchImpl }) {
  const listeners = {};
  const self = { addEventListener: (type, fn) => { listeners[type] = fn; } };
  const run = new Function("self", "fetch", "Response", "setTimeout", "clearTimeout", SCRIPT);
  run(self, fetchImpl, globalThis.Response, setTimeout, clearTimeout);
  return listeners.fetch;
}

function navEvent(overrides = {}) {
  const request = {
    url: "https://100.122.52.79:3000/agents/606011a9b2d3",
    mode: "navigate",
    method: "GET",
    destination: "document",
    ...overrides,
  };
  let responded = null;
  return {
    request,
    respondWith: (p) => { responded = Promise.resolve(p); },
    get responded() { return responded; },
  };
}

describe("sw-navigation fetch handler", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("ignores non-navigation requests and frames", () => {
    const onFetch = loadWorker({ fetchImpl: () => { throw new Error("must not be called"); } });
    const api = navEvent({ mode: "cors", destination: "" });
    onFetch(api);
    expect(api.responded).toBeNull();
    const frame = navEvent({ destination: "iframe" });
    onFetch(frame);
    expect(frame.responded).toBeNull();
    const post = navEvent({ method: "POST" });
    onFetch(post);
    expect(post.responded).toBeNull();
  });

  it("passes the network response through when the fetch succeeds", async () => {
    const real = new Response("<html>app</html>", { status: 200, headers: { "Content-Type": "text/html" } });
    const onFetch = loadWorker({ fetchImpl: (req) => { expect(req.mode).toBe("navigate"); return Promise.resolve(real); } });
    const ev = navEvent();
    onFetch(ev);
    expect(await ev.responded).toBe(real);
  });

  it("answers with the inline reconnecting page instead of failing when fetch rejects", async () => {
    const onFetch = loadWorker({ fetchImpl: () => Promise.reject(new TypeError("Load failed")) });
    const ev = navEvent();
    onFetch(ev);
    const res = await ev.responded;
    expect(res.status).toBe(503);
    expect(res.headers.get("Content-Type")).toMatch(/text\/html/);
    const body = await res.text();
    expect(body).toContain("Reconnecting");
    expect(body).toContain("/api/health");      // native probe, bypasses the worker
    expect(body).toContain("xy-offline-bounces"); // stuck-worker self-reset
  });

  it("answers with the reconnecting page when fetch throws synchronously", async () => {
    const onFetch = loadWorker({ fetchImpl: () => { throw new TypeError("boom"); } });
    const ev = navEvent();
    onFetch(ev);
    const res = await ev.responded;
    expect(res.status).toBe(503);
  });

  it("gives up on a hung fetch after the timeout and serves the reconnecting page", async () => {
    const onFetch = loadWorker({ fetchImpl: () => new Promise(() => {}) });
    const ev = navEvent();
    onFetch(ev);
    let settled = null;
    ev.responded.then((r) => { settled = r; });
    await vi.advanceTimersByTimeAsync(14999);
    expect(settled).toBeNull();
    await vi.advanceTimersByTimeAsync(1);
    expect(settled).not.toBeNull();
    expect(settled.status).toBe(503);
  });

  it("never rejects the respondWith promise", async () => {
    const onFetch = loadWorker({ fetchImpl: () => Promise.resolve(null) });
    const ev = navEvent();
    onFetch(ev);
    await expect(ev.responded).resolves.toBeInstanceOf(Response);
  });
});
