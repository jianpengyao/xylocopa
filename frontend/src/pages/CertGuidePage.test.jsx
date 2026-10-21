import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import CertGuidePage from "./CertGuidePage";

const res = (status, body = {}, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: new Headers(headers),
  json: async () => body,
});

let calls;
let regenAnswer;
beforeEach(() => {
  calls = [];
  localStorage.clear();
  vi.stubGlobal("fetch", vi.fn((url, opts = {}) => {
    calls.push({ url, opts });
    if (url === "/api/cert/info") return Promise.resolve(res(200, { ips: ["127.0.0.1"] }));
    if (url === "/api/cert/regenerate") return Promise.resolve(regenAnswer());
    return Promise.resolve(res(200, {}));
  }));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

async function regenerate(ip = "100.64.0.10") {
  render(<CertGuidePage />);
  // The first textbox is the IP to add to the certificate.
  fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: ip } });
  fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));
}
const regenCall = () => calls.find((c) => c.url === "/api/cert/regenerate");

describe("CertGuidePage regenerate", () => {
  it("sends the stored session as Bearer", async () => {
    localStorage.setItem("cc-auth-token", "xys_me");
    regenAnswer = () => res(200, { skipped: true });
    await regenerate();
    expect(await screen.findByText("Already covered — no regen needed.")).toBeInTheDocument();
    const call = regenCall();
    expect(call.opts.method).toBe("POST");
    expect(call.opts.headers.Authorization).toBe("Bearer xys_me");
    expect(call.opts.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(call.opts.body)).toEqual({ ips: ["100.64.0.10"], dns: ["xylocopa", "localhost"] });
  });

  it("a 401 is shown inline with a sign-in link; nothing is cleared or dispatched", async () => {
    localStorage.setItem("cc-auth-token", "xys_stale");
    const expired = vi.fn();
    window.addEventListener("auth-expired", expired);
    regenAnswer = () => res(401, { detail: "Not authenticated" });
    await regenerate();
    expect(await screen.findByText(/Sign in first to regenerate the certificate/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_stale");
    expect(expired).not.toHaveBeenCalled();
    window.removeEventListener("auth-expired", expired);
  });

  it("even a session 401 (X-Auth-Reason) stays inline on this public page", async () => {
    localStorage.setItem("cc-auth-token", "xys_revoked");
    const expired = vi.fn();
    window.addEventListener("auth-expired", expired);
    regenAnswer = () => res(401, { detail: "Session revoked" }, { "X-Auth-Reason": "revoked" });
    await regenerate();
    expect(await screen.findByText(/Sign in first to regenerate the certificate/)).toBeInTheDocument();
    expect(localStorage.getItem("cc-auth-token")).toBe("xys_revoked");
    expect(expired).not.toHaveBeenCalled();
    window.removeEventListener("auth-expired", expired);
  });

  it("without a stored session it still asks (DISABLE_AUTH needs none)", async () => {
    regenAnswer = () => res(200, { skipped: true });
    await regenerate();
    await waitFor(() => expect(regenCall()).toBeTruthy());
    expect(regenCall().opts.headers.Authorization).toBeUndefined();
    expect(await screen.findByText("Already covered — no regen needed.")).toBeInTheDocument();
  });

  it("other errors show the server's detail", async () => {
    regenAnswer = () => res(400, { detail: "Invalid IP address" });
    await regenerate();
    expect(await screen.findByText("Invalid IP address")).toBeInTheDocument();
  });
});
