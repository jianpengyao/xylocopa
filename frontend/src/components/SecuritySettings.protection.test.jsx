import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// The "Password protection" card (mirrors DISABLE_AUTH in .env) with the
// API mocked. SecuritySettings.protectionFlow.test.jsx runs the same switch
// through the real AuthGuard, api.js and WebSocketProvider.

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});
vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal()),
  fetchAuthSettings: vi.fn(),
  fetchAuthProtection: vi.fn(),
  updateAuthSettings: vi.fn(),
  fetchAuthSessions: vi.fn(),
  revokeAuthSession: vi.fn(),
  revokeOtherSessions: vi.fn(),
  authChangePassword: vi.fn(),
  authLogout: vi.fn(),
  setAuthProtection: vi.fn(),
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
  env_file_disable_auth: false,
  env_file_in_sync: true,
  ...over,
});
const OFF = (over = {}) => SETTINGS({ auth_disabled: true, env_file_disable_auth: true, ...over });
const SESSIONS = () => ({ sessions: [], current_id: null, auth_disabled: true });
const TURNED_ON = (over = {}) => ({
  token: "xys_fresh", session_id: "s9", expires_at: 1e10, idle_timeout_minutes: 30,
  expires_minutes: 30, media_token: "m.fresh", media_expires_at: 1e10, settings: SETTINGS(),
  protection: SETTINGS(), ...over,
});
// GET /api/settings/auth and GET /api/auth/protection answer from the same
// fixture (extra keys are ignored by each reader).
function mockSettings(settings) {
  api.fetchAuthSettings.mockResolvedValue(settings);
  api.fetchAuthProtection.mockResolvedValue(settings);
}
const httpError = (status, message) => Object.assign(new Error(message), { status });

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

const card = () => screen.getByTestId("protection-card");
const theSwitch = () => within(card()).getByRole("switch", { name: "Password protection" });
// The switch is enabled once the settings (password_set) are in.
async function openForm() {
  await waitFor(() => expect(theSwitch()).not.toBeDisabled());
  fireEvent.click(theSwitch());
}

