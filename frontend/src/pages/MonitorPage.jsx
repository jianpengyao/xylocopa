import { useEffect, useState, useCallback } from "react";
import PageHeader from "../components/PageHeader";
import {
  truncateLogs,
  fetchTelemetryStatus, setTelemetryEnabled,
} from "../lib/api";
import { useMonitor } from "../contexts/MonitorContext";
import { getEinkMode, setEinkMode } from "../lib/einkMode";
import { getOrbEnabled, setOrbEnabled } from "../lib/orbMode";
import ThemeSettings from "../components/ThemeSettings";


const HEALTH_COLORS = {
  ok: "bg-ok",
  error: "bg-danger",
  degraded: "bg-attn",
  unavailable: "bg-danger",
  unknown: "bg-gray-500",
};


function formatResetTime(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  if (isNaN(d)) return "";
  const now = new Date();
  const diffMs = d - now;
  if (diffMs <= 0) return "now";
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 60) return `in ${diffMin}m`;
  const diffH = Math.floor(diffMin / 60);
  const remMin = diffMin % 60;
  if (diffH < 24) return `in ${diffH}h${remMin > 0 ? ` ${remMin}m` : ""}`;
  const diffD = Math.floor(diffH / 24);
  const remH = diffH % 24;
  return `in ${diffD}d ${remH}h`;
}

function UsageBar({ label, pct, detail }) {
  const barColor =
    pct >= 90 ? "bg-danger" : pct >= 70 ? "bg-attn" : "bg-accent";
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-label">{label}</span>
        <span className="text-xs text-dim font-mono">{detail}</span>
      </div>
      <div className="h-2 rounded-full bg-elevated overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${barColor}`}
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
    </div>
  );
}

function HealthCard({ label, status }) {
  const color = HEALTH_COLORS[status] || HEALTH_COLORS.unknown;
  return (
    <div className="rounded-xl bg-surface shadow-card p-4 flex items-center gap-3 min-w-0">
      <span className={`inline-block w-2.5 h-2.5 rounded-full ${color}`} />
      <div className="min-w-0">
        <p className="text-xs text-dim uppercase tracking-wider">{label}</p>
        <p className="text-sm font-medium text-heading truncate">{status}</p>
      </div>
    </div>
  );
}

// Legend dots ride the token utilities; SVG segments read the same vars
// via inline style (presentation attributes can't hold var()). "cyan" is
// the theme accent so ring and legend agree with the palette; the rest are
// --chart-* tokens, per-theme muted via --chart-mute in index.css.
const STORAGE_COLORS = {
  cyan: { dot: "bg-accent", stroke: "var(--color-accent)" },
  violet: { dot: "bg-chart-violet", stroke: "var(--chart-violet)" },
  amber: { dot: "bg-chart-amber", stroke: "var(--chart-amber)" },
  emerald: { dot: "bg-chart-emerald", stroke: "var(--chart-emerald)" },
  orange: { dot: "bg-chart-orange", stroke: "var(--chart-orange)" },
  rose: { dot: "bg-chart-rose", stroke: "var(--chart-rose)" },
  gray: { dot: "bg-gray-400", stroke: "#9ca3af" },
};

function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const val = bytes / Math.pow(1024, i);
  return `${val < 10 ? val.toFixed(1) : Math.round(val)} ${units[i]}`;
}

