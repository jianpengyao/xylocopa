import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Check, Eye, EyeOff, FileX, SlidersHorizontal, Star, StarOff } from "lucide-react";
import FilterTabs from "../FilterTabs";
import FluentEmoji from "../FluentEmoji";
import ImageLightbox from "../ImageLightbox";
import WebAppPreview from "../WebAppPreview";
import ProjectBrowserModal from "../ProjectBrowserModal";
import useDraft from "../../hooks/useDraft";
import usePageVisible from "../../hooks/usePageVisible";
import { useWsEvent } from "../../hooks/useWebSocket";
import { useWebSocketContext } from "../../contexts/WebSocketContext";
import { useToast } from "../../contexts/ToastContext";
import { downloadFile, fetchProjectAssets, updateAssetMarks } from "../../lib/api";
import { fileUrl } from "../../lib/urls";
import { forwardState } from "../../lib/nav";
import AssetTile from "./AssetTile";
import AssetDetailSheet from "./AssetDetailSheet";
import {
  SORT_OPTIONS,
  TYPE_TABS,
  applyServerMarks,
  assetArtifact,
  assetWebapp,
  displayName,
  filterAssets,
  normalizeMarkFields,
  patchAssetMarks,
  snapshotMarks,
  sortAssets,
} from "./assetFilters";
import { chatLinkFor, isLightboxAsset, lightboxItem, openActionFor } from "./assetMedia";
import { assetsCache } from "./assetsCache";

// Tiles rendered per page; more mount as the sentinel nears the viewport.
const PAGE_SIZE = 120;
// assets_changed fires once per sync batch; coalesce bursts into one refetch.
const REFETCH_DEBOUNCE_MS = 1200;
// POST /assets/marks accepts at most this many paths per request.
const MAX_MARK_PATHS = 1000;

const SORT_KEYS = new Set(SORT_OPTIONS.map((o) => o.value));
const TYPE_KEYS = new Set(TYPE_TABS.map((t) => t.key));

