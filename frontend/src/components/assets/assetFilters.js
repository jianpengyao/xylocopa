// Pure filter / sort / patch helpers for the project Assets tab. Kept free of
// React and network code so they can be unit-tested (assetFilters.test.js).
//
// Asset shape (GET /api/projects/{name}/assets → assets[]):
//   { path, name, dir, kind, ext, sources, tier, first_seen_at, last_seen_at,
//     mention_count, last_agent_id, last_agent_name, last_message_id,
//     exists, size, mtime, starred, hidden, note, webapp }

// Type chips. "web" groups raw .html files with webapp_present apps.
export const TYPE_TABS = [
  { key: "all", label: "All" },
  { key: "image", label: "Images" },
  { key: "video", label: "Videos" },
  { key: "web", label: "Web" },
  { key: "pdf", label: "PDF" },
  { key: "office", label: "Office" },
  { key: "3d", label: "3D" },
];

export const SORT_OPTIONS = [
  { value: "recent", label: "Recent" },
  { value: "name", label: "Name" },
  { value: "size", label: "Size" },
];

export function typeKeyOf(asset) {
  const kind = asset?.kind;
  if (kind === "html" || kind === "webapp") return "web";
  return kind || "other";
}

/** Parse a backend timestamp to epoch ms. Naive ISO strings are UTC. */
export function parseTs(value) {
  if (value == null || value === "") return 0;
  if (typeof value === "number") return value;
  let s = String(value);
  if (/^\d{4}-\d{2}-\d{2}T[\d:.]+$/.test(s)) s += "Z";
  const t = new Date(s).getTime();
  return Number.isNaN(t) ? 0 : t;
}

/** The asset's webapp descriptor ({kind, target, title?, src?}) or null.
 *  A port / url ref whose WebApp row is gone comes back with webapp: null —
 *  rebuild kind + target from its "webapp:port:<p>" / "webapp:url:<u>" path
 *  so it is never mistaken for a project file. */
export function assetWebapp(asset) {
  if (asset?.webapp) return asset.webapp;
  const m = /^webapp:(port|url):([\s\S]+)$/.exec(asset?.path || "");
  return m ? { kind: m[1], target: m[2] } : null;
}

/** Human label for a tile / sheet title. Webapps prefer their title. */
export function displayName(asset) {
  if (!asset) return "";
  const app = assetWebapp(asset);
  if (app) {
    if (app.title) return app.title;
    if (app.kind === "port") return `localhost:${app.target}`;
    if (app.kind === "url") {
      try { return new URL(app.target).host || app.target; } catch { return app.target || asset.path; }
    }
  }
  return asset.name || (asset.path || "").split("/").pop() || asset.path || "";
}

/** Short uppercase badge: file extension, or APP / PORT / URL for webapps. */
export function extBadge(asset) {
  const app = assetWebapp(asset);
  if (app?.kind === "port") return "PORT";
  if (app?.kind === "url") return "URL";
  if (asset?.kind === "webapp") return "APP";
  return (asset?.ext || "").toUpperCase();
}

/** Case-insensitive match on name / path / note / webapp title+target.
 *  Whitespace-separated terms must all match (AND). */
export function matchesQuery(asset, query) {
  const raw = (query || "").trim().toLowerCase();
  if (!raw) return true;
  const hay = [
    asset.path,
    asset.name,
    asset.note,
    asset.webapp?.title,
    asset.webapp?.target,
  ].filter(Boolean).join("\n").toLowerCase();
  return raw.split(/\s+/).every((term) => hay.includes(term));
}

/**
 * Apply the toolbar filters. Returns `{ list, counts, excluded }` where
 * `counts` holds per-type-chip totals computed BEFORE the type filter (so
 * each chip shows how many items tapping it would reveal), `list` is the
 * type-filtered result (unsorted) and `excluded.seen` / `excluded.hidden`
 * count the items that turning on just showSeen / showHidden would add to
 * `list` (empty-state hint).
 *
 * filters: { type, starredOnly, showSeen, showHidden, showMissing, query }
 * sticky:  optional Set of paths that bypass the starred / hidden filters —
 *          an item the user just un-starred or hid via a single-item action
 *          stays put instead of vanishing under their finger.
 */
