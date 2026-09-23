import { useState, useEffect, useRef, useCallback, lazy, Suspense } from "react";
import { Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import LoginPage from "./pages/LoginPage";
import CertGuidePage, { CERT_FLOW_KEY, CERT_FLOW_TTL_MS } from "./pages/CertGuidePage";
import ErrorBoundary from "./components/ErrorBoundary";
import useTheme from "./hooks/useTheme";
import { authCheck, clearAuthToken, fetchClaudeMdPending, getAuthToken } from "./lib/api";
import { isPushSupported, setupPushNotifications, reRegisterExistingSubscription } from "./lib/pushNotifications";
import useIdleLock from "./hooks/useIdleLock";
import usePageVisible from "./hooks/usePageVisible";
import { MonitorProvider } from "./contexts/MonitorContext";
import { ToastProvider } from "./contexts/ToastContext";
import { WebSocketProvider } from "./contexts/WebSocketContext";
import { WebAppDockHost } from "./components/WebAppPreview";
import { UnreadProvider, useUnread } from "./contexts/UnreadContext";
import { AgentsProvider } from "./contexts/AgentsContext";
import { FoldersProvider } from "./contexts/FoldersContext";
import { InboxTasksProvider } from "./contexts/InboxTasksContext";
import { HealthProvider } from "./contexts/HealthContext";
import AttentionButton from "./components/AttentionButton";
import BottomNavBar from "./components/BottomNavBar";
import RouteFallback from "./components/skeletons/RouteFallback";

const MODULE_RELOAD_KEY = "ah:module-import-reload-attempted";

async function clearModuleCachesBestEffort() {
  try {
    if ("serviceWorker" in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((r) => r.unregister()));
    }
  } catch {
    // Best-effort cleanup only.
  }
  try {
    if ("caches" in window) {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    }
  } catch {
    // Best-effort cleanup only.
  }
}

function lazyPage(importer) {
  // Single shared Promise per route. React.lazy reads .status synchronously
  // when its factory returns an already-settled Promise; sharing the same
  // Promise between preload() and the lazy factory means a preloaded route
  // mounts without ever showing the Suspense fallback.
  let cached = null;
  const load = () => {
    if (cached) return cached;
    cached = (async () => {
      let lastErr;
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const mod = await importer();
          try { sessionStorage.removeItem(MODULE_RELOAD_KEY); } catch { }
          return mod;
        } catch (err) {
          lastErr = err;
          if (attempt < 3) await new Promise((r) => setTimeout(r, 1000));
        }
      }
      // All retries exhausted — one-time reload as last resort
      let shouldReload = true;
      try {
        shouldReload = sessionStorage.getItem(MODULE_RELOAD_KEY) !== "1";
        if (shouldReload) sessionStorage.setItem(MODULE_RELOAD_KEY, "1");
      } catch { }
      if (shouldReload) {
        await clearModuleCachesBestEffort();
        window.location.reload();
        return new Promise(() => {});
      }
      throw lastErr;
    })().catch((err) => { cached = null; throw err; });
    return cached;
  };
  const Component = lazy(load);
  Component.preload = load;
  return Component;
}

const ProjectsPage = lazyPage(() => import("./pages/ProjectsPage"));
const TrashPage = lazyPage(() => import("./pages/TrashPage"));
const ProjectDetailPage = lazyPage(() => import("./pages/ProjectDetailPage"));
const AgentsPage = lazyPage(() => import("./pages/AgentsPage"));
const AgentChatPage = lazyPage(() => import("./pages/AgentChatPage"));
const TasksPage = lazyPage(() => import("./pages/TasksPage"));
const NewPage = lazyPage(() => import("./pages/NewPage"));
const MonitorPage = lazyPage(() => import("./pages/MonitorPage"));
const GitPage = lazyPage(() => import("./pages/GitPage"));
const TaskDetailPage = lazyPage(() => import("./pages/TaskDetailPage"));
const NewTaskPage = lazyPage(() => import("./pages/NewTaskPage"));
const SplitScreenPage = lazyPage(() => import("./pages/SplitScreenPage"));

// Preload registry — lib/prefetchChunks.js reads window.__xylocopa_preloaders
// to warm up heavy routes during idle time. Sharing the lazy component's
// own load function (not a parallel import()) means React.lazy gets back
// the already-settled Promise and skips the Suspense fallback on mount.
if (typeof window !== "undefined") {
  window.__xylocopa_preloaders = {
    AgentChatPage: () => AgentChatPage.preload(),
    ProjectDetailPage: () => ProjectDetailPage.preload(),
    TaskDetailPage: () => TaskDetailPage.preload(),
    NewTaskPage: () => NewTaskPage.preload(),
  };
}


