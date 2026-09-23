#!/usr/bin/env python3
"""Repair duplicate user bubbles caused by Claude Code's <pasted_content> wrapper.

Claude Code 2.1.28x (first seen in session JSONL on 2026-09-19) wraps every
tmux paste in ``<pasted_content id="NNNN">…</pasted_content id="NNNN">``
before writing the user turn. Until the parser learned to unwrap it
(jsonl_parser.unwrap_pasted_content), every web/task message echoed back
wrapped, failed ContentMatcher, its SENT row was never promoted, and a
second ``source=cli`` row still carrying the wrapper was created — the user
saw each message twice.

Pass 1 — for every ``source=cli`` USER row whose content carries the wrapper:

  * compute the clean text exactly as the parser now does
    (unwrap + strip orchestrator preamble/postamble);
  * look for the un-promoted web / task / plan_continue row it duplicates
    (same agent, ``jsonl_uuid IS NULL``, ``delivered_at IS NULL``, matched
    with ContentMatcher — the same matcher the live sync path uses);
  * match found  → promote that row (copy jsonl_uuid / session_seq /
    delivered_at, status=COMPLETED) and delete the duplicate cli row;
  * no match     → keep the cli row but store the clean content.

Pass 2 — web rows still SENT whose cli twin is gone. A compact-time
full scan purges cli rows it believes orphaned (see sync_engine
sync_full_scan); for the affected agents that removed the duplicate but
left the web row un-promoted. For every agent with un-promoted rows
created since the CC change (2026-09-19) the session JSONL is parsed in
full (no byte cap) and each user turn that has no DB row is matched
against those rows with ContentMatcher — exactly what the live sync path
would have done — and the row is promoted with the turn's uuid/seq/time.

Afterwards the display file of every touched agent is rebuilt from the DB
(display_writer.rebuild_agent). The server never rebuilds display files on
startup, so this has to happen here — also for live agents. The rebuild
keeps pre-sent (queued) entries and takes the file lock; a message flushed
by the running server in the same instant can at worst appear as a
duplicate line, which the frontend de-duplicates by id.

Usage:
    .venv/bin/python tools/repair_pasted_content_dupes.py                    # dry run
    .venv/bin/python tools/repair_pasted_content_dupes.py --apply            # write
    .venv/bin/python tools/repair_pasted_content_dupes.py --apply --rebuild ID[,ID]
        # additionally rebuild these agents' display files (e.g. agents
        # repaired by an earlier run while the old server was still up)

Idempotent: a second run finds nothing to do.
"""
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "orchestrator"))
os.chdir(PROJECT_ROOT)

from sqlalchemy import or_  # noqa: E402

from content_matcher import ContentMatcher  # noqa: E402
from database import SessionLocal  # noqa: E402
from display_writer import rebuild_agent  # noqa: E402
from jsonl_parser import (  # noqa: E402
    parse_session_turns,
    strip_agent_preamble,
    unwrap_pasted_content,
)
from models import (  # noqa: E402
    Agent,
    Message,
    MessageRole,
    MessageStatus,
    Project,
)

APPLY = "--apply" in sys.argv[1:]
EXTRA_REBUILD: list[str] = []
if "--rebuild" in sys.argv[1:]:
    EXTRA_REBUILD = sys.argv[sys.argv.index("--rebuild") + 1].split(",")
CC_CHANGE_AT = datetime(2026, 9, 19)  # first wrapped turn seen (UTC)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _unpromoted(db, agent_id: str) -> list[Message]:
    return (
        db.query(Message)
        .filter(
            Message.agent_id == agent_id,
            Message.role == MessageRole.USER,
            Message.status != MessageStatus.CANCELLED,
            or_(
                Message.source == "web",
                Message.source == "plan_continue",
                Message.source == "task",
            ),
            Message.jsonl_uuid.is_(None),
            Message.delivered_at.is_(None),
        )
        .order_by(Message.created_at.asc())
        .all()
    )


def _promote(web_msg: Message, jsonl_uuid: str | None, session_seq: int | None,
             delivered) -> None:
    web_msg.jsonl_uuid = jsonl_uuid
    web_msg.session_seq = session_seq
    web_msg.delivered_at = delivered
    web_msg.status = MessageStatus.COMPLETED
    web_msg.completed_at = web_msg.completed_at or delivered


