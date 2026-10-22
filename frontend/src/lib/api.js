/** Centralized API wrapper for Xylocopa. */

import { calibrate, serverNow } from "./serverTime";
import { isUserActive } from "./activity";

const BASE = "";
// Opaque server session token (`xys_…`). Travels only as a Bearer header —
// never in a URL.
export const AUTH_TOKEN_KEY = "cc-auth-token";
const TOKEN_KEY = AUTH_TOKEN_KEY;
const LOGIN_TS_KEY = "cc-login-ts";
// Media token, JSON {token, exp}: a session-bound `scope=media` JWT that the
// server accepts as ?token= on GET/HEAD of /api/files, /api/thumbs and
// /api/uploads only (<img>, <video>, downloads). `exp` is epoch seconds.
export const MEDIA_TOKEN_KEY = "cc-media-token";
// Set (to the reason, "idle") while the stored session is locked by the
// server's idle timeout. The token is kept: logging in with it as Bearer
// retires the locked session instead of leaving it in the device list.
export const AUTH_LOCKED_KEY = "cc-auth-locked";

// Timestamp of the most recent setAuthSession() call — used to protect the
// fresh token from being nuked by stale callbacks during the first few seconds
// after login (e.g. in-flight 401s racing with React renders).
// Persisted in localStorage so the grace period survives iOS PWA page reloads.
let _lastLoginTs = Number(localStorage.getItem(LOGIN_TS_KEY) || 0);
const _LOGIN_GRACE_MS = 3000; // 3-second grace window after login
// Turn-on requests of setAuthProtection() still waiting for their answer.
// The server switches auth on before that answer (this tab's new session)
// arrives, so a token-less request failing meanwhile is not a logout.
let _protectionTurningOn = 0;
// A media token this close to its expiry is treated as gone, so a URL built
// now does not die before the browser fetches it.
const _MEDIA_EXPIRY_MARGIN_MS = 30 * 1000;

export function getAuthToken() {
  return localStorage.getItem(TOKEN_KEY);
}

/** Returns the persisted login timestamp (epoch ms), or 0 if never set. */
export function getLoginTs() {
  return _lastLoginTs || Number(localStorage.getItem(LOGIN_TS_KEY) || 0);
}

/** Store a bare session token (no media token). Prefer setAuthSession(). */
export function setAuthToken(token) {
  setAuthSession({ token });
}

/**
 * Store a LoginResponse (login, set-password, change-password): the session
 * token, its media token and the login timestamp, and drop any idle-lock flag.
 * The token is written last so other tabs (useAuthSync) see a complete
 * session when its storage event arrives.
 */
export function setAuthSession(resp) {
  if (!resp?.token) return;
  _lastLoginTs = Date.now();
  localStorage.setItem(LOGIN_TS_KEY, String(_lastLoginTs));
  setMediaToken(resp.media_token || null, resp.media_expires_at ?? null);
  localStorage.removeItem(AUTH_LOCKED_KEY);
  localStorage.setItem(TOKEN_KEY, resp.token);
}

// getMediaToken() runs while rendering every media URL; re-parse only when
// the stored value changes.
let _mediaRaw = null;
let _mediaParsed = null;

function _readMedia() {
  let raw;
  try { raw = localStorage.getItem(MEDIA_TOKEN_KEY); } catch { return null; }
  if (!raw) return null;
  if (raw !== _mediaRaw) {
    _mediaRaw = raw;
    try { _mediaParsed = JSON.parse(raw); } catch { _mediaParsed = null; }
  }
  return _mediaParsed?.token ? _mediaParsed : null;
}

/** The stored media token, or null when missing or (about to be) expired. */
export function getMediaToken() {
  const m = _readMedia();
  if (!m) return null;
  if (typeof m.exp === "number" && m.exp * 1000 - serverNow() <= _MEDIA_EXPIRY_MARGIN_MS) return null;
  return m.token;
}

/** Expiry (epoch seconds) of the stored media token, or null. */
export function getMediaTokenExp() {
  const m = _readMedia();
  return typeof m?.exp === "number" ? m.exp : null;
}

// Fired on window after setMediaToken() stores or drops the media token.
export const MEDIA_TOKEN_EVENT = "media-token-changed";

/** Store (or, with a falsy token, remove) the media token. */
export function setMediaToken(token, exp) {
  try {
    if (!token) localStorage.removeItem(MEDIA_TOKEN_KEY);
    else localStorage.setItem(MEDIA_TOKEN_KEY, JSON.stringify({ token, exp: typeof exp === "number" ? exp : null }));
  } catch { /* storage unavailable: media URLs go out without a token */ }
  window.dispatchEvent(new Event(MEDIA_TOKEN_EVENT));
}

/**
 * Call `cb` when the media token changes, in this tab (setMediaToken) or
 * another (storage event). Returns an unsubscribe function.
 */
export function subscribeMediaToken(cb) {
  const onStorage = (e) => {
    if (e.key === MEDIA_TOKEN_KEY || e.key === null) cb();
  };
  window.addEventListener(MEDIA_TOKEN_EVENT, cb);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(MEDIA_TOKEN_EVENT, cb);
    window.removeEventListener("storage", onStorage);
  };
}

/** Reason the stored session is locked ("idle"), or null. */
export function getAuthLockReason() {
  try { return localStorage.getItem(AUTH_LOCKED_KEY) || null; } catch { return null; }
}

/**
 * Mark the stored session as locked without dropping its token (the server
 * answered `idle`). Returns false when there is no token to lock.
 */
