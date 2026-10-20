import { describe, it, expect } from "vitest";
import {
  TYPE_TABS,
  typeKeyOf,
  parseTs,
  displayName,
  extBadge,
  matchesQuery,
  assetWebapp,
  filterAssets,
  sortAssets,
  normalizeMarkFields,
  patchAssetMarks,
  applyServerMarks,
  snapshotMarks,
} from "./assetFilters";

function asset(path, extra = {}) {
  const name = path.split("/").pop();
  const ext = name.includes(".") ? name.split(".").pop() : "";
  return {
    path,
    name,
    dir: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "",
    kind: "image",
    ext,
    sources: ["text"],
    tier: "delivered",
    first_seen_at: "2026-10-01T00:00:00",
    last_seen_at: "2026-10-01T00:00:00",
    mention_count: 1,
    exists: true,
    size: 100,
    mtime: 1,
    starred: false,
    hidden: false,
    note: null,
    webapp: null,
    ...extra,
  };
}

describe("typeKeyOf", () => {
  it("groups html and webapp under web", () => {
    expect(typeKeyOf({ kind: "html" })).toBe("web");
    expect(typeKeyOf({ kind: "webapp" })).toBe("web");
    expect(typeKeyOf({ kind: "image" })).toBe("image");
    expect(typeKeyOf({ kind: "3d" })).toBe("3d");
  });
  it("every non-all chip key is reachable", () => {
    const kinds = ["image", "video", "html", "pdf", "office", "3d"];
    const keys = new Set(kinds.map((k) => typeKeyOf({ kind: k })));
    for (const t of TYPE_TABS) if (t.key !== "all") expect(keys.has(t.key)).toBe(true);
  });
});

describe("parseTs", () => {
  it("treats naive ISO strings as UTC", () => {
    expect(parseTs("2026-10-01T00:00:00")).toBe(Date.UTC(2026, 9, 1));
    expect(parseTs("2026-10-01T00:00:00Z")).toBe(Date.UTC(2026, 9, 1));
    expect(parseTs("2026-10-01T00:00:00+00:00")).toBe(Date.UTC(2026, 9, 1));
  });
  it("returns 0 for empty / invalid", () => {
    expect(parseTs(null)).toBe(0);
    expect(parseTs("")).toBe(0);
    expect(parseTs("nope")).toBe(0);
  });
});

describe("displayName / extBadge", () => {
  it("uses file name for files", () => {
    const a = asset("out/plot.png");
    expect(displayName(a)).toBe("plot.png");
    expect(extBadge(a)).toBe("PNG");
  });
  it("prefers webapp title, falls back per kind", () => {
    expect(displayName(asset("webapp:port:6006", { kind: "webapp", webapp: { kind: "port", target: "6006", title: "" } }))).toBe("localhost:6006");
    expect(displayName(asset("webapp:url:https://wandb.ai/x", { kind: "webapp", webapp: { kind: "url", target: "https://wandb.ai/x" } }))).toBe("wandb.ai");
    expect(displayName(asset("viewer/index.html", { kind: "webapp", webapp: { kind: "static", target: "viewer/index.html", title: "Viewer" } }))).toBe("Viewer");
  });
  it("badges webapps by kind", () => {
    expect(extBadge({ kind: "webapp", webapp: { kind: "port" } })).toBe("PORT");
    expect(extBadge({ kind: "webapp", webapp: { kind: "url" } })).toBe("URL");
    expect(extBadge({ kind: "webapp", ext: "html", webapp: { kind: "static" } })).toBe("APP");
  });
});

describe("assetWebapp", () => {
  it("returns the server object when present", () => {
    const app = { kind: "static", target: "v/index.html" };
    expect(assetWebapp({ path: "v/index.html", webapp: app })).toBe(app);
    expect(assetWebapp({ path: "a.png", webapp: null })).toBe(null);
  });
  it("rebuilds port / url webapps from the path when webapp is null", () => {
    expect(assetWebapp({ path: "webapp:port:18998", webapp: null })).toEqual({ kind: "port", target: "18998" });
    expect(assetWebapp({ path: "webapp:url:https://x.io/a:b", webapp: null })).toEqual({ kind: "url", target: "https://x.io/a:b" });
    const port = { path: "webapp:port:18998", kind: "webapp", webapp: null };
    expect(displayName(port)).toBe("localhost:18998");
    expect(extBadge(port)).toBe("PORT");
  });
});

