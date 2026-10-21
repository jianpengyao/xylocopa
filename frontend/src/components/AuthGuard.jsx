import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { CERT_FLOW_KEY, CERT_FLOW_TTL_MS } from "../pages/CertGuidePage";
import {
  AuthTimeoutError,
  authCheck,
  clearAuthLock,
  clearAuthToken,
  getAuthToken,
  getMediaTokenExp,
  lockAuthSession,
  mintMediaToken,
  setMediaToken,
  withTimeout,
} from "../lib/api";
import { nextBackoff, onNetworkResume } from "../lib/reconnect";
import { serverNow } from "../lib/serverTime";
import { startPushDeviceSync } from "../lib/pushNotifications";
import { AuthProvider } from "../contexts/AuthContext";
import useActivitySignal from "../hooks/useActivitySignal";

// Server-down probe cadence (AuthGuard): 2s, growing 1.5x to a 10s cap, forever.
const SERVER_PROBE_INITIAL_MS = 2000;
const SERVER_PROBE_MAX_MS = 10000;
// An auth check (boot or probe) that has not answered by then is abandoned.
const AUTH_CHECK_TIMEOUT_MS = 8000;
// Media token upkeep: checked on resume and every 15 min, re-minted once less
// than 2 h of its ~12 h lifetime is left. Already-built URLs keep the old
// token, which stays valid until its own expiry; memoized chat attachments
// are re-stamped on the next mint (FilePreview, hooks/useMediaToken).
const MEDIA_REFRESH_INTERVAL_MS = 15 * 60 * 1000;
const MEDIA_REFRESH_MARGIN_MS = 2 * 60 * 60 * 1000;

const isServerError = (err) =>
  err instanceof TypeError
  || (typeof err?.status === "number" && err.status >= 500)
  || (err?.message && /^HTTP 5\d\d/.test(err.message));

