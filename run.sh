#!/bin/bash
# Xylocopa — cross-platform launch script (Linux + macOS)
# Uses pm2 for process management.  Auto-migrates from systemd on first run.
# Usage:
#   ./run.sh                       — restart both backend + frontend
#   ./run.sh stop                  — stop both
#   ./run.sh status                — show service status
#   ./run.sh logs                  — follow pm2 logs
#   ./run.sh startup               — enable auto-start on boot
#   ./run.sh build-frontend-if-stale — rebuild dist/ only if src is newer
#   ./run.sh reset-password [...]  — break-glass web UI password reset (--help)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ECOSYSTEM="$SCRIPT_DIR/ecosystem.config.cjs"

# ── Ensure required directories exist ─────────────────────────────────
mkdir -p "$SCRIPT_DIR/data" "$SCRIPT_DIR/logs" "$SCRIPT_DIR/backups" "$SCRIPT_DIR/project-configs"

# ── Load .env for variable resolution ─────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

# ── Break-glass password reset ───────────────────────────────────────
# Dispatched before the systemd/pm2 steps so it works with only the venv
# (server running or stopped). See orchestrator/reset_password.py.
if [ "${1:-}" = "reset-password" ]; then
    shift
    PY="$SCRIPT_DIR/.venv/bin/python"
    [ -x "$PY" ] || PY=python3
    cd "$SCRIPT_DIR/orchestrator"
    exec "$PY" reset_password.py "$@"
fi

# ── Migrate from systemd → pm2 (one-time, Linux only) ────────────────
_migrate_from_systemd() {
    local SYSTEMD_DIR="$HOME/.config/systemd/user"
    local BACKEND_UNIT="cc-orchestrator.service"
    local FRONTEND_UNIT="cc-frontend.service"
    local migrated=0

    if [ -f "$SYSTEMD_DIR/$BACKEND_UNIT" ] || [ -f "$SYSTEMD_DIR/$FRONTEND_UNIT" ]; then
        echo "Migrating from systemd to pm2..."
        # Stop and disable old services
        systemctl --user stop "$FRONTEND_UNIT" "$BACKEND_UNIT" 2>/dev/null || true
        systemctl --user disable "$FRONTEND_UNIT" "$BACKEND_UNIT" 2>/dev/null || true
        # Remove unit files
        rm -f "$SYSTEMD_DIR/$BACKEND_UNIT" "$SYSTEMD_DIR/$FRONTEND_UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
        echo "Old systemd services removed."
        migrated=1
    fi

    return $migrated
}

# Only attempt migration on Linux where systemctl exists
if command -v systemctl >/dev/null 2>&1; then
    _migrate_from_systemd || true
fi

# ── Frontend stale-detection + auto-rebuild ──────────────────────────
# Since we serve dist/ via `vite preview` (no HMR), restarts must rebuild
# the bundle when src/ has moved ahead of dist/.  Returns 0 if a rebuild
# is needed, 1 if dist/ is up to date.
_needs_frontend_build() {
    local fdir="$SCRIPT_DIR/frontend"
    local dist_index="$fdir/dist/index.html"
    [ -f "$dist_index" ] || return 0   # no dist at all → build
    # Any source file newer than dist/index.html → build.  Excludes
    # dist/, node_modules/, dev-dist/.  Tracks the same file types vite
    # processes plus root-level config (vite.config.js, package.json).
    local newer
    newer=$(find "$fdir" \
        \( -path "$fdir/dist" -o -path "$fdir/node_modules" -o -path "$fdir/dev-dist" \) -prune -o \
        -type f \( -name "*.js" -o -name "*.jsx" -o -name "*.ts" -o -name "*.tsx" \
                -o -name "*.css" -o -name "*.html" -o -name "*.json" \
                -o -name "*.svg" -o -name "*.png" \) \
        -newer "$dist_index" -print 2>/dev/null | head -1)
    [ -n "$newer" ]
}

_build_frontend_if_stale() {
    if _needs_frontend_build; then
        echo "Frontend src newer than dist — rebuilding..."
        if ! ( cd "$SCRIPT_DIR/frontend" && npx vite build ); then
            echo "Frontend build FAILED — aborting." >&2
            return 1
        fi
        echo "Frontend rebuilt."
    else
        echo "Frontend dist up to date — skipping build."
    fi
}

