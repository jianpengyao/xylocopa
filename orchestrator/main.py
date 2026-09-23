"""Xylocopa — FastAPI entry point."""

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

# Clear Claude Code nesting-detection vars from the orchestrator process
# so spawned agents don't refuse to start.
os.environ.pop("CLAUDECODE", None)
os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)

import yaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from sqlalchemy.orm import Session

from config import CORS_ORIGINS, PORT, PROJECT_CONFIGS_PATH, CC_MODEL, VALID_MODELS
from database import SessionLocal, get_db, init_db
from log_config import setup_logging
from models import Agent, Project, Task, TaskStatus, AgentStatus
from auth import get_jwt_secret, get_password_hash, verify_token

setup_logging()
logger = logging.getLogger("orchestrator")

# Frontend debug logger — writes to a dedicated file for easy tailing.
# Size-rotated (20 MB × 3 files): unbounded FileHandler let this grow to 100 MB+.
_fe_handler = RotatingFileHandler(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "frontend-debug.log"),
    maxBytes=20 * 1024 * 1024, backupCount=2, encoding="utf-8",
)
_fe_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
_fe_logger = logging.getLogger("frontend.debug")
_fe_logger.addHandler(_fe_handler)
_fe_logger.setLevel(logging.DEBUG)
_fe_logger.propagate = False  # dedicated file only — keep [fe] chatter out of orchestrator.log

# Keyboard-viewport debug logger — high-volume per-frame samples from
# /api/debug/kb-log. Raw lines, no timestamp prefix (samples carry t=).
# Size-rotated (20 MB × 3): the old raw-append writer grew to 1.2 GB.
_kb_handler = RotatingFileHandler(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "kb-debug.log"),
    maxBytes=20 * 1024 * 1024, backupCount=2, encoding="utf-8",
)
_kb_handler.setFormatter(logging.Formatter("%(message)s"))
_kb_logger = logging.getLogger("kb.debug")
_kb_logger.addHandler(_kb_handler)
_kb_logger.setLevel(logging.DEBUG)
_kb_logger.propagate = False  # per-frame samples must not spill into orchestrator.log


# ---- Registry loader ----

def load_registry(db: Session):
    """Load projects from registry.yaml into database."""
    registry_path = os.path.join(PROJECT_CONFIGS_PATH, "registry.yaml")
    if not os.path.exists(registry_path):
        logger.warning("registry.yaml not found at %s", registry_path)
        return

    with open(registry_path) as f:
        data = yaml.safe_load(f)

    projects = data.get("projects") or []
    if not projects:
        logger.info("No projects in registry.yaml")
        return

    _valid_name = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    for p in projects:
        pname = p.get("name", "")
        if not pname or not _valid_name.match(pname) or "/" in pname or "\\" in pname:
            logger.warning("Skipping project with invalid name: %r", pname)
            continue
        # Validate model name — fall back to global default if invalid
        raw_model = p.get("default_model", CC_MODEL)
        if raw_model not in VALID_MODELS:
            logger.warning(
                "Project %r has invalid default_model %r, using %s",
                pname, raw_model, CC_MODEL,
            )
            raw_model = CC_MODEL

        existing = db.get(Project, p["name"])
        if existing:
            existing.display_name = p.get("display_name", p["name"])
            existing.path = p.get("path", f'/projects/{p["name"]}')
            existing.git_remote = p.get("git_remote")
            existing.description = p.get("description")
            existing.max_concurrent = p.get("max_concurrent", 8)
            existing.default_model = raw_model
        else:
            db.add(Project(
                name=p["name"],
                display_name=p.get("display_name", p["name"]),
                path=p.get("path", f'/projects/{p["name"]}'),
                git_remote=p.get("git_remote"),
                description=p.get("description"),
                max_concurrent=p.get("max_concurrent", 8),
                default_model=raw_model,
            ))
    db.commit()
    logger.info("Loaded %d projects from registry.yaml", len(projects))


# ---- One-shot migration: pre_sent legacy rows ----

