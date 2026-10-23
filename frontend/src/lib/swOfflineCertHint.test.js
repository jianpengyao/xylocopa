import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
/* global process */
import fs from "node:fs";
import path from "node:path";

// The offline page's certificate-trust hint (public/sw-navigation.js).
// Probes refused within FAST_MS twice in a row — a device that does not
// trust the server certificate rejects every TLS handshake at once — switch
// the hint from the plain Tailscale/VPN text to one that also names
// certificate trust. Hanging probes and slow errors keep the VPN text.
// Same harness as swOfflinePage.test.js: read the page out of the worker,
// mount its markup into jsdom and run its inline script with fakes.
const WORKER = fs.readFileSync(path.resolve(process.cwd(), "public/sw-navigation.js"), "utf8");
const DIAG = "xy-offline-diag";
const IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0.1 Mobile/15E148 Safari/604.1";
const DESKTOP_MODE_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0.1 Safari/605.1.15";
const IOS_PATH = "Settings → General → About → Certificate Trust Settings → turn on full trust for “mkcert”";

let MARKUP = "";
let SCRIPT = "";

async function readOfflineHtml() {
  const listeners = {};
  const self = { addEventListener: (type, fn) => { listeners[type] = fn; } };
  new Function("self", "fetch", "Response", WORKER)(
    self,
    () => Promise.reject(new TypeError("Load failed")),
    globalThis.Response,
  );
  let responded = null;
  listeners.fetch({
    request: { url: "https://127.0.0.1:3000/agents", mode: "navigate", method: "GET", destination: "document" },
    respondWith: (p) => { responded = Promise.resolve(p); },
  });
  return (await responded).text();
}

// Every probe stays pending until the test settles it (fail / ok) or its
// signal aborts (the page's 8s timeout).
function makeFetch() {
  const calls = [];
  const fn = vi.fn((url, opts) => {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    const call = {
      ok: (status = 200) => resolve(new Response("x", { status })),
      fail: (err = new TypeError("Load failed")) => reject(err),
    };
    if (opts && opts.signal) {
      opts.signal.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
    }
    calls.push(call);
    return promise;
  });
  fn.calls = calls;
  return fn;
}

function mount({ userAgent, maxTouchPoints, onLine = true } = {}) {
  document.body.innerHTML = MARKUP;
  const fetch = makeFetch();
  const navigator = { onLine };
  if (userAgent !== undefined) navigator.userAgent = userAgent;
  if (maxTouchPoints !== undefined) navigator.maxTouchPoints = maxTouchPoints;
  const win = new EventTarget();
  win.stop = vi.fn();
  win.AbortController = AbortController;
  const doc = {
    visibilityState: "visible",
    getElementById: (id) => document.getElementById(id),
    addEventListener: () => {},
  };
  const location = { reload: vi.fn() };
  const caches = { keys: vi.fn(() => Promise.resolve([])), delete: vi.fn(() => Promise.resolve(true)) };
  new Function(
    "window", "document", "navigator", "location", "fetch", "caches",
    "sessionStorage", "localStorage", "AbortController", SCRIPT,
  )(win, doc, navigator, location, fetch, caches, window.sessionStorage, window.localStorage, AbortController);
  return {
    calls: fetch.calls,
    navigator,
    location,
    hint: document.getElementById("hint"),
    diag: () => JSON.parse(window.localStorage.getItem(DIAG) || "null"),
  };
}

const advance = (ms) => vi.advanceTimersByTimeAsync(ms);
const flush = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  await vi.advanceTimersByTimeAsync(0);
};

// Run the schedule until the next background probe goes out (backoff is at
// most 5s), then fail it `after` ms later (+ up to one 100ms step).
async function nextProbe(page) {
  const n = page.calls.length;
  for (let i = 0; i < 60 && page.calls.length === n; i++) await advance(100);
  expect(page.calls.length).toBe(n + 1);
  return page.calls[n];
}

async function failNext(page, { after = 30, err } = {}) {
  const call = await nextProbe(page);
  await advance(after);
  call.fail(err);
  await flush();
}