export function lockAuthSession(reason = "idle") {
  if (!getAuthToken()) return false;
  try { localStorage.setItem(AUTH_LOCKED_KEY, reason); } catch { /* storage unavailable */ }
  return true;
}

/** Drop a stale lock flag (the server accepts the session again). */
export function clearAuthLock() {
  try { localStorage.removeItem(AUTH_LOCKED_KEY); } catch { /* storage unavailable */ }
}

/**
 * Clear the auth token (and its media token and lock flag).  Returns true if
 * the token was actually removed, false if the call was blocked by the
 * post-login grace window. `force`: the user signed this device out on
 * purpose — no grace window applies.
 */
export function clearAuthToken(reason, { force = false } = {}) {
  // During the post-login grace window, refuse to clear the token.
  // This prevents stale visibilitychange handlers, frozen timers, and
  // in-flight 401 responses from racing with a fresh login.
  // Check both in-memory AND persisted timestamp (survives iOS PWA reloads).
  const loginTs = _lastLoginTs || Number(localStorage.getItem(LOGIN_TS_KEY) || 0);
  const sinceLogin = Date.now() - loginTs;
  if (!force && loginTs && sinceLogin < _LOGIN_GRACE_MS) {
    console.warn("[auth] clearAuthToken BLOCKED during login grace period (%dms since login, reason=%s)", sinceLogin, reason || "unknown");
    _sendAuthDiag("clear_blocked", reason || "unknown", sinceLogin);
    return false;
  }
  console.warn("[auth] clearAuthToken called (reason=%s)", reason || "unknown");
  _sendAuthDiag("clear", reason || "unknown", sinceLogin);
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(LOGIN_TS_KEY);
  localStorage.removeItem(MEDIA_TOKEN_KEY);
  localStorage.removeItem(AUTH_LOCKED_KEY);
  _lastLoginTs = 0;
  return true;
}

/** Fire-and-forget diagnostic event to the server for post-mortem debugging. */
function _sendAuthDiag(action, reason, sinceLoginMs) {
  try {
    fetch("/api/debug/auth-diag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action, reason,
        since_login_ms: sinceLoginMs,
        path: window.location.pathname,
        has_token: !!localStorage.getItem(TOKEN_KEY),
        ts: Date.now(),
      }),
    }).catch(() => {});
  } catch { /* best-effort */ }
}

/**
 * Low-level fetch with auth headers. Returns the raw Response object.
 * Use this when you need to inspect status codes or handle non-JSON responses.
 */
export async function authedFetch(url, opts = {}) {
  const headers = { ...opts.headers };
  const token = getAuthToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  if (isUserActive()) headers["X-User-Activity"] = "1";
  return fetch(`${BASE}${url}`, { ...opts, headers });
}

/**
 * The session failed (a 401 carrying X-Auth-Reason, or a WebSocket 4001).
 * Message is kept as "Not authenticated": the global error toast (main.jsx)
 * skips it, and callers that print err.message show the same text as before.
 */
export class AuthError extends Error {
  constructor(reason) {
    super("Not authenticated");
    this.name = "AuthError";
    this.reason = reason || null;
  }
}

/** withTimeout() gave up: the server did not answer in time. */
export class AuthTimeoutError extends Error {
  constructor(ms) {
    super(`No answer from the server after ${Math.round(ms / 1000)}s`);
    this.name = "AuthTimeoutError";
  }
}

/**
 * React to a failed session: reason ∈ missing|invalid|expired|revoked|idle.
 *   - The stored token changed since the failing request was sent (a newer
 *     login won the race): no-op.
 *   - idle: keep the token (logging in with it replaces the locked session)
 *     and flag the lock.
 *   - otherwise: clear the token, unless the post-login grace window refuses.
 * `auth-expired` (detail {reason}) is dispatched only when that took effect
 * and the page is not already /login. Always rejects with an AuthError.
 * `requestToken` omitted (WebSocket close, activity hint) means "the current
 * token"; null means the request carried no token.
 */
export async function expireSession(reason, { requestToken, source } = {}) {
  const current = getAuthToken();
  if (requestToken !== undefined && current && current !== requestToken) {
    throw new AuthError(reason);
  }
  // Sent without a token while this tab is turning password protection on:
  // the session it is about to receive covers it (setAuthProtection).
  if (requestToken === null && _protectionTurningOn > 0) {
    throw new AuthError(reason);
  }
  let effective;
  if (reason === "idle") {
    lockAuthSession("idle");
    effective = true;
  } else {
    if (source) console.warn("[auth] session failed (reason=%s, source=%s)", reason, source);
    effective = clearAuthToken(`401:${reason}`);
  }
  if (effective && window.location.pathname !== "/login") {
    window.dispatchEvent(new CustomEvent("auth-expired", { detail: { reason } }));
  }
  throw new AuthError(reason);
}

/**
 * A 401 from request()/formDataRequest(). Only a 401 carrying X-Auth-Reason
 * is a session failure (rejects via expireSession). Any other 401 — e.g. a
 * wrong preview token — returns, and the caller throws a plain Error(detail)
 * without logging anyone out.
 */
async function handle401(res, requestToken, source) {
  const reason = res.headers?.get?.("X-Auth-Reason");
  if (!reason) return;
  await expireSession(reason, { requestToken, source });
}

/**
 * Run `promiseFn(signal)` with an AbortController that fires after `ms`.
 * Rejects with AuthTimeoutError on timeout — even if promiseFn ignores the
 * signal — and with an AbortError when the optional outer `signal` aborts
 * first (a Retry superseding this attempt).
 */