def _migrate_pre_sent_legacy():
    """Clean up pre-cutover DB rows that no longer belong in `messages`.

    Pre-Phase-2 code created DB rows for SENT/CANCELLED web/task/
    plan_continue messages. Post-Phase-2 those states live in the display
    file's pre-sent zone (no DB row) or, once dispatched, as COMPLETED
    rows. This migration reconciles residue.

    PENDING rows are pre-cleaned by database.py on enum load (the
    MessageStatus.PENDING value was removed); this function only
    handles CANCELLED residue.

    Rules:
      - CANCELLED with display_seq: was delivered then cancelled (historical
        quirk); flip to COMPLETED to honor the "DB only holds delivered"
        invariant; display-file tombstone already hides the bubble.
      - CANCELLED without display_seq: pure pre-sent cancel; display
        file already has the tombstone; just delete the row.
      - SENT without delivered_at: tmux send already succeeded (the row
        only enters DB after `_promote_pre_sent_to_sent` returns OK).
        delivered_at=NULL just means UserPromptSubmit hook never fired —
        could be a TUI modal, agent crashed, agent busy, etc. Leave the
        row alone; user can manually re-send if they want. (Older code
        re-queued these on every startup, which caused the same message
        to be re-dispatched to tmux multiple times.)

    Idempotent. Runs on every startup; a clean DB makes it a no-op.
    """
    from models import Message, MessageStatus

    db = SessionLocal()
    fixed_completed = 0
    deleted_cancelled = 0
    try:
        legacy = (
            db.query(Message)
            .filter(
                Message.source.in_(("web", "task", "plan_continue")),
                Message.status == MessageStatus.CANCELLED,
            )
            .all()
        )
        for msg in legacy:
            try:
                if msg.display_seq is not None:
                    msg.status = MessageStatus.COMPLETED
                    if not msg.completed_at:
                        msg.completed_at = msg.delivered_at
                    fixed_completed += 1
                else:
                    db.delete(msg)
                    deleted_cancelled += 1
            except Exception:
                logger.exception(
                    "Predelivery migration: failed for msg %s (agent %s)",
                    msg.id[:8], msg.agent_id[:8],
                )
        db.commit()
        if fixed_completed or deleted_cancelled:
            logger.info(
                "Predelivery migration: completed=%d, cancelled-deleted=%d",
                fixed_completed, deleted_cancelled,
            )
        else:
            logger.info("Predelivery migration: nothing to migrate")
    finally:
        db.close()


