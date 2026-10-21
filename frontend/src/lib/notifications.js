const MUTED_KEY = "xylocopa-muted-agents";

// One-time migration of legacy "agenthive-*" localStorage keys
try {
  const pairs = [
    ["agenthive-muted-agents", MUTED_KEY],
  ];
  for (const [oldK, newK] of pairs) {
    const legacy = localStorage.getItem(oldK);
    if (legacy !== null) {
      if (localStorage.getItem(newK) === null) localStorage.setItem(newK, legacy);
      localStorage.removeItem(oldK);
    }
  }
} catch {}

/** Get the set of muted agent IDs from localStorage. */
function getMutedAgents() {
  try {
    const v = localStorage.getItem(MUTED_KEY);
    return v ? new Set(JSON.parse(v)) : new Set();
  } catch (err) {
    console.warn("getMutedAgents: failed to read muted agents:", err);
    return new Set();
  }
}

/** Check if a specific agent is muted. */
export function isAgentMuted(agentId) {
  return getMutedAgents().has(agentId);
}

/** Set mute state for a specific agent. */
export function setAgentMuted(agentId, muted) {
  const set = getMutedAgents();
  if (muted) set.add(agentId);
  else set.delete(agentId);
  localStorage.setItem(MUTED_KEY, JSON.stringify([...set]));
  window.dispatchEvent(new CustomEvent("agent-mute-changed", { detail: { agentId, muted } }));
}

/** Agents currently being viewed (across all panes / tabs). */
const _viewingAgents = new Set();
/** Number of active TasksPage / TaskDetailPage instances viewing tasks. */
let _viewingTasksCount = 0;

/** Per-agent timestamp (ms) of the most recent user interaction within
 *  that agent's pane. Used to pick the "primary" agent for time-tracking
 *  in split-screen (only the pane the user is actively interacting with
 *  accrues viewing time). */
const _agentInteractionAt = new Map();

export function registerViewing(agentId) {
  if (!agentId) return;
  _viewingAgents.add(agentId);
  // Seed interaction so a freshly-opened session registers immediately
  // without waiting for the user to move the mouse.
  _agentInteractionAt.set(agentId, Date.now());
}
export function unregisterViewing(agentId) {
  if (!agentId) return;
  _viewingAgents.delete(agentId);
  _agentInteractionAt.delete(agentId);
}
export function registerViewingTasks() { _viewingTasksCount++; }
export function unregisterViewingTasks() { _viewingTasksCount = Math.max(0, _viewingTasksCount - 1); }

/** Record user interaction inside a specific agent's pane. */
export function touchAgentInteraction(agentId) {
  if (!agentId) return;
  _agentInteractionAt.set(agentId, Date.now());
}

/** Return the agent ID with the most recent interaction within
 *  `maxAgeMs`, or null if no viewed agent has recent activity.
 *  Restricted to the provided `viewingSet` so we never pick an agent
 *  that isn't currently mounted. */
export function pickPrimaryAgent(viewingSet, maxAgeMs = 180000) {
  const cutoff = Date.now() - maxAgeMs;
  let bestId = null;
  let bestTs = -Infinity;
  for (const id of viewingSet) {
    const ts = _agentInteractionAt.get(id);
    if (ts == null || ts < cutoff) continue;
    if (ts > bestTs) { bestTs = ts; bestId = id; }
  }
  return bestId;
}

/** No-op — notifications are now handled entirely by backend web push. */
export function clearAgentNotified(_agentId) {}
