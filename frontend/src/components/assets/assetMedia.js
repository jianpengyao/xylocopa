// URL + open-behaviour helpers for Assets tiles. Pure (no React) so the
// mapping from asset → what a tap does is unit-tested in assetMedia.test.js.

import { fileUrl, thumbUrl } from "../../lib/urls";
import { withCacheBust } from "../../lib/mediaState";
import { assetWebapp } from "./assetFilters";

// /api/thumbs renders these (Pillow / ffmpeg); svg is served raw by the same
// route, so svg tiles load the file itself instead.
const THUMB_IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp"]);
const THUMB_VIDEO_EXTS = new Set(["mp4", "webm", "mov", "m4v"]);

// Grid tiles ask for the small variant; the backend caches it separately.
export const TILE_THUMB_WIDTH = 320;

function withParam(url, param) {
  return `${url}${url.includes("?") ? "&" : "?"}${param}`;
}

/** True for assets backed by a project file (not a port / url webapp). */
export function isFileAsset(asset) {
  const k = assetWebapp(asset)?.kind;
  return k !== "port" && k !== "url";
}

/** Small cache-busted thumbnail for a grid tile, or null for icon tiles.
 *  Never returns the video file itself — tiles must not load mp4s. */
export function tileThumbUrl(project, asset) {
  if (!asset || asset.exists === false || !isFileAsset(asset)) return null;
  const ext = (asset.ext || "").toLowerCase();
  if (asset.kind === "image" && ext === "svg") {
    return withCacheBust(fileUrl(project, asset.path), asset.mtime);
  }
  if ((asset.kind === "image" && THUMB_IMAGE_EXTS.has(ext))
    || (asset.kind === "video" && THUMB_VIDEO_EXTS.has(ext))) {
    return withCacheBust(withParam(thumbUrl(project, asset.path), `w=${TILE_THUMB_WIDTH}`), asset.mtime);
  }
  return null;
}

/** ImageLightbox media item ({type, src, thumbSrc, filename}). */
export function lightboxItem(project, asset) {
  const ext = (asset.ext || "").toLowerCase();
  const hasThumb = asset.kind === "video" ? THUMB_VIDEO_EXTS.has(ext) : THUMB_IMAGE_EXTS.has(ext);
  return {
    type: asset.kind === "video" ? "video" : "image",
    src: fileUrl(project, asset.path),
    thumbSrc: hasThumb ? thumbUrl(project, asset.path) : undefined,
    filename: asset.name || asset.path.split("/").pop(),
  };
}

export function isLightboxAsset(asset) {
  return (asset?.kind === "image" || asset?.kind === "video") && asset.exists !== false;
}

/**
 * What tapping an asset does:
 *   lightbox  — image / video (swipeable ImageLightbox)
 *   static    — html file or static webapp → sandboxed WebAppPreview
 *   port      — port webapp → WebAppPreview via its proxy src
 *   port-chat — port webapp without a proxy src → jump to its chat card
 *   url       — url webapp → new tab
 *   pdf       — ProjectBrowserModal viewer
 *   download  — office / 3d
 *   details   — missing (or nothing better) → detail sheet
 */
export function openActionFor(asset) {
  if (!asset) return "details";
  if (asset.exists === false) return "details";
  const app = assetWebapp(asset);
  if (app?.kind === "url") return app.target ? "url" : "details";
  if (app?.kind === "port") {
    if (app.src) return "port";
    return asset.last_agent_id ? "port-chat" : "details";
  }
  switch (asset.kind) {
    case "image":
    case "video":
      return "lightbox";
    case "html":
    case "webapp":
      return "static";
    case "pdf":
      return "pdf";
    case "office":
    case "3d":
      return "download";
    default:
      return "details";
  }
}

/** Chat deep link for the message that last referenced the asset. */
export function chatLinkFor(asset) {
  if (!asset?.last_agent_id) return null;
  const base = `/agents/${asset.last_agent_id}`;
  return asset.last_message_id ? `${base}?focus=${encodeURIComponent(asset.last_message_id)}` : base;
}
