"""Probe webhook trigger — wakes a chat by external event.

A probe is a single-fire token that, when POSTed, inserts a fixed
chat-authored message into the target agent (envelope-wrapped) and
optionally fires a push notification. Token in the URL is the bearer:
the path is intentionally exempt from auth_middleware.

The trigger is dumb on purpose: the body is ignored, all wake content
comes from the chat-authored `message` set at probe_create time. This
prevents prompt injection by anyone who gets the URL.

Visual presentation: the wake message lands as a USER-role pre_sent
entry with `source="probe"`, then the frontend renders it as a system
bubble (matches /loop wakeup precedent).
"""

import asyncio
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from models import Agent, AgentStatus, Probe
from utils import utcnow as _utcnow

logger = logging.getLogger("orchestrator")

router = APIRouter(tags=["probes"])


def _envelope(probe: Probe) -> str:
    """Wrap the chat-authored message with a hard-coded probe envelope.

    Format mirrors `/loop wakeup`'s "Claude resuming /loop wakeup
    (Apr 24 3:05pm)" — same compact parens-time idiom — so the visual
    contract feels native.

    Both prefix and footer are hard-coded backend-side; create-time and
    dispatch-time validation reject any user message containing either
    string, so the wake target sees a uniquely demarcated envelope.
    """
    fired_at = _utcnow()
    # %b → "May", %-d / %-I strip leading zeros (Linux glibc).
    # Strftime "%p" returns "AM"/"PM"; lowercase to match /loop's pm/am.
    month_day = fired_at.strftime("%b %-d")
    hour_min = fired_at.strftime("%-I:%M") + fired_at.strftime("%p").lower()
    return (
        f"{Probe.ENVELOPE_PREFIX}{probe.id} fired ({month_day} {hour_min})\n\n"
        f"{probe.message}\n\n"
        f"{Probe.ENVELOPE_FOOTER}"
    )


@router.post("/api/probe-trigger/{token}")
async def trigger_probe(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Fire a probe — single-fire, idempotent on subsequent attempts.

    Returns 200 on first successful fire, 410 if already fired or
    expired, 404 if the token is unknown.

    The request body is read but ignored: by design, the wake content
    is locked at probe_create time. Empty body is fine.
    """
    probe = db.query(Probe).filter(Probe.token == token).first()
    if probe is None:
        raise HTTPException(status_code=404, detail="Probe not found")

    # SQLite stores datetimes naive UTC; strip tzinfo from `now` for in-memory
    # comparisons to avoid the offset-naive/aware TypeError.
    now = _utcnow().replace(tzinfo=None)
    if probe.fired_at is not None:
        raise HTTPException(
            status_code=410,
            detail=f"Probe already fired at {probe.fired_at.isoformat()}",
        )
    if probe.expires_at <= now:
        raise HTTPException(
            status_code=410,
            detail=f"Probe expired at {probe.expires_at.isoformat()}",
        )

    agent = db.get(Agent, probe.agent_id)
    if agent is None:
        # Agent was deleted out from under the probe — close it out.
        probe.fired_at = now
        db.commit()
        raise HTTPException(status_code=404, detail="Target agent no longer exists")

    # Defense in depth: re-validate `message` at dispatch time. Catches the
    # case where the probe row was tampered with directly in DB (bypassing
    # probe_create's input gate) or where future code paths added a
    # `probe_update(message=...)` without re-running validation. On failure,
    # burn the probe (set fired_at) so the bad message can't be replayed.
    err = Probe.validate_message(probe.message)
    if err:
        probe.fired_at = now
        db.commit()
        logger.warning(
            "probe %s rejected at dispatch: %s. Either DB tampering or a "
            "validation regression — investigate.", probe.id, err,
        )
        raise HTTPException(
            status_code=422,
            detail=f"Probe message failed validation: {err}",
        )

    # Mark fired BEFORE attempting delivery — race-safe against double-POST.
    # If delivery fails downstream, the probe stays "fired" with a stale
    # state; better than potentially double-waking a chat.
    probe.fired_at = now
    db.commit()

    envelope = _envelope(probe)
    msg_id = uuid.uuid4().hex[:12]
    # source="web" — probe rides the standard web message path. Probe identity
    # is preserved in metadata.probe_id (used by frontend SystemBubble to render
    # as a system event rather than user input). Reusing source="web" means the
    # sync_engine dedup whitelist needs no probe-specific entry.
    entry = {
        "id": msg_id,
        "role": "USER",
        "content": envelope,
        "source": "web",
        "status": "queued",
        "created_at": now.isoformat(),
        "scheduled_at": None,
        "metadata": {"probe_id": probe.id},
    }

    # Update agent preview so the agent list shows the probe arrival.
    agent.last_message_preview = envelope[:200]
    agent.last_message_at = now
    db.commit()

    # Write pre_sent entry — survives even if agent is STOPPED.
    from display_writer import pre_sent_create
    from websocket import emit_pre_sent_created
    pre_sent_create(agent.id, entry)
    asyncio.ensure_future(emit_pre_sent_created(agent.id, msg_id))

    # Live agent: dispatch immediately via existing pre_sent flow.
    # STOPPED agent: probes targeting a STOPPED agent are auto-expired by
    # the SQL trigger on agents.status transition; this path normally won't
    # be reached for stopped agents (the probe's expires_at == now would
    # have caused a 410 earlier in this handler). Defensive guard kept.
    ad = getattr(request.app.state, "agent_dispatcher", None)
    if ad and agent.status != AgentStatus.STOPPED:
        asyncio.ensure_future(ad.dispatch_pending_message(agent.id, delay=0))

    # No probe-specific push notify: the agent's stop hook will fire its own
    # notification when the response turn completes (via the standard message
    # channel). This respects per-agent mute and per-device switches, matching
    # the behavior of any other web-originated message.

    logger.info(
        "probe %s fired for agent %s (queued msg=%s)",
        probe.id, agent.id[:8], msg_id,
    )
    return {
        "status": "ok",
        "probe_id": probe.id,
        "agent_id": agent.id,
        "message_id": msg_id,
        "fired_at": now.isoformat(),
    }


@router.get("/api/probes")
async def list_probes(
    agent_id: str | None = None,
    include_fired: bool = False,
    db: Session = Depends(get_db),
):
    """List probes, optionally filtered by agent_id."""
    q = db.query(Probe)
    if agent_id:
        q = q.filter(Probe.agent_id == agent_id)
    if not include_fired:
        q = q.filter(Probe.fired_at.is_(None))
    q = q.order_by(Probe.created_at.desc()).limit(100)
    return [
        {
            "id": p.id,
            "agent_id": p.agent_id,
            "message": p.message,
            "created_at": p.created_at.isoformat(),
            "expires_at": p.expires_at.isoformat(),
            "fired_at": p.fired_at.isoformat() if p.fired_at else None,
        }
        for p in q.all()
    ]


@router.get("/api/probes/{probe_id}")
async def get_probe(probe_id: str, db: Session = Depends(get_db)):
    """Get a single probe by ID (not token — token is for trigger only)."""
    probe = db.get(Probe, probe_id)
    if probe is None:
        raise HTTPException(status_code=404, detail="Probe not found")
    return {
        "id": probe.id,
        "agent_id": probe.agent_id,
        "message": probe.message,
        "created_at": probe.created_at.isoformat(),
        "expires_at": probe.expires_at.isoformat(),
        "fired_at": probe.fired_at.isoformat() if probe.fired_at else None,
    }
