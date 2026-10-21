import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { installPushEnv, uninstallPushEnv, setVisibility, settingsRequests } from "../lib/pushEnv.testutil";

const IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";
const MAC_SAFARI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15";
const LINUX_CHROME_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

// Fresh module graph per test: the push lib keeps per-page state.
async function renderBell() {
  vi.resetModules();
  const { ToastProvider } = await import("../contexts/ToastContext");
  const { default: DeviceNotifBell } = await import("./DeviceNotifBell");
  const lib = await import("../lib/pushNotifications");
  render(<ToastProvider><DeviceNotifBell /></ToastProvider>);
  return { bell: screen.getByRole("button"), lib };
}

beforeEach(() => {
  vi.spyOn(console, "debug").mockImplementation(() => {});
  localStorage.clear();
  setVisibility("visible");
});

afterEach(() => {
  // Spec: the bell never touches the legacy global settings endpoint.
  expect(settingsRequests()).toEqual([]);
  vi.restoreAllMocks();
  uninstallPushEnv();
});

/** Make /api/push/subscribe wait until the returned release() is called. */
function holdSubscribe() {
  const realFetch = globalThis.fetch;
  let release;
  const gate = new Promise((r) => { release = r; });
  vi.stubGlobal("fetch", vi.fn(async (url, opts) => {
    if (url === "/api/push/subscribe") await gate;
    return realFetch(url, opts);
  }));
  return () => release();
}

describe("DeviceNotifBell", () => {
  it("off-device: mounting never prompts; a tap prompts inside the click", async () => {
    const env = installPushEnv({ permission: "default" });
    const { bell } = await renderBell();
    expect(bell).toHaveAttribute("data-state", "off-device");
    expect(bell).toHaveAttribute("title", "Turn on notifications on this device");
    await waitFor(() => expect(env.requests.map((r) => r.url)).toContain("/api/push/vapid-public-key"));
    expect(env.requestPermission).not.toHaveBeenCalled();

    fireEvent.click(bell);
    // fireEvent dispatches synchronously: the prompt was requested during the click.
    expect(env.requestPermission).toHaveBeenCalledTimes(1);

    await waitFor(() => expect(bell).toHaveAttribute("data-state", "on"));
    expect(env.posts()).toEqual(["/api/push/subscribe", "/api/push/test"]);
    expect(await screen.findByText("Notifications on for this device")).toBeInTheDocument();
  });

  it("on → tap mutes this device only", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: true });
    const { bell, lib } = await renderBell();
    await lib.syncPushDevice();
    await waitFor(() => expect(bell).toHaveAttribute("data-state", "on"));
    expect(bell).toHaveAttribute("title", "Mute notifications on this device");

    fireEvent.click(bell);
    await waitFor(() => expect(bell).toHaveAttribute("data-state", "muted"));
    expect(env.requests.at(-1)).toMatchObject({ url: "/api/push/device", body: { agents_enabled: false } });
    expect(await screen.findByText("Notifications muted on this device")).toBeInTheDocument();
    expect(env.requests.some((r) => r.url.startsWith("/api/settings"))).toBe(false);
    expect(env.requestPermission).not.toHaveBeenCalled();
  });

  it("muted → tap unmutes", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: true, device: { agents_enabled: false } });
    const { bell, lib } = await renderBell();
    await lib.syncPushDevice();
    await waitFor(() => expect(bell).toHaveAttribute("data-state", "muted"));
    expect(bell).toHaveAttribute("title", "Unmute notifications on this device");

    expect(bell.className).not.toMatch(/opacity-/); // muted ≠ blocked at a glance

    fireEvent.click(bell);
    await waitFor(() => expect(bell).toHaveAttribute("data-state", "on"));
    expect(env.requests.at(-1)).toMatchObject({ url: "/api/push/device", body: { agents_enabled: true } });
    expect(await screen.findByText("Notifications unmuted on this device")).toBeInTheDocument();
  });

  it("granted, nothing cached: shows 'syncing' (not off-device) and a tap can't unmute", async () => {
    const env = installPushEnv({ permission: "granted", subscribed: true, device: { agents_enabled: false } });
    const release = holdSubscribe();
    const { bell } = await renderBell();
    // The bell asks the server itself; it never offers "turn on" meanwhile.
    expect(bell).toHaveAttribute("data-state", "syncing");
    expect(bell).toHaveAttribute("aria-busy", "true");
    fireEvent.click(bell);
    expect(env.requestPermission).not.toHaveBeenCalled();

    release();
    await waitFor(() => expect(bell).toHaveAttribute("data-state", "muted"));
    expect(env.posts()).toEqual(["/api/push/subscribe"]);
    expect(env.device.agents_enabled).toBe(false);
  });

  it("server state wins over the cached state", async () => {
    localStorage.setItem("xylocopa-push-device", JSON.stringify({ agents_enabled: true }));
    installPushEnv({ permission: "granted", subscribed: true, device: { agents_enabled: false } });
    const { bell, lib } = await renderBell();
    expect(bell).toHaveAttribute("data-state", "on");
    await lib.syncPushDevice();
    await waitFor(() => expect(bell).toHaveAttribute("data-state", "muted"));
  });

  it("unsupported: tap shows the Home Screen hint and never prompts", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(IPHONE_UA);
    const env = installPushEnv({ supported: false });
    const { bell } = await renderBell();
    expect(bell).toHaveAttribute("data-state", "unsupported");
    fireEvent.click(bell);
    expect(await screen.findByText("Add Xylocopa to your Home Screen to get notifications")).toBeInTheDocument();
    expect(env.requestPermission).not.toHaveBeenCalled();
    expect(env.posts()).toEqual([]);
  });

  it("denied: tap explains how to re-allow and never prompts", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(IPHONE_UA);
    const env = installPushEnv({ permission: "denied" });
    const { bell } = await renderBell();
    expect(bell).toHaveAttribute("data-state", "denied");
    expect(bell.className).toMatch(/opacity-40/); // dimmed, unlike muted
    fireEvent.click(bell);
    expect(await screen.findByText(/Settings › Notifications › Xylocopa/)).toBeInTheDocument();
    expect(env.requestPermission).not.toHaveBeenCalled();
    expect(env.posts()).toEqual([]);
  });

  it.each([
    ["Mac Safari tab", MAC_SAFARI_UA, false, /Safari › Settings › Websites › Notifications/],
    ["Mac Dock web app", MAC_SAFARI_UA, true, /System Settings › Notifications › Xylocopa/],
    ["other desktop browsers", LINUX_CHROME_UA, false, /Allow notifications for this site in your browser's site settings/],
  ])("denied hint for %s", async (_name, ua, standalone, hint) => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(ua);
    vi.stubGlobal("matchMedia", vi.fn((q) => ({ matches: standalone && q === "(display-mode: standalone)" })));
    installPushEnv({ permission: "denied" });
    const { bell } = await renderBell();
    fireEvent.click(bell);
    expect(await screen.findByText(hint)).toBeInTheDocument();
    expect(screen.queryByText(/lock icon/)).toBeNull();
  });
});
