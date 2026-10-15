import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
/* global process */
import fs from "node:fs";
import path from "node:path";

// The "Reconnecting…" page lives inline in public/sw-navigation.js (OFFLINE_HTML)
// and is only reachable through the worker's fetch handler. Get it the way a
// browser does: evaluate the worker with a fake `self`, fail a navigation, and
// read the Response body. Then mount its markup into jsdom and run its inline
// <script> with injected fakes (fetch, location, navigator, caches, window).
// vitest runs with cwd = frontend/ (see swNavigation.test.js).
const WORKER = fs.readFileSync(path.resolve(process.cwd(), "public/sw-navigation.js"), "utf8");

const FLAG = "xy-offline-probe-ok";
const BOUNCES = "xy-offline-bounces";
const DIAG = "xy-offline-diag";
const CHECKING = "Checking the server…";
const STILL_LOADING = "Still loading… trying again.";
const CHECKING_RESET = "Checking the server before resetting…";

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
  const res = await responded;
  expect(res.status).toBe(503);
  return res.text();
}

// fetch double: every call returns a promise the test settles by hand
// (call.ok(status) / call.fail(err)); it rejects with AbortError when its
// signal aborts, like the real thing. mode "fail" rejects every call at once.
function makeFetch(mode) {
  const calls = [];
  const fn = vi.fn((url, opts) => {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    const call = {
      url,
      opts,
      aborted: false,
      ok: (status = 200) => resolve(new Response(status === 204 ? null : "x", { status })),
      fail: (err = new TypeError("Load failed")) => reject(err),
    };
    const signal = opts && opts.signal;
    if (signal) {
      signal.addEventListener("abort", () => {
        call.aborted = true;
        reject(new DOMException("The operation was aborted.", "AbortError"));
      });
    }
    calls.push(call);
    if (mode === "fail") call.fail();
    return promise;
  });
  fn.calls = calls;
  return fn;
}

function mount({ onLine = true, fetchMode, serviceWorker = true, registrations, cacheNames = ["workbox-precache-v2-https://x/", "xy-runtime"] } = {}) {
  document.body.innerHTML = MARKUP;
  const el = (id) => document.getElementById(id);
  const retryListen = vi.spyOn(el("retry"), "addEventListener");
  const resetListen = vi.spyOn(el("reset"), "addEventListener");

  const fetch = makeFetch(fetchMode);
  const regs = registrations || [0, 1].map(() => ({ unregister: vi.fn(() => Promise.resolve(true)) }));
  const navigator = { onLine };
  if (serviceWorker) navigator.serviceWorker = { getRegistrations: vi.fn(() => Promise.resolve(regs)) };
  const caches = {
    keys: vi.fn(() => Promise.resolve(cacheNames.slice())),
    delete: vi.fn(() => Promise.resolve(true)),
  };
  const win = new EventTarget();
  win.stop = vi.fn();
  win.AbortController = AbortController;
  win.caches = caches;
  const docEvents = new EventTarget();
  const doc = {
    visibilityState: "visible",
    getElementById: (id) => document.getElementById(id),
    addEventListener: (type, fn) => docEvents.addEventListener(type, fn),
  };
  const location = { reload: vi.fn() };

  new Function(
    "window", "document", "navigator", "location", "fetch", "caches",
    "sessionStorage", "localStorage", "AbortController", SCRIPT,
  )(win, doc, navigator, location, fetch, caches, window.sessionStorage, window.localStorage, AbortController);

  return {
    fetch,
    calls: fetch.calls,
    regs,
    navigator,
    caches,
    location,
    stop: win.stop,
    retryListen,
    resetListen,
    hint: el("hint"),
    msg: () => el("msg").textContent,
    click: (id) => el(id).click(),
    online: () => win.dispatchEvent(new Event("online")),
    visibility: (state) => { doc.visibilityState = state; docEvents.dispatchEvent(new Event("visibilitychange")); },
    diag: () => JSON.parse(window.localStorage.getItem(DIAG) || "null"),
  };
}

const advance = (ms) => vi.advanceTimersByTimeAsync(ms);
const flush = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  await vi.advanceTimersByTimeAsync(0);
};

beforeAll(async () => {
  const html = await readOfflineHtml();
  MARKUP = html.match(/<body>([\s\S]*?)<script>/)[1];
  SCRIPT = html.match(/<script>([\s\S]*?)<\/script>/)[1];
});

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
  vi.useFakeTimers({ now: new Date("2026-10-15T01:03:00Z") });
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("offline page markup", () => {
  it("has the retry/reset buttons, a hidden hint and the probe script", () => {
    const page = mount();
    expect(document.querySelector("h1").textContent).toBe("Reconnecting…");
    expect(page.msg()).toMatch(/retries on its own/);
    expect(page.hint.hidden).toBe(true);
    expect(page.hint.textContent).toMatch(/Tailscale/);
    expect(SCRIPT).toContain("/api/health?src=offline");
  });
});

