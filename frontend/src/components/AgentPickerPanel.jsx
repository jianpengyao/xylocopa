import { useState, useEffect, useCallback } from "react";
import { fetchAgents } from "../lib/api";
import { AGENT_STATUS_COLORS, AGENT_STATUS_TEXT_COLORS, agentBotState } from "../lib/constants";
import { relativeTime } from "../lib/formatters";
import BotIcon from "./BotIcon";

export default function AgentPickerPanel({ onSelect }) {
  const [agents, setAgents] = useState([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    fetchAgents("limit=200")
      .then((data) => {
        setAgents(data.agents || data || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const q = search.toLowerCase();
  const filtered = agents.filter(
    (a) =>
      !q ||
      a.name?.toLowerCase().includes(q) ||
      a.project?.toLowerCase().includes(q) ||
      a.id?.toLowerCase().includes(q)
  );

  return (
    <div className="flex flex-col h-full bg-page">
      {/* Header */}
      <div className="shrink-0 px-4 py-3 border-b border-divider bg-surface">
        <h3 className="text-sm font-semibold text-heading mb-2">Select Agent</h3>
        <input
          type="search"
          autoComplete="off"
          placeholder="Search agents..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full px-3 py-2 text-sm rounded-lg bg-input border border-edge text-body placeholder:text-faint outline-none focus:border-accent transition-colors"
        />
      </div>

      {/* Agent list */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {loading ? (
          <div className="text-center py-10 text-sm text-dim animate-pulse">Loading agents...</div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-10 text-sm text-dim">
            {search ? "No matching agents" : "No agents found"}
          </div>
        ) : (
          filtered.map((agent) => {
            const statusDot = AGENT_STATUS_COLORS[agent.status] || "bg-gray-500";
            const statusTextColor = AGENT_STATUS_TEXT_COLORS[agent.status] || "text-dim";
            const state = agentBotState(agent.status);

            return (
              <button
                key={agent.id}
                type="button"
                onClick={() => onSelect(agent.id)}
                className="w-full text-left rounded-xl bg-surface shadow-card p-3 flex items-center gap-3 transition-colors active:bg-input hover:ring-1 hover:ring-ring-hover"
              >
                <BotIcon state={state} size={36} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-heading truncate">
                      {agent.name}
                    </span>
                    {agent.unread_count > 0 && (
                      <span className="inline-flex items-center justify-center min-w-[16px] h-[16px] px-1 rounded-full bg-red-500 text-white text-[10px] font-bold leading-none">
                        {agent.unread_count > 99 ? "99+" : agent.unread_count}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 mt-0.5">
                    <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${statusDot}`} />
                    <span className={`text-xs ${statusTextColor}`}>
                      {agent.status?.toLowerCase().replace("_", " ")}
                    </span>
                    <span className="text-xs text-faint truncate">
                      {agent.project}
                    </span>
                  </div>
                  {agent.last_message_at && (
                    <div className="text-[10px] text-faint mt-0.5">
                      {relativeTime(agent.last_message_at)}
                    </div>
                  )}
                </div>
                <svg className="w-4 h-4 text-dim shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
