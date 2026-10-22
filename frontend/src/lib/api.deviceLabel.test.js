import { describe, it, expect, vi, afterEach } from "vitest";

// deviceLabel() for Apple devices that send a Mac User-Agent: iPadOS Safari,
// and an iPhone set to "Request Desktop Website" (seen from an iPhone 17 Pro
// Max home-screen app). Touch points + screen size tell them apart.
const MAC_SAFARI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0.1 Safari/605.1.15";

async function load() {
  vi.resetModules();
  localStorage.clear();
  return import("./api");
}

function device(ua, { touch = 0, width = 0, height = 0, standalone = false } = {}) {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(ua);
  Object.defineProperty(navigator, "maxTouchPoints", { value: touch, configurable: true });
  vi.spyOn(window.screen, "width", "get").mockReturnValue(width);
  vi.spyOn(window.screen, "height", "get").mockReturnValue(height);
  vi.stubGlobal("matchMedia", (q) => ({ matches: standalone && q === "(display-mode: standalone)" }));
}

afterEach(() => {
  delete navigator.maxTouchPoints;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("deviceLabel with a Mac User-Agent", () => {
  it("iPhone in desktop mode (home-screen app) is an iPhone, not an iPad", async () => {
    const api = await load();
    device(MAC_SAFARI_UA, { touch: 5, width: 440, height: 956, standalone: true }); // iPhone 17 Pro Max
    expect(api.deviceLabel()).toBe("iPhone · Installed app");
    device(MAC_SAFARI_UA, { touch: 5, width: 956, height: 440 }); // landscape
    expect(api.deviceLabel()).toBe("iPhone · Safari");
    device(MAC_SAFARI_UA, { touch: 5, width: 375, height: 667 }); // iPhone SE
    expect(api.deviceLabel()).toBe("iPhone · Safari");
  });

  it("iPads stay iPads, iPad mini included", async () => {
    const api = await load();
    device(MAC_SAFARI_UA, { touch: 5, width: 744, height: 1133 }); // iPad mini
    expect(api.deviceLabel()).toBe("iPad · Safari");
    device(MAC_SAFARI_UA, { touch: 5, width: 1366, height: 1024, standalone: true }); // iPad Pro 12.9"
    expect(api.deviceLabel()).toBe("iPad · Installed app");
  });

  it("unknown screen size keeps the iPad label; no touch is a Mac whatever the screen", async () => {
    const api = await load();
    device(MAC_SAFARI_UA, { touch: 5 });
    expect(api.deviceLabel()).toBe("iPad · Safari");
    device(MAC_SAFARI_UA, { touch: 0, width: 1512, height: 982, standalone: true });
    expect(api.deviceLabel()).toBe("Mac · Installed app");
    device(MAC_SAFARI_UA, { touch: 0, width: 400, height: 500 }); // small window, still a Mac
    expect(api.deviceLabel()).toBe("Mac · Safari");
  });
});