beforeEach(() => {
  localStorage.clear();
  mockSettings(OFF());
  api.fetchAuthSessions.mockResolvedValue(SESSIONS());
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: async () => ({}) })));
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("Password protection card: off", () => {
  it("the switch mirrors DISABLE_AUTH=1; only the switch opens the confirm form", async () => {
    renderCard({ authDisabled: true });
    // Before the settings arrive the card follows this device's auth check.
    expect(within(card()).getByText(/Password protection is off on this server \(DISABLE_AUTH=1\)/)).toBeInTheDocument();
    expect(theSwitch()).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByLabelText("Password")).toBeNull();
    await openForm();
    expect(theSwitch()).toHaveAttribute("aria-expanded", "true");
    expect(within(card()).getByLabelText("Password")).toBeInTheDocument();
    expect(within(card()).getByRole("button", { name: "Turn on" })).toBeDisabled(); // empty password
    fireEvent.click(within(card()).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Password")).toBeNull();
    expect(theSwitch()).toHaveAttribute("aria-checked", "false");
    expect(api.setAuthProtection).not.toHaveBeenCalled();
  });

  it("Turn on: sends the password, applies the returned settings, re-checks auth in place", async () => {
    api.setAuthProtection.mockResolvedValue(TURNED_ON());
    renderCard({ authDisabled: true });
    await openForm();
    fireEvent.change(within(card()).getByLabelText("Password"), { target: { value: "pw-123456" } });
    fireEvent.click(within(card()).getByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(theSwitch()).toHaveAttribute("aria-checked", "true"));
    expect(api.setAuthProtection).toHaveBeenCalledTimes(1);
    expect(api.setAuthProtection.mock.calls[0][0]).toMatchObject({ enabled: true, password: "pw-123456" });
    expect(api.setAuthProtection.mock.calls[0][0]).not.toHaveProperty("new_password");
    expect(within(card()).getByText(/Password protection is on \(DISABLE_AUTH=0\) — every device needs the password\./)).toBeInTheDocument();
    expect(screen.queryByText(/Password protection is off on this server/)).toBeNull();
    expect(screen.queryByLabelText("Password")).toBeNull();
    expect(await screen.findByText("Password protection is on")).toBeInTheDocument(); // toast
    // One re-check (AuthGuard flips authDisabled), not a second one from the
    // settings-vs-context sync; no /login, no reload.
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(mockNavigate).not.toHaveBeenCalled();
    await waitFor(() => expect(api.fetchAuthSessions).toHaveBeenCalledTimes(2)); // this device is listed now
  });

  it("no password set yet: choose + confirm, checked here first, then sent as new_password", async () => {
    mockSettings(OFF({ password_set: false }));
    api.setAuthProtection.mockResolvedValue(TURNED_ON());
    renderCard({ authDisabled: true });
    await openForm();
    expect(screen.queryByLabelText("Password")).toBeNull();
    const pick = within(card()).getByLabelText("Choose a password");
    const confirm = within(card()).getByLabelText("Confirm the password");
    fireEvent.change(pick, { target: { value: "short" } });
    fireEvent.change(confirm, { target: { value: "short" } });
    fireEvent.click(within(card()).getByRole("button", { name: "Turn on" }));
    expect(await within(card()).findByText("Password must be at least 8 characters")).toBeInTheDocument();
    fireEvent.change(pick, { target: { value: "long-enough-1" } });
    fireEvent.change(confirm, { target: { value: "long-enough-2" } });
    fireEvent.click(within(card()).getByRole("button", { name: "Turn on" }));
    expect(await within(card()).findByText("Passwords don't match")).toBeInTheDocument();
    expect(api.setAuthProtection).not.toHaveBeenCalled();
    fireEvent.change(confirm, { target: { value: "long-enough-1" } });
    fireEvent.click(within(card()).getByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(api.setAuthProtection).toHaveBeenCalledTimes(1));
    expect(api.setAuthProtection.mock.calls[0][0]).toMatchObject({ enabled: true, password: "", new_password: "long-enough-1" });
    await waitFor(() => expect(theSwitch()).toHaveAttribute("aria-checked", "true"));
    // The change-password form replaces the "no password yet" note.
    expect(screen.getByLabelText("Current password")).toBeInTheDocument();
  });

  it("wrong password (403): inline detail, still off, nobody signed out", async () => {
    localStorage.setItem("cc-auth-token", "xys_old");
    api.setAuthProtection.mockRejectedValue(httpError(403, "Password is wrong"));
    renderCard({ authDisabled: true });
    await openForm();
    fireEvent.change(within(card()).getByLabelText("Password"), { target: { value: "nope-nope" } });
    fireEvent.click(within(card()).getByRole("button", { name: "Turn on" }));
    expect(await within(card()).findByRole("alert")).toHaveTextContent("Password is wrong");
    expect(theSwitch()).toHaveAttribute("aria-checked", "false");
    expect(within(card()).getByLabelText("Password")).toHaveValue("nope-nope"); // form stays open
    expect(refresh).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_old");
  });

  it.each([
    [httpError(429, "Too many failed attempts. Try again in 42s."), "Too many failed attempts. Try again in 42s."],
    [httpError(429, "HTTP 429"), "Too many failed attempts. Try again later."],
    [httpError(500, "Could not update .env: Permission denied"), "Could not update .env: Permission denied"],
  ])("%s: shows %s, state unchanged", async (err, text) => {
    api.setAuthProtection.mockRejectedValue(err);
    renderCard({ authDisabled: true });
    await openForm();
    fireEvent.change(within(card()).getByLabelText("Password"), { target: { value: "pw-123456" } });
    fireEvent.click(within(card()).getByRole("button", { name: "Turn on" }));
    expect(await within(card()).findByRole("alert")).toHaveTextContent(text);
    expect(theSwitch()).toHaveAttribute("aria-checked", "false");
    expect(refresh).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});

describe("Password protection card: on", () => {
  beforeEach(() => {
    mockSettings(SETTINGS());
  });

  it("shows on; Turn off sends the password and switches the app to auth-off in place", async () => {
    localStorage.setItem("cc-auth-token", "xys_me");
    api.setAuthProtection.mockResolvedValue({ settings: OFF() });
    renderCard({ authDisabled: false });
    expect(within(card()).getByText(/Password protection is on \(DISABLE_AUTH=0\)/)).toBeInTheDocument();
    expect(theSwitch()).toHaveAttribute("aria-checked", "true");
    await openForm();
    expect(within(card()).queryByRole("button", { name: "Turn on" })).toBeNull();
    fireEvent.change(within(card()).getByLabelText("Password"), { target: { value: "pw-123456" } });
    fireEvent.click(within(card()).getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(theSwitch()).toHaveAttribute("aria-checked", "false"));
    expect(api.setAuthProtection.mock.calls[0][0]).toEqual({ enabled: false, password: "pw-123456" });
    expect(within(card()).getByText(/Password protection is off on this server \(DISABLE_AUTH=1\)/)).toBeInTheDocument();
    expect(await screen.findByText("Password protection is off")).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_me"); // sessions are kept
  });

  it("Turn off with a wrong password: 403 inline, still on, still signed in", async () => {
    localStorage.setItem("cc-auth-token", "xys_me");
    api.setAuthProtection.mockRejectedValue(httpError(403, "Password is wrong"));
    renderCard({ authDisabled: false });
    await openForm();
    fireEvent.change(within(card()).getByLabelText("Password"), { target: { value: "nope-nope" } });
    fireEvent.click(within(card()).getByRole("button", { name: "Turn off" }));
    expect(await within(card()).findByRole("alert")).toHaveTextContent("Password is wrong");
    expect(theSwitch()).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_me");
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("switched off on another device: the card follows the server and re-checks auth once", async () => {
    mockSettings(OFF());
    renderCard({ authDisabled: false });
    await waitFor(() => expect(theSwitch()).toHaveAttribute("aria-checked", "false"));
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    expect(within(card()).getByText(/Password protection is off on this server/)).toBeInTheDocument();
  });
});

describe(".env sync hint", () => {
  it.each([
    [OFF({ env_file_disable_auth: false, env_file_in_sync: false }), true, /now says DISABLE_AUTH=0 \(protection on\)/],
    [SETTINGS({ env_file_disable_auth: true, env_file_in_sync: false }), false, /now says DISABLE_AUTH=1 \(protection off\)/],
    [OFF({ env_file_disable_auth: null, env_file_in_sync: false }), true, /no longer sets DISABLE_AUTH \(protection on\)/],
  ])("out of sync: says what .env holds, how to apply it", async (settings, authDisabled, says) => {
    mockSettings(settings);
    renderCard({ authDisabled });
    const hint = await screen.findByTestId("env-file-hint");
    expect(hint).toHaveTextContent(says);
    expect(hint).toHaveTextContent(/edited by hand/);
    expect(hint).toHaveTextContent(/Restart the server to apply it, or use this switch, which rewrites \.env\./);
  });

  it("in sync, or a payload without the field: no hint", async () => {
    mockSettings(OFF());
    const { unmount } = renderCard({ authDisabled: true });
    await waitFor(() => expect(theSwitch()).not.toBeDisabled());
    expect(screen.queryByTestId("env-file-hint")).toBeNull();
    unmount();
    const legacy = OFF();
    delete legacy.env_file_disable_auth;
    delete legacy.env_file_in_sync;
    mockSettings(legacy);
    renderCard({ authDisabled: true });
    await waitFor(() => expect(theSwitch()).not.toBeDisabled());
    expect(screen.queryByTestId("env-file-hint")).toBeNull();
  });
});
