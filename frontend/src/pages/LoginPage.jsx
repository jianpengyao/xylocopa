import { useState, useEffect, useRef, useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  AuthTimeoutError,
  authCheck,
  authLogin,
  authSetPassword,
  deviceLabel,
  getAuthLockReason,
  setAuthSession,
  withTimeout,
} from "../lib/api";
import beeLogo from "../assets/xylocopa-bee.svg";

// Same minimum as the server (set-password / change-password).
const MIN_PASSWORD_LENGTH = 8;
const CHECK_TIMEOUT_MS = 8000;

function formatMinutes(minutes) {
  if (minutes >= 60 && minutes % 60 === 0) return `${minutes / 60} h`;
  return `${minutes} min`;
}

// Why the app is on the lock screen. `reason` comes from the redirect
// (auth-expired / auth check / another tab) or, after a reload, from the
// server's check.
function lockedSubtitle(reason, idleMinutes) {
  switch (reason) {
    case "idle":
      return idleMinutes > 0
        ? `Locked after ${formatMinutes(idleMinutes)} of inactivity`
        : "Locked after inactivity";
    case "revoked":
      return "Signed out from another device";
    case "expired":
      return "Session expired";
    case "signed-out-elsewhere":
      return "Signed out in another tab";
    case "signed-out":
      return "Signed out";
    default:
      return "Locked";
  }
}

