import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  tileThumbUrl,
  lightboxItem,
  isLightboxAsset,
  openActionFor,
  chatLinkFor,
  isFileAsset,
} from "./assetMedia";

const TOKEN_KEY = "cc-auth-token";

function asset(path, extra = {}) {
  const name = path.split("/").pop();
  return {
    path,
    name,
    kind: "image",
    ext: name.split(".").pop(),
    exists: true,
    mtime: 1700000000.5,
    webapp: null,
    last_agent_id: null,
    last_message_id: null,
    ...extra,
  };
}

describe("tileThumbUrl", () => {
  beforeEach(() => localStorage.removeItem(TOKEN_KEY));
  afterEach(() => localStorage.removeItem(TOKEN_KEY));

  it("asks for the 320px variant with an mtime cache-bust", () => {
    const url = tileThumbUrl("proj", asset("out/a b.png"));
    expect(url).toBe("/api/thumbs/proj/out/a%20b.png?w=320&_v=1700000000.5");
  });

  it("keeps the auth token intact", () => {
    localStorage.setItem(TOKEN_KEY, "tok");
    const url = tileThumbUrl("proj", asset("out/a.png"));
    expect(url).toBe("/api/thumbs/proj/out/a.png?token=tok&w=320&_v=1700000000.5");
  });

  it("uses the file itself for svg", () => {
    expect(tileThumbUrl("proj", asset("x/logo.svg"))).toBe("/api/files/proj/x/logo.svg?_v=1700000000.5");
  });

  it("uses the poster thumb for video, never the mp4", () => {
    const url = tileThumbUrl("proj", asset("v/clip.mp4", { kind: "video", ext: "mp4" }));
    expect(url.startsWith("/api/thumbs/proj/v/clip.mp4?w=320")).toBe(true);
  });

  it("returns null for icon tiles, missing files and non-file webapps", () => {
    expect(tileThumbUrl("proj", asset("d.pdf", { kind: "pdf", ext: "pdf" }))).toBe(null);
    expect(tileThumbUrl("proj", asset("a.png", { exists: false }))).toBe(null);
    expect(tileThumbUrl("proj", asset("webapp:port:1", { kind: "webapp", ext: "", webapp: { kind: "port", target: "1" } }))).toBe(null);
  });

  it("omits the cache-bust when mtime is unknown", () => {
    expect(tileThumbUrl("proj", asset("a.png", { mtime: null }))).toBe("/api/thumbs/proj/a.png?w=320");
  });
});

describe("lightboxItem", () => {
  it("builds the FilePreview media shape", () => {
    expect(lightboxItem("proj", asset("o/a.png"))).toEqual({
      type: "image", src: "/api/files/proj/o/a.png", thumbSrc: "/api/thumbs/proj/o/a.png", filename: "a.png",
    });
    expect(lightboxItem("proj", asset("o/v.webm", { kind: "video", ext: "webm" }))).toMatchObject({
      type: "video", thumbSrc: "/api/thumbs/proj/o/v.webm",
    });
    expect(lightboxItem("proj", asset("o/i.svg")).thumbSrc).toBeUndefined();
  });

  it("isLightboxAsset only for existing image/video", () => {
    expect(isLightboxAsset(asset("a.png"))).toBe(true);
    expect(isLightboxAsset(asset("a.png", { exists: false }))).toBe(false);
    expect(isLightboxAsset(asset("a.pdf", { kind: "pdf" }))).toBe(false);
  });
});

describe("openActionFor", () => {
  it("maps kinds to actions", () => {
    expect(openActionFor(asset("a.png"))).toBe("lightbox");
    expect(openActionFor(asset("a.mp4", { kind: "video" }))).toBe("lightbox");
    expect(openActionFor(asset("a.html", { kind: "html" }))).toBe("static");
    expect(openActionFor(asset("v/index.html", { kind: "webapp", webapp: { kind: "static", target: "v/index.html" } }))).toBe("static");
    expect(openActionFor(asset("a.pdf", { kind: "pdf" }))).toBe("pdf");
    expect(openActionFor(asset("a.docx", { kind: "office" }))).toBe("download");
    expect(openActionFor(asset("a.ply", { kind: "3d" }))).toBe("download");
  });

  it("handles port / url webapps", () => {
    const port = { kind: "webapp", webapp: { kind: "port", target: "6006" } };
    expect(openActionFor(asset("webapp:port:6006", { ...port, webapp: { ...port.webapp, src: "/api/preview/p/s/proj/6006/" } }))).toBe("port");
    expect(openActionFor(asset("webapp:port:6006", { ...port, last_agent_id: "abc" }))).toBe("port-chat");
    expect(openActionFor(asset("webapp:port:6006", port))).toBe("details");
    expect(openActionFor(asset("webapp:url:x", { kind: "webapp", webapp: { kind: "url", target: "https://x.io" } }))).toBe("url");
  });

  it("missing assets open details", () => {
    expect(openActionFor(asset("a.png", { exists: false }))).toBe("details");
    expect(openActionFor(null)).toBe("details");
  });

  it("treats port / url refs with webapp: null as webapps, not files", () => {
    const port = asset("webapp:port:18998", { kind: "webapp", ext: "", webapp: null });
    expect(isFileAsset(port)).toBe(false);
    expect(tileThumbUrl("proj", port)).toBe(null);
    expect(openActionFor(port)).toBe("details");
    expect(openActionFor({ ...port, last_agent_id: "ag1" })).toBe("port-chat");
    const url = asset("webapp:url:https://x.io/a", { kind: "webapp", ext: "", webapp: null });
    expect(isFileAsset(url)).toBe(false);
    expect(openActionFor(url)).toBe("url");
  });

  it("isFileAsset excludes port / url webapps", () => {
    expect(isFileAsset(asset("a.png"))).toBe(true);
    expect(isFileAsset({ webapp: { kind: "static" } })).toBe(true);
    expect(isFileAsset({ webapp: { kind: "port" } })).toBe(false);
    expect(isFileAsset({ webapp: { kind: "url" } })).toBe(false);
  });
});

describe("chatLinkFor", () => {
  it("links to the focused message when known", () => {
    expect(chatLinkFor(asset("a.png", { last_agent_id: "ag1", last_message_id: "m/1" }))).toBe("/agents/ag1?focus=m%2F1");
    expect(chatLinkFor(asset("a.png", { last_agent_id: "ag1" }))).toBe("/agents/ag1");
    expect(chatLinkFor(asset("a.png"))).toBe(null);
  });
});
