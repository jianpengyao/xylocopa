import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import useIdleLock from "./useIdleLock";

const MIN = 60 * 1000;

describe("useIdleLock", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    // clearAuthToken() fires a best-effort diagnostics beacon.
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true })));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("locks and redirects to /login after 30 minutes without activity", () => {
    localStorage.setItem("cc-auth-token", "tok");
    const navigate = vi.fn();
    renderHook(() => useIdleLock(navigate));
    act(() => { vi.advanceTimersByTime(29 * MIN); });
    expect(navigate).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(2 * MIN); });
    expect(navigate).toHaveBeenCalledWith("/login", { replace: true });
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
  });

  it("locks immediately on mount when already idle too long", () => {
    localStorage.setItem("cc-auth-token", "tok");
    localStorage.setItem("cc-last-activity", String(Date.now() - 31 * MIN));
    const navigate = vi.fn();
    renderHook(() => useIdleLock(navigate));
    expect(navigate).toHaveBeenCalledWith("/login", { replace: true });
  });

  it("does nothing when disabled (server runs without a password)", () => {
    localStorage.setItem("cc-auth-token", "tok");
    localStorage.setItem("cc-last-activity", String(Date.now() - 31 * MIN));
    const navigate = vi.fn();
    renderHook(() => useIdleLock(navigate, { enabled: false }));
    act(() => { vi.advanceTimersByTime(120 * MIN); });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(navigate).not.toHaveBeenCalled();
    expect(localStorage.getItem("cc-auth-token")).toBe("tok");
  });

  it("stops locking once it is disabled after mount", () => {
    localStorage.setItem("cc-auth-token", "tok");
    const navigate = vi.fn();
    const { rerender } = renderHook(({ enabled }) => useIdleLock(navigate, { enabled }), {
      initialProps: { enabled: true },
    });
    rerender({ enabled: false });
    act(() => { vi.advanceTimersByTime(60 * MIN); });
    expect(navigate).not.toHaveBeenCalled();
  });
});
