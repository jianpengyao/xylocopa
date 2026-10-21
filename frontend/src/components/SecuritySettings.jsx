import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  authChangePassword,
  authLogout,
  clearAuthToken,
  deviceLabel,
  fetchAuthSessions,
  fetchAuthSettings,
  revokeAuthSession,
  revokeOtherSessions,
  updateAuthSettings,
} from "../lib/api";
import { serverNow } from "../lib/serverTime";
import { useAuth } from "../contexts/AuthContext";
import { useToast } from "../contexts/ToastContext";

// Monitor > Security: server idle lock, signed-in devices, password, sign
// out. Everything here talks to /api/settings/auth and /api/auth/*; under
// DISABLE_AUTH the settings are stored and take effect once auth is on.

const DEFAULT_IDLE_CHOICES = [0, 5, 15, 30, 60, 120, 240, 480, 1440];
const MIN_PASSWORD_LENGTH = 8; // same as the server
const THUMBNAIL_CACHE = "thumbnail-cache"; // sw-runtime-caching.js

function idleLabel(minutes, defaultMinutes) {
  let label;
  if (minutes === 0) label = "Off";
  else if (minutes > 60 && minutes % 60 === 0) label = `${minutes / 60} h`;
  else label = `${minutes} min`;
  return minutes === defaultMinutes ? `${label} (default)` : label;
}

// Epoch seconds (server clock) → "just now" / "5 min ago" / "3 h ago" / "2 d ago".
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(serverNow() / 1000 - ts));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

const inputClass =
  "w-full px-3 py-2 rounded-lg bg-input border border-divider text-sm text-heading placeholder-hint focus:outline-none focus:border-accent";
const smallButtonClass =
  "px-3 py-1.5 rounded-lg text-xs font-medium border border-divider text-label hover:bg-hover transition-colors disabled:opacity-40 disabled:cursor-not-allowed";