describe("offline page probing", () => {
  it("sends the first background probe after 1s to /api/health with no-store and an abort signal", async () => {
    const page = mount();
    await advance(999);
    expect(page.calls).toHaveLength(0);
    await advance(1);
    expect(page.calls).toHaveLength(1);
    const { url, opts } = page.calls[0];
    expect(url).toBe("/api/health?src=offline");
    expect(opts.cache).toBe("no-store");
    expect(opts.credentials).toBe("same-origin");
    expect(opts.signal).toBeInstanceOf(AbortSignal);
    // A background probe does not announce itself.
    expect(page.msg()).toMatch(/retries on its own/);
  });

  it("Retry while a probe is in flight aborts it and starts a new fetch immediately", async () => {
    const page = mount();
    await advance(1000);
    expect(page.calls).toHaveLength(1);

    page.click("retry");
    // Synchronous: no timer needs to fire for the tap to take effect.
    expect(page.calls[0].aborted).toBe(true);
    expect(page.calls).toHaveLength(2);
    expect(page.msg()).toBe(CHECKING);

    // The superseded probe's AbortError is ignored: no failure text, no diag.
    await flush();
    expect(page.msg()).toBe(CHECKING);
    expect(page.diag()).toBeNull();

    // Repeated taps keep superseding.
    page.click("retry");
    page.click("retry");
    expect(page.calls).toHaveLength(4);
    expect(page.calls.slice(0, 3).every((c) => c.aborted)).toBe(true);
    expect(page.calls[3].aborted).toBe(false);

    page.calls[3].ok(200);
    await flush();
    expect(page.location.reload).toHaveBeenCalledTimes(1);
  });

  it("the superseded probe's 8s timer does not cut the new probe short", async () => {
    const page = mount();
    await advance(1000);           // probe #1 at t=1s, its kill timer at t=9s
    await advance(4000);
    page.click("retry");           // probe #2 at t=5s, kill at t=13s
    expect(page.calls).toHaveLength(2);
    await advance(4000);           // t=9s
    expect(page.calls[1].aborted).toBe(false);
    expect(page.msg()).toBe(CHECKING);
    await advance(3999);           // t=12.999s
    expect(page.calls[1].aborted).toBe(false);
    await advance(1);              // t=13s
    expect(page.calls[1].aborted).toBe(true);
    await flush();
    expect(page.msg()).toMatch(/no response in 8s/);
  });

  it("reschedules failed background probes with 1s -> x1.5 -> 5s backoff", async () => {
    const page = mount({ fetchMode: "fail" });
    const gaps = [1000, 1000, 1500, 2250, 3375, 5000, 5000];
    for (const gap of gaps) {
      const before = page.calls.length;
      await advance(gap - 1);
      expect(page.calls).toHaveLength(before);
      await advance(1);
      expect(page.calls).toHaveLength(before + 1);
    }
    expect(page.msg()).toBe(
      "Can’t reach the server: connection failed (Load failed). Offline for 19s, retrying on its own.",
    );
  });

  it("a probe with no response for 8s is aborted and reported as \"no response in 8s\"", async () => {
    const page = mount();
    await advance(1000);
    await advance(7999);
    expect(page.calls[0].aborted).toBe(false);
    await advance(1);
    expect(page.calls[0].aborted).toBe(true);
    await flush();
    expect(page.msg()).toBe(
      "Can’t reach the server: no response in 8s. Offline for 9s, retrying on its own.",
    );
    // Backoff continues from the first step.
    await advance(999);
    expect(page.calls).toHaveLength(1);
    await advance(1);
    expect(page.calls).toHaveLength(2);
  });

  it.each([200, 404, 502, 503])("any HTTP status (%i) counts as reachable and reloads", async (status) => {
    const page = mount();
    await advance(1000);
    page.calls[0].ok(status);
    await flush();
    expect(page.location.reload).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(FLAG)).toBe("1");
    expect(page.msg()).toBe("Server reachable — loading the app…");
    // No background probing while the reload is pending.
    await advance(19000);
    expect(page.calls).toHaveLength(1);
  });

  it("returning to the foreground or going online re-probes after 500ms, superseding a hung probe", async () => {
    const page = mount();
    await advance(1000);
    page.visibility("hidden");
    await advance(500);
    expect(page.calls).toHaveLength(1);

    page.visibility("visible");
    await advance(499);
    expect(page.calls).toHaveLength(1);
    await advance(1);
    expect(page.calls).toHaveLength(2);
    expect(page.calls[0].aborted).toBe(true);
    expect(page.msg()).toBe(CHECKING);

    page.online();
    await advance(500);
    expect(page.calls).toHaveLength(3);
    expect(page.calls[1].aborted).toBe(true);
  });
});

