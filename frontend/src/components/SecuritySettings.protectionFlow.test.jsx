import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

// The password-protection switch end to end in the browser: the real
// AuthGuard, api.js, WebSocketProvider and SecuritySettings against a small
// fake server (fetch + WebSocket stubs) whose DISABLE_AUTH can flip.
//   - this device: turning protection on stores a session and leaves
//     auth-disabled mode in place (no reload, no /login); its socket comes
//     back with a ticket. Turning it off goes the other way.
//   - other devices: one that booted with protection off lands on /login
//     after the server's 4001 or its next 401; a signed-in one keeps working
//     when protection goes off elsewhere.

vi.mock("../lib/pushNotifications", () => ({
  startPushDeviceSync: vi.fn(() => Promise.resolve()),
}));

import AuthGuard from "./AuthGuard";
import SecuritySettings from "./SecuritySettings";
import { WebSocketProvider } from "../contexts/WebSocketContext";
import { ToastProvider } from "../contexts/ToastContext";

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.CONNECTING;
    MockWebSocket.instances.push(this);
  }
  send() {}
  close() {
    if (this.readyState === MockWebSocket.CLOSED) return;
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({});
  }
  _open() { this.readyState = MockWebSocket.OPEN; this.onopen?.(); }
  // A server-side close frame.
  _drop(code, reason) { this.readyState = MockWebSocket.CLOSED; this.onclose?.({ code, reason }); }
}
const sockets = () => MockWebSocket.instances;
const lastSocket = () => sockets()[sockets().length - 1];

const PASSWORD = "pw-123456";
const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});

// --- fake server -----------------------------------------------------------
let server;
function settingsBody() {
  return {
    idle_timeout_minutes: 30, choices: [0, 30, 60], default_idle_timeout_minutes: 30, session_max_days: 90,
    auth_disabled: server.authOff, password_set: true,
  };
}
function protectionBody() {
  return {
    auth_disabled: server.authOff, password_set: true,
    env_file_disable_auth: server.authOff, env_file_in_sync: true,
  };
}
function serverFetch(url, opts = {}) {
  server.calls.push(url);
  const bearer = opts.headers?.Authorization || "";
  const token = bearer.startsWith("Bearer ") ? bearer.slice(7) : null;
  const valid = !!token && server.tokens.has(token);
  if (url === "/api/debug/auth-diag") return res(200, { ok: true });
  if (url === "/api/auth/check") {
    const base = {
      authenticated: false, needs_setup: false, auth_disabled: false, reason: null, session: null,
      idle_timeout_minutes: 30, idle_remaining_s: null, media_token: null, media_expires_at: null,
      server_time: Date.now() / 1000,
    };
    if (server.authOff) return res(200, { ...base, authenticated: true, auth_disabled: true });
    if (!token) return res(200, { ...base, reason: "missing" });
    if (!valid) return res(200, { ...base, reason: "invalid" });
    return res(200, {
      ...base, authenticated: true, idle_remaining_s: 1799,
      session: { id: `s-${token}`, legacy: false, device_label: "Linux · Chrome", created_at: 1, expires_at: 1e10 },
      media_token: `m-${token}`, media_expires_at: Date.now() / 1000 + 12 * 3600,
    });
  }
  // The auth middleware: everything below needs a session while auth is on.
  if (!server.authOff && !valid) {
    return res(401, { detail: "Not authenticated" }, { "X-Auth-Reason": token ? "invalid" : "missing" });
  }
  const body = opts.body ? JSON.parse(opts.body) : {};
  switch (url) {
    case "/api/auth/protection": {
      if ((opts.method || "GET") === "GET") return res(200, protectionBody());
      if (body.password !== PASSWORD) return res(403, { detail: "Password is wrong" });
      server.authOff = !body.enabled;
      if (!body.enabled) return res(200, { settings: settingsBody(), protection: protectionBody() });
      const fresh = "xys_fresh";
      server.tokens.add(fresh);
      return res(200, {
        token: fresh, session_id: `s-${fresh}`, expires_at: 1e10, idle_timeout_minutes: 30, expires_minutes: 30,
        media_token: `m-${fresh}`, media_expires_at: Date.now() / 1000 + 12 * 3600, settings: settingsBody(),
        protection: protectionBody(),
      });
    }
    case "/api/settings/auth":
      return res(200, settingsBody());
    case "/api/auth/sessions":
      return res(200, {
        sessions: valid ? [{ id: `s-${token}`, device_label: "Linux · Chrome", last_seen_at: Date.now() / 1000, state: "active", current: true }] : [],
        current_id: valid ? `s-${token}` : null,
        auth_disabled: server.authOff,
      });
    case "/api/auth/ws-ticket":
      return res(200, server.authOff ? { ticket: null, expires_in: 0 } : { ticket: `xyt_${++server.tickets}`, expires_in: 30 });
    default:
      return res(200, {});
  }
}

