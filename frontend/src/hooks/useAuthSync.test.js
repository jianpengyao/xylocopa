import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter } from "react-router-dom";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

import useAuthSync from "./useAuthSync";

function mountAt(path) {
  const wrapper = ({ children }) => createElement(MemoryRouter, { initialEntries: [path] }, children);
  return renderHook(() => useAuthSync(), { wrapper });
}

// jsdom does not fire storage events for same-document writes; another tab's
// write is simulated by dispatching the event by hand.
const otherTab = (key, newValue, storageArea = localStorage) =>
  window.dispatchEvent(new StorageEvent("storage", { key, newValue, storageArea }));

describe("useAuthSync (cross-tab)", () => {
  beforeEach(() => {
    localStorage.clear();
    mockNavigate.mockReset();
  });

  it("token removed in another tab → /login 'signed-out-elsewhere'", () => {
    mountAt("/agents");
    otherTab("cc-auth-token", null);
    expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true, state: { reason: "signed-out-elsewhere" } });
  });

  it("token set in another tab while on /login → /", () => {
    mountAt("/login");
    otherTab("cc-auth-token", "xys_new");
    expect(mockNavigate).toHaveBeenCalledWith("/", { replace: true });
  });

  it("token replaced while signed in here: nothing to do", () => {
    mountAt("/agents");
    otherTab("cc-auth-token", "xys_new");
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("idle-lock flag set in another tab → /login 'idle'", () => {
    mountAt("/projects/x");
    otherTab("cc-auth-locked", "idle");
    expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true, state: { reason: "idle" } });
  });

  it("lock flag removed (unlocked elsewhere) is not a navigation by itself", () => {
    mountAt("/login");
    otherTab("cc-auth-locked", null);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("leaves public pages alone", () => {
    mountAt("/cert-guide");
    otherTab("cc-auth-token", null);
    otherTab("cc-auth-locked", "idle");
    expect(mockNavigate).not.toHaveBeenCalled();
    mountAt("/login");
    otherTab("cc-auth-token", null);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("localStorage.clear() in another tab counts as a sign-out", () => {
    mountAt("/agents");
    otherTab(null, null);
    expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true, state: { reason: "signed-out-elsewhere" } });
  });

  it("ignores unrelated keys and sessionStorage", () => {
    mountAt("/agents");
    otherTab("draft:chat:x", null);
    otherTab("cc-auth-token", null, sessionStorage);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("stops listening on unmount", () => {
    const { unmount } = mountAt("/agents");
    unmount();
    otherTab("cc-auth-token", null);
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
