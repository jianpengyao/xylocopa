import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});
vi.mock("../lib/pushNotifications", () => ({
  startPushDeviceSync: vi.fn(() => Promise.resolve()),
}));

import AuthGuard from "./AuthGuard";
import { useAuth } from "../contexts/AuthContext";
import { getMediaToken } from "../lib/api";
import { fileUrl } from "../lib/urls";

const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});

const MEDIA_EXP = () => Date.now() / 1000 + 12 * 3600;
const AUTHED = () => ({
  authenticated: true, needs_setup: false, auth_disabled: false, reason: null,
  session: { id: "s1", legacy: false, device_label: "Mac · Chrome", created_at: 1, expires_at: 2 },
  idle_timeout_minutes: 30, idle_remaining_s: 1799,
  media_token: "m.boot", media_expires_at: MEDIA_EXP(), server_time: Date.now() / 1000,
});

// fetch stub: /api/auth/check answers come from a queue of handlers; every
// call records its signal so tests can assert aborts.
let checks;
let checkQueue;
let others;
function stubFetch() {
  checks = [];
  others = [];
  vi.stubGlobal("fetch", vi.fn((url, opts = {}) => {
    if (url === "/api/auth/check") {
      const handler = checkQueue.shift() || (() => new Promise(() => {}));
      const entry = { signal: opts.signal, headers: opts.headers };
      checks.push(entry);
      return new Promise((resolve, reject) => {
        opts.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        Promise.resolve().then(handler).then(resolve, reject);
      });
    }
    others.push({ url, opts });
    if (url === "/api/auth/media-token") {
      return Promise.resolve(res(200, { token: "m.fresh", expires_at: MEDIA_EXP(), expires_in: 43200 }));
    }
    return Promise.resolve(res(200, {}));
  }));
}
const hang = () => new Promise(() => {});

function renderGuard(child = <div>app content</div>) {
  return render(
    <MemoryRouter>
      <AuthGuard>{child}</AuthGuard>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  mockNavigate.mockReset();
  checkQueue = [];
  stubFetch();
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AuthGuard boot check", () => {
  it("shows Retry after 8 s without an answer; Retry aborts the check in flight", async () => {
    vi.useFakeTimers();
    checkQueue = [hang, hang, () => res(200, AUTHED())];
    renderGuard();
    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(7999); });
    expect(screen.queryByText("Still connecting…")).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText("Still connecting…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
    // The timed-out check was aborted and another attempt is already running.
    expect(checks).toHaveLength(2);
    expect(checks[0].signal.aborted).toBe(true);
    expect(checks[1].signal.aborted).toBe(false);

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry" })); });
    expect(checks[1].signal.aborted).toBe(true);
    expect(checks).toHaveLength(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("app content")).toBeInTheDocument();
    expect(screen.queryByText("Still connecting…")).toBeNull();
  });

  it("stores the media token before the children's first render", async () => {
    checkQueue = [() => res(200, AUTHED())];
    const firstRender = [];
    function Child() {
      const [url] = [fileUrl("p", "a.png")];
      firstRender.push({ token: getMediaToken(), url });
      return <img alt="x" src={url} />;
    }
    renderGuard(<Child />);
    await screen.findByRole("img");
    expect(firstRender[0]).toEqual({ token: "m.boot", url: "/api/files/p/a.png?token=m.boot" });
  });

  it("shares the session through useAuth()", async () => {
    checkQueue = [() => res(200, AUTHED())];
    const renders = [];
    function Child() { renders.push(useAuth()); return <div>child</div>; }
    renderGuard(<Child />);
    await screen.findByText("child");
    const seen = renders.at(-1);
    expect(seen).toMatchObject({
      authReady: true, authDisabled: false, idleTimeoutMinutes: 30, session: { id: "s1", device_label: "Mac · Chrome" },
    });
    expect(typeof seen.refresh).toBe("function");
  });

  it("under DISABLE_AUTH renders everything, with no token and no media token", async () => {
    localStorage.setItem("cc-media-token", JSON.stringify({ token: "m.stale", exp: MEDIA_EXP() }));
    checkQueue = [() => res(200, { ...AUTHED(), auth_disabled: true, session: null, media_token: null, media_expires_at: null })];
    const renders = [];
    function Child() { renders.push(useAuth()); return <img alt="x" src={fileUrl("p", "a.png")} />; }
    renderGuard(<Child />);
    const img = await screen.findByRole("img");
    expect(img).toHaveAttribute("src", "/api/files/p/a.png");
    expect(renders.at(-1)).toMatchObject({ authReady: true, authDisabled: true, session: null });
    expect(mockNavigate).not.toHaveBeenCalled();
    // Activity signal and media upkeep stay off.
    fireEvent.pointerDown(window);
    document.dispatchEvent(new Event("visibilitychange"));
    await act(async () => { await Promise.resolve(); });
    expect(others.filter((c) => c.url.startsWith("/api/auth/"))).toEqual([]);
  });

  it("refresh() after DISABLE_AUTH was flipped off turns auth on in place, with a media token", async () => {
    localStorage.setItem("cc-auth-token", "xys_tab");
    checkQueue = [
      () => res(200, { ...AUTHED(), auth_disabled: true, session: null, media_token: null, media_expires_at: null }),
      () => res(200, AUTHED()),
    ];
    const renders = [];
    function Child() { renders.push(useAuth()); return <div>child</div>; }
    renderGuard(<Child />);
    await screen.findByText("child");
    expect(renders.at(-1).authDisabled).toBe(true);
    await act(async () => { await renders.at(-1).refresh(); });
    expect(renders.at(-1)).toMatchObject({ authReady: true, authDisabled: false, session: { id: "s1" } });
    expect(getMediaToken()).toBe("m.boot");
    expect(screen.getByText("child")).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("idle: keeps the token, flags the lock and goes to /login with the reason", async () => {
    localStorage.setItem("cc-auth-token", "xys_a");
    checkQueue = [() => res(200, { ...AUTHED(), authenticated: false, reason: "idle", session: null, media_token: null })];
    renderGuard();
    await act(async () => { await Promise.resolve(); });
    await vi.waitFor(() => expect(mockNavigate).toHaveBeenCalled());
    expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true, state: { reason: "idle" } });
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
    expect(localStorage.getItem("cc-auth-locked")).toBe("idle");
    expect(screen.queryByText("app content")).toBeNull();
  });

  it("revoked: drops the token and goes to /login with the reason", async () => {
    localStorage.setItem("cc-auth-token", "xys_a");
    checkQueue = [() => res(200, { ...AUTHED(), authenticated: false, reason: "revoked", session: null, media_token: null })];
    renderGuard();
    await vi.waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true, state: { reason: "revoked" } }));
    expect(localStorage.getItem("cc-auth-token")).toBeNull();
  });

  it("503 'Auth store unavailable' shows the reconnect screen and keeps the token", async () => {
    localStorage.setItem("cc-auth-token", "xys_a");
    checkQueue = [() => res(503, { detail: "Auth store unavailable" })];
    renderGuard();
    expect(await screen.findByText("Reconnecting to server...")).toBeInTheDocument();
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});

