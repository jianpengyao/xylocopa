// claude.ai artifact link cards in the Assets grid and detail sheet. Mock
// block and render helper mirror AssetsSection.test.jsx; every URL and id
// below is synthetic.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { ToastProvider } from "../../contexts/ToastContext";

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal()),
  fetchProjectAssets: vi.fn(),
  updateAssetMarks: vi.fn(),
}));

vi.mock("../../hooks/useWebSocket", () => ({
  useWsEvent: () => {},
}));
vi.mock("../../contexts/WebSocketContext", () => ({
  useWebSocketContext: () => ({ onOpen: () => () => {}, subscribe: () => () => {} }),
}));
vi.mock("../../hooks/usePageVisible", () => ({ default: () => true }));

import { fetchProjectAssets } from "../../lib/api";
import AssetsSection from "./AssetsSection";
import { assetsCache } from "./assetsCache";

const SHORT_URL = "https://claude.ai/artifact/TestArtifactAAAAAAAAAA";
const CODE_URL = "https://claude.ai/code/artifact/00000000-0000-4000-8000-000000000001";
const HOUR = 3600 * 1000;
// 2h05m ago: reads "2h ago" however long the test takes.
const PUBLISHED_AT = new Date(Date.now() - 2 * HOUR - 5 * 60 * 1000).toISOString();

function image(path, extra = {}) {
  const name = path.split("/").pop();
  return {
    path, name, dir: "", kind: "image", ext: name.split(".").pop(),
    sources: ["text"], tier: "delivered",
    first_seen_at: "2026-10-01T00:00:00", last_seen_at: "2026-10-01T00:00:00",
    mention_count: 1, last_agent_id: "ag1", last_agent_name: "Agent", last_message_id: "m1",
    exists: true, size: 10, mtime: 1, starred: false, hidden: false, note: null, webapp: null,
    artifact: null,
    ...extra,
  };
}

function artifact(extra = {}, art = {}) {
  const meta = {
    url: SHORT_URL,
    kind: "slides",
    type_name: "Slides",
    title: "Quarterly review",
    description: "Synthetic deck for tests",
    version: 5,
    publish_count: 6,
    first_published_at: "2026-10-01T00:00:00Z",
    last_published_at: PUBLISHED_AT,
    published_by_agent_id: "ag1",
    published_by_agent_name: "Deck Agent",
    deleted: false,
    ...art,
  };
  return {
    path: `artifact:${SHORT_URL}`, name: meta.title || SHORT_URL, dir: "", kind: "artifact", ext: "",
    sources: ["artifact", "text"], tier: "delivered",
    first_seen_at: "2026-10-01T00:00:00Z", last_seen_at: PUBLISHED_AT,
    mention_count: 3, last_agent_id: "ag1", last_agent_name: "Deck Agent", last_message_id: "m1",
    exists: true, size: null, mtime: null, starred: false, hidden: false, note: null, webapp: null,
    artifact: meta,
    ...extra,
  };
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{`${loc.pathname}${loc.search}`}</div>;
}

function renderSection() {
  return render(
    <MemoryRouter initialEntries={["/projects/proj"]}>
      <ToastProvider>
        <Routes>
          <Route path="/projects/:name" element={<AssetsSection projectName="proj" visible />} />
          <Route path="/agents/:id" element={<LocationProbe />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  );
}

const overlay = () => document.querySelector(".fixed.inset-0");
let openSpy;

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  assetsCache.clear();
  openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
  fetchProjectAssets.mockResolvedValue({
    assets: [image("out/a.png"), artifact()],
    indexed_at: "2026-10-03T00:00:00",
  });
});

afterEach(() => {
  openSpy.mockRestore();
});