function StorageChart({ data, onRefresh }) {
  const [cleaning, setCleaning] = useState(false);

  const handleCleanLogs = useCallback(async () => {
    setCleaning(true);
    try {
      await truncateLogs();
      if (onRefresh) await onRefresh();
    } catch (err) {
      console.error("Failed to truncate logs:", err);
    } finally {
      setCleaning(false);
    }
  }, [onRefresh]);

  if (!data) return null;
  const { categories, total_bytes } = data;
  const visible = categories.filter((c) => c.size_bytes > 0);
  if (visible.length === 0) return null;

  const radius = 52;
  const stroke = 14;
  const size = 140;
  const circumference = 2 * Math.PI * radius;

  // Build segments
  let offset = 0;
  const segments = visible.map((cat) => {
    const pct = total_bytes > 0 ? cat.size_bytes / total_bytes : 0;
    const dash = pct * circumference;
    const gap = circumference - dash;
    const seg = { ...cat, pct, dash, gap, offset };
    offset += dash;
    return seg;
  });

  return (
    <section>
      <h2 className="text-xs font-semibold text-dim uppercase tracking-wider mb-2">Storage</h2>
      <div className="rounded-xl bg-surface shadow-card p-4 flex items-center gap-4">
        {/* Donut ring — left */}
        <div className="relative shrink-0" style={{ width: size, height: size }}>
          <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
            <circle
              cx={size / 2} cy={size / 2} r={radius}
              fill="none" strokeWidth={stroke}
              className="stroke-elevated"
            />
            {segments.map((seg) => (
              <circle
                key={seg.name}
                cx={size / 2} cy={size / 2} r={radius}
                fill="none" strokeWidth={stroke}
                style={{ stroke: (STORAGE_COLORS[seg.color] || STORAGE_COLORS.gray).stroke }}
                strokeDasharray={`${seg.dash} ${seg.gap}`}
                strokeDashoffset={-seg.offset}
                strokeLinecap="butt"
                transform={`rotate(-90 ${size / 2} ${size / 2})`}
              />
            ))}
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className="text-sm font-bold text-heading">{formatBytes(total_bytes)}</span>
            <span className="text-[10px] text-dim">total</span>
          </div>
        </div>
        {/* Legend — right */}
        <div className="flex-1 min-w-0 space-y-1">
          {visible.map((cat) => {
            const colors = STORAGE_COLORS[cat.color] || STORAGE_COLORS.gray;
            const isLogs = cat.name === "Logs";
            return (
              <div key={cat.name} className="flex items-center gap-2 text-xs">
                <span className={`w-2 h-2 rounded-full shrink-0 ${colors.dot}`} />
                <span className="text-label truncate flex-1">{cat.name}</span>
                <span className="text-dim font-mono shrink-0">{formatBytes(cat.size_bytes)}</span>
                {isLogs && cat.size_bytes > 50 * 1024 * 1024 && (
                  <button
                    onClick={handleCleanLogs}
                    disabled={cleaning}
                    className="ml-1 text-dim hover:text-orange-500 transition-colors disabled:opacity-40"
                    title="Truncate log files"
                  >
                    {cleaning ? (
                      <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2v4m0 12v4m-7.07-3.93l2.83-2.83m8.48-8.48l2.83-2.83M2 12h4m12 0h4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83" /></svg>
                    ) : (
                      <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" /></svg>
                    )}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function TokenUsageSection({ tokenUsage, onRefresh }) {
  const [spinning, setSpinning] = useState(false);
  const handleRefresh = useCallback(async () => {
    setSpinning(true);
    await onRefresh();
    setTimeout(() => setSpinning(false), 400);
  }, [onRefresh]);

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-xs font-semibold text-dim uppercase tracking-wider">Token Usage</h2>
        <button
          type="button"
          onClick={handleRefresh}
          title="Refresh token usage"
          className="w-6 h-6 flex items-center justify-center rounded-md hover:bg-input transition-colors"
        >
          <svg className={`w-3.5 h-3.5 text-dim ${spinning ? "animate-spin-reverse" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        </button>
      </div>
      <div className="rounded-xl bg-surface shadow-card p-4 space-y-3">
        {!tokenUsage || tokenUsage._error ? (
          <p className="text-xs text-faint">
            {tokenUsage?._error ? "Unable to fetch — tap refresh to retry" : "Loading..."}
          </p>
        ) : (
          <>
            {tokenUsage.session && (
              <UsageBar
                label="Session (5h)"
                pct={tokenUsage.session.utilization ?? 0}
                detail={`${tokenUsage.session.utilization ?? 0}% — resets ${formatResetTime(tokenUsage.session.resets_at)}`}
              />
            )}
            {tokenUsage.weekly && (
              <UsageBar
                label="Weekly (7d)"
                pct={tokenUsage.weekly.utilization ?? 0}
                detail={`${tokenUsage.weekly.utilization ?? 0}% — resets ${formatResetTime(tokenUsage.weekly.resets_at)}`}
              />
            )}
            {(tokenUsage.weekly_scoped || []).map((m) => (
              <UsageBar
                key={m.label}
                label={`Weekly (${m.label})`}
                pct={m.utilization ?? 0}
                detail={`${m.utilization ?? 0}% — resets ${formatResetTime(m.resets_at)}`}
              />
            ))}
          </>
        )}
      </div>
    </section>
  );
}

export default function MonitorPage({ theme, onToggleTheme }) {
  const {
    health, healthError, sysStats, tokenUsage, storageStats,
    refresh, refreshTokenUsage, activate, deactivate,
  } = useMonitor();
  const [refreshing, setRefreshing] = useState(false);
  const [telemetry, setTelemetry] = useState(null);
  const [telemetryBusy, setTelemetryBusy] = useState(false);
  const [einkOn, setEinkOn] = useState(() => getEinkMode());
  const [orbOn, setOrbOn] = useState(() => getOrbEnabled());

  const handleEinkToggle = useCallback(() => {
    const next = !einkOn;
    setEinkOn(next);
    setEinkMode(next);
  }, [einkOn]);

  const handleOrbToggle = useCallback(() => {
    const next = !orbOn;
    setOrbOn(next);
    setOrbEnabled(next);
  }, [orbOn]);

  const loadTelemetry = useCallback(async () => {
    try {
      setTelemetry(await fetchTelemetryStatus());
    } catch (e) {
      console.warn("Telemetry status fetch failed:", e);
    }
  }, []);

  const handleTelemetryToggle = useCallback(async () => {
    if (!telemetry || telemetry.env_locked || telemetryBusy) return;
    setTelemetryBusy(true);
    try {
      const next = await setTelemetryEnabled(!telemetry.enabled);
      setTelemetry(next);
    } catch (e) {
      console.warn("Telemetry toggle failed:", e);
    } finally {
      setTelemetryBusy(false);
    }
  }, [telemetry, telemetryBusy]);

  // Activate fast polling while this page is mounted; show cached data
  // immediately, then do a fresh fetch.
  useEffect(() => {
    activate();
    refresh();
    loadTelemetry();
    return () => deactivate();
  }, [activate, deactivate, refresh, loadTelemetry]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    await refresh();
    // Minimum 400ms spinner display to prevent jarring sub-frame flicker
    setTimeout(() => setRefreshing(false), 400);
  }, [refresh]);

  return (
    <div className="h-full flex flex-col">
      <PageHeader title="Monitor" theme={theme} onToggleTheme={onToggleTheme}>
        <div className="px-4 pb-2 flex items-center justify-between">
          <span className="text-xs text-faint">Auto-refreshing every 5s</span>
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
      </PageHeader>

      <div className="flex-1 overflow-y-auto overflow-x-hidden">
      <div className="pb-24 p-4 space-y-5 max-w-2xl mx-auto w-full">
        {/* System Health */}
        <section>
          <h2 className="text-xs font-semibold text-dim uppercase tracking-wider mb-2">System Health</h2>
          {healthError && !health ? (
            <div className="rounded-xl bg-surface shadow-card p-4">
              <p className="text-sm text-danger">Failed to reach health endpoint.</p>
            </div>
          ) : (
            <div className="grid grid-cols-3 gap-3">
              <HealthCard label="Overall" status={health?.status || "unknown"} />
              <HealthCard label="Database" status={health?.db || "unknown"} />
              <HealthCard label="Claude CLI" status={health?.claude_cli || "unknown"} />
            </div>
          )}
        </section>

        {/* System Resources */}
        {sysStats && (
          <section>
            <h2 className="text-xs font-semibold text-dim uppercase tracking-wider mb-2">Resources</h2>
            <div className="space-y-3">
              {/* CPU / Memory / Disk bars */}
              <div className="rounded-xl bg-surface shadow-card p-4 space-y-3">
                {sysStats.cpu && (
                  <UsageBar
                    label={`CPU (${sysStats.cpu.cores} cores)`}
                    pct={sysStats.cpu.usage_pct}
                    detail={`Load ${sysStats.cpu.load_1m}`}
                  />
                )}
                {sysStats.memory && (
                  <UsageBar
                    label="Memory"
                    pct={sysStats.memory.usage_pct}
                    detail={`${sysStats.memory.used_gb} / ${sysStats.memory.total_gb} GB`}
                  />
                )}
                {sysStats.disk && (
                  <UsageBar
                    label="Disk"
                    pct={sysStats.disk.usage_pct}
                    detail={`${sysStats.disk.used_gb} / ${sysStats.disk.total_gb} GB`}
                  />
                )}
                {(sysStats.xylocopa || sysStats.agenthive) && (() => {
                  const proc = sysStats.xylocopa || sysStats.agenthive;
                  return (
                    <UsageBar
                      label="Xylocopa"
                      pct={sysStats.memory ? Math.min(Math.round(proc.mem_mb / (sysStats.memory.total_gb * 1024) * 100), 100) : 0}
                      detail={`${proc.mem_mb} MB / ${proc.cpu_pct}% CPU`}
                    />
                  );
                })()}
              </div>

              {/* GPUs */}
              {sysStats.gpus && sysStats.gpus.length > 0 && (
                <div className="rounded-xl bg-surface shadow-card p-4 space-y-3">
                  {sysStats.gpus.map((gpu) => (
                    <div key={gpu.index}>
                      <p className="text-xs text-label font-medium mb-2">
                        GPU {gpu.index}: {gpu.name}
                        <span className="text-dim ml-2">{gpu.temp_c}°C</span>
                      </p>
                      <div className="space-y-2">
                        <UsageBar label="Compute" pct={gpu.gpu_pct} detail={`${gpu.gpu_pct}%`} />
                        <UsageBar
                          label="VRAM"
                          pct={gpu.mem_pct}
                          detail={`${gpu.mem_used_mb} / ${gpu.mem_total_mb} MB`}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
        )}

        {/* Token Usage — auto-refreshes every 10 min */}
        <TokenUsageSection tokenUsage={tokenUsage} onRefresh={refreshTokenUsage} />

        {/* Storage */}
        <StorageChart data={storageStats} onRefresh={refresh} />


        {/* Display: theme palettes + custom editor */}
        <ThemeSettings theme={theme} />

        {/* Display: assistant character (orb) toggle */}
        <section className="rounded-xl bg-surface shadow-card p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-heading flex items-center gap-2">
                Assistant character
                <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-amber-500/15 text-amber-600 dark:text-amber-400">
                  Experimental
                </span>
              </h3>
              <p className="text-xs text-dim mt-1 leading-relaxed">
                The orb assistant with its chat bubble — talk to it to set up
                attention jobs (reminders, watchers, scheduled tasks). Still
                being polished. When off, the corner button is the classic
                unread badge — tap opens the oldest unread chat, long-press
                opens split screen.
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={orbOn ? "true" : "false"}
              aria-label="Toggle assistant character"
              onClick={handleOrbToggle}
              className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                orbOn ? "bg-accent" : "bg-elevated"
              }`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                  orbOn ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </div>
        </section>

        {/* Display: e-ink mode toggle */}
        <section className="rounded-xl bg-surface shadow-card p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-heading">E-ink mode</h3>
              <p className="text-xs text-dim mt-1 leading-relaxed">
                Grayscale palette, no animations or blur — for e-paper
                displays (Bigme, BOOX, Kindle). Also enters fullscreen.
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={einkOn ? "true" : "false"}
              aria-label="Toggle e-ink mode"
              onClick={handleEinkToggle}
              className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                einkOn ? "bg-accent" : "bg-elevated"
              }`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                  einkOn ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </div>
        </section>

        {/* Help improve Xylocopa (telemetry toggle) */}
        <section className="rounded-xl bg-surface shadow-card p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-heading">Help improve Xylocopa</h3>
              <p className="text-xs text-dim mt-1 leading-relaxed">
                A simple anonymous heartbeat helps me know the project is being used.
                No IPs, prompts, code, or paths.{" "}
                <a
                  href="https://github.com/jianpengyao/xylocopa#telemetry"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-accent hover:underline"
                >
                  Details
                </a>
              </p>
              {telemetry?.env_locked && (
                <p className="text-xs text-amber-500 mt-2">
                  Locked off via <code className="font-mono">XYLOCOPA_TELEMETRY</code> env var.
                </p>
              )}
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={telemetry?.enabled ? "true" : "false"}
              aria-label="Toggle anonymous telemetry"
              disabled={!telemetry || telemetry.env_locked || telemetryBusy}
              onClick={handleTelemetryToggle}
              className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                telemetry?.enabled ? "bg-accent" : "bg-elevated"
              }`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                  telemetry?.enabled ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </div>
        </section>

      </div>
      </div>
    </div>
  );
}
