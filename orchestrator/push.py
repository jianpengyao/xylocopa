"""Push notification sender — non-blocking, fire-and-forget.

Backend: Web Push (VAPID) — browser-based, works when PWA is open.
"""

import atexit
import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests as _requests

from config import (
    VAPID_PRIVATE_KEY,
    VAPID_PUBLIC_KEY,
    VAPID_SUBJECT,
)

logger = logging.getLogger("orchestrator.push")

# Thread pool for webpush fanout. Each push is an HTTPS round-trip
# (TLS + POST to Apple/Google/Mozilla) ~200-250ms.  Callers submit
# fire-and-forget tasks; workers handle their own 404/410 cleanup so the
# event loop never waits on network I/O.
_push_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="webpush")

# Drain in-flight pushes on process exit.  Without this, SIGTERM during
# a restart could truncate pending TLS POSTs.
atexit.register(lambda: _push_pool.shutdown(wait=True))


# Per-device flag that gates each channel.  Channels not listed here
# (permission, notify_at, attention, test) reach every subscription.
# The old global SystemConfig flags (notifications_*_enabled) gate nothing.
CHANNEL_DEVICE_FLAG = {
    "message": "agents_enabled",
    "task_complete": "tasks_enabled",
}


def send_push_notification(title: str, body: str, url: str = "/",
                           channel: str | None = None) -> None:
    """Send a push notification via Web Push to the devices that want *channel*."""
    _send_webpush(title, body, url, channel=channel)


def send_test_push(endpoint: str) -> int:
    """Send the bell's confirmation push to one subscription only.

    Ignores that device's flags: the user just asked for it.  Returns the
    number of pushes queued (0 if Web Push is unavailable or the endpoint
    is unknown).
    """
    return _send_webpush(
        "Xylocopa", "Notifications are on for this device", "/",
        channel="test", endpoint=endpoint,
    )


def _send_one(sub_data: dict, title: str, body: str, url: str, nid: str) -> None:
    """Worker: send one webpush and self-clean if endpoint is gone.

    Runs in a _push_pool thread.  sub_data is a plain dict (materialized
    from the ORM) so there's no risk of DetachedInstanceError.
    """
    from urllib.parse import urlparse
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return

    sub_id = sub_data["id"]
    endpoint = sub_data["endpoint"]
    parsed = urlparse(endpoint)
    aud = f"{parsed.scheme}://{parsed.netloc}"
    payload = json.dumps({"title": title, "body": body, "url": url, "nid": nid})
    subscription_info = {
        "endpoint": endpoint,
        "keys": {
            "p256dh": sub_data["p256dh_key"],
            "auth": sub_data["auth_key"],
        },
    }
    vapid_claims = {"sub": VAPID_SUBJECT, "aud": aud}

    sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
    accepted = False
    expired = False
    status = None
    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=vapid_claims,
        )
        accepted = True
        logger.info(
            "push send: nid=%s sub=%s host=%s ok",
            nid, sub_id, parsed.netloc,
        )
    except WebPushException as e:
        # Only the push service saying the subscription is gone (404/410)
        # deletes it; silence from the device never does.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            expired = True
        else:
            logger.warning("Push failed for %s: %s", endpoint[:60], e)
    except _requests.exceptions.RequestException:
        logger.debug("Push network error for %s", endpoint[:60], exc_info=True)
    except Exception:
        logger.warning("Push error for %s", endpoint[:60], exc_info=True)

    if accepted:
        # Diagnostic counter, reset by /api/push/ack.  Skip it when an ack
        # already landed after this send started (ack beat our 201).
        from database import SessionLocal
        from models import PushSubscription
        from sqlalchemy import or_
        _db = SessionLocal()
        try:
            _db.query(PushSubscription).filter(
                PushSubscription.id == sub_id,
                or_(PushSubscription.last_ack_at.is_(None),
                    PushSubscription.last_ack_at < sent_at),
            ).update(
                {PushSubscription.unacked_sends: PushSubscription.unacked_sends + 1},
                synchronize_session=False,
            )
            _db.commit()
        except Exception:
            logger.debug("unacked_sends bump failed for sub=%s", sub_id, exc_info=True)
        finally:
            _db.close()

    if expired:
        from database import SessionLocal
        from models import PushSubscription
        _db = SessionLocal()
        try:
            _db.query(PushSubscription).filter(
                PushSubscription.id == sub_id
            ).delete(synchronize_session=False)
            _db.commit()
            logger.info("Removed expired push subscription id=%s (push service %s)",
                        sub_id, status)
        except Exception:
            logger.warning(
                "Failed to delete expired sub id=%s", sub_id, exc_info=True,
            )
        finally:
            _db.close()


def _send_webpush(title: str, body: str, url: str = "/",
                  channel: str | None = None,
                  endpoint: str | None = None) -> int:
    """Fire-and-forget fanout to the subscribed browsers that want *channel*.

    Reads the subscription list (a short indexed query), keeps the rows
    whose per-device flag allows *channel* (or only *endpoint*, if given),
    materializes each into a plain dict, and submits one fire-and-forget
    task per subscription.  Returns before any TLS POST completes, so the
    caller (typically the sync-engine coroutine) is not blocked by
    push RTTs and subsequent WebSocket emits can proceed immediately.
    Returns the number of pushes queued.
    """
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return 0

    try:
        import pywebpush  # noqa: F401 — presence check only
    except ImportError:
        logger.debug("pywebpush not installed — skipping web push")
        return 0

    from database import SessionLocal
    from models import PushSubscription

    flag = CHANNEL_DEVICE_FLAG.get(channel or "")
    db = SessionLocal()
    try:
        subs = db.query(PushSubscription).all()
        if not subs:
            return 0
        total = len(subs)
        if endpoint is not None:
            subs = [s for s in subs if s.endpoint == endpoint]
        elif flag:
            subs = [s for s in subs if getattr(s, flag)]
        sub_data_list = [
            {
                "id": s.id,
                "endpoint": s.endpoint,
                "p256dh_key": s.p256dh_key,
                "auth_key": s.auth_key,
            }
            for s in subs
        ]
    finally:
        db.close()

    # Diagnostic correlation id — SW echoes this back via /api/push/ack
    nid = secrets.token_hex(4)
    logger.info(
        "push send: nid=%s subs=%d (of %d) channel=%s title=%r",
        nid, len(sub_data_list), total, channel or "-", title[:60],
    )
    queued = 0
    for sd in sub_data_list:
        try:
            _push_pool.submit(_send_one, sd, title, body, url, nid)
            queued += 1
        except RuntimeError:
            # Pool already shut down (process exiting) — drop silently.
            logger.debug("push pool shut down, dropping push to sub=%s", sd["id"])
    return queued