describe("AssetsSection artifacts", () => {
  it("renders a link card: title, type, relative time and publisher", async () => {
    renderSection();
    const tile = await screen.findByRole("button", { name: "Quarterly review" });
    expect(within(tile).getByText("SLIDES")).toBeInTheDocument();
    expect(within(tile).getByText("2h ago")).toBeInTheDocument();
    expect(within(tile).getByText("Deck Agent")).toBeInTheDocument();
    expect(tile).toHaveAttribute("title", SHORT_URL);
    expect(tile.querySelector("img")).toBeNull();
    // Caption is the title, hovering it shows the link too
    const caption = screen.getAllByText("Quarterly review").find((el) => el.tagName === "P");
    expect(caption).toHaveAttribute("title", SHORT_URL);
  });

  it("tap opens the artifact in a new tab and mounts no overlay", async () => {
    renderSection();
    const tile = await screen.findByRole("button", { name: "Quarterly review" });
    fireEvent.pointerDown(tile);
    fireEvent.pointerUp(tile);
    fireEvent.click(tile);
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(openSpy).toHaveBeenCalledWith(SHORT_URL, "_blank", "noopener,noreferrer");
    expect(overlay()).toBeNull();
  });

  it("detail sheet: Open artifact link, Copy link, publish rows, no file rows", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    try {
      renderSection();
      await screen.findByRole("button", { name: "Quarterly review" });
      fireEvent.click(screen.getByRole("button", { name: "Details for Quarterly review" }));
      const dialog = await screen.findByRole("dialog");

      expect(within(dialog).getByText("Artifact")).toBeInTheDocument();
      expect(within(dialog).getByText("SLIDES")).toBeInTheDocument();
      expect(within(dialog).getByText("Synthetic deck for tests")).toBeInTheDocument();
      expect(within(dialog).getByText(SHORT_URL)).toBeInTheDocument();

      const link = within(dialog).getByRole("link", { name: /Open artifact/ });
      expect(link).toHaveAttribute("href", SHORT_URL);
      expect(link).toHaveAttribute("target", "_blank");
      expect(link.getAttribute("rel")).toContain("noopener");

      fireEvent.click(within(dialog).getByRole("button", { name: "Copy link" }));
      expect(writeText).toHaveBeenCalledWith(SHORT_URL);

      expect(within(dialog).getByText("First published")).toBeInTheDocument();
      expect(within(dialog).getByText("Last published")).toBeInTheDocument();
      expect(within(dialog).getByText("Published by")).toBeInTheDocument();
      expect(within(dialog).getAllByText("Deck Agent").length).toBeGreaterThan(0);
      expect(within(dialog).getByText("Version")).toBeInTheDocument();
      expect(within(dialog).getByText("5")).toBeInTheDocument();
      expect(within(dialog).getByText("Published · Mentioned")).toBeInTheDocument();
      for (const gone of ["Created", "Size", "Modified", "First seen", "Last seen", "Last agent"]) {
        expect(within(dialog).queryByText(gone)).toBeNull();
      }
      expect(within(dialog).queryByRole("button", { name: "Download" })).toBeNull();
      expect(within(dialog).queryByRole("button", { name: "Copy path" })).toBeNull();

      // Following the anchor leaves the sheet open (and never goes through
      // window.open). preventDefault keeps jsdom from attempting navigation.
      const stop = (e) => e.preventDefault();
      document.addEventListener("click", stop);
      try {
        fireEvent.click(link);
      } finally {
        document.removeEventListener("click", stop);
      }
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(openSpy).not.toHaveBeenCalled();

      fireEvent.click(within(dialog).getByRole("button", { name: /Open in chat/ }));
      expect(await screen.findByTestId("location")).toHaveTextContent("/agents/ag1?focus=m1");
    } finally {
      delete navigator.clipboard;
    }
  });

  it("counts artifacts under the Web chip", async () => {
    fetchProjectAssets.mockResolvedValue({ assets: [image("out/a.png")], indexed_at: null });
    const { unmount } = renderSection();
    await screen.findByRole("button", { name: "a.png" });
    expect(screen.queryByRole("button", { name: /^Web/ })).toBeNull();
    unmount();
    assetsCache.clear();

    fetchProjectAssets.mockResolvedValue({
      assets: [image("out/a.png"), artifact()],
      indexed_at: null,
    });
    renderSection();
    await screen.findByRole("button", { name: "Quarterly review" });
    expect(screen.queryByRole("button", { name: /^Artifacts/ })).toBeNull();
    const web = screen.getByRole("button", { name: /^Web/ });
    expect(web).toHaveTextContent("Web1");
    fireEvent.click(web);
    await waitFor(() => expect(screen.queryByRole("button", { name: "a.png" })).toBeNull());
    expect(screen.getByRole("button", { name: "Quarterly review" })).toBeInTheDocument();
  });

  it("filters by type and publisher", async () => {
    renderSection();
    await screen.findByRole("button", { name: "Quarterly review" });
    const search = screen.getByRole("searchbox", { name: "Filter assets by name or path" });
    fireEvent.change(search, { target: { value: "deck agent" } });
    await waitFor(() => expect(screen.queryByRole("button", { name: "a.png" })).toBeNull());
    expect(screen.getByRole("button", { name: "Quarterly review" })).toBeInTheDocument();
  });

  it("a deleted (starred) artifact shows deleted and opens details", async () => {
    fetchProjectAssets.mockResolvedValue({
      assets: [artifact({ exists: false, starred: true }, { deleted: true })],
      indexed_at: null,
    });
    renderSection();
    const tile = await screen.findByRole("button", { name: "Quarterly review" });
    expect(within(tile).getByText("deleted")).toBeInTheDocument();
    expect(within(tile).queryByText("2h ago")).toBeNull();
    fireEvent.click(tile);
    const dialog = await screen.findByRole("dialog");
    expect(openSpy).not.toHaveBeenCalled();
    expect(within(dialog).getByText("Deleted")).toBeInTheDocument();
    expect(within(dialog).queryByRole("link", { name: /Open artifact/ })).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Copy link" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /Open in chat/ })).toBeInTheDocument();
  });

  it("an artifact with no publish times keeps the First seen / Last seen rows", async () => {
    // A ref-less mark in a project that never published it: key-only object.
    fetchProjectAssets.mockResolvedValue({
      assets: [artifact(
        { sources: [], starred: true, mention_count: 0, last_agent_id: null,
          last_agent_name: null, last_message_id: null,
          first_seen_at: "2026-10-01T00:00:00Z", last_seen_at: "2026-10-01T00:00:00Z" },
        { kind: "other", type_name: null, title: null, description: null, version: null,
          publish_count: 0, first_published_at: null, last_published_at: null,
          published_by_agent_id: null, published_by_agent_name: null },
      )],
      indexed_at: null,
    });
    renderSection();
    await screen.findByRole("button", { name: "Untitled artifact" });
    fireEvent.click(screen.getByRole("button", { name: "Details for Untitled artifact" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("First seen")).toBeInTheDocument();
    expect(within(dialog).getByText("Last seen")).toBeInTheDocument();
    for (const gone of ["First published", "Last published", "Published by", "Version"]) {
      expect(within(dialog).queryByText(gone)).toBeNull();
    }
    expect(within(dialog).getByRole("link", { name: /Open artifact/ })).toHaveAttribute("href", SHORT_URL);
  });

  it("a row with artifact: null still opens its link from the path", async () => {
    fetchProjectAssets.mockResolvedValue({
      assets: [artifact({ path: `artifact:${CODE_URL}`, name: CODE_URL, artifact: null })],
      indexed_at: null,
    });
    renderSection();
    const tile = await screen.findByRole("button", { name: "Untitled artifact" });
    expect(within(tile).getByText("ARTIFACT")).toBeInTheDocument();
    fireEvent.click(tile);
    expect(openSpy).toHaveBeenCalledWith(CODE_URL, "_blank", "noopener,noreferrer");
  });
});
