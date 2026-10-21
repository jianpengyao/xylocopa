import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  _resetSwUpdateForTests,
  flushPendingReload,
  isEditable,
  isReloadPending,
  registerServiceWorker,
  requestReload,
  setUpdateScreen,
  shouldDeferReload,
} from "./swUpdate";

let reload;
let hidden;
const setVisibility = (state) => {
  hidden = state;
  document.dispatchEvent(new Event("visibilitychange"));
};
const field = (tag = "input", attrs = {}) => {
  const el = document.createElement(tag);
  Object.assign(el, attrs);
  document.body.appendChild(el);
  return el;
};

beforeEach(() => {
  vi.useFakeTimers();
  reload = vi.fn();
  _resetSwUpdateForTests({ reload });
  hidden = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => hidden });
  window.history.replaceState({}, "", "/projects");
  vi.spyOn(console, "info").mockImplementation(() => {});
});
afterEach(() => {
  _resetSwUpdateForTests();
  document.activeElement?.blur?.();
  document.body.innerHTML = "";
  delete document.visibilityState;
  window.history.replaceState({}, "", "/");
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("shouldDeferReload / isEditable", () => {
  it("text fields, textareas and contentEditable are editable; buttons and checkboxes are not", () => {
    expect(isEditable(field("input"))).toBe(true);
    expect(isEditable(field("input", { type: "password" }))).toBe(true);
    expect(isEditable(field("textarea"))).toBe(true);
    const div = field("div");
    Object.defineProperty(div, "isContentEditable", { value: true });
    expect(isEditable(div)).toBe(true);
    expect(isEditable(field("input", { type: "checkbox" }))).toBe(false);
    expect(isEditable(field("button"))).toBe(false);
    expect(isEditable(null)).toBe(false);
  });

  it("defers on /login and while a text field has focus", () => {
    expect(shouldDeferReload()).toBe(false);
    window.history.replaceState({}, "", "/login");
    expect(shouldDeferReload()).toBe(true);
    window.history.replaceState({}, "", "/agents/a1");
    field("textarea").focus();
    expect(shouldDeferReload()).toBe(true);
  });

  it("never defers while the full-screen update gate is up", () => {
    window.history.replaceState({}, "", "/login");
    setUpdateScreen(true);
    expect(shouldDeferReload()).toBe(false);
    setUpdateScreen(false);
    expect(shouldDeferReload()).toBe(true);
  });
});

describe("requestReload", () => {
  it("reloads at once on another route with nothing focused", () => {
    expect(requestReload("sw-update")).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(isReloadPending()).toBe(false);
  });

  it("on /login: waits, even through the poll, until the user leaves /login", () => {
    window.history.replaceState({}, "", "/login");
    expect(requestReload("sw-update")).toBe(false);
    expect(isReloadPending()).toBe(true);
    vi.advanceTimersByTime(5000);
    expect(reload).not.toHaveBeenCalled();
    // A second request while pending does not stack another waiter.
    expect(requestReload("sw-update")).toBe(false);
    window.history.replaceState({}, "", "/projects");
    vi.advanceTimersByTime(1000);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(isReloadPending()).toBe(false);
    vi.advanceTimersByTime(5000);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("while typing: waits, then reloads on focusout once nothing editable has focus", () => {
    const composer = field("textarea");
    composer.focus();
    requestReload("sw-update");
    vi.advanceTimersByTime(3000);
    expect(reload).not.toHaveBeenCalled();
    // Tab to another text field: still typing.
    const other = field("input");
    other.focus();
    vi.advanceTimersByTime(0);
    expect(reload).not.toHaveBeenCalled();
    other.blur();
    vi.advanceTimersByTime(0);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("flushes when the page is hidden (nothing deferring any more)", () => {
    const input = field("input");
    input.focus();
    requestReload("sw-update");
    input.blur();
    // The poll would also catch it; hidden flushes at once.
    setVisibility("hidden");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("hidden on /login keeps waiting (the password field must survive)", () => {
    window.history.replaceState({}, "", "/login");
    requestReload("sw-update");
    setVisibility("hidden");
    expect(reload).not.toHaveBeenCalled();
    expect(isReloadPending()).toBe(true);
  });

  it("the update gate going up runs a pending reload", () => {
    window.history.replaceState({}, "", "/login");
    requestReload("sw-update");
    setUpdateScreen(true);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("flushPendingReload is a no-op with nothing pending", () => {
    flushPendingReload();
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("registerServiceWorker", () => {
  let sw;
  let registration;
  let listeners;
  const fire = (name) => listeners[name]?.forEach((fn) => fn());
  function stubSw({ controller }) {
    listeners = {};
    registration = { update: vi.fn(() => Promise.resolve()) };
    sw = {
      controller,
      addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
      register: vi.fn(() => Promise.resolve(registration)),
    };
    Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: sw });
  }
  afterEach(() => {
    delete navigator.serviceWorker;
  });

  it("registers the given URL at scope / (document already loaded)", async () => {
    stubSw({ controller: null });
    registerServiceWorker({ swUrl: "/sw.js" });
    await vi.advanceTimersByTimeAsync(0);
    expect(sw.register).toHaveBeenCalledWith("/sw.js", { scope: "/" });
  });

  it("an update taking over a controlled page asks for a reload (deferred on /login)", () => {
    stubSw({ controller: {} });
    registerServiceWorker({ swUrl: "/sw.js" });
    window.history.replaceState({}, "", "/login");
    fire("controllerchange");
    expect(reload).not.toHaveBeenCalled();
    expect(isReloadPending()).toBe(true);
    window.history.replaceState({}, "", "/projects");
    vi.advanceTimersByTime(1000);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("first install (no controller at load) does not reload; a later update does", () => {
    stubSw({ controller: null });
    registerServiceWorker({ swUrl: "/sw.js" });
    fire("controllerchange");
    expect(reload).not.toHaveBeenCalled();
    expect(isReloadPending()).toBe(false);
    fire("controllerchange");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("update checks run on foreground and every 30 min, but not while a reload would wait", async () => {
    stubSw({ controller: {} });
    registerServiceWorker({ swUrl: "/sw.js" });
    await vi.advanceTimersByTimeAsync(0);
    setVisibility("visible");
    expect(registration.update).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(30 * 60 * 1000);
    expect(registration.update).toHaveBeenCalledTimes(2);
    window.history.replaceState({}, "", "/login");
    setVisibility("visible");
    vi.advanceTimersByTime(30 * 60 * 1000);
    expect(registration.update).toHaveBeenCalledTimes(2);
  });

  it("without service-worker support it does nothing", () => {
    expect("serviceWorker" in navigator).toBe(false);
    expect(() => registerServiceWorker({ swUrl: "/sw.js" })).not.toThrow();
  });
});