export function filterAssets(assets, filters = {}, sticky = null) {
  const {
    type = "all",
    starredOnly = false,
    showSeen = false,
    showHidden = false,
    showMissing = false,
    query = "",
  } = filters;
  const counts = { all: 0 };
  for (const t of TYPE_TABS) counts[t.key] = 0;
  const excluded = { seen: 0, hidden: 0 };
  const list = [];
  for (const a of assets || []) {
    if (!a) continue;
    const isSticky = !!sticky?.has?.(a.path);
    if (starredOnly && !a.starred && !isSticky) continue;
    // Server already drops missing (non-starred) rows unless include_missing;
    // repeat it here so a cached include_missing=1 list paints correctly.
    if (!showMissing && a.exists === false && !a.starred) continue;
    if (!matchesQuery(a, query)) continue;
    const key = typeKeyOf(a);
    const offSeen = !showSeen && a.tier === "seen";
    const offHidden = !showHidden && a.hidden && !isSticky;
    if (offSeen || offHidden) {
      if (type === "all" || type === key) {
        if (!offHidden) excluded.seen += 1;
        else if (!offSeen) excluded.hidden += 1;
      }
      continue;
    }
    counts.all += 1;
    if (key in counts) counts[key] += 1;
    if (type === "all" || type === key) list.push(a);
  }
  return { list, counts, excluded };
}

const _nameCollator = typeof Intl !== "undefined"
  ? new Intl.Collator(undefined, { numeric: true, sensitivity: "base" })
  : null;
const _cmpName = (a, b) => (_nameCollator
  ? _nameCollator.compare(a, b)
  : a < b ? -1 : a > b ? 1 : 0);

/** Return a NEW array sorted by "recent" (last_seen desc), "name" (natural,
 *  asc) or "size" (desc, unknown sizes last). Ties break on path. */
export function sortAssets(list, sort = "recent") {
  const out = [...(list || [])];
  const byPath = (a, b) => _cmpName(a.path || "", b.path || "");
  if (sort === "name") {
    out.sort((a, b) => _cmpName(displayName(a), displayName(b)) || byPath(a, b));
  } else if (sort === "size") {
    out.sort((a, b) => {
      const sa = typeof a.size === "number" ? a.size : -1;
      const sb = typeof b.size === "number" ? b.size : -1;
      return sb - sa || byPath(a, b);
    });
  } else {
    out.sort((a, b) => (parseTs(b.last_seen_at) - parseTs(a.last_seen_at)) || byPath(a, b));
  }
  return out;
}

/** Pick the mark fields that were actually set. null / undefined = unchanged
 *  for starred / hidden; note: string sets ("" clears → null), null = unchanged. */
export function normalizeMarkFields(fields = {}) {
  const out = {};
  if (typeof fields.starred === "boolean") out.starred = fields.starred;
  if (typeof fields.hidden === "boolean") out.hidden = fields.hidden;
  if (typeof fields.note === "string") out.note = fields.note.trim() ? fields.note : null;
  return out;
}

// Fields already in normalized form (normalizeMarkFields output): a present
// `note: null` means "cleared", not "unchanged".
function _normalizedPatch(fields = {}) {
  const out = {};
  if (typeof fields.starred === "boolean") out.starred = fields.starred;
  if (typeof fields.hidden === "boolean") out.hidden = fields.hidden;
  if ("note" in fields && (fields.note === null || typeof fields.note === "string")) {
    out.note = fields.note && fields.note.trim() ? fields.note : null;
  }
  return out;
}

/** Return a new assets array with `fields` (normalized — see
 *  normalizeMarkFields) applied to every asset in `paths`. Returns the same
 *  array when nothing changes (cheap setState bail-out). */
export function patchAssetMarks(assets, paths, fields) {
  const patch = _normalizedPatch(fields);
  const keys = Object.keys(patch);
  if (!assets || !keys.length) return assets;
  const set = paths instanceof Set ? paths : new Set(paths || []);
  if (!set.size) return assets;
  let changed = false;
  const next = assets.map((a) => {
    if (!set.has(a.path)) return a;
    if (keys.every((k) => (a[k] ?? null) === patch[k])) return a;
    changed = true;
    return { ...a, ...patch };
  });
  return changed ? next : assets;
}

/** Apply a POST response `{ marks: { path: {starred, hidden, note} } }`. */
export function applyServerMarks(assets, marks, onlyPaths = null) {
  if (!assets || !marks) return assets;
  let changed = false;
  const next = assets.map((a) => {
    const m = marks[a.path];
    if (!m) return a;
    if (onlyPaths && !onlyPaths.has(a.path)) return a;
    const starred = !!m.starred;
    const hidden = !!m.hidden;
    const note = m.note || null;
    if (a.starred === starred && a.hidden === hidden && (a.note || null) === note) return a;
    changed = true;
    return { ...a, starred, hidden, note };
  });
  return changed ? next : assets;
}

/** Snapshot the mark fields of `paths` (for optimistic-update rollback). */
export function snapshotMarks(assets, paths) {
  const set = paths instanceof Set ? paths : new Set(paths || []);
  const snap = {};
  for (const a of assets || []) {
    if (set.has(a.path)) snap[a.path] = { starred: !!a.starred, hidden: !!a.hidden, note: a.note || null };
  }
  return snap;
}