export function withTimeout(promiseFn, ms, { signal } = {}) {
  const ctl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, ms);
  const onOuterAbort = () => ctl.abort();
  if (signal) {
    if (signal.aborted) ctl.abort();
    else signal.addEventListener("abort", onOuterAbort, { once: true });
  }
  return new Promise((resolve, reject) => {
    const fail = (err) => reject(timedOut ? new AuthTimeoutError(ms) : err);
    if (ctl.signal.aborted) {
      fail(new DOMException("Aborted", "AbortError"));
      return;
    }
    ctl.signal.addEventListener("abort", () => fail(new DOMException("Aborted", "AbortError")), { once: true });
    Promise.resolve().then(() => promiseFn(ctl.signal)).then(resolve, fail);
  }).finally(() => {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onOuterAbort);
  });
}

const _API_SLOW_MS = 300;

// --- Frontend log mirroring ---
// Mirror selected console.log lines to the backend so they land in
// logs/frontend-debug.log. Useful for diagnosing render/timing issues
// without copy-pasting out of the browser console.
// Off by default: with the mirror on, every open client POSTed a batch every
// 500 ms (37+ requests/min, 16% of all API calls, measured 2026-09-22).
// Toggle on:  localStorage.setItem("ah:clog", "1"); location.reload();
const _clogMirror = (() => { try { return localStorage.getItem("ah:clog") === "1"; } catch { return false; } })();
let _clogBuffer = [];
let _clogFlushTimer = null;
function _flushClog() {
  if (_clogBuffer.length === 0) return;
  const batch = _clogBuffer;
  _clogBuffer = [];
  _clogFlushTimer = null;
  const token = getAuthToken();
  const headers = { "Content-Type": "text/plain" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // Best-effort fire-and-forget — never block UI on log delivery.
  fetch(`${BASE}/api/debug/frontend-log`, {
    method: "POST",
    headers,
    body: batch.join("\n"),
    keepalive: true,
  }).catch(() => { /* ignore */ });
}
export function clog(...args) {
  // eslint-disable-next-line no-console
  console.log(...args);
  if (!_clogMirror) return;
  const line = args.map((a) => typeof a === "string" ? a : (() => { try { return JSON.stringify(a); } catch { return String(a); } })()).join(" ");
  _clogBuffer.push(`${new Date().toISOString().slice(11, 23)} ${line}`);
  if (_clogFlushTimer) return;
  _clogFlushTimer = setTimeout(_flushClog, 500);
}

function _logTiming(method, url, status, durMs) {
  const tag = durMs >= _API_SLOW_MS ? "[api SLOW]" : "[api]";
  // eslint-disable-next-line no-console
  console.log(`${tag} ${method} ${url} → ${status} ${durMs.toFixed(0)}ms`);
}

// `opts.activity`: true forces `X-User-Activity: 1`, false suppresses it;
// left out, the header rides along only within a few seconds of a real user
// gesture (lib/activity.js), so background polls never keep a session alive.
async function request(url, opts = {}) {
  const { activity, ...fetchOpts } = opts;
  const headers = { "Content-Type": "application/json", ...fetchOpts.headers };
  const token = getAuthToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  if (activity ?? isUserActive()) headers["X-User-Activity"] = "1";

  const method = fetchOpts.method || "GET";
  const t0 = performance.now();
  const res = await fetch(`${BASE}${url}`, { ...fetchOpts, headers });
  _logTiming(method, url, res.status, performance.now() - t0);

  if (res.status === 401) await handle401(res, token, url);

  // Calibrate clock offset from HTTP Date header (works before WS connects)
  const serverDate = res.headers.get("Date");
  if (serverDate) calibrate(serverDate);

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    const msg = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map(e => e.msg || JSON.stringify(e)).join("; ")
      : `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// --- Auth ---
// /api/auth/check never answers 401: {authenticated, needs_setup,
// auth_disabled, reason, session, idle_timeout_minutes, idle_remaining_s,
// media_token, media_expires_at, server_time}. `activity` as for request().
export const authCheck = ({ signal, activity } = {}) =>
  request("/api/auth/check", { method: "POST", signal, activity });

/**
 * Short device name for the signed-in devices list, e.g. "iPhone · Safari",
 * or "iPhone · Installed app" for the home-screen PWA. null when nothing is
 * recognisable (the server then derives one from the User-Agent).
 */
export function deviceLabel() {
  const nav = typeof navigator !== "undefined" ? navigator : {};
  const ua = nav.userAgent || "";
  let os = "";
  if (/iPhone|iPod/.test(ua)) os = "iPhone";
  else if (/iPad/.test(ua)) os = "iPad";
  // iPadOS 13+ Safari reports a Mac User-Agent, and so does an iPhone set to
  // "Request Desktop Website"; touch support tells them from a Mac, and the
  // screen from each other (short side: iPhone ≤ 440 pt, iPad ≥ 744 pt).
  // Screen size unknown (0): iPad, as before.
  else if (/Macintosh/.test(ua) && nav.maxTouchPoints > 1) {
    const side = screenShortSide();
    os = side > 0 && side < 600 ? "iPhone" : "iPad";
  }
  else if (/Android/.test(ua)) os = "Android";
  else if (/Macintosh|Mac OS X/.test(ua)) os = "Mac";
  else if (/Windows/.test(ua)) os = "Windows";
  else if (/Linux|X11|CrOS/.test(ua)) os = "Linux";
  let browser = "";
  if (/Edg\/|EdgiOS|EdgA\//.test(ua)) browser = "Edge";
  else if (/Firefox\/|FxiOS/.test(ua)) browser = "Firefox";
  else if (/Chrome\/|CriOS|Chromium\//.test(ua)) browser = "Chrome";
  else if (/Safari\//.test(ua) || (/AppleWebKit/.test(ua) && (os === "iPhone" || os === "iPad"))) browser = "Safari";
  let standalone = false;
  try {
    standalone = nav.standalone === true
      || !!(typeof window !== "undefined" && window.matchMedia?.("(display-mode: standalone)")?.matches);
  } catch { /* matchMedia unavailable */ }
  if (standalone) browser = "Installed app";
  return [os, browser].filter(Boolean).join(" · ") || null;
}

function screenShortSide() {
  try {
    return Math.min(screen.width || 0, screen.height || 0);
  } catch {
    return 0;
  }
}

function _withDeviceLabel(body, label) {
  return label ? { ...body, device_label: label } : body;
}

/**
 * Password login. Raw fetch: a wrong password (401 without X-Auth-Reason),
 * 429 lockout or 415 must never trigger the session-expiry flow. The stored
 * token (e.g. an idle-locked one) goes along as Bearer so the server retires
 * that session ("replaced"). Resolves to a LoginResponse; callers store it
 * with setAuthSession(). Errors carry the server `detail` and `.status`.
 */
export const authLogin = async (password, { deviceLabel: label } = {}) => {
  const headers = { "Content-Type": "application/json" };
  const prev = getAuthToken();
  if (prev) headers["Authorization"] = `Bearer ${prev}`;
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers,
    body: JSON.stringify(_withDeviceLabel({ password }, label)),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    const err = new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
};
/** First-run password. Resolves to a LoginResponse (callers: setAuthSession). */
export const authSetPassword = (password, { deviceLabel: label } = {}) =>
  request("/api/auth/set-password", {
    method: "POST",
    body: JSON.stringify(_withDeviceLabel({ password }, label)),
  });
/**
 * Change the password. A wrong current password is a 403 (plain Error, no
 * logout). On success the server revokes every earlier session — this one's
 * token included — so the returned LoginResponse (+ revoked_sessions) is
 * stored right here, before any other in-flight request can see the old
 * token fail. Calling setAuthSession(res) again is harmless.
 */
export const authChangePassword = async (current_password, new_password, { deviceLabel: label } = {}) => {
  const res = await request("/api/auth/change-password", {
    method: "POST",
    body: JSON.stringify(_withDeviceLabel({ current_password, new_password }, label)),
  });
  setAuthSession(res);
  return res;
};
/** Revoke this device's session ({ok, revoked}); idempotent, works when idle. */
export const authLogout = () => request("/api/auth/logout", { method: "POST" });
/** {sessions:[SessionRow], current_id, auth_disabled} */
export const fetchAuthSessions = () => request("/api/auth/sessions");
/** {ok, current}: `current` true means this device just signed itself out. */
export const revokeAuthSession = (id) =>
  request(`/api/auth/sessions/${encodeURIComponent(id)}/revoke`, { method: "POST" });
/** {ok, revoked: n} */
export const revokeOtherSessions = () =>
  request("/api/auth/sessions/revoke-others", { method: "POST" });
/** Always counts as user activity: {ok, idle_timeout_minutes, idle_remaining_s}. */
export const pingActivity = () =>
  request("/api/auth/activity", { method: "POST", activity: true });
/**
 * One-time WebSocket ticket {ticket, expires_in} (30 s, single use); ticket
 * null when auth is off. Fetched by a (re)connect, never by the user: no
 * activity header.
 */
export const fetchWsTicket = ({ signal } = {}) =>
  request("/api/auth/ws-ticket", { method: "POST", signal, activity: false });
/**
 * Mint a fresh media token and store it ({token, expires_at, expires_in};
 * token null when auth is off, which clears the stored one). Not stored if
 * the session changed while the request was in flight.
 */
export async function mintMediaToken() {
  const sent = getAuthToken();
  const r = await request("/api/auth/media-token", { method: "POST" });
  if (getAuthToken() === sent) setMediaToken(r?.token || null, r?.expires_at ?? null);
  return r;
}
/**
 * Turn the server's password protection on or off (POST /api/auth/protection;
 * the server rewrites DISABLE_AUTH in .env, then applies it at once).
 *   on:  {enabled: true, password} — or, with no password set yet,
 *        {enabled: true, new_password} — resolves to a LoginResponse plus
 *        {settings, protection}. The session is stored right here, like
 *        change-password, so this device stays signed in.
 *   off: {enabled: false, password} resolves to {settings, protection};
 *        sessions stay.
 * A wrong password is a 403 (never a logout), a lockout 429, a failed .env
 * write 500: Error(detail) with .status, like the other auth calls.
 */
export async function setAuthProtection({ enabled, password, new_password, device_label } = {}) {
  const body = { enabled: !!enabled, password: password || "" };
  if (new_password) body.new_password = new_password;
  if (enabled && device_label) body.device_label = device_label;
  if (enabled) _protectionTurningOn += 1;
  try {
    const res = await request("/api/auth/protection", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (res?.token) setAuthSession(res);
    return res;
  } finally {
    if (enabled) _protectionTurningOn -= 1;
  }
}
/**
 * The protection switch's state: {auth_disabled, password_set,
 * env_file_disable_auth, env_file_in_sync}. auth_disabled is the running
 * value; env_file_disable_auth what .env says now (null when it has no
 * DISABLE_AUTH line or cannot be read).
 */
export const fetchAuthProtection = () => request("/api/auth/protection");
/** {idle_timeout_minutes, choices, default_idle_timeout_minutes, session_max_days, auth_disabled, password_set} */
export const fetchAuthSettings = () => request("/api/settings/auth");
export const updateAuthSettings = (body) =>
  request("/api/settings/auth", { method: "PUT", body: JSON.stringify(body) });

// --- Telemetry ---
export const fetchTelemetryStatus = () => request("/api/telemetry");
export const setTelemetryEnabled = (enabled) =>
  request("/api/telemetry", {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });

// --- Projects ---
const e = encodeURIComponent;
export const fetchProjects = () => request("/api/projects");
export const fetchSkills = (project) =>
  request(`/api/skills${project ? `?project=${e(project)}` : ""}`);
export const createProject = (data) =>
  request("/api/projects", { method: "POST", body: JSON.stringify(data) });
export const fetchAllFolders = () =>
  request(`/api/projects/folders?tz_offset=${new Date().getTimezoneOffset()}`);
export const scanProjects = () =>
  request("/api/projects/scan", { method: "POST" });
export const renameProject = (name, newName, displayName) =>
  request(`/api/projects/${e(name)}/rename`, { method: "PUT", body: JSON.stringify({ new_name: newName, display_name: displayName || undefined }) });
export const archiveProject = (name) =>
  request(`/api/projects/${e(name)}/archive`, { method: "POST" });
export const fetchTrashFolders = () => request("/api/projects/trash");
export const deleteProject = (name) =>
  request(`/api/projects/${e(name)}`, { method: "DELETE" });
export const deleteTrashFolder = (name) =>
  request(`/api/projects/trash/${e(name)}`, { method: "DELETE" });
export const restoreTrashFolder = (name) =>
  request(`/api/projects/trash/${e(name)}/restore`, { method: "POST" });
export const fetchProjectAgents = (name, params = "") =>
  request(`/api/projects/${e(name)}/agents${params ? `?${params}` : ""}`);
export const fetchProjectWorktrees = (name) =>
  request(`/api/projects/${e(name)}/worktrees`);
export const fetchProjectSessions = (name) =>
  request(`/api/projects/${e(name)}/sessions`);
export const starSession = (project, sessionId) =>
  request(`/api/projects/${e(project)}/sessions/${e(sessionId)}/star`, { method: "PUT" });
export const unstarSession = (project, sessionId) =>
  request(`/api/projects/${e(project)}/sessions/${e(sessionId)}/star`, { method: "DELETE" });
export const fetchProjectBookmarks = (project) =>
  request(`/api/projects/${e(project)}/bookmarks`);
export const createBookmark = (project, messageId, userNote = null) =>
  request(`/api/projects/${e(project)}/messages/${e(messageId)}/bookmark`, {
    method: "POST",
    body: JSON.stringify({ user_note: userNote }),
  });
export const updateBookmark = (project, messageId, userNote) =>
  request(`/api/projects/${e(project)}/bookmarks/${e(messageId)}`, {
    method: "PATCH",
    body: JSON.stringify({ user_note: userNote }),
  });
export const deleteBookmark = (project, messageId) =>
  request(`/api/projects/${e(project)}/messages/${e(messageId)}/bookmark`, {
    method: "DELETE",
  });
export const fetchProjectAssets = (project, { includeMissing = false } = {}) =>
  request(`/api/projects/${e(project)}/assets?include_missing=${includeMissing ? 1 : 0}`);
// starred / hidden / note: null or omitted = unchanged; note "" clears.
export const updateAssetMarks = (project, { paths, starred, hidden, note }) => {
  const body = { paths };
  if (starred != null) body.starred = starred;
  if (hidden != null) body.hidden = hidden;
  if (note != null) body.note = note;
  return request(`/api/projects/${e(project)}/assets/marks`, {
    method: "POST",
    body: JSON.stringify(body),
  });
};
export const filesExistsBatch = (items) =>
  request(`/api/files/exists-batch`, { method: "POST", body: JSON.stringify({ items }) });
export const mintPreviewToken = (project) =>
  request("/api/preview/token", { method: "POST", body: JSON.stringify({ project }) });
export const fetchProjectFile = (project, path) =>
  request(`/api/projects/${e(project)}/file?path=${e(path)}`);
export const updateProjectFile = (project, path, content) =>
  request(`/api/projects/${e(project)}/file`, { method: "PUT", body: JSON.stringify({ path, content }) });
export const fetchProjectTree = (project, depth = 3) =>
  request(`/api/projects/${e(project)}/tree?depth=${depth}`);
export const browseProjectFile = (project, path) =>
  request(`/api/projects/${e(project)}/browse?path=${e(path)}`);
export const searchProjectFiles = (project, query, limit = 30) =>
  request(`/api/projects/${e(project)}/search-files?q=${encodeURIComponent(query)}&limit=${limit}`);
export const refreshClaudeMd = (project) =>
  request(`/api/projects/${e(project)}/refresh-claudemd`, { method: "POST" });
export const refreshClaudeMdStatus = (project) =>
  request(`/api/projects/${e(project)}/refresh-claudemd/status`);
export const discardClaudeMd = (project) =>
  request(`/api/projects/${e(project)}/refresh-claudemd`, { method: "DELETE" });
export const applyClaudeMd = (project, payload) =>
  request(`/api/projects/${e(project)}/apply-claudemd`, { method: "POST", body: JSON.stringify(payload) });
export const fetchClaudeMdPending = () =>
  request("/api/projects/claudemd-pending");
export const summarizeProgress = (project) =>
  request(`/api/projects/${e(project)}/summarize-progress`, { method: "POST" });
export const summarizeProgressStatus = (project) =>
  request(`/api/projects/${e(project)}/summarize-progress/status`);
export const discardProgressSummary = (project) =>
  request(`/api/projects/${e(project)}/summarize-progress`, { method: "DELETE" });
export const applyProgressSummary = (project) =>
  request(`/api/projects/${e(project)}/apply-progress`, { method: "POST" });
export const updateProjectSettings = (project, settings) =>
  request(`/api/projects/${e(project)}/settings`, { method: "PATCH", body: JSON.stringify(settings) });
export const rebuildInsights = (project) =>
  request(`/api/projects/${e(project)}/rebuild-insights`, { method: "POST" });

// --- Tasks ---
export const fetchTasksV2 = (params = "") =>
  request(`/api/v2/tasks${params ? `?${params}` : ""}`);
export const fetchTaskCounts = (project) => {
  const tz = new Date().getTimezoneOffset();
  const params = new URLSearchParams({ tz_offset: tz });
  if (project) params.set("project", project);
  return request(`/api/v2/tasks/counts?${params}`);
};
export const fetchTaskV2 = (id) => request(`/api/v2/tasks/${id}`);
export const createTaskV2 = (data) =>
  request("/api/v2/tasks", { method: "POST", body: JSON.stringify(data) });
export const updateTaskV2 = (id, data) =>
  request(`/api/v2/tasks/${id}`, { method: "PUT", body: JSON.stringify(data) });
export const reorderTasks = (taskIds) =>
  request("/api/v2/tasks/reorder", { method: "PUT", body: JSON.stringify({ task_ids: taskIds }) });
export const dispatchTask = (id) =>
  request(`/api/v2/tasks/${id}/dispatch`, { method: "POST" });
export const cancelTask = (id) =>
  request(`/api/v2/tasks/${id}/cancel`, { method: "POST" });
export const completeTask = (id) =>
  request(`/api/v2/tasks/${id}/complete`, { method: "POST" });
export const regenerateTaskSummary = (id) =>
  request(`/api/v2/tasks/${id}/regenerate-summary`, { method: "POST" });
export const fetchQueueStatus = () =>
  request(`/api/v2/tasks/queue?tz_offset=${new Date().getTimezoneOffset()}`);
export const batchProcessTasks = (taskIds) =>
  request("/api/v2/tasks/batch-process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(taskIds ? { task_ids: taskIds } : {}),
  });

// --- Agents ---
export const fetchAgents = (params = "") =>
  request(`/api/agents${params ? `?${params}` : ""}`);
export const fetchAgent = (id, { includeSubagents = true } = {}) => request(`/api/agents/${id}${includeSubagents ? "" : "?include_subagents=false"}`);
export const fetchAgentContextUsage = (id) => request(`/api/agents/${id}/context-usage`);
export const fetchUnreadCount = () => request("/api/agents/unread");
export const fetchUnreadList = () => request("/api/agents/unread-list");
export const createAgent = (data) =>
  request("/api/agents", { method: "POST", body: JSON.stringify(data) });
export const renameAgent = (id, name) =>
  request(`/api/agents/${id}`, { method: "PUT", body: JSON.stringify({ name }) });
export const updateAgent = (id, data) =>
  request(`/api/agents/${id}`, { method: "PUT", body: JSON.stringify(data) });
export const launchTmuxAgent = (data) =>
  request("/api/agents/launch-tmux", { method: "POST", body: JSON.stringify(data) });
export const divergeMessage = (agentId, messageId, body) =>
  request(`/api/agents/${agentId}/messages/${messageId}/diverge`, {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
export const scanAgents = () =>
  request("/api/agents/scan", { method: "POST" });
export const wakeSyncAll = () =>
  request("/api/agents/wake-sync-all", { method: "POST" });
export const fetchUnlinkedSessions = () =>
  request("/api/unlinked-sessions");
export const replayPendingUnlinked = () =>
  request("/api/unlinked-sessions/replay", { method: "POST" });
export const adoptUnlinkedSession = (sessionId, body) =>
  request(`/api/unlinked-sessions/${sessionId}/adopt`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const convertAndAdoptUnlinkedSession = (fileKey, body) =>
  request(`/api/unlinked-sessions/${fileKey}/convert-and-adopt`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const dropUnlinkedSession = (fileKey) =>
  request(`/api/unlinked-sessions/${fileKey}/drop`, { method: "POST" });
export const stopAgent = (id, { generateSummary = false, taskComplete = true, taskDrop = false, incompleteReason = null } = {}) => {
  const params = new URLSearchParams();
  if (generateSummary) params.set("generate_summary", "true");
  if (!taskComplete) params.set("task_complete", "false");
  if (taskDrop) params.set("task_drop", "true");
  if (incompleteReason) params.set("incomplete_reason", incompleteReason);
  const qs = params.toString();
  return request(`/api/agents/${id}${qs ? "?" + qs : ""}`, { method: "DELETE" });
};
export const deleteAgent = (id) =>
  request(`/api/agents/${id}/permanent`, { method: "DELETE" });
export const resumeAgent = (id, body = null) =>
  request(`/api/agents/${id}/resume`, {
    method: "POST",
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
export const fetchDisplaySent = (agentId, { offset = 0, tailBytes = 0, focusId = null } = {}) => {
  const params = new URLSearchParams();
  if (offset) params.set("offset", String(offset));
  if (tailBytes) params.set("tail_bytes", String(tailBytes));
  if (focusId) params.set("focus_id", String(focusId));
  return request(`/api/agents/${agentId}/display/sent?${params}`);
};
export const fetchDisplayPreSent = (agentId) =>
  request(`/api/agents/${agentId}/display/pre-sent`);
export const wakeSync = (agentId) =>
  request(`/api/agents/${agentId}/wake-sync`, { method: "POST" });
export const sendMessage = (agentId, content, { scheduled_at = null } = {}) =>
  request(`/api/agents/${agentId}/messages`, {
    method: "POST",
    body: JSON.stringify({ content, scheduled_at }),
  });
export const markAgentRead = (agentId) =>
  request(`/api/agents/${agentId}/read`, { method: "PUT" });
export const markAllAgentsRead = () =>
  request("/api/agents/read-all", { method: "PUT" });
// Hard-delete: tombstones the bubble (used by per-message DELETE button).
export const deleteMessage = (agentId, messageId) =>
  request(`/api/agents/${agentId}/messages/${messageId}`, { method: "DELETE" });
// Soft-cancel: status='cancelled', bubble stays visible (used by ESC).
export const cancelMessage = (agentId, messageId) =>
  request(`/api/agents/${agentId}/messages/${messageId}/cancel`, { method: "POST" });
export const updateMessage = (agentId, messageId, data) =>
  request(`/api/agents/${agentId}/messages/${messageId}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
// --- Interactive Answer (AskUserQuestion / ExitPlanMode) ---
export const answerAgent = (agentId, payload) =>
  request(`/api/agents/${agentId}/answer`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const escapeAgent = (agentId) =>
  request(`/api/agents/${agentId}/escape`, { method: "POST" });
// --- Tool Permission Approval ---
export const respondPermission = (agentId, requestId, payload) =>
  request(`/api/agents/${agentId}/permission/${requestId}/respond`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const fetchPendingPermissions = (agentId) =>
  request(`/api/agents/${agentId}/permissions/pending`);
// --- Agent Insight Suggestions ---
export const fetchAgentSuggestions = (agentId) =>
  request(`/api/agents/${agentId}/suggestions`);
export const fetchProcessedSuggestions = (agentId) =>
  request(`/api/agents/${agentId}/suggestions?status=processed`);
export const applyAgentSuggestions = (agentId, payload) =>
  request(`/api/agents/${agentId}/apply-suggestions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const discardAgentSuggestions = (agentId) =>
  request(`/api/agents/${agentId}/suggestions`, { method: "DELETE" });
export const regenerateAgentInsights = (agentId) =>
  request(`/api/agents/${agentId}/regenerate-insights`, { method: "POST" });

// --- Message search ---
export const searchMessages = (query, { project, role, limit, includeSubagents } = {}) => {
  const params = new URLSearchParams({ q: query });
  if (project) params.set("project", project);
  if (role) params.set("role", role);
  if (limit) params.set("limit", String(limit));
  if (includeSubagents === false) params.set("include_subagents", "false");
  return request(`/api/messages/search?${params}`);
};

// --- Health ---
export const fetchHealth = () => request("/api/health");

// --- Git ---
export const fetchGitLog = (project, limit = 30) =>
  request(`/api/git/${e(project)}/log?limit=${limit}`);
export const fetchGitBranches = (project) =>
  request(`/api/git/${e(project)}/branches`);
export const fetchGitStatus = (project) =>
  request(`/api/git/${e(project)}/status`);
export const fetchGitWorktrees = (project) =>
  request(`/api/git/${e(project)}/worktrees`);
export const checkoutBranch = (project, branch) =>
  request(`/api/git/${e(project)}/checkout/${e(branch)}`, { method: "POST" });
export const gitPush = (project) =>
  request(`/api/git/${e(project)}/push`, { method: "POST" });
// --- System ---
export const fetchSystemStats = () => request("/api/system/stats");
export const fetchStorageStats = () => request("/api/system/storage");
export const truncateLogs = () => request("/api/system/logs/truncate", { method: "POST" });
export const fetchTokenUsage = () => request("/api/system/token-usage");
export const restartServer = () => request("/api/system/restart", { method: "POST" });
export const scanOrphans = () => request("/api/system/orphans/scan");
export const cleanOrphans = () => request("/api/system/orphans/clean", { method: "POST" });
export const fetchBackupStatus = () => request("/api/system/backup");
export const triggerBackup = () => request("/api/system/backup", { method: "POST" });
export const purgeBackups = () => request("/api/system/backup", { method: "DELETE" });
export const deleteSingleBackup = (name) => request(`/api/system/backup/${name}`, { method: "DELETE" });
export const restoreBackup = (name) => request(`/api/system/backup/${name}/restore`, { method: "POST" });
export const updateBackupConfig = (data) =>
  request("/api/system/backup/config", { method: "PUT", body: JSON.stringify(data) });
export const importBackup = (file) => {
  const fd = new FormData(); fd.append("file", file);
  return formDataRequest("/api/system/backup/import", fd, "Import backup");
};
export const downloadBackupUrl = (name) => `/api/system/backup/${name}/download`;

// --- Dropbox sync ---
export const fetchDropboxStatus = () => request("/api/dropbox/status");
export const updateDropboxConfig = (data) =>
  request("/api/dropbox/config", { method: "PUT", body: JSON.stringify(data) });
export const startDropboxLink = (opts = {}) =>
  request("/api/dropbox/link/start", { method: "POST", body: JSON.stringify({ mode: opts.mode || "auto", return_to: opts.returnTo }) });
export const completeDropboxLink = (code) =>
  request("/api/dropbox/link/complete", { method: "POST", body: JSON.stringify({ code }) });
export const unlinkDropbox = () => request("/api/dropbox/link", { method: "DELETE" });
export const triggerDropboxSync = (project = null) =>
  request("/api/dropbox/sync", { method: "POST", body: JSON.stringify(project ? { project } : {}) });
export const pauseDropboxSync = () => request("/api/dropbox/pause", { method: "POST" });
export const resumeDropboxSync = () => request("/api/dropbox/resume", { method: "POST" });
export const startDropboxDryRun = (project, folders = undefined, ignore = undefined) =>
  request("/api/dropbox/dry-run", { method: "POST", body: JSON.stringify({ project, folders, ignore }) });
export const fetchDropboxDryRun = (jobId) => request(`/api/dropbox/dry-run/${e(jobId)}`);
export const stopDropboxDryRun = (jobId) =>
  request(`/api/dropbox/dry-run/${e(jobId)}`, { method: "DELETE" });
export const fetchDropboxFolders = (project) =>
  request(`/api/projects/${e(project)}/dropbox/folders`);
export const fetchProjectDropboxStatus = (project) =>
  request(`/api/projects/${e(project)}/dropbox/status`);

// --- FormData helper (shared by voice + upload) ---
// Uploads, voice and backup import are always user-driven, so they always
// count as activity.
async function formDataRequest(url, formData, errorLabel = "Request", opts = {}) {
  const headers = { "X-User-Activity": "1" };
  const token = getAuthToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const fetchOpts = { method: "POST", body: formData, headers, ...opts };
  let res;
  try {
    res = await fetch(`${BASE}${url}`, fetchOpts);
  } catch (e) {
    // keepalive has a 64 KB body-size limit — retry without it on TypeError
    if (opts.keepalive && e instanceof TypeError) {
      const { keepalive: _, ...rest } = fetchOpts;
      res = await fetch(`${BASE}${url}`, rest);
    } else {
      throw e;
    }
  }
  if (res.status === 401) await handle401(res, token, url);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || `${errorLabel} (${res.status})`);
  }
  return res.json();
}

// --- Voice ---
export async function transcribeVoice(audioBlob, mimeType) {
  // Pick file extension matching actual format (Safari = mp4, Chrome = webm)
  const ext = mimeType && mimeType.includes("mp4") ? "mp4"
    : mimeType && mimeType.includes("ogg") ? "ogg"
    : "webm";
  const formData = new FormData();
  formData.append("file", audioBlob, `recording.${ext}`);
  return formDataRequest("/api/voice", formData, "Voice API error", { keepalive: true });
}

export const refineVoiceText = (text) =>
  request("/api/voice/refine", { method: "POST", body: JSON.stringify({ text }), keepalive: true });

export async function uploadFile(file, project = null) {
  const formData = new FormData();
  formData.append("file", file);
  if (project) formData.append("project", project);
  return formDataRequest("/api/upload", formData, "Upload failed");
}

/**
 * Trigger a browser-native download of `url` as `filename`.
 *
 * Single universal path: append `?download=1` so the backend returns
 * `Content-Disposition: attachment`, then click an <a download>. Starlette's
 * FileResponse already streams from disk in 64 KB chunks with Range support,
 * so the browser handles progress, pause/resume, and never touches the JS
 * heap — no size threshold, no platform branch, no Blob, no Web Share.
 *
 * Synchronous: returns immediately after the click dispatches.
 */
export function downloadFile(url, filename) {
  const dlUrl = url + (url.includes("?") ? "&" : "?") + "download=1";
  const a = document.createElement("a");
  a.href = dlUrl;
  a.download = filename || "";
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

export async function generateWorktreeName(prompt) {
  const res = await authedFetch("/api/worktree-name", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (!res.ok) return null;
  const data = await res.json();
  return data.name || null;
}

// ---- Session viewing time statistics ----

export const fetchViewingStatsWeek = (days = 7) =>
  request(`/api/stats/viewing/week?days=${days}&tz_offset=${new Date().getTimezoneOffset()}`);

export const fetchViewingStatsDay = (date) =>
  request(`/api/stats/viewing/day?date=${encodeURIComponent(date)}&tz_offset=${new Date().getTimezoneOffset()}`);

// ---- Attention assistant ----

// Compile natural language into a job spec WITHOUT persisting it. The
// response carries `preview_next_run_at`, computed by the backend trigger
// rather than by the model's prose, so the confirmation the user approves
// reflects the real schedule.
export const compileAttentionRequest = (text) =>
  request("/api/attention/compile", { method: "POST", body: JSON.stringify({ text }) });

// One conversational turn for the bubble. `messages` is the recent
// transcript as [{role: "user"|"assistant", content}]. Returns
// {say, spec|null}; a spec is a proposal — nothing is persisted until the
// user confirms and the client POSTs /jobs.
export const chatAttention = (messages) =>
  request("/api/attention/chat", { method: "POST", body: JSON.stringify({ messages }) });

export const fetchAttentionJobs = (includeFinished = false) =>
  request(`/api/attention/jobs?include_finished=${includeFinished ? "true" : "false"}`);

export const createAttentionJob = (job) =>
  request("/api/attention/jobs", { method: "POST", body: JSON.stringify(job) });

export const patchAttentionJob = (id, patch) =>
  request(`/api/attention/jobs/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export const deleteAttentionJob = (id) =>
  request(`/api/attention/jobs/${id}`, { method: "DELETE" });

export const snoozeAttentionJob = (id, minutes = 15) =>
  request(`/api/attention/jobs/${id}/snooze?minutes=${minutes}`, { method: "POST" });

export const runAttentionJobNow = (id) =>
  request(`/api/attention/jobs/${id}/run-now`, { method: "POST" });

export const fetchAttentionCapabilities = () =>
  request("/api/attention/capabilities");

// Design a new orb character with a strong model. Returns {character};
// nothing persists server-side — the client stores accepted designs.
export const generateAttentionCharacter = (text) =>
  request("/api/attention/character", { method: "POST", body: JSON.stringify({ text }) });
