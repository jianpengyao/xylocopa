import { createContext, useContext, useEffect, useMemo, useRef, useState, useCallback } from "react";
import { fetchUnreadList } from "../lib/api";
import { useWebSocketContext } from "./WebSocketContext";

const UnreadContext = createContext(null);

/**
 * Single source of truth for per-agent unread counts across the app.
 *
 * Consumers (BottomNav "Agents" badge, AttentionButton FAB) read from
 * this provider so they update in the same React render when any
 * agent's unread_count changes via WebSocket — no more 5s HTTP polls,
 * no more divergence between the FAB total and the nav dot.
 *
 * AgentsPage / ProjectDetailPage / AgentChatPage keep their own local
 * agent lists and consume the same WS stream directly; that's fine —
 * this provider is an additional parallel consumer, not a replacement.
 */
export function UnreadProvider({ children }) {
  const { subscribe, onOpen } = useWebSocketContext();
  // unreadMap: { [agent_id]: { id, unread_count, last_message_preview, last_message_at } }
  const [unreadMap, setUnreadMap] = useState({});

  // HTTP resync: pull the authoritative list and replace local state.
  // Used on mount and on WS (re)connect to catch anything we missed.
  // No "token stored" gate: this provider renders only inside AuthGuard,
  // and under DISABLE_AUTH no token is ever stored.
  const resync = useCallback(async (reason) => {
    try {
      const r = await fetchUnreadList();
      const agents = r.agents || [];
      const next = {};
      for (const a of agents) {
        if (!a?.id || !(a.unread_count > 0)) continue;
        next[a.id] = {
          id: a.id,
          unread_count: a.unread_count,
          last_message_preview: a.last_message_preview || null,
          last_message_at: a.last_message_at || null,
        };
      }
      setUnreadMap(next);
      const total = agents.reduce((s, a) => s + (a.unread_count || 0), 0);
      // eslint-disable-next-line no-console
      console.info(`[unread] ${reason}`, agents.length, "total=", total);
    } catch (err) {
      console.warn("[unread] resync failed:", err);
    }
  }, []);

  // Initial seed on mount.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    resync("seed");
  }, [resync]);

  // WS event → incremental merge. Runs synchronously for every event
  // (subscribe delivers outside React batching) so rapid updates don't
  // collapse into a single render.
  useEffect(() => {
    const unsub = subscribe((event) => {
      if (event?.type !== "agent_update") return;
      const d = event.data || {};
      const { agent_id, unread_count, last_message_preview, last_message_at } = d;
      if (!agent_id) return;
      // Insight-only updates don't carry unread_count — ignore.
      if (unread_count === undefined || unread_count === null) return;

      setUnreadMap((prev) => {
        const next = { ...prev };
        if (unread_count > 0) {
          next[agent_id] = {
            id: agent_id,
            unread_count,
            last_message_preview: last_message_preview ?? prev[agent_id]?.last_message_preview ?? null,
            last_message_at: last_message_at ?? prev[agent_id]?.last_message_at ?? null,
          };
        } else {
          delete next[agent_id];
        }
        const newTotal = Object.values(next).reduce((s, a) => s + (a.unread_count || 0), 0);
        // eslint-disable-next-line no-console
        console.info("[unread] ws-merge", String(agent_id).slice(0, 8), unread_count, "total=", newTotal);
        return next;
      });
    });
    return unsub;
  }, [subscribe]);

  // WS (re)connect → HTTP resync to close any gap from missed events.
  useEffect(() => {
    if (typeof onOpen !== "function") return;
    const unsub = onOpen(() => {
      // Skip the very first open — the initial mount seed already covers it.
      if (!seededRef.current) return;
      resync("resync");
    });
    return unsub;
  }, [onOpen, resync]);

  // Explicit invalidation from other parts of the app.
  useEffect(() => {
    const onDataChanged = () => resync("agents-data-changed");
    window.addEventListener("agents-data-changed", onDataChanged);
    return () => window.removeEventListener("agents-data-changed", onDataChanged);
  }, [resync]);

  // Safety net for silently-dead sockets: resync once when the tab
  // regains visibility (the classic stale case — laptop sleep, long
  // background). Event-driven, no polling; a no-op when WS is healthy.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") resync("visible");
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [resync]);

  // Authoritative feed from a full agents list — the SAME payload the
  // agent cards render. Any page that polls the list pushes it here, so
  // the badge total is the sum of the numbers on screen rather than a
  // parallel WS-only view that can diverge from the cards.
  const syncFromAgents = useCallback((agents) => {
    if (!Array.isArray(agents)) return;
    setUnreadMap(() => {
      const next = {};
      for (const a of agents) {
        if (!a?.id || a.is_subagent || !(a.unread_count > 0)) continue;
        next[a.id] = {
          id: a.id,
          unread_count: a.unread_count,
          last_message_preview: a.last_message_preview || null,
          last_message_at: a.last_message_at || null,
        };
      }
      return next;
    });
  }, []);

  const value = useMemo(() => {
    const entries = Object.values(unreadMap).filter((a) => a.unread_count > 0);
    const total = entries.reduce((s, a) => s + (a.unread_count || 0), 0);
    // Oldest first — matches /api/agents/unread-list semantics.
    const list = entries.slice().sort((a, b) => {
      const ta = a.last_message_at ? Date.parse(a.last_message_at) : 0;
      const tb = b.last_message_at ? Date.parse(b.last_message_at) : 0;
      return ta - tb;
    });
    return { unreadMap, list, total, syncFromAgents };
  }, [unreadMap, syncFromAgents]);

  return <UnreadContext.Provider value={value}>{children}</UnreadContext.Provider>;
}

// Fallback so consumers rendered outside the provider (e.g. /login) don't crash.
const _fallback = { unreadMap: {}, list: [], total: 0, syncFromAgents: () => {} };

export function useUnread() {
  const ctx = useContext(UnreadContext);
  if (!ctx) return _fallback;
  return ctx;
}
