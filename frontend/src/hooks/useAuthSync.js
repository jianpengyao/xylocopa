import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { AUTH_LOCKED_KEY, AUTH_TOKEN_KEY, getAuthToken } from "../lib/api";

// Routes that need no session: another tab signing out leaves them alone.
const PUBLIC_PATHS = new Set(["/login", "/cert-guide"]);

/**
 * Cross-tab auth sync. localStorage `storage` events fire only in the other
 * tabs of this browser (iOS PWA and Safari do not share storage, so this
 * matters for desktop tabs):
 *   - token removed (logout / revoked in another tab) → /login,
 *     reason "signed-out-elsewhere"
 *   - token set while on /login (logged in or unlocked elsewhere) → /
 *   - idle-lock flag set (another tab hit the server's idle lock) → /login,
 *     reason "idle"
 * Mounted once in App, the only always-mounted component.
 */
export default function useAuthSync() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const navigateRef = useRef(navigate);
  const pathRef = useRef(pathname);
  useEffect(() => {
    navigateRef.current = navigate;
    pathRef.current = pathname;
  }, [navigate, pathname]);

  useEffect(() => {
    const onStorage = (e) => {
      if (e.storageArea && e.storageArea !== window.localStorage) return;
      const path = pathRef.current;
      const go = (to, reason) =>
        navigateRef.current(to, { replace: true, ...(reason ? { state: { reason } } : {}) });
      // key null: localStorage.clear() in another tab.
      if (e.key === AUTH_TOKEN_KEY || e.key === null) {
        const token = e.key === null ? getAuthToken() : e.newValue;
        if (!token) {
          if (!PUBLIC_PATHS.has(path)) go("/login", "signed-out-elsewhere");
        } else if (path === "/login") {
          go("/");
        }
      } else if (e.key === AUTH_LOCKED_KEY && e.newValue) {
        if (!PUBLIC_PATHS.has(path)) go("/login", "idle");
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
}