function AuthGuard({ children }) {
  const navigate = useNavigate();
  const [checked, setChecked] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [serverDown, setServerDown] = useState(false);
  const [retrying, setRetrying] = useState(false);

  // Auto-lock after 30 min of inactivity (clears token + redirects to login)
  useIdleLock(navigate);

  // Listen for auth-expired events dispatched by api.js (graceful 401 handling)
  useEffect(() => {
    const handler = () => {
      clearAuthToken("auth-expired-event");
      navigate("/login", { replace: true });
    };
    window.addEventListener("auth-expired", handler);
    return () => window.removeEventListener("auth-expired", handler);
  }, [navigate]);

  // If the user just clicked the CA-cert download on /cert-guide, the same-tab
  // navigation can land them at "/" with no auth. Bounce them back to
  // /cert-guide instead of /login (consume once, TTL-bounded).
  const consumeCertFlowRedirect = () => {
    const ts = sessionStorage.getItem(CERT_FLOW_KEY);
    if (!ts) return false;
    sessionStorage.removeItem(CERT_FLOW_KEY);
    if (Date.now() - Number(ts) > CERT_FLOW_TTL_MS) return false;
    navigate("/cert-guide", { replace: true });
    return true;
  };

  // Attempt auth check with auto-retry for transient server-down (e.g. restart)
  const attemptAuth = (token) => {
    const doCheck = () =>
      authCheck()
        .then((r) => {
          if (r.authenticated) {
            setAuthed(true);
            setServerDown(false);
            // Always re-send existing subscription to backend (works in dev mode too)
            reRegisterExistingSubscription();
            // Full setup (SW registration + new subscription) only in production
            if (isPushSupported()) {
              setupPushNotifications().catch((err) => {
                console.warn("Push notification setup failed:", err);
              });
            }
          } else if (token) {
            clearAuthToken("authcheck-not-authenticated");
            if (!consumeCertFlowRedirect()) navigate("/login", { replace: true });
          } else {
            if (!consumeCertFlowRedirect()) navigate("/login", { replace: true });
          }
          setChecked(true);
        })
        .catch((err) => {
          // Network failures (TypeError from fetch) or 5xx → server is down
          // 401 is handled inside request() via auth-expired event
          if (err instanceof TypeError || (err.message && /^HTTP 5\d\d/.test(err.message))) {
            setServerDown(true);
          } else {
            console.warn("Auth check failed with unexpected error:", err);
            setServerDown(true);
          }
          setChecked(true);
        });
    doCheck();
  };

  useEffect(() => {
    attemptAuth(getAuthToken());
  }, [navigate]);

  // Auto-retry when server is down (polls every 2s for up to 60s)
  useEffect(() => {
    if (!serverDown) return;
    setRetrying(true);
    let attempts = 0;
    const interval = setInterval(async () => {
      attempts++;
      try {
        await authCheck();
        // Server is back — soft reset: re-run auth flow without unmounting the app
        clearInterval(interval);
        setServerDown(false);
        setRetrying(false);
        setChecked(false);           // causes the Loading... state to render briefly
        attemptAuth(getAuthToken()); // re-run the normal auth flow — this will set checked+authed again
      } catch (err) {
        if (attempts >= 30) {
          // 60s elapsed — stop auto-retry, keep manual button
          console.warn("Server reconnect gave up after 60s, last error:", err);
          clearInterval(interval);
          setRetrying(false);
        }
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [serverDown]);

  if (!checked) {
    return (
      <div className="flex items-center justify-center h-full bg-page">
        <div className="animate-pulse text-dim">Loading...</div>
      </div>
    );
  }

  if (serverDown) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-page gap-4">
        {retrying ? (
          <p className="text-label text-sm animate-pulse">Reconnecting to server...</p>
        ) : (
          <p className="text-label text-sm">Unable to reach server</p>
        )}
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="px-4 py-2 rounded-lg bg-accent text-white text-sm hover:bg-accent transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }

  return authed ? children : null;
}

function AppRoutes({ themeProps }) {
  const location = useLocation();
  const bgLocation = location.state?.backgroundLocation;

  // Persist current route so app resumes where you left off
  useEffect(() => {
    const path = location.pathname;
    if (path && path !== "/login" && path !== "/" && path !== "/split" && !path.startsWith("/new")) {
      localStorage.setItem("ah:last-route", path);
    }
    // eslint-disable-next-line no-console
    console.log(`[nav] → ${path} @ ${performance.now().toFixed(0)}ms`);
  }, [location.pathname]);

  // On first mount, redirect to last-visited route
  const savedRoute = localStorage.getItem("ah:last-route");
  const resumeTo = savedRoute && savedRoute !== "/" && savedRoute !== "/login" ? savedRoute : "/projects";

  // Effective path for routing decisions: bg if overlay is open, else current.
  const effectivePath = (bgLocation || location).pathname;

  // Which of the four keep-mounted main tabs is currently visible (if any).
  // These pages stay mounted across navigation — switching tabs flips visibility
  // via CSS instead of unmounting/remounting, so re-entry is instant.
  const keepMountedActive =
    effectivePath === "/projects" ? "projects" :
    effectivePath === "/agents" ? "agents" :
    effectivePath === "/tasks" ? "tasks" :
    effectivePath === "/git" ? "git" :
    null;

  // Dynamic routes render only when the active path is not one of the keep-mounted tabs.
  const showingDynamic = keepMountedActive === null;

  // Hide inactive keep-mounted tabs via `visibility:hidden + position:absolute`
  // rather than `display:none`. CSS keyframe animations (animate-ping,
  // animate-glow) restart from frame 0 whenever an element flips from
  // `display:none` to `display:block` — which produced a visible flash on
  // every tab switch back into Projects/Agents/Tasks/Git. visibility keeps
  // the rendering tree intact so animations stay in-phase across switches.
  // data-page-hidden lets CSS pause animations inside parked tabs.
  const tabProps = (active) => active
    ? {}
    : { style: { position: "absolute", inset: 0, visibility: "hidden", pointerEvents: "none" }, "data-page-hidden": "" };

  return (
    <div className="relative h-full">
      {/* Keep-mounted main tabs — always rendered, visibility toggled by CSS.
          Each receives `isActive` so its polling/effects pause when hidden. */}
      <div className="h-full" {...tabProps(keepMountedActive === "projects")}>
        <ProjectsPage {...themeProps} isActive={keepMountedActive === "projects"} />
      </div>
      <div className="h-full" {...tabProps(keepMountedActive === "agents")}>
        <AgentsPage {...themeProps} isActive={keepMountedActive === "agents"} />
      </div>
      <div className="h-full" {...tabProps(keepMountedActive === "tasks")}>
        <TasksPage {...themeProps} isActive={keepMountedActive === "tasks"} />
      </div>
      <div className="h-full" {...tabProps(keepMountedActive === "git")}>
        <GitPage {...themeProps} isActive={keepMountedActive === "git"} />
      </div>

      {/* Dynamic routes — mount/unmount as before */}
      {showingDynamic && (
        <Routes location={bgLocation || location}>
          <Route path="/" element={<Navigate to={resumeTo} replace />} />
          <Route path="/projects/trash" element={<TrashPage {...themeProps} />} />
          <Route path="/projects/:name" element={<ProjectDetailPage {...themeProps} />} />
          <Route path="/agents/:id" element={<AgentChatPage {...themeProps} />} />
          <Route path="/tasks/:id" element={<TaskDetailPage {...themeProps} />} />
          <Route path="/new" element={<NewPage {...themeProps} />} />
          <Route path="/monitor" element={<MonitorPage {...themeProps} />} />
          <Route path="/split" element={<SplitScreenPage />} />
        </Routes>
      )}

      {/* NewTaskPage is now rendered as a modal in AppChrome, not via routes */}
    </div>
  );
}

// Debug overlay: visualizes safe-area boundaries and keyboard position.
// Toggle on/off via browser console: localStorage.setItem("ah:debug-lines", "1") then reload.
// Or call window.__toggleDebugLines() at runtime.
function DebugSafeAreaOverlay() {
  const [info, setInfo] = useState("");
  const [kbInfo, setKbInfo] = useState(null);
  const baseH = useRef(null);
  useEffect(() => {
    const update = () => {
      const probeTop = document.createElement("div");
      probeTop.style.cssText = "position:fixed;top:env(safe-area-inset-top,0px);left:0;visibility:hidden;pointer-events:none";
      const probeBot = document.createElement("div");
      probeBot.style.cssText = "position:fixed;bottom:env(safe-area-inset-bottom,0px);left:0;visibility:hidden;pointer-events:none";
      document.body.appendChild(probeTop);
      document.body.appendChild(probeBot);
      const safeTop = probeTop.getBoundingClientRect().top;
      const safeBot = window.innerHeight - probeBot.getBoundingClientRect().top;
      document.body.removeChild(probeTop);
      document.body.removeChild(probeBot);
      const vv = window.visualViewport;
      const vvh = vv?.height ?? window.innerHeight;
      const vvOffset = vv?.offsetTop ?? 0;
      if (baseH.current === null) baseH.current = window.innerHeight;
      const kbH = Math.round(baseH.current - (vvOffset + vvh));
      const kbTopPos = Math.round(vvOffset + vvh);
      const standalone = window.navigator.standalone ? "yes" : (window.matchMedia("(display-mode: standalone)").matches ? "yes(mm)" : "no");
      setInfo(`safe-top:${safeTop} | safe-bot:${safeBot} | vh:${baseH.current} | vvh:${Math.round(vvh)} | ${standalone}`);
      setKbInfo(kbH > 10 ? { top: kbTopPos, label: `kb:${kbH} vvOff:${Math.round(vvOffset)}` } : null);
    };
    update();
    window.addEventListener("resize", update);
    window.visualViewport?.addEventListener("resize", update);
    window.visualViewport?.addEventListener("scroll", update);
    return () => {
      window.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("scroll", update);
    };
  }, []);
  const line = (pos, color, extra) => <div style={{ position:"fixed", ...pos, left:0, right:0, height:"2px", background:color, zIndex:9999, pointerEvents:"none", ...extra }} />;
  return (
    <>
      {/* Top: red = screen edge, blue = safe area */}
      {line({ top:0 }, "red")}
      {line({ top:"env(safe-area-inset-top, 0px)" }, "blue")}
      {/* Bottom: red = screen edge, blue = safe area */}
      {line({ bottom:0 }, "red")}
      {line({ bottom:"env(safe-area-inset-bottom, 0px)" }, "blue")}
      {/* Info label at safe-area top */}
      <div style={{ position:"fixed", top:"env(safe-area-inset-top, 0px)", left:0, right:0, zIndex:9999, pointerEvents:"none", display:"flex", justifyContent:"center" }}>
        <span style={{ background:"rgba(0,0,0,0.75)", color:"#0f0", fontSize:"10px", padding:"2px 8px", borderRadius:"0 0 4px 4px", fontFamily:"monospace", whiteSpace:"nowrap" }}>{info}</span>
      </div>
      {/* Keyboard top: green line + label */}
      {kbInfo && (
        <>
          <div style={{ position:"fixed", top:kbInfo.top, left:0, right:0, height:"4px", background:"#00ff00", zIndex:9999, pointerEvents:"none", boxShadow:"0 0 6px #00ff00" }} />
          <div style={{ position:"fixed", top:kbInfo.top - 18, left:0, right:0, zIndex:9999, pointerEvents:"none", display:"flex", justifyContent:"center" }}>
            <span style={{ background:"rgba(0,0,0,0.75)", color:"#0f0", fontSize:"10px", padding:"2px 8px", borderRadius:"4px", fontFamily:"monospace" }}>{kbInfo.label}</span>
          </div>
        </>
      )}
    </>
  );
}

function useDebugLines() {
  const [on, setOn] = useState(() => localStorage.getItem("ah:debug-lines") === "1");
  useEffect(() => {
    window.__toggleDebugLines = () => {
      const next = localStorage.getItem("ah:debug-lines") !== "1";
      localStorage.setItem("ah:debug-lines", next ? "1" : "0");
      setOn(next);
    };
    return () => { delete window.__toggleDebugLines; };
  }, []);
  return on;
}

// Inner shell rendered inside the auth-gated providers
// (WebSocketProvider → UnreadProvider). Owns the BottomNav badge,
// AttentionButton, and PWA app-badge — all read from useUnread() so
// they update in the same React render when any agent's unread_count
// changes via WS.
function AppChrome({ themeProps }) {
  const location = useLocation();
  const navigate = useNavigate();
  const hideNav = location.pathname.match(/^\/agents\/[^/]+$/) || location.pathname.match(/^\/tasks\/[^/]+$/) || location.pathname === "/login" || location.pathname === "/split";
  const { total: unread } = useUnread();
  const [claudeMdPending, setClaudeMdPending] = useState(0);
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const visible = usePageVisible();
  const pathnameRef = useRef(location.pathname);
  pathnameRef.current = location.pathname;
  const lastTapRef = useRef({});

  const handleNavDoubleTap = useCallback((key, e) => {
    const now = Date.now();
    const prev = lastTapRef.current[key] || 0;
    lastTapRef.current[key] = now;
    if (now - prev > 350) return; // not a double-tap
    lastTapRef.current[key] = 0;
    e.preventDefault();
    window.dispatchEvent(new CustomEvent("nav-scroll-to-unread", { detail: { tab: key } }));
  }, []);

  useEffect(() => {
    if (!visible) return;
    const poll = () => {
      if (pathnameRef.current === "/login" || !getAuthToken()) return;
      fetchClaudeMdPending().then((r) => setClaudeMdPending(r.count || 0)).catch((err) => {
        console.warn("Claude MD pending poll failed:", err);
      });
    };
    poll();
    const id = setInterval(poll, 30000);
    return () => clearInterval(id);
  }, [visible]);

  // PWA app icon badge — agent unread count
  useEffect(() => {
    if (!navigator.setAppBadge) return;
    if (unread > 0) navigator.setAppBadge(unread).catch(() => {});
    else navigator.clearAppBadge?.().catch(() => {});
  }, [unread]);

  return (
    <>
      <ErrorBoundary>
        <Suspense fallback={<RouteFallback />}>
          <AppRoutes themeProps={themeProps} />
        </Suspense>
      </ErrorBoundary>

      {/* Attention button — FAB shows unread total, long-press opens split screen */}
      <AttentionButton />

      {/* NewTaskPage modal — rendered in-place, no route change */}
      {newTaskOpen && (
        <Suspense fallback={null}>
          <NewTaskPage
            onClose={() => setNewTaskOpen(false)}
            contextPath={location.pathname}
          />
        </Suspense>
      )}

      {/* Bottom tab bar — floating glass pill */}
      {!hideNav && (
        <BottomNavBar
          className="fixed bottom-[13px] left-0 right-0 z-40 flex justify-center px-4"
          badges={{ agents: unread, projects: claudeMdPending }}
          onDoubleTap={handleNavDoubleTap}
          onNewTask={() => setNewTaskOpen(true)}
          onProjectsTap={(e) => {
            e.preventDefault();
            // Double-tap detection for projects
            const now = Date.now();
            const prev = lastTapRef.current.projects || 0;
            lastTapRef.current.projects = now;
            if (now - prev <= 350) {
              lastTapRef.current.projects = 0;
              if (!location.pathname.startsWith("/projects") || location.pathname !== "/projects") {
                navigate("/projects", { replace: true });
              }
              window.dispatchEvent(new CustomEvent("nav-scroll-to-unread", { detail: { tab: "projects" } }));
              return;
            }
            // Already on a /projects route → go to list
            if (location.pathname.startsWith("/projects")) {
              navigate("/projects", { replace: true });
              sessionStorage.removeItem("returnedFrom:projects");
              return;
            }
            const returnedFrom = sessionStorage.getItem("returnedFrom:projects");
            const lastViewed = localStorage.getItem("lastViewed:projects");
            if (returnedFrom) {
              sessionStorage.removeItem("returnedFrom:projects");
              localStorage.removeItem("lastViewed:projects");
              navigate("/projects", { replace: true });
            } else if (lastViewed) {
              navigate(`/projects/${encodeURIComponent(lastViewed)}`, { replace: true });
            } else {
              navigate("/projects", { replace: true });
            }
          }}
        />
      )}
    </>
  );
}

export default function App() {
  const { theme, toggle } = useTheme();
  const themeProps = { theme, onToggleTheme: toggle };
  const location = useLocation();
  const navigate = useNavigate();
  const showDebug = useDebugLines();

  // SW update gate — block all UI (including LoginPage) until we know
  // whether a new Service Worker is pending. Prevents the race where SW
  // activation reloads the page mid-login (two-unlocks bug).
  const [swState, setSwState] = useState('checking');
  useEffect(() => {
    if (!('serviceWorker' in navigator)) { setSwState('ready'); return; }
    let cancelled = false;
    (async () => {
      try {
        const reg = await navigator.serviceWorker.getRegistration();
        if (!reg) { if (!cancelled) setSwState('ready'); return; }
        try { await reg.update(); } catch { /* offline — proceed */ }
        if (cancelled) return;
        setSwState(reg.installing || reg.waiting ? 'updating' : 'ready');
      } catch {
        if (!cancelled) setSwState('ready');
      }
    })();
    // Safety: never sit on the gate forever
    const timer = setTimeout(() => { if (!cancelled) setSwState('ready'); }, 8000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);

  // Service Worker notification click — split-screen aware navigation.
  // Listener is mounted ONCE and reads pathname/navigate via refs so it
  // never detaches across route changes.  Previous deps-based effect
  // dropped messages that arrived during the brief teardown→reattach
  // window when iOS resumed the PWA from background.
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const pathnameRef = useRef(location.pathname);
  pathnameRef.current = location.pathname;
  useEffect(() => {
    const handler = (event) => {
      if (event.data?.type !== "notification-navigate") return;
      const url = event.data.url || "/";
      if (pathnameRef.current === "/split") {
        window.dispatchEvent(new CustomEvent("split-navigate", { detail: { url } }));
      } else {
        navigateRef.current(url);
      }
    };
    navigator.serviceWorker?.addEventListener("message", handler);
    return () => navigator.serviceWorker?.removeEventListener("message", handler);
  }, []);

  // Safari iOS: after keyboard dismiss the visual viewport desyncs from
  // the layout viewport.  The ONLY thing that fixes it is an actual
  // scroll event.  body::after adds 1px so scrollBy has room to move.
  useEffect(() => {
    const timers = [];
    const microScroll = () => {
      timers.forEach(clearTimeout);
      timers.length = 0;
      // Fire immediately, then once more after a short delay as a safety net
      const doIt = () => {
        window.scrollTo({ top: 1, behavior: "instant" });
        window.scrollTo({ top: 0, behavior: "instant" });
      };
      doIt();
      timers.push(setTimeout(doIt, 50));
    };

    // Keyboard dismiss: input/textarea loses focus
    const onFocusOut = (e) => {
      if (e.target?.tagName === "TEXTAREA" || e.target?.tagName === "INPUT") {
        microScroll();
      }
    };

    // Tab resume
    const onVisibility = () => {
      if (document.visibilityState === "visible") microScroll();
    };

    document.addEventListener("focusout", onFocusOut);
    document.addEventListener("visibilitychange", onVisibility);
    // NOTE: intentionally NOT listening to window.resize here.
    // Resize fires while the keyboard is open (autocomplete bar changes, etc.)
    // and the scrollTo(1)→scrollTo(0) micro-scroll causes visible jitter.
    // focusout + visibilitychange cover the dismiss/resume cases we need.
    return () => {
      timers.forEach(clearTimeout);
      document.removeEventListener("focusout", onFocusOut);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  if (swState !== 'ready') {
    return (
      <ErrorBoundary>
      <ToastProvider>
      <div className="flex flex-col h-screen bg-page text-heading min-w-[320px] overflow-x-hidden">
        <main className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 px-4">
          {swState === 'checking' ? (
            <p className="text-dim text-sm animate-pulse">Loading...</p>
          ) : (
            <>
              <p className="text-label text-sm animate-pulse">Updating to latest version…</p>
              <p className="text-dim text-xs">The app will reload automatically.</p>
            </>
          )}
        </main>
      </div>
      </ToastProvider>
      </ErrorBoundary>
    );
  }

  return (
    <ErrorBoundary>
    <ToastProvider>
    <div className="flex flex-col h-screen bg-page text-heading min-w-[320px] overflow-x-hidden">
      {showDebug && <DebugSafeAreaOverlay />}
      {/* Main content area */}
      <main className="flex-1 min-h-0 overflow-hidden">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/cert-guide" element={<CertGuidePage />} />
          <Route
            path="/*"
            element={
              <AuthGuard>
                <WebSocketProvider>
                <HealthProvider>
                <MonitorProvider>
                <UnreadProvider>
                <AgentsProvider>
                <FoldersProvider>
                <InboxTasksProvider>
                  <AppChrome themeProps={themeProps} />
                  {/* Global web-app dock: minimized previews survive
                      navigation; torn down when their agent fully stops. */}
                  <WebAppDockHost />
                </InboxTasksProvider>
                </FoldersProvider>
                </AgentsProvider>
                </UnreadProvider>
                </MonitorProvider>
                </HealthProvider>
                </WebSocketProvider>
              </AuthGuard>
            }
          />
        </Routes>
      </main>
    </div>
    </ToastProvider>
    </ErrorBoundary>
  );
}
