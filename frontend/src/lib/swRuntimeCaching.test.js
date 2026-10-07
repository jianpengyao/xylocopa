import { describe, it, expect } from "vitest";
import { runtimeCaching } from "../../sw-runtime-caching.js";

// Mirrors workbox-routing's matcher semantics for runtimeCaching entries:
// a RegExp is tested against the full URL (same-origin: href; cross-origin:
// only matches when the pattern anchors at the origin), a function receives
// { url, request, event }.
function matches(route, url, request) {
  const { urlPattern } = route;
  if (urlPattern instanceof RegExp) return urlPattern.test(url.href);
  if (typeof urlPattern === "function") return !!urlPattern({ url, request, event: null });
  return false;
}

const ORIGIN = "https://100.122.52.79:3000";
const req = (path, mode = "cors", method = "GET") => {
  const url = new URL(path, ORIGIN);
  return { url, request: { url: url.href, mode, method, destination: "" } };
};

function matchingRoutes(path, mode) {
  const { url, request } = req(path, mode);
  return runtimeCaching.filter((r) => matches(r, url, request));
}

describe("service worker runtimeCaching routes", () => {
  it("never intercepts navigation requests (page loads)", () => {
    for (const path of ["/", "/login", "/agents/606011a9b2d3", "/split", "/cert-guide", "/index.html"]) {
      expect(matchingRoutes(path, "navigate"), path).toEqual([]);
    }
  });

  it("never intercepts API calls (incl. files/uploads/auth/health)", () => {
    for (const path of [
      "/api/auth/check",
      "/api/health",
      "/api/agents",
      "/api/v2/tasks?x=1",
      "/api/files/paper-writing/figures_src/v8.png",
      "/api/uploads/abc.mp4",
      "/api/push/vapid-public-key",
      "/api/preview/t/tok/index.html",
    ]) {
      expect(matchingRoutes(path, "cors"), path).toEqual([]);
    }
  });

  it("never intercepts the service worker script, manifest or websocket paths", () => {
    for (const path of ["/sw.js", "/push-handler.js", "/manifest.webmanifest", "/ws/status"]) {
      expect(matchingRoutes(path, "cors"), path).toEqual([]);
    }
  });

  it("only uses cache-first handlers for image assets", () => {
    expect(runtimeCaching.length).toBeGreaterThan(0);
    for (const route of runtimeCaching) {
      expect(route.handler).toBe("CacheFirst");
      expect(route.urlPattern).toBeInstanceOf(RegExp);
    }
  });

  it("still caches thumbnails and emoji images", () => {
    expect(matchingRoutes("/api/thumbs/proj/figure.png?token=abc", "cors").map((r) => r.options.cacheName))
      .toEqual(["thumbnail-cache"]);
    expect(matchingRoutes("/api/files/proj/x.thumb.jpg", "cors").map((r) => r.options.cacheName))
      .toEqual(["thumbnail-cache"]);
    const emoji = new URL("https://cdn.jsdelivr.net/gh/microsoft/fluentui-emoji@main/assets/Bee/3D/bee_3d.svg");
    expect(runtimeCaching.filter((r) => matches(r, emoji, { url: emoji.href, mode: "cors", method: "GET" }))
      .map((r) => r.options.cacheName)).toEqual(["fluent-emoji-cache"]);
  });
});
