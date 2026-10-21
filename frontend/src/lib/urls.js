/**
 * Centralized API URL builders and backend path patterns.
 *
 * All resource URL construction goes through these helpers so that
 * auth mechanisms (signed URLs, cookies) or platform changes (macOS)
 * only require updating this single file.
 */

import { getMediaToken } from "./api";

// ---------------------------------------------------------------------------
// API path prefixes
// ---------------------------------------------------------------------------
export const API_FILES_PREFIX = "/api/files/";
export const API_THUMBS_PREFIX = "/api/thumbs/";
export const API_UPLOADS_PREFIX = "/api/uploads/";
export const API_PREVIEW_PREFIX = "/api/preview/t/";

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

/**
 * Append the media token as ?token= for browser-initiated media requests
 * (<img>, <video>, <a download>). The session token never goes into a URL;
 * the media token (scope=media, session-bound, ~12 h) is accepted only on
 * GET/HEAD of /api/files, /api/thumbs and /api/uploads. Without one (auth
 * off, or not minted yet) the URL is returned unchanged.
 */
export function withMediaToken(url) {
  return appendToken(url, getMediaToken());
}

function appendToken(url, token) {
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

const MEDIA_PREFIXES = [API_FILES_PREFIX, API_THUMBS_PREFIX, API_UPLOADS_PREFIX];

/**
 * Re-stamp a media URL built earlier (and memoized) with `token`, the
 * current media token by default, so it outlives the token it was built
 * with. A URL that already carries that token, and any non-media URL, is
 * returned unchanged.
 */
export function withCurrentMediaToken(url, token = getMediaToken()) {
  if (typeof url !== "string" || !MEDIA_PREFIXES.some((p) => url.startsWith(p))) return url;
  const m = /[?&]token=([^&#]*)/.exec(url);
  if ((m ? m[1] : "") === (token ? encodeURIComponent(token) : "")) return url;
  return appendToken(stripAuthParams(url), token);
}

/**
 * Drop `token` and `ticket` query parameters, keeping every other parameter
 * (`_v`, `tile`, `download`) in order and as encoded. Used to key caches and
 * stat maps so they survive a media-token change.
 */
export function stripAuthParams(url) {
  if (typeof url !== "string") return url;
  const q = url.indexOf("?");
  if (q < 0) return url;
  const h = url.indexOf("#", q);
  const query = url.slice(q + 1, h < 0 ? undefined : h);
  const hash = h < 0 ? "" : url.slice(h);
  const kept = query.split("&").filter((p) => p && !/^(?:token|ticket)(?:=|$)/.test(p));
  return url.slice(0, q) + (kept.length ? `?${kept.join("&")}` : "") + hash;
}

// ---------------------------------------------------------------------------
// URL builders
// ---------------------------------------------------------------------------

/** Build URL for a user-uploaded file. */
export function uploadUrl(filename) {
  return withMediaToken(`${API_UPLOADS_PREFIX}${encodeURIComponent(filename)}`);
}

/**
 * Build the URL for an uploaded attachment from its backend path.
 * Attachments stored inside a project (`<project>/.xylocopa/uploads/...`)
 * are project files (served with thumbnails); anything else lives in the
 * global uploads dir. Must be checked before the uploads pattern — both
 * paths contain `.xylocopa/uploads/`.
 */
export function attachmentUrl(absPath) {
  const inProject = (absPath || "").match(RE_PROJECTS_PATH);
  if (inProject) return fileUrl(inProject[1], inProject[2]);
  const filename = (absPath || "").split("/").pop() || absPath;
  return uploadUrl(filename);
}

/** Build URL for a project file. */
export function fileUrl(project, relPath) {
  const segments = relPath.split("/").map(encodeURIComponent).join("/");
  return withMediaToken(`${API_FILES_PREFIX}${encodeURIComponent(project)}/${segments}`);
}

/** Build URL for a project file thumbnail. */
export function thumbUrl(project, relPath) {
  const segments = relPath.split("/").map(encodeURIComponent).join("/");
  return withMediaToken(`${API_THUMBS_PREFIX}${encodeURIComponent(project)}/${segments}`);
}

/** Build URL for a sandboxed web-app preview. The preview-scoped token rides
 *  in the path (not a query param) so relative subresource refs inherit it. */
export function previewUrl(token, project, relPath) {
  const segments = relPath.split("/").map(encodeURIComponent).join("/");
  return `${API_PREVIEW_PREFIX}${encodeURIComponent(token)}/${encodeURIComponent(project)}/${segments}`;
}

/** Convert an /api/files/ URL to its /api/thumbs/ equivalent. */
export function fileUrlToThumbUrl(url) {
  if (!url.startsWith(API_FILES_PREFIX)) return url;
  return API_THUMBS_PREFIX + url.slice(API_FILES_PREFIX.length);
}

// ---------------------------------------------------------------------------
// Backend path patterns (for recognizing backend filesystem paths)
// ---------------------------------------------------------------------------

/** Matches `.xylocopa/uploads/` (or legacy `.agenthive/uploads/`)`<filename>`. Group 1 = filename. */
export const RE_UPLOADS_PATH = /\.(?:xylocopa|agenthive)\/uploads\/([^/]+)$/;

/** Matches `xylocopa-projects/` (or legacy `agenthive-projects/`)`<project>/<rest>`. Groups: 1=project, 2=rest. */
export const RE_PROJECTS_PATH = /(?:xylocopa|agenthive)-projects\/([^/]+)\/(.+)/;

/** Segment string for checking if a path references xylocopa-projects (preferred). */
export const PROJECTS_DIR_SEGMENT = "xylocopa-projects/";

/** Legacy segment retained so paths that still reference agenthive-projects are recognized. */
export const LEGACY_PROJECTS_DIR_SEGMENT = "agenthive-projects/";