describe("offline page hint", () => {
  // With every probe failing at once, failures land at
  // t = 1, 2, 3.5, 5.75, 9.125, 14.125, 19.125 s.
  it("stays hidden before 15s and shows the Tailscale/VPN hint after", async () => {
    const page = mount({ fetchMode: "fail" });
    await advance(14125);
    expect(page.calls).toHaveLength(6);
    expect(page.hint.hidden).toBe(true);
    await advance(5000);
    expect(page.calls).toHaveLength(7);
    expect(page.hint.hidden).toBe(false);
    expect(page.hint.textContent).toMatch(/Tailscale or another VPN/);
  });

  it("says the device is offline when navigator.onLine === false", async () => {
    const page = mount({ fetchMode: "fail", onLine: false });
    await advance(1000);
    expect(page.hint.hidden).toBe(true);
    expect(page.hint.textContent).toMatch(/This device is offline/);
    await advance(18125);
    expect(page.hint.hidden).toBe(false);
    expect(page.hint.textContent).toMatch(/This device is offline\. Turn on Wi-Fi or cellular data/);
    expect(page.hint.textContent).not.toMatch(/Tailscale/);
  });

  // A phone that was briefly offline, then regains Wi-Fi/cellular while its
  // Tailscale tunnel is still dead (the case this hint was written for), must
  // get the VPN text back instead of "Turn on Wi-Fi or cellular data".
  it("restores the Tailscale/VPN hint once the device is back online but still cannot reach the server", async () => {
    const page = mount({ fetchMode: "fail", onLine: false });
    await advance(1000);
    expect(page.hint.textContent).toMatch(/This device is offline/);
    page.navigator.onLine = true;
    await advance(18125);
    expect(page.hint.hidden).toBe(false);
    expect(page.hint.textContent).toMatch(/Tailscale or another VPN/);
  });
});

describe("offline page reset", () => {
  it("Reset while the server is unreachable does not unregister or reload, and explains why", async () => {
    const page = mount();
    await advance(1000);
    page.click("reset");
    expect(page.calls).toHaveLength(2);
    expect(page.calls[0].aborted).toBe(true);
    expect(page.msg()).toBe(CHECKING_RESET);

    page.calls[1].fail(new TypeError("Load failed"));
    await flush();
    expect(page.msg()).toBe(
      "The server is unreachable (connection failed (Load failed)). Resetting would not help — it only works once the server answers. Still retrying.",
    );
    expect(page.navigator.serviceWorker.getRegistrations).not.toHaveBeenCalled();
    expect(page.regs[0].unregister).not.toHaveBeenCalled();
    expect(page.caches.keys).not.toHaveBeenCalled();
    expect(page.caches.delete).not.toHaveBeenCalled();
    expect(page.location.reload).not.toHaveBeenCalled();

    // Still retrying in the background.
    await advance(1000);
    expect(page.calls).toHaveLength(3);
  });

  it("Reset whose check times out also leaves the worker alone", async () => {
    const page = mount();
    page.click("reset");
    await advance(8000);
    await flush();
    expect(page.msg()).toMatch(/^The server is unreachable \(no response in 8s\)\. Resetting would not help/);
    expect(page.regs[0].unregister).not.toHaveBeenCalled();
    expect(page.location.reload).not.toHaveBeenCalled();
  });

  it("a Reset check still in flight keeps its intent when the page returns to the foreground", async () => {
    const page = mount();
    await advance(1000);
    page.click("reset");           // calls[1]: the reset check, hanging
    page.visibility("visible");    // user toggled the VPN and came back
    await advance(500);
    expect(page.calls).toHaveLength(3);
    expect(page.calls[1].aborted).toBe(true);
    expect(page.msg()).toBe(CHECKING_RESET);
    page.calls[2].ok(200);
    await flush();
    for (const r of page.regs) expect(r.unregister).toHaveBeenCalledTimes(1);
    expect(page.location.reload).toHaveBeenCalledTimes(1);
  });

  it("Reset while reachable unregisters every worker, deletes every cache, then reloads", async () => {
    window.sessionStorage.setItem(BOUNCES, "1");
    const page = mount();
    await advance(1000);
    page.click("reset");
    page.calls[1].ok(502);
    await flush();

    expect(page.msg()).toBe("Resetting app data…");
    expect(page.navigator.serviceWorker.getRegistrations).toHaveBeenCalledTimes(1);
    for (const r of page.regs) expect(r.unregister).toHaveBeenCalledTimes(1);
    expect(page.caches.delete.mock.calls.map((c) => c[0])).toEqual(["workbox-precache-v2-https://x/", "xy-runtime"]);
    expect(page.location.reload).toHaveBeenCalledTimes(1);
    const reloadOrder = page.location.reload.mock.invocationCallOrder[0];
    for (const r of page.regs) expect(r.unregister.mock.invocationCallOrder[0]).toBeLessThan(reloadOrder);
    for (const o of page.caches.delete.mock.invocationCallOrder) expect(o).toBeLessThan(reloadOrder);
    // A reset is not a "probe ok" reload: it must not arm bounce detection.
    expect(window.sessionStorage.getItem(FLAG)).toBeNull();
    expect(window.sessionStorage.getItem(BOUNCES)).toBeNull();

    // Taps are ignored while the reset reload is pending.
    page.click("retry");
    page.click("reset");
    expect(page.calls).toHaveLength(2);
  });

  it("still reloads when unregistering fails or there is no serviceWorker API", async () => {
    const broken = mount({ registrations: [{ unregister: vi.fn(() => Promise.reject(new Error("nope"))) }] });
    broken.click("reset");
    broken.calls[0].ok(200);
    await flush();
    expect(broken.caches.delete).toHaveBeenCalledTimes(2);
    expect(broken.location.reload).toHaveBeenCalledTimes(1);

    const noSw = mount({ serviceWorker: false });
    noSw.click("reset");
    noSw.calls[0].ok(200);
    await flush();
    expect(noSw.caches.delete).toHaveBeenCalledTimes(2);
    expect(noSw.location.reload).toHaveBeenCalledTimes(1);
  });
});

