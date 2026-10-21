import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { draftAttachmentForCache, restoreDraftAttachment } from "./draftAttachments";
import { attachmentUrl, uploadUrl } from "./urls";

const setMedia = (token) =>
  localStorage.setItem("cc-media-token", JSON.stringify({ token, exp: Date.now() / 1000 + 3600 }));
const chatUrl = (p) => uploadUrl(p.split("/").pop());

describe("draft attachment cache", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it("persists the upload path and metadata, never a URL", () => {
    setMedia("m.jwt");
    const cached = draftAttachmentForCache({
      id: "a1",
      uploadedPath: "/h/.xylocopa/uploads/ab_shot.png",
      originalName: "shot.png",
      size: 12,
      file: { type: "image/png" },
      previewUrl: "blob:https://x/1",
      uploading: false,
    });
    expect(cached).toEqual({
      id: "a1", uploadedPath: "/h/.xylocopa/uploads/ab_shot.png", originalName: "shot.png", size: 12, mimeType: "image/png",
    });
    expect(JSON.stringify(cached)).not.toContain("token");
  });

  it("rebuilds an image preview from the path with the current media token", () => {
    setMedia("m.now");
    const att = restoreDraftAttachment(
      { id: "a1", uploadedPath: "/h/.xylocopa/uploads/ab_shot.png", mimeType: "image/png" },
      chatUrl,
    );
    expect(att).toMatchObject({ id: "a1", uploading: false, file: null, previewUrl: "/api/uploads/ab_shot.png?token=m.now" });
  });

  it("drops a legacy token-bearing thumbnailUrl and rebuilds it", () => {
    setMedia("m.now");
    const legacy = {
      id: "a1",
      uploadedPath: "/h/xylocopa-projects/p/.xylocopa/uploads/ab_shot.png",
      mimeType: "image/png",
      thumbnailUrl: "/api/files/p/.xylocopa/uploads/ab_shot.png?token=OLD.SESSION.JWT",
    };
    const att = restoreDraftAttachment(legacy, attachmentUrl);
    expect(att.previewUrl).toBe("/api/files/p/.xylocopa/uploads/ab_shot.png?token=m.now");
    expect(att).not.toHaveProperty("thumbnailUrl");
    expect(JSON.stringify(att)).not.toContain("OLD.SESSION");
  });

  it("a legacy entry without a path keeps its URL minus the old token", () => {
    const att = restoreDraftAttachment({ id: "a1", thumbnailUrl: "/api/uploads/x.png?token=OLD" }, chatUrl);
    expect(att.previewUrl).toBe("/api/uploads/x.png");
  });

  it("non-images get no preview", () => {
    const att = restoreDraftAttachment({ id: "a2", uploadedPath: "/h/.xylocopa/uploads/r.pdf", mimeType: "application/pdf" }, chatUrl);
    expect(att.previewUrl).toBeNull();
  });
});