export default function AuthGuard({ children }) {
  const navigate = useNavigate();
  const [checked, setChecked] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [serverDown, setServerDown] = useState(false);
  // The boot check has not answered within AUTH_CHECK_TIMEOUT_MS: show
  // "Still connecting…" with Retry while further attempts run.
  const [slow, setSlow] = useState(false);
  // Server runs with DISABLE_AUTH: no password anywhere, so the activity
  // signal and media-token upkeep stay off. Reported by every auth check.
  const [authDisabled, setAuthDisabled] = useState(false);
  const [session, setSession] = useState(null);
  const [idleTimeoutMinutes, setIdleTimeoutMinutes] = useState(null);
  // Latest navigate() without making it an effect dependency: under
  // <BrowserRouter>, useNavigate() returns a new function on every pathname
  // change, and the auth check used to re-run on every in-app navigation —
  // one transient failure while tapping a tab unmounted the whole app
  // behind the reconnect screen.
  const navigateRef = useRef(navigate);
  useEffect(() => { navigateRef.current = navigate; }, [navigate]);
  // Set by the server-down probe effect; lets the Retry button probe now.
  const retryNowRef = useRef(null);
  // AbortControllers of the in-flight boot check and server-down probe: a
  // Retry aborts the one in flight before starting the next.
  const checkCtlRef = useRef(null);
  const probeCtlRef = useRef(null);

  // Server-side idle lock: activity header, throttled ping, resume hint.
  useActivitySignal({ enabled: authed && !authDisabled, idleTimeoutMinutes });

  // api.js dispatches auth-expired after it already cleared (or, for idle,
  // locked) the token — and only when that took effect. Just show /login
  // with the reason for its subtitle.
  useEffect(() => {
    const handler = (e) => {
      const reason = e.detail?.reason;
      navigateRef.current("/login", { replace: true, state: reason ? { reason } : undefined });
    };
    window.addEventListener("auth-expired", handler);
    return () => window.removeEventListener("auth-expired", handler);
  }, []);

  // If the user just clicked the CA-cert download on /cert-guide, the same-tab
  // navigation can land them at "/" with no auth. Bounce them back to
  // /cert-guide instead of /login (consume once, TTL-bounded).
  const consumeCertFlowRedirect = useCallback(() => {
    const ts = sessionStorage.getItem(CERT_FLOW_KEY);
    if (!ts) return false;
    sessionStorage.removeItem(CERT_FLOW_KEY);
    if (Date.now() - Number(ts) > CERT_FLOW_TTL_MS) return false;
    navigateRef.current("/cert-guide", { replace: true });
    return true;
  }, []);

  // An authenticated check. The media token is stored before `authed` flips,
  // so the children's first render already builds media URLs with it.
  const applyAuthenticated = useCallback((r) => {
    setMediaToken(r.media_token || null, r.media_expires_at ?? null);
    clearAuthLock();
    setAuthDisabled(!!r.auth_disabled);
    setSession(r.session || null);
    setIdleTimeoutMinutes(typeof r.idle_timeout_minutes === "number" ? r.idle_timeout_minutes : null);
    setAuthed(true);
  }, []);

  // Not authenticated: an idle session keeps its token (unlocking with it
  // replaces that session); any other reason drops it. Then /login with the
  // reason — or back to /cert-guide in the middle of a cert install.
  const leaveForLogin = useCallback((r, token) => {
    const reason = r.reason || null;
    if (reason === "idle") lockAuthSession("idle");
    else if (token) clearAuthToken("authcheck-not-authenticated");
    if (!consumeCertFlowRedirect()) {
      navigateRef.current("/login", { replace: true, state: reason ? { reason } : undefined });
    }
  }, [consumeCertFlowRedirect]);

  // One auth check, abandoned after AUTH_CHECK_TIMEOUT_MS. Network failures /
  // 5xx (incl. 503 "Auth store unavailable") flip to the server-down screen,
  // whose probe keeps trying until the server answers (see below); the token
  // is kept. A timeout shows "Still connecting…" and tries again at once.
  const attemptAuth = useCallback(function attempt() {
    checkCtlRef.current?.abort();
    const ctl = new AbortController();
    checkCtlRef.current = ctl;
    const token = getAuthToken();
    withTimeout((signal) => authCheck({ signal }), AUTH_CHECK_TIMEOUT_MS, { signal: ctl.signal })
      .then((r) => {
        if (ctl.signal.aborted) return; // superseded by a Retry
        setSlow(false);
        if (r.authenticated) {
          applyAuthenticated(r);
          setServerDown(false);
          // Re-register this device's push subscription and adopt its
          // server-side switches. Never prompts: permission is asked only
          // from the bell (a user gesture — required on iOS).
          startPushDeviceSync().catch((err) => {
            console.warn("Push notification sync failed:", err);
          });
        } else if (getAuthToken() !== token) {
          // A login (here or in another tab) replaced the token while this
          // check was in flight: judge the new one instead.
          attempt();
          return;
        } else {
          leaveForLogin(r, token);
        }
        setChecked(true);
      })
      .catch((err) => {
        if (ctl.signal.aborted) return;
        if (err instanceof AuthTimeoutError) {
          setSlow(true);
          attempt();
          return;
        }
        setSlow(false);
        if (!isServerError(err)) console.warn("Auth check failed with unexpected error:", err);
        setServerDown(true);
        setChecked(true);
      });
  }, [applyAuthenticated, leaveForLogin]);

  // Once on mount — not on every route change (see navigateRef above).
  useEffect(() => {
    attemptAuth();
    return () => { checkCtlRef.current?.abort(); };
  }, [attemptAuth]);

  // Re-check in place (AuthContext.refresh), e.g. after the idle timeout
  // changed. Children stay mounted unless the session is gone.
  const refresh = useCallback(async () => {
    const token = getAuthToken();
    const r = await authCheck();
    if (r.authenticated) applyAuthenticated(r);
    else if (getAuthToken() === token) leaveForLogin(r, token);
    return r;
  }, [applyAuthenticated, leaveForLogin]);

  // Server-down probe: never gives up. Capped backoff (2s → 10s), plus an
  // immediate probe when the PWA is foregrounded or the browser reports the
  // network is back. The previous 60s give-up left a dead "Unable to reach
  // server" screen whose only exit was a full page reload — which, with the
  // old service worker, re-entered the very route that was failing.
  // Each probe is abandoned after AUTH_CHECK_TIMEOUT_MS; "Retry now" aborts
  // the one in flight (a hung probe used to swallow the tap) and probes again.
  useEffect(() => {
    if (!serverDown) return;
    let cancelled = false;
    let timer = null;
    let delay = SERVER_PROBE_INITIAL_MS;
    const probe = async () => {
      timer = null;
      probeCtlRef.current?.abort();
      const ctl = new AbortController();
      probeCtlRef.current = ctl;
      try {
        await withTimeout((signal) => authCheck({ signal }), AUTH_CHECK_TIMEOUT_MS, { signal: ctl.signal });
        if (cancelled || ctl.signal.aborted) return;
        // Server is back — soft reset: re-run auth flow without unmounting the app
        setServerDown(false);
        setChecked(false);           // causes the Loading... state to render briefly
        attemptAuth();               // re-run the normal auth flow — this will set checked+authed again
      } catch {
        if (cancelled || ctl.signal.aborted) return; // a newer probe took over
        timer = setTimeout(probe, delay);
        delay = nextBackoff(delay, { max: SERVER_PROBE_MAX_MS });
      }
    };
    const probeNow = () => {
      if (cancelled) return;
      clearTimeout(timer);
      timer = null;
      delay = SERVER_PROBE_INITIAL_MS;
      probe();
    };
    retryNowRef.current = probeNow;
    timer = setTimeout(probe, SERVER_PROBE_INITIAL_MS);
    const offResume = onNetworkResume(probeNow);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      probeCtlRef.current?.abort();
      offResume();
      retryNowRef.current = null;
    };
  }, [serverDown, attemptAuth]);

  // Media-token upkeep (skipped under DISABLE_AUTH: no media tokens).
  useEffect(() => {
    if (!authed || authDisabled) return undefined;
    let busy = false;
    const maybeRefresh = () => {
      if (busy) return;
      const exp = getMediaTokenExp();
      if (exp != null && exp * 1000 - serverNow() >= MEDIA_REFRESH_MARGIN_MS) return;
      busy = true;
      mintMediaToken()
        .catch((err) => console.warn("Media token refresh failed:", err))
        .finally(() => { busy = false; });
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") maybeRefresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    const id = setInterval(maybeRefresh, MEDIA_REFRESH_INTERVAL_MS);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      clearInterval(id);
    };
  }, [authed, authDisabled]);

  const authValue = useMemo(() => ({
    authReady: authed,
    authDisabled,
    session,
    idleTimeoutMinutes,
    refresh,
  }), [authed, authDisabled, session, idleTimeoutMinutes, refresh]);

  if (!checked) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-page gap-4">
        <div className="animate-pulse text-dim">{slow ? "Still connecting…" : "Loading..."}</div>
        {slow && (
          <>
            <button
              type="button"
              onClick={() => attemptAuth()}
              className="px-4 py-2 rounded-lg bg-accent text-white text-sm hover:bg-accent transition-colors"
            >
              Retry
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="text-dim text-xs underline"
            >
              Reload page
            </button>
          </>
        )}
      </div>
    );
  }

  if (serverDown) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-page gap-4">
        <p className="text-label text-sm animate-pulse">Reconnecting to server...</p>
        <button
          type="button"
          onClick={() => retryNowRef.current?.()}
          className="px-4 py-2 rounded-lg bg-accent text-white text-sm hover:bg-accent transition-colors"
        >
          Retry now
        </button>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="text-dim text-xs underline"
        >
          Reload page
        </button>
      </div>
    );
  }

  return authed ? <AuthProvider value={authValue}>{children}</AuthProvider> : null;
}
