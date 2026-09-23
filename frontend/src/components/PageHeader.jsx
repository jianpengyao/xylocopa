import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import useHealthStatus from "../hooks/useHealthStatus";
import { useMonitor } from "../contexts/MonitorContext";
import { restartServer, fetchHealth, fetchQueueStatus, updateProjectSettings, fetchViewingStatsWeek } from "../lib/api";
import { isSystemHealthy } from "../lib/constants";
import { relativeTime, durationDisplay } from "../lib/formatters";
import { forwardState } from "../lib/nav";
import PopoverArrow from "./PopoverArrow";

// Project bars/dots in the viewing-time popover use the theme accent token
// with decreasing opacity by rank, so the top project reads as primary and
// the tail fades out. Avoids rainbow palettes that clash with the rest of
// the UI, which never differentiates projects by hue.
const RANK_OPACITY = [1.0, 0.75, 0.55, 0.4, 0.3, 0.22, 0.18, 0.15];
function rankOpacity(i) {
  return RANK_OPACITY[Math.min(i, RANK_OPACITY.length - 1)];
}
function formatDuration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}
function formatHoursShort(seconds) {
  const s = Math.max(0, seconds || 0);
  if (s < 60) return "0h";
  const h = s / 3600;
  if (h < 1) return `${Math.round(s / 60)}m`;
  if (h < 10) return `${h.toFixed(1)}h`;
  return `${Math.round(h)}h`;
}

/* ── Status icon + row for queue items ── */
function QueueItem({ status, label, sub, dim, onClick }) {
  const s = status || "";
  const isIdle = s === "IDLE";
  const bgClass = isIdle ? "ok-tint-15 text-ok"
    : "accent-tint-15 text-accent";
  return (
    <div className="flex items-center gap-2 text-xs cursor-pointer hover:bg-surface-hover rounded px-1 -mx-1 py-0.5" onClick={onClick}>
      <span className={`w-4 h-4 rounded-full flex items-center justify-center shrink-0 ${bgClass}`}>
        {isIdle ? (
          /* static pause icon */
          <svg className="w-2.5 h-2.5" viewBox="0 0 24 24" fill="currentColor">
            <rect x="6" y="5" width="4" height="14" rx="1" />
            <rect x="14" y="5" width="4" height="14" rx="1" />
          </svg>
        ) : (
          <svg className="w-2.5 h-2.5 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
        )}
      </span>
      <span className={`text-body truncate flex-1 min-w-0${dim ? " opacity-70" : ""}`}>{label}</span>
      <span className="text-faint text-[10px] shrink-0">{sub}</span>
    </div>
  );
}