/** Sort + the default-off "also show" toggles, behind one toolbar button. */
function ViewMenu({ sort, onSort, toggles }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (!wrapRef.current?.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const modified = sort !== "recent" || toggles.some((t) => t.active);
  return (
    <div ref={wrapRef} className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label="View options"
        aria-haspopup="true"
        aria-expanded={open}
        title="Sort and show"
        className={`relative h-9 w-9 flex items-center justify-center rounded-lg border border-divider transition-colors ${
          open ? "bg-input text-body" : "bg-surface text-label hover:bg-input hover:text-body"
        }`}
      >
        <SlidersHorizontal className="w-4 h-4" />
        {modified && <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 rounded-full bg-accent" aria-hidden="true" />}
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1.5 z-30 w-60 rounded-xl bg-surface border border-divider shadow-popover p-1.5">
          <p className="px-2 pt-1 pb-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">Sort by</p>
          <div className="flex gap-0.5 p-0.5 rounded-lg bg-input">
            {SORT_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                onClick={() => onSort(o.value)}
                aria-pressed={sort === o.value}
                className={`flex-1 h-7 rounded-md text-xs font-medium transition-colors ${
                  sort === o.value ? "bg-surface text-heading shadow-sm" : "text-dim hover:text-body"
                }`}
              >
                {o.label}
              </button>
            ))}
          </div>
          <p className="px-2 pt-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">Also show</p>
          {toggles.map((t) => {
            const Icon = t.icon;
            return (
              <button
                key={t.key}
                type="button"
                onClick={t.onToggle}
                aria-pressed={t.active}
                title={t.title}
                className="w-full flex items-center gap-2.5 h-9 px-2 rounded-lg text-sm text-body hover:bg-input transition-colors"
              >
                <Icon className="w-4 h-4 shrink-0 text-dim" />
                <span className="flex-1 text-left truncate">{t.label}</span>
                {t.count > 0 && <span className="text-xs text-faint tabular-nums">{t.count}</span>}
                <span
                  className={`shrink-0 w-4 h-4 rounded flex items-center justify-center border transition-colors ${
                    t.active ? "bg-accent border-accent text-white" : "border-divider"
                  }`}
                >
                  {t.active && <Check className="w-3 h-3" strokeWidth={3} />}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function BulkButton({ onClick, disabled, icon, label, count, tone = "neutral", fill = false }) {
  const Icon = icon;
  const toneCls = tone === "star"
    ? "bg-amber-500 text-white hover:bg-amber-400"
    : "bg-input text-body hover:bg-elevated";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={count > 0 ? `${label} ${count}` : label}
      className={`flex-1 min-w-0 flex flex-col sm:flex-row items-center justify-center gap-0.5 sm:gap-1.5 min-h-[44px] sm:min-h-[40px] px-0.5 sm:px-1 py-1 sm:py-0 rounded-lg text-xs sm:text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors ${toneCls}`}
    >
      {/* Icon over label on phones: four side-by-side labels don't fit 375px */}
      <Icon className="w-4 h-4 shrink-0" fill={fill ? "currentColor" : "none"} />
      <span className="truncate max-w-full">{label}</span>
      {count > 0 && <span className="hidden sm:inline text-xs opacity-75">{count}</span>}
    </button>
  );
}

function SkeletonGrid() {
  return (
    <div className="grid grid-cols-3 sm:grid-cols-4 gap-x-2 gap-y-3" aria-hidden="true">
      {Array.from({ length: 12 }, (_, i) => (
        <div key={i}>
          <div className="aspect-square rounded-xl bg-skel animate-pulse" />
          <div className="h-2.5 mt-1.5 w-2/3 rounded bg-skel animate-pulse" />
        </div>
      ))}
    </div>
  );
}

/**
 * Project "Assets" tab: every file the chat already previews (images, videos,
 * html / web apps, pdf, office, 3D), with star / hide / note marks and
 * multi-select batch marking. Fetches only while `visible` (TaskGraphSection
 * pattern) and renders null otherwise.
 */
export default function AssetsSection({ projectName, visible }) {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const pageVisible = usePageVisible();

  // ---- Per-viewer prefs (swept by the project-rename prefix cleanup) ----
  const prefKey = (k) => `ui:project:${projectName}:assets:${k}`;
  const [typePref, setType] = useDraft(prefKey("type"), "all");
  const [sortPref, setSort] = useDraft(prefKey("sort"), "recent");
  const [query, setQuery] = useDraft(prefKey("q"), "");
  const [starredOnly, setStarredOnly] = useDraft(prefKey("starred"), false);
  const [showSeen, setShowSeen] = useDraft(prefKey("seen"), false);
  const [showHidden, setShowHidden] = useDraft(prefKey("hidden"), false);
  const [showMissing, setShowMissing] = useDraft(prefKey("missing"), false);
  const type = TYPE_KEYS.has(typePref) ? typePref : "all";
  const sort = SORT_KEYS.has(sortPref) ? sortPref : "recent";
  const includeMissing = !!showMissing;

  // ---- Data ----
  const [assets, setAssetsState] = useState(() => assetsCache.get(projectName, includeMissing)?.assets || null);
  const [error, setError] = useState(null);
  const assetsRef = useRef(assets);
  useEffect(() => { assetsRef.current = assets; }, [assets]);

  // Local mark intents, re-applied over any list fetched while they were in
  // flight (or confirmed after that fetch started) so a slow GET can't
  // resurrect pre-mutation marks. path → { fields, pending, at, base,
  // confirmed }: base = marks before the first in-flight request, confirmed
  // = server marks from the latest request that succeeded (rollback target).
  const overlayRef = useRef(new Map());

  const applyToList = useCallback((fn) => {
    setAssetsState((prev) => (prev ? fn(prev) : prev));
    assetsCache.update(projectName, fn);
  }, [projectName]);

  const seqRef = useRef(0);
  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    const startedAt = performance.now();
    try {
      const res = await fetchProjectAssets(projectName, { includeMissing });
      if (seq !== seqRef.current) return;
      let list = Array.isArray(res?.assets) ? res.assets : [];
      const overlay = overlayRef.current;
      if (overlay.size) {
        const live = new Map();
        for (const [p, e] of overlay) {
          if (e.pending > 0 || e.at >= startedAt) live.set(p, e.fields);
          else overlay.delete(p);
        }
        if (live.size) list = list.map((a) => (live.has(a.path) ? { ...a, ...live.get(a.path) } : a));
      }
      setAssetsState(list);
      setError(null);
      assetsCache.set(projectName, includeMissing, { assets: list, indexedAt: res?.indexed_at || null });
    } catch (err) {
      if (seq !== seqRef.current) return;
      setError(err?.message || "Failed to load assets");
    }
  }, [projectName, includeMissing]);
  const loadRef = useRef(load);
  useEffect(() => { loadRef.current = load; }, [load]);

  // Paint from cache on (re)entry / project switch, then refetch. Also
  // refetches when the app returns to the foreground.
  const lastProjectRef = useRef(projectName);
  useEffect(() => {
    if (lastProjectRef.current !== projectName) {
      lastProjectRef.current = projectName;
      overlayRef.current = new Map();
      setAssetsState(assetsCache.get(projectName, includeMissing)?.assets || null);
      setError(null);
    }
    if (!visible || !pageVisible) return;
    const cached = assetsCache.get(projectName, includeMissing);
    if (cached) setAssetsState(cached.assets);
    load();
  }, [visible, pageVisible, projectName, includeMissing, load]);

  const visibleRef = useRef(visible);
  useEffect(() => { visibleRef.current = visible; }, [visible]);
  const refetchTimer = useRef(null);
  const scheduleRefetch = useCallback(() => {
    clearTimeout(refetchTimer.current);
    refetchTimer.current = setTimeout(() => {
      refetchTimer.current = null;
      if (visibleRef.current) loadRef.current?.();
    }, REFETCH_DEBOUNCE_MS);
  }, []);
  useEffect(() => () => clearTimeout(refetchTimer.current), []);

  // Live updates: new refs indexed → debounced refetch (only while the tab
  // is showing; re-entry refetches anyway). Marks changed on any device →
  // patch in place, skipping paths with a local mutation in flight.
  useWsEvent((event) => {
    const d = event?.data;
    if (!d || d.project !== projectName) return;
    if (event.type === "assets_changed") {
      if (visibleRef.current) scheduleRefetch();
    } else if (event.type === "asset_marks_changed") {
      const overlay = overlayRef.current;
      const paths = (Array.isArray(d.paths) ? d.paths : []).filter((p) => !(overlay.get(p)?.pending > 0));
      // The event carries only the fields that were set; a present-but-null
      // note means it was cleared.
      const hasNote = Object.prototype.hasOwnProperty.call(d, "note");
      const patch = normalizeMarkFields({
        starred: d.starred, hidden: d.hidden, note: hasNote ? (d.note ?? "") : undefined,
      });
      if (!paths.length || !Object.keys(patch).length) return;
      const now = performance.now();
      for (const p of paths) {
        const prev = overlay.get(p);
        overlay.set(p, { fields: { ...prev?.fields, ...patch }, pending: 0, at: now });
      }
      applyToList((list) => patchAssetMarks(list, paths, patch));
    }
  });

  // WS reconnect: events may have been missed while offline.
  const { onOpen } = useWebSocketContext();
  useEffect(() => onOpen(() => { if (visibleRef.current) scheduleRefetch(); }), [onOpen, scheduleRefetch]);

  // ---- Marks (optimistic + rollback) ----
  const [sticky, setSticky] = useState(() => new Set());

  const mutateMarks = useCallback(async (paths, fields, { sticky: keepVisible = false } = {}) => {
    const patch = normalizeMarkFields(fields);
    if (!paths?.length || !Object.keys(patch).length) return false;
    const pathSet = new Set(paths);
    const snap = snapshotMarks(assetsRef.current, pathSet);
    const overlay = overlayRef.current;
    const startedAt = performance.now();
    for (const p of paths) {
      const prev = overlay.get(p);
      // Overlapping requests: keep the first one's base — later snapshots
      // already include earlier optimistic patches that may yet fail.
      const inFlight = prev?.pending > 0;
      overlay.set(p, {
        fields: { ...prev?.fields, ...patch },
        pending: (prev?.pending || 0) + 1,
        at: startedAt,
        base: inFlight ? prev.base : snap[p],
        confirmed: inFlight ? prev.confirmed : undefined,
      });
    }
    if (keepVisible) {
      setSticky((prev) => {
        if (paths.every((p) => prev.has(p))) return prev;
        const next = new Set(prev);
        for (const p of paths) next.add(p);
        return next;
      });
    }
    applyToList((list) => patchAssetMarks(list, pathSet, patch));

    // Settle overlay entries; returns the paths with no newer request pending.
    const settle = (onSettled) => {
      const done = new Set();
      const now = performance.now();
      for (const p of paths) {
        const e = overlay.get(p);
        if (!e) continue;
        e.pending = Math.max(0, e.pending - 1);
        e.at = now;
        if (e.pending === 0) { done.add(p); onSettled(p, e); }
      }
      return done;
    };

    try {
      const res = await updateAssetMarks(projectName, { paths, ...fields });
      const marks = res?.marks || {};
      // Remember what the server stored, for a later overlapping failure.
      for (const p of paths) {
        const e = overlay.get(p);
        if (!e) continue;
        const m = marks[p];
        const prior = e.confirmed || e.base;
        e.confirmed = m
          ? { starred: !!m.starred, hidden: !!m.hidden, note: m.note || null }
          : prior && { ...prior, ...patch };
      }
      const done = settle((p, e) => {
        if (marks[p]) e.fields = { ...e.confirmed };
      });
      if (done.size) applyToList((list) => applyServerMarks(list, marks, done));
      return true;
    } catch (err) {
      // Only the last request to settle rolls back, to the pre-burst base
      // plus whatever overlapping requests the server did accept.
      const rollback = {};
      settle((p, e) => {
        const target = e.confirmed || e.base;
        if (target) { e.fields = { ...target }; rollback[p] = target; }
      });
      applyToList((list) => applyServerMarks(list, rollback));
      toast.error(`Couldn't update ${paths.length === 1 ? "asset" : `${paths.length} assets`}: ${err?.message || "error"}`);
      return false;
    }
  }, [projectName, applyToList, toast]);

  // ---- Filtering ----
  const filters = useMemo(() => ({
    type, starredOnly: !!starredOnly, showSeen: !!showSeen, showHidden: !!showHidden,
    showMissing: !!showMissing, query,
  }), [type, starredOnly, showSeen, showHidden, showMissing, query]);
  const { list: filtered, counts, excluded } = useMemo(
    () => filterAssets(assets || [], filters, sticky),
    [assets, filters, sticky],
  );
  const visibleList = useMemo(() => sortAssets(filtered, sort), [filtered, sort]);
  const visibleListRef = useRef(visibleList);
  useEffect(() => { visibleListRef.current = visibleList; }, [visibleList]);

  // Filter / sort change → drop "keep visible" exemptions and restart paging.
  const [limit, setLimit] = useState(PAGE_SIZE);
  useEffect(() => {
    setSticky((prev) => (prev.size ? new Set() : prev));
    setLimit(PAGE_SIZE);
  }, [filters, sort, projectName]);

  // ---- Selection ----
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const enterSelect = useCallback((path) => {
    setSelecting(true);
    setSelected(path ? new Set([path]) : new Set());
  }, []);
  const exitSelect = useCallback(() => {
    setSelecting(false);
    setSelected(new Set());
  }, []);
  const toggleSelect = useCallback((path) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);
  // Batch actions apply to selected items that are still visible.
  const selectedVisible = useMemo(
    () => (selecting ? visibleList.filter((a) => selected.has(a.path)) : []),
    [selecting, visibleList, selected],
  );
  const allSelected = visibleList.length > 0 && selectedVisible.length === visibleList.length;
  const selectAll = () => setSelected(new Set(visibleList.map((a) => a.path)));
  const deselectAll = () => setSelected(new Set());

  const toStar = selectedVisible.filter((a) => !a.starred);
  const toUnstar = selectedVisible.filter((a) => a.starred);
  const toHide = selectedVisible.filter((a) => !a.hidden);
  const toUnhide = selectedVisible.filter((a) => a.hidden);

  const runBulk = async (targets, fields, verb) => {
    if (!targets.length || bulkBusy) return;
    setBulkBusy(true);
    const paths = targets.map((a) => a.path);
    let ok = true;
    for (let i = 0; i < paths.length && ok; i += MAX_MARK_PATHS) {
      ok = await mutateMarks(paths.slice(i, i + MAX_MARK_PATHS), fields);
    }
    setBulkBusy(false);
    if (ok) {
      toast.success(`${verb} ${paths.length} asset${paths.length !== 1 ? "s" : ""}`);
      exitSelect();
    }
  };

  // ---- Open / detail ----
  const [detailPath, setDetailPath] = useState(null);

  // Leaving the tab ends selection mode, closes the detail sheet and drops
  // keep-visible exemptions.
  useEffect(() => {
    if (visible) return;
    exitSelect();
    setDetailPath(null);
    setSticky((prev) => (prev.size ? new Set() : prev));
  }, [visible, exitSelect]);
  const [lightbox, setLightbox] = useState(null); // { media, initialIndex }
  const [preview, setPreview] = useState(null); // { project, path?, src?, filename }
  const [pdfFile, setPdfFile] = useState(null); // ProjectBrowserModal initialFile

  const openChat = useCallback((asset) => {
    const link = chatLinkFor(asset);
    if (link) navigate(link, { state: forwardState(location) });
  }, [navigate, location]);

  const download = useCallback((asset) => {
    downloadFile(fileUrl(projectName, asset.path), asset.name || asset.path.split("/").pop());
  }, [projectName]);

  const openAsset = useCallback((asset) => {
    const action = openActionFor(asset);
    switch (action) {
      case "lightbox": {
        const media = visibleListRef.current.filter(isLightboxAsset);
        const idx = media.findIndex((a) => a.path === asset.path);
        if (idx === -1) return;
        setLightbox({ media: media.map((a) => lightboxItem(projectName, a)), initialIndex: idx });
        return;
      }
      case "static":
        setPreview({ project: projectName, path: asset.path, filename: displayName(asset) });
        return;
      case "port":
        setPreview({ project: projectName, src: assetWebapp(asset).src, filename: displayName(asset) });
        return;
      case "port-chat":
        toast.info("Open this app from its card in chat");
        openChat(asset);
        return;
      case "url":
        window.open(assetWebapp(asset).target, "_blank", "noopener,noreferrer");
        return;
      case "artifact":
        window.open(assetArtifact(asset).url, "_blank", "noopener,noreferrer");
        return;
      case "pdf":
        setPdfFile({ path: asset.path, name: asset.name || asset.path.split("/").pop(), type: "file" });
        return;
      case "download":
        download(asset);
        return;
      default:
        setDetailPath(asset.path);
    }
  }, [projectName, openChat, download, toast]);

  const toggleStar = useCallback((asset) => {
    mutateMarks([asset.path], { starred: !asset.starred }, { sticky: true });
  }, [mutateMarks]);
  const showDetails = useCallback((asset) => setDetailPath(asset.path), []);

  const detailAsset = useMemo(
    () => (detailPath && assets ? assets.find((a) => a.path === detailPath) || null : null),
    [detailPath, assets],
  );
  useEffect(() => {
    if (detailPath && assets && !detailAsset) setDetailPath(null);
  }, [detailPath, assets, detailAsset]);

  // ---- Incremental rendering ----
  const sentinelRef = useRef(null);
  const hasMore = limit < visibleList.length;
  useEffect(() => {
    const el = sentinelRef.current;
    if (!visible || !hasMore || !el || typeof IntersectionObserver === "undefined") return undefined;
    // Re-created after every page so a still-visible sentinel loads the next one.
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) setLimit((l) => l + PAGE_SIZE);
    }, { rootMargin: "800px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [visible, hasMore, limit]);

  if (!visible) return null;

  const userFiltered = type !== "all" || !!query || !!starredOnly;
  const clearFilters = () => {
    setType("all");
    setQuery("");
    setStarredOnly(false);
  };
  const shown = visibleList.slice(0, limit);
  const typeTabs = TYPE_TABS.filter((t) => t.key === "all" || t.key === type || counts[t.key] > 0);
  const totalLoaded = assets?.length || 0;

  return (
    <div className="space-y-3">
      {/* Toolbar row: search + sort + select (or the selection header) */}
      {selecting ? (
        <div className="grid grid-cols-3 items-center h-9">
          <button
            type="button"
            onClick={allSelected ? deselectAll : selectAll}
            disabled={visibleList.length === 0}
            className="justify-self-start text-sm font-medium text-accent hover:opacity-80 disabled:opacity-40 transition-colors px-2 py-1 -ml-2"
          >
            {allSelected ? "Deselect All" : "Select All"}
          </button>
          <span className="justify-self-center text-sm text-label whitespace-nowrap">
            {selectedVisible.length > 0 ? `${selectedVisible.length} selected` : "Select assets"}
          </span>
          <button
            type="button"
            onClick={exitSelect}
            className="justify-self-end text-sm font-semibold text-accent hover:opacity-80 transition-colors px-2 py-1 -mr-2"
          >
            Done
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <div className="relative flex-1 min-w-0">
            <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-faint pointer-events-none" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <circle cx="11" cy="11" r="8" />
              <path strokeLinecap="round" d="m21 21-4.35-4.35" />
            </svg>
            <input
              type="search"
              autoComplete="off"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter files"
              aria-label="Filter assets by name or path"
              className="w-full h-9 pl-9 pr-8 rounded-lg bg-surface border border-divider text-sm text-body placeholder-hint focus:outline-none focus:ring-1 focus:ring-accent"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                aria-label="Clear filter"
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-faint hover:text-label transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" d="M6 18 18 6M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
          <button
            type="button"
            onClick={() => setStarredOnly((v) => !v)}
            aria-label="Starred only"
            aria-pressed={!!starredOnly}
            title="Only starred assets"
            className={`shrink-0 h-9 w-9 flex items-center justify-center rounded-lg border transition-colors ${
              starredOnly
                ? "border-amber-400/60 bg-amber-400/15 text-amber-500"
                : "border-divider bg-surface text-label hover:bg-input hover:text-body"
            }`}
          >
            <Star className="w-4 h-4" fill={starredOnly ? "currentColor" : "none"} />
          </button>
          <ViewMenu
            sort={sort}
            onSort={setSort}
            toggles={[
              { key: "seen", label: "Seen by agent", icon: Eye, active: !!showSeen, count: excluded.seen,
                onToggle: () => setShowSeen((v) => !v), title: "Include files the agent only read" },
              { key: "hidden", label: "Hidden", icon: EyeOff, active: !!showHidden, count: excluded.hidden,
                onToggle: () => setShowHidden((v) => !v), title: "Show hidden assets" },
              { key: "missing", label: "Missing", icon: FileX, active: !!showMissing,
                onToggle: () => setShowMissing((v) => !v), title: "Show files that no longer exist" },
            ]}
          />
          <button
            type="button"
            onClick={() => enterSelect()}
            disabled={visibleList.length === 0}
            className="shrink-0 h-9 px-3 rounded-lg bg-surface border border-divider text-sm font-medium text-label hover:bg-input hover:text-body disabled:opacity-40 transition-colors"
          >
            Select
          </button>
        </div>
      )}

      {/* Type chips — bleed to the page edge like the header tab row.
          Empty kinds are left out (the active one always stays). mb-0
          overrides space-y: FilterTabs' own pb-3 is the gap to the grid. */}
      <div className="-mx-4 mb-0">
        <FilterTabs tabs={typeTabs} active={type} onChange={setType} counts={assets ? counts : undefined} />
      </div>

      {error && assets && (
        <p className="text-xs text-danger px-1">Couldn't refresh: {error}</p>
      )}

      {/* Body */}
      {!assets ? (
        error ? (
          <div className="rounded-2xl bg-surface shadow-card py-8 px-4 text-center">
            <p className="text-sm text-danger">{error}</p>
            <button
              type="button"
              onClick={() => load()}
              className="mt-2 text-xs font-medium text-accent hover:opacity-80"
            >
              Retry
            </button>
          </div>
        ) : (
          <SkeletonGrid />
        )
      ) : totalLoaded === 0 ? (
        <div className="rounded-2xl bg-surface shadow-card py-10 text-center">
          <div className="mb-2 opacity-70">
            <FluentEmoji char="🖼️" size={28} />
          </div>
          <p className="text-sm text-dim">No assets yet</p>
          <p className="text-xs text-faint mt-1 px-6">
            Images, videos, web apps, documents and Claude artifacts agents share in chat collect here
          </p>
        </div>
      ) : visibleList.length === 0 ? (
        <div className="rounded-2xl bg-surface shadow-card py-10 text-center">
          <div className="mb-2 opacity-70">
            <FluentEmoji char="🔍" size={28} />
          </div>
          <p className="text-sm text-dim">
            {userFiltered ? "No assets match these filters" : "No assets to show"}
          </p>
          {/* Say what the default-off toggles are holding back — a project
              whose files were only ever Read otherwise looks empty. */}
          {(excluded.seen > 0 || excluded.hidden > 0) && (
            <p className="text-xs text-faint mt-1 px-6">
              {[
                excluded.seen > 0 && `${excluded.seen} file${excluded.seen !== 1 ? "s" : ""} only read by the agent`,
                excluded.hidden > 0 && `${excluded.hidden} hidden`,
              ].filter(Boolean).join(" · ")}
            </p>
          )}
          {(excluded.seen > 0 || excluded.hidden > 0 || userFiltered) && (
            <div className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 px-4">
              {excluded.seen > 0 && (
                <button
                  type="button"
                  onClick={() => setShowSeen(true)}
                  className="text-xs font-medium text-accent hover:opacity-80"
                >
                  Show seen by agent
                </button>
              )}
              {excluded.hidden > 0 && (
                <button
                  type="button"
                  onClick={() => setShowHidden(true)}
                  className="text-xs font-medium text-accent hover:opacity-80"
                >
                  Show hidden
                </button>
              )}
              {userFiltered && (
                <button
                  type="button"
                  onClick={clearFilters}
                  className="text-xs font-medium text-accent hover:opacity-80"
                >
                  Clear filters
                </button>
              )}
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-3 sm:grid-cols-4 gap-x-2 gap-y-3">
            {shown.map((asset) => (
              <AssetTile
                key={asset.path}
                project={projectName}
                asset={asset}
                selecting={selecting}
                selected={selecting && selected.has(asset.path)}
                onOpen={openAsset}
                onToggleSelect={toggleSelect}
                onEnterSelect={enterSelect}
                onToggleStar={toggleStar}
                onInfo={showDetails}
              />
            ))}
          </div>
          {hasMore && (
            <div ref={sentinelRef} className="flex justify-center pt-1">
              <button
                type="button"
                onClick={() => setLimit((l) => l + PAGE_SIZE)}
                className="text-xs font-medium text-dim hover:text-label px-3 py-1.5 rounded-lg hover:bg-input transition-colors"
              >
                Show more ({visibleList.length - limit})
              </button>
            </div>
          )}
          {/* Keep the last row clear of the fixed bulk bar */}
          {selecting && <div className="h-16" aria-hidden="true" />}
        </>
      )}

      {/* Bulk action bar (ProjectDetailPage pattern: clears BottomNavBar).
          Below md the right inset leaves room for the global attention FAB
          at its default spot (DraggableFab, right 20-64px) — it is z-50 and
          would otherwise sit on top of the Unhide button. */}
      {selecting && selectedVisible.length > 0 && (
        <div className="fixed bottom-20 left-0 right-0 z-20 pl-4 pr-[4.5rem] md:pr-4 pb-2 animate-bar-slide-up">
          <div className="max-w-xl mx-auto bg-surface border border-divider rounded-xl shadow-lg p-2 sm:p-3 flex items-center justify-center gap-1.5 sm:gap-2">
            <BulkButton
              onClick={() => runBulk(toStar, { starred: true }, "Starred")}
              disabled={bulkBusy || toStar.length === 0}
              icon={Star}
              fill
              tone="star"
              label="Star"
              count={toStar.length}
            />
            <BulkButton
              onClick={() => runBulk(toUnstar, { starred: false }, "Unstarred")}
              disabled={bulkBusy || toUnstar.length === 0}
              icon={StarOff}
              label="Unstar"
              count={toUnstar.length}
            />
            <BulkButton
              onClick={() => runBulk(toHide, { hidden: true }, "Hid")}
              disabled={bulkBusy || toHide.length === 0}
              icon={EyeOff}
              label="Hide"
              count={toHide.length}
            />
            <BulkButton
              onClick={() => runBulk(toUnhide, { hidden: false }, "Unhid")}
              disabled={bulkBusy || toUnhide.length === 0}
              icon={Eye}
              label="Unhide"
              count={toUnhide.length}
            />
          </div>
        </div>
      )}

      {detailAsset && (
        <AssetDetailSheet
          asset={detailAsset}
          onClose={() => setDetailPath(null)}
          onMark={mutateMarks}
          onOpen={(a) => { setDetailPath(null); openAsset(a); }}
          onOpenChat={(a) => { setDetailPath(null); openChat(a); }}
          onDownload={download}
        />
      )}

      {lightbox && (
        <ImageLightbox
          media={lightbox.media}
          initialIndex={lightbox.initialIndex}
          onClose={() => setLightbox(null)}
        />
      )}

      {preview && (
        <WebAppPreview
          project={preview.project}
          path={preview.path}
          src={preview.src}
          filename={preview.filename}
          onClose={() => setPreview(null)}
        />
      )}

      {pdfFile && (
        <ProjectBrowserModal
          project={projectName}
          initialFile={pdfFile}
          onClose={() => setPdfFile(null)}
        />
      )}
    </div>
  );
}
