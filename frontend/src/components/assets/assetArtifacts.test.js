// claude.ai artifact rows (kind "artifact") in the pure Assets helpers.
// Kept out of assetMedia.test.js / assetFilters.test.js on purpose. Every URL
// and id below is synthetic.
import { describe, it, expect } from "vitest";
import {
  ARTIFACT_LABELS,
  TYPE_TABS,
  assetArtifact,
  displayName,
  extBadge,
  filterAssets,
  matchesQuery,
  sortAssets,
  typeKeyOf,
} from "./assetFilters";
import {
  chatLinkFor,
  isFileAsset,
  isLightboxAsset,
  openActionFor,
  tileThumbUrl,
} from "./assetMedia";

const SHORT_URL = "https://claude.ai/artifact/TestArtifactAAAAAAAAAA";
const CODE_URL = "https://claude.ai/code/artifact/00000000-0000-4000-8000-000000000001";

function art(extra = {}) {
  return {
    url: SHORT_URL,
    kind: "slides",
    type_name: "Slides",
    title: "Quarterly review",
    description: "Synthetic deck for tests",
    version: 5,
    publish_count: 6,
    first_published_at: "2026-10-01T00:00:00Z",
    last_published_at: "2026-10-02T00:00:00Z",
    published_by_agent_id: "ag1",
    published_by_agent_name: "Deck Agent",
    deleted: false,
    ...extra,
  };
}

function artifactRow(extra = {}, artifact = art()) {
  return {
    path: `artifact:${SHORT_URL}`,
    name: artifact?.title || SHORT_URL,
    dir: "",
    kind: "artifact",
    ext: "",
    sources: ["artifact", "text"],
    tier: "delivered",
    first_seen_at: "2026-10-01T00:00:00Z",
    last_seen_at: "2026-10-02T00:00:00Z",
    mention_count: 2,
    last_agent_id: "ag1",
    last_agent_name: "Deck Agent",
    last_message_id: "m1",
    exists: true,
    size: null,
    mtime: null,
    starred: false,
    hidden: false,
    note: null,
    webapp: null,
    artifact,
    ...extra,
  };
}

function fileRow(path, extra = {}) {
  const name = path.split("/").pop();
  return {
    path, name, dir: "", kind: "image", ext: name.split(".").pop(),
    sources: ["text"], tier: "delivered",
    first_seen_at: "2026-10-01T00:00:00", last_seen_at: "2026-10-01T00:00:00",
    mention_count: 1, exists: true, size: 100, mtime: 1,
    starred: false, hidden: false, note: null, webapp: null, artifact: null,
    ...extra,
  };
}

describe("assetArtifact", () => {
  it("passes the server object through", () => {
    const a = art();
    expect(assetArtifact(artifactRow({}, a))).toBe(a);
  });
  it("rebuilds a minimal descriptor from the path when artifact is null", () => {
    expect(assetArtifact({ path: `artifact:${SHORT_URL}`, artifact: null }))
      .toEqual({ url: SHORT_URL, kind: "other", title: null });
    expect(assetArtifact({ path: `artifact:${CODE_URL}`, artifact: null }))
      .toEqual({ url: CODE_URL, kind: "other", title: null });
  });
  it("is null for files, webapps and junk artifact: paths", () => {
    expect(assetArtifact(fileRow("out/a.png"))).toBeNull();
    expect(assetArtifact({ path: `webapp:url:${SHORT_URL}`, webapp: null })).toBeNull();
    expect(assetArtifact({ path: "artifact:javascript:x" })).toBeNull();
    expect(assetArtifact({ path: "artifact:https://example.com/a" })).toBeNull();
    expect(assetArtifact({ path: `artifact:${SHORT_URL}?x=1` })).toBeNull();
    expect(assetArtifact(null)).toBeNull();
  });
});

