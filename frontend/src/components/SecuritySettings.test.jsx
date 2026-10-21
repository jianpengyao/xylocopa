import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});
vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal()),
  fetchAuthSettings: vi.fn(),
  updateAuthSettings: vi.fn(),
  fetchAuthSessions: vi.fn(),
  revokeAuthSession: vi.fn(),
  revokeOtherSessions: vi.fn(),
  authChangePassword: vi.fn(),
  authLogout: vi.fn(),
}));

import SecuritySettings from "./SecuritySettings";
import { AuthProvider } from "../contexts/AuthContext";
import { ToastProvider } from "../contexts/ToastContext";
import * as api from "../lib/api";

const SETTINGS = (over = {}) => ({
  idle_timeout_minutes: 30,
  choices: [0, 5, 15, 30, 60, 120, 240, 480, 1440],
  default_idle_timeout_minutes: 30,
  session_max_days: 90,
  auth_disabled: false,
  password_set: true,
  ...over,
});
const now = () => Date.now() / 1000;
const ROW = (over) => ({
  id: "s1", device_label: "Mac · Chrome", user_agent: "UA", created_at: now() - 3600,
  last_seen_at: now() - 30, last_active_at: now() - 30, last_ip: "100.64.0.10",
  expires_at: now() + 86400, state: "active", current: false, ...over,
});
const SESSIONS = () => ({
  sessions: [
    ROW({ id: "me", device_label: "Linux · Chrome", current: true }),
    ROW({ id: "phone", device_label: "iPhone · Safari", last_ip: "100.64.0.11", last_seen_at: now() - 7200, state: "idle" }),
  ],
  current_id: "me",
  auth_disabled: false,
});