describe("AuthGuard while signed in", () => {
  it("auth-expired navigates to /login with the reason (api.js already handled the token)", async () => {
    localStorage.setItem("cc-auth-token", "xys_a");
    checkQueue = [() => res(200, AUTHED())];
    renderGuard();
    await screen.findByText("app content");
    act(() => { window.dispatchEvent(new CustomEvent("auth-expired", { detail: { reason: "revoked" } })); });
    expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true, state: { reason: "revoked" } });
    // The listener itself no longer touches the token.
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_a");
  });

  it("re-mints the media token on resume once less than 2 h are left", async () => {
    localStorage.setItem("cc-auth-token", "xys_a");
    checkQueue = [() => res(200, { ...AUTHED(), media_expires_at: Date.now() / 1000 + 3 * 3600 })];
    renderGuard();
    await screen.findByText("app content");
    const visible = () => act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    await visible();
    expect(others.filter((c) => c.url === "/api/auth/media-token")).toHaveLength(0);

    localStorage.setItem("cc-media-token", JSON.stringify({ token: "m.boot", exp: Date.now() / 1000 + 3600 }));
    await visible();
    await vi.waitFor(() => expect(getMediaToken()).toBe("m.fresh"));
    expect(others.filter((c) => c.url === "/api/auth/media-token")).toHaveLength(1);
  });
});

describe("AuthGuard server-down probe", () => {
  it("Retry now aborts the probe in flight and probes again", async () => {
    vi.useFakeTimers();
    checkQueue = [() => { throw new TypeError("Failed to fetch"); }, hang, hang];
    renderGuard();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("Reconnecting to server...")).toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(checks).toHaveLength(2);
    const hungProbe = checks[1];
    expect(hungProbe.signal.aborted).toBe(false);

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry now" })); });
    expect(hungProbe.signal.aborted).toBe(true);
    expect(checks).toHaveLength(3);
    expect(checks[2].signal.aborted).toBe(false);
    // The aborted probe does not schedule a retry of its own.
    await act(async () => { await vi.advanceTimersByTimeAsync(7000); });
    expect(checks).toHaveLength(3);
  });

  it("recovers once a probe answers: the normal auth flow runs again", async () => {
    vi.useFakeTimers();
    checkQueue = [() => { throw new TypeError("Failed to fetch"); }, () => res(200, AUTHED()), () => res(200, AUTHED())];
    renderGuard();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("Reconnecting to server...")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText("app content")).toBeInTheDocument();
    expect(checks).toHaveLength(3);
  });

  it("a hung probe is abandoned after 8 s and retried with backoff", async () => {
    vi.useFakeTimers();
    checkQueue = [() => { throw new TypeError("Failed to fetch"); }, hang, hang];
    renderGuard();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(checks).toHaveLength(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(checks[1].signal.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(checks).toHaveLength(3);
  });
});