function LocationProbe() {
  return <div data-testid="location">{useLocation().pathname}</div>;
}
function renderApp() {
  return render(
    <MemoryRouter initialEntries={["/monitor"]}>
      <ToastProvider>
        <Routes>
          <Route path="/login" element={<div>login screen</div>} />
          <Route
            path="/*"
            element={(
              <AuthGuard>
                <WebSocketProvider>
                  <SecuritySettings />
                </WebSocketProvider>
              </AuthGuard>
            )}
          />
        </Routes>
        <LocationProbe />
      </ToastProvider>
    </MemoryRouter>,
  );
}

const pathname = () => screen.getByTestId("location").textContent;
const card = () => screen.getByTestId("protection-card");
const theSwitch = () => within(card()).getByRole("switch", { name: "Password protection" });
async function confirmSwitch(buttonName, password = PASSWORD) {
  await waitFor(() => expect(theSwitch()).not.toBeDisabled());
  fireEvent.click(theSwitch());
  fireEvent.change(within(card()).getByLabelText("Password"), { target: { value: password } });
  fireEvent.click(within(card()).getByRole("button", { name: buttonName }));
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  server = { authOff: true, tokens: new Set(), tickets: 0, calls: [] };
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  vi.stubGlobal("fetch", vi.fn(async (url, opts) => serverFetch(url, opts)));
  vi.stubGlobal("caches", { delete: vi.fn(() => Promise.resolve(true)) });
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("this device", () => {
  it("turning protection on: signed in at once, auth mode flips without /login, socket returns with a ticket", async () => {
    renderApp();
    expect(await screen.findByText(/Password protection is off on this server \(DISABLE_AUTH=1\)/)).toBeInTheDocument();
    await waitFor(() => expect(sockets()).toHaveLength(1));
    const bare = sockets()[0];
    expect(bare.url).not.toContain("ticket=");
    act(() => bare._open());
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();

    await confirmSwitch("Turn on");

    await waitFor(() => expect(theSwitch()).toHaveAttribute("aria-checked", "true"));
    // Stored like a login.
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_fresh");
    expect(JSON.parse(localStorage.getItem("cc-media-token")).token).toMatch(/^m-xys_fresh$/);
    // AuthGuard re-checked in place: authDisabled is false now.
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
    // The bare socket was replaced by one opened with a ticket.
    await waitFor(() => expect(lastSocket().url).toContain("ticket=xyt_"));
    expect(bare.readyState).toBe(MockWebSocket.CLOSED);
    // The server's delayed 4001 for the ticketless socket changes nothing.
    act(() => bare._drop(4001, "missing"));
    // Signed-in device list now shows this device.
    expect(await screen.findByText("This device")).toBeInTheDocument();
    expect(pathname()).toBe("/monitor");
    expect(screen.queryByText("login screen")).toBeNull();
  });

  it("a wrong password while turning on stores nothing and stays off", async () => {
    renderApp();
    await screen.findByText(/Password protection is off on this server/);
    await confirmSwitch("Turn on", "wrong-password");
    expect(await within(card()).findByRole("alert")).toHaveTextContent("Password is wrong");
    expect(server.authOff).toBe(true);
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
    expect(pathname()).toBe("/monitor");
  });

  it("turning protection off: auth-disabled mode in place, session kept, socket reconnects bare", async () => {
    server.authOff = false;
    server.tokens.add("xys_me");
    localStorage.setItem("cc-auth-token", "xys_me");
    renderApp();
    expect(await screen.findByText(/Password protection is on \(DISABLE_AUTH=0\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    await waitFor(() => expect(sockets()).toHaveLength(1));
    const ticketed = sockets()[0];
    expect(ticketed.url).toContain("ticket=xyt_1");
    act(() => ticketed._open());

    await confirmSwitch("Turn off");

    await waitFor(() => expect(theSwitch()).toHaveAttribute("aria-checked", "false"));
    expect(server.authOff).toBe(true);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull());
    await waitFor(() => expect(sockets().length).toBeGreaterThan(1));
    expect(lastSocket().url).not.toContain("ticket=");
    expect(ticketed.readyState).toBe(MockWebSocket.CLOSED);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_me");
    expect(pathname()).toBe("/monitor");
  });

  it("a wrong password while turning off is a 403: still on, still signed in", async () => {
    server.authOff = false;
    server.tokens.add("xys_me");
    localStorage.setItem("cc-auth-token", "xys_me");
    renderApp();
    await screen.findByText(/Password protection is on \(DISABLE_AUTH=0\)/);
    await confirmSwitch("Turn off", "wrong-password");
    expect(await within(card()).findByRole("alert")).toHaveTextContent("Password is wrong");
    expect(server.authOff).toBe(false);
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_me");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(pathname()).toBe("/monitor");
  });
});

describe("other devices", () => {
  // api.js keeps the post-login grace timestamp in module memory, and the
  // turn-on tests above stored a session moments ago. Another device never
  // logged in here: move the clock past that window (Date only; timers stay
  // real).
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(Date.now() + 60 * 60 * 1000));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  // The backend picks the close reason; every variant must end on /login.
  it.each(["missing", "revoked", "auth-enabled"])(
    "booted with protection off: the server's 4001 (%s) on the bare socket lands on /login",
    async (reason) => {
      renderApp();
      await screen.findByText(/Password protection is off on this server/);
      await waitFor(() => expect(sockets()).toHaveLength(1));
      act(() => sockets()[0]._open());
      server.authOff = false; // turned on from another device
      act(() => sockets()[0]._drop(4001, reason));
      expect(await screen.findByText("login screen")).toBeInTheDocument();
      expect(pathname()).toBe("/login");
    },
  );

  it("booted with protection off: its next API 401 (X-Auth-Reason) lands on /login", async () => {
    renderApp();
    const select = await screen.findByLabelText("Idle timeout");
    await waitFor(() => expect(select).not.toBeDisabled());
    server.authOff = false;
    fireEvent.change(select, { target: { value: "60" } });
    expect(await screen.findByText("login screen")).toBeInTheDocument();
    expect(pathname()).toBe("/login");
  });

  it("signed in when protection goes off elsewhere: keeps working, reconnects bare, no /login", async () => {
    server.authOff = false;
    server.tokens.add("xys_me");
    localStorage.setItem("cc-auth-token", "xys_me");
    renderApp();
    const select = await screen.findByLabelText("Idle timeout");
    await waitFor(() => expect(select).not.toBeDisabled());
    await waitFor(() => expect(sockets()).toHaveLength(1));
    act(() => sockets()[0]._open());
    server.authOff = true; // turned off from another device
    act(() => sockets()[0]._drop(1006, ""));
    await waitFor(() => expect(sockets()).toHaveLength(2), { timeout: 3000 });
    expect(sockets()[1].url).not.toContain("ticket=");
    fireEvent.change(select, { target: { value: "60" } });
    await waitFor(() => expect(server.calls.filter((u) => u === "/api/settings/auth").length).toBeGreaterThan(1));
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_me");
    expect(pathname()).toBe("/monitor");
  });
});
