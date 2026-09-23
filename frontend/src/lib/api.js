/** Centralized API wrapper for Xylocopa. */

import { calibrate } from "./serverTime";

const BASE = "";
const TOKEN_KEY = "cc-auth-token";
const LOGIN_TS_KEY = "cc-login-ts";

// Timestamp of the most recent setAuthToken() call — used to protect the fresh
// token from being nuked by stale callbacks during the first few seconds after
// login (e.g. idle-lock visibilitychange handlers racing with React renders).
// Persisted in localStorage so the grace period survives iOS PWA page reloads.
let _lastLoginTs = Number(localStorage.getItem(LOGIN_TS_KEY) || 0);
const _LOGIN_GRACE_MS = 3000; // 3-second grace window after login

export function getAuthToken() {
  return localStorage.getItem(TOKEN_KEY);
}

/** Returns the persisted login timestamp (epoch ms), or 0 if never set. */
export function getLoginTs() {
  return _lastLoginTs || Number(localStorage.getItem(LOGIN_TS_KEY) || 0);
}

export function setAuthToken(token) {
  _lastLoginTs = Date.now();
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(LOGIN_TS_KEY, String(_lastLoginTs));
}

/**
 * Clear the auth token.  Returns true if the token was actually removed,
 * false if the call was blocked by the post-login grace window.
 */
export function clearAuthToken(reason) {
  // During the post-login grace window, refuse to clear the token.
  // This prevents stale visibilitychange handlers, frozen timers, and
  // in-flight 401 responses from racing with a fresh login.
  // Check both in-memory AND persisted timestamp (survives iOS PWA reloads).
  const loginTs = _lastLoginTs || Number(localStorage.getItem(LOGIN_TS_KEY) || 0);
  const sinceLogin = Date.now() - loginTs;
  if (loginTs && sinceLogin < _LOGIN_GRACE_MS) {
    console.warn("[auth] clearAuthToken BLOCKED during login grace period (%dms since login, reason=%s)", sinceLogin, reason || "unknown");
    _sendAuthDiag("clear_blocked", reason || "unknown", sinceLogin);
    return false;
  }
  console.warn("[auth] clearAuthToken called (reason=%s)", reason || "unknown");
  _sendAuthDiag("clear", reason || "unknown", sinceLogin);
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(LOGIN_TS_KEY);
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
  return fetch(`${BASE}${url}`, { ...opts, headers });
}

/** Handle 401 by clearing auth and emitting event. */
function handle401(requestToken) {
  // If the token changed since this request was sent (e.g. user re-logged in
  // while this stale request was in-flight), don't nuke the fresh token.
  const currentToken = getAuthToken();
  if (currentToken && currentToken !== requestToken) {
    throw new Error("Not authenticated");
  }
  clearAuthToken("handle401");
  if (window.location.pathname !== "/login") {
    window.dispatchEvent(new Event("auth-expired"));
  }
  throw new Error("Not authenticated");
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

async function request(url, opts = {}) {
  const headers = { "Content-Type": "application/json", ...opts.headers };
  const token = getAuthToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const method = opts.method || "GET";
  const t0 = performance.now();
  const res = await fetch(`${BASE}${url}`, { ...opts, headers });
  _logTiming(method, url, res.status, performance.now() - t0);

  if (res.status === 401) handle401(token);

  // Calibrate clock offset from HTTP Date header (works before WS connects)
  const serverDate = res.headers.get("Date");
  if (serverDate) calibrate(serverDate);

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    const msg = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map(e => e.msg || JSON.stringify(e)).join("; ")
      : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}

// --- Auth ---
export const authCheck = () =>
  request("/api/auth/check", { method: "POST" });
export const authLogin = async (password) => {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
  }
  return res.json();
};
export const authSetPassword = (password) =>
  request("/api/auth/set-password", { method: "POST", body: JSON.stringify({ password }) });
export const authChangePassword = (current_password, new_password) =>
  request("/api/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password, new_password }),
  });

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

// --- Notification Settings ---
export const fetchNotificationSettings = () => request("/api/settings/notifications");
export const updateNotificationSettings = (data) =>
  request("/api/settings/notifications", { method: "PUT", body: JSON.stringify(data) });

// --- FormData helper (shared by voice + upload) ---
async function formDataRequest(url, formData, errorLabel = "Request", opts = {}) {
  const headers = {};
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
  if (res.status === 401) handle401(token);
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
