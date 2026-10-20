// Module-level cache for the Assets tab (same idea as lib/detailCache.js
// projectDetailCache): ProjectDetailPage remounts on every navigation, so
// re-entering the tab paints the last list instantly and refetches in the
// background. In-memory only; asset lists can run to thousands of rows, so
// keep only the most recently used few projects.
//
// Key: `${project}|${includeMissing ? 1 : 0}` → { assets, indexedAt, ts }

const MAX_ENTRIES = 8;
const store = new Map();

const keyFor = (project, includeMissing) => `${project}|${includeMissing ? 1 : 0}`;

export const assetsCache = {
  get(project, includeMissing) {
    const k = keyFor(project, includeMissing);
    const v = store.get(k);
    if (v) {
      // Refresh LRU position.
      store.delete(k);
      store.set(k, v);
    }
    return v;
  },
  set(project, includeMissing, value) {
    const k = keyFor(project, includeMissing);
    store.delete(k);
    store.set(k, { ...value, ts: Date.now() });
    while (store.size > MAX_ENTRIES) store.delete(store.keys().next().value);
  },
  /** Patch every cached list of `project` (both include_missing variants) —
   *  used for mark changes so the other variant doesn't repaint stale. */
  update(project, fn) {
    for (const flag of [false, true]) {
      const k = keyFor(project, flag);
      const v = store.get(k);
      if (!v) continue;
      const next = fn(v.assets);
      if (next !== v.assets) store.set(k, { ...v, assets: next });
    }
  },
  clear() { store.clear(); },
};
