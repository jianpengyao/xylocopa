import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  RE_PROJECTS_PATH, RE_UPLOADS_PATH, fileUrl, fileUrlToThumbUrl, API_FILES_PREFIX, attachmentUrl,
  thumbUrl, uploadUrl, previewUrl, stripAuthParams, withMediaToken, withCurrentMediaToken,
} from "./urls";

describe("RE_PROJECTS_PATH", () => {
  it("extracts project and rel-path for a project-scoped upload", () => {
    const absPath = "/home/x/xylocopa-projects/proj/.xylocopa/uploads/a_b.png";
    const m = absPath.match(RE_PROJECTS_PATH);
    expect(m).not.toBeNull();
    expect(m[1]).toBe("proj");
    expect(m[2]).toBe(".xylocopa/uploads/a_b.png");
  });

  it("extracts project and rel-path for a regular project file", () => {
    const absPath = "/home/x/xylocopa-projects/myproj/src/main.py";
    const m = absPath.match(RE_PROJECTS_PATH);
    expect(m).not.toBeNull();
    expect(m[1]).toBe("myproj");
    expect(m[2]).toBe("src/main.py");
  });

  it("matches legacy agenthive-projects paths", () => {
    const absPath = "/home/x/agenthive-projects/old/.xylocopa/uploads/f.png";
    const m = absPath.match(RE_PROJECTS_PATH);
    expect(m).not.toBeNull();
    expect(m[1]).toBe("old");
    expect(m[2]).toBe(".xylocopa/uploads/f.png");
  });
});

describe("fileUrl for project-scoped uploads", () => {
  it("builds /api/files/<project>/... for a .xylocopa/uploads path", () => {
    const url = fileUrl("proj", ".xylocopa/uploads/a_b.png");
    // fileUrl appends ?token=... when auth is set; strip query to check path
    const path = url.split("?")[0];
    expect(path).toBe("/api/files/proj/.xylocopa/uploads/a_b.png");
  });

  it("produces a URL convertible to a thumb URL", () => {
    const url = fileUrl("proj", ".xylocopa/uploads/a_b.png").split("?")[0];
    const thumb = fileUrlToThumbUrl(url);
    expect(thumb.startsWith("/api/thumbs/")).toBe(true);
  });
});

describe("attachmentUrl", () => {
  it("routes a project-stored attachment to /api/files/<project>/.xylocopa/uploads/...", () => {
    const url = attachmentUrl("/home/x/xylocopa-projects/proj/.xylocopa/uploads/ab12_shot.png");
    expect(url.startsWith("/api/files/proj/.xylocopa/uploads/ab12_shot.png")).toBe(true);
  });

  it("routes a global upload (~/.xylocopa/uploads) to /api/uploads/<name>", () => {
    const url = attachmentUrl("/home/x/.xylocopa/uploads/ab12_shot.png");
    expect(url.startsWith("/api/uploads/ab12_shot.png")).toBe(true);
  });

  it("falls back to /api/uploads for a bare filename", () => {
    expect(attachmentUrl("ab12_shot.png").startsWith("/api/uploads/ab12_shot.png")).toBe(true);
  });
});

