import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Signed-in devices: each row shows its device's push state
// ("notifications" from GET /api/auth/sessions).

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal()),
  fetchAuthSettings: vi.fn(),
  fetchAuthSessions: vi.fn(),
}));

import SecuritySettings from "./SecuritySettings";
import { AuthProvider } from "../contexts/AuthContext";
import { ToastProvider } from "../contexts/ToastContext";
import * as api from "../lib/api";

const now = () => Date.now() / 1000;
const ROW = (over) => ({
  id: "s1", device_label: "Mac · Chrome", user_agent: "UA", created_at: now() - 3600,
  last_seen_at: now() - 30, last_active_at: now() - 30, last_ip: "100.64.0.10",
  expires_at: now() + 86400, state: "active", current: false, notifications: null, ...over,
});

function renderCard() {
  const value = { authReady: true, authDisabled: false, session: null, idleTimeoutMinutes: 30,
    refresh: vi.fn(() => Promise.resolve(null)) };
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

beforeEach(() => {
  localStorage.setItem("cc-auth-token", "xys_me");
  api.fetchAuthSettings.mockResolvedValue({
    idle_timeout_minutes: 30, choices: [0, 30], default_idle_timeout_minutes: 30,
    session_max_days: 90, auth_disabled: false, password_set: true,
  });
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: async () => ({}) })));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("Signed-in devices: notification state", () => {
  it("shows on / muted / off per device and nothing when unknown", async () => {
    api.fetchAuthSessions.mockResolvedValue({
      sessions: [
        ROW({ id: "me", device_label: "iPad · Installed app", current: true, notifications: "on" }),
        ROW({ id: "phone", device_label: "iPhone · Installed app", state: "idle", notifications: "muted" }),
        ROW({ id: "mac", device_label: "Mac · Installed app", notifications: "off" }),
        ROW({ id: "old", device_label: "iPad · Safari", notifications: null }),
      ],
      current_id: "me",
      auth_disabled: false,
    });
    renderCard();
    const list = await screen.findByTestId("device-list");
    const row = (label) => within(list).getByText(label).closest("li");
    expect(within(row("iPad · Installed app")).getByText("Notifications on")).toBeInTheDocument();
    const phone = row("iPhone · Installed app");
    expect(within(phone).getByText("Locked")).toBeInTheDocument();
    expect(within(phone).getByText("Notifications muted")).toHaveAttribute("title", expect.stringMatching(/bell/));
    expect(within(row("Mac · Installed app")).getByText("Notifications off")).toBeInTheDocument();
    expect(within(row("iPad · Safari")).queryByText(/^Notifications/)).toBeNull();
  });
});
