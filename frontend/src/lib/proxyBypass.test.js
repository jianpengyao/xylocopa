// @vitest-environment node
import { describe, it, expect } from "vitest";
import {
  backendPath,
  isHookPath,
  proxyBypass,
  redactUrl,
  refuseHookPaths,
  setForwardedHeaders,
} from "../../proxy-bypass.js";
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
        // proxyBypass = forwarded headers + refuseHookPaths
        expect(block.proxy[key].bypass, key).toBe(proxyBypass);
        // IPv4 literal: uvicorn trusts X-Forwarded-* only from 127.0.0.1
        expect(block.proxy[key].target, key).toMatch(/^(http|ws):\/\/127\.0\.0\.1:8080$/);
      }
      expect(block.proxy["/api"].ws).toBe(true);
    }
  });
});

function fakeReq(url, { peer = "100.64.0.10", encrypted = true, headers = {} } = {}) {
  return {
    url,
    socket: { remoteAddress: peer, encrypted },
    headers: { host: "box.example:3000", ...headers },
  };
}

describe("proxy bypass: real client IP for the backend", () => {
  it("overwrites a client-supplied X-Forwarded-For with the TCP peer", () => {
    const req = fakeReq("/api/auth/login", {
      headers: { "x-forwarded-for": "6.6.6.6", "x-forwarded-proto": "http", "x-forwarded-host": "evil.example" },
    });
    expect(proxyBypass(req)).toBeUndefined();
    expect(req.headers["x-forwarded-for"]).toBe("100.64.0.10");
    expect(req.headers["x-forwarded-proto"]).toBe("https");
    expect(req.headers["x-forwarded-host"]).toBe("box.example:3000");
  });

  it("strips the ::ffff: prefix of v4-mapped peers", () => {
    const req = fakeReq("/api/agents", { peer: "::ffff:192.0.2.7" });
    setForwardedHeaders(req);
    expect(req.headers["x-forwarded-for"]).toBe("192.0.2.7");
    const v6 = fakeReq("/api/agents", { peer: "fd7a:115c:a1e0::1" });
    setForwardedHeaders(v6);
    expect(v6.headers["x-forwarded-for"]).toBe("fd7a:115c:a1e0::1");
  });

  it("sets the headers on WebSocket upgrades too", () => {
    const req = fakeReq("/ws/status?ticket=xyt_abc", {
      headers: { upgrade: "websocket", connection: "Upgrade", "x-forwarded-for": "6.6.6.6" },
    });
    expect(proxyBypass(req)).toBeUndefined();
    expect(req.headers["x-forwarded-for"]).toBe("100.64.0.10");
    expect(req.headers["x-forwarded-proto"]).toBe("https");
  });

  it("still refuses hook paths", () => {
    for (const url of ["/api/hooks/agent-stop", "/api/%68ooks/agent-stop", "/ws/../api/hooks/agent-stop"]) {
      expect(proxyBypass(fakeReq(url)), url).toBe(false);
    }
  });

  it("drops other forwarding headers and handles plain HTTP / missing peers", () => {
    const req = fakeReq("/api/agents", {
      encrypted: false,
      headers: { forwarded: "for=6.6.6.6", "x-real-ip": "6.6.6.6" },
    });
    setForwardedHeaders(req);
    expect(req.headers.forwarded).toBeUndefined();
    expect(req.headers["x-real-ip"]).toBeUndefined();
    expect(req.headers["x-forwarded-proto"]).toBe("http");

    const h2 = { url: "/api/agents", socket: {}, headers: { ":authority": "box.example:3000", "x-forwarded-for": "6.6.6.6" } };
    setForwardedHeaders(h2);
    expect(h2.headers["x-forwarded-for"]).toBeUndefined();  // never forward the client's value
    expect(h2.headers["x-forwarded-host"]).toBe("box.example:3000");
  });
});

describe("access-log redaction", () => {
  it("masks media tokens, WS tickets and path capabilities", () => {
    expect(redactUrl("/api/files/p/a.png?download=1&token=eyJh.sig&x=1"))
      .toBe("/api/files/p/a.png?download=1&token=<redacted>&x=1");
    expect(redactUrl("/api/thumbs/p/a.jpg?token=abc")).toBe("/api/thumbs/p/a.jpg?token=<redacted>");
    expect(redactUrl("/ws/status?ticket=xyt_abc")).toBe("/ws/status?ticket=<redacted>");
    expect(redactUrl("/api/preview/t/eyJh.sig/proj/index.html"))
      .toBe("/api/preview/t/<redacted>/proj/index.html");
    expect(redactUrl("/api/preview/p/0123abcd/proj/6006/")).toBe("/api/preview/p/<redacted>/proj/6006/");
    expect(redactUrl("/api/probe-trigger/tok123")).toBe("/api/probe-trigger/<redacted>");
    expect(redactUrl("/api/agents?mytoken=keep")).toBe("/api/agents?mytoken=keep");
    expect(redactUrl(undefined)).toBe("");
  });
});