# ── Ensure pm2 is available ──────────────────────────────────────────
if ! command -v pm2 >/dev/null 2>&1; then
    echo "pm2 not found — installing globally..."
    npm install -g pm2
fi

# ── Port config ──────────────────────────────────────────────────────
PORT="${PORT:-8080}"
FPORT="${FRONTEND_PORT:-3000}"

# ── Command dispatch ──────────────────────────────────────────────────
CMD="${1:-restart}"

case "$CMD" in
    stop)
        echo "Stopping Xylocopa..."
        pm2 stop "$ECOSYSTEM" 2>/dev/null || true
        echo "Stopped."
        ;;
    status)
        pm2 status
        ;;
    logs)
        pm2 logs --lines 50
        ;;
    startup)
        echo "Configuring auto-start on boot..."
        "$SCRIPT_DIR/heal-venv.sh" || echo "heal-venv: continuing despite errors"
        pm2 start "$ECOSYSTEM" 2>/dev/null || true
        pm2 save
        pm2 startup
        echo "Follow the instructions above if prompted."

        # Install tmux user service for cold-boot reliability.
        # tmux servers forked from pm2's systemd cgroup are killed at
        # cold boot; this service starts tmux in user.slice instead.
        if command -v systemctl >/dev/null 2>&1; then
            _TMUX_UNIT_DIR="$HOME/.config/systemd/user"
            _TMUX_UNIT="$_TMUX_UNIT_DIR/xylocopa-tmux.service"
            mkdir -p "$_TMUX_UNIT_DIR"
            cat > "$_TMUX_UNIT" <<'UNIT'
[Unit]
Description=Xylocopa tmux server
After=default.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'exec tmux start-server \; set-option -g exit-empty off'

[Install]
WantedBy=default.target
UNIT
            systemctl --user daemon-reload 2>/dev/null || true
            systemctl --user enable xylocopa-tmux.service 2>/dev/null || true
            # Enable lingering so user services start at boot without login
            loginctl enable-linger "$(whoami)" 2>/dev/null || true
            echo "tmux cold-boot service installed."
        fi
        ;;
    build-frontend-if-stale)
        _build_frontend_if_stale
        ;;
    restart|start)
        echo "Restarting Xylocopa..."
        # Self-heal venv shebangs/activate paths if the project dir was moved
        "$SCRIPT_DIR/heal-venv.sh" || echo "heal-venv: continuing despite errors"
        # Rebuild frontend dist/ if src/ has moved ahead — `vite preview`
        # serves the static bundle, so a stale dist would mask code changes.
        _build_frontend_if_stale || exit 1
        # Delete stale processes first — a prior crash or direct-kill can leave
        # PM2's process table referencing dead PIDs, causing TypeError crashes
        # on `pm2 restart`.  `delete` is idempotent and clears that state.
        # Delete both new (xylocopa-*) and legacy (agenthive-*) names so an
        # upgraded install doesn't end up running both.
        pm2 delete xylocopa-backend xylocopa-frontend agenthive-backend agenthive-frontend 2>/dev/null || true
        sleep 1   # let PM2 daemon finish cleanup to avoid stale-process race
        pm2 start "$ECOSYSTEM"

        # Wait for backend health
        echo -n "Waiting for backend..."
        for i in $(seq 1 30); do
            if curl -sf "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
                echo " ready!"
                break
            fi
            echo -n "."
            sleep 1
        done

        # Verify frontend
        echo -n "Waiting for frontend..."
        for i in $(seq 1 15); do
            if curl -sfk "https://localhost:${FPORT}" >/dev/null 2>&1; then
                echo " ready!"
                break
            fi
            echo -n "."
            sleep 1
        done

        echo ""
        pm2 status
        echo ""
        # Refresh dump.pm2 so a future reboot resurrects the current config.
        # pm2 resurrect replays dump.pm2 verbatim — it does not re-read
        # ecosystem.config.cjs or .env — so a stale dump would bring the
        # service back with whatever env was saved last, not what's running now.
        pm2 save >/dev/null 2>&1 || true
        echo "Xylocopa running at https://localhost:${FPORT}"
        ;;
    *)
        echo "Usage: ./run.sh [start|stop|restart|status|logs|startup|build-frontend-if-stale|reset-password]"
        exit 1
        ;;
esac