/* ── Queue Capacity Popover ── */
function QueuePopover({ onClose, containerRef, navigate }) {
  const location = useLocation();
  const [data, setData] = useState(null);
  const [showDone, setShowDone] = useState(false);

  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose, containerRef]);

  const load = useCallback(async () => {
    try {
      const res = await fetchQueueStatus();
      setData(res);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 4000);
    return () => clearInterval(interval);
  }, [load]);

  const adjustCap = async (project, delta) => {
    if (!data) return;
    const cur = data.capacity[project]?.max_concurrent ?? 2;
    const next = Math.max(1, cur + delta);
    if (next === cur) return;
    try {
      await updateProjectSettings(project, { max_concurrent: next });
      load();
    } catch { /* skip */ }
  };

  const agents = data?.agents ?? [];
  const pending = data?.pending ?? [];
  const capacity = data?.capacity ?? {};
  const todayDone = data?.today_done ?? [];
  const todayCompleted = todayDone.filter(t => t.status === "COMPLETE");
  // Sort: EXECUTING first, then STARTING, IDLE last
  const statusOrder = { EXECUTING: 0, STARTING: 1, IDLE: 2 };
  const sortedAgents = [...agents].sort((a, b) =>
    (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9)
  );
  const totalRunning = agents.length;
  const activeProjects = Object.entries(capacity)
    .filter(([name, c]) => (c.alive ?? c.active) > 0 || pending.some(t => t.project_name === name))
    .sort(([, a], [, b]) => (b.alive ?? b.active) - (a.alive ?? a.active));

  return (
    <div className="absolute right-0 top-full mt-2 z-50" style={{ width: 260 }}>
      <PopoverArrow size={12} align="right" offset={12} />
      <div className="bg-surface border border-edge rounded-xl shadow-lg overflow-hidden" style={{ boxShadow: "0 8px 30px var(--color-shadow)" }}>

        {/* Summary header */}
        <div className="px-4 pt-3.5 pb-3 flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg accent-tint-15 flex items-center justify-center">
            <svg className="w-4.5 h-4.5 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-heading text-sm font-semibold">Agent Queue</div>
            <div className="text-dim text-xs mt-0.5 flex items-center gap-2">
              {totalRunning > 0 && <span className="text-accent">{totalRunning} running</span>}
              {totalRunning > 0 && pending.length > 0 && <span>·</span>}
              {pending.length > 0 && <span className="text-amber-500">{pending.length} waiting</span>}
              {totalRunning === 0 && pending.length === 0 && <span>idle</span>}
            </div>
          </div>
          {todayCompleted.length > 0 && (
            <button
              type="button"
              onClick={() => setShowDone(v => !v)}
              className="shrink-0 flex items-center gap-1 px-2 py-1 rounded-full ok-tint-10 text-ok hover:ok-tint-25 transition-colors"
            >
              <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
              </svg>
              <span className="text-[11px] font-semibold tabular-nums">{todayCompleted.length}</span>
            </button>
          )}
        </div>

        {/* Per-project slot grid */}
        {activeProjects.length > 0 && (
          <div className="border-t border-divider px-4 py-3 space-y-3">
            {activeProjects.map(([name, cap]) => {
              const max = cap.max_concurrent;
              const active = cap.active;         // dispatcher-counted (STARTING/EXECUTING/IDLE)
              const alive = cap.alive ?? active;  // all non-stopped (includes IDLE)
              const idleCount = cap.idle ?? 0; // IDLE subset of active
              const executing = active - idleCount; // STARTING/EXECUTING
              const idle = alive - active;        // IDLE agents
              const projPending = pending.filter(t => t.project_name === name).length;
              return (
                <div key={name}>
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-xs font-medium text-body truncate max-w-[140px]">{name}</span>
                    <div className="flex items-center gap-1">
                      <button type="button" onClick={() => adjustCap(name, -1)}
                        className="w-5 h-5 rounded flex items-center justify-center text-faint hover:text-heading hover:bg-input transition-colors text-xs font-bold">
                        -
                      </button>
                      <span className="text-[11px] tabular-nums text-dim font-medium w-7 text-center">{alive}/{max}</span>
                      <button type="button" onClick={() => adjustCap(name, 1)}
                        className="w-5 h-5 rounded flex items-center justify-center text-faint hover:text-heading hover:bg-input transition-colors text-xs font-bold">
                        +
                      </button>
                    </div>
                  </div>
                  {/* Dot grid: each dot = 1 agent slot */}
                  <div className="flex flex-wrap gap-[5px]">
                    {/* Executing/Starting agents — cyan */}
                    {Array.from({ length: executing }, (_, i) => (
                      <div key={`a${i}`} className={`w-[10px] h-[10px] rounded-sm transition-all ${
                        i >= max
                          ? "bg-amber-500 shadow-[0_0_4px_rgba(245,158,11,0.5)]"
                          : "bg-accent shadow-[0_0_4px_rgba(6,182,212,0.4)]"
                      }`} />
                    ))}
                    {/* Idle agents — green */}
                    {Array.from({ length: idleCount }, (_, i) => (
                      <div key={`s${i}`} className={`w-[10px] h-[10px] rounded-sm transition-all ${
                        (executing + i) >= max
                          ? "bg-amber-500 shadow-[0_0_4px_rgba(245,158,11,0.5)]"
                          : "bg-emerald-500 shadow-[0_0_4px_rgba(16,185,129,0.4)]"
                      }`} />
                    ))}
                    {/* Idle agents — dimmer */}
                    {Array.from({ length: idle }, (_, i) => (
                      <div key={`i${i}`} className="w-[10px] h-[10px] rounded-sm accent-tint-20 border accent-edge-40" />
                    ))}
                    {/* Empty slots */}
                    {Array.from({ length: Math.max(0, max - alive) }, (_, i) => (
                      <div key={`e${i}`} className="w-[10px] h-[10px] rounded-sm bg-elevated border border-edge/40" />
                    ))}
                    {/* Pending task indicators */}
                    {Array.from({ length: projPending }, (_, i) => (
                      <div key={`p${i}`} className="w-[10px] h-[10px] rounded-sm border border-amber-500/60 bg-amber-500/15 animate-pulse" />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Agent + pending task list */}
        {(pending.length > 0 || sortedAgents.length > 0) && (
          <div className="border-t border-divider px-4 py-2.5 space-y-1.5 max-h-[180px] overflow-y-auto">
            {pending.map((t, i) => (
              <div key={t.id} className="flex items-center gap-2 text-xs cursor-pointer hover:bg-surface-hover rounded px-1 -mx-1 py-0.5"
                onClick={() => { onClose(); navigate(`/tasks/${t.id}`); }}>
                <span className="w-4 h-4 rounded-full bg-amber-500/15 text-amber-500 flex items-center justify-center text-[9px] font-bold shrink-0">
                  {i + 1}
                </span>
                <span className="text-body truncate flex-1 min-w-0">{t.title}</span>
                <span className="text-faint text-[10px] shrink-0">{(t.project_name || "").slice(0, 8)}</span>
              </div>
            ))}
            {sortedAgents.map(a => (
              <QueueItem key={a.agent_id} status={a.status}
                label={a.task_title || a.name}
                sub={(a.project || "").slice(0, 8)}
                dim={!a.task_id}
                onClick={() => { onClose(); navigate(`/agents/${a.agent_id}`, { state: forwardState(location) }); }} />
            ))}
          </div>
        )}

        {/* Today's completed tasks */}
        {showDone && todayCompleted.length > 0 && (
          <div className="border-t border-divider px-4 py-2.5 space-y-1.5 max-h-[200px] overflow-y-auto">
            <div className="text-faint text-[10px] uppercase tracking-wider font-medium mb-1">
              Completed today · {todayCompleted.length}
            </div>
            {todayCompleted.map(t => (
              <div key={t.id} className="flex items-center gap-2 text-xs cursor-pointer hover:bg-surface-hover rounded px-1 -mx-1 py-0.5"
                onClick={() => { onClose(); navigate(`/tasks/${t.id}`); }}>
                <span className="w-4 h-4 rounded-full ok-tint-15 text-ok flex items-center justify-center shrink-0">
                  <svg className="w-2.5 h-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                  </svg>
                </span>
                <span className="text-body truncate flex-1 min-w-0">{t.title}</span>
                <span className="text-faint text-[10px] shrink-0 whitespace-nowrap">
                  {t.started_at && t.completed_at ? durationDisplay(t.started_at, t.completed_at) : relativeTime(t.completed_at)}
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Empty state */}
        {!data && (
          <div className="px-4 py-4 text-center text-dim text-xs animate-pulse">Loading...</div>
        )}
        {data && activeProjects.length === 0 && tasks.length === 0 && (
          <div className="border-t border-divider px-4 py-4 text-center text-faint text-xs">No projects configured</div>
        )}
      </div>
    </div>
  );
}

/* ── Task Stats Popover ── */
function TaskStatsPopover({ taskStats, onClose, containerRef }) {
  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose, containerRef]);

  const wTotal = taskStats?.weekly_total ?? 0;
  const wCompleted = taskStats?.weekly_completed ?? 0;
  const wFailed = taskStats?.weekly_failed ?? 0;
  const wTimeout = taskStats?.weekly_timeout ?? 0;
  const wCancelled = taskStats?.weekly_cancelled ?? 0;
  const wRejected = taskStats?.weekly_rejected ?? 0;
  const wRetries = taskStats?.weekly_retries ?? 0;
  const wFirstAttempt = taskStats?.weekly_first_attempt ?? 0;
  const wFirstAttemptPct = taskStats?.weekly_first_attempt_pct ?? 0;
  const wPct = taskStats?.weekly_success_pct ?? 0;

  const ringColor = wTotal === 0 ? "#9ca3af" : wPct >= 80 ? "#22c55e" : wPct >= 50 ? "#eab308" : "#f87171";

  const rows = [
    { label: "Completed", count: wCompleted, color: "#22c55e" },
    { label: "Retries",   count: wRetries,   color: "#fb923c" },
    { label: "Failed",    count: wFailed,    color: "#f87171" },
    { label: "Timeout",   count: wTimeout,   color: "#f59e0b" },
    { label: "Dropped",   count: wCancelled, color: "#9ca3af" },
    { label: "Rejected",  count: wRejected,  color: "#a78bfa" },
  ].filter(r => r.count > 0);

  return (
    <div className="absolute right-0 top-full mt-2 z-50" style={{ minWidth: 260 }}>
      {/* Arrow */}
      <PopoverArrow size={12} align="right" offset={12} />
      {/* Card */}
      <div className="bg-surface border border-edge rounded-xl shadow-lg overflow-hidden" style={{ boxShadow: "0 8px 30px var(--color-shadow)" }}>
        {/* Header — big ring with bare number (no percent sign) */}
        <div className="px-4 pt-4 pb-3 flex items-center gap-3">
          <svg width="44" height="44" viewBox="0 0 44 44">
            <circle cx="22" cy="22" r="17" fill="transparent" stroke={ringColor} strokeWidth="3.5" opacity={0.18} />
            <circle cx="22" cy="22" r="17" fill="transparent" stroke={ringColor} strokeWidth="3.5"
              strokeLinecap="round"
              strokeDasharray={2 * Math.PI * 17}
              strokeDashoffset={2 * Math.PI * 17 * (1 - wPct / 100)}
              transform="rotate(-90 22 22)"
              style={{ transition: "stroke-dashoffset 0.6s ease" }} />
            <text x="22" y="22" textAnchor="middle" dominantBaseline="central"
              fill={ringColor} style={{ fontSize: "12px", fontWeight: 700 }}>
              {wPct}
            </text>
          </svg>
          <div>
            <div className="text-heading text-sm font-semibold">Weekly Success Rate</div>
            <div className="text-dim text-xs mt-0.5">{wTotal} tasks{wRetries > 0 ? ` · ${wRetries} retries` : ""}</div>
          </div>
        </div>

        {/* Divider */}
        <div className="border-t border-divider" />

        {/* Breakdown rows */}
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

        {/* First-attempt success rate */}
        {wCompleted > 0 && (
          <div className="border-t border-divider px-4 py-2.5 flex items-center justify-between">
            <div className="text-xs text-dim">First-attempt success</div>
            <div className="text-xs font-medium tabular-nums" style={{ color: wFirstAttemptPct >= 80 ? "#22c55e" : wFirstAttemptPct >= 50 ? "#eab308" : "#f87171" }}>
              {wFirstAttempt}/{wCompleted} ({wFirstAttemptPct}%)
            </div>
          </div>
        )}

        {/* Daily success rate sparkline */}
        {(() => {
          const daily = taskStats?.daily;
          if (!daily || daily.length === 0) return null;
          // Only show if there's at least one day with data
          const hasData = daily.some(d => d.total > 0);
          if (!hasData) return null;

          const W = 228, H = 72, PX = 8, PY = 14;
          const plotW = W - PX * 2, plotH = H - PY * 2;
          // Points with data
          const points = daily.map((d, i) => ({
            x: PX + (i / Math.max(daily.length - 1, 1)) * plotW,
            pct: d.success_pct,
            total: d.total,
            date: d.date,
          }));
          // Build line path (skip null points)
          const validPts = points.filter(p => p.pct != null);
          const yOf = (pct) => PY + plotH - (pct / 100) * plotH;
          const linePath = validPts.map((p, i) =>
            `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${yOf(p.pct).toFixed(1)}`
          ).join(" ");
          // Gradient fill path
          const fillPath = validPts.length >= 2
            ? `${linePath} L${validPts[validPts.length - 1].x.toFixed(1)},${H - PY} L${validPts[0].x.toFixed(1)},${H - PY} Z`
            : "";
          // Day labels (weekday abbreviation)
          const dayLabels = daily.map(d => {
            const dt = new Date(d.date + "T00:00:00");
            return ["S","M","T","W","T","F","S"][dt.getDay()];
          });

          return (
            <div className="border-t border-divider px-4 py-2.5">
              <div className="text-faint text-[10px] uppercase tracking-wider font-medium mb-1.5">Daily Success Rate</div>
              <svg width={W} height={H + 14} viewBox={`0 0 ${W} ${H + 14}`} className="w-full" style={{ maxWidth: W }}>
                <defs>
                  <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#22c55e" stopOpacity="0.25" />
                    <stop offset="100%" stopColor="#22c55e" stopOpacity="0.02" />
                  </linearGradient>
                </defs>
                {/* Grid lines at 0%, 50%, 100% */}
                {[0, 50, 100].map(pct => (
                  <line key={pct} x1={PX} x2={W - PX} y1={yOf(pct)} y2={yOf(pct)}
                    stroke="var(--color-edge)" strokeWidth="0.5" strokeDasharray={pct === 50 ? "2,2" : "none"} opacity={0.5} />
                ))}
                {/* Fill area */}
                {fillPath && <path d={fillPath} fill="url(#sparkFill)" />}
                {/* Line */}
                {validPts.length >= 2 && <path d={linePath} fill="none" stroke="#22c55e" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />}
                {/* Dots */}
                {validPts.map((p, i) => (
                  <circle key={i} cx={p.x} cy={yOf(p.pct)} r="2.5" fill="#22c55e" stroke="var(--color-surface)" strokeWidth="1" />
                ))}
                {/* Percentage labels on dots */}
                {validPts.map((p, i) => (
                  <text key={`lbl${i}`} x={p.x} y={yOf(p.pct) - 5} textAnchor="middle" fill="var(--color-heading)"
                    style={{ fontSize: "9px", fontWeight: 600 }}>{p.pct}%</text>
                ))}
                {/* Day labels */}
                {points.map((p, i) => (
                  <text key={`day${i}`} x={p.x} y={H + 10} textAnchor="middle" fill="var(--color-dim)"
                    style={{ fontSize: "9px" }}>{dayLabels[i]}</text>
                ))}
                {/* No-data markers */}
                {points.filter(p => p.pct == null).map((p, i) => (
                  <circle key={`empty${i}`} cx={p.x} cy={PY + plotH / 2} r="1.5" fill="var(--color-edge)" opacity="0.5" />
                ))}
              </svg>
            </div>
          );
        })()}

      </div>
    </div>
  );
}

/* ── Viewing Time Stats Popover ── */
function TimeStatsPopover({ onClose, containerRef, mode, onModeChange }) {
  const [week, setWeek] = useState(null);

  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose, containerRef]);

  useEffect(() => {
    fetchViewingStatsWeek(7).then(setWeek).catch(() => setWeek({ days: [], projects: [], total_seconds: 0 }));
  }, []);

  return (
    <div className="absolute right-0 top-full mt-2 z-50" style={{ minWidth: 260 }}>
      <PopoverArrow size={12} align="right" offset={12} />
      <div className="bg-surface border border-edge rounded-xl shadow-lg overflow-hidden" style={{ boxShadow: "0 8px 30px var(--color-shadow)" }}>
        <WeekView week={week} mode={mode} onModeChange={onModeChange} />
      </div>
    </div>
  );
}

function ModeToggle({ mode, onModeChange }) {
  const btn = (key, label) => (
    <button
      type="button"
      onClick={() => onModeChange(key)}
      className={`px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider transition-colors ${
        mode === key
          ? "accent-tint-15 text-accent"
          : "bg-transparent text-dim hover:bg-input"
      }`}
    >
      {label}
    </button>
  );
  return (
    <div className="inline-flex rounded-md border border-edge overflow-hidden">
      {btn("day", "Day")}
      {btn("week", "Week")}
    </div>
  );
}

function WeekView({ week, mode, onModeChange }) {
  if (!week) return <div className="px-4 py-6 text-center text-dim text-xs animate-pulse">Loading...</div>;
  const days = week.days || [];
  const weekProjects = week.projects || [];
  const weekTotal = week.total_seconds || 0;
  const maxDaySecs = Math.max(1, ...days.map((d) => d.seconds || 0));

  const todayBucket = days.length ? days[days.length - 1] : null;
  const todayTotal = todayBucket?.seconds || 0;
  const todayProjects = todayBucket
    ? Object.entries(todayBucket.by_project || {})
        .map(([project, seconds]) => ({ project, seconds }))
        .sort((a, b) => b.seconds - a.seconds)
    : [];

  const isDay = mode === "day";
  const projects = isDay ? todayProjects : weekProjects;
  const total = isDay ? todayTotal : weekTotal;
  const title = isDay ? "Today" : "This Week";

  const accent = "#06b6d4";

  // Bar chart dims (match SR sparkline: 228 wide).
  // BPT reserves room above the tallest bar for its duration label so the
  // label never clips against the popover's rounded-card top edge.
  const BW = 228, BH = 72, BPX = 4, BPT = 14, BPB = 6, LH = 14;
  const nBars = days.length || 1;
  const gap = 4;
  const barW = Math.max(6, (BW - BPX * 2 - gap * (nBars - 1)) / nBars);

  return (
    <>
      {/* Header — title + day/week toggle */}
      <div className="px-4 pt-4 pb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-heading text-sm font-semibold">{title}</div>
          <div className="text-dim text-xs mt-0.5">{formatDuration(total)} · {projects.length} project{projects.length !== 1 ? "s" : ""}</div>
        </div>
        <ModeToggle mode={mode} onModeChange={onModeChange} />
      </div>

      {/* Daily bar chart — each bar shows its duration above in dim text */}
      <div className="border-t border-divider px-4 py-2.5">
        <div className="text-faint text-[10px] uppercase tracking-wider font-medium mb-1.5">Daily</div>
        <svg width={BW} height={BH + LH} viewBox={`0 0 ${BW} ${BH + LH}`} className="w-full"
          style={{ maxWidth: BW }}>
          {days.map((d, i) => {
            const x = BPX + i * (barW + gap);
            const secs = d.seconds || 0;
            const h = secs > 0 ? Math.max(2, ((secs / maxDaySecs) * (BH - BPT - BPB))) : 0;
            const y = BH - BPB - h;
            const dt = new Date(d.date + "T00:00:00");
            const weekday = ["S","M","T","W","T","F","S"][dt.getDay()];
            const isToday = i === days.length - 1;
            const barOpacity = isDay ? (isToday ? 1 : 0.3) : 0.85;
            return (
              <g key={d.date}>
                {h > 0 && (
                  <rect x={x} y={y} width={barW} height={h} rx="2"
                    fill={accent} opacity={barOpacity} />
                )}
                {secs > 0 && (
                  <text x={x + barW / 2} y={y - 4} textAnchor="middle"
                    fill="var(--color-faint)"
                    style={{ fontSize: "9px", fontWeight: 500 }}>
                    {formatHoursShort(secs)}
                  </text>
                )}
                <text x={x + barW / 2} y={BH + 10} textAnchor="middle"
                  fill="var(--color-dim)" style={{ fontSize: "9px" }}>{weekday}</text>
              </g>
            );
          })}
        </svg>
      </div>

      {/* Per-project ranking */}
      {projects.length > 0 && (
        <div className="border-t border-divider px-4 py-2.5">
          <div className="text-faint text-[10px] uppercase tracking-wider font-medium mb-1.5">By Project</div>
          <ProjectList projects={projects} totalSecs={Math.max(1, projects[0].seconds)} />
        </div>
      )}
      {projects.length === 0 && (
        <div className="border-t border-divider px-4 py-4 text-center text-dim text-xs">
          {isDay ? "No viewing time today" : "No viewing time recorded yet"}
        </div>
      )}
    </>
  );
}

function ProjectList({ projects, totalSecs }) {
  const accent = "#06b6d4";
  return (
    <div className="space-y-2">
      {projects.map((p, i) => {
        const pct = (p.seconds / totalSecs) * 100;
        const op = rankOpacity(i);
        return (
          <div key={p.project}>
            <div className="flex items-center justify-between text-xs mb-1">
              <div className="flex items-center gap-2 min-w-0">
                <span className="inline-block w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: accent, opacity: op }} />
                <span className="text-body truncate">{p.project}</span>
              </div>
              <span className="text-heading font-medium tabular-nums shrink-0 ml-2">{formatDuration(p.seconds)}</span>
            </div>
            <div className="h-1 rounded-full overflow-hidden" style={{ backgroundColor: "var(--color-input)" }}>
              <div style={{ width: `${Math.max(2, pct)}%`, height: "100%", backgroundColor: accent, opacity: op, borderRadius: 999 }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

const SunIcon = (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
  </svg>
);

const MoonIcon = (
  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
    <path strokeLinecap="round" strokeLinejoin="round" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
  </svg>
);

export default function PageHeader({ title, theme, onToggleTheme, actions, selectAction, showTaskRing, showTimeRing, showQueueButton, hideMonitor, children }) {
  const navigate = useNavigate();
  const health = useHealthStatus();
  const { taskStats, sysStats } = useMonitor();
  const [restarting, setRestarting] = useState(false);
  const [showStatsPopover, setShowStatsPopover] = useState(false);
  const [showQueuePopover, setShowQueuePopover] = useState(false);
  const [showTimePopover, setShowTimePopover] = useState(false);
  const [timeWeekTotal, setTimeWeekTotal] = useState(null);
  const [timeDayTotal, setTimeDayTotal] = useState(null);
  const [timeMode, setTimeMode] = useState(() => {
    try { return localStorage.getItem("pref:time-badge:mode") === "day" ? "day" : "week"; }
    catch { return "week"; }
  });
  const onTimeModeChange = useCallback((m) => {
    setTimeMode(m);
    try { localStorage.setItem("pref:time-badge:mode", m); } catch { /* ignore */ }
  }, []);
  const ringContainerRef = useRef(null);
  const queueContainerRef = useRef(null);
  const timeRingContainerRef = useRef(null);
  const pollRef = useRef(null);
  const abortRef = useRef(null);
  const closePopover = useCallback(() => setShowStatsPopover(false), []);
  const closeQueuePopover = useCallback(() => setShowQueuePopover(false), []);
  const closeTimePopover = useCallback(() => setShowTimePopover(false), []);

  // Poll weekly viewing total for the time ring label
  useEffect(() => {
    if (!showTimeRing) return;
    let cancelled = false;
    const load = () => fetchViewingStatsWeek(7)
      .then((r) => {
        if (cancelled) return;
        setTimeWeekTotal(r.total_seconds || 0);
        const days = r.days || [];
        setTimeDayTotal(days.length ? (days[days.length - 1].seconds || 0) : 0);
      })
      .catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => { cancelled = true; clearInterval(id); };
  }, [showTimeRing]);

  // Cleanup restart polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  const isHealthy = isSystemHealthy(health);
  // Memory pressure — orchestrator + children RSS in MB. Steady state is
  // ~200MB; thresholds below trip the pill amber/red so the OOM cascade
  // we saw in early May (47GB before kernel kill) leaves a UI signal long
  // before pm2's 8GB max_memory_restart fires.
  const memMb = sysStats?.xylocopa?.mem_mb;
  const memHigh = typeof memMb === "number" && memMb > 5000;   // 5 GB — almost certainly leaking
  const memWarn = typeof memMb === "number" && memMb > 1000;   // 1 GB — suspicious
  const memLabel = typeof memMb === "number"
    ? memMb >= 1024 ? `${(memMb / 1024).toFixed(1)} GB` : `${Math.round(memMb)} MB`
    : null;

  let chipCls, dotColor, chipLabel, chipTitle;
  if (health === null) {
    chipCls = "bg-gray-500/15 text-gray-400";
    dotColor = "bg-gray-400";
    chipLabel = "...";
    chipTitle = "Checking system health...";
  } else if (!isHealthy) {
    chipCls = "bg-red-500/15 text-red-400";
    dotColor = "bg-danger";
    chipLabel = "Error";
    chipTitle = "System issue detected";
  } else if (memHigh) {
    chipCls = "bg-red-500/15 text-red-400";
    dotColor = "bg-danger";
    chipLabel = `Mem ${memLabel}`;
    chipTitle = `High memory: orchestrator using ${memLabel} (steady state ~200 MB). Likely leak — check logs.`;
  } else if (memWarn) {
    chipCls = "bg-amber-500/15 text-amber-500";
    dotColor = "bg-amber-500";
    chipLabel = `Mem ${memLabel}`;
    chipTitle = `Elevated memory: orchestrator using ${memLabel} (steady state ~200 MB).`;
  } else {
    chipCls = "ok-tint-15 text-ok";
    dotColor = "bg-ok";
    chipLabel = "OK";
    chipTitle = "System healthy";
  }
  const chipPulse = (!isHealthy && health !== null) || memHigh;

  // Weekly task stats — Apple Watch ring (only shown when parent passes showTaskRing)
  const wTotal = taskStats?.weekly_total ?? 0;
  const wPct = taskStats?.weekly_success_pct ?? 0;
  const ringR = 10, ringStroke = 2.5, ringC = 2 * Math.PI * ringR;
  const ringOffset = ringC * (1 - wPct / 100);
  const ringColor = wTotal === 0 ? "#9ca3af" : wPct >= 80 ? "#22c55e" : wPct >= 50 ? "#eab308" : "#f87171";

  return (
    <div className="shrink-0 bg-page border-b border-divider z-10">
      <div className="flex items-center gap-3 px-4 pb-2" style={{ paddingTop: "max(1rem, env(safe-area-inset-top, 1rem))" }}>
        <h1 className="text-xl font-bold text-heading flex-1 shrink-0">{title}</h1>
        {showTaskRing && taskStats && (
          <div className="relative" ref={ringContainerRef}>
            <button
              type="button"
              onClick={() => setShowStatsPopover(v => !v)}
              title={`This week: ${wTotal} tasks, ${wPct}% success`}
              className="shrink-0 flex items-center justify-center w-8 h-8 hover:opacity-80 transition-opacity"
            >
              <svg width="26" height="26" viewBox="0 0 26 26">
                <circle cx="13" cy="13" r={ringR} fill="transparent" stroke={ringColor} strokeWidth={ringStroke} opacity={0.18} />
                <circle cx="13" cy="13" r={ringR} fill="transparent" stroke={ringColor} strokeWidth={ringStroke}
                  strokeLinecap="round" strokeDasharray={ringC} strokeDashoffset={ringOffset}
                  transform="rotate(-90 13 13)" style={{ transition: "stroke-dashoffset 0.6s ease" }} />
                <text x="13" y="13" textAnchor="middle" dominantBaseline="central"
                  fill={ringColor} style={{ fontSize: "8px", fontWeight: 700 }}>
                  {wPct}
                </text>
              </svg>
            </button>
            {showStatsPopover && <TaskStatsPopover taskStats={taskStats} onClose={closePopover} containerRef={ringContainerRef} />}
          </div>
        )}
        {showTimeRing && (() => {
          const badgeSecs = timeMode === "day" ? timeDayTotal : timeWeekTotal;
          const badgeTitle = badgeSecs != null
            ? (timeMode === "day"
                ? `Today: ${formatDuration(badgeSecs)} viewing`
                : `This week: ${formatDuration(badgeSecs)} viewing`)
            : "Viewing time";
          return (
            <div className="relative" ref={timeRingContainerRef}>
              <button
                type="button"
                onClick={() => setShowTimePopover(v => !v)}
                title={badgeTitle}
                className="shrink-0 inline-flex items-center gap-1 px-2 h-7 rounded-full accent-tint-15 text-accent hover:accent-tint-25 transition-colors"
              >
                <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2}>
                  <circle cx="12" cy="12" r="9" />
                  <path strokeLinecap="round" d="M12 7v5l3 2" />
                </svg>
                <span className="text-[11px] font-semibold tabular-nums leading-none">
                  {badgeSecs != null ? formatHoursShort(badgeSecs) : "·"}
                </span>
              </button>
              {showTimePopover && (
                <TimeStatsPopover
                  onClose={closeTimePopover}
                  containerRef={timeRingContainerRef}
                  mode={timeMode}
                  onModeChange={onTimeModeChange}
                />
              )}
            </div>
          );
        })()}
        {!hideMonitor && (
          <button
            type="button"
            onClick={() => navigate("/monitor")}
            title={chipTitle}
            className={`shrink-0 inline-flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium transition-colors hover:opacity-80 ${chipCls}`}
          >
            <span className={`inline-block w-1.5 h-1.5 rounded-full ${dotColor} ${chipPulse ? "animate-pulse" : ""}`} />
            {chipLabel}
          </button>
        )}
        {actions}
        {/* Agent Queue button removed — no concurrency cap */}
        <button
          type="button"
          title="Restart Xylocopa"
          disabled={restarting}
          onClick={async () => {
            if (!confirm("Restart Xylocopa server?")) return;
            setRestarting(true);
            if (pollRef.current) clearInterval(pollRef.current);
            if (abortRef.current) abortRef.current.abort();
            const controller = new AbortController();
            abortRef.current = controller;
            try {
              await restartServer();
              let attempts = 0;
              let sawDown = false;
              let consecutiveOk = 0;
              pollRef.current = setInterval(async () => {
                if (controller.signal.aborted) {
                  clearInterval(pollRef.current);
                  return;
                }
                attempts++;
                if (attempts > 60) {
                  clearInterval(pollRef.current);
                  pollRef.current = null;
                  setRestarting(false);
                  alert("Server did not restart after 60s. Check logs.");
                  return;
                }
                try {
                  const h = await fetchHealth();
                  if (controller.signal.aborted) return;
                  if (!sawDown) return;
                  consecutiveOk++;
                  if (consecutiveOk >= 2 && h?.status === "ok") {
                    clearInterval(pollRef.current);
                    pollRef.current = null;
                    // Wipe SW + Cache Storage before reload so the tab
                    // picks up the freshly-built bundle. Plain reload()
                    // would still be served by the old SW precache.
                    try {
                      if ("serviceWorker" in navigator) {
                        const regs = await navigator.serviceWorker.getRegistrations();
                        await Promise.all(regs.map((r) => r.unregister()));
                      }
                      if ("caches" in window) {
                        const keys = await caches.keys();
                        await Promise.all(keys.map((k) => caches.delete(k)));
                      }
                    } catch (e) {
                      console.warn("SW/cache cleanup failed before reload:", e);
                    }
                    window.location.reload();
                  }
                } catch (err) {
                  if (controller.signal.aborted) return;
                  if (err instanceof TypeError || (err.message && /^HTTP 5\d\d/.test(err.message))) {
                    sawDown = true;
                    consecutiveOk = 0;
                  } else {
                    console.warn("Restart poll unexpected error:", err);
                    sawDown = true;
                    consecutiveOk = 0;
                  }
                }
              }, 1000);
            } catch (e) {
              setRestarting(false);
              alert(e.message || "Restart failed");
            }
          }}
          className={`shrink-0 w-8 h-8 flex items-center justify-center rounded-lg transition-colors ${restarting ? "text-amber-400 animate-pulse" : "text-dim hover:text-heading hover:bg-input"}`}
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M5.636 5.636a9 9 0 1012.728 0M12 3v9" />
          </svg>
        </button>
        {selectAction}
        {onToggleTheme && (
          <button
            type="button"
            onClick={onToggleTheme}
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            className="shrink-0 w-8 h-8 flex items-center justify-center rounded-lg text-dim hover:text-heading hover:bg-input transition-colors"
          >
            {theme === "dark" ? SunIcon : MoonIcon}
          </button>
        )}
      </div>
      {children}
    </div>
  );
}