export default function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [needsSetup, setNeedsSetup] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [shake, setShake] = useState(false);
  // The mount check did not answer in time: the form stays usable, with a
  // Retry for the check.
  const [checkSlow, setCheckSlow] = useState(false);
  const [checkReason, setCheckReason] = useState(null);
  const [idleMinutes, setIdleMinutes] = useState(null);
  const navigateRef = useRef(navigate);
  useEffect(() => { navigateRef.current = navigate; }, [navigate]);
  const checkCtlRef = useRef(null);

  // Already signed in (or auth is off)? Then leave. Otherwise learn whether
  // this is first-run setup and why the session ended. Abandoned after
  // CHECK_TIMEOUT_MS; Retry aborts the check in flight and starts another.
  const runCheck = useCallback(() => {
    checkCtlRef.current?.abort();
    const ctl = new AbortController();
    checkCtlRef.current = ctl;
    setCheckSlow(false);
    withTimeout((signal) => authCheck({ signal }), CHECK_TIMEOUT_MS, { signal: ctl.signal })
      .then((r) => {
        if (ctl.signal.aborted) return;
        if (r.authenticated) {
          navigateRef.current("/", { replace: true });
          return;
        }
        setNeedsSetup(!!r.needs_setup);
        setCheckReason(r.reason || null);
        if (typeof r.idle_timeout_minutes === "number") setIdleMinutes(r.idle_timeout_minutes);
      })
      .catch((err) => {
        if (ctl.signal.aborted) return;
        if (err instanceof AuthTimeoutError) setCheckSlow(true);
        else console.error("authCheck failed:", err);
      })
      .finally(() => { if (!ctl.signal.aborted) setLoading(false); });
  }, []);

  useEffect(() => {
    runCheck();
    return () => { checkCtlRef.current?.abort(); };
  }, [runCheck]);

  const reason = location.state?.reason || checkReason || getAuthLockReason();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");

    if (needsSetup) {
      if (password !== confirmPassword) {
        setError("Passwords don't match");
        triggerShake();
        return;
      }
      if (password.length < MIN_PASSWORD_LENGTH) {
        setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters`);
        triggerShake();
        return;
      }
    }

    setSubmitting(true);
    try {
      const label = deviceLabel();
      const res = needsSetup
        ? await authSetPassword(password, { deviceLabel: label })
        : await authLogin(password, { deviceLabel: label });
      setAuthSession(res);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message || "Login failed");
      triggerShake();
    } finally {
      setSubmitting(false);
    }
  };

  const triggerShake = () => {
    setShake(true);
    setTimeout(() => setShake(false), 500);
  };

  if (loading) {
    return (
      <div className="fixed inset-0 z-50 bg-page/80 backdrop-blur-xl flex items-center justify-center">
        <div className="animate-pulse text-dim">Loading...</div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center px-4">
      {/* Soft color blobs behind the glass — give the blur something to refract */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none">
        <div
          className="absolute top-[20%] left-[30%] w-[420px] h-[420px] rounded-full opacity-60"
          style={{ background: "radial-gradient(circle, rgba(6,182,212,0.35) 0%, transparent 65%)" }}
        />
        <div
          className="absolute bottom-[15%] right-[25%] w-[380px] h-[380px] rounded-full opacity-50"
          style={{ background: "radial-gradient(circle, rgba(127,119,221,0.30) 0%, transparent 65%)" }}
        />
        <div
          className="absolute top-[55%] left-[60%] w-[300px] h-[300px] rounded-full opacity-40"
          style={{ background: "radial-gradient(circle, rgba(168,85,247,0.25) 0%, transparent 65%)" }}
        />
      </div>

      <div className={`relative z-10 w-full max-w-md ${shake ? "animate-shake" : ""}`}>
        {/* Single horizontal liquid-glass card: bee | form */}
        <div className="glass-bar rounded-3xl p-5 flex items-center gap-5">
          <img
            src={beeLogo}
            alt="Xylocopa"
            className="w-24 h-24 shrink-0 select-none drop-shadow-[0_2px_8px_rgba(0,0,0,0.15)]"
            draggable={false}
          />

          <div className="flex-1 min-w-0">
            <h1 className="text-base font-semibold text-heading leading-tight">Xylocopa</h1>
            <p className="text-xs text-dim mb-3">
              {needsSetup ? "Set a password to get started" : lockedSubtitle(reason, idleMinutes)}
            </p>
            {checkSlow && (
              <p className="text-xs text-dim mb-2">
                Server is slow to respond.{" "}
                <button type="button" onClick={runCheck} className="text-accent hover:underline">
                  Retry
                </button>
              </p>
            )}

            <form onSubmit={handleSubmit} autoComplete="on" className="space-y-2">
              {/* Hidden username for iOS autofill credential matching */}
              <input
                type="text"
                name="username"
                autoComplete="username"
                value="user"
                readOnly
                aria-hidden="true"
                tabIndex={-1}
                style={{ position: "absolute", width: 0, height: 0, overflow: "hidden", opacity: 0 }}
              />
              <input
                type="password"
                name="password"
                id="password"
                autoComplete={needsSetup ? "new-password" : "current-password"}
                value={password}
                onChange={(e) => { setPassword(e.target.value); setError(""); }}
                placeholder={needsSetup ? "New password" : "Password"}
                autoFocus
                className="w-full px-3 py-2 rounded-lg bg-black/5 dark:bg-black/30 border-0 text-sm text-heading placeholder-dim focus:outline-none transition-shadow"
                style={{ boxShadow: "inset 0 1.5px 3px rgba(0,0,0,0.18), inset 0 -0.5px 0 rgba(255,255,255,0.4)" }}
              />

              {needsSetup && (
                <input
                  type="password"
                  name="confirm-password"
                  id="confirm-password"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(e) => { setConfirmPassword(e.target.value); setError(""); }}
                  placeholder="Confirm password"
                  className="w-full px-3 py-2 rounded-lg bg-black/5 dark:bg-black/30 border-0 text-sm text-heading placeholder-dim focus:outline-none transition-shadow"
                  style={{ boxShadow: "inset 0 1.5px 3px rgba(0,0,0,0.18), inset 0 -0.5px 0 rgba(255,255,255,0.4)" }}
                />
              )}

              {error && (
                <p className="text-danger text-xs">{error}</p>
              )}

              <button
                type="submit"
                disabled={submitting || !password}
                className="w-full py-2 rounded-lg text-sm font-medium transition-all bg-accent-90 text-white hover:bg-accent disabled:opacity-40 disabled:cursor-not-allowed active:scale-[0.98]"
              >
                {submitting
                  ? "..."
                  : needsSetup
                    ? "Set Password"
                    : "Unlock"}
              </button>
            </form>
          </div>
        </div>

        {/* Cert install hint for mobile */}
        <p className="text-center text-xs text-dim mt-4">
          First time on this device?{" "}
          <a
            href="/cert-guide"
            className="text-accent hover:underline"
          >
            Install CA certificate
          </a>
        </p>
      </div>
    </div>
  );
}
