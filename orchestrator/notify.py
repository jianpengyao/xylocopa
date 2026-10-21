"""Unified notification gateway — three-tier channel model.

Channels:
  notify_at      — user-initiated reminder, always sends
  attention      — attention-assistant job fired, always sends
  permission     — permission requests, always sends
  task_complete  — to devices with tasks_enabled; no production caller today
                   (only /api/test/notify) — see below
  message        — conversational content, respects per-agent mute + in-use,
                   to devices with agents_enabled

Per-device filtering (push_subscriptions.agents_enabled / tasks_enabled)
happens in push._send_webpush; the "always" channels reach every device.
The global notifications_*_enabled settings no longer gate anything.

Agent/task completions (stop hook, CLI session end) and the interactive
permission / AskUserQuestion / plan cards all go out as "message"
(AgentDispatcher._send_agent_notification), so a device's agents_enabled
switch gates them; only a permission *timeout* uses "permission".
"""

import logging

from push import send_push_notification

logger = logging.getLogger("orchestrator.notify")


def notify(
    channel: str,
    agent_id: str,
    title: str,
    body: str,
    url: str = "/",
    *,
    in_use: bool = False,
    muted: bool = False,
    meta: dict | None = None,
) -> str:
    """Send a notification through the appropriate channel.

    Returns decision string: "SEND", "SKIP (reason)", or "DROP (unknown)".
    """
    if channel == "permission":
        # Permission requests are urgent — always send, never suppressed
        logger.info("notify: %s agent=%s → SEND (always)", channel, agent_id[:8])
        _send(title, body, url, channel)
        return "SEND"

    if channel == "notify_at":
        logger.info("notify: %s → SEND (always)", channel)
        _send(title, body, url, channel)
        return "SEND"

    if channel == "attention":
        # An attention job exists only because the user explicitly created
        # it, so it carries the same "always send" contract as notify_at —
        # not gated by the per-device message switch or per-agent mute.
        logger.info("notify: %s → SEND (always)", channel)
        _send(title, body, url, channel)
        _emit_attention_ws(title, body, url, meta)
        return "SEND"

    if channel == "task_complete":
        logger.info("notify: %s agent=%s → SEND", channel, agent_id[:8])
        _send(title, body, url, channel)
        return "SEND"

    if channel == "message":
        if muted:
            logger.info("notify: %s agent=%s → SKIP (muted)", channel, agent_id[:8])
            return "SKIP (muted)"
        if in_use:
            logger.info("notify: %s agent=%s → SKIP (in-use)", channel, agent_id[:8])
            return "SKIP (in-use)"
        logger.info("notify: %s agent=%s → SEND", channel, agent_id[:8])
        _send(title, body, url, channel)
        return "SEND"

    logger.warning("notify: unknown channel %r — dropping", channel)
    return "DROP (unknown)"


def _send(title: str, body: str, url: str, channel: str) -> None:
    """Send via all backends; push filters devices by *channel*."""
    send_push_notification(title, body, url, channel=channel)


def _emit_attention_ws(title: str, body: str, url: str,
                       meta: dict | None) -> None:
    """Broadcast the foreground twin of an attention push.

    Best-effort only: outside a running event loop (tests, the MCP process)
    there is no WS hub to speak through, and the push above already went
    out — so any failure here is swallowed by design.
    """
    try:
        import asyncio

        from websocket import emit_attention_fired

        asyncio.get_running_loop()
        asyncio.ensure_future(emit_attention_fired(title, body, url, meta))
    except Exception:
        pass
