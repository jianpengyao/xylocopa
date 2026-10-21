// Shared primitives for "is this media file actually there?" checks across
// FilePreview, ImageLightbox, and ProjectBrowserModal. The backend
// /api/files/exists-batch endpoint backs `useBatchExists`.
//
// Why centralized: each component used to roll its own "is the file gone"
// detection (cache-bust + onError heuristics, or nothing at all), which led
// to false positives, false negatives, and inconsistent UI. Moving the
// existence probe + cache-bust here means all three surfaces share one
// signal and one fallback UX.

import { useState, useEffect, useMemo, useCallback } from "react";
import { filesExistsBatch } from "./api";
import { stripAuthParams } from "./urls";

// Parse a media URL into the shape expected by /api/files/exists-batch.
// Strips ?query (notably the ?token=… that fileUrl() appends) so the
// backend doesn't see "<path>?token=..." as the filename. Returns null
// for URLs we can't probe (http://, data:, blob:) — caller treats those
// as always-exists.
export function parseFileUrl(url) {
  if (!url) return null;
  const noQuery = url.split("?")[0];
  let m = noQuery.match(/^\/?api\/files\/([^/]+)\/(.+)$/);
  if (m) return { project: decodeURIComponent(m[1]), path: decodeURIComponent(m[2]) };
  m = noQuery.match(/^\/?api\/uploads\/(.+)$/);
  if (m) return { upload: decodeURIComponent(m[1]) };
  return null;
}

// Batch-probe existence + size + mtime for a list of URLs. One round-trip
// replaces N HEAD probes (and HEAD isn't auto-registered on @router.get
// anyway, so per-row probes used to come back 405). Returns:
//   statMap — keyed by stripAuthParams(url) with { exists, size, mtime }
//             (URLs that don't parse are absent; caller treats them as
//             exists). Keyed without ?token= so a media-token refresh
//             neither re-probes nor flashes a null stat.
//   refresh — re-runs the probe (for retry-after-missing UX)
export function useBatchExists(urls) {
  const [statMap, setStatMap] = useState({});
  const [refreshTick, setRefreshTick] = useState(0);
  const probeKeys = useMemo(() => {
    if (!urls) return [];
    return urls.filter((u) => parseFileUrl(u)).map(stripAuthParams).sort();
  }, [urls]);
  const cacheKey = probeKeys.join("|");
  useEffect(() => {
    if (!probeKeys.length) { setStatMap({}); return; }
    let cancelled = false;
    const items = probeKeys.map((u) => parseFileUrl(u));
    filesExistsBatch(items)
      .then((resp) => {
        if (cancelled) return;
        const next = {};
        const results = resp?.results || [];
        probeKeys.forEach((u, i) => { next[u] = results[i] || { exists: false }; });
        setStatMap(next);
      })
      .catch(() => { if (!cancelled) setStatMap({}); });
    return () => { cancelled = true; };
  }, [cacheKey, refreshTick]); // eslint-disable-line react-hooks/exhaustive-deps
  const refresh = useCallback(() => setRefreshTick((t) => t + 1), []);
  return { statMap, refresh };
}

// Single-URL convenience. Returns { stat, refresh } where stat is:
//   null while probing,
//   { exists: true, size, mtime } / { exists: false, ... } once known,
//   { exists: true } for non-parseable URLs (optimistic — can't probe).
export function useFileExists(url) {
  const urls = useMemo(() => (url ? [url] : []), [url]);
  const { statMap, refresh } = useBatchExists(urls);
  let stat;
  if (!url) stat = null;
  else if (!parseFileUrl(url)) stat = { exists: true };
  else stat = statMap[stripAuthParams(url)] || null;
  return { stat, refresh };
}

// Append a cache-bust query param. Pass the file's mtime (from a stat
// result) when available — the URL only changes when the file actually
// changes, so the browser cache stays useful. Pass null/undefined to skip.
export function withCacheBust(url, version) {
  if (!url || version == null) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}_v=${version}`;
}