def pass1(db) -> tuple[int, int, set[str]]:
    """Merge wrapped source=cli duplicates into their un-promoted twins."""
    promoted = rewritten = 0
    touched: set[str] = set()
    dups = (
        db.query(Message)
        .filter(
            Message.role == MessageRole.USER,
            Message.source == "cli",
            Message.content.like("%<pasted_content%"),
        )
        .order_by(Message.created_at.asc())
        .all()
    )
    print(f"pass 1: {len(dups)} cli rows carry the wrapper")
    for dup in dups:
        unwrapped = unwrap_pasted_content(dup.content or "")
        if unwrapped == dup.content:
            continue  # literal mention, not a wrapper — leave alone
        clean = strip_agent_preamble(unwrapped)
        web_msg, method = ContentMatcher.match(clean, _unpromoted(db, dup.agent_id))
        touched.add(dup.agent_id)
        preview = clean.replace("\n", " ")[:60]
        if web_msg is not None:
            print(f"  promote {web_msg.id} ({web_msg.source}, {method}) "
                  f"<- drop cli {dup.id}  agent={dup.agent_id[:8]}  \"{preview}\"")
            promoted += 1
            if APPLY:
                delivered = dup.delivered_at or dup.created_at or _utcnow()
                jsonl_uuid, session_seq = dup.jsonl_uuid, dup.session_seq
                db.delete(dup)      # (agent_id, jsonl_uuid) is unique — free it first
                db.flush()
                _promote(web_msg, jsonl_uuid, session_seq, delivered)
                db.commit()
        else:
            print(f"  rewrite cli {dup.id} (no un-promoted twin)  "
                  f"agent={dup.agent_id[:8]}  \"{preview}\"")
            rewritten += 1
            if APPLY:
                dup.content = clean
                db.commit()
    return promoted, rewritten, touched


def _session_jsonl(db, agent: Agent) -> str | None:
    from agent_dispatcher import _resolve_session_jsonl
    if not agent.session_id:
        return None
    proj = db.query(Project).filter(Project.name == agent.project).first()
    if not proj:
        return None
    try:
        return _resolve_session_jsonl(agent.session_id, proj.path, agent.worktree)
    except Exception as e:  # noqa: BLE001 — report and move on
        print(f"  agent {agent.id[:8]}: cannot resolve session JSONL: {e}")
        return None


def pass2(db) -> tuple[int, set[str]]:
    """Promote still-SENT rows straight from their JSONL echo."""
    from sync_engine import _parse_jsonl_ts

    promoted = 0
    touched: set[str] = set()
    agent_ids = [
        r[0] for r in (
            db.query(Message.agent_id)
            .filter(
                Message.role == MessageRole.USER,
                Message.status != MessageStatus.CANCELLED,
                or_(
                    Message.source == "web",
                    Message.source == "plan_continue",
                    Message.source == "task",
                ),
                Message.jsonl_uuid.is_(None),
                Message.delivered_at.is_(None),
                Message.created_at >= CC_CHANGE_AT,
            )
            .distinct()
            .all()
        )
    ]
    print(f"pass 2: {len(agent_ids)} agents have un-promoted rows since {CC_CHANGE_AT:%Y-%m-%d}")
    for aid in agent_ids:
        agent = db.get(Agent, aid)
        if agent is None:
            continue
        jsonl_path = _session_jsonl(db, agent)
        if not jsonl_path or not os.path.isfile(jsonl_path):
            print(f"  agent {aid[:8]}: no session JSONL on disk — skipped")
            continue
        candidates = _unpromoted(db, aid)
        known_uuids = {
            r[0] for r in db.query(Message.jsonl_uuid)
            .filter(Message.agent_id == aid, Message.jsonl_uuid.isnot(None)).all()
        }
        turns = parse_session_turns(jsonl_path)  # full read — no byte cap
        for idx, turn in enumerate(turns):
            role, content, _meta, uuid, kind, ts = turn
            if role != "user" or not uuid or uuid in known_uuids or kind == "slash_signal":
                continue
            web_msg, method = ContentMatcher.match(content, candidates)
            if web_msg is None:
                continue
            candidates.remove(web_msg)
            known_uuids.add(uuid)
            touched.add(aid)
            promoted += 1
            preview = content.replace("\n", " ")[:60]
            print(f"  promote {web_msg.id} ({web_msg.source}, {method}) "
                  f"<- jsonl turn {idx} {uuid[:8]}  agent={aid[:8]}  \"{preview}\"")
            if APPLY:
                _promote(web_msg, uuid, idx, _parse_jsonl_ts(ts) or web_msg.created_at or _utcnow())
                db.commit()
        left = len(candidates)
        if left:
            print(f"  agent {aid[:8]}: {left} un-promoted rows have no JSONL echo — left as is")
    return promoted, touched


def main() -> int:
    print("APPLY" if APPLY else "DRY RUN")
    db = SessionLocal()
    try:
        p1, rw, touched = pass1(db)
        p2, touched2 = pass2(db)
        touched |= touched2
        print(f"\npass 1: {p1} promoted+deduplicated, {rw} rewritten; "
              f"pass 2: {p2} promoted from JSONL; {len(touched)} agents touched")
        for aid in EXTRA_REBUILD:
            if db.get(Agent, aid) is None:
                print(f"  --rebuild {aid}: no such agent — skipped")
            else:
                touched.add(aid)
    finally:
        db.close()

    if not touched:
        return 0
    if APPLY:
        for aid in sorted(touched):
            rebuild_agent(aid)
            print(f"  rebuilt display file for agent {aid[:8]}")
    else:
        print(f"  would rebuild display files for {len(touched)} agents: "
              + ", ".join(a[:8] for a in sorted(touched)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
