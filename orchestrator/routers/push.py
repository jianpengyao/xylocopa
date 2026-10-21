"""Push notification routes — VAPID key, subscribe, per-device settings."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db

logger = logging.getLogger("orchestrator")

router = APIRouter(prefix="/api/push", tags=["push"])

# Subscriptions are deleted only when the push service answers 404/410
# (push._send_one).  There is no age/ack-based prune: a muted device gets
# no pushes, so it never acks, and must not be dropped for that.


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# First match wins: Android/ChromeOS UAs also say "Linux", Chrome/Edge UAs
# also say "Safari/".
_UA_OS = (("Android", "Android"), ("CrOS", "ChromeOS"), ("Macintosh", "Mac"),
          ("Windows", "Windows"), ("Linux", "Linux"))
_UA_BROWSER = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
               ("Chrome/", "Chrome"), ("Safari/", "Safari"))


def label_from_user_agent(ua: str) -> str | None:
    """Short device label: "iPhone", "Safari on Mac", "Chrome on Linux"."""
    if not ua:
        return None
    for dev in ("iPhone", "iPad"):
        if dev in ua:
            return dev
    os_name = next((name for key, name in _UA_OS if key in ua), None)
    browser = next((name for key, name in _UA_BROWSER if key in ua), None)
    if browser and os_name:
        return f"{browser} on {os_name}"
    return browser or os_name


def _device_settings(sub) -> dict:
    return {
        "agents_enabled": bool(sub.agents_enabled),
        "tasks_enabled": bool(sub.tasks_enabled),
        "device_label": sub.device_label,
    }


async def _body_endpoint(request: Request) -> tuple[dict, str]:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    endpoint = body.get("endpoint") or ""
    if not isinstance(endpoint, str) or not endpoint:
        raise HTTPException(status_code=400, detail="Missing endpoint")
    return body, endpoint


def _find_sub(db: Session, endpoint: str):
    from models import PushSubscription
    sub = db.query(PushSubscription).filter(
        PushSubscription.endpoint == endpoint
    ).first()
    if not sub:
        # 404 tells the client to re-subscribe (the server lost this device).
        raise HTTPException(status_code=404, detail="Unknown push subscription")
    return sub


@router.get("/vapid-public-key")
async def push_vapid_public_key():
    """Return the VAPID public key for Web Push subscription."""
    from config import VAPID_PUBLIC_KEY
    if not VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="VAPID keys not configured")
    return {"publicKey": VAPID_PUBLIC_KEY}


@router.post("/subscribe")
async def push_subscribe(request: Request, db: Session = Depends(get_db)):
    """Register a push subscription (upsert by endpoint).

    Re-registering keeps the device's flags.  Optional boolean
    ``agents_enabled`` / ``tasks_enabled`` in the body seed a *new* row
    only: a device whose endpoint rotated sends its last known switches so
    a mute survives the new endpoint; an existing row's flags always win.
    Returns this device's settings so the client's bell reflects the
    server, not localStorage, and ``created`` (the server did not know
    this endpoint).
    """
    from models import PushSubscription

    body = await request.json()
    endpoint = body.get("endpoint", "")
    keys = body.get("keys", {})
    p256dh = keys.get("p256dh", "")
    auth = keys.get("auth", "")

    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Missing endpoint or keys")

    label = label_from_user_agent(request.headers.get("user-agent", ""))
    existing = db.query(PushSubscription).filter(
        PushSubscription.endpoint == endpoint
    ).first()
    if existing:
        existing.p256dh_key = p256dh
        existing.auth_key = auth
        existing.last_seen_at = _utcnow()
        if label:
            existing.device_label = label
        sub = existing
        logger.info("push/subscribe: updated existing subscription sub=%s (endpoint=%s…)",
                    sub.id, endpoint[:60])
    else:
        seed = {k: body[k] if isinstance(body.get(k), bool) else True
                for k in ("agents_enabled", "tasks_enabled")}
        sub = PushSubscription(
            endpoint=endpoint,
            p256dh_key=p256dh,
            auth_key=auth,
            **seed,
            device_label=label,
            last_seen_at=_utcnow(),
        )
        db.add(sub)
        logger.info("push/subscribe: registered new subscription (%s, agents=%s tasks=%s, "
                    "endpoint=%s…)", label, seed["agents_enabled"], seed["tasks_enabled"],
                    endpoint[:60])
    db.commit()
    total = db.query(PushSubscription).count()
    logger.info("push/subscribe: total active subscriptions = %d", total)
    return {"status": "subscribed", "created": existing is None,
            "device": _device_settings(sub)}


@router.post("/device")
async def push_device(request: Request, db: Session = Depends(get_db)):
    """Update this device's switches.

    Body: {endpoint, agents_enabled?, tasks_enabled?}.  The endpoint is a
    capability-like secret, so it travels in the body, never the URL.
    """
    body, endpoint = await _body_endpoint(request)
    sub = _find_sub(db, endpoint)
    for key in ("agents_enabled", "tasks_enabled"):
        if key in body:
            if not isinstance(body[key], bool):
                raise HTTPException(status_code=400, detail=f"{key} must be a boolean")
            setattr(sub, key, body[key])
    db.commit()
    logger.info("push/device: sub=%s (%s) agents=%s tasks=%s",
                sub.id, sub.device_label, sub.agents_enabled, sub.tasks_enabled)
    return {"status": "updated", "device": _device_settings(sub)}


@router.post("/test")
async def push_test(request: Request, db: Session = Depends(get_db)):
    """Send one confirmation push to this device only. Body: {endpoint}."""
    from push import send_test_push

    _, endpoint = await _body_endpoint(request)
    sub = _find_sub(db, endpoint)
    if not send_test_push(endpoint):
        raise HTTPException(status_code=503, detail="Web Push not configured")
    logger.info("push/test: queued for sub=%s (%s)", sub.id, sub.device_label)
    return {"status": "sent"}


@router.post("/unsubscribe")
async def push_unsubscribe(request: Request, db: Session = Depends(get_db)):
    """Remove a push subscription by endpoint."""
    from models import PushSubscription

    body = await request.json()
    endpoint = body.get("endpoint", "")
    if not endpoint:
        raise HTTPException(status_code=400, detail="Missing endpoint")

    db.query(PushSubscription).filter(
        PushSubscription.endpoint == endpoint
    ).delete(synchronize_session=False)
    db.commit()
    return {"status": "unsubscribed"}


@router.post("/ack")
async def push_ack(request: Request, db: Session = Depends(get_db)):
    """Diagnostic: SW posts here from its push handler so we can tell
    'push reached device' apart from 'push never arrived'.

    Body: {nid, shown, ts, ua?, endpoint?}
    """
    from urllib.parse import urlparse

    from models import PushSubscription

    try:
        body = await request.json()
    except Exception:
        body = {}
    nid = body.get("nid", "")
    shown = body.get("shown")
    ts = body.get("ts")
    ua = (body.get("ua") or "")[:120]
    endpoint = body.get("endpoint", "") or ""

    sub_id = ""
    host = ""
    if endpoint:
        host = urlparse(endpoint).netloc
        sub = db.query(PushSubscription).filter(
            PushSubscription.endpoint == endpoint
        ).first()
        if sub:
            sub_id = sub.id
            sub.last_ack_at = datetime.now(timezone.utc).replace(tzinfo=None)
            sub.unacked_sends = 0
            db.commit()
    logger.info(
        "push ack: nid=%s sub=%s host=%s shown=%s ts=%s ua=%s",
        nid, sub_id, host, shown, ts, ua,
    )
    return {"status": "ok"}
