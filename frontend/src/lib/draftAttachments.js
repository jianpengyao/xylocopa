// Draft attachment chips persisted in localStorage
// (`draft:chat:<id>:attachments`, `draft:new-task:attachments`).
//
// Only the upload's backend path and metadata are stored — never a URL.
// Media URLs carry a rotating media token (lib/urls.js withMediaToken), so a
// stored URL would go stale and would leave a credential in localStorage.
// Image previews are rebuilt from `uploadedPath` on every restore.

import { stripAuthParams, withMediaToken } from "./urls";

/** The persisted shape of a completed (uploaded) draft attachment. */
export function draftAttachmentForCache(a) {
  return {
    id: a.id,
    uploadedPath: a.uploadedPath,
    originalName: a.originalName,
    size: a.size,
    mimeType: a.mimeType || a.file?.type || null,
  };
}

/**
 * Turn a cached entry back into a composer attachment. `buildUrl(uploadedPath)`
 * gives the media URL (uploadUrl / attachmentUrl). Entries written by older
 * builds carry a `thumbnailUrl` with the then-current token baked in: it is
 * dropped and rebuilt — from `uploadedPath`, or failing that from the stored
 * URL with its token stripped.
 */
export function restoreDraftAttachment(cached, buildUrl) {
  const { thumbnailUrl: legacyUrl, ...rest } = cached || {};
  const isImage = (rest.mimeType || "").startsWith("image/") || !!legacyUrl;
  let previewUrl = null;
  if (isImage) {
    if (rest.uploadedPath) previewUrl = buildUrl(rest.uploadedPath);
    else if (legacyUrl) previewUrl = withMediaToken(stripAuthParams(legacyUrl));
  }
  return { ...rest, uploading: false, file: null, previewUrl };
}
