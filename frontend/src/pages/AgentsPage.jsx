import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { Bell, BellOff, Link2, ChevronDown, ChevronUp, X } from "lucide-react";
import { useNavigate, useLocation } from "react-router-dom";
import { fetchAgents, stopAgent, deleteAgent, scanAgents, wakeSyncAll, searchMessages, markAgentRead, updateNotificationSettings, fetchUnlinkedSessions, replayPendingUnlinked, adoptUnlinkedSession, convertAndAdoptUnlinkedSession, dropUnlinkedSession, unstarSession, clog } from "../lib/api";
import { relativeTime } from "../lib/formatters";
import { POLL_INTERVAL, SYNC_SETTLE_DELAY_GLOBAL } from "../lib/constants";
import PageHeader from "../components/PageHeader";
import FilterTabs from "../components/FilterTabs";
import AgentRow from "../components/AgentRow";
import useDraft from "../hooks/useDraft";
import useWebSocket, { useWsEvent, isAgentNotificationsEnabled, setAgentNotificationsEnabled } from "../hooks/useWebSocket";
import usePageVisible from "../hooks/usePageVisible";
import { useUnread } from "../contexts/UnreadContext";
import { useToast } from "../contexts/ToastContext";
import { forwardState } from "../lib/nav";
import { cacheAgentBriefs, agentBriefCache } from "../lib/detailCache";
import { useAgents, useAgentsActions } from "../contexts/AgentsContext";

const FILTER_TABS = [
  { key: "ALL", label: "All" },
  { key: "ACTIVE", label: "Active" },
  { key: "INSIGHTS", label: "Insights" },
  { key: "STOPPED", label: "Stopped" },
];

