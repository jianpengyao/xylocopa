import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import {
  createAgent,
  createProject,
  deleteProject as deleteProjectApi,
  archiveProject as archiveProjectApi,
  renameProject as renameProjectApi,
  fetchProjectBookmarks,
  deleteBookmark,
  scanAgents,
  fetchProjectFile,
  refreshClaudeMd,
  refreshClaudeMdStatus,
  discardClaudeMd,
  summarizeProgress,
  summarizeProgressStatus,
  applyProgressSummary,
  updateProjectSettings,
  rebuildInsights,
  fetchTaskCounts,
  searchMessages,
  searchProjectFiles,
  stopAgent,
  deleteAgent,
  markAgentRead,
  unstarSession,
  clog,
} from "../lib/api";
import { useAgents, useAgentsSeeded } from "../contexts/AgentsContext";
import { useFolders } from "../contexts/FoldersContext";
import { useWsEvent } from "../hooks/useWebSocket";
import BotIcon from "../components/BotIcon";
import PopoverArrow from "../components/PopoverArrow";
import ProjectRing from "../components/ProjectRing";
import EmojiPicker from "../components/EmojiPicker";
import AgentRow from "../components/AgentRow";
import DropboxSyncRow from "../components/dropbox/DropboxSyncRow";
import useDraft from "../hooks/useDraft";
import { relativeTime } from "../lib/formatters";
import { AGENT_STATUS_COLORS, AGENT_STATUS_TEXT_COLORS, modelDisplayName, agentBotState } from "../lib/constants";
import FilterTabs from "../components/FilterTabs";
import ProjectFileModal from "../components/ProjectFileModal";
import ProjectBrowserModal from "../components/ProjectBrowserModal";
import ClaudeMdDiffModal from "../components/ClaudeMdDiffModal";
import BookmarksSection from "../components/BookmarksSection";
import TaskGraphSection from "../components/TaskGraphSection";
import usePageVisible from "../hooks/usePageVisible";
import { useToast } from "../contexts/ToastContext";
import { forwardState } from "../lib/nav";
import { projectDetailCache, projectBriefCache, agentBriefCache } from "../lib/detailCache";
import ProjectDetailSkeleton from "../components/skeletons/ProjectDetailSkeleton";

const AGENT_TABS = [
  { key: "all", label: "All" },
  { key: "active", label: "Active" },
  { key: "insights", label: "Insights" },
  { key: "stopped", label: "Stopped" },
  { key: "graph", label: "Graph" },
];

function TaskRing({ total, completed, pct: pctOverride, size = 22 }) {
  if (!total && pctOverride == null) return null;
  const pct = pctOverride != null ? pctOverride : Math.round(completed / total * 100);
  const r = (size - 4) / 2, c = 2 * Math.PI * r;
  const offset = c * (1 - pct / 100);
  const color = pct >= 80 ? "#22c55e" : pct >= 50 ? "#eab308" : "#f87171";
  const half = size / 2;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0">
      <circle cx={half} cy={half} r={r} fill="transparent" stroke={color} strokeWidth={2} opacity={0.18} />
      <circle cx={half} cy={half} r={r} fill="transparent" stroke={color} strokeWidth={2}
        strokeLinecap="round" strokeDasharray={c} strokeDashoffset={offset}
        transform={`rotate(-90 ${half} ${half})`} style={{ transition: "stroke-dashoffset 0.6s ease" }} />
      <text x={half} y={half} textAnchor="middle" dominantBaseline="central"
        fill={color} style={{ fontSize: `${size * 0.32}px`, fontWeight: 700 }}>
        {pct}
      </text>
    </svg>
  );
}

