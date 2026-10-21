import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

import LoginPage from "./LoginPage";

const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});

const CHECK = (over = {}) => ({
  authenticated: false, needs_setup: false, auth_disabled: false, reason: "missing", session: null,
  idle_timeout_minutes: 30, idle_remaining_s: null, media_token: null, media_expires_at: null,
  server_time: Date.now() / 1000, ...over,
});
const LOGIN_RESPONSE = {
  token: "xys_new", session_id: "s2", expires_at: 1e10, idle_timeout_minutes: 30, expires_minutes: 30,
  media_token: "m.new", media_expires_at: Date.now() / 1000 + 43200,
};

let calls;
let checkBody;
let loginReply;
function stubFetch() {
  calls = [];
  vi.stubGlobal("fetch", vi.fn((url, opts = {}) => {
    calls.push({ url, opts });
    if (url === "/api/auth/check") {
      if (checkBody === "hang") {
        return new Promise((_, reject) => {
          opts.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      }
      return Promise.resolve(res(200, checkBody));
    }
    if (url === "/api/auth/login" || url === "/api/auth/set-password") return Promise.resolve(loginReply());
    return Promise.resolve(res(200, {}));
  }));
}
const posts = (url) => calls.filter((c) => c.url === url);

function renderLogin(state) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/login", state }]}>
      <LoginPage />
    </MemoryRouter>,
  );
}

const typePassword = (value, placeholder = "Password") =>
  fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });

let expired;
const onExpired = (e) => expired.push(e.detail);

beforeEach(() => {
  localStorage.clear();
  mockNavigate.mockReset();
  checkBody = CHECK();
  loginReply = () => res(200, LOGIN_RESPONSE);
  stubFetch();
  expired = [];
  window.addEventListener("auth-expired", onExpired);
  vi.spyOn(console, "log").mockImplementation(() => {});
});
afterEach(() => {
  window.removeEventListener("auth-expired", onExpired);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LoginPage subtitle", () => {
  it.each([
    [{ reason: "revoked" }, CHECK({ reason: "missing" }), "Signed out from another device"],
    [{ reason: "expired" }, CHECK({ reason: "missing" }), "Session expired"],
    [{ reason: "signed-out-elsewhere" }, CHECK({ reason: "missing" }), "Signed out in another tab"],
    [{ reason: "signed-out" }, CHECK({ reason: "missing" }), "Signed out"],
    [{ reason: "idle" }, CHECK({ reason: "idle", idle_timeout_minutes: 30 }), "Locked after 30 min of inactivity"],
    [undefined, CHECK({ reason: "idle", idle_timeout_minutes: 120 }), "Locked after 2 h of inactivity"],
    [undefined, CHECK({ reason: "missing" }), "Locked"],
    [{ reason: "invalid" }, CHECK({ reason: "invalid" }), "Locked"],
  ])("state %j + check reason → %s", async (state, check, text) => {
    checkBody = check;
    renderLogin(state);
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("falls back to the stored lock flag when the check did not say", async () => {
    localStorage.setItem("cc-auth-token", "xys_a");
    localStorage.setItem("cc-auth-locked", "idle");
    checkBody = CHECK({ reason: null, idle_timeout_minutes: 5 });
    renderLogin();
    expect(await screen.findByText("Locked after 5 min of inactivity")).toBeInTheDocument();
  });

  it("leaves for the app when the check is already authenticated (or auth is off)", async () => {
    checkBody = CHECK({ authenticated: true, auth_disabled: true, reason: null });
    renderLogin();
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/", { replace: true }));
  });
});

describe("LoginPage submit", () => {
  it("logs in with the old token as Bearer and the device label, then stores the session", async () => {
    localStorage.setItem("cc-auth-token", "xys_locked");
    localStorage.setItem("cc-auth-locked", "idle");
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    );
    checkBody = CHECK({ reason: "idle" });
    renderLogin({ reason: "idle" });
    await screen.findByText("Locked after 30 min of inactivity");
    typePassword("correct-horse");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Unlock" })); });

    const [login] = posts("/api/auth/login");
    expect(login.opts.headers.Authorization).toBe("Bearer xys_locked");
    expect(JSON.parse(login.opts.body)).toEqual({ password: "correct-horse", device_label: "Mac · Chrome" });
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_new");
    expect(JSON.parse(localStorage.getItem("cc-media-token")).token).toBe("m.new");
    expect(localStorage.getItem("cc-auth-locked")).toBeNull();
    expect(mockNavigate).toHaveBeenCalledWith("/", { replace: true });
  });

  it("a wrong password shows the server's message and logs nobody out", async () => {
    localStorage.setItem("cc-auth-token", "xys_locked");
    loginReply = () => res(401, { detail: "Wrong password" });
    renderLogin({ reason: "idle" });
    await screen.findByPlaceholderText("Password");
    typePassword("nope-nope");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Unlock" })); });
    expect(await screen.findByText("Wrong password")).toBeInTheDocument();
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_locked");
    expect(expired).toEqual([]);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("first-run setup requires at least 8 characters", async () => {
    checkBody = CHECK({ needs_setup: true, reason: null });
    renderLogin();
    expect(await screen.findByText("Set a password to get started")).toBeInTheDocument();
    typePassword("seven77", "New password");
    fireEvent.change(screen.getByPlaceholderText("Confirm password"), { target: { value: "seven77" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Set Password" })); });
    expect(screen.getByText("Password must be at least 8 characters")).toBeInTheDocument();
    expect(posts("/api/auth/set-password")).toHaveLength(0);

    typePassword("eight888", "New password");
    fireEvent.change(screen.getByPlaceholderText("Confirm password"), { target: { value: "eight888" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Set Password" })); });
    const [setup] = posts("/api/auth/set-password");
    expect(JSON.parse(setup.opts.body).password).toBe("eight888");
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_new");
    expect(mockNavigate).toHaveBeenCalledWith("/", { replace: true });
  });
});

describe("LoginPage mount check timeout", () => {
  it("shows the form after 8 s with a Retry that aborts the hung check", async () => {
    vi.useFakeTimers();
    checkBody = "hang";
    renderLogin({ reason: "revoked" });
    expect(screen.getByText("Loading...")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(screen.getByText(/Server is slow to respond/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Password")).toBeInTheDocument();
    expect(screen.getByText("Signed out from another device")).toBeInTheDocument();

    const checks = posts("/api/auth/check");
    expect(checks).toHaveLength(1);
    expect(checks[0].opts.signal.aborted).toBe(true);
    checkBody = CHECK({ reason: "revoked" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry" })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(posts("/api/auth/check")).toHaveLength(2);
    expect(screen.queryByText(/Server is slow to respond/)).toBeNull();
  });
});