export default function SecuritySettings() {
  const { authDisabled, refresh } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();

  const [settings, setSettings] = useState(null);
  const [settingsError, setSettingsError] = useState("");
  const [savingIdle, setSavingIdle] = useState(false);

  const [sessions, setSessions] = useState(null); // SessionRow[]
  const [sessionsError, setSessionsError] = useState("");
  const [busy, setBusy] = useState(null); // session id being revoked, or "others"

  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwError, setPwError] = useState("");
  const [pwBusy, setPwBusy] = useState(false);

  const [signingOut, setSigningOut] = useState(false);

  const loadSettings = useCallback(async () => {
    try {
      setSettings(await fetchAuthSettings());
      setSettingsError("");
    } catch (err) {
      setSettingsError(err.message || "Could not load security settings");
    }
  }, []);

  const loadSessions = useCallback(async () => {
    try {
      const r = await fetchAuthSessions();
      setSessions(Array.isArray(r?.sessions) ? r.sessions : []);
      setSessionsError("");
    } catch (err) {
      setSessionsError(err.message || "Could not load devices");
    }
  }, []);

  useEffect(() => {
    loadSettings();
    loadSessions();
  }, [loadSettings, loadSessions]);

  // This browser's session is gone (signed out here, or revoked from the
  // list): drop it locally, purge cached thumbnails, show the lock screen.
  // Forced: a deliberate sign-out right after a login is not one of the
  // stale callbacks the post-login grace window guards against.
  const leaveSignedOut = useCallback(async () => {
    clearAuthToken("logout", { force: true });
    try {
      if (typeof caches !== "undefined") await caches.delete(THUMBNAIL_CACHE);
    } catch { /* Cache Storage unavailable */ }
    navigate("/login", { replace: true, state: { reason: "signed-out" } });
  }, [navigate]);

  const handleIdleChange = async (e) => {
    const minutes = Number(e.target.value);
    setSavingIdle(true);
    try {
      setSettings(await updateAuthSettings({ idle_timeout_minutes: minutes }));
      setSettingsError("");
      // AuthGuard's idleTimeoutMinutes drives the activity signal.
      refresh().catch(() => {});
    } catch (err) {
      setSettingsError(err.message || "Could not save the idle timeout");
    } finally {
      setSavingIdle(false);
    }
  };

  const handleRevoke = async (id) => {
    setBusy(id);
    try {
      const r = await revokeAuthSession(id);
      if (r?.current) {
        await leaveSignedOut();
        return;
      }
    } catch (err) {
      setSessionsError(err.message || "Could not sign that device out");
    } finally {
      setBusy(null);
    }
    loadSessions();
  };

  const handleRevokeOthers = async () => {
    setBusy("others");
    try {
      const r = await revokeOtherSessions();
      toast.success(r?.revoked ? `Signed out ${r.revoked} other device${r.revoked === 1 ? "" : "s"}` : "No other devices signed in");
    } catch (err) {
      setSessionsError(err.message || "Could not sign out other devices");
    } finally {
      setBusy(null);
    }
    loadSessions();
  };

  const handleChangePassword = async (e) => {
    e.preventDefault();
    setPwError("");
    if (newPw.length < MIN_PASSWORD_LENGTH) {
      setPwError(`New password must be at least ${MIN_PASSWORD_LENGTH} characters`);
      return;
    }
    if (newPw !== confirmPw) {
      setPwError("New passwords don't match");
      return;
    }
    setPwBusy(true);
    try {
      // Stores the replacement session itself (the server revoked this
      // one's old token too); a wrong current password is a 403 that never
      // signs anyone out.
      await authChangePassword(currentPw, newPw, { deviceLabel: deviceLabel() });
      setCurrentPw("");
      setNewPw("");
      setConfirmPw("");
      toast.success("Password changed — other devices signed out");
      refresh().catch(() => {});
      loadSessions();
    } catch (err) {
      setPwError(err.message || "Could not change the password");
    } finally {
      setPwBusy(false);
    }
  };

  const handleSignOut = async () => {
    setSigningOut(true);
    try {
      await authLogout(); // idempotent; works on a locked session too
    } catch (err) {
      console.warn("Logout request failed, signing out locally:", err);
    }
    await leaveSignedOut();
  };

  const choices = settings?.choices?.length ? settings.choices : DEFAULT_IDLE_CHOICES;
  const defaultIdle = settings?.default_idle_timeout_minutes ?? 30;
  const passwordSet = settings ? settings.password_set !== false : true;
  const others = (sessions || []).filter((s) => !s.current);

  return (
    <section>
      <h2 className="text-xs font-semibold text-dim uppercase tracking-wider mb-2">Security</h2>
      <div className="space-y-3">
        {authDisabled && (
          <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3">
            <p className="text-xs text-amber-600 dark:text-amber-400 leading-relaxed">
              Password protection is off on this server (DISABLE_AUTH=1). Settings below take effect when it is turned on.
            </p>
          </div>
        )}

        {/* Idle lock */}
        <div className="rounded-xl bg-surface shadow-card p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-heading">Lock after inactivity</h3>
              <p className="text-xs text-dim mt-1 leading-relaxed">
                A device with no taps or keystrokes for this long must enter the password again.
              </p>
            </div>
            <select
              aria-label="Idle timeout"
              value={settings?.idle_timeout_minutes ?? ""}
              onChange={handleIdleChange}
              disabled={!settings || savingIdle}
              className="text-xs bg-input text-heading rounded-lg px-2 py-1 border border-divider shrink-0 disabled:opacity-50"
            >
              {!settings && <option value="">…</option>}
              {choices.map((m) => (
                <option key={m} value={m}>{idleLabel(m, defaultIdle)}</option>
              ))}
            </select>
          </div>
          {settingsError && <p className="text-xs text-danger mt-2">{settingsError}</p>}
        </div>

        {/* Signed-in devices */}
        <div className="rounded-xl bg-surface shadow-card p-4">
          <h3 className="text-sm font-medium text-heading">Signed-in devices</h3>
          {sessions === null && !sessionsError && <p className="text-xs text-faint mt-2">Loading...</p>}
          {sessions !== null && sessions.length === 0 && (
            <p className="text-xs text-dim mt-2">No signed-in devices.</p>
          )}
          {sessions !== null && sessions.length > 0 && (
            <ul className="mt-2" data-testid="device-list">
              {sessions.map((s) => (
                <li key={s.id} className="py-2 flex items-center gap-3 min-w-0 border-t border-divider first:border-t-0">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-sm text-body truncate">{s.device_label || "Unknown device"}</span>
                      {s.current && (
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold accent-tint-15 text-accent">This device</span>
                      )}
                      {s.state === "idle" && (
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-600 dark:text-amber-400">Locked</span>
                      )}
                    </div>
                    <p className="text-[11px] text-dim mt-0.5 truncate">
                      Last seen {ago(s.last_seen_at)}
                      {s.last_ip ? ` · ${s.last_ip}` : ""}
                    </p>
                  </div>
                  {!s.current && (
                    <button
                      type="button"
                      onClick={() => handleRevoke(s.id)}
                      disabled={busy !== null}
                      className={smallButtonClass}
                    >
                      {busy === s.id ? "Revoking…" : "Revoke"}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {sessionsError && <p className="text-xs text-danger mt-2">{sessionsError}</p>}
          <button
            type="button"
            onClick={handleRevokeOthers}
            disabled={busy !== null || others.length === 0}
            className={`${smallButtonClass} mt-3`}
          >
            {busy === "others" ? "Signing out…" : "Sign out other devices"}
          </button>
        </div>

        {/* Password */}
        <div className="rounded-xl bg-surface shadow-card p-4">
          <h3 className="text-sm font-medium text-heading">Change password</h3>
          {passwordSet ? (
            <form onSubmit={handleChangePassword} className="mt-2 space-y-2" autoComplete="on">
              <input
                type="password"
                autoComplete="current-password"
                aria-label="Current password"
                placeholder="Current password"
                value={currentPw}
                onChange={(e) => setCurrentPw(e.target.value)}
                className={inputClass}
              />
              <input
                type="password"
                autoComplete="new-password"
                aria-label="New password"
                placeholder={`New password (at least ${MIN_PASSWORD_LENGTH} characters)`}
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                className={inputClass}
              />
              <input
                type="password"
                autoComplete="new-password"
                aria-label="Confirm new password"
                placeholder="Confirm new password"
                value={confirmPw}
                onChange={(e) => setConfirmPw(e.target.value)}
                className={inputClass}
              />
              {pwError && <p className="text-xs text-danger">{pwError}</p>}
              <p className="text-[11px] text-dim">Every other device is signed out; this one stays signed in.</p>
              <button
                type="submit"
                disabled={pwBusy || !currentPw || !newPw || !confirmPw}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-90 text-white hover:bg-accent transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {pwBusy ? "Changing…" : "Change password"}
              </button>
            </form>
          ) : (
            <p className="text-xs text-dim mt-1 leading-relaxed">
              No password is set yet. You will be asked to create one at sign-in once password protection is on.
            </p>
          )}
        </div>

        {/* Sign out (nothing to sign out of while auth is off) */}
        {!authDisabled && (
          <div className="rounded-xl bg-surface shadow-card p-4 flex items-center justify-between gap-4">
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-heading">Sign out this device</h3>
              <p className="text-xs text-dim mt-1">Ends this session and clears cached thumbnails.</p>
            </div>
            <button
              type="button"
              onClick={handleSignOut}
              disabled={signingOut}
              className="px-3 py-1.5 rounded-lg text-xs font-medium border danger-edge-40 text-danger hover:danger-tint-25 transition-colors shrink-0 disabled:opacity-40"
            >
              {signingOut ? "Signing out…" : "Sign out"}
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
