import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal()),
  filesExistsBatch: vi.fn(),
}));

import { act } from "@testing-library/react";
import { filesExistsBatch, setMediaToken } from "../lib/api";
import FileAttachments from "./FilePreview";
import { ToastProvider } from "../contexts/ToastContext";

// Media URLs carry ?token=<media token> (auth step 3). The existence stats
// are keyed without it, and FilePreview must look them up the same way.
describe("FileAttachments with media-token URLs", () => {
  beforeEach(() => {
    filesExistsBatch.mockReset();
    filesExistsBatch.mockImplementation(async (items) => ({ results: items.map(() => ({ exists: false })) }));
  });

  it("shows the missing card for a token-bearing image URL the server reports gone", async () => {
    const attachments = [
      { path: "out/gone.png", resolvedUrl: "/api/files/proj/out/gone.png?token=m.jwt", type: "image", ext: "png" },
    ];
    render(<ToastProvider><FileAttachments attachments={attachments} /></ToastProvider>);
    await waitFor(() => expect(screen.getByText("missing")).toBeInTheDocument());
    expect(filesExistsBatch).toHaveBeenCalledWith([{ project: "proj", path: "out/gone.png" }]);
  });

  it("marks a token-bearing doc row as missing inside a grouped card", async () => {
    const attachments = ["a", "b", "c", "d"].map((n) => (
      { path: `docs/${n}.md`, resolvedUrl: `/api/files/proj/docs/${n}.md?token=m.jwt`, type: "doc", ext: "md" }
    ));
    filesExistsBatch.mockImplementation(async (items) => ({
      results: items.map((it) => ({ exists: it.path !== "docs/c.md" })),
    }));
    render(<ToastProvider><FileAttachments attachments={attachments} /></ToastProvider>);
    await waitFor(() => expect(filesExistsBatch).toHaveBeenCalled());
    // Expand the group, then exactly one row (c.md) renders as missing.
    fireEvent.click(await screen.findByText("4 files referenced"));
    await waitFor(() => expect(screen.getAllByText("missing")).toHaveLength(1));
    expect(screen.getByText("missing").parentElement.textContent).toContain("c.md");
  });
});

// Chat bubbles memoize their attachments (AgentChatPage ChatBubble), so the
// URLs keep the media token of their first render. FileAttachments must
// re-stamp them when AuthGuard re-mints the token, or links, videos and
// downloads die with the old token (~12 h).
describe("FileAttachments after a media-token refresh", () => {
  const exp = () => Date.now() / 1000 + 12 * 3600;
  beforeEach(() => {
    localStorage.clear();
    filesExistsBatch.mockReset();
    filesExistsBatch.mockImplementation(async (items) => ({ results: items.map(() => ({ exists: true })) }));
  });

  it("re-stamps memoized URLs (thumbnail, download, lightbox) with the current token", async () => {
    setMediaToken("m.a", exp());
    const attachments = [{
      path: "out/clip.png", type: "image", ext: "png",
      resolvedUrl: "/api/files/proj/out/clip.png?token=m.a",
      thumbUrl: "/api/thumbs/proj/out/clip.png?token=m.a",
    }, {
      path: "https://example.com/x.png", type: "image", ext: "png",
      resolvedUrl: "https://example.com/x.png",
    }];
    const { container } = render(<ToastProvider><FileAttachments attachments={attachments} /></ToastProvider>);
    const thumb = () => container.querySelector('img[alt="clip.png"]');
    expect(thumb().getAttribute("src")).toBe("/api/thumbs/proj/out/clip.png?token=m.a");

    await waitFor(() => expect(filesExistsBatch).toHaveBeenCalledTimes(1));
    act(() => setMediaToken("m.b", exp()));
    expect(thumb().getAttribute("src")).toBe("/api/thumbs/proj/out/clip.png?token=m.b");
    // Existence stats are keyed without the token: no second query.
    await act(async () => { await Promise.resolve(); });
    expect(filesExistsBatch).toHaveBeenCalledTimes(1);
    const download = container.querySelector('a[download="clip.png"]');
    expect(download.getAttribute("href")).toBe("/api/files/proj/out/clip.png?token=m.b&download=1");
    // Non-media URLs are never given a token.
    expect(container.querySelector('img[alt="x.png"]').getAttribute("src")).toBe("https://example.com/x.png");

    fireEvent.click(thumb());
    const big = await screen.findAllByRole("img");
    expect(big.map((i) => i.getAttribute("src")).filter((u) => u.includes("token=m.a"))).toEqual([]);
  });

  it("leaves URLs alone without a media token (DISABLE_AUTH)", () => {
    const attachments = [{
      path: "out/a.png", type: "image", ext: "png",
      resolvedUrl: "/api/files/proj/out/a.png", thumbUrl: "/api/thumbs/proj/out/a.png?tile=1",
    }];
    const { container } = render(<ToastProvider><FileAttachments attachments={attachments} /></ToastProvider>);
    expect(container.querySelector('img[alt="a.png"]').getAttribute("src")).toBe("/api/thumbs/proj/out/a.png?tile=1");
    expect(container.querySelector('a[download="a.png"]').getAttribute("href")).toBe("/api/files/proj/out/a.png?download=1");
  });
});