describe("matchesQuery", () => {
  const a = asset("results/fig_loss.png", { note: "Final ablation" });
  it("matches path, name and note case-insensitively", () => {
    expect(matchesQuery(a, "")).toBe(true);
    expect(matchesQuery(a, "FIG_")).toBe(true);
    expect(matchesQuery(a, "results/")).toBe(true);
    expect(matchesQuery(a, "ablation")).toBe(true);
    expect(matchesQuery(a, "nothing")).toBe(false);
  });
  it("requires every term", () => {
    expect(matchesQuery(a, "results loss")).toBe(true);
    expect(matchesQuery(a, "results video")).toBe(false);
  });
  it("matches webapp title", () => {
    const w = asset("webapp:port:8080", { kind: "webapp", webapp: { kind: "port", target: "8080", title: "TensorBoard" } });
    expect(matchesQuery(w, "tensor")).toBe(true);
  });
});

describe("filterAssets", () => {
  const all = [
    asset("a.png"),
    asset("seen.png", { tier: "seen" }),
    asset("hidden.png", { hidden: true }),
    asset("star.png", { starred: true }),
    asset("gone.png", { exists: false }),
    asset("gone-star.png", { exists: false, starred: true }),
    asset("clip.mp4", { kind: "video" }),
    asset("page.html", { kind: "html" }),
    asset("webapp:port:1", { kind: "webapp", webapp: { kind: "port", target: "1" } }),
    asset("doc.pdf", { kind: "pdf" }),
    asset("deck.pptx", { kind: "office" }),
    asset("scene.ply", { kind: "3d" }),
  ];
  const paths = (r) => r.list.map((a) => a.path).sort();

  it("defaults hide seen-tier, hidden and missing (non-starred)", () => {
    const r = filterAssets(all, {});
    expect(paths(r)).toEqual([
      "a.png", "clip.mp4", "deck.pptx", "doc.pdf", "gone-star.png", "page.html",
      "scene.ply", "star.png", "webapp:port:1",
    ]);
  });

  it("toggles reveal seen / hidden / missing", () => {
    expect(paths(filterAssets(all, { showSeen: true }))).toContain("seen.png");
    expect(paths(filterAssets(all, { showHidden: true }))).toContain("hidden.png");
    expect(paths(filterAssets(all, { showMissing: true }))).toContain("gone.png");
  });

  it("starredOnly keeps starred, including starred-missing", () => {
    expect(paths(filterAssets(all, { starredOnly: true }))).toEqual(["gone-star.png", "star.png"]);
  });

  it("type filter narrows list but counts are pre-type", () => {
    const r = filterAssets(all, { type: "web" });
    expect(paths(r)).toEqual(["page.html", "webapp:port:1"]);
    expect(r.counts).toMatchObject({ all: 9, image: 3, video: 1, web: 2, pdf: 1, office: 1, "3d": 1 });
  });

  it("counts follow the starred / query filters", () => {
    const r = filterAssets(all, { starredOnly: true });
    expect(r.counts).toMatchObject({ all: 2, image: 2, video: 0, web: 0 });
    const q = filterAssets(all, { query: "clip" });
    expect(q.counts).toMatchObject({ all: 1, video: 1, image: 0 });
  });

  it("sticky paths bypass starred / hidden filters only", () => {
    const sticky = new Set(["hidden.png", "a.png", "seen.png"]);
    const r = filterAssets(all, { starredOnly: true }, sticky);
    expect(paths(r)).toEqual(["a.png", "gone-star.png", "hidden.png", "star.png"]);
  });

  it("excluded counts what the seen / hidden toggles alone would reveal", () => {
    const r = filterAssets([
      ...all,
      asset("seen-hidden.png", { tier: "seen", hidden: true }),
      asset("seen.pdf", { tier: "seen", kind: "pdf" }),
    ], {});
    expect(r.excluded).toEqual({ seen: 2, hidden: 1 });
    // Follows the type filter, and is 0 once the toggle is on
    expect(filterAssets(all, { type: "pdf" }).excluded).toEqual({ seen: 0, hidden: 0 });
    expect(filterAssets(all, { showSeen: true, showHidden: true }).excluded).toEqual({ seen: 0, hidden: 0 });
    // Sticky hidden items are already in the list
    expect(filterAssets(all, {}, new Set(["hidden.png"])).excluded.hidden).toBe(0);
  });

  it("tolerates empty input", () => {
    expect(filterAssets(null, {}).list).toEqual([]);
    expect(filterAssets([], {}).counts.all).toBe(0);
  });
});

