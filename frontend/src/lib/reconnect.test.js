import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { nextBackoff, onNetworkResume, RESUME_PROBE_DELAY_MS } from "./reconnect";

describe("nextBackoff", () => {
  it("grows geometrically and caps at max", () => {
    expect(nextBackoff(1000)).toBe(1500);
    expect(nextBackoff(1500)).toBe(2250);
    expect(nextBackoff(25000)).toBe(30000);
    expect(nextBackoff(30000)).toBe(30000);
    expect(nextBackoff(2000, { factor: 2, max: 5000 })).toBe(4000);
    expect(nextBackoff(4000, { factor: 2, max: 5000 })).toBe(5000);
  });
});

describe("onNetworkResume", () => {
  let visibility = "visible";
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  });
  afterEach(() => {
    vi.useRealTimers();
    delete document.visibilityState;
  });

  it("fires once, after the resume delay, when the page becomes visible", () => {
    const handler = vi.fn();
    const off = onNetworkResume(handler);
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    expect(handler).not.toHaveBeenCalled();
    vi.advanceTimersByTime(RESUME_PROBE_DELAY_MS - 1);
    expect(handler).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(handler).toHaveBeenCalledTimes(1);
    off();
  });

  it("ignores the page going hidden", () => {
    const handler = vi.fn();
    const off = onNetworkResume(handler);
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(RESUME_PROBE_DELAY_MS * 2);
    expect(handler).not.toHaveBeenCalled();
    off();
  });

  it("coalesces online + visibilitychange + pageshow into one call", () => {
    const handler = vi.fn();
    const off = onNetworkResume(handler);
    window.dispatchEvent(new Event("online"));
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    const pageshow = new Event("pageshow");
    Object.defineProperty(pageshow, "persisted", { value: true });
    window.dispatchEvent(pageshow);
    vi.advanceTimersByTime(RESUME_PROBE_DELAY_MS);
    expect(handler).toHaveBeenCalledTimes(1);
    // A later trigger fires again.
    window.dispatchEvent(new Event("online"));
    vi.advanceTimersByTime(RESUME_PROBE_DELAY_MS);
    expect(handler).toHaveBeenCalledTimes(2);
    off();
  });

  it("stops firing after cleanup, including a pending timer", () => {
    const handler = vi.fn();
    const off = onNetworkResume(handler);
    window.dispatchEvent(new Event("online"));
    off();
    vi.advanceTimersByTime(RESUME_PROBE_DELAY_MS * 2);
    window.dispatchEvent(new Event("online"));
    vi.advanceTimersByTime(RESUME_PROBE_DELAY_MS * 2);
    expect(handler).not.toHaveBeenCalled();
  });
});
