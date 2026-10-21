import { createContext, useContext } from "react";

/**
 * Auth state shared by AuthGuard (components/AuthGuard.jsx):
 *   authReady          — the boot auth check passed (true for every consumer
 *                        inside AuthGuard, which renders children only then;
 *                        also true under DISABLE_AUTH, unlike "a token is stored")
 *   authDisabled       — the server runs with DISABLE_AUTH=1
 *   session            — SessionInfo from /api/auth/check ({id, legacy,
 *                        device_label, created_at, expires_at}) or null
 *   idleTimeoutMinutes — server idle lock (0 = off), or null when unknown
 *   refresh()          — re-run the auth check in place (e.g. after changing
 *                        the idle timeout); resolves to the check response
 * Outside AuthGuard (login, cert guide, tests) useAuth() returns the
 * signed-out default below.
 */
const SIGNED_OUT = Object.freeze({
  authReady: false,
  authDisabled: false,
  session: null,
  idleTimeoutMinutes: null,
  refresh: async () => null,
});

const AuthContext = createContext(SIGNED_OUT);

export function AuthProvider({ value, children }) {
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}