describe("displayName / extBadge", () => {
  it("uses the artifact title, else Untitled artifact", () => {
    expect(displayName(artifactRow())).toBe("Quarterly review");
    expect(displayName(artifactRow({}, art({ title: null })))).toBe("Untitled artifact");
    expect(displayName({ path: `artifact:${CODE_URL}`, name: CODE_URL, artifact: null }))
      .toBe("Untitled artifact");
  });
  it("badges by artifact type, unknown types as ARTIFACT", () => {
    const badge = (kind) => extBadge(artifactRow({}, art({ kind })));
    expect(badge("slides")).toBe("SLIDES");
    expect(badge("doc")).toBe("DOC");
    expect(badge("page")).toBe("PAGE");
    expect(badge("dashboard")).toBe("DASHBOARD");
    expect(badge("other")).toBe("ARTIFACT");
    expect(badge("something-new")).toBe("ARTIFACT");
    expect(extBadge({ path: `artifact:${SHORT_URL}`, artifact: null })).toBe("ARTIFACT");
    expect(ARTIFACT_LABELS.other).toBe("Artifact");
  });
});

describe("matchesQuery", () => {
  const row = artifactRow();
  it("matches title, url, type, description and publisher", () => {
    expect(matchesQuery(row, "quarterly")).toBe(true);
    expect(matchesQuery(row, "TestArtifactAAAA")).toBe(true);
    expect(matchesQuery(row, "slides")).toBe(true);
    expect(matchesQuery(row, "synthetic deck")).toBe(true);
    expect(matchesQuery(row, "deck agent")).toBe(true);
    expect(matchesQuery(row, "quarterly nope")).toBe(false);
  });
});

describe("type chip (Artifacts chip not approved → Web)", () => {
  it("counts artifacts under the Web chip", () => {
    expect(typeKeyOf({ kind: "artifact" })).toBe("web");
    expect(TYPE_TABS.some((t) => t.key === "artifact")).toBe(false);
  });
  it("filterAssets counts and lists artifacts with web", () => {
    const rows = [
      fileRow("out/a.png"),
      fileRow("site/index.html", { kind: "html", ext: "html" }),
      artifactRow(),
    ];
    const all = filterAssets(rows, { type: "all" });
    expect(all.counts).toMatchObject({ all: 3, image: 1, web: 2 });
    const web = filterAssets(rows, { type: "web" });
    expect(web.list.map((a) => a.kind).sort()).toEqual(["artifact", "html"]);
  });
  it("hides a deleted artifact unless Missing is on or it is starred", () => {
    const gone = artifactRow({ exists: false }, art({ deleted: true }));
    expect(filterAssets([gone], {}).list).toHaveLength(0);
    expect(filterAssets([gone], { showMissing: true }).list).toHaveLength(1);
    expect(filterAssets([{ ...gone, starred: true }], {}).list).toHaveLength(1);
  });
});

describe("sortAssets", () => {
  it("sorts artifacts by title and puts them last by size", () => {
    const rows = [artifactRow(), fileRow("out/zz.png", { size: 5 })];
    expect(sortAssets(rows, "name").map(displayName)).toEqual(["Quarterly review", "zz.png"]);
    expect(sortAssets(rows, "size").map((a) => a.kind)).toEqual(["image", "artifact"]);
  });
});

describe("assetMedia for artifacts", () => {
  it("is never a file: no thumbnail, no lightbox", () => {
    expect(isFileAsset(artifactRow())).toBe(false);
    expect(isFileAsset({ path: `artifact:${SHORT_URL}`, kind: "artifact", artifact: null })).toBe(false);
    expect(tileThumbUrl("proj", artifactRow())).toBeNull();
    expect(isLightboxAsset(artifactRow())).toBe(false);
    // Files are unaffected
    expect(isFileAsset(fileRow("out/a.png"))).toBe(true);
  });
  it("opens as artifact; details when deleted or without a url", () => {
    expect(openActionFor(artifactRow())).toBe("artifact");
    expect(openActionFor({ path: `artifact:${CODE_URL}`, kind: "artifact", artifact: null, exists: true }))
      .toBe("artifact");
    expect(openActionFor(artifactRow({ exists: false }, art({ deleted: true })))).toBe("details");
    expect(openActionFor(artifactRow({}, art({ url: null })))).toBe("details");
  });
  it("chatLinkFor uses the backend's chosen ref", () => {
    expect(chatLinkFor(artifactRow())).toBe("/agents/ag1?focus=m1");
  });
});