def _detect_tmux_oom_policy() -> str | None:
    """Return the effective OOMPolicy of xylocopa-tmux.service.

    Returns "continue", "stop", "kill" or None if not detected.
    The tmux service hosts all agent sessions — OOMPolicy=continue is
    required so that a child-process OOM doesn't cascade-kill the tmux
    server and every running agent.
    """
    try:
        import subprocess as _sp
        r = _sp.run(
            ["systemctl", "--user", "show", "xylocopa-tmux.service",
             "-p", "OOMPolicy", "--value"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        val = (r.stdout or "").strip().lower()
        return val or None
    except (OSError, ValueError):
        return None


# ---- Lifespan ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    import socket

    port = PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            logger.error(
                "Port %d already in use — another instance may be running. "
                "Exiting to avoid conflicts.",
                port,
            )
            import sys
            sys.exit(1)

    logger.info("Xylocopa starting up...")
    _main_event_loop = asyncio.get_event_loop()

    # Ensure a tmux server is running before any agent resume/launch.
    #
    # Root cause (confirmed May 2026): tmux servers forked from pm2's
    # systemd cgroup (system.slice/pm2-<user>.service) are killed by
    # systemd at cold boot.  The fix is a dedicated user service
    # (xylocopa-tmux.service) that starts the tmux server with
    # exit-empty=off in the user slice, isolated from pm2's cgroup.
    # No keepalive session needed — the server stays alive with zero
    # sessions.
    try:
        import subprocess as _sp_init

        # Activate the user service (tmux server lands in user.slice).
        _svc = _sp_init.run(
            ["systemctl", "--user", "start", "xylocopa-tmux.service"],
            capture_output=True, text=True, timeout=10,
        )
        if _svc.returncode != 0:
            logger.info(
                "tmux preflight: user service unavailable (%s), "
                "falling back to direct creation",
                _svc.stderr.strip(),
            )
            _sp_init.run(
                ["tmux", "start-server", ";", "set-option", "-g",
                 "exit-empty", "off"],
                capture_output=True, text=True, timeout=5,
            )

        # Verify server is reachable.
        _ping = _sp_init.run(
            ["tmux", "list-sessions"],
            capture_output=True, text=True, timeout=5,
        )
        _alive = _ping.returncode == 0 or "no server running" not in _ping.stderr
        if _alive:
            oom_policy = _detect_tmux_oom_policy()
            if oom_policy == "stop":
                logger.warning(
                    "tmux preflight: xylocopa-tmux.service has OOMPolicy=stop "
                    "— a child-process OOM will cascade-kill the tmux server "
                    "and every running agent. Add OOMPolicy=continue to the "
                    "[Service] section of xylocopa-tmux.service.",
                )
            else:
                logger.info(
                    "tmux preflight: server ready via xylocopa-tmux.service "
                    "(OOMPolicy=%s)", oom_policy or "unknown",
                )
        else:
            logger.warning(
                "tmux preflight: server not reachable after start "
                "(stderr=%s)", _ping.stderr.strip(),
            )
    except (OSError, _sp_init.TimeoutExpired) as e:
        logger.warning("tmux preflight failed (non-fatal): %s", e)

    # Anonymous daily heartbeat (opt-out). See orchestrator/telemetry.py.
    try:
        import telemetry
        telemetry.record_heartbeat()
    except Exception:
        logger.debug("Telemetry heartbeat failed (non-fatal)", exc_info=True)

    _check_frontend_dist_staleness()

    # One-time migration: rename legacy ~/.agenthive → ~/.xylocopa if needed
    try:
        _migrate_legacy_user_dirs()
    except Exception:
        logger.exception("Legacy path migration failed (non-fatal)")

    # Make the event loop available to routers that need it for background threads
    from routers import projects as _projects_router
    _projects_router._main_event_loop = _main_event_loop
    from routers import bookmarks as _bookmarks_router
    _bookmarks_router._main_event_loop = _main_event_loop

    init_db()
    logger.info("Database initialized")

    # One-shot migration: move legacy pre-sent DB rows to pre_sent zone.
    # Pre-delivery web/task/plan_continue messages no longer own DB rows. Any
    # legacy PENDING/QUEUED/CANCELLED rows from before the cutover are
    # reconciled here. Idempotent — after the first successful run the SELECT
    # returns zero rows.
    try:
        _migrate_pre_sent_legacy()
    except Exception:
        logger.exception("Predelivery migration failed on startup")

    # Display files: no rebuild on startup. Files are append-only mirrors
    # of DB state, maintained consistent by the write paths
    # (_promote_pre_sent_to_sent etc). Rebuild is reserved for compact
    # (sync_engine) and session rotation (agent_dispatcher) paths where
    # the JSONL identity actually changes. The pre-sent index loads
    # lazily via _ensure_index_loaded on first read.

    # Mark interrupted insight generations as failed
    try:
        from models import Agent
        _startup_db = SessionLocal()
        _stuck = _startup_db.query(Agent).filter(Agent.insight_status == "generating").all()
        for _a in _stuck:
            _a.insight_status = "failed"
            logger.info("Marked interrupted insight generation as failed for agent %s", _a.id)
        if _stuck:
            _startup_db.commit()
        _startup_db.close()
    except Exception:
        logger.exception("Failed to mark interrupted insight generations")

    # Prune zombie push subscriptions (never-acked + older than grace window)
    try:
        from routers.push import prune_zombie_subscriptions
        _push_db = SessionLocal()
        try:
            pruned = prune_zombie_subscriptions(_push_db)
            if pruned:
                logger.info("startup: pruned %d zombie push subscriptions", pruned)
        finally:
            _push_db.close()
    except Exception:
        logger.exception("startup: push-sub prune failed (non-fatal)")

    db = SessionLocal()
    try:
        load_registry(db)
        # Bootstrap the `.xylo-internal` placeholder project (host for AI
        # triage and other meta-agents).  Idempotent — creates the dir,
        # writes a self-contained .mcp.json + CLAUDE.md, and ensures the
        # DB row.  Must run before any meta-agent dispatch path resolves
        # its host project.
        try:
            from routers.projects import ensure_internal_project
            ensure_internal_project(db)
        except Exception:
            logger.exception("startup ensure_internal_project failed (non-fatal)")
        # Auto-replay any SessionStart events the hook stashed while we were
        # offline. Same logic the Agents page refresh button uses — surfaces
        # unlinked entries on backend restart without requiring a manual click.
        try:
            from routers.agents import _do_replay_pending_unlinked
            _do_replay_pending_unlinked(db)
        except Exception:
            logger.exception("startup replay_pending_unlinked failed (non-fatal)")
    finally:
        db.close()

    # Disable Claude Code session auto-cleanup
    from session_cache import ensure_cleanup_disabled
    ensure_cleanup_disabled()

    # Start dispatchers and git manager
    agent_dispatch_task = None
    backup_task = None
    session_cache_task = None
    from agent_dispatcher import AgentDispatcher
    from git_manager import GitManager
    from worker_manager import WorkerManager
    wm = WorkerManager()
    agent_dispatcher = AgentDispatcher(wm)
    gm = GitManager()
    from permissions import PermissionManager
    app.state.permission_manager = PermissionManager()
    app.state.agent_dispatcher = agent_dispatcher
    app.state.worker_manager = wm
    app.state.git_manager = gm
    # One-time migration: pre-rebrand orchestrators wrote user.name=AgentHive
    # into each managed repo's .git/config; rewrite to the current identity so
    # agents stop committing as AgentHive. Idempotent, non-fatal.
    try:
        _migrate_legacy_git_identity(gm)
    except Exception:
        logger.exception("Legacy git identity migration failed (non-fatal)")
    agent_dispatch_task = asyncio.create_task(agent_dispatcher.run())
    logger.info("Dispatcher started")

    # Reap port-webapp processes orphaned by agents that stopped while the
    # server was down (or before reap-on-stop existed).
    try:
        from webapp_reaper import reap_orphan_webapps
        _db_reap = SessionLocal()
        try:
            reap_orphan_webapps(_db_reap)
        finally:
            _db_reap.close()
    except Exception:
        logger.exception("startup webapp orphan sweep failed")

    # Start session cache loop
    from session_cache import run_session_cache_loop
    session_cache_task = asyncio.create_task(
        run_session_cache_loop(agent_dispatcher.get_active_sessions)
    )

    # Install global SessionStart hook so ALL claude processes are detected
    from routers.agents import _write_global_session_hook
    _write_global_session_hook()

    # Refresh project-level hook configs (ensures new hook types are registered)
    from routers.agents import _write_agent_hooks_config, _write_mcp_config
    _db_hooks = SessionLocal()
    _project_paths = [
        p.path for p in _db_hooks.query(Project.path).distinct().all()
        if p.path and os.path.isdir(p.path)
    ]
    _db_hooks.close()
    for _pp in _project_paths:
        _write_agent_hooks_config(_pp)
        _write_mcp_config(_pp)
    if _project_paths:
        logger.info("Refreshed hook configs for %d projects", len(_project_paths))

    # Warm the per-project skills cache off-thread so the first /-trigger in
    # the picker doesn't pay disk-scan latency. Failures are non-fatal.
    def _warm_skills_cache():
        try:
            from skills import refresh_skills_cache
            n = refresh_skills_cache(_project_paths)
            logger.info("Warmed skills cache for %d entries", n)
        except Exception:
            logger.exception("Failed to warm skills cache (non-fatal)")

    import threading
    threading.Thread(target=_warm_skills_cache, name="skills-cache-warmup", daemon=True).start()

    # Recover tasks stuck in EXECUTING whose agent already stopped/errored
    from task_state import TaskStateMachine as _TSM
    _rdb = SessionLocal()
    try:
        _stuck_tasks = (
            _rdb.query(Task)
            .join(Agent, Task.agent_id == Agent.id)
            .filter(
                Task.status == TaskStatus.EXECUTING,
                Agent.status.in_([AgentStatus.STOPPED, AgentStatus.ERROR]),
            )
            .all()
        )
        for _st in _stuck_tasks:
            _agent = _rdb.get(Agent, _st.agent_id)
            if _agent and _agent.status == AgentStatus.ERROR:
                _TSM.transition(_st, TaskStatus.FAILED, strict=False)
            else:
                _TSM.transition(_st, TaskStatus.COMPLETE, strict=False)
        if _stuck_tasks:
            _rdb.commit()
            logger.info("Recovered %d stuck EXECUTING tasks at startup", len(_stuck_tasks))
    finally:
        _rdb.close()

    # Start backup loop
    from backup import run_backup_loop
    backup_task = asyncio.create_task(run_backup_loop())
    logger.info("Backup loop started")

    # Start Dropbox sync loop
    from dropbox_sync.engine import run_sync_loop
    dropbox_task = asyncio.create_task(run_sync_loop())
    logger.info("Dropbox sync loop started")

    # Start WebSocket stale-connection pruning loop
    ws_prune_task = None
    from websocket import ws_manager

    async def _ws_prune_loop():
        while True:
            await asyncio.sleep(30)
            await ws_manager.prune_stale()

    ws_prune_task = asyncio.create_task(_ws_prune_loop())

    # RSS sampler — three log channels so the next leak leaves a usable
    # forensic trail without spamming the steady-state log:
    #
    # 1. INFO every 5 min: baseline sample. Always fires. Gives us a
    #    curve when looking back instead of just threshold crossings.
    # 2. WARNING on threshold cross (200/500/1000/2000/5000/10000/20000 MB):
    #    one-shot per process, marks the level was first hit.
    # 3. WARNING on rapid growth (>200MB/min): smoke alarm. Lower than
    #    before (was 300MB) so we catch a steady leak before it OOMs.
    #
    # Also fires the leak-alert probe (XY_RSS_LEAK_PROBE_URL) once when
    # we first cross 5GB so the diagnostic chat gets woken in real time.
    # Probe is single-fire by design; renew after each fire via probe_create.
    async def _rss_watch_loop():
        import httpx
        import time as _time
        thresholds_mb = [200, 500, 1000, 2000, 5000, 10000, 20000]
        crossed: set[int] = set()
        prev_rss_mb = 0
        # Baseline log every 5 min. Cheap (1 line + 1 /proc read per tick).
        BASELINE_INTERVAL_S = 300
        last_baseline_at = 0.0
        probe_url = os.environ.get("XY_RSS_LEAK_PROBE_URL", "").strip()
        probe_fired = False
        while True:
            await asyncio.sleep(60)
            try:
                vm = {}
                with open(f"/proc/{os.getpid()}/status") as f:
                    for line in f:
                        for k in ("VmRSS:", "VmSize:", "VmPeak:", "RssAnon:", "RssFile:"):
                            if line.startswith(k):
                                vm[k[:-1]] = int(line.split()[1]) // 1024  # MB
                                break
                rss_mb = vm.get("VmRSS")
                if rss_mb is None:
                    continue
            except OSError:
                continue
            now = _time.monotonic()
            # 1. Baseline sample
            if now - last_baseline_at >= BASELINE_INTERVAL_S:
                last_baseline_at = now
                logger.info(
                    "RSS_WATCH baseline: rss=%dMB vm=%dMB peak=%dMB anon=%dMB file=%dMB",
                    rss_mb, vm.get("VmSize", 0), vm.get("VmPeak", 0),
                    vm.get("RssAnon", 0), vm.get("RssFile", 0),
                )
            # 2. Threshold crosses
            for t in thresholds_mb:
                if rss_mb >= t and t not in crossed:
                    crossed.add(t)
                    logger.warning("RSS_WATCH: crossed %d MB (now %d MB, peak %d MB)",
                                   t, rss_mb, vm.get("VmPeak", 0))
            # 3. Rapid growth alarm
            if rss_mb - prev_rss_mb > 200 and prev_rss_mb > 0:
                logger.warning(
                    "RSS_WATCH: jumped %d MB in last minute (%d → %d MB, peak %d MB)",
                    rss_mb - prev_rss_mb, prev_rss_mb, rss_mb, vm.get("VmPeak", 0),
                )
            prev_rss_mb = rss_mb
            # Fire the wake probe once when we cross 5GB. pm2 force-restarts
            # at 8GB so this gives ~3GB headroom for a live investigation.
            if not probe_fired and probe_url and rss_mb >= 5000:
                probe_fired = True
                try:
                    async with httpx.AsyncClient(timeout=5.0) as _hx:
                        _r = await _hx.post(probe_url)
                    logger.warning(
                        "RSS_WATCH: probe fired at %d MB (status=%d)",
                        rss_mb, _r.status_code,
                    )
                except Exception:
                    logger.exception("RSS_WATCH: probe fire failed (non-fatal)")

    rss_watch_task = asyncio.create_task(_rss_watch_loop())

    # Start session-viewing time-tracking loop
    from view_tracking import run_tick_loop as _view_tick
    view_track_task = asyncio.create_task(_view_tick())

    # Daily heartbeat for long-running orchestrators that never restart.
    # The Worker dedupes per-day for Discord, so exact alignment isn't needed.
    async def _daily_heartbeat_loop():
        import telemetry as _telemetry
        while True:
            await asyncio.sleep(86400)
            try:
                _telemetry.record_heartbeat()
            except Exception:
                logger.debug("Scheduled heartbeat failed (non-fatal)", exc_info=True)

    daily_heartbeat_task = asyncio.create_task(_daily_heartbeat_loop())

    # CC-session reconcile disabled — the live writer in sync_engine and
    # agent_dispatcher covers normal operation. The reconcile sweep scanned
    # 131K JSONL files every 30 minutes (700s each) for ~2 inserts per run,
    # and its single-transaction design held the SQLite write lock for the
    # entire duration, blocking all other writers. The code is kept in
    # cc_session_reconcile.py for manual CLI use (`python reconcile.py`).
    cc_session_reconcile_task = None

    yield

    # Shutdown
    for task in (agent_dispatch_task, backup_task, dropbox_task, session_cache_task, ws_prune_task, rss_watch_task, view_track_task, daily_heartbeat_task, cc_session_reconcile_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Background task raised during shutdown")
    if agent_dispatch_task:
        agent_dispatcher.stop()
    logger.info("Xylocopa shutting down...")


def _check_frontend_dist_staleness():
    # If any frontend/src file is newer than dist/index.html, someone committed
    # but didn't rebuild — old build is still being served. This warning makes
    # that gap visible instead of silently shipping stale JS to every browser.
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    src = root / "frontend" / "src"
    dist_index = root / "frontend" / "dist" / "index.html"
    if not src.is_dir() or not dist_index.is_file():
        return
    try:
        src_mtime = max(p.stat().st_mtime for p in src.rglob("*") if p.is_file())
    except ValueError:
        return
    dist_mtime = dist_index.stat().st_mtime
    delta = src_mtime - dist_mtime
    if delta > 5:
        mins = int(delta // 60)
        logger.warning(
            "DIST STALE: frontend/src is %ds (%dm) newer than frontend/dist/index.html "
            "— rebuild with `cd frontend && npx vite build` or POST /api/system/restart",
            int(delta), mins,
        )


def _migrate_legacy_user_dirs():
    """Rename legacy ~/.agenthive → ~/.xylocopa on startup.

    Only runs if the new dir does not already exist. Safe to call repeatedly.
    """
    home = os.path.expanduser("~")
    old = os.path.join(home, ".agenthive")
    new = os.path.join(home, ".xylocopa")
    if os.path.isdir(old) and not os.path.exists(new):
        os.rename(old, new)
        logger.info("Migrated legacy %s → %s", old, new)


def _migrate_legacy_git_identity(gm) -> int:
    """Rewrite the legacy ``AgentHive`` git identity in every managed repo.

    Iterates all projects known to the DB (registry + filesystem-scanned),
    including archived ones — an archived project can be un-archived later
    and its worktrees share the parent's config. Only the exact legacy values
    are rewritten (see ``git_manager.migrate_legacy_identity``); user-set
    identities are never touched. Returns the number of repos changed.
    """
    db = SessionLocal()
    try:
        paths = [p for (p,) in db.query(Project.path).all() if p]
    finally:
        db.close()
    changed = 0
    for path in paths:
        if not os.path.isdir(path):
            continue
        try:
            if gm.migrate_legacy_identity(path):
                changed += 1
        except Exception:
            logger.debug("git identity migration skipped for %s", path, exc_info=True)
    if changed:
        logger.info("Migrated legacy git identity in %d repo(s)", changed)
    return changed


# ---- App creation ----

app = FastAPI(
    title="Xylocopa",
    description="Multi-instance Claude Code orchestration system",
    version="0.2.0",
    lifespan=lifespan,
)

# Responses were never compressed: the agent list (~660 KB), project folders
# (~97 KB) and every JSON payload went over the wire raw. Starlette gzips
# non-streaming responses above minimum_size; there are no SSE endpoints.
# Added before CORS so CORS stays the outermost layer.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    # "null" is the literal Origin sent by sandboxed preview iframes (opaque
    # origin) — needed so their XHR/fetch preflights (e.g. TensorBoard's
    # custom request headers) pass. Safe: allow_credentials is False and the
    # session JWT lives in localStorage, unreachable from an opaque origin.
    allow_origins=CORS_ORIGINS + ["null"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Middleware ----

_TIMING_SLOW_MS = float(os.environ.get("API_TIMING_SLOW_MS", "100"))


@app.middleware("http")
async def api_timing_logger(request: Request, call_next):
    """Log request duration for /api/* calls, flag slow ones at WARNING."""
    path = request.url.path
    if not path.startswith("/api/") or path.startswith("/api/hooks/"):
        return await call_next(request)
    t0 = time.perf_counter()
    response = await call_next(request)
    dur_ms = (time.perf_counter() - t0) * 1000.0
    level = logging.WARNING if dur_ms >= _TIMING_SLOW_MS else logging.INFO
    logger.log(
        level,
        "API_TIMING: %s %s status=%d dur=%.1fms",
        request.method, path, response.status_code, dur_ms,
    )
    return response


@app.middleware("http")
async def hook_request_logger(request: Request, call_next):
    """Log EVERY request to /api/hooks/* for debugging."""
    if request.url.path.startswith("/api/hooks/"):
        agent_id = request.headers.get("X-Agent-Id", "<none>")
        hook_name = request.url.path.split("/api/hooks/")[-1]
        # Body size from Content-Length so we don't have to read the stream
        # (reading would interfere with route body parsing). Internal log,
        # INFO level — not exposed to user.
        _cl = request.headers.get("content-length")
        try:
            _body_kb = (int(_cl) / 1024) if _cl else 0
        except (TypeError, ValueError):
            _body_kb = 0
        logger.info(
            "HOOK_HTTP_IN: %s agent=%s method=%s body=%.1fKB",
            hook_name, agent_id[:12] if agent_id != "<none>" else "<none>",
            request.method, _body_kb,
        )
        # Loud warning if a hook body is unusually large (>1MB).
        # Recent OOM patterns suggested some hooks were sending full
        # transcripts; this surfaces that immediately.
        if _body_kb > 1024:
            logger.warning(
                "HOOK_BIG_BODY: %s agent=%s body=%.1fMB — check for accidentally-large hook payload",
                hook_name, agent_id[:12] if agent_id != "<none>" else "<none>",
                _body_kb / 1024,
            )
    response = await call_next(request)
    if request.url.path.startswith("/api/hooks/") and response.status_code >= 400:
        agent_id = request.headers.get("X-Agent-Id", "<none>")
        hook_name = request.url.path.split("/api/hooks/")[-1]
        logger.error(
            "HOOK_HTTP_ERR: %s agent=%s status=%d",
            hook_name, agent_id[:12] if agent_id != "<none>" else "<none>", response.status_code,
        )
    return response


_AUTH_EXEMPT_PREFIXES = ("/api/auth/", "/api/health", "/api/cert", "/api/webclip", "/api/hooks/", "/api/debug/auth-diag", "/api/debug/clear-cache", "/api/debug/mem-introspect", "/api/push/ack", "/api/probe-trigger/", "/api/preview/t/", "/api/preview/p/", "/api/dropbox/callback")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Reject unauthenticated requests to protected endpoints."""
    # Allow DISABLE_AUTH=1 for development/testing
    if os.environ.get("DISABLE_AUTH", "").strip() in ("1", "true", "yes"):
        return await call_next(request)

    path = request.url.path

    # Skip auth for exempt paths and non-API static assets
    if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)

    # Check for password — if none set, allow all requests (first-time setup)
    db = SessionLocal()
    try:
        pw_hash = get_password_hash(db)
        if pw_hash is None:
            return await call_next(request)

        # Verify bearer token (header) or query param (for <img src="..."> etc.)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = request.query_params.get("token", "")

        if not token:
            # Skip noisy debug endpoints to keep logs clean
            if path != "/api/debug/frontend-state":
                logger.info("AUTH_REJECT no_token: %s %s", request.method, path)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)

        jwt_secret = get_jwt_secret(db)
        if not verify_token(token, jwt_secret):
            logger.info("AUTH_REJECT bad_token: %s %s (token=%s…)", request.method, path, token[:16])
            return JSONResponse({"detail": "Token expired or invalid"}, status_code=401)
    finally:
        db.close()

    return await call_next(request)


# ---- Voice and WebSocket ----

from voice import router as voice_router
app.include_router(voice_router)

from websocket import websocket_endpoint
app.websocket("/ws/status")(websocket_endpoint)



# ---- Include all routers ----

from routers.auth import router as auth_router
from routers.system import router as system_router
from routers.dropbox import router as dropbox_router
from routers.projects import router as projects_router
from routers.tasks import router as tasks_router
from routers.hooks import router as hooks_router
from routers.agents import router as agents_router
from routers.git import router as git_router
from routers.files import router as files_router
from routers.preview import router as preview_router
from routers.push import router as push_router
from routers.workers import router as workers_router
from routers.logs import router as logs_router
from routers.skills import router as skills_router
from routers.stats import router as stats_router
from routers.bookmarks import router as bookmarks_router
from routers.probes import router as probes_router
from routers.terminal import router as terminal_router
from routers.attention import router as attention_router

app.include_router(auth_router)
app.include_router(system_router)
app.include_router(dropbox_router)
app.include_router(projects_router)
app.include_router(tasks_router)
app.include_router(hooks_router)
app.include_router(agents_router)
app.include_router(git_router)
app.include_router(files_router)
app.include_router(preview_router)
app.include_router(push_router)
app.include_router(workers_router)
app.include_router(logs_router)
app.include_router(skills_router)
app.include_router(stats_router)
app.include_router(bookmarks_router)
app.include_router(probes_router)
app.include_router(terminal_router)
app.include_router(attention_router)
