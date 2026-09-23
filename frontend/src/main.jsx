import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "katex/dist/katex.min.css";
import "./index.css";
import App from "./App.jsx";
import { registerSW } from "virtual:pwa-register";
import { setupFrameLogger } from "./lib/frameLogger";
import { prefetchHeavyChunks } from "./lib/prefetchChunks";
import { applyEinkModeFromStorage } from "./lib/einkMode";

// Frame-by-frame DOM mutation logger (off by default).
// Enable: localStorage.setItem("ah:frame-log", "1") then reload.
setupFrameLogger();

// Idle-time preload for AgentChatPage / ProjectDetailPage / TaskDetailPage
// / NewTaskPage chunks so first navigation into them doesn't show the
// Suspense "Loading..." fallback.
prefetchHeavyChunks();

// Mark e-ink devices as glass-incapable: `html.no-glass` pins .glass-bar and
// .glass-bar-nav opaque + blur-free (see index.css). Their vendor-forked
// engines often parse backdrop-filter without rendering the blur, and e-ink
// panels can't display it anyway.
//
// The former Linux-desktop blacklist (d8e9a3b) was removed 2026-07: Chrome
// 146 verifiably renders backdrop-filter in every compositing mode incl.
// pure software (on-device repro: /glass-diag.html). Residual unknown
// engines degrade safely — .glass-bar-nav uses alpha ≥.65 so a dropped blur
// reads as a washed-out surface, not see-through glass.
(function tagGlassCapability() {
  try {
    const ua = navigator.userAgent || "";
    const isMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(ua);
    const isLinux = /Linux/i.test(ua) && !/Android/i.test(ua);
    const isEInk = /Onyx|BOOX|Kindle|Silk|reMarkable|PocketBook|Likebook|InkPad|MEEbook|Bigme|Hisense.*ink|Meebook|iReader/i.test(ua);
    // Touch devices too: the composer bar sits over the scrolling message
    // list and a 16px backdrop blur re-blurs that area every frame on a
    // phone GPU. no-glass swaps in each theme's opaque bar colours (the
    // translucent tokens alone read as see-through once the blur is gone).
    const isTouch = matchMedia("(hover: none) and (pointer: coarse)").matches;
    if (isEInk || isTouch) {
      document.documentElement.classList.add("no-glass");
    }

    applyEinkModeFromStorage();

    if (/[?&]eink-diag=1/.test(location.search)) {
      const updateSlow = matchMedia("(update: slow)").matches;
      const monochrome = matchMedia("(monochrome)").matches;
      const monoDepth = matchMedia("(min-monochrome: 1)").matches;
      const noGlass = document.documentElement.classList.contains("no-glass");
      const info = {
        ua, isMobile, isLinux, isEInk,
        noGlass, updateSlow, monochrome, monoDepth,
        colorDepth: screen.colorDepth,
        dpr: devicePixelRatio,
        viewport: `${innerWidth}x${innerHeight}`,
      };
      const ready = () => {
        const box = document.createElement("pre");
        box.textContent = "EINK-DIAG\n" + JSON.stringify(info, null, 2);
        Object.assign(box.style, {
          position: "fixed", top: "0", left: "0", right: "0",
          zIndex: "999999",
          background: "#fff", color: "#000",
          font: "12px/1.4 monospace",
          padding: "12px", margin: "0",
          border: "2px solid #000",
          whiteSpace: "pre-wrap", wordBreak: "break-all",
          maxHeight: "60vh", overflow: "auto",
        });
        document.body.appendChild(box);
      };
      if (document.body) ready();
      else document.addEventListener("DOMContentLoaded", ready);
    }
  } catch { /* best-effort */ }
})();

// --- Reload tracing probe (event listeners only) ---------------------------
// The location.reload() monkey-patch lives in index.html so it installs
// before any ES module (including vite client) loads.  Here we add the
// remaining event listeners that don't need to run pre-module.
(function installReloadProbe() {
  const beacon = (payload) => {
    try {
      const body = JSON.stringify({ ...payload, ts: Date.now(), path: location.pathname });
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/debug/auth-diag", new Blob([body], { type: "application/json" }));
      } else {
        fetch("/api/debug/auth-diag", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
          keepalive: true,
        }).catch(() => {});
      }
    } catch { /* best-effort */ }
  };
  window.addEventListener("pagehide", (e) => {
    beacon({ action: "reload-trace", reason: "pagehide", persisted: e.persisted });
  });
  window.addEventListener("beforeunload", () => {
    beacon({ action: "reload-trace", reason: "beforeunload" });
  });
  if ("serviceWorker" in navigator) {
    // New SW activation is traced but does NOT force a page reload. With
    // clientsClaim the fresh SW controls the page immediately, so updated
    // assets are served on the next navigation. Forcing location.reload()
    // on every controllerchange reloaded every open tab on every rebuild;
    // genuinely stale in-memory chunk refs are caught by the chunk-recovery
    // net in index.html (capture-phase asset 404 + dynamic-import failure).
    let hadController = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      beacon({ action: "reload-trace", reason: "sw-controllerchange", hadController });
      hadController = true;
    });
  }
  try {
    const nav = performance.getEntriesByType("navigation")[0];
    if (nav?.type === "reload") {
      beacon({ action: "reload-trace", reason: "load-after-reload" });
    }
  } catch { /* performance API may be restricted */ }
})();

// Register VitePWA service worker with autoUpdate.
// Precaches all static assets (JS/CSS/HTML with content hashes).
if ("serviceWorker" in navigator) {
  registerSW({
    onRegisteredSW(swUrl, registration) {
      if (!registration) return;
      // Check for SW updates every 30 minutes (background)
      setInterval(() => { registration.update(); }, 30 * 60 * 1000);
      // Also check on every tab/app focus — catches rebuilds immediately
      // when user switches back to the app (mobile PWA, iPad, etc.)
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") registration.update();
      });
    },
  });
}

// Global error handlers — catch async/event-handler errors that React
// error boundaries cannot intercept.  Shows a raw DOM toast so it works
// even if React itself has crashed.
function showErrorToast(msg) {
  // Skip expected auth errors
  if (typeof msg === "string" && msg.includes("Not authenticated")) return;

  let container = document.getElementById("global-error-toast");
  if (!container) {
    container = document.createElement("div");
    container.id = "global-error-toast";
    Object.assign(container.style, {
      position: "fixed",
      bottom: "80px",
      left: "50%",
      transform: "translateX(-50%)",
      zIndex: "99999",
      maxWidth: "90vw",
      padding: "10px 18px",
      borderRadius: "10px",
      background: "#dc2626",
      color: "#fff",
      fontSize: "13px",
      fontFamily: "system-ui, sans-serif",
      boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
      pointerEvents: "none",
      opacity: "0",
      transition: "opacity 0.3s",
    });
    document.body.appendChild(container);
  }
  container.textContent = String(msg).slice(0, 200);
  container.style.opacity = "1";
  clearTimeout(container._timer);
  container._timer = setTimeout(() => {
    container.style.opacity = "0";
  }, 5000);
}

window.addEventListener("error", (e) => {
  showErrorToast(e.message || "Uncaught error");
});

window.addEventListener("unhandledrejection", (e) => {
  const msg = e.reason?.message || e.reason || "Unhandled promise rejection";
  showErrorToast(msg);
});

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>
);
