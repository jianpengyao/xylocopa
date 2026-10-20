"""Scan and delete orphaned session JSONL files, output logs, and stale agents."""

import glob
import logging
import os
import shutil
import tempfile
import subprocess
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession

logger = logging.getLogger("orchestrator.orphan_cleanup")

from config import CLAUDE_HOME
from database import engine
from session_cache import CACHE_DIR

# Default retention: agents older than this are eligible for cleanup
STALE_AGENT_DAYS = 30


def scan_orphans() -> dict:
    """Walk ~/.claude/projects/ and tempdir/claude-output-*.log, return orphan
    file list with sizes.  Does NOT delete anything."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT session_id FROM agents WHERE session_id IS NOT NULL")
        ).fetchall()
        live_sessions = {r[0] for r in rows}

        rows = conn.execute(text("SELECT id FROM messages")).fetchall()
        live_msg_ids = {r[0] for r in rows}

        # Orphan = DB row whose folder is gone, regardless of archived state.
        # The Projects page (active or inactive view) only shows rows whose
        # folder exists on disk, so a missing-folder row is invisible everywhere.
        rows = conn.execute(text("SELECT name, path FROM projects")).fetchall()
        orphan_projects = [
            {"name": r[0], "path": r[1]} for r in rows if not os.path.isdir(r[1])
        ]

    # 1. Session JSONL files
    projects_dir = os.path.join(CLAUDE_HOME, "projects")
    orphan_sessions = []
    total_sessions = 0
    if os.path.isdir(projects_dir):
        for dirpath, _, filenames in os.walk(projects_dir):
            for fname in filenames:
                if not fname.endswith(".jsonl"):
                    continue
                total_sessions += 1
                session_id = fname[:-6]
                if session_id not in live_sessions:
                    fp = os.path.join(dirpath, fname)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        sz = 0
                    orphan_sessions.append({"path": fp, "size": sz})

    # 2. Output log files in system temp dir
    orphan_logs = []
    total_logs = 0
    for log_path in glob.glob(os.path.join(tempfile.gettempdir(), "claude-output-*.log")):
        total_logs += 1
        basename = os.path.basename(log_path)
        msg_id = basename.replace("claude-output-", "").replace(".log", "")
        if msg_id not in live_msg_ids:
            try:
                sz = os.path.getsize(log_path)
            except OSError:
                sz = 0
            orphan_logs.append({"path": log_path, "size": sz})

    # 3. Empty directories
    empty_dirs = []
    if os.path.isdir(projects_dir):
        for entry in os.listdir(projects_dir):
            d = os.path.join(projects_dir, entry)
            if os.path.isdir(d) and not os.listdir(d):
                empty_dirs.append(d)

    session_bytes = sum(f["size"] for f in orphan_sessions)
    log_bytes = sum(f["size"] for f in orphan_logs)

    return {
        "orphan_sessions": orphan_sessions,
        "orphan_logs": orphan_logs,
        "empty_dirs": empty_dirs,
        "orphan_projects": orphan_projects,
        "orphan_session_count": len(orphan_sessions),
        "orphan_session_bytes": session_bytes,
        "orphan_log_count": len(orphan_logs),
        "orphan_log_bytes": log_bytes,
        "empty_dir_count": len(empty_dirs),
        "orphan_project_count": len(orphan_projects),
        "total_files": len(orphan_sessions) + len(orphan_logs),
        "total_bytes": session_bytes + log_bytes,
    }


def delete_orphans(scan_result: dict) -> dict:
    """Delete files from a scan result.  Returns counts + freed bytes."""
    deleted_sessions = 0
    deleted_logs = 0
    deleted_dirs = 0
    freed = 0

    for f in scan_result.get("orphan_sessions", []):
        try:
            os.remove(f["path"])
            freed += f["size"]
            deleted_sessions += 1
        except OSError as e:
            logger.warning("Failed to remove orphan session %s: %s", f["path"], e)

    for f in scan_result.get("orphan_logs", []):
        try:
            os.remove(f["path"])
            freed += f["size"]
            deleted_logs += 1
        except OSError as e:
            logger.warning("Failed to remove orphan log %s: %s", f["path"], e)

    # Evict orphan sessions from session cache (mirrors ~/.claude/projects/ layout)
    evicted_cache = 0
    orphan_sids = set()
    for f in scan_result.get("orphan_sessions", []):
        sid = os.path.basename(f["path"]).replace(".jsonl", "")
        orphan_sids.add(sid)
    if orphan_sids and os.path.isdir(CACHE_DIR):
        for dirpath, _, filenames in os.walk(CACHE_DIR):
            for fname in filenames:
                if not fname.endswith(".jsonl"):
                    continue
                sid = fname[:-6]
                if sid in orphan_sids:
                    cache_path = os.path.join(dirpath, fname)
                    try:
                        os.remove(cache_path)
                        evicted_cache += 1
                    except OSError as e:
                        logger.warning("Failed to evict cached session %s: %s", cache_path, e)
        # Also remove orphan session subdirectories (chunked cache)
        for dirpath, dirnames, _ in os.walk(CACHE_DIR):
            for dname in dirnames:
                if dname in orphan_sids:
                    subdir = os.path.join(dirpath, dname)
                    try:
                        shutil.rmtree(subdir)
                        evicted_cache += 1
                    except OSError as e:
                        logger.warning("Failed to evict cached session dir %s: %s", subdir, e)
    if evicted_cache:
        logger.info("Evicted %d orphan entries from session cache", evicted_cache)

    for d in scan_result.get("empty_dirs", []):
        try:
            os.rmdir(d)
            deleted_dirs += 1
        except OSError as e:
            logger.warning("Failed to remove empty dir %s: %s", d, e)

    # Hard-delete project rows whose folder is gone.  Models declare
    # ON DELETE CASCADE on the FKs, but historical SQLite tables were created
    # before those constraints landed and SQLite doesn't backfill them — so
    # cascade is unreliable in practice.  Wipe related rows explicitly.
    deleted_projects = 0
    orphan_projects = scan_result.get("orphan_projects", [])
    if orphan_projects:
        with engine.begin() as conn:
            for p in orphan_projects:
                name = p["name"]
                try:
                    # messages → cascade through agents
                    conn.execute(text(
                        "DELETE FROM messages WHERE agent_id IN "
                        "(SELECT id FROM agents WHERE project = :n)"
                    ), {"n": name})
                    conn.execute(text("DELETE FROM agents WHERE project = :n"), {"n": name})
                    conn.execute(text(
                        "DELETE FROM tasks WHERE project_name = :n OR project = :n"
                    ), {"n": name})
                    conn.execute(text("DELETE FROM starred_sessions WHERE project = :n"), {"n": name})
                    conn.execute(text("DELETE FROM progress_insights WHERE project = :n"), {"n": name})
                    conn.execute(text("DELETE FROM session_view_events WHERE project = :n"), {"n": name})
                    conn.execute(text("DELETE FROM asset_refs WHERE project = :n"), {"n": name})
                    conn.execute(text("DELETE FROM asset_marks WHERE project = :n"), {"n": name})
                    conn.execute(text("DELETE FROM projects WHERE name = :n"), {"n": name})
                    deleted_projects += 1
                except Exception as e:
                    logger.warning("Failed to hard-delete project %s: %s", name, e)

    return {
        "deleted_sessions": deleted_sessions,
        "deleted_logs": deleted_logs,
        "deleted_dirs": deleted_dirs,
        "deleted_projects": deleted_projects,
        "evicted_cache": evicted_cache,
        "freed_bytes": freed,
    }


# ---------------------------------------------------------------------------
# Stale agent cleanup — removes old stopped/error agents and their subagents
# ---------------------------------------------------------------------------

def _active_tmux_sessions() -> set[str]:
    """Return set of active orchestrator-managed tmux session names
    (new ``xy-`` and legacy ``ah-`` prefixes)."""
    from route_helpers import is_managed_tmux_session
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return {
                s.strip() for s in result.stdout.splitlines()
                if s.strip() and is_managed_tmux_session(s.strip())
            }
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
        logger.debug("Failed to list tmux sessions: %s", e)
    return set()


def scan_stale_agents(db: SASession, *, max_age_days: int = STALE_AGENT_DAYS) -> dict:
    """Find parent agents eligible for cleanup (stopped/error, older than max_age_days).

    Skips:
    - Agents with starred sessions
    - Agents with active tmux sessions
    - Subagents (they cascade with their parent)

    Returns scan result with agent IDs and subagent counts.
    """
    from models import Agent, AgentStatus, Message, StarredSession

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max_age_days)

    # Find starred session_ids and agent_ids
    starred_sids = {
        row[0] for row in db.query(StarredSession.session_id).all()
    }

    # Active tmux sessions → protected agent ID prefixes
    from route_helpers import tmux_session_to_agent_prefix
    active_tmux = _active_tmux_sessions()
    protected_prefixes = {
        prefix for s in active_tmux
        if (prefix := tmux_session_to_agent_prefix(s)) is not None
    }

    # Query stale parent agents (not subagents)
    candidates = (
        db.query(Agent)
        .filter(
            Agent.status.in_([AgentStatus.STOPPED, AgentStatus.ERROR]),
            Agent.is_subagent == False,  # noqa: E712
            Agent.created_at < cutoff,
        )
        .all()
    )

    eligible = []
    skipped_starred = 0
    skipped_tmux = 0

    for agent in candidates:
        # Skip starred agents
        if agent.session_id and agent.session_id in starred_sids:
            skipped_starred += 1
            continue
        if agent.id in starred_sids:
            skipped_starred += 1
            continue

        # Skip agents with active tmux sessions
        if agent.id[:8] in protected_prefixes:
            skipped_tmux += 1
            continue

        # Count subagents that will cascade
        sub_count = (
            db.query(Agent.id)
            .filter(Agent.parent_id == agent.id, Agent.is_subagent == True)  # noqa: E712
            .count()
        )
        eligible.append({
            "agent_id": agent.id,
            "name": agent.name,
            "project": agent.project,
            "status": agent.status.value,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "subagent_count": sub_count,
        })

    # Also find orphan subagents (parent deleted or missing)
    orphan_subs = (
        db.query(Agent)
        .filter(
            Agent.is_subagent == True,  # noqa: E712
            Agent.status.in_([AgentStatus.STOPPED, AgentStatus.ERROR]),
        )
        .all()
    )
    # A subagent is orphaned if its parent_id is None or points to a non-existent agent
    live_agent_ids = {row[0] for row in db.query(Agent.id).all()}
    orphan_sub_ids = []
    for sub in orphan_subs:
        if not sub.parent_id or sub.parent_id not in live_agent_ids:
            orphan_sub_ids.append(sub.id)

    total_subagents = sum(e["subagent_count"] for e in eligible)

    return {
        "eligible_agents": eligible,
        "orphan_subagent_ids": orphan_sub_ids,
        "eligible_count": len(eligible),
        "total_subagents": total_subagents,
        "orphan_subagent_count": len(orphan_sub_ids),
        "skipped_starred": skipped_starred,
        "skipped_tmux": skipped_tmux,
    }


def delete_stale_agents(db: SASession, scan_result: dict) -> dict:
    """Delete stale parent agents, cascading to their subagents and messages.

    Also cleans up orphan subagents whose parents no longer exist.
    """
    from models import Agent, Message, Task
    from session_cache import cleanup_source_session, evict_session

    deleted_agents = 0
    deleted_subagents = 0
    deleted_messages = 0
    cleaned_files = 0

    for entry in scan_result.get("eligible_agents", []):
        agent_id = entry["agent_id"]
        agent = db.get(Agent, agent_id)
        if not agent:
            continue

        # Collect all IDs: parent + subagents
        child_ids = [
            row[0] for row in db.query(Agent.id)
            .filter(Agent.parent_id == agent_id, Agent.is_subagent == True)  # noqa: E712
            .all()
        ]
        all_ids = [agent_id] + child_ids

        # Collect session info for file cleanup
        session_infos = []
        for a in db.query(Agent).filter(Agent.id.in_(all_ids)).all():
            if a.session_id:
                session_infos.append((a.session_id, a.project, a.worktree))

        # Delete messages
        msg_count = db.query(Message).filter(
            Message.agent_id.in_(all_ids)
        ).delete(synchronize_session=False)
        deleted_messages += msg_count

        # Unlink tasks
        db.query(Task).filter(Task.agent_id.in_(all_ids)).update(
            {Task.agent_id: None}, synchronize_session=False
        )

        # Delete subagents first, then parent
        sub_count = db.query(Agent).filter(
            Agent.parent_id == agent_id, Agent.is_subagent == True  # noqa: E712
        ).delete(synchronize_session=False)
        deleted_subagents += sub_count

        db.delete(agent)
        deleted_agents += 1

        # Clean session files
        for sid, proj_name, worktree in session_infos:
            from models import Project
            project = db.query(Project).filter(Project.name == proj_name).first()
            if project:
                if cleanup_source_session(sid, project.path, worktree):
                    cleaned_files += 1

    # Clean up orphan subagents
    orphan_ids = scan_result.get("orphan_subagent_ids", [])
    if orphan_ids:
        msg_count = db.query(Message).filter(
            Message.agent_id.in_(orphan_ids)
        ).delete(synchronize_session=False)
        deleted_messages += msg_count

        db.query(Task).filter(Task.agent_id.in_(orphan_ids)).update(
            {Task.agent_id: None}, synchronize_session=False
        )

        orphan_count = db.query(Agent).filter(
            Agent.id.in_(orphan_ids)
        ).delete(synchronize_session=False)
        deleted_subagents += orphan_count

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("Failed to commit stale agent cleanup", exc_info=True)
        return {"error": "commit failed"}

    logger.info(
        "Stale agent cleanup: %d agents, %d subagents, %d messages deleted, %d files cleaned",
        deleted_agents, deleted_subagents, deleted_messages, cleaned_files,
    )

    return {
        "deleted_agents": deleted_agents,
        "deleted_subagents": deleted_subagents,
        "deleted_messages": deleted_messages,
        "cleaned_files": cleaned_files,
    }
