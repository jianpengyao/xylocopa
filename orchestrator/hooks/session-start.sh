#!/bin/bash
# SessionStart hook: notifies the orchestrator about ALL session starts.
# Installed globally (~/.claude/settings.json) so every claude process
# is detected, whether or not Xylocopa spawned it.
#
# Two modes:
#   Managed (XY_AGENT_ID set): session rotation signal for existing agent
#   Unmanaged: pending-session entry for user to confirm in the UI
#
# Tries HTTP POST first; falls back to local file when orchestrator is offline.
#
# Env vars: XY_PORT/XY_AGENT_ID (preferred), AHIVE_PORT/AHIVE_AGENT_ID (legacy).
# Port resolution: env vars > repo .env > 8080.  Unmanaged (user-launched)
# sessions don't inherit XY_PORT, so we fall back to reading PORT from the
# orchestrator's .env file located relative to this script.

PAYLOAD=$(cat)
export SESSION_ID=$(echo "$PAYLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
[ -z "$SESSION_ID" ] && exit 0
export SESSION_SOURCE=$(echo "$PAYLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin).get('source',''))" 2>/dev/null)

PORT="${XY_PORT:-${AHIVE_PORT:-}}"
if [ -z "$PORT" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
  ENV_FILE="${SCRIPT_DIR}/../../.env"
  if [ -f "$ENV_FILE" ]; then
    PORT=$(grep -E '^[[:space:]]*PORT=' "$ENV_FILE" | head -n1 | sed -E 's/^[[:space:]]*PORT=//' | tr -d '"' | tr -d "'" | tr -d '[:space:]')
  fi
fi
PORT="${PORT:-8080}"
AGENT_ID="${XY_AGENT_ID:-${AHIVE_AGENT_ID:-}}"

# Try HTTP POST to orchestrator. The hook nonce reaches curl on stdin
# (-H @-), never in argv: /proc/<pid>/cmdline is world-readable. An empty
# header value is not sent.
HTTP_CODE=$(printf 'X-Xy-Hook-Nonce: %s\n' "${XY_HOOK_NONCE:-}" | curl -sf -o /dev/null -w "%{http_code}" \
  -X POST "http://localhost:${PORT}/api/hooks/agent-session-start" \
  -H "Content-Type: application/json" \
  -H "X-Agent-Id: ${AGENT_ID}" \
  -H "X-Session-Cwd: ${PWD}" \
  -H "X-Tmux-Pane: ${TMUX_PANE:-}" \
  -H @- \
  -d "$(printf '{"session_id":"%s","source":"%s"}' "$SESSION_ID" "$SESSION_SOURCE")" \
  2>/dev/null)

[ "$HTTP_CODE" = "200" ] && exit 0

# Orchestrator offline — persist event for later pickup

if [ -n "$AGENT_ID" ]; then
  # Managed agent: signal file for session rotation detection
  echo "$SESSION_ID" > "/tmp/xy-${AGENT_ID}.newsession" 2>/dev/null
else
  # Unmanaged session: stash full event so backend can replay on refresh
  mkdir -p "/tmp/xy-pending-unlinked" 2>/dev/null
  export TMUX_SESSION_NAME=$(tmux display-message -t "${TMUX_PANE:-}" -p '#{session_name}' 2>/dev/null)
  export PANE_KEY=$(printf '%s' "${TMUX_PANE:-unknown}" | tr -d '%/')
  python3 -c '
import json, os, time
key = os.environ.get("PANE_KEY", "unknown")
data = {
    "session_id": os.environ.get("SESSION_ID", ""),
    "cwd": os.environ.get("PWD", ""),
    "tmux_pane": os.environ.get("TMUX_PANE", ""),
    "tmux_session": os.environ.get("TMUX_SESSION_NAME", ""),
    "source": os.environ.get("SESSION_SOURCE", ""),
    "ts": time.time(),
}
with open("/tmp/xy-pending-unlinked/pane-" + key + ".json", "w") as f:
    json.dump(data, f)
' 2>/dev/null
fi
