import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../../contexts/ToastContext";

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal()),
  fetchProjectAssets: vi.fn(),
  updateAssetMarks: vi.fn(),
}));

let wsHandler = null;
vi.mock("../../hooks/useWebSocket", () => ({
  useWsEvent: (h) => { wsHandler = h; },
}));
vi.mock("../../contexts/WebSocketContext", () => ({
  useWebSocketContext: () => ({ onOpen: () => () => {}, subscribe: () => () => {} }),
}));
vi.mock("../../hooks/usePageVisible", () => ({ default: () => true }));

import { fetchProjectAssets, updateAssetMarks } from "../../lib/api";
import AssetsSection from "./AssetsSection";
import { assetsCache } from "./assetsCache";

function asset(path, extra = {}) {
  const name = path.split("/").pop();
  return {
    path, name, dir: "", kind: "image", ext: name.split(".").pop(),
    sources: ["text"], tier: "delivered",
    first_seen_at: "2026-10-01T00:00:00", last_seen_at: "2026-10-01T00:00:00",
    mention_count: 1, last_agent_id: "ag1", last_agent_name: "Agent", last_message_id: "m1",
    exists: true, size: 10, mtime: 1, starred: false, hidden: false, note: null, webapp: null,
    ...extra,
  };
}

const ASSETS = [
  asset("out/a.png", { last_seen_at: "2026-10-03T00:00:00" }),
  asset("out/b.png", { last_seen_at: "2026-10-02T00:00:00" }),
  asset("in/read.png", { tier: "seen", sources: ["read"] }),
];

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function renderSection() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <AssetsSection projectName="proj" visible />
      </ToastProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  assetsCache.clear();
  wsHandler = null;
  fetchProjectAssets.mockResolvedValue({ assets: ASSETS, indexed_at: "2026-10-03T00:00:00" });
});