export default function AgentsPage({ theme, onToggleTheme, isActive = true }) {
  const navigate = useNavigate();
  const location = useLocation();
  const visible = usePageVisible();
  // Stage-2 pilot: agents now live in AgentsContext (single source of
  // truth shared with ProjectDetailPage). AgentsPage remains the only
  // writer — every setMany / seed / patchOne / prepend below is the
  // mutation it used to do via local setAgents.
  const agents = useAgents();
  const agentsActions = useAgentsActions();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useDraft("ui:agents:filter", "ALL");
  const [search, setSearch] = useDraft("ui:agents:search", "");
  const pollRef = useRef(null);

  // Multi-select state
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState(new Set());
  const [bulkStopping, setBulkStopping] = useState(false);
  const toast = useToast();

  // Deferred-unstar state. Tapping the inline star on a row only toggles
  // a pending entry — the API call (and the WS round-trip that would move
  // the row out of STARRED) is deferred until the user navigates away.
  // Mirrors the BookmarksSection soft-toggle intent: a mistap can be undone
  // with one more tap, no flicker. Chat-page star/unstar is unchanged
  // (immediate). Map<agentId, {project, sessionId}>.
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
        console.warn("[agents] deferred unstar failed", agentId, err);
      });
    }
  }, []);

  const showToast = useCallback((message, type = "success") => {
    if (type === "error") toast.error(message);
    else toast.success(message);
  }, [toast]);

  // Notification toggle
  const [agentNotifsOn, setAgentNotifsOn] = useState(() => isAgentNotificationsEnabled());

  const handleToggleAgentNotifs = useCallback(() => {
    const next = !agentNotifsOn;
    setAgentNotifsOn(next);
    setAgentNotificationsEnabled(next);
    updateNotificationSettings({ agents_enabled: next }).catch(() => console.warn("Failed to update notification settings"));
    showToast(next ? "Agent notifications enabled" : "Agent notifications disabled");
    window.dispatchEvent(new CustomEvent("agent-notifs-changed", { detail: { enabled: next } }));
  }, [agentNotifsOn, showToast]);

  // Message content search
  const [searchResults, setSearchResults] = useState(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const searchTimerRef = useRef(null);

  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    const q = search.trim();
    if (q.length < 2) {
      setSearchResults(null);
      setSearchLoading(false);
      return;
    }
    setSearchLoading(true);
    searchTimerRef.current = setTimeout(() => {
      searchMessages(q)
        .then((data) => {
          setSearchResults(data);
          setSearchLoading(false);
        })
        .catch((err) => {
          console.error('searchMessages failed:', err);
          setSearchResults(null);
          setSearchLoading(false);
        });
    }, 300);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [search]);

  const [refreshing, setRefreshing] = useState(false);
  const { syncFromAgents } = useUnread();

  // Authoritative full-replace fetch. Used for initial mount + every
  // explicit invalidation (refresh button, agents-data-changed,
  // post-bulk-op reloads). Drops deleted rows.
  const load = useCallback(async () => {
    const t0 = performance.now();
    try {
      const data = await fetchAgents();
      const t1 = performance.now();
      const list = Array.isArray(data) ? data : [];
      agentsActions.seed(list);
      // Badge totals derive from the same list the cards render.
      syncFromAgents(list);
      // Seed the brief cache so AgentChatPage can paint its header
      // (name / project / status) without waiting for fetchAgent.
      cacheAgentBriefs(list);
      clog(`[agents] fetch ${(t1 - t0).toFixed(0)}ms n=${list.length}`);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [agentsActions, syncFromAgents]);

  // Background 5s poll. Uses merge semantics so a transient API miss
  // doesn't blow away rows held by the store (e.g. a partial response
  // mid-write). Deletes are surfaced via the explicit-invalidation
  // path above (agents-data-changed → load() → seed()).
  const pollTick = useCallback(async () => {
    try {
      const data = await fetchAgents();
      const list = Array.isArray(data) ? data : [];
      agentsActions.setMany(list, "merge");
      cacheAgentBriefs(list);
      syncFromAgents(list);
    } catch (err) {
      // Silent — periodic poll, the next tick will retry.
      console.warn("[agents] poll failed:", err.message);
    }
  }, [agentsActions, syncFromAgents]);

  // --- Unlinked sessions ---
  const [unlinked, setUnlinked] = useState([]);
  const [unlinkedOpen, setUnlinkedOpen] = useState(true);
  const [adoptingId, setAdoptingId] = useState(null);

  const loadUnlinked = useCallback(async () => {
    try {
      const data = await fetchUnlinkedSessions();
      setUnlinked(Array.isArray(data) ? data : []);
    } catch {
      // Silently ignore — not critical
    }
  }, []);

  const handleAdopt = useCallback(async (session) => {
    const fileKey = (session.file || "").replace(/\.json$/, "") || session.session_id;
    setAdoptingId(fileKey);
    try {
      await adoptUnlinkedSession(fileKey, {
        project: session.project_name,
      });
      showToast(`Session confirmed → syncing ${session.project_name}`);
      setUnlinked((prev) => prev.filter((s) => s !== session));
      load(); // Refresh agent list
      window.dispatchEvent(new CustomEvent("agents-data-changed"));
    } catch (err) {
      showToast(err.message || "Failed to adopt session", "error");
    } finally {
      setAdoptingId(null);
    }
  }, [showToast, load]);

  const handleDrop = useCallback(async (session) => {
    const fileKey = (session.file || "").replace(/\.json$/, "");
    if (!fileKey) return;
    // Optimistic remove; reconcile on next poll if it failed.
    setUnlinked((prev) => prev.filter((s) => s !== session));
    try {
      await dropUnlinkedSession(fileKey);
    } catch (err) {
      showToast(err.message || "Failed to dismiss", "error");
      loadUnlinked();
    }
  }, [showToast, loadUnlinked]);

  const handleConvertAndAdopt = useCallback(async (session) => {
    const fileKey = (session.file || "").replace(/\.json$/, "");
    if (!fileKey) return;
    if (!window.confirm(
      "This will SIGTERM your terminal's claude process so it can be resumed " +
      "inside tmux and adopted. Your terminal claude will exit. Continue?"
    )) return;
    setAdoptingId(fileKey);
    try {
      await convertAndAdoptUnlinkedSession(fileKey, {
        project: session.project_name,
      });
      showToast(`Converted to tmux → syncing ${session.project_name}`);
      setUnlinked((prev) => prev.filter((s) => s !== session));
      load();
      window.dispatchEvent(new CustomEvent("agents-data-changed"));
    } catch (err) {
      showToast(err.message || "Failed to convert session", "error");
    } finally {
      setAdoptingId(null);
    }
  }, [showToast, load]);

  // Cross-pane sync: notification toggle + data refresh + star toggle
  useEffect(() => {
    const onNotifsChanged = (e) => setAgentNotifsOn(e.detail.enabled);
    const onDataChanged = () => { load(); loadUnlinked(); };
    const onStarChanged = (e) => {
      const { agentId, starred } = e.detail || {};
      if (!agentId) return;
      agentsActions.patchOne(agentId, { starred });
    };
    window.addEventListener("agent-notifs-changed", onNotifsChanged);
    window.addEventListener("agents-data-changed", onDataChanged);
    window.addEventListener("agent-star-changed", onStarChanged);
    return () => {
      window.removeEventListener("agent-notifs-changed", onNotifsChanged);
      window.removeEventListener("agents-data-changed", onDataChanged);
      window.removeEventListener("agent-star-changed", onStarChanged);
    };
  }, [load, loadUnlinked]);

  // Flush deferred unstars when the page becomes inactive (user navigated
  // off /agents). Also on unmount as a safety net. The WS handler picks up
  // session_star_changed afterward and patches the store; on return, the
  // row is correctly out of STARRED.
  useEffect(() => {
    if (isActive) return;
    flushPendingUnstars();
  }, [isActive, flushPendingUnstars]);

  useEffect(() => {
    return () => { flushPendingUnstars(); };
  }, [flushPendingUnstars]);

  // Reconcile pending entries against the latest store snapshot — if an
  // agent's `starred` flipped to false externally (chat page, another
  // device, the 5s poll), drop its pending entry so the icon stops showing
  // a stale "pending unstar" outline and we don't fire a redundant API.
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

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    // Fire scan + wake + replay in parallel, then wait for sync loops to import.
    // replayPendingUnlinked picks up SessionStart events the hook stashed
    // while the backend was offline.
    await Promise.allSettled([scanAgents(), wakeSyncAll(), replayPendingUnlinked()]);
    await new Promise((r) => setTimeout(r, SYNC_SETTLE_DELAY_GLOBAL));
    await Promise.all([load(), loadUnlinked()]);
    setTimeout(() => setRefreshing(false), 400);
  }, [load, loadUnlinked]);

  // Initial seed — fires on first AgentsPage mount regardless of
  // isActive/visible. The store is the source of truth for both this
  // page AND ProjectDetailPage (Stage-2 pilot); if the user lands on
  // /projects/foo first, AgentsPage is keep-mounted under the page
  // shell but never visible/isActive — without this, the project
  // page's filtered view would be empty until the user switched tabs.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    clog("[agents] initial seed");
    load();
    loadUnlinked();
  }, [load, loadUnlinked]);

  // Periodic 5s poll — gated on visibility + active so we don't burn
  // CPU on pages the user isn't looking at. WS handles the
  // foreground-update path; this is just the safety net.
  // Fire one tick immediately on activate — without this, returning from
  // background (or switching to the Agents tab) shows up to 5s of stale
  // data before the first setInterval tick lands. The mount-time seed
  // doesn't cover this path because seededRef persists across activations.
  useEffect(() => {
    if (!visible || !isActive) return;
    clog(`[agents] activate visible=${visible} isActive=${isActive}`);
    pollTick();
    loadUnlinked();
    pollRef.current = setInterval(() => { pollTick(); loadUnlinked(); }, POLL_INTERVAL);
    return () => clearInterval(pollRef.current);
  }, [pollTick, loadUnlinked, visible, isActive]);

  // Real-time status updates via WebSocket (agent_update events).
  // Backend now attaches unread_count / last_message_preview / last_message_at
  // so the badge and preview update without waiting for the 5s poll tick.
  // patchOne() preserves keys the WS payload doesn't carry — important
  // because emit_agent_update sends sparse fields, not the full brief.
  useWsEvent(useCallback((event) => {
    if (event.type !== "agent_update") return;
    const d = event.data || {};
    const { agent_id } = d;
    if (!agent_id) return;
    // Skip subagents — GET /api/agents filters them out, and PATCH_ONE
    // would otherwise upsert an unknown subagent id as a partial row
    // (AgentsContext.jsx:123-128). Other subscribers (AgentChatPage on a
    // direct subagent URL, parent-view cards) still receive the event.
    if (d.is_subagent) return;
    const partial = {};
    if (d.status !== undefined) partial.status = d.status;
    if (d.unread_count !== undefined) partial.unread_count = d.unread_count;
    if (d.last_message_preview !== undefined) partial.last_message_preview = d.last_message_preview;
    if (d.last_message_at !== undefined) partial.last_message_at = d.last_message_at;
    if (d.has_pending_suggestions !== undefined) partial.has_pending_suggestions = d.has_pending_suggestions;
    if (d.insight_status !== undefined) partial.insight_status = d.insight_status;
    if (d.deferred_to !== undefined) partial.deferred_to = d.deferred_to;
    if (d.muted !== undefined) partial.muted = d.muted;
    if (d.name !== undefined) partial.name = d.name;
    if (Object.keys(partial).length > 0) {
      agentsActions.patchOne(agent_id, partial);
      // Mirror the patch into agentBriefCache so AgentChatPage's
      // first-paint seed (line ~2811) sees fresh state on cold-mount
      // (deep link / reload) — not just on warm navigation from here.
      const existing = agentBriefCache.get(agent_id);
      if (existing) agentBriefCache.set(agent_id, { ...existing, ...partial });
    }
  }, [agentsActions]));

  // New agent appearance via WebSocket (agent_created events).
  // Backend emits this right after a new Agent row is committed, with the
  // full AgentBrief payload — prepend directly, no follow-up fetch.
  // Mirrors how task_update drives TasksPage's loadTasks on new tasks.
  useWsEvent(useCallback((event) => {
    if (event.type !== "agent_created") return;
    const newAgent = event.data;
    if (!newAgent?.id) return;
    console.log('[ws] agent_created', newAgent.id?.slice(0, 8), newAgent.status, newAgent.project);
    agentsActions.prepend(newAgent);
  }, [agentsActions]));

  // Star toggled from any device → patch the shared store so STARRED
  // section regrouping happens immediately, not after the 5s poll.
  // Backend includes agent_id in the payload (resolved from session_id) so
  // we can patch by id without maintaining a session→agent reverse index.
  useWsEvent(useCallback((event) => {
    if (event.type !== "session_star_changed") return;
    const { agent_id, starred } = event.data || {};
    if (!agent_id) return;
    agentsActions.patchOne(agent_id, { starred: !!starred });
  }, [agentsActions]));

  // Double-tap nav: scroll to first unread agent
  useEffect(() => {
    const handler = (e) => {
      if (e.detail?.tab !== "agents") return;
      const el = document.querySelector("[data-unread='1']");
      if (!el) return;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("ring-2", "ring-accent");
      setTimeout(() => el.classList.remove("ring-2", "ring-accent"), 1500);
    };
    window.addEventListener("nav-scroll-to-unread", handler);
    return () => window.removeEventListener("nav-scroll-to-unread", handler);
  }, []);

  const statusFiltered = useMemo(() =>
    filter === "ALL"
      ? agents
      : filter === "ACTIVE"
        ? agents.filter((a) => a.status !== "STOPPED")
        : filter === "INSIGHTS"
          ? agents.filter((a) => a.has_pending_suggestions || a.insight_status === "failed" || a.insight_status === "generating")
          : agents.filter((a) => a.status === "STOPPED"),
    [agents, filter]);

  const filtered = useMemo(() => {
    let list = search.trim()
      ? statusFiltered.filter((a) => {
          const q = search.toLowerCase();
          return (
            a.id?.toLowerCase().includes(q) ||
            a.name?.toLowerCase().includes(q) ||
            a.project?.toLowerCase().includes(q) ||
            a.last_message_preview?.toLowerCase().includes(q)
          );
        })
      : statusFiltered;
    list = [...list].sort((a, b) => {
      const ta = a.last_message_at || a.created_at;
      const tb = b.last_message_at || b.created_at;
      return new Date(tb) - new Date(ta);
    });
    return list;
  }, [statusFiltered, search]);

  // Split filtered into active vs deferred. Only group deferred separately
  // on Active — that tab is the "what's live" view where snoozed agents
  // would be noise. On All / Insights / Stopped, deferred agents render
  // inline. Truthy `deferred_to` is the source of truth: the dispatcher
  // sweep (agent_dispatcher._sweep_expired_defers) clears it on expiry and
  // emits agent_update, so we don't need a `> now` time comparison here.
  const { activeAgents, deferredAgents } = useMemo(() => {
    if (filter !== "ACTIVE") {
      return { activeAgents: filtered, deferredAgents: [] };
    }
    const active = [];
    const deferred = [];
    for (const a of filtered) {
      if (a.deferred_to) deferred.push(a);
      else active.push(a);
    }
    deferred.sort((a, b) => new Date(a.deferred_to) - new Date(b.deferred_to));
    return { activeAgents: active, deferredAgents: deferred };
  }, [filtered, filter]);

  // Pin starred agents to top with a STARRED section header (like Bookmarks).
  // The split applies to the non-deferred portion of the current tab.
  const { starredAgents, regularAgents } = useMemo(() => {
    const starred = [];
    const regular = [];
    for (const a of activeAgents) {
      if (a.starred) starred.push(a);
      else regular.push(a);
    }
    return { starredAgents: starred, regularAgents: regular };
  }, [activeAgents]);

  const [showDeferred, setShowDeferred] = useDraft("ui:agents:showDeferred", false);

  const enterSelectMode = (preSelectId) => {
    setSelecting(true);
    setSelected(preSelectId ? new Set([preSelectId]) : new Set());
  };

  const exitSelectMode = () => {
    setSelecting(false);
    setSelected(new Set());
  };

  const toggleOne = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAll = () => {
    setSelected(new Set(filtered.map((a) => a.id)));
  };

  const deselectAll = () => {
    setSelected(new Set());
  };

  const allSelected = filtered.length > 0 && selected.size === filtered.length;

  const filterCounts = useMemo(() => ({
    ALL: agents.length,
    ACTIVE: agents.filter(a => a.status !== "STOPPED").length,
    STOPPED: agents.filter(a => a.status === "STOPPED").length,
    INSIGHTS: agents.filter(a => a.has_pending_suggestions || a.insight_status === "failed" || a.insight_status === "generating").length,
  }), [agents]);

  // Count how many selected agents are stoppable (not already stopped)
  const stoppableSelected = filtered.filter(
    (a) => selected.has(a.id) && a.status !== "STOPPED"
  );

  const unreadSelected = filtered.filter(
    (a) => selected.has(a.id) && a.unread_count > 0
  );

  const [bulkMarking, setBulkMarking] = useState(false);

  const handleBulkMarkRead = async () => {
    if (unreadSelected.length === 0) return;
    setBulkMarking(true);
    let marked = 0;
    let failed = 0;
    for (const agent of unreadSelected) {
      try {
        await markAgentRead(agent.id);
        marked++;
      } catch {
        failed++;
      }
    }
    setBulkMarking(false);
    if (failed > 0) {
      showToast(`Marked ${marked} read, failed ${failed}`, "error");
    } else {
      showToast(`Marked ${marked} agent${marked !== 1 ? "s" : ""} as read`);
    }
    setSelected(new Set());
    setSelecting(false);
    load();
    window.dispatchEvent(new CustomEvent("agents-data-changed"));
  };

  const handleBulkStop = async () => {
    if (stoppableSelected.length === 0) return;
    setBulkStopping(true);
    let stopped = 0;
    let failed = 0;
    for (const agent of stoppableSelected) {
      try {
        await stopAgent(agent.id);
        stopped++;
      } catch {
        failed++;
      }
    }
    setBulkStopping(false);
    if (failed > 0) {
      showToast(`Stopped ${stopped}, failed ${failed}`, "error");
    } else {
      showToast(`Stopped ${stopped} agent${stopped !== 1 ? "s" : ""}`);
    }
    setSelected(new Set());
    setSelecting(false);
    load();
    window.dispatchEvent(new CustomEvent("agents-data-changed"));
  };

  const deletableSelected = filtered.filter(
    (a) => selected.has(a.id) && (a.status === "STOPPED" || a.status === "ERROR")
  );

  const [bulkDeleting, setBulkDeleting] = useState(false);

  const handleBulkDelete = async () => {
    if (deletableSelected.length === 0) return;
    if (!confirm(`Permanently delete ${deletableSelected.length} agent${deletableSelected.length !== 1 ? "s" : ""} and all their messages? This cannot be undone.`)) return;
    setBulkDeleting(true);
    let deleted = 0;
    let failed = 0;
    for (const agent of deletableSelected) {
      try {
        await deleteAgent(agent.id);
        deleted++;
      } catch {
        failed++;
      }
    }
    setBulkDeleting(false);
    if (failed > 0) {
      showToast(`Deleted ${deleted}, failed ${failed}`, "error");
    } else {
      showToast(`Deleted ${deleted} agent${deleted !== 1 ? "s" : ""}`);
    }
    setSelected(new Set());
    setSelecting(false);
    load();
    window.dispatchEvent(new CustomEvent("agents-data-changed"));
  };

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="Agents"
        theme={theme}
        onToggleTheme={onToggleTheme}
        actions={!selecting ? (
          <div className="flex items-center gap-1 shrink-0">
            <button
              type="button"
              onClick={handleToggleAgentNotifs}
              title={agentNotifsOn ? "Mute all agent notifications" : "Unmute all agent notifications"}
              className={`w-8 h-8 flex items-center justify-center rounded-lg hover:bg-input transition-colors ${agentNotifsOn ? "text-accent" : "text-dim"}`}
            >
              {agentNotifsOn ? (
                <Bell className="w-4 h-4" />
              ) : (
                <BellOff className="w-4 h-4" />
              )}
            </button>
            <button
              type="button"
              onClick={handleRefresh}
              title="Refresh"
              className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-input transition-colors"
            >
              <svg className={`w-4 h-4 text-label ${refreshing ? "animate-spin-reverse" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </button>
          </div>
        ) : undefined}
      >
        {!selecting ? (
          <FilterTabs
            tabs={FILTER_TABS}
            active={filter}
            onChange={setFilter}
            counts={filterCounts}
          />
        ) : (
          <div className="grid grid-cols-3 items-center px-4 pb-2">
            <button
              type="button"
              onClick={allSelected ? deselectAll : selectAll}
              className="justify-self-start text-sm font-medium text-accent hover:opacity-80 transition-colors px-2 py-1"
            >
              {allSelected ? "Deselect All" : "Select All"}
            </button>
            <span className="justify-self-center text-sm text-label">
              {selected.size > 0 ? `${selected.size} selected` : "Select items"}
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
      </PageHeader>

      <div className="flex-1 overflow-y-auto overflow-x-hidden">
      <div className="max-w-2xl mx-auto w-full">
      {/* Search bar */}
      <div className="px-4 pt-3 pb-1">
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
            placeholder="Search agents & messages..."
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
      </div>

      {/* Message search results */}
      {search.trim().length >= 2 && (
        <div className="px-4 py-2">
          {searchLoading && (
            <p className="text-xs text-dim animate-pulse">Searching messages...</p>
          )}
          {searchResults && searchResults.results && searchResults.results.length > 0 && (
            <div className="space-y-1 mb-2">
              <p className="text-xs text-dim font-medium">
                {searchResults.total} message{searchResults.total !== 1 ? "s" : ""} found
              </p>
              {/* Group results by agent */}
              {Object.entries(
                searchResults.results.reduce((acc, r) => {
                  const key = r.agent_id;
                  if (!acc[key]) acc[key] = { agent_name: r.agent_name, project: r.project, items: [] };
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
                    <span className="text-[10px] text-dim ml-2">{group.project}</span>
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
          {searchResults && searchResults.results && searchResults.results.length === 0 && !searchLoading && (
            <p className="text-xs text-dim">No messages match "{search.trim()}"</p>
          )}
        </div>
      )}

      {/* Unlinked sessions banner */}
      {unlinked.length > 0 && !selecting && (() => {
        const adoptable = unlinked.filter((s) => !s.rejected);
        const rejected = unlinked.filter((s) => s.rejected);
        const total = unlinked.length;
        return (
          <div className="mx-4 mt-2 rounded-xl bg-surface border border-edge overflow-hidden">
            <button
              type="button"
              onClick={() => setUnlinkedOpen((v) => !v)}
              className="w-full flex items-center gap-2 px-4 py-2.5 text-left hover:bg-hover transition-colors"
            >
              <Link2 className="w-4 h-4 text-dim shrink-0" />
              <span className="text-sm font-medium text-heading flex-1">
                {total} session{total !== 1 ? "s" : ""} detected
              </span>
              {unlinkedOpen
                ? <ChevronUp className="w-4 h-4 text-faint" />
                : <ChevronDown className="w-4 h-4 text-faint" />
              }
            </button>
            {unlinkedOpen && (
              <div className="border-t border-divider divide-y divide-divider">
                {adoptable.map((s) => {
                  const fk = (s.file || "").replace(/\.json$/, "") || s.session_id;
                  return (
                    <div key={fk} className="px-4 py-2.5 flex items-center gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-heading truncate">
                            {s.tmux_session || s.project_name || "unknown"}
                          </span>
                          {s.project_name && (
                            <span className="text-[10px] font-medium px-1.5 py-px rounded-full accent-tint-15 text-accent truncate">
                              {s.project_name}
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-dim truncate mt-0.5">
                          {s.session_id ? `${s.session_id.slice(0, 12)}… · ` : ""}pane {s.tmux_pane || "?"}
                        </p>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleAdopt(s)}
                        disabled={adoptingId === fk}
                        className="shrink-0 px-3 py-1 rounded-full text-xs font-medium accent-tint-15 text-accent hover:accent-tint-25 transition-colors disabled:opacity-50"
                      >
                        {adoptingId === fk ? "Linking…" : "Adopt"}
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDrop(s)}
                        className="shrink-0 p-1 rounded-full text-faint hover:text-dim hover:bg-hover transition-colors"
                        title="Dismiss — won't re-appear if detected again"
                      >
                        <X className="w-4 h-4" />
                      </button>
                    </div>
                  );
                })}
                {rejected.map((s) => {
                  const fk = (s.file || "").replace(/\.json$/, "") || s.session_id;
                  const reasonText = s.reason === "missing_tmux_pane"
                    ? "Running outside tmux — convert to adopt"
                    : (s.reason || "Cannot adopt");
                  return (
                    <div key={fk} className="px-4 py-2.5 flex items-center gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-heading truncate">
                            {s.project_name || "unknown"}
                          </span>
                          <span className="text-[10px] font-medium px-1.5 py-px rounded-full bg-amber-500/15 text-amber-500 dark:text-amber-400 shrink-0">
                            not in tmux
                          </span>
                        </div>
                        <p className="text-xs text-dim truncate mt-0.5">
                          {s.session_id ? `${s.session_id.slice(0, 12)}… · ` : ""}{reasonText}
                        </p>
                      </div>
                      {s.reason === "missing_tmux_pane" && (
                        <button
                          type="button"
                          onClick={() => handleConvertAndAdopt(s)}
                          disabled={adoptingId === fk}
                          className="shrink-0 px-3 py-1 rounded-full text-xs font-medium bg-amber-500/15 text-amber-500 dark:text-amber-400 hover:bg-amber-500/25 transition-colors disabled:opacity-50"
                          title="SIGTERM your terminal claude and resume inside tmux"
                        >
                          {adoptingId === fk ? "Moving…" : "Move to tmux"}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => handleDrop(s)}
                        className="shrink-0 p-1 rounded-full text-faint hover:text-dim hover:bg-hover transition-colors"
                        title="Dismiss — won't re-appear if detected again"
                      >
                        <X className="w-4 h-4" />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })()}

      {/* Agent list */}
      <div className={`${selecting ? "pb-32" : "pb-24"} px-4 py-2 space-y-3`}>
        {error && (
          <div className="bg-red-950/40 border border-red-800 rounded-xl p-4">
            <p className="text-red-400 text-sm">Failed to fetch agents: {error}</p>
            <button type="button" onClick={load} className="mt-2 text-xs text-red-300 underline hover:text-red-200">
              Retry
            </button>
          </div>
        )}

        {!loading && !error && filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 text-faint">
            <svg className="w-12 h-12 mb-3" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
            </svg>
            <p className="text-sm">No agents yet</p>
            <p className="text-xs mt-1 text-ghost">Create one from the New tab</p>
          </div>
        )}

        <div className="space-y-3">
          {[...starredAgents, ...regularAgents].map((agent) => (
            <AgentRow
              key={agent.id}
              agent={agent}
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

        {deferredAgents.length > 0 && (
          <div className="mt-6">
            <button
              type="button"
              onClick={() => setShowDeferred(v => !v)}
              className="flex items-center justify-center w-full text-sm text-faint hover:text-dim transition-colors"
            >
              <span className="relative">
                <svg
                  className={`absolute right-full top-1/2 -translate-y-1/2 mr-1.5 w-3.5 h-3.5 transition-transform ${showDeferred ? "rotate-90" : ""}`}
                  fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
                Deferred ({deferredAgents.length})
              </span>
            </button>
            {showDeferred && (
              <div className="space-y-3 mt-3">
                {deferredAgents.map((agent) => (
                  <AgentRow
                    key={agent.id}
                    agent={agent}
                    onClick={() => navigate(`/agents/${agent.id}`, { state: forwardState(location) })}
                    selecting={selecting}
                    selected={selected.has(agent.id)}
                    onToggle={toggleOne}
                    onEnterSelect={enterSelectMode}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        <div className="h-4" />
      </div>
      </div>
      </div>

      {/* Bottom toolbar in selection mode */}
      {selecting && selected.size > 0 && (
        <div className="fixed bottom-20 left-0 right-0 z-20 px-4 pb-2">
          <div className="max-w-xl mx-auto bg-surface border border-divider rounded-xl shadow-lg p-3 flex items-center justify-center gap-3">
            <button
              type="button"
              onClick={handleBulkMarkRead}
              disabled={bulkMarking || unreadSelected.length === 0}
              className="flex-1 flex items-center justify-center gap-2 min-h-[40px] rounded-lg bg-accent hover:opacity-90 text-accent-ink text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 18h10a2 2 0 002-2V8H3v8a2 2 0 002 2zM17 8h2a2 2 0 010 4h-2M8 2v3M12 2v3" />
              </svg>
              {bulkMarking
                ? "Marking..."
                : unreadSelected.length === 0
                  ? "Read"
                  : `Read ${unreadSelected.length}`
              }
            </button>
            <button
              type="button"
              onClick={handleBulkStop}
              disabled={bulkStopping || stoppableSelected.length === 0}
              className="flex-1 flex items-center justify-center gap-2 min-h-[40px] rounded-lg bg-red-600 hover:bg-red-500 text-white text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
              {bulkStopping
                ? "Stopping..."
                : stoppableSelected.length === 0
                  ? "Stop"
                  : `Stop ${stoppableSelected.length}`
              }
            </button>
            <button
              type="button"
              onClick={handleBulkDelete}
              disabled={bulkDeleting || deletableSelected.length === 0}
              className="flex-1 flex items-center justify-center gap-2 min-h-[40px] rounded-lg bg-red-600 hover:bg-red-500 text-white text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
              {bulkDeleting
                ? "Deleting..."
                : deletableSelected.length === 0
                  ? "Delete"
                  : `Delete ${deletableSelected.length}`
              }
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