describe("offline page bounce detection", () => {
  it("second bounce auto-wipes, and Retry/Reset are still wired up", async () => {
    window.sessionStorage.setItem(FLAG, "1");
    window.sessionStorage.setItem(BOUNCES, "1");
    const page = mount();

    // Listeners are attached even though wipe() ran during startup.
    expect(page.retryListen).toHaveBeenCalledWith("click", expect.any(Function));
    expect(page.resetListen).toHaveBeenCalledWith("click", expect.any(Function));

    await flush();
    expect(page.msg()).toBe("Resetting app data…");
    for (const r of page.regs) expect(r.unregister).toHaveBeenCalledTimes(1);
    expect(page.caches.delete).toHaveBeenCalledTimes(2);
    expect(page.location.reload).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(FLAG)).toBeNull();
    expect(window.sessionStorage.getItem(BOUNCES)).toBeNull();

    // No probing while the wipe's reload is pending (the startup schedule no-ops).
    await advance(19999);
    expect(page.calls).toHaveLength(0);

    // Not replaced after 20s: the watchdog resumes probing...
    await advance(1);
    expect(page.calls).toHaveLength(1);
    expect(page.msg()).toBe(STILL_LOADING);

    // ...and both buttons work from there.
    page.click("retry");
    expect(page.calls[0].aborted).toBe(true);
    expect(page.calls).toHaveLength(2);
    page.click("reset");
    expect(page.calls[1].aborted).toBe(true);
    expect(page.calls).toHaveLength(3);
    expect(page.msg()).toBe(CHECKING_RESET);
  });

  it("first bounce only counts", async () => {
    window.sessionStorage.setItem(FLAG, "1");
    const page = mount();
    expect(window.sessionStorage.getItem(FLAG)).toBeNull();
    expect(window.sessionStorage.getItem(BOUNCES)).toBe("1");
    await flush();
    expect(page.regs[0].unregister).not.toHaveBeenCalled();
    expect(page.location.reload).not.toHaveBeenCalled();
    await advance(1000);
    expect(page.calls).toHaveLength(1);
  });

  it("does not wipe while the device reports offline", async () => {
    window.sessionStorage.setItem(FLAG, "1");
    window.sessionStorage.setItem(BOUNCES, "1");
    const page = mount({ onLine: false });
    await flush();
    expect(window.sessionStorage.getItem(BOUNCES)).toBe("2");
    expect(page.regs[0].unregister).not.toHaveBeenCalled();
    expect(page.location.reload).not.toHaveBeenCalled();
  });

  it("an arrival without the probe-ok flag resets the bounce count", () => {
    window.sessionStorage.setItem(BOUNCES, "1");
    mount();
    expect(window.sessionStorage.getItem(BOUNCES)).toBeNull();
  });
});