describe("media token in media URLs", () => {
  const setMedia = (token, expInS) =>
    localStorage.setItem("cc-media-token", JSON.stringify({ token, exp: Date.now() / 1000 + expInS }));
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it("appends the media token to every media builder", () => {
    setMedia("m.jwt", 3600);
    expect(fileUrl("p", "a b/c.png")).toBe("/api/files/p/a%20b/c.png?token=m.jwt");
    expect(thumbUrl("p", "c.png")).toBe("/api/thumbs/p/c.png?token=m.jwt");
    expect(uploadUrl("x.png")).toBe("/api/uploads/x.png?token=m.jwt");
    expect(attachmentUrl("/h/xylocopa-projects/p/.xylocopa/uploads/u.png")).toBe("/api/files/p/.xylocopa/uploads/u.png?token=m.jwt");
    expect(withMediaToken("/api/files/p/a.png?download=1")).toBe("/api/files/p/a.png?download=1&token=m.jwt");
  });

  it("appends nothing without a media token (auth off), even with a session token stored", () => {
    localStorage.setItem("cc-auth-token", "xys_session");
    expect(fileUrl("p", "c.png")).toBe("/api/files/p/c.png");
    expect(thumbUrl("p", "c.png")).toBe("/api/thumbs/p/c.png");
    expect(uploadUrl("x.png")).toBe("/api/uploads/x.png");
  });

  it("appends nothing once the media token has expired", () => {
    setMedia("m.old", -5);
    expect(fileUrl("p", "c.png")).toBe("/api/files/p/c.png");
  });

  it("never touches preview URLs (their own scoped token rides in the path)", () => {
    setMedia("m.jwt", 3600);
    expect(previewUrl("ptok", "p", "index.html")).toBe("/api/preview/t/ptok/p/index.html");
  });

  it("fileUrlToThumbUrl carries the media token along", () => {
    setMedia("m.jwt", 3600);
    expect(fileUrlToThumbUrl(fileUrl("p", "v.mp4"))).toBe("/api/thumbs/p/v.mp4?token=m.jwt");
  });
});

describe("stripAuthParams", () => {
  it("drops token and ticket, keeping other params in order and as encoded", () => {
    expect(stripAuthParams("/api/thumbs/p/a.png?token=abc.def&tile=1&_v=1700000000.5"))
      .toBe("/api/thumbs/p/a.png?tile=1&_v=1700000000.5");
    expect(stripAuthParams("/api/files/p/a%20b.png?download=1&token=x")).toBe("/api/files/p/a%20b.png?download=1");
    expect(stripAuthParams("/ws/status?ticket=xyt_1")).toBe("/ws/status");
    expect(stripAuthParams("/api/files/p/a.png?token=x#frag")).toBe("/api/files/p/a.png#frag");
    expect(stripAuthParams("/api/files/p/a.png?q=a%2Bb&token=")).toBe("/api/files/p/a.png?q=a%2Bb");
  });

  it("leaves URLs without auth params (and non-strings) alone", () => {
    expect(stripAuthParams("/api/files/p/a.png")).toBe("/api/files/p/a.png");
    expect(stripAuthParams("/api/files/p/a.png?tokenish=1&_v=2")).toBe("/api/files/p/a.png?tokenish=1&_v=2");
    expect(stripAuthParams("blob:https://x/1")).toBe("blob:https://x/1");
    expect(stripAuthParams(null)).toBe(null);
  });

  it("maps two media-token generations of one URL to the same key", () => {
    expect(stripAuthParams("/api/files/p/a.png?token=old&_v=3")).toBe(stripAuthParams("/api/files/p/a.png?token=new&_v=3"));
  });
});

describe("withCurrentMediaToken", () => {
  it("re-stamps a media URL with the given token, keeping its other parameters", () => {
    expect(withCurrentMediaToken("/api/thumbs/p/a.png?token=old&tile=1&_v=2", "new.jwt"))
      .toBe("/api/thumbs/p/a.png?tile=1&_v=2&token=new.jwt");
    expect(withCurrentMediaToken("/api/files/p/a.png", "new.jwt")).toBe("/api/files/p/a.png?token=new.jwt");
    expect(withCurrentMediaToken("/api/uploads/x.png?token=old", "new")).toBe("/api/uploads/x.png?token=new");
  });

  it("returns the very same string when the token is already current", () => {
    const url = "/api/thumbs/p/a.png?token=cur&tile=1";
    expect(withCurrentMediaToken(url, "cur")).toBe(url);
    expect(withCurrentMediaToken("/api/files/p/a.png?tile=1", null)).toBe("/api/files/p/a.png?tile=1");
  });

  it("drops a stale token when there is no current one", () => {
    expect(withCurrentMediaToken("/api/files/p/a.png?token=old&_v=3", null)).toBe("/api/files/p/a.png?_v=3");
  });

  it("never touches non-media URLs", () => {
    for (const url of ["https://example.com/a.png?x=1", "blob:https://x/1", "/api/preview/t/tok/p/a.html", undefined, null]) {
      expect(withCurrentMediaToken(url, "new")).toBe(url);
    }
  });
});