beforeAll(async () => {
  const html = await readOfflineHtml();
  MARKUP = html.match(/<body>([\s\S]*?)<script>/)[1];
  SCRIPT = html.match(/<script>([\s\S]*?)<\/script>/)[1];
});

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
  vi.useFakeTimers({ now: new Date("2026-10-23T18:56:00Z") });
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("offline page certificate hint", () => {
  it("names certificate trust after two probes refused within 2s, keeping the VPN advice", async () => {
    const page = mount({ userAgent: IPHONE_UA, maxTouchPoints: 5 });
    await failNext(page);
    expect(page.hint.textContent).toMatch(/Tailscale or another VPN/);
    expect(page.hint.textContent).not.toMatch(/certificate/);   // one fast failure is not enough
    await failNext(page);
    expect(page.hint.textContent).toMatch(/^The connection is refused right away\./);
    expect(page.hint.textContent).toMatch(/If Tailscale or another VPN is off, turn it on\./);
    expect(page.hint.textContent).toContain("does not trust the server’s certificate");
    expect(page.hint.textContent).toContain(IOS_PATH);
    expect(page.hint.textContent).toMatch(/restart the device if it still fails/);
  });

  it("is still hidden before 15s, like the VPN hint", async () => {
    const page = mount({ userAgent: IPHONE_UA, maxTouchPoints: 5 });
    await failNext(page);
    await failNext(page);
    expect(page.hint.textContent).toContain("certificate");
    expect(page.hint.hidden).toBe(true);
    while (Date.now() - new Date("2026-10-23T18:56:00Z").getTime() < 15000) await failNext(page);
    expect(page.hint.hidden).toBe(false);
    expect(page.hint.textContent).toContain(IOS_PATH);
  });

  it("gives the iOS Settings path to an iPhone in desktop mode (Macintosh UA with touch)", async () => {
    const page = mount({ userAgent: DESKTOP_MODE_UA, maxTouchPoints: 5 });
    await failNext(page);
    await failNext(page);
    expect(page.hint.textContent).toContain(IOS_PATH);
  });

  it("gives a real Mac (no touch) and unknown agents the generic instruction", async () => {
    for (const opts of [{ userAgent: DESKTOP_MODE_UA, maxTouchPoints: 0 }, {}]) {
      const page = mount(opts);
      await failNext(page);
      await failNext(page);
      expect(page.hint.textContent).toContain("install the server’s CA certificate as a trusted root, then reopen this app.");
      expect(page.hint.textContent).not.toContain("Certificate Trust Settings");
      document.body.innerHTML = "";
    }
  });

  it("keeps the plain VPN hint when errors take 2s or more", async () => {
    const page = mount({ userAgent: IPHONE_UA, maxTouchPoints: 5 });
    await failNext(page, { after: 2000 });
    await failNext(page, { after: 2500 });
    await failNext(page, { after: 3000 });
    expect(page.hint.textContent).toMatch(/^This device can’t reach the server\./);
    expect(page.hint.textContent).not.toContain("certificate");
  });

  it("a probe that hangs to its 8s abort resets the streak back to the VPN hint", async () => {
    const page = mount({ userAgent: IPHONE_UA, maxTouchPoints: 5 });
    await failNext(page);
    await failNext(page);
    expect(page.hint.textContent).toContain("certificate");
    // next probe hangs until the page aborts it
    await nextProbe(page);
    await advance(8000);
    await flush();
    expect(page.hint.textContent).toMatch(/^This device can’t reach the server\./);
    // one fast failure after the timeout is not enough to switch back
    await failNext(page);
    expect(page.hint.textContent).not.toContain("certificate");
    await failNext(page);
    expect(page.hint.textContent).toContain("certificate");
  });

  it("the offline hint still wins when the device reports no network", async () => {
    const page = mount({ userAgent: IPHONE_UA, maxTouchPoints: 5, onLine: false });
    await failNext(page);
    await failNext(page);
    expect(page.hint.textContent).toMatch(/^This device is offline\./);
  });

  // Coming back online while the tunnel is still re-establishing: fast
  // failures counted during the offline spell must not make the very first
  // online failure read as a certificate problem.
  it("does not count fast failures while offline toward the streak", async () => {
    const page = mount({ userAgent: IPHONE_UA, maxTouchPoints: 5, onLine: false });
    const nav = page.navigator;
    await failNext(page);
    await failNext(page);
    nav.onLine = true;
    await failNext(page);
    expect(page.hint.textContent).toMatch(/^This device can’t reach the server\./);
    await failNext(page);
    expect(page.hint.textContent).toContain("does not trust the server’s certificate");
  });

  it("counts fast failures in xy-offline-diag", async () => {
    const page = mount({ userAgent: IPHONE_UA, maxTouchPoints: 5 });
    await failNext(page);
    await failNext(page, { after: 2500 });
    await failNext(page);
    expect(page.diag()).toMatchObject({ probes: 3, errors: 3, timeouts: 0, fast: 2, why: "connection failed (Load failed)" });
  });

  it("a server that answers after fast failures still reloads into the app", async () => {
    const page = mount({ userAgent: IPHONE_UA, maxTouchPoints: 5 });
    await failNext(page);
    await failNext(page);
    (await nextProbe(page)).ok(200);
    await flush();
    expect(page.location.reload).toHaveBeenCalledTimes(1);
  });
});