describe("offline page diagnostics", () => {
  it("accumulates probes/timeouts/errors in localStorage xy-offline-diag across launches", async () => {
    const t0 = Date.now();
    const page = mount();
    await advance(1000);
    page.calls[0].fail(new TypeError("Load failed"));
    await flush();
    expect(page.diag()).toMatchObject({ first: t0 + 1000, probes: 1, timeouts: 0, errors: 1, why: "connection failed (Load failed)" });

    await advance(1000);           // probe #2 at t=2s hangs
    await advance(8000);           // aborted at t=10s
    await flush();
    expect(page.diag()).toMatchObject({ probes: 2, timeouts: 1, errors: 1, why: "no response in 8s", last: t0 + 10000 });

    await advance(1500);           // probe #3 at t=11.5s
    page.calls[2].fail(new TypeError(""));
    await flush();
    expect(page.diag()).toMatchObject({ first: t0 + 1000, probes: 3, timeouts: 1, errors: 2, why: "connection failed" });

    // Superseded probes are not failures.
    await advance(2250);
    page.click("retry");
    await flush();
    expect(page.diag().probes).toBe(3);

    // A relaunch onto the offline page keeps adding to the same record,
    // including failed Reset checks.
    const again = mount();
    again.click("reset");
    again.calls[0].fail(new TypeError("Load failed"));
    await flush();
    expect(again.diag()).toMatchObject({ first: t0 + 1000, probes: 4, timeouts: 1, errors: 3 });
  });

  it("starts a fresh record when the stored value is not valid JSON", async () => {
    window.localStorage.setItem(DIAG, "{not json");
    const page = mount({ fetchMode: "fail" });
    await advance(1000);
    expect(page.diag()).toMatchObject({ probes: 1, errors: 1, timeouts: 0 });
  });
});

describe("offline page reload watchdog", () => {
  it("resumes probing 20s after a reload that did not replace the page", async () => {
    const page = mount();
    await advance(1000);
    page.calls[0].ok(200);
    await flush();
    expect(page.location.reload).toHaveBeenCalledTimes(1);

    await advance(19999);
    expect(page.calls).toHaveLength(1);
    await advance(1);
    expect(page.calls).toHaveLength(2);
    expect(page.msg()).toBe(STILL_LOADING);

    page.calls[1].ok(200);
    await flush();
    expect(page.location.reload).toHaveBeenCalledTimes(2);
    expect(window.sessionStorage.getItem(FLAG)).toBe("1");
  });

  it("cancels the pending reload before probing again (WebKit fails fetches while it is pending)", async () => {
    const page = mount();
    await advance(1000);
    page.calls[0].ok(200);
    await flush();
    expect(page.stop).not.toHaveBeenCalled();
    await advance(20000);
    expect(page.stop).toHaveBeenCalledTimes(1);
    expect(page.stop.mock.invocationCallOrder[0]).toBeLessThan(page.fetch.mock.invocationCallOrder[1]);
    page.calls[1].ok(200);
    await flush();
    page.click("retry");
    expect(page.stop).toHaveBeenCalledTimes(2);
    expect(page.calls).toHaveLength(3);
  });

  it("Retry during a pending reload probes again right away", async () => {
    const page = mount();
    await advance(1000);
    page.calls[0].ok(200);
    await flush();
    page.click("retry");
    expect(page.calls).toHaveLength(2);
    expect(page.msg()).toBe(CHECKING);
  });

  // Reload at t, Retry + successful probe + reload again at t+10s: the first
  // watchdog is superseded (generation token) and must not re-probe or
  // re-reload only 10s into the second reload.
  it("a stale watchdog does not cut short a later pending reload", async () => {
    const page = mount();
    await advance(1000);
    page.calls[0].ok(200);         // reload #1 at t=1s, watchdog due t=21s
    await flush();
    await advance(10000);
    page.click("retry");           // t=11s
    page.calls[1].ok(200);         // reload #2 at t=11s, its watchdog due t=31s
    await flush();
    expect(page.location.reload).toHaveBeenCalledTimes(2);

    await advance(10000);          // t=21s: only the stale watchdog is due
    expect(page.calls).toHaveLength(2);
    await advance(10000);          // t=31s
    expect(page.calls).toHaveLength(3);
  });
});