let refresh;
function renderCard({ authDisabled = false } = {}) {
  refresh = vi.fn(() => Promise.resolve(null));
  const value = { authReady: true, authDisabled, session: null, idleTimeoutMinutes: 30, refresh };
  return render(
    <MemoryRouter>
      <ToastProvider>
        <AuthProvider value={value}>
          <SecuritySettings />
        </AuthProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

let cacheDelete;
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("cc-auth-token", "xys_me");
  api.fetchAuthSettings.mockResolvedValue(SETTINGS());
  api.fetchAuthSessions.mockResolvedValue(SESSIONS());
  cacheDelete = vi.fn(() => Promise.resolve(true));
  vi.stubGlobal("caches", { delete: cacheDelete });
  // clearAuthToken() sends a best-effort diagnostics beacon.
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: async () => ({}) })));
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("SecuritySettings", () => {
  it("under DISABLE_AUTH: shows the banner, keeps the settings, hides sign-out", async () => {
    api.fetchAuthSettings.mockResolvedValue(SETTINGS({ auth_disabled: true }));
    renderCard({ authDisabled: true });
    expect(screen.getByText(/Password protection is off on this server \(DISABLE_AUTH=1\)/)).toBeInTheDocument();
    const select = await screen.findByLabelText("Idle timeout");
    await waitFor(() => expect(select).toHaveValue("30"));
    expect(screen.getByText("30 min (default)")).toBeInTheDocument();
    expect(screen.getByText("Off")).toBeInTheDocument();
    expect(screen.getByText("24 h")).toBeInTheDocument();
    expect(screen.getByLabelText("Current password")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });

  it("changing the idle timeout saves it and refreshes the auth context", async () => {
    api.updateAuthSettings.mockResolvedValue(SETTINGS({ idle_timeout_minutes: 0 }));
    renderCard();
    const select = await screen.findByLabelText("Idle timeout");
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: "0" } });
    await waitFor(() => expect(api.updateAuthSettings).toHaveBeenCalledWith({ idle_timeout_minutes: 0 }));
    await waitFor(() => expect(select).toHaveValue("0"));
    expect(refresh).toHaveBeenCalled();
  });

  it("an invalid idle value shows the server's message", async () => {
    api.updateAuthSettings.mockRejectedValue(new Error("idle_timeout_minutes must be one of [0, 5, 15, 30, 60, 120, 240, 480, 1440]"));
    renderCard();
    const select = await screen.findByLabelText("Idle timeout");
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: "60" } });
    expect(await screen.findByText(/must be one of/)).toBeInTheDocument();
  });

  it("lists devices with a This-device marker, state and last IP; no Revoke on this device", async () => {
    renderCard();
    const list = await screen.findByTestId("device-list");
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Linux · Chrome")).toBeInTheDocument();
    expect(within(rows[0]).getByText("This device")).toBeInTheDocument();
    expect(within(rows[0]).queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(within(rows[1]).getByText("iPhone · Safari")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Locked")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/2 h ago · 100\.64\.0\.11/)).toBeInTheDocument();
  });

  it("Revoke signs that device out and refreshes the list", async () => {
    api.revokeAuthSession.mockResolvedValue({ ok: true, current: false });
    renderCard();
    const list = await screen.findByTestId("device-list");
    api.fetchAuthSessions.mockResolvedValue({ ...SESSIONS(), sessions: [SESSIONS().sessions[0]] });
    fireEvent.click(within(list).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(api.revokeAuthSession).toHaveBeenCalledWith("phone"));
    await waitFor(() => expect(within(screen.getByTestId("device-list")).getAllByRole("listitem")).toHaveLength(1));
    expect(api.fetchAuthSessions).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_me");
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("a revoke that turns out to be this device signs out locally", async () => {
    api.revokeAuthSession.mockResolvedValue({ ok: true, current: true });
    renderCard();
    const list = await screen.findByTestId("device-list");
    fireEvent.click(within(list).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true, state: { reason: "signed-out" } }));
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    expect(cacheDelete).toHaveBeenCalledWith("thumbnail-cache");
  });

  it("Sign out other devices calls revoke-others and refreshes the list", async () => {
    api.revokeOtherSessions.mockResolvedValue({ ok: true, revoked: 1 });
    renderCard();
    await screen.findByTestId("device-list");
    fireEvent.click(screen.getByRole("button", { name: "Sign out other devices" }));
    await waitFor(() => expect(api.revokeOtherSessions).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Signed out 1 other device")).toBeInTheDocument();
    await waitFor(() => expect(api.fetchAuthSessions).toHaveBeenCalledTimes(2));
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_me");
  });

  it("change password: client-side checks, no request", async () => {
    renderCard();
    await screen.findByTestId("device-list");
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old-password" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "short" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("New password must be at least 8 characters")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "long-enough-1" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "long-enough-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("New passwords don't match")).toBeInTheDocument();
    expect(api.authChangePassword).not.toHaveBeenCalled();
  });

  it("change password 403: inline message, still signed in", async () => {
    const err = new Error("Current password is wrong");
    err.status = 403;
    api.authChangePassword.mockRejectedValue(err);
    renderCard();
    await screen.findByTestId("device-list");
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "wrong-password" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-password-1" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "new-password-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("Current password is wrong")).toBeInTheDocument();
    expect(api.authChangePassword).toHaveBeenCalledWith("wrong-password", "new-password-1", expect.objectContaining({}));
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_me");
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("change password success: toast, fields cleared, list and auth refreshed", async () => {
    api.authChangePassword.mockResolvedValue({ token: "xys_new", revoked_sessions: 2 });
    renderCard();
    await screen.findByTestId("device-list");
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "old-password" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "new-password-1" } });
    fireEvent.change(screen.getByLabelText("Confirm new password"), { target: { value: "new-password-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("Password changed — other devices signed out")).toBeInTheDocument();
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(screen.getByLabelText("New password")).toHaveValue("");
    expect(refresh).toHaveBeenCalled();
    await waitFor(() => expect(api.fetchAuthSessions).toHaveBeenCalledTimes(2));
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("Sign out: logout, clear the token, drop the thumbnail cache, go to /login", async () => {
    api.authLogout.mockResolvedValue({ ok: true, revoked: true });
    renderCard();
    await screen.findByTestId("device-list");
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true, state: { reason: "signed-out" } }));
    expect(api.authLogout).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    expect(cacheDelete).toHaveBeenCalledWith("thumbnail-cache");
    // The local sign-out happens after the server logout was asked for.
    expect(api.authLogout.mock.invocationCallOrder[0]).toBeLessThan(cacheDelete.mock.invocationCallOrder[0]);
  });

  it("Sign out right after a login still clears the token (no grace window)", async () => {
    api.authLogout.mockResolvedValue({ ok: true, revoked: true });
    localStorage.setItem("cc-login-ts", String(Date.now()));
    renderCard();
    await screen.findByTestId("device-list");
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login", expect.anything()));
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    expect(localStorage.getItem("cc-login-ts")).toBeNull();
  });

  it("Sign out still signs out locally when the logout request fails", async () => {
    api.authLogout.mockRejectedValue(new TypeError("Failed to fetch"));
    renderCard();
    await screen.findByTestId("device-list");
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login", expect.anything()));
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
  });

  it("no password set yet: explains instead of a change form", async () => {
    api.fetchAuthSettings.mockResolvedValue(SETTINGS({ password_set: false }));
    renderCard();
    expect(await screen.findByText(/No password is set yet/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Current password")).toBeNull();
  });
});