describe("AssetsSection", () => {
  it("fetches and hides seen-tier assets by default", async () => {
    renderSection();
    await screen.findByRole("button", { name: "a.png" });
    expect(fetchProjectAssets).toHaveBeenCalledWith("proj", { includeMissing: false });
    expect(screen.getByRole("button", { name: "b.png" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "read.png" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Seen by agent/ }));
    expect(await screen.findByRole("button", { name: "read.png" })).toBeInTheDocument();
    expect(localStorage.getItem("draft:ui:project:proj:assets:seen")).toBe("true");
  });

  it("refetches with include_missing when Missing is toggled", async () => {
    renderSection();
    await screen.findByRole("button", { name: "a.png" });
    fireEvent.click(screen.getByRole("button", { name: /Missing/ }));
    await waitFor(() => expect(fetchProjectAssets).toHaveBeenCalledWith("proj", { includeMissing: true }));
  });

  it("stars optimistically and posts one mark", async () => {
    let resolve;
    updateAssetMarks.mockReturnValue(new Promise((r) => { resolve = r; }));
    renderSection();
    const tile = await screen.findByRole("button", { name: "a.png" });
    fireEvent.click(within(tile).getByRole("button", { name: "Star asset" }));
    expect(within(tile).getByRole("button", { name: "Unstar asset" })).toBeInTheDocument();
    expect(updateAssetMarks).toHaveBeenCalledWith("proj", { paths: ["out/a.png"], starred: true });
    await act(async () => {
      resolve({ marks: { "out/a.png": { starred: true, hidden: false, note: null } } });
    });
    expect(within(tile).getByRole("button", { name: "Unstar asset" })).toBeInTheDocument();
  });

  it("rolls back when the mark request fails", async () => {
    updateAssetMarks.mockRejectedValue(new Error("boom"));
    renderSection();
    const tile = await screen.findByRole("button", { name: "a.png" });
    fireEvent.click(within(tile).getByRole("button", { name: "Star asset" }));
    await waitFor(() => expect(within(tile).getByRole("button", { name: "Star asset" })).toBeInTheDocument());
    expect(await screen.findByText(/Couldn't update asset: boom/)).toBeInTheDocument();
  });

  it("bulk-stars the selection with a single request", async () => {
    updateAssetMarks.mockResolvedValue({
      marks: {
        "out/a.png": { starred: true, hidden: false, note: null },
        "out/b.png": { starred: true, hidden: false, note: null },
      },
    });
    renderSection();
    await screen.findByRole("button", { name: "a.png" });
    fireEvent.click(screen.getByRole("button", { name: "Select" }));
    for (const name of ["a.png", "b.png"]) {
      const tile = screen.getByRole("button", { name });
      fireEvent.pointerDown(tile);
      fireEvent.pointerUp(tile);
      fireEvent.click(tile);
    }
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Star 2" }));
    await waitFor(() => expect(updateAssetMarks).toHaveBeenCalledTimes(1));
    const [, body] = updateAssetMarks.mock.calls[0];
    expect(body.starred).toBe(true);
    expect([...body.paths].sort()).toEqual(["out/a.png", "out/b.png"]);
    // Selection mode exits after success
    await waitFor(() => expect(screen.queryByText("2 selected")).not.toBeInTheDocument());
  });

  it("patches marks from asset_marks_changed for this project only", async () => {
    renderSection();
    const tile = await screen.findByRole("button", { name: "b.png" });
    act(() => {
      wsHandler({ type: "asset_marks_changed", data: { project: "other", paths: ["out/b.png"], starred: true } });
    });
    expect(within(tile).getByRole("button", { name: "Star asset" })).toBeInTheDocument();
    act(() => {
      wsHandler({ type: "asset_marks_changed", data: { project: "proj", paths: ["out/b.png"], starred: true } });
    });
    expect(within(tile).getByRole("button", { name: "Unstar asset" })).toBeInTheDocument();
  });

  it("clears a note when asset_marks_changed carries note: null", async () => {
    fetchProjectAssets.mockResolvedValue({
      assets: [asset("out/n.png", { note: "keep me" })],
      indexed_at: null,
    });
    renderSection();
    await screen.findByRole("button", { name: "n.png" });
    fireEvent.click(screen.getByRole("button", { name: "Details for n.png" }));
    expect(await screen.findByDisplayValue("keep me")).toBeInTheDocument();
    // starred-only event leaves the note alone
    act(() => {
      wsHandler({ type: "asset_marks_changed", data: { project: "proj", paths: ["out/n.png"], starred: true } });
    });
    expect(screen.getByDisplayValue("keep me")).toBeInTheDocument();
    act(() => {
      wsHandler({ type: "asset_marks_changed", data: { project: "proj", paths: ["out/n.png"], note: null } });
    });
    await waitFor(() => expect(screen.queryByDisplayValue("keep me")).not.toBeInTheDocument());
  });

  it("opens a tile on click, not on pointerup (touch ghost click)", async () => {
    renderSection();
    const tile = await screen.findByRole("button", { name: "a.png" });
    const overlay = () => document.querySelector(".fixed.inset-0");
    // On touch the browser's compatibility click follows pointerup by ~5ms
    // and hit-tests whatever pointerup mounted — so nothing may open there.
    fireEvent.pointerDown(tile);
    fireEvent.pointerUp(tile);
    expect(overlay()).toBeNull();
    fireEvent.click(tile);
    await waitFor(() => expect(overlay()).not.toBeNull());
    await act(() => new Promise((r) => setTimeout(r, 500)));
    expect(overlay()).not.toBeNull();
  });

  it("long-press selects and swallows the trailing click", async () => {
    renderSection();
    const tile = await screen.findByRole("button", { name: "a.png" });
    fireEvent.pointerDown(tile);
    await act(() => new Promise((r) => setTimeout(r, 550)));
    fireEvent.pointerUp(tile);
    fireEvent.click(tile);
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    expect(document.querySelector(".fixed.inset-0")).toBeNull();
    // The next tap is a normal toggle again
    fireEvent.pointerDown(tile);
    fireEvent.pointerUp(tile);
    fireEvent.click(tile);
    expect(screen.getByText("Select assets")).toBeInTheDocument();
  });

  it("overlapping mark failures roll back to the pre-burst state", async () => {
    renderSection();
    const tile = await screen.findByRole("button", { name: "a.png" });
    const dA = deferred();
    const dB = deferred();
    updateAssetMarks.mockReturnValueOnce(dA.promise).mockReturnValueOnce(dB.promise);
    fireEvent.click(within(tile).getByRole("button", { name: "Star asset" }));
    fireEvent.click(screen.getByRole("button", { name: "Details for a.png" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Hide" }));
    expect(updateAssetMarks).toHaveBeenCalledTimes(2);
    // A fails first (B still pending), then B fails
    await act(async () => { dA.reject(new Error("offline")); });
    await act(async () => { dB.reject(new Error("offline")); });
    expect(within(dialog).getByRole("button", { name: "Star" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Hide" })).toBeInTheDocument();
  });

  it("a failed overlapping mark keeps what an earlier request stored", async () => {
    renderSection();
    const tile = await screen.findByRole("button", { name: "a.png" });
    const dA = deferred();
    const dB = deferred();
    updateAssetMarks.mockReturnValueOnce(dA.promise).mockReturnValueOnce(dB.promise);
    fireEvent.click(within(tile).getByRole("button", { name: "Star asset" }));
    fireEvent.click(screen.getByRole("button", { name: "Details for a.png" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Hide" }));
    await act(async () => {
      dA.resolve({ marks: { "out/a.png": { starred: true, hidden: false, note: null } } });
    });
    await act(async () => { dB.reject(new Error("offline")); });
    expect(within(dialog).getByRole("button", { name: "Starred" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Hide" })).toBeInTheDocument();
  });

  it("explains an empty default view whose assets are all seen-tier", async () => {
    fetchProjectAssets.mockResolvedValue({
      assets: [
        asset("in/r1.png", { tier: "seen", sources: ["read"] }),
        asset("in/r2.png", { tier: "seen", sources: ["read"] }),
      ],
      indexed_at: null,
    });
    renderSection();
    expect(await screen.findByText("No assets to show")).toBeInTheDocument();
    expect(screen.getByText("2 files only read by the agent")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show seen by agent" }));
    expect(await screen.findByRole("button", { name: "r1.png" })).toBeInTheDocument();
  });

  it("shows the empty state when the project has no assets", async () => {
    fetchProjectAssets.mockResolvedValue({ assets: [], indexed_at: null });
    renderSection();
    expect(await screen.findByText("No assets yet")).toBeInTheDocument();
  });
});