describe("sortAssets", () => {
  const list = [
    asset("b/img10.png", { last_seen_at: "2026-10-02T00:00:00", size: 50 }),
    asset("a/img2.png", { last_seen_at: "2026-10-03T00:00:00", size: null }),
    asset("c/Img1.png", { last_seen_at: "2026-10-01T00:00:00Z", size: 500 }),
  ];
  it("recent = last_seen desc", () => {
    expect(sortAssets(list, "recent").map((a) => a.name)).toEqual(["img2.png", "img10.png", "Img1.png"]);
  });
  it("name = natural, case-insensitive", () => {
    expect(sortAssets(list, "name").map((a) => a.name)).toEqual(["Img1.png", "img2.png", "img10.png"]);
  });
  it("size = desc with unknown sizes last", () => {
    expect(sortAssets(list, "size").map((a) => a.name)).toEqual(["Img1.png", "img10.png", "img2.png"]);
  });
  it("does not mutate input", () => {
    const copy = [...list];
    sortAssets(list, "name");
    expect(list).toEqual(copy);
  });
});

describe("mark patching", () => {
  const list = [asset("a.png"), asset("b.png", { starred: true, note: "keep" }), asset("c.png")];

  it("normalizeMarkFields drops unchanged fields and clears empty notes", () => {
    expect(normalizeMarkFields({ starred: true, hidden: null, note: null })).toEqual({ starred: true });
    expect(normalizeMarkFields({ note: "" })).toEqual({ note: null });
    expect(normalizeMarkFields({ note: "  " })).toEqual({ note: null });
    expect(normalizeMarkFields({ note: "hi", hidden: false })).toEqual({ note: "hi", hidden: false });
  });

  it("patchAssetMarks patches only listed paths", () => {
    const next = patchAssetMarks(list, ["a.png", "c.png"], { starred: true });
    expect(next.map((a) => a.starred)).toEqual([true, true, true]);
    expect(next[1]).toBe(list[1]);
    expect(list[0].starred).toBe(false);
  });

  it("patchAssetMarks treats a normalized note: null as a clear", () => {
    const next = patchAssetMarks(list, ["b.png"], normalizeMarkFields({ note: "" }));
    expect(next[1].note).toBe(null);
    expect(next[1].starred).toBe(true);
  });

  it("patchAssetMarks returns the same array when nothing changes", () => {
    expect(patchAssetMarks(list, ["b.png"], { starred: true })).toBe(list);
    expect(patchAssetMarks(list, ["a.png"], {})).toBe(list);
    expect(patchAssetMarks(list, [], { starred: true })).toBe(list);
  });

  it("snapshot + applyServerMarks round-trips (rollback)", () => {
    const snap = snapshotMarks(list, ["a.png", "b.png"]);
    const changed = patchAssetMarks(list, ["a.png", "b.png"], { hidden: true, note: "" });
    expect(changed[1].note).toBe(null);
    const restored = applyServerMarks(changed, snap);
    expect(restored.map((a) => [a.hidden, a.note])).toEqual([[false, null], [false, "keep"], [false, null]]);
  });

  it("applyServerMarks honours onlyPaths", () => {
    const marks = { "a.png": { starred: true, hidden: false, note: null }, "c.png": { starred: true, hidden: false, note: null } };
    const next = applyServerMarks(list, marks, new Set(["a.png"]));
    expect(next.map((a) => a.starred)).toEqual([true, true, false]);
  });
});
