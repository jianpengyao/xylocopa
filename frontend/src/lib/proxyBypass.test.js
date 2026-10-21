// @vitest-environment node
import { describe, it, expect } from "vitest";
import { backendPath, isHookPath, refuseHookPaths } from "../../proxy-bypass.js";
import viteConfig from "../../vite.config.js";

describe("proxy bypass: /api/hooks/* is refused by the HTTPS proxy", () => {
  it("refuses the hook endpoints", () => {
    for (const url of [
      "/api/hooks/agent-stop",
      "/api/hooks/agent-tool-activity?x=1",
      "/api/hooks/agent-permission",
      "/api/hooks",
      "/api/hooks/",
    ]) {
      expect(refuseHookPaths({ url })).toBe(false);
    }
  });

  it("refuses encoded, dotted and doubled-slash spellings of the same path", () => {
    for (const url of [
      "/api/%68ooks/agent-stop",
      "/api/%68%6F%6F%6B%73/agent-stop",
      "/api%2Fhooks%2Fagent-stop",
      "/api//hooks/agent-stop",
      "/api/hooks//agent-stop",
      "/api/x/../hooks/agent-stop",
      "/api/x/%2e%2e/hooks/agent-stop",
      "/api/x/..%2Fhooks/agent-stop",
      "/api/./hooks/agent-stop",
      "/api\\hooks\\agent-stop",
      "/api/HOOKS/agent-stop",
    ]) {
      expect(isHookPath(url), url).toBe(true);
    }
  });

  it("refuses hook paths reached through another proxy key (/ws, /wsx)", () => {
    // Vite matches '/ws' with a raw startsWith(); the proxy then resolves
    // the dot segments and the backend routes /api/hooks/agent-stop.
    for (const url of [
      "/ws/../api/hooks/agent-stop",
      "/ws/%2e%2e/api/hooks/agent-stop",
      "/ws/%2E%2E/api/hooks/agent-stop",
      "/ws/./../api/hooks/agent-stop",
      "/wsx/../api/hooks/agent-stop",
      "/ws\\..\\api\\hooks\\agent-stop",
      "/ws/status/../../api/hooks/agent-permission",
    ]) {
      expect(refuseHookPaths({ url }), url).toBe(false);
    }
    for (const url of ["/ws/status", "/ws/terminal/abcd1234", "/ws/status?token=x"]) {
      expect(refuseHookPaths({ url }), url).toBeUndefined();
    }
  });

  it("passes every other /api path through to the proxy", () => {
    for (const url of [
      "/api/health",
      "/api/hooksX",
      "/api/agents/abc/hooks/x",
      "/api/debug/auth-diag",
      "/api/cert/regenerate",
      "/api/preview/p/sig/proj/5173/ws",
      "/api/files/proj/docs/hooks/readme.md",
    ]) {
      expect(refuseHookPaths({ url }), url).toBeUndefined();
    }
  });

  it("normalises the path the way proxy + uvicorn would route it", () => {
    expect(backendPath("/api/%68ooks/a?q=%2Fapi%2Fhooks")).toBe("/api/hooks/a");
    expect(backendPath("/api/a/b/../c")).toBe("/api/a/c");
    expect(backendPath("//api/hooks/x")).toBe("/api/hooks/x");
  });

  it("is wired into every proxy entry of the dev and preview servers", () => {
    for (const block of [viteConfig.server, viteConfig.preview]) {
      const keys = Object.keys(block.proxy);
      expect(keys).toEqual(expect.arrayContaining(["/api", "/ws"]));
      for (const key of keys) {
        expect(block.proxy[key].bypass, key).toBe(refuseHookPaths);
      }
      expect(block.proxy["/api"].ws).toBe(true);
    }
  });
});
