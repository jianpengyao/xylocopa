"""Project + FK + session reconcile / orphan scanner.

Scopes (all dry-run by default; destructive actions gated on ``--apply``):

  Project layer
    - ``projects.path`` rows whose filesystem path no longer exists (Orphan)
    - Filesystem dirs under ``PROJECTS_DIR`` with no DB row (Unregistered)
    - Dead symlinks inside ``PROJECTS_DIR``
    - registry.yaml ↔ DB drift (yaml names not in DB, or DB actives not in yaml)

  FK orphans
    - ``agents.project`` → missing Project
    - ``tasks.project_name`` → missing Project
    - ``messages.agent_id`` → missing Agent
    - ``starred_sessions.project`` → missing Project
    - ``asset_refs.project`` / ``asset_marks.project`` → missing Project
    - ``agents.parent_id`` dangling (subagents whose parent row was deleted)

  Session layer
    - ``starred_sessions.session_id`` → missing JSONL under ``~/.claude/projects/``

  Residue (report only, never modified by --apply)
    - Stale managed tmux sessions (``xy-*``/``ah-*``) with no matching agent prefix
    - ``.trash/`` entries older than N days (default 30)

This script DELEGATES to ``orphan_cleanup.scan_orphans`` / ``scan_stale_agents``
for JSONL + output-log + stale-agent layers it already covers — don't duplicate.

Usage:
    python -m reconcile                # scan, print report, exit
    python -m reconcile --apply        # apply fixes (deletes FK orphans,
                                       # deletes project-row orphans, deletes
                                       # stale starred_sessions rows)
    python -m reconcile --json         # machine-readable report to stdout
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

import yaml
from sqlalchemy import func, text
from sqlalchemy.orm import Session as SASession

# Make this file runnable both as a module AND as a script.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CLAUDE_HOME, PROJECT_CONFIGS_PATH, PROJECTS_DIR
from database import SessionLocal, engine
from models import Agent, AssetMark, AssetRef, Message, Project, StarredSession, Task
from orphan_cleanup import scan_orphans, scan_stale_agents
from route_helpers import is_managed_tmux_session, tmux_session_to_agent_prefix

logger = logging.getLogger("orchestrator.reconcile")

TRASH_STALE_DAYS = 30


# ---------------------------------------------------------------------------
# Project layer
# ---------------------------------------------------------------------------

def _scan_projects(db: SASession) -> dict[str, Any]:
    """Classify DB rows and filesystem dirs against each other."""
    projects_dir = PROJECTS_DIR or ""
    projects_dir_abs = os.path.abspath(projects_dir).rstrip("/") + "/" if projects_dir else ""

    # Filesystem snapshot
    fs_entries: list[str] = []
    dead_symlinks: list[str] = []
    if projects_dir and os.path.isdir(projects_dir):
        for entry in os.listdir(projects_dir):
            if entry.startswith("."):
                continue
            full = os.path.join(projects_dir, entry)
            if os.path.islink(full) and not os.path.exists(full):
                dead_symlinks.append(full)
            elif os.path.isdir(full):
                fs_entries.append(entry)

    # DB snapshot — exclude reserved system projects (`.` prefix). They live
    # under PROJECTS_DIR but the disk scan above already skips dot-prefixed
    # entries, so reconciling them would (a) compare to nothing on disk and
    # (b) always classify them as orphans. The `.xylo-internal` placeholder
    # is bootstrapped by ensure_internal_project() and managed separately.
    db_rows = [p for p in db.query(Project).all() if not p.name.startswith(".")]
    db_names = {p.name for p in db_rows}

    orphan_rows: list[dict[str, Any]] = []      # DB row, path missing
    external_rows: list[dict[str, Any]] = []    # DB row, path outside PROJECTS_DIR (info only)
    for p in db_rows:
        if not p.path:
            continue
        path_abs = os.path.abspath(p.path)
        inside = bool(projects_dir_abs) and path_abs.startswith(projects_dir_abs)
        if not os.path.isdir(p.path):
            orphan_rows.append({"name": p.name, "path": p.path, "archived": bool(p.archived)})
        elif not inside:
            external_rows.append({"name": p.name, "path": p.path, "archived": bool(p.archived)})

    unregistered = sorted(set(fs_entries) - db_names)

    # registry.yaml drift
    registry_path = os.path.join(PROJECT_CONFIGS_PATH, "registry.yaml") if PROJECT_CONFIGS_PATH else ""
    yaml_names: set[str] = set()
    if registry_path and os.path.isfile(registry_path):
        try:
            with open(registry_path) as f:
                data = yaml.safe_load(f) or {}
            for p in data.get("projects") or []:
                n = p.get("name")
                if n:
                    yaml_names.add(n)
        except (OSError, yaml.YAMLError) as e:
            logger.warning("Failed to read %s: %s", registry_path, e)

    yaml_missing_from_db = sorted(yaml_names - db_names)
    db_active_missing_from_yaml = sorted(
        {p.name for p in db_rows if not p.archived} - yaml_names
    )

    return {
        "orphan_rows": orphan_rows,
        "external_rows": external_rows,
        "unregistered": unregistered,
        "dead_symlinks": dead_symlinks,
        "yaml_missing_from_db": yaml_missing_from_db,
        "db_active_missing_from_yaml": db_active_missing_from_yaml,
    }


def _apply_project_layer(db: SASession, scan_result: dict[str, Any]) -> dict[str, int]:
    """Delete Project rows whose filesystem path is missing.

    FK-cascades are handled by ``_apply_fk_orphans`` in the same transaction,
    so call project-layer first, then FK-orphan pass.
    """
    deleted = 0
    for row in scan_result.get("orphan_rows", []):
        proj = db.get(Project, row["name"])
        if proj is None:
            continue
        db.delete(proj)
        deleted += 1
    return {"orphan_project_rows_deleted": deleted}


# ---------------------------------------------------------------------------
# FK orphans
# ---------------------------------------------------------------------------

def _scan_fk_orphans(db: SASession) -> dict[str, Any]:
    project_names = {n for (n,) in db.query(Project.name).all()}
    agent_ids = {a for (a,) in db.query(Agent.id).all()}

    # agents.project → missing project
    bad_agents = [
        {"id": a.id, "project": a.project, "name": a.name}
        for a in db.query(Agent).filter(Agent.project.isnot(None)).all()
        if a.project and a.project not in project_names
    ]

    # tasks.project_name → missing project
    bad_tasks = [
        {"id": t.id, "project_name": t.project_name, "title": t.title}
        for t in db.query(Task).filter(Task.project_name.isnot(None)).all()
        if t.project_name and t.project_name not in project_names
    ]

    # messages.agent_id → missing agent
    bad_message_rows = (
        db.query(Message.id, Message.agent_id)
        .filter(Message.agent_id.isnot(None))
        .all()
    )
    bad_messages = [
        {"id": mid, "agent_id": aid}
        for mid, aid in bad_message_rows
        if aid not in agent_ids
    ]

    # starred_sessions.project → missing project (NULL project is allowed)
    bad_stars = [
        {"session_id": s.session_id, "project": s.project}
        for s in db.query(StarredSession).filter(StarredSession.project.isnot(None)).all()
        if s.project and s.project not in project_names
    ]

    # asset_refs / asset_marks.project → missing project (grouped: the index
    # can hold thousands of refs per project). The tables only exist once the
    # server has run init_db with them, so skip them when absent — probe
    # sqlite_master rather than catch the error: a rollback here would undo
    # apply_all's already-flushed project-row deletes.
    def _bad_asset_projects(model):
        if db.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"),
            {"n": model.__tablename__},
        ).first() is None:
            return []
        rows = db.query(model.project, func.count()).group_by(model.project).all()
        return [{"project": p, "count": n} for p, n in rows if p not in project_names]

    bad_asset_refs = _bad_asset_projects(AssetRef)
    bad_asset_marks = _bad_asset_projects(AssetMark)

    # agents.parent_id dangling (subagents whose parent was deleted)
    dangling_subagents = [
        {"id": a.id, "parent_id": a.parent_id, "name": a.name}
        for a in db.query(Agent).filter(
            Agent.parent_id.isnot(None),
            Agent.is_subagent == True,  # noqa: E712
        ).all()
        if a.parent_id and a.parent_id not in agent_ids
    ]

    return {
        "agents_bad_project": bad_agents,
        "tasks_bad_project": bad_tasks,
        "messages_bad_agent": bad_messages,
        "starred_bad_project": bad_stars,
        "asset_refs_bad_project": bad_asset_refs,
        "asset_marks_bad_project": bad_asset_marks,
        "dangling_subagents": dangling_subagents,
    }


def _apply_fk_orphans(db: SASession, scan_result: dict[str, Any]) -> dict[str, int]:
    counts = {
        "agents_bad_project_deleted": 0,
        "tasks_bad_project_deleted": 0,
        "messages_bad_agent_deleted": 0,
        "starred_bad_project_deleted": 0,
        "asset_refs_bad_project_deleted": 0,
        "asset_marks_bad_project_deleted": 0,
        "dangling_subagents_deleted": 0,
    }
    if scan_result.get("messages_bad_agent"):
        ids = [m["id"] for m in scan_result["messages_bad_agent"]]
        counts["messages_bad_agent_deleted"] = db.query(Message).filter(
            Message.id.in_(ids)
        ).delete(synchronize_session=False)

    if scan_result.get("agents_bad_project"):
        ids = [a["id"] for a in scan_result["agents_bad_project"]]
        counts["agents_bad_project_deleted"] = db.query(Agent).filter(
            Agent.id.in_(ids)
        ).delete(synchronize_session=False)

    if scan_result.get("tasks_bad_project"):
        ids = [t["id"] for t in scan_result["tasks_bad_project"]]
        counts["tasks_bad_project_deleted"] = db.query(Task).filter(
            Task.id.in_(ids)
        ).delete(synchronize_session=False)

    if scan_result.get("starred_bad_project"):
        sids = [s["session_id"] for s in scan_result["starred_bad_project"]]
        counts["starred_bad_project_deleted"] = db.query(StarredSession).filter(
            StarredSession.session_id.in_(sids)
        ).delete(synchronize_session=False)

    if scan_result.get("asset_refs_bad_project"):
        names = [r["project"] for r in scan_result["asset_refs_bad_project"]]
        counts["asset_refs_bad_project_deleted"] = db.query(AssetRef).filter(
            AssetRef.project.in_(names)
        ).delete(synchronize_session=False)

    if scan_result.get("asset_marks_bad_project"):
        names = [r["project"] for r in scan_result["asset_marks_bad_project"]]
        counts["asset_marks_bad_project_deleted"] = db.query(AssetMark).filter(
            AssetMark.project.in_(names)
        ).delete(synchronize_session=False)

    if scan_result.get("dangling_subagents"):
        ids = [s["id"] for s in scan_result["dangling_subagents"]]
        counts["dangling_subagents_deleted"] = db.query(Agent).filter(
            Agent.id.in_(ids)
        ).delete(synchronize_session=False)

    return counts


# ---------------------------------------------------------------------------
# Session layer (starred sessions pointing at missing JSONL)
# ---------------------------------------------------------------------------

def _all_jsonl_session_ids() -> set[str]:
    """All session IDs present as ``<id>.jsonl`` under ``~/.claude/projects/``."""
    projects_dir = os.path.join(CLAUDE_HOME, "projects")
    sids: set[str] = set()
    if not os.path.isdir(projects_dir):
        return sids
    for _, _, filenames in os.walk(projects_dir):
        for fname in filenames:
            if fname.endswith(".jsonl"):
                sids.add(fname[:-6])
    return sids


def _scan_session_layer(db: SASession) -> dict[str, Any]:
    jsonl_sids = _all_jsonl_session_ids()
    starred = db.query(StarredSession).all()
    starred_missing = [
        {"session_id": s.session_id, "project": s.project}
        for s in starred
        if s.session_id not in jsonl_sids
    ]
    return {
        "starred_missing_jsonl": starred_missing,
        "jsonl_count": len(jsonl_sids),
    }


# ---------------------------------------------------------------------------
# Residue report (informational only — never deleted)
# ---------------------------------------------------------------------------

def _active_tmux() -> list[str]:
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        return [s.strip() for s in result.stdout.splitlines() if s.strip()]
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []


def _scan_residue(db: SASession, trash_days: int = TRASH_STALE_DAYS) -> dict[str, Any]:
    # Agent ID prefixes that are still in the DB — if a managed tmux session's
    # prefix is NOT in this set, the session is a leak (usually test fixtures).
    live_prefixes = {row[0][:8] for row in db.query(Agent.id).all()}
    stale_tmux: list[str] = []
    for name in _active_tmux():
        if not is_managed_tmux_session(name):
            continue
        prefix = tmux_session_to_agent_prefix(name)
        if prefix and prefix not in live_prefixes:
            stale_tmux.append(name)

    # .trash entries older than N days
    trash_dir = os.path.join(PROJECTS_DIR or "", ".trash") if PROJECTS_DIR else ""
    stale_trash: list[dict[str, Any]] = []
    cutoff = time.time() - trash_days * 86400
    if trash_dir and os.path.isdir(trash_dir):
        for entry in os.listdir(trash_dir):
            full = os.path.join(trash_dir, entry)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            if mtime < cutoff:
                stale_trash.append({
                    "name": entry,
                    "age_days": round((time.time() - mtime) / 86400, 1),
                })

    return {
        "stale_tmux": stale_tmux,
        "stale_trash": stale_trash,
        "trash_stale_threshold_days": trash_days,
    }


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def _scan_cc_sessions(db: SASession) -> dict[str, Any]:
    """Sweep on-disk JSONLs into the ``cc_sessions`` table.

    Unlike the rest of the reconcile pipeline (which is dry-run by
    default), this scan is the only writer for ``cc_sessions`` and runs
    immediately — there's no destructive behaviour to gate. Idempotent:
    re-running without new disk state is a no-op.
    """
    try:
        from cc_session_reconcile import reconcile_all
        return reconcile_all(db=db)
    except Exception as e:
        logger.exception("cc_session reconcile sweep failed: %s", e)
        return {
            "agents": 0, "discovered": 0, "inserted": 0,
            "updated": 0, "skipped": 0, "error": str(e),
        }


def scan_all(db: SASession) -> dict[str, Any]:
    return {
        "projects": _scan_projects(db),
        "fk_orphans": _scan_fk_orphans(db),
        "sessions": _scan_session_layer(db),
        "files": scan_orphans(),
        "stale_agents": scan_stale_agents(db),
        "residue": _scan_residue(db),
        "cc_sessions": _scan_cc_sessions(db),
    }


def apply_all(db: SASession, result: dict[str, Any]) -> dict[str, Any]:
    """Apply reversible cleanup — project rows + FK orphans + starred cleanup.

    Does NOT apply: file deletes (use ``orphan_cleanup.delete_orphans``),
    stale-agent deletes (use ``orphan_cleanup.delete_stale_agents``),
    residue (report only), registry.yaml (manual).
    """
    applied: dict[str, Any] = {}

    # 1. Project rows first. This can CREATE new FK orphans (agents/tasks that
    # referenced the just-deleted projects), so we re-scan FK orphans after
    # this step rather than trusting the pre-apply snapshot.
    applied.update(_apply_project_layer(db, result["projects"]))
    db.flush()

    # 2. FK orphans (re-scanned post-project-delete)
    fk_rescan = _scan_fk_orphans(db)
    applied.update(_apply_fk_orphans(db, fk_rescan))

    # 3. Starred sessions pointing at missing JSONL
    missing = result["sessions"].get("starred_missing_jsonl", [])
    if missing:
        sids = [m["session_id"] for m in missing]
        applied["starred_missing_jsonl_deleted"] = db.query(StarredSession).filter(
            StarredSession.session_id.in_(sids)
        ).delete(synchronize_session=False)
    else:
        applied["starred_missing_jsonl_deleted"] = 0

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("reconcile apply: commit failed")
        return {"error": "commit failed"}

    return applied


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def _fmt_list(label: str, items: list[Any], fmt=lambda x: str(x)) -> str:
    if not items:
        return f"  {label}: 0\n"
    lines = [f"  {label}: {len(items)}"]
    for x in items[:10]:
        lines.append(f"    - {fmt(x)}")
    if len(items) > 10:
        lines.append(f"    ... and {len(items) - 10} more")
    return "\n".join(lines) + "\n"


def render_report(result: dict[str, Any]) -> str:
    buf = []
    buf.append("=== Project layer ===\n")
    P = result["projects"]
    buf.append(_fmt_list("Orphan DB rows (path missing)", P["orphan_rows"],
                          lambda r: f"{r['name']}  ({r['path']})"))
    buf.append(_fmt_list("External DB rows (outside PROJECTS_DIR)", P["external_rows"],
                          lambda r: f"{r['name']}  ({r['path']})"))
    buf.append(_fmt_list("Unregistered fs dirs", P["unregistered"]))
    buf.append(_fmt_list("Dead symlinks", P["dead_symlinks"]))
    buf.append(_fmt_list("yaml entries missing from DB", P["yaml_missing_from_db"]))
    buf.append(_fmt_list("DB active rows missing from yaml", P["db_active_missing_from_yaml"]))

    buf.append("\n=== FK orphans ===\n")
    F = result["fk_orphans"]
    buf.append(_fmt_list("agents.project → missing", F["agents_bad_project"],
                          lambda a: f"{a['id'][:8]}  project={a['project']}"))
    buf.append(_fmt_list("tasks.project_name → missing", F["tasks_bad_project"],
                          lambda t: f"{t['id'][:8]}  project_name={t['project_name']}"))
    buf.append(_fmt_list("messages.agent_id → missing", F["messages_bad_agent"],
                          lambda m: f"msg={m['id'][:8]}  agent_id={m['agent_id'][:8]}"))
    buf.append(_fmt_list("starred_sessions.project → missing", F["starred_bad_project"],
                          lambda s: f"sid={s['session_id'][:8]}  project={s['project']}"))
    buf.append(_fmt_list("asset_refs.project → missing", F.get("asset_refs_bad_project", []),
                          lambda r: f"project={r['project']}  rows={r['count']}"))
    buf.append(_fmt_list("asset_marks.project → missing", F.get("asset_marks_bad_project", []),
                          lambda r: f"project={r['project']}  rows={r['count']}"))
    buf.append(_fmt_list("Dangling subagents (parent missing)", F["dangling_subagents"],
                          lambda a: f"{a['id'][:8]}  parent={a['parent_id'][:8]}"))

    buf.append("\n=== Session layer ===\n")
    S = result["sessions"]
    buf.append(f"  JSONL session files present: {S['jsonl_count']}\n")
    buf.append(_fmt_list("Starred → missing JSONL", S["starred_missing_jsonl"],
                          lambda s: f"sid={s['session_id'][:8]}  project={s['project']}"))

    buf.append("\n=== Files (delegated to orphan_cleanup) ===\n")
    FF = result["files"]
    buf.append(f"  Orphan session JSONLs: {FF['orphan_session_count']}  ({FF['orphan_session_bytes']/1024:.1f} KB)\n")
    buf.append(f"  Orphan output logs:    {FF['orphan_log_count']}  ({FF['orphan_log_bytes']/1024:.1f} KB)\n")
    buf.append(f"  Empty session dirs:    {FF['empty_dir_count']}\n")

    buf.append("\n=== Stale agents (delegated to orphan_cleanup) ===\n")
    SA = result["stale_agents"]
    buf.append(f"  Eligible parent agents: {SA['eligible_count']}  (cascades to {SA['total_subagents']} subagents)\n")
    buf.append(f"  Orphan subagents:       {SA['orphan_subagent_count']}\n")
    buf.append(f"  Skipped (starred):      {SA['skipped_starred']}\n")
    buf.append(f"  Skipped (active tmux):  {SA['skipped_tmux']}\n")

    buf.append("\n=== CC sessions (JSONL → cc_sessions reconcile) ===\n")
    CC = result.get("cc_sessions") or {}
    buf.append(f"  Agents swept:    {CC.get('agents', 0)}\n")
    buf.append(f"  JSONLs scanned:  {CC.get('discovered', 0)}\n")
    buf.append(f"  Rows inserted:   {CC.get('inserted', 0)}\n")
    buf.append(f"  Rows updated:    {CC.get('updated', 0)}\n")
    buf.append(f"  Rows skipped:    {CC.get('skipped', 0)}\n")
    if CC.get("error"):
        buf.append(f"  Error: {CC['error']}\n")

    buf.append("\n=== Residue (report only) ===\n")
    R = result["residue"]
    buf.append(_fmt_list("Stale managed tmux sessions", R["stale_tmux"]))
    buf.append(_fmt_list(
        f"Trash entries older than {R['trash_stale_threshold_days']}d",
        R["stale_trash"],
        lambda t: f"{t['name']}  ({t['age_days']}d)"
    ))
    return "".join(buf)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="Delete orphan project rows, FK orphans, and "
                             "starred_sessions with missing JSONL. Files and "
                             "stale agents are NOT touched — see orphan_cleanup.")
    parser.add_argument("--json", action="store_true",
                        help="Emit scan result as JSON (no text report).")
    parser.add_argument("--trash-days", type=int, default=TRASH_STALE_DAYS,
                        help="Report .trash entries older than N days (default 30).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db = SessionLocal()
    try:
        result = scan_all(db)
        # Re-scan residue with overridden threshold if differs
        if args.trash_days != TRASH_STALE_DAYS:
            result["residue"] = _scan_residue(db, trash_days=args.trash_days)

        if args.json:
            print(json.dumps(result, default=str, indent=2))
        else:
            print(render_report(result))

        if args.apply:
            print("\n=== Applying ===")
            applied = apply_all(db, result)
            for k, v in applied.items():
                print(f"  {k}: {v}")
        else:
            print("\n(dry run — re-run with --apply to delete orphan rows)")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