function ProjectStatsPopover({ stats, onClose, containerRef }) {
  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose, containerRef]);

  const wTotal = stats?.weekly_total ?? 0;
  const wCompleted = stats?.weekly_completed ?? 0;
  const wFailed = stats?.weekly_failed ?? 0;
  const wTimeout = stats?.weekly_timeout ?? 0;
  const wCancelled = stats?.weekly_cancelled ?? 0;
  const wRejected = stats?.weekly_rejected ?? 0;
  const wRetries = stats?.weekly_retries ?? 0;
  const wPct = stats?.weekly_success_pct ?? 0;
  const ringColor = wTotal === 0 ? "#9ca3af" : wPct >= 80 ? "#22c55e" : wPct >= 50 ? "#eab308" : "#f87171";

  const rows = [
    { label: "Completed", count: wCompleted, color: "#22c55e" },
    { label: "Retries",   count: wRetries,   color: "#fb923c" },
    { label: "Failed",    count: wFailed,    color: "#f87171" },
    { label: "Timeout",   count: wTimeout,   color: "#f59e0b" },
    { label: "Dropped",   count: wCancelled, color: "#9ca3af" },
    { label: "Rejected",  count: wRejected,  color: "#a78bfa" },
  ].filter(r => r.count > 0);

  const daily = stats?.daily;
  const hasDaily = daily && daily.some(d => d.total > 0);

  return (
    <div className="absolute right-0 top-full mt-2 z-50" style={{ minWidth: 260 }}>
      <PopoverArrow size={12} align="right" offset={12} />
      <div className="bg-surface border border-edge rounded-xl shadow-lg overflow-hidden" style={{ boxShadow: "0 8px 30px var(--color-shadow)" }}>
        {/* Header */}
        <div className="px-4 pt-4 pb-3 flex items-center gap-3">
          <svg width="44" height="44" viewBox="0 0 44 44">
            <circle cx="22" cy="22" r="17" fill="transparent" stroke={ringColor} strokeWidth="3.5" opacity={0.18} />
            <circle cx="22" cy="22" r="17" fill="transparent" stroke={ringColor} strokeWidth="3.5"
              strokeLinecap="round" strokeDasharray={2 * Math.PI * 17} strokeDashoffset={2 * Math.PI * 17 * (1 - wPct / 100)}
              transform="rotate(-90 22 22)" style={{ transition: "stroke-dashoffset 0.6s ease" }} />
            <text x="22" y="22" textAnchor="middle" dominantBaseline="central"
              fill={ringColor} style={{ fontSize: "12px", fontWeight: 700 }}>{wPct}</text>
          </svg>
          <div>
            <div className="text-heading text-sm font-semibold">Weekly Success Rate</div>
            <div className="text-dim text-xs mt-0.5">{wTotal} tasks{wRetries > 0 ? ` · ${wRetries} retries` : ""}</div>
          </div>
        </div>

        <div className="border-t border-divider" />

        {/* Breakdown */}
        <div className="px-4 py-2.5 space-y-1.5">
          {rows.length === 0 ? (
            <div className="text-dim text-xs py-1">No completed tasks this week</div>
          ) : rows.map(r => (
            <div key={r.label} className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-2">
                <span className="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: r.color }} />
                <span className="text-body">{r.label}</span>
              </div>
              <span className="text-heading font-medium tabular-nums">{r.count}</span>
            </div>
          ))}
        </div>

        {/* Progress bar (adjusted for retries) */}
        {wTotal > 0 && (() => {
          const adjTotal = wTotal + wRetries;
          return (
            <div className="px-4 pb-3">
              <div className="h-1.5 rounded-full overflow-hidden flex" style={{ backgroundColor: "var(--color-input)" }}>
                {wCompleted > 0 && <div style={{ width: `${(wCompleted / adjTotal) * 100}%`, backgroundColor: "#22c55e" }} />}
                {wRetries > 0 && <div style={{ width: `${(wRetries / adjTotal) * 100}%`, backgroundColor: "#fb923c" }} />}
                {wFailed > 0 && <div style={{ width: `${(wFailed / adjTotal) * 100}%`, backgroundColor: "#f87171" }} />}
                {wTimeout > 0 && <div style={{ width: `${(wTimeout / adjTotal) * 100}%`, backgroundColor: "#f59e0b" }} />}
                {wCancelled > 0 && <div style={{ width: `${(wCancelled / adjTotal) * 100}%`, backgroundColor: "#9ca3af" }} />}
                {wRejected > 0 && <div style={{ width: `${(wRejected / adjTotal) * 100}%`, backgroundColor: "#a78bfa" }} />}
              </div>
            </div>
          );
        })()}

        {/* Daily sparkline */}
        {hasDaily && (() => {
          const W = 228, H = 72, PX = 8, PY = 14;
          const plotW = W - PX * 2, plotH = H - PY * 2;
          const points = daily.map((d, i) => ({
            x: PX + (i / Math.max(daily.length - 1, 1)) * plotW,
            pct: d.success_pct, total: d.total, date: d.date,
          }));
          const validPts = points.filter(p => p.pct != null);
          const yOf = (pct) => PY + plotH - (pct / 100) * plotH;
          const linePath = validPts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${yOf(p.pct).toFixed(1)}`).join(" ");
          const fillPath = validPts.length >= 2
            ? `${linePath} L${validPts[validPts.length - 1].x.toFixed(1)},${H - PY} L${validPts[0].x.toFixed(1)},${H - PY} Z` : "";
          const dayLabels = daily.map(d => ["S","M","T","W","T","F","S"][new Date(d.date + "T00:00:00").getDay()]);

          return (
            <div className="border-t border-divider px-4 py-2.5">
              <div className="text-faint text-[10px] uppercase tracking-wider font-medium mb-1.5">Daily Success Rate</div>
              <svg width={W} height={H + 14} viewBox={`0 0 ${W} ${H + 14}`} className="w-full" style={{ maxWidth: W }}>
                <defs>
                  <linearGradient id="projSparkFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#22c55e" stopOpacity="0.25" />
                    <stop offset="100%" stopColor="#22c55e" stopOpacity="0.02" />
                  </linearGradient>
                </defs>
                {[0, 50, 100].map(pct => (
                  <line key={pct} x1={PX} x2={W - PX} y1={yOf(pct)} y2={yOf(pct)}
                    stroke="var(--color-edge)" strokeWidth="0.5" strokeDasharray={pct === 50 ? "2,2" : "none"} opacity={0.5} />
                ))}
                {fillPath && <path d={fillPath} fill="url(#projSparkFill)" />}
                {validPts.length >= 2 && <path d={linePath} fill="none" stroke="#22c55e" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />}
                {validPts.map((p, i) => (
                  <circle key={i} cx={p.x} cy={yOf(p.pct)} r="2.5" fill="#22c55e" stroke="var(--color-surface)" strokeWidth="1" />
                ))}
                {validPts.map((p, i) => (
                  <text key={`lbl${i}`} x={p.x} y={yOf(p.pct) - 5} textAnchor="middle" fill="var(--color-heading)"
                    style={{ fontSize: "9px", fontWeight: 600 }}>{p.pct}%</text>
                ))}
                {points.map((p, i) => (
                  <text key={`day${i}`} x={p.x} y={H + 10} textAnchor="middle" fill="var(--color-dim)"
                    style={{ fontSize: "9px" }}>{dayLabels[i]}</text>
                ))}
              </svg>
            </div>
          );
        })()}

      </div>
    </div>
  );
}

function projectBotState(proj) {
  if (!proj.active) return "idle";
  if ((proj.agent_active || 0) > 0) return "running";
  if (proj.agent_count > 0) return "completed";
  return "idle";
}


export default function ProjectDetailPage({ theme, onToggleTheme }) {
  const { name } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const visible = usePageVisible();
  // Read shared folder list from context — no per-mount fetchAllFolders.
  const { folders, seeded: foldersSeeded } = useFolders();

  // Remember last-viewed project so the tab bar can auto-navigate back.
  // Clear returnedFrom since the user is actively viewing a project.
  useEffect(() => {
    if (name) localStorage.setItem("lastViewed:projects", name);
    sessionStorage.removeItem("returnedFrom:projects");
  }, [name]);

  // Seed initial state from module-level cache so re-entering a recently
  // viewed project paints instantly. loadData() still runs in the
  // background to refresh against the latest data. Falls back to
  // projectBriefCache (populated by ProjectsPage) so even a never-
  // visited project paints its header from the folder list snapshot.
  const initialCached = projectDetailCache.get(name);
  const [project, setProject] = useState(initialCached?.project || projectBriefCache.get(name) || null);
  // Stage-2 pilot: agents come from AgentsContext (single source of
  // truth shared with AgentsPage, the only writer). Filter by project
  // name; defensive `!is_subagent` keeps parity with the old
  // fetchProjectAgents endpoint which excludes subagents by default.
  // The filter ref keeps identity stable across renders so the
  // memoized selector inside useAgents() doesn't churn.
  const agentFilterRef = useRef(null);
  if (!agentFilterRef.current || agentFilterRef.current.__name !== name) {
    const f = (a) => a.project === name && !a.is_subagent;
    f.__name = name;
    agentFilterRef.current = f;
  }
  const agents = useAgents(agentFilterRef.current);
  const agentsSeeded = useAgentsSeeded();
  const [bookmarks, setBookmarks] = useState(initialCached?.bookmarks || []);
  // Cache hit → no spinner, just paint cached data and refetch silently.
  const [loading, setLoading] = useState(!initialCached);
  const [loadError, setLoadError] = useState(null);
  const [agentTab, setAgentTab] = useDraft(`ui:project:${name}:tab`, "active");

  // In-project search (agents + messages + files)
  const [search, setSearch] = useDraft(`ui:project:${name}:search`, "");
  const [messageResults, setMessageResults] = useState(null);
  const [messageSearchLoading, setMessageSearchLoading] = useState(false);
  const [fileResults, setFileResults] = useState(null);
  const [fileSearchLoading, setFileSearchLoading] = useState(false);
  const [openFile, setOpenFile] = useState(null); // {path, name} when clicked from search
  const searchTimerRef = useRef(null);

  const [showEmojiPicker, setShowEmojiPicker] = useState(false);
  const emojiAnchorRef = useRef(null);
  const [emojiAnchorRect, setEmojiAnchorRect] = useState(null);
  const toast = useToast();

  const [refreshing, setRefreshing] = useState(false);
  const [fileModal, setFileModal] = useState(null); // "CLAUDE.md" | "PROGRESS.md" | null

  // Multi-select state for the agent list (mirrors AgentsPage)
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  // Deferred-unstar state. See AgentsPage for rationale: tapping the inline
  // star only toggles a pending entry; the API call is deferred until the
  // user navigates away from the project page (route change / unmount).
  const [pendingUnstars, setPendingUnstars] = useState(() => new Map());
  const pendingUnstarsRef = useRef(pendingUnstars);
  useEffect(() => { pendingUnstarsRef.current = pendingUnstars; }, [pendingUnstars]);

  const handleTogglePendingStar = useCallback((agentId, project, sessionId) => {
    setPendingUnstars((prev) => {
      const next = new Map(prev);
      if (next.has(agentId)) next.delete(agentId);
      else next.set(agentId, { project, sessionId });
      return next;
    });
  }, []);

  const flushPendingUnstars = useCallback(() => {
    const map = pendingUnstarsRef.current;
    if (!map || map.size === 0) return;
    const entries = Array.from(map.entries());
    pendingUnstarsRef.current = new Map();
    setPendingUnstars(new Map());
    for (const [agentId, { project, sessionId }] of entries) {
      unstarSession(project, sessionId).catch((err) => {
        console.warn("[project] deferred unstar failed", agentId, err);
      });
    }
  }, []);

  // Deferred bookmark removal — same shape as pendingUnstars. Tapping the
  // bookmark icon only flips the pending entry; the row stays visible
  // (loadData keeps fetching it from the API since the row hasn't actually
  // been deleted) and the icon renders outline. Flush on navigate-away.
  const [pendingBookmarkDeletes, setPendingBookmarkDeletes] = useState(() => new Set());
  const pendingBookmarkDeletesRef = useRef(pendingBookmarkDeletes);
  useEffect(() => { pendingBookmarkDeletesRef.current = pendingBookmarkDeletes; }, [pendingBookmarkDeletes]);

  const handleTogglePendingBookmarkDelete = useCallback((messageId) => {
    setPendingBookmarkDeletes((prev) => {
      const next = new Set(prev);
      if (next.has(messageId)) next.delete(messageId);
      else next.add(messageId);
      clog(`[project] toggle pending bookmark delete msg=${messageId} now-pending=${next.has(messageId)} size=${next.size}`);
      return next;
    });
  }, []);

  const flushPendingBookmarkDeletes = useCallback(() => {
    const set = pendingBookmarkDeletesRef.current;
    clog(`[project] flushPendingBookmarkDeletes name=${name} size=${set?.size || 0}`);
    if (!set || set.size === 0) return;
    const ids = Array.from(set);
    pendingBookmarkDeletesRef.current = new Set();
    setPendingBookmarkDeletes(new Set());
    for (const messageId of ids) {
      clog(`[project] firing deleteBookmark project=${name} msg=${messageId}`);
      deleteBookmark(name, messageId).catch((err) => {
        console.warn("[project] deferred bookmark delete failed", messageId, err);
      });
    }
  }, [name]);

  // Flush on unmount (route change away from /projects/:name) and on
  // project name change (navigating between two projects without
  // unmounting). AgentsPage uses isActive instead — ProjectDetailPage isn't
  // in the keep-mounted shell, so unmount is the natural signal.
  useEffect(() => {
    return () => {
      flushPendingUnstars();
      flushPendingBookmarkDeletes();
    };
  }, [name, flushPendingUnstars, flushPendingBookmarkDeletes]);

  // Reconcile pending entries against the latest store snapshot — drop
  // entries for agents that are no longer starred (chat-page unstar, WS
  // from another device, 5s poll), so the icon doesn't show a stale
  // outline and we don't fire a redundant API on flush.
  useEffect(() => {
    setPendingUnstars((prev) => {
      if (prev.size === 0) return prev;
      let changed = false;
      const next = new Map(prev);
      for (const agentId of Array.from(next.keys())) {
        const a = agents.find((x) => x.id === agentId);
        if (!a || !a.starred) {
          next.delete(agentId);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [agents]);

  // Same reconcile for bookmarks — drop pending deletes for bookmarks
  // that have already disappeared from the list (deleted on another
  // device, project_update WS triggered a loadData() that no longer
  // returns them).
  useEffect(() => {
    setPendingBookmarkDeletes((prev) => {
      if (prev.size === 0) return prev;
      const liveIds = new Set(bookmarks.map((b) => b.message_id));
      let changed = false;
      const next = new Set(prev);
      for (const id of Array.from(next)) {
        if (!liveIds.has(id)) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [bookmarks]);

  const enterSelectMode = useCallback((preSelectId) => {
    setSelecting(true);
    setSelected(preSelectId ? new Set([preSelectId]) : new Set());
  }, []);
  const exitSelectMode = useCallback(() => {
    setSelecting(false);
    setSelected(new Set());
  }, []);
  const toggleOne = useCallback((id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const [showBrowser, setShowBrowser] = useState(false);
  const [fileExists, setFileExists] = useState({ "CLAUDE.md": null, "PROGRESS.md": null });
  const [refreshingClaudeMd, setRefreshingClaudeMd] = useState(false);
  const [claudeMdReady, setClaudeMdReady] = useState(false); // completed result available
  const [diffData, setDiffData] = useState(null); // response from refresh-claudemd
  const [summarizingProgress, setSummarizingProgress] = useState(false);
  const [progressReady, setProgressReady] = useState(false);
  const [progressDiffData, setProgressDiffData] = useState(null);

  // Rename
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [showRenameConfirm, setShowRenameConfirm] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const nameInputRef = useRef(null);

  // Task stats popover
  const [showStats, setShowStats] = useState(false);
  const [projectStats, setProjectStats] = useState(initialCached?.stats || null);
  const statsRingRef = useRef(null);

  // Activate / Archive / Delete
  const [activating, setActivating] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const showToast = useCallback((message, type = "success") => {
    if (type === "error") toast.error(message);
    else toast.success(message);
  }, [toast]);

  // Auto-select name input when rename starts (useEffect runs after DOM commit)
  useEffect(() => {
    if (editingName) nameInputRef.current?.select();
  }, [editingName]);

  // Rename handlers
  const startRename = () => {
    setNameDraft(project?.display_name || project?.name || "");
    setEditingName(true);
  };

  const requestRename = () => {
    const trimmed = nameDraft.trim();
    if (!trimmed || trimmed === (project?.display_name || project?.name)) {
      setEditingName(false);
      return;
    }
    setEditingName(false);
    setShowRenameConfirm(true);
  };

  const deriveSlug = (text) =>
    text.trim().toLowerCase().replace(/[^a-z0-9._-]/g, "-").replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "");

  const confirmRename = async () => {
    const slug = deriveSlug(nameDraft);
    if (!slug) {
      showToast("Invalid project name", "error");
      setShowRenameConfirm(false);
      return;
    }
    setRenaming(true);
    try {
      const displayName = nameDraft.trim() !== slug ? nameDraft.trim() : undefined;
      await renameProjectApi(name, slug, displayName);

      // Invalidate every in-memory cache that's keyed by project name OR
      // contains agents whose `project` field has just gone stale. The
      // post-rename navigate() remounts ProjectDetailPage; the next
      // load will repopulate from the backend with fresh data.
      projectDetailCache.invalidate(name);
      projectDetailCache.invalidate(slug);
      projectBriefCache.invalidate(name);
      // agentBriefCache rows have agent.project pointing at the old
      // project name; clearing wholesale is simpler than scanning. Next
      // AgentsPage / ProjectDetailPage fetch reseeds from the server.
      agentBriefCache.clear();

      // Clean up every per-project localStorage key. Use a prefix sweep
      // so we don't have to remember each useDraft / pref site by name.
      try {
        // Sweep any draft:ui:project:{name}:* and pref:*:{name} entries
        const oldDraftPrefix = `draft:ui:project:${name}:`;
        const oldGenerateSummary = `pref:generateSummary:${name}`;
        const toRemove = [];
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (!k) continue;
          if (k.startsWith(oldDraftPrefix)) toRemove.push(k);
          else if (k === oldGenerateSummary) toRemove.push(k);
        }
        toRemove.forEach((k) => localStorage.removeItem(k));

        // Update single-value keys that reference the project name.
        const lastViewed = localStorage.getItem("lastViewed:projects");
        if (lastViewed === name) {
          localStorage.setItem("lastViewed:projects", slug);
        }
        const orderRaw = localStorage.getItem("projects-custom-order");
        if (orderRaw) {
          const order = JSON.parse(orderRaw);
          const idx = order.indexOf(name);
          if (idx !== -1) {
            order[idx] = slug;
            localStorage.setItem("projects-custom-order", JSON.stringify(order));
          }
        }
      } catch { /* ignore localStorage errors */ }

      // Notify other open panes (split-screen, AgentsPage in another
      // route stack) so their lists invalidate and refetch.
      window.dispatchEvent(new CustomEvent("projects-data-changed"));
      window.dispatchEvent(new CustomEvent("agents-data-changed"));

      showToast("Project renamed!");
      setShowRenameConfirm(false);

      navigate(`/projects/${encodeURIComponent(slug)}`, { replace: true });
    } catch (err) {
      showToast("Rename failed: " + err.message, "error");
      setShowRenameConfirm(false);
    } finally {
      setRenaming(false);
    }
  };

  // Fetch stats + bookmarks. Folders come from FoldersContext (provider
  // owns the /api/projects fetch + 10s polling). Agents come from
  // AgentsContext. ProjectDetailPage owns nothing list-shaped — only the
  // per-project stats and bookmarks endpoints, which are scoped to `name`.
  const loadData = useCallback(async () => {
    try {
      const [stats, bookmarkList] = await Promise.all([
        fetchTaskCounts(name).catch(() => null),
        fetchProjectBookmarks(name).catch(() => []),
      ]);
      if (stats) setProjectStats(stats);
      const fresh = bookmarkList || [];
      setBookmarks(fresh);
      setLoadError(null);
      projectDetailCache.set(name, {
        project: projectDetailCache.get(name)?.project || null,
        stats: stats || null,
        bookmarks: fresh,
      });
    } catch (err) {
      console.error("Failed to load project data:", err);
      setLoadError(err.message || "Failed to load project data");
    } finally {
      setLoading(false);
    }
  }, [name]);

  // Derive `project` from context folders. Sync state on every change so
  // header/badges stay live when other panes mutate the project list.
  // Redirect to /projects only after the first fetch resolved (seeded)
  // and the project genuinely isn't in the list — avoids racing the
  // initial load.
  useEffect(() => {
    const folder = folders.find((f) => f.name === name);
    if (folder) {
      setProject(folder);
      const cached = projectDetailCache.get(name);
      projectDetailCache.set(name, {
        project: folder,
        stats: cached?.stats || null,
        bookmarks: cached?.bookmarks || [],
      });
      return;
    }
    if (foldersSeeded) {
      navigate("/projects", { replace: true });
    }
  }, [folders, foldersSeeded, name, navigate]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    scanAgents().catch(() => {});
    await loadData();
    // Minimum 400ms spinner display to prevent jarring sub-frame flicker
    setTimeout(() => setRefreshing(false), 400);
  }, [loadData]);

  const pollRef = useRef(null);
  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    setRefreshingClaudeMd(true);
    pollRef.current = setInterval(async () => {
      try {
        const res = await refreshClaudeMdStatus(name);
        if (res.status === "complete") {
          stopPolling();
          setRefreshingClaudeMd(false);
          setClaudeMdReady(true);
        } else if (res.status === "error") {
          stopPolling();
          setRefreshingClaudeMd(false);
          showToast(res.message || "Failed to analyze project — try again", "error");
        }
      } catch (err) {
        console.error("CLAUDE.md refresh poll failed:", err);
        stopPolling();
        setRefreshingClaudeMd(false);
        showToast("Failed to check refresh status", "error");
      }
    }, 2000);
  }, [name, stopPolling, showToast]);

  // Check for pending/complete job on mount
  useEffect(() => {
    if (!name) return;
    refreshClaudeMdStatus(name).then((res) => {
      if (res.status === "running") startPolling();
      else if (res.status === "complete") setClaudeMdReady(true);
    }).catch(() => {});
    return stopPolling;
  }, [name, startPolling, stopPolling]);

  const handleRefreshClaudeMd = useCallback(async () => {
    setRefreshingClaudeMd(true);
    setClaudeMdReady(false);
    try {
      await refreshClaudeMd(name);
      startPolling();
    } catch (err) {
      setRefreshingClaudeMd(false);
      showToast(err.message || "Failed to analyze project — try again", "error");
    }
  }, [name, showToast, startPolling]);

  const handleReviewUpdates = useCallback(async () => {
    try {
      const res = await refreshClaudeMdStatus(name);
      if (res.status === "complete") {
        setDiffData(res.data);
      } else {
        showToast("Update expired — run refresh again", "error");
        setClaudeMdReady(false);
      }
    } catch {
      showToast("Failed to load updates", "error");
    }
  }, [name, showToast]);

  // --- PROGRESS.md summary polling ---
  const progressPollRef = useRef(null);
  const stopProgressPolling = useCallback(() => {
    if (progressPollRef.current) { clearInterval(progressPollRef.current); progressPollRef.current = null; }
  }, []);

  const startProgressPolling = useCallback(() => {
    stopProgressPolling();
    setSummarizingProgress(true);
    progressPollRef.current = setInterval(async () => {
      try {
        const res = await summarizeProgressStatus(name);
        if (res.status === "complete") {
          stopProgressPolling();
          setSummarizingProgress(false);
          setProgressReady(true);
        } else if (res.status === "error") {
          stopProgressPolling();
          setSummarizingProgress(false);
          showToast(res.message || "Failed to summarize — try again", "error");
        }
      } catch (err) {
        console.error("PROGRESS.md summary poll failed:", err);
        stopProgressPolling();
        setSummarizingProgress(false);
        showToast("Failed to check summary status", "error");
      }
    }, 2000);
  }, [name, stopProgressPolling, showToast]);

  useEffect(() => {
    if (!name) return;
    summarizeProgressStatus(name).then((res) => {
      if (res.status === "running") startProgressPolling();
      else if (res.status === "complete") setProgressReady(true);
    }).catch(() => {});
    return stopProgressPolling;
  }, [name, startProgressPolling, stopProgressPolling]);

  const handleSummarizeProgress = useCallback(async () => {
    setSummarizingProgress(true);
    setProgressReady(false);
    try {
      await summarizeProgress(name);
      startProgressPolling();
    } catch (err) {
      setSummarizingProgress(false);
      showToast(err.message || "Failed to start summary — try again", "error");
    }
  }, [name, showToast, startProgressPolling]);

  const handleReviewProgressSummary = useCallback(async () => {
    try {
      const res = await summarizeProgressStatus(name);
      if (res.status === "complete") {
        setProgressDiffData(res.data);
      } else {
        showToast("Summary expired — run again", "error");
        setProgressReady(false);
      }
    } catch {
      showToast("Failed to load summary", "error");
    }
  }, [name, showToast]);

  const handleToggleAutoProgress = useCallback(async (enabled) => {
    try {
      const updated = await updateProjectSettings(name, { auto_progress_summary: enabled });
      setProject((prev) => prev ? { ...prev, auto_progress_summary: updated.auto_progress_summary } : prev);
      showToast(enabled ? "Daily auto-summary enabled" : "Daily auto-summary disabled");
    } catch (err) {
      showToast(err.message || "Failed to update setting", "error");
    }
  }, [name, showToast]);

  const handleToggleAiInsights = useCallback(async (enabled) => {
    try {
      const updated = await updateProjectSettings(name, { ai_insights: enabled });
      setProject((prev) => prev ? { ...prev, ai_insights: updated.ai_insights } : prev);
      showToast(enabled ? "AI-filtered insights enabled" : "AI-filtered insights disabled");
    } catch (err) {
      showToast(err.message || "Failed to update setting", "error");
    }
  }, [name, showToast]);

  const handleEmojiSelect = useCallback(async (char) => {
    setProject((prev) => prev ? { ...prev, emoji: char } : prev);
    try {
      await updateProjectSettings(name, { emoji: char });
      showToast(`Icon updated to ${char}`);
      window.dispatchEvent(new CustomEvent("projects-data-changed"));
    } catch (err) {
      showToast(err.message || "Failed to save icon", "error");
    }
  }, [name, showToast]);

  const handleEmojiClear = useCallback(async () => {
    setProject((prev) => prev ? { ...prev, emoji: null } : prev);
    try {
      await updateProjectSettings(name, { emoji: null });
      showToast("Icon reset to default");
      window.dispatchEvent(new CustomEvent("projects-data-changed"));
    } catch (err) {
      showToast(err.message || "Failed to reset icon", "error");
    }
  }, [name, showToast]);

  const openEmojiPicker = useCallback(() => {
    const rect = emojiAnchorRef.current?.getBoundingClientRect();
    if (rect) setEmojiAnchorRect(rect);
    setShowEmojiPicker(true);
  }, []);

  const closeEmojiPicker = useCallback(() => setShowEmojiPicker(false), []);

  const [rebuildingInsights, setRebuildingInsights] = useState(false);
  const handleRebuildInsights = useCallback(async () => {
    setRebuildingInsights(true);
    try {
      const res = await rebuildInsights(name);
      showToast(`Rebuilt insights: ${res.purged} purged, ${res.imported} imported`);
    } catch (err) {
      showToast(err.message || "Failed to rebuild insights", "error");
    } finally {
      setRebuildingInsights(false);
    }
  }, [name, showToast]);

  useEffect(() => {
    if (!visible) return;
    loadData();
    const interval = setInterval(loadData, 5000);
    return () => clearInterval(interval);
  }, [loadData, visible]);

  // Cross-device WS invalidation: bookmark added/edited/deleted, session
  // starred/unstarred, or project settings changed (emoji/toggles) on
  // another device. loadData refreshes bookmarks + stats; the project
  // emoji/toggles update via FoldersContext (which also listens).
  useWsEvent(useCallback((event) => {
    if (event.type === "project_update" && event.data?.name === name) {
      loadData();
    }
  }, [name, loadData]));

  // Check CLAUDE.md / PROGRESS.md existence
  useEffect(() => {
    if (!name) return;
    Promise.all([
      fetchProjectFile(name, "CLAUDE.md").catch(() => ({ exists: false })),
      fetchProjectFile(name, "PROGRESS.md").catch(() => ({ exists: false })),
    ]).then(([c, p]) => {
      setFileExists({ "CLAUDE.md": c.exists, "PROGRESS.md": p.exists });
    });
  }, [name]);

  // Debounced search: messages (server, project-scoped) + files (server)
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    const q = search.trim();
    if (q.length < 2) {
      setMessageResults(null);
      setFileResults(null);
      setMessageSearchLoading(false);
      setFileSearchLoading(false);
      return;
    }
    setMessageSearchLoading(true);
    setFileSearchLoading(true);
    searchTimerRef.current = setTimeout(() => {
      searchMessages(q, { project: name, includeSubagents: false })
        .then((data) => setMessageResults(data))
        .catch((err) => { console.error("searchMessages failed:", err); setMessageResults(null); })
        .finally(() => setMessageSearchLoading(false));
      searchProjectFiles(name, q)
        .then((data) => setFileResults(data))
        .catch((err) => { console.error("searchProjectFiles failed:", err); setFileResults(null); })
        .finally(() => setFileSearchLoading(false));
    }, 300);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [search, name]);

  // Client-side agent filter — by name / id / preview, applied on top of tab filter
  const tabFiltered = useMemo(() => (
    agentTab === "active"
      ? agents.filter((a) => a.status !== "STOPPED")
      : agentTab === "insights"
        ? agents.filter((a) => a.has_pending_suggestions || a.insight_status === "failed" || a.insight_status === "generating")
        : agentTab === "stopped"
          ? agents.filter((a) => a.status === "STOPPED")
          : agents
  ), [agents, agentTab]);

  const filtered = useMemo(() => {
    const raw = search.trim();
    if (!raw) return tabFiltered;
    const hasWildcard = raw.includes("*") || raw.includes("?");
    let predicate;
    if (hasWildcard) {
      // Convert glob to case-insensitive regex (* → .*?, ? → .)
      const escaped = raw.replace(/[.+^${}()|[\]\\]/g, "\\$&");
      const reSrc = escaped.replace(/\*/g, ".*?").replace(/\?/g, ".");
      const re = new RegExp(reSrc, "i");
      predicate = (s) => typeof s === "string" && re.test(s);
    } else {
      const q = raw.toLowerCase();
      predicate = (s) => typeof s === "string" && s.toLowerCase().includes(q);
    }
    return tabFiltered.filter((a) =>
      predicate(a.id) || predicate(a.name) || predicate(a.last_message_preview)
    );
  }, [tabFiltered, search]);

  // Pin starred agents to top with a STARRED section header — same pattern
  // as AgentsPage, mirrors BookmarksSection.
  const { starredAgents, regularAgents } = useMemo(() => {
    const starred = [];
    const regular = [];
    for (const a of filtered) {
      if (a.starred) starred.push(a);
      else regular.push(a);
    }
    return { starredAgents: starred, regularAgents: regular };
  }, [filtered]);

  // Selection partition for the bulk action bar
  const stoppableSelected = useMemo(
    () => filtered.filter((a) => selected.has(a.id) && a.status !== "STOPPED"),
    [filtered, selected],
  );
  const unreadSelected = useMemo(
    () => filtered.filter((a) => selected.has(a.id) && a.unread_count > 0),
    [filtered, selected],
  );
  const deletableSelected = useMemo(
    () => filtered.filter((a) => selected.has(a.id) && (a.status === "STOPPED" || a.status === "ERROR")),
    [filtered, selected],
  );
  const allSelected = filtered.length > 0 && selected.size === filtered.length;
  const selectAll = useCallback(() => setSelected(new Set(filtered.map((a) => a.id))), [filtered]);
  const deselectAll = useCallback(() => setSelected(new Set()), []);

  // Stage-2 pilot: ProjectDetailPage is read-only on the agent list.
  // Bulk actions dispatch 'agents-data-changed' which AgentsPage's
  // listener picks up and turns into a fresh seed() on the shared
  // store. No local refetch here — single-writer rule.
  const handleBulkMarkRead = useCallback(async () => {
    if (unreadSelected.length === 0 || bulkBusy) return;
    setBulkBusy(true);
    let ok = 0, failed = 0;
    for (const a of unreadSelected) {
      try { await markAgentRead(a.id); ok++; } catch { failed++; }
    }
    setBulkBusy(false);
    if (failed > 0) toast.error(`Marked ${ok} read, failed ${failed}`);
    else toast.success(`Marked ${ok} agent${ok !== 1 ? "s" : ""} as read`);
    exitSelectMode();
    window.dispatchEvent(new CustomEvent("agents-data-changed"));
  }, [unreadSelected, bulkBusy, toast, exitSelectMode]);

  const handleBulkStop = useCallback(async () => {
    if (stoppableSelected.length === 0 || bulkBusy) return;
    setBulkBusy(true);
    let ok = 0, failed = 0;
    for (const a of stoppableSelected) {
      try { await stopAgent(a.id); ok++; } catch { failed++; }
    }
    setBulkBusy(false);
    if (failed > 0) toast.error(`Stopped ${ok}, failed ${failed}`);
    else toast.success(`Stopped ${ok} agent${ok !== 1 ? "s" : ""}`);
    exitSelectMode();
    window.dispatchEvent(new CustomEvent("agents-data-changed"));
  }, [stoppableSelected, bulkBusy, toast, exitSelectMode]);

  const handleBulkDelete = useCallback(async () => {
    if (deletableSelected.length === 0 || bulkBusy) return;
    if (!confirm(`Permanently delete ${deletableSelected.length} agent${deletableSelected.length !== 1 ? "s" : ""}? This cannot be undone.`)) return;
    setBulkBusy(true);
    let ok = 0, failed = 0;
    for (const a of deletableSelected) {
      try { await deleteAgent(a.id); ok++; } catch { failed++; }
    }
    setBulkBusy(false);
    if (failed > 0) toast.error(`Deleted ${ok}, failed ${failed}`);
    else toast.success(`Deleted ${ok} agent${ok !== 1 ? "s" : ""}`);
    exitSelectMode();
    window.dispatchEvent(new CustomEvent("agents-data-changed"));
  }, [deletableSelected, bulkBusy, toast, exitSelectMode]);

  // Tab counts
  const tabCounts = {
    all: agents.length,
    active: agents.filter((a) => a.status !== "STOPPED").length,
    insights: agents.filter((a) => a.has_pending_suggestions || a.insight_status === "failed" || a.insight_status === "generating").length,
    stopped: agents.filter((a) => a.status === "STOPPED").length,
  };

  // Activate project
  const handleActivate = async () => {
    setActivating(true);
    try {
      await createProject({ name });
      showToast("Project activated!");
      await loadData();
    } catch (err) {
      showToast("Activate failed: " + err.message, "error");
    } finally {
      setActivating(false);
    }
  };

  // Archive project
  const handleArchive = async () => {
    setArchiving(true);
    try {
      await archiveProjectApi(name);
      showToast("Project archived");
      await loadData();
    } catch (err) {
      showToast("Archive failed: " + err.message, "error");
    } finally {
      setArchiving(false);
    }
  };

  // Delete project
  const handleDelete = async () => {
    setDeleting(true);
    try {
      await deleteProjectApi(name);
      projectDetailCache.invalidate(name);
      navigate("/projects", { replace: true });
    } catch (err) {
      showToast("Delete failed: " + err.message, "error");
    } finally {
      setDeleting(false);
      setShowDelete(false);
    }
  };

  // Full skeleton only when we have no project metadata at all. With a
  // brief seed we render the real header + filter tabs immediately and
  // only the agent list area gets a skeleton (see body below).
  if (loading && !project) {
    return <ProjectDetailSkeleton />;
  }

  if (!project && loadError) {
    return (
      <div className="px-4 py-10">
        <div className="bg-red-950/40 border border-red-800 rounded-xl p-4">
          <p className="text-danger text-sm">Failed to load project: {loadError}</p>
          <button type="button" onClick={loadData} className="mt-2 text-xs text-red-300 underline hover:text-red-200">
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!project) return null;

  return (
    <div className="h-full flex flex-col">
      {/* Fixed Header */}
      <div className="shrink-0 bg-page border-b border-divider relative z-10 safe-area-pt">
        <div className="max-w-2xl mx-auto px-4 pt-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => { localStorage.removeItem("lastViewed:projects"); navigate("/projects", { replace: true }); }}
              title="Back to projects"
              aria-label="Back to projects"
              className="shrink-0 w-7 h-9 -ml-2 flex items-center justify-center rounded-lg text-label hover:text-heading hover:bg-input transition-colors"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
              </svg>
            </button>
            <button
              ref={emojiAnchorRef}
              type="button"
              onClick={openEmojiPicker}
              title="Change project icon"
              className="shrink-0 rounded-lg p-1 -m-1 hover:bg-input transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <ProjectRing
                emoji={project.emoji}
                hasActiveAgents={(project.agent_active || 0) > 0}
                size={36}
              />
            </button>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                {editingName ? (
                  <input
                    ref={nameInputRef}
                    value={nameDraft}
                    onChange={(e) => setNameDraft(e.target.value)}
                    onBlur={requestRename}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") requestRename();
                      if (e.key === "Escape") setEditingName(false);
                    }}
                    maxLength={100}
                    className="text-lg font-bold text-heading min-w-0 flex-1 bg-input border border-accent rounded px-1.5 py-0.5 outline-none"
                  />
                ) : (
                  <h1
                    onDoubleClick={startRename}
                    title="Double-tap to rename"
                    className="text-lg font-bold text-heading truncate select-none"
                  >
                    {project.display_name || project.name}
                  </h1>
                )}
                {project.active ? (
                  <span className="shrink-0 px-2 py-0.5 text-[10px] font-bold uppercase rounded-full bg-emerald-500/15 text-emerald-400 tracking-wide">Active</span>
                ) : (
                  <span className="shrink-0 px-2 py-0.5 text-[10px] font-bold uppercase rounded-full bg-zinc-500/15 text-zinc-400 tracking-wide">Inactive</span>
                )}
                <div className="ml-auto flex items-center gap-1">
                  {(project.task_total || 0) > 0 && (
                    <div className="relative" ref={statsRingRef}>
                      <button
                        type="button"
                        onClick={() => {
                          if (!showStats) {
                            fetchTaskCounts(project.name).then(setProjectStats).catch(() => {});
                          }
                          setShowStats(v => !v);
                        }}
                        title={`Weekly: ${projectStats?.weekly_completed ?? (project.task_completed || 0)}/${projectStats?.weekly_total ?? project.task_total} tasks completed`}
                        className="shrink-0 flex items-center justify-center rounded-md hover:bg-white/5 transition-colors p-0.5"
                      >
                        <TaskRing total={projectStats?.weekly_total ?? project.task_total} completed={projectStats?.weekly_completed ?? (project.task_completed || 0)} pct={projectStats?.weekly_success_pct} size={24} />
                      </button>
                      {showStats && projectStats && (
                        <ProjectStatsPopover stats={projectStats} onClose={() => setShowStats(false)} containerRef={statsRingRef} />
                      )}
                    </div>
                  )}
                  {["CLAUDE.md", "PROGRESS.md"].map((fn) => {
                    const letter = fn === "CLAUDE.md" ? "C" : "P";
                    const exists = fileExists[fn];
                    const color = exists === false ? "text-zinc-500 hover:text-zinc-400" : "text-accent hover:opacity-80";
                    return (
                      <button
                        key={fn}
                        type="button"
                        onClick={() => setFileModal(fn)}
                        title={fn}
                        className={`relative shrink-0 w-6 h-6 flex items-center justify-center rounded-md hover:bg-white/5 transition-colors ${color}`}
                      >
                        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
                          <path strokeLinecap="round" strokeLinejoin="round" d="M14 2v6h6" />
                          <text x="12" y="17" textAnchor="middle" fill="currentColor" stroke="none" fontSize="7" fontWeight="700" fontFamily="system-ui">{letter}</text>
                        </svg>
                        {fn === "CLAUDE.md" && claudeMdReady && (
                          <span className="absolute -top-1 -right-1 flex h-3 w-3 items-center justify-center rounded-full bg-amber-500 text-[7px] font-bold text-white">1</span>
                        )}
                      </button>
                    );
                  })}
                  <button
                    type="button"
                    onClick={() => setShowBrowser(true)}
                    title="Browse files"
                    className="shrink-0 w-6 h-6 flex items-center justify-center rounded-md text-zinc-400 hover:text-zinc-300 hover:bg-white/5 transition-colors"
                  >
                    <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    onClick={handleRefresh}
                    title="Refresh"
                    className="w-6 h-6 flex items-center justify-center rounded-md hover:bg-white/5 transition-colors"
                  >
                    <svg className={`w-4 h-4 text-label ${refreshing ? "animate-spin-reverse" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div className="max-w-2xl mx-auto mt-3">
          <FilterTabs tabs={AGENT_TABS} active={agentTab} onChange={setAgentTab} counts={tabCounts} />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto overflow-x-hidden">
      <div className="pb-24 p-4 max-w-2xl mx-auto w-full space-y-5">

      {/* Search bar */}
      <div className="relative">
        <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-faint pointer-events-none" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <circle cx="11" cy="11" r="8" />
          <path strokeLinecap="round" d="m21 21-4.35-4.35" />
        </svg>
        <input
          type="search"
          autoComplete="off"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search agents, messages & files..."
          className="w-full h-9 pl-9 pr-8 rounded-lg bg-surface border border-divider text-sm text-body placeholder-hint focus:outline-none focus:ring-1 focus:ring-accent"
        />
        {search && (
          <button
            type="button"
            onClick={() => setSearch("")}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-faint hover:text-label transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" d="M6 18 18 6M6 6l12 12" />
            </svg>
          </button>
        )}
      </div>

      {/* Search results: messages + files */}
      {search.trim().length >= 2 && (
        <div className="space-y-3">
          {/* Messages */}
          {messageSearchLoading && (
            <p className="text-xs text-dim animate-pulse">Searching messages...</p>
          )}
          {messageResults && messageResults.results && messageResults.results.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs text-dim font-medium">
                {messageResults.total} message{messageResults.total !== 1 ? "s" : ""} found
              </p>
              {Object.entries(
                messageResults.results.reduce((acc, r) => {
                  const key = r.agent_id;
                  if (!acc[key]) acc[key] = { agent_name: r.agent_name, items: [] };
                  acc[key].items.push(r);
                  return acc;
                }, {})
              ).map(([agentId, group]) => (
                <div key={agentId} className="rounded-lg bg-surface border border-divider overflow-hidden">
                  <button
                    type="button"
                    onClick={() => navigate(`/agents/${agentId}`, { state: forwardState(location) })}
                    className="w-full text-left px-3 py-2 bg-elevated hover:bg-hover transition-colors"
                  >
                    <span className="text-xs font-semibold text-heading">{group.agent_name}</span>
                    <span className="text-[10px] text-faint ml-1">({group.items.length})</span>
                  </button>
                  {group.items.slice(0, 3).map((r) => (
                    <button
                      key={r.message_id}
                      type="button"
                      onClick={() => navigate(
                        `/agents/${r.agent_id}?focus=${encodeURIComponent(r.message_id)}&q=${encodeURIComponent(search)}`,
                        { state: forwardState(location) },
                      )}
                      className="w-full text-left px-3 py-1.5 border-t border-divider hover:bg-hover transition-colors"
                    >
                      <p className="text-xs text-body line-clamp-2">{r.content_snippet}</p>
                      <span className="text-[10px] text-faint">{relativeTime(r.created_at)}</span>
                    </button>
                  ))}
                  {group.items.length > 3 && (
                    <button
                      type="button"
                      onClick={() => navigate(`/agents/${agentId}`, { state: forwardState(location) })}
                      className="w-full text-left px-3 py-1 border-t border-divider text-[10px] text-accent hover:opacity-80 transition-colors"
                    >
                      +{group.items.length - 3} more
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
          {messageResults && messageResults.results && messageResults.results.length === 0 && !messageSearchLoading && (
            <p className="text-xs text-dim">No messages match "{search.trim()}"</p>
          )}

          {/* Files */}
          {fileSearchLoading && (
            <p className="text-xs text-dim animate-pulse">Searching files...</p>
          )}
          {fileResults && (fileResults.name_matches?.length > 0 || fileResults.content_matches?.length > 0) && (
            <div className="space-y-1">
              <p className="text-xs text-dim font-medium">
                {fileResults.total_files} file{fileResults.total_files !== 1 ? "s" : ""} found
                {fileResults.truncated ? " (search truncated)" : ""}
              </p>
              {/* Filename matches */}
              {fileResults.name_matches?.slice(0, 8).map((path) => {
                const baseName = path.split("/").pop();
                return (
                  <button
                    key={`name-${path}`}
                    type="button"
                    onClick={() => setOpenFile({ path, name: baseName, type: "file" })}
                    className="w-full text-left rounded-lg bg-surface border border-divider px-3 py-2 hover:bg-hover transition-colors flex items-center gap-2"
                  >
                    <svg className="w-3.5 h-3.5 text-zinc-400 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
                      <path strokeLinecap="round" strokeLinejoin="round" d="M14 2v6h6" />
                    </svg>
                    <span className="text-xs font-medium text-heading truncate">{baseName}</span>
                    <span className="text-[10px] text-faint truncate">{path}</span>
                  </button>
                );
              })}
              {/* Content matches */}
              {fileResults.content_matches?.map((file) => {
                const baseName = file.path.split("/").pop();
                return (
                  <div key={`content-${file.path}`} className="rounded-lg bg-surface border border-divider overflow-hidden">
                    <button
                      type="button"
                      onClick={() => setOpenFile({ path: file.path, name: baseName, type: "file" })}
                      className="w-full text-left px-3 py-2 bg-elevated hover:bg-hover transition-colors flex items-center gap-2"
                    >
                      <svg className="w-3.5 h-3.5 text-zinc-400 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
                        <path strokeLinecap="round" strokeLinejoin="round" d="M14 2v6h6" />
                      </svg>
                      <span className="text-xs font-semibold text-heading truncate">{baseName}</span>
                      <span className="text-[10px] text-faint truncate">{file.path}</span>
                      <span className="text-[10px] text-faint ml-auto shrink-0">({file.matches.length})</span>
                    </button>
                    {file.matches.map((m, i) => (
                      <button
                        key={i}
                        type="button"
                        onClick={() => setOpenFile({ path: file.path, name: baseName, type: "file" })}
                        className="w-full text-left px-3 py-1.5 border-t border-divider hover:bg-hover transition-colors flex items-start gap-2"
                      >
                        <span className="text-[10px] text-faint font-mono shrink-0 mt-0.5 w-8 text-right">{m.line}</span>
                        <code className="text-xs text-body font-mono break-all line-clamp-2">{m.text}</code>
                      </button>
                    ))}
                  </div>
                );
              })}
            </div>
          )}
          {fileResults && fileResults.total_files === 0 && !fileSearchLoading && (
            <p className="text-xs text-dim">No files match "{search.trim()}"</p>
          )}
        </div>
      )}

      {/* Inactive project banner */}
      {!project.active && (
        <div className="rounded-xl bg-amber-500/10 border border-amber-500/20 p-4 flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium text-amber-300">This project is inactive</p>
            <p className="text-xs text-amber-400/70 mt-0.5">Activate to create new agents and run tasks</p>
          </div>
          <button
            type="button"
            disabled={activating}
            onClick={handleActivate}
            className="shrink-0 px-4 py-2 rounded-lg bg-accent text-white text-sm font-semibold hover:bg-accent disabled:opacity-50 transition-colors"
          >
            {activating ? "Activating..." : "Activate"}
          </button>
        </div>
      )}

      {/* Agent list (hidden on Graph tab) */}
      {agentTab !== "graph" && (
        <div>
          {(loading || !agentsSeeded) && agents.length === 0 ? (
            // Header was seeded from briefCache; agent list still loading.
            // Leave the body empty rather than flashing ghost rows.
            <div className="min-h-[8rem]" />
          ) : filtered.length === 0 ? (
            <div className="text-center py-8 text-faint text-sm">
              No {agentTab === "all" ? "" : agentTab + " "}agents
            </div>
          ) : (
            <div className="space-y-3">
              {selecting && (
                <div className="grid grid-cols-3 items-center px-1 -mt-1 mb-1">
                  <button
                    type="button"
                    onClick={allSelected ? deselectAll : selectAll}
                    className="justify-self-start text-sm font-medium text-accent hover:opacity-80 transition-colors px-2 py-1"
                  >
                    {allSelected ? "Deselect All" : "Select All"}
                  </button>
                  <span className="justify-self-center text-sm text-label">
                    {selected.size > 0 ? `${selected.size} selected` : "Select agents"}
                  </span>
                  <button
                    type="button"
                    onClick={exitSelectMode}
                    className="justify-self-end text-sm font-semibold text-accent hover:opacity-80 transition-colors px-2 py-1"
                  >
                    Done
                  </button>
                </div>
              )}
              {[...starredAgents, ...regularAgents].map((agent) => (
                <AgentRow
                  key={agent.id}
                  agent={agent}
                  hideProjectTag
                  onClick={() => navigate(`/agents/${agent.id}`, { state: forwardState(location) })}
                  selecting={selecting}
                  selected={selected.has(agent.id)}
                  onToggle={toggleOne}
                  onEnterSelect={enterSelectMode}
                  isPendingUnstar={pendingUnstars.has(agent.id)}
                  onTogglePendingStar={handleTogglePendingStar}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Task graph view */}
      <TaskGraphSection projectName={name} visible={agentTab === "graph"} />

      <BookmarksSection
        projectName={name}
        items={bookmarks}
        pendingDeletes={pendingBookmarkDeletes}
        onTogglePendingDelete={handleTogglePendingBookmarkDelete}
        onPatched={(messageId, updated) => {
          setBookmarks((prev) =>
            prev.map((b) => (b.message_id === messageId ? { ...b, ...updated } : b)),
          );
        }}
      />

      {/* Project settings */}
      <div className="rounded-xl bg-surface shadow-card p-4 space-y-3">
        <h2 className="text-sm font-semibold text-label uppercase tracking-wider">Settings</h2>
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-body">Refresh CLAUDE.md</p>
            <p className="text-xs text-dim">AI-analyze project and propose updates</p>
          </div>
          {claudeMdReady ? (
            <button
              type="button"
              onClick={handleReviewUpdates}
              className="shrink-0 px-3 py-1 rounded-full text-xs font-medium transition-colors flex items-center gap-1.5 bg-amber-500/15 text-amber-600 hover:bg-amber-500/25 active:bg-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400 dark:hover:bg-amber-500/20 dark:active:bg-amber-500/25"
            >
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-500 opacity-75 dark:bg-amber-400" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500 dark:bg-amber-400" />
              </span>
              Review Updates
            </button>
          ) : (
            <button
              type="button"
              disabled={refreshingClaudeMd}
              onClick={handleRefreshClaudeMd}
              className="shrink-0 px-3 py-1 rounded-full text-xs font-medium transition-colors flex items-center gap-1.5 accent-tint-15 text-accent hover:accent-tint-25 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {refreshingClaudeMd ? (
                <>
                  <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="31.4" strokeLinecap="round" /></svg>
                  Analyzing...
                </>
              ) : (
                <>
                  <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor"><path fillRule="evenodd" d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z" clipRule="evenodd" /></svg>
                  Refresh CLAUDE.md
                </>
              )}
            </button>
          )}
        </div>
        {/* Daily summary settings hidden — per-agent insights still available */}
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-body">Rebuild Insights DB</p>
            <p className="text-xs text-dim">Re-import all insights from PROGRESS.md</p>
          </div>
          <button
            type="button"
            disabled={rebuildingInsights}
            onClick={handleRebuildInsights}
            className="shrink-0 px-3 py-1 rounded-full text-xs font-medium transition-colors accent-tint-15 text-accent hover:accent-tint-25 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {rebuildingInsights ? "Rebuilding..." : "Rebuild"}
          </button>
        </div>
        {project.active && (
          <DropboxSyncRow
            project={project}
            onProjectChange={(p) => setProject((prev) => ({ ...prev, ...p }))}
          />
        )}
        {project.active ? (
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="text-sm text-body">Archive Project</p>
              <p className="text-xs text-dim">Deactivate — code and history stay, re-activate anytime</p>
            </div>
            <button
              type="button"
              disabled={archiving}
              onClick={handleArchive}
              className="shrink-0 px-3 py-1 rounded-full text-xs font-medium transition-colors bg-amber-500/15 text-amber-600 hover:bg-amber-500/25 active:bg-amber-500/30 dark:bg-amber-500/10 dark:text-amber-400 dark:hover:bg-amber-500/20 dark:active:bg-amber-500/25 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {archiving ? "Archiving..." : "Archive"}
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="text-sm text-body">Activate Project</p>
              <p className="text-xs text-dim">Register this folder to create agents and run tasks</p>
            </div>
            <button
              type="button"
              disabled={activating}
              onClick={handleActivate}
              className="shrink-0 px-3 py-1 rounded-full text-xs font-medium transition-colors accent-tint-15 text-accent hover:accent-tint-25 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {activating ? "Activating..." : "Activate"}
            </button>
          </div>
        )}
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-body">Delete Project</p>
            <p className="text-xs text-dim">Move files to .trash</p>
          </div>
          <button
            type="button"
            onClick={() => setShowDelete(true)}
            className="shrink-0 px-3 py-1 rounded-full text-xs font-medium transition-colors bg-red-500/15 text-red-600 hover:bg-red-500/25 active:bg-red-500/30 dark:bg-red-500/10 dark:text-red-400 dark:hover:bg-red-500/20 dark:active:bg-red-500/25"
          >
            Delete
          </button>
        </div>
      </div>

      {/* Rename confirmation modal */}
      {showRenameConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="bg-surface rounded-2xl p-6 max-w-sm w-full space-y-4 shadow-card">
            <h3 className="text-lg font-bold text-heading">Rename Project?</h3>
            <p className="text-sm text-label">
              This will rename <span className="font-semibold text-heading">"{name}"</span> to{" "}
              <span className="font-semibold text-heading">"{deriveSlug(nameDraft)}"</span>.
            </p>
            <p className="text-xs text-dim">
              All agents, tasks, session references, and local files will be updated. This cannot be undone.
            </p>
            <div className="flex gap-3">
              <button
                type="button"
                disabled={renaming}
                onClick={confirmRename}
                className="flex-1 min-h-[44px] rounded-lg bg-accent hover:opacity-90 text-accent-ink font-semibold text-sm transition-colors disabled:opacity-50"
              >
                {renaming ? "Renaming..." : "Rename"}
              </button>
              <button
                type="button"
                onClick={() => setShowRenameConfirm(false)}
                className="flex-1 min-h-[44px] rounded-lg bg-input hover:bg-elevated text-body text-sm transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete confirmation modal */}
      {showDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="bg-surface rounded-2xl p-6 max-w-sm w-full space-y-4 shadow-card">
            <h3 className="text-lg font-bold text-heading">Delete "{project.display_name}"?</h3>
            <p className="text-sm text-label">
              This will remove the project record. Agents will remain in history. This cannot be undone.
            </p>
            <div className="flex gap-3">
              <button
                type="button"
                disabled={deleting}
                onClick={handleDelete}
                className="flex-1 min-h-[44px] rounded-lg bg-red-600 hover:bg-red-500 text-white font-semibold text-sm transition-colors disabled:opacity-50"
              >
                {deleting ? "Deleting..." : "Delete"}
              </button>
              <button
                type="button"
                onClick={() => setShowDelete(false)}
                className="flex-1 min-h-[44px] rounded-lg bg-input hover:bg-elevated text-body text-sm transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
      </div>
      </div>

      {fileModal && (
        <ProjectFileModal
          project={name}
          filename={fileModal}
          onClose={() => {
            setFileModal(null);
            // Refresh existence state after modal closes
            Promise.all([
              fetchProjectFile(name, "CLAUDE.md").catch(() => ({ exists: false })),
              fetchProjectFile(name, "PROGRESS.md").catch(() => ({ exists: false })),
            ]).then(([c, p]) => {
              setFileExists({ "CLAUDE.md": c.exists, "PROGRESS.md": p.exists });
            });
          }}
        />
      )}

      {showBrowser && (
        <ProjectBrowserModal
          project={name}
          onClose={() => setShowBrowser(false)}
        />
      )}

      {openFile && (
        <ProjectBrowserModal
          project={name}
          initialFile={openFile}
          onClose={() => setOpenFile(null)}
        />
      )}

      {diffData && (
        <ClaudeMdDiffModal
          data={diffData}
          project={name}
          onClose={() => { setDiffData(null); setClaudeMdReady(false); discardClaudeMd(name).catch(() => {}); }}
          onApplied={(lines, error) => {
            setDiffData(null);
            setClaudeMdReady(false);
            if (error) {
              showToast("Apply failed: " + error, "error");
            } else {
              showToast(`CLAUDE.md updated (${lines} lines)`);
            }
          }}
        />
      )}
      {progressDiffData && (
        <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4">
          <div className="bg-surface rounded-xl shadow-2xl max-w-2xl w-full max-h-[80vh] flex flex-col">
            <div className="flex items-center justify-between px-4 py-3 border-b border-border">
              <h3 className="text-sm font-semibold text-label">PROGRESS.md Summary</h3>
              <button type="button" onClick={() => { setProgressDiffData(null); setProgressReady(false); }} className="text-dim hover:text-body">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className="flex-1 overflow-auto p-4">
              {progressDiffData.message ? (
                <p className="text-sm text-dim">{progressDiffData.message}</p>
              ) : progressDiffData.diff ? (
                <pre className="text-xs font-mono whitespace-pre-wrap text-body">{progressDiffData.diff}</pre>
              ) : (
                <pre className="text-xs font-mono whitespace-pre-wrap text-body">{progressDiffData.proposed}</pre>
              )}
            </div>
            <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border">
              <button
                type="button"
                onClick={() => { setProgressDiffData(null); setProgressReady(false); }}
                className="px-3 py-1.5 rounded-lg text-xs text-dim hover:text-body transition-colors"
              >
                Discard
              </button>
              {!progressDiffData.message && (
                <button
                  type="button"
                  onClick={async () => {
                    try {
                      const res = await applyProgressSummary(name);
                      setProgressDiffData(null);
                      setProgressReady(false);
                      showToast(`PROGRESS.md updated (${res.lines} lines)`);
                    } catch (err) {
                      showToast("Apply failed: " + (err.message || "unknown error"), "error");
                    }
                  }}
                  className="px-3 py-1.5 rounded-lg bg-accent text-white text-xs font-medium hover:bg-accent transition-colors"
                >
                  Apply
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {showEmojiPicker && (
        <EmojiPicker
          current={project?.emoji || null}
          anchorRect={emojiAnchorRect}
          onSelect={handleEmojiSelect}
          onClear={handleEmojiClear}
          onClose={closeEmojiPicker}
        />
      )}

      {selecting && selected.size > 0 && (
        <div className="fixed bottom-20 left-0 right-0 z-20 px-4 pb-2 animate-bar-slide-up">
          <div className="max-w-xl mx-auto bg-surface border border-divider rounded-xl shadow-lg p-3 flex items-center justify-center gap-3">
            <button
              type="button"
              onClick={handleBulkMarkRead}
              disabled={bulkBusy || unreadSelected.length === 0}
              className="flex-1 flex items-center justify-center gap-2 min-h-[40px] rounded-lg bg-accent text-white text-sm font-medium hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 18h10a2 2 0 002-2V8H3v8a2 2 0 002 2zM17 8h2a2 2 0 010 4h-2M8 2v3M12 2v3" />
              </svg>
              {unreadSelected.length === 0 ? "Read" : `Read ${unreadSelected.length}`}
            </button>
            <button
              type="button"
              onClick={handleBulkStop}
              disabled={bulkBusy || stoppableSelected.length === 0}
              className="flex-1 flex items-center justify-center gap-2 min-h-[40px] rounded-lg bg-red-600 text-white text-sm font-medium hover:bg-red-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
              {stoppableSelected.length === 0 ? "Stop" : `Stop ${stoppableSelected.length}`}
            </button>
            <button
              type="button"
              onClick={handleBulkDelete}
              disabled={bulkBusy || deletableSelected.length === 0}
              className="flex-1 flex items-center justify-center gap-2 min-h-[40px] rounded-lg bg-red-600 text-white text-sm font-medium hover:bg-red-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
              {deletableSelected.length === 0 ? "Delete" : `Delete ${deletableSelected.length}`}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
