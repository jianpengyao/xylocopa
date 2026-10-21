"""WebSocket hub — broadcasts real-time events to connected clients."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger("orchestrator.ws")

# Timeout for individual WS send operations (seconds)
_SEND_TIMEOUT = 5

# Event types to log at INFO in broadcast(). Others (high-frequency
# tool_activity / message_* / display signals) stay silent to avoid log spam.
# Keep this list focused on agent-lifecycle / insights / project-shape
# transitions where post-hoc verification matters.
_LOGGED_BROADCAST_TYPES = {
    "agent_update",
    "agent_created",
    "progress_suggestions_ready",
    "session_star_changed",
    "project_update",
    "dropbox_update",
}


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self):
        self.active: list[WebSocket] = []
        # Track which agents each WS client is currently viewing
        self._viewing: dict[WebSocket, set[str]] = {}
        # Track browser window focus state per client
        self._has_focus: dict[WebSocket, bool] = {}
        # Track the single "primary" agent per client — the pane/page the
        # user is actively interacting with. None = idle (no interaction
        # within the client's idle threshold). Used for time-accounting.
        self._primary: dict[WebSocket, str | None] = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        self._viewing[ws] = set()
        self._has_focus[ws] = True
        self._primary[ws] = None
        logger.info("WebSocket client connected (%d total)", len(self.active))

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        self._viewing.pop(ws, None)
        self._has_focus.pop(ws, None)
        self._primary.pop(ws, None)
        logger.info("WebSocket client disconnected (%d total)", len(self.active))

    def set_viewing(self, ws: WebSocket, agent_ids: set[str],
                    has_focus: bool | None = None,
                    primary_agent_id: str | None | type = ...):
        """Record which agents a client is currently viewing."""
        old = self._viewing.get(ws, set())
        old_focus = self._has_focus.get(ws)
        self._viewing[ws] = agent_ids
        if has_focus is not None:
            self._has_focus[ws] = has_focus
        if primary_agent_id is not ...:
            self._primary[ws] = primary_agent_id
        cur_focus = self._has_focus.get(ws)
        if old != agent_ids or old_focus != cur_focus:
            logger.info(
                "viewing update: agents=%s has_focus=%s primary=%s (was agents=%s has_focus=%s)",
                sorted(a[:8] for a in agent_ids), cur_focus,
                (self._primary.get(ws) or "")[:8] or None,
                sorted(a[:8] for a in old), old_focus,
            )

    def is_agent_viewed(self, agent_id: str) -> bool:
        """True if any connected client is currently viewing this agent."""
        return any(agent_id in v for v in self._viewing.values())

    def is_any_client_focused(self) -> bool:
        """True if any connected client's browser window has focus."""
        return any(self._has_focus.get(ws, False) for ws in self.active)

    def active_primary_agents(self) -> set[str]:
        """Deduplicated set of agent IDs currently being actively viewed
        across all focused clients. Empty if no client is focused /
        interacting. Used by the view-tracking tick loop — the same agent
        open on multiple devices counts once.
        """
        result: set[str] = set()
        for ws in self.active:
            if not self._has_focus.get(ws, False):
                continue
            pri = self._primary.get(ws)
            if pri:
                result.add(pri)
        return result

    async def broadcast(self, event_type: str, data: dict) -> int:
        """Send an event to all connected clients. Returns count of successful sends."""
        message = json.dumps({
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # Send to all clients in parallel with per-client timeout
        # so one stale connection doesn't block the rest.
        async def _send(ws):
            try:
                await asyncio.wait_for(ws.send_text(message), timeout=_SEND_TIMEOUT)
                return ws, True
            except Exception:
                logger.debug("WS send failed for %s", ws.client, exc_info=True)
                return ws, False

        results = await asyncio.gather(*[_send(ws) for ws in list(self.active)])
        disconnected = [ws for ws, ok in results if not ok]
        for ws in disconnected:
            self.disconnect(ws)
        sent_count = sum(1 for _, ok in results if ok)
        if event_type in _LOGGED_BROADCAST_TYPES:
            _key = (data.get("agent_id") or data.get("project")
                    or data.get("name") or data.get("session_id") or "?")
            logger.info("WS broadcast: type=%s key=%s sent=%d/%d",
                        event_type, str(_key)[:8], sent_count, len(results))
        return sent_count

    async def prune_stale(self):
        """Ping all clients and remove any that fail to respond."""
        ping_msg = json.dumps({"type": "ping"})
        disconnected = []
        for ws in list(self.active):
            try:
                await asyncio.wait_for(ws.send_text(ping_msg), timeout=_SEND_TIMEOUT)
            except Exception:
                logger.debug("WS ping failed for %s", ws.client, exc_info=True)
                disconnected.append(ws)
        if disconnected:
            for ws in disconnected:
                self.disconnect(ws)
            logger.info("Pruned %d stale WebSocket connections", len(disconnected))


# Singleton manager
ws_manager = ConnectionManager()


async def websocket_endpoint(ws: WebSocket):
    """WebSocket handler for /ws/status. Requires a one-time ?ticket= (from
    POST /api/auth/ws-ticket) when a password is set; see ws_auth.py."""
    import ws_auth

    ctx = await ws_auth.authorize_ws(ws)
    if ctx is None:
        return

    await ws_manager.connect(ws)

    # Send currently-generating agents so the client can seed its streaming
    # set and defer notifications correctly even after a reconnect.
    ad = getattr(ws.app.state, "agent_dispatcher", None)
    generating = list(ad._generating_agents) if ad else []
    if generating:
        await ws.send_text(json.dumps({
            "type": "generating_agents",
            "data": {"agent_ids": generating},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }))

    # Re-checks the session while the socket is open; closes 4001 on
    # revoke / expiry / idle. None when auth is off.
    wd = ws_auth.start_watchdog(ws, ctx, lambda: ws_manager.disconnect(ws))
    # Accepted without a session: closed 4001 when protection is switched on.
    ws_auth.track_open(ws, ctx, lambda: ws_manager.disconnect(ws))
    try:
        while True:
            # Keep connection alive; client may send pings or viewing updates
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            else:
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "viewing":
                        ids = msg.get("agent_ids")
                        has_focus = msg.get("has_focus")
                        # primary_agent_id may be absent (legacy clients),
                        # null (explicit idle), or a string.
                        if "primary_agent_id" in msg:
                            primary = msg.get("primary_agent_id")
                        else:
                            primary = ...  # sentinel: leave unchanged
                        if ids is not None:
                            ws_manager.set_viewing(
                                ws, set(ids), has_focus,
                                primary_agent_id=primary,
                            )
                        else:
                            aid = msg.get("agent_id")
                            ws_manager.set_viewing(
                                ws, {aid} if aid else set(), has_focus,
                                primary_agent_id=primary,
                            )
                except json.JSONDecodeError:
                    logger.debug("WS received invalid JSON: %s", data[:100])
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        logger.warning("WebSocket handler error", exc_info=True)
        ws_manager.disconnect(ws)
    finally:
        if wd is not None:
            wd.cancel()
        ws_auth.untrack_open(ws)


# ---- Event helpers (called from dispatcher/main) ----

async def emit_task_update(task_id: str, status: str, project: str,
                           title: str | None = None, agent_id: str | None = None):
    payload = {
        "task_id": task_id,
        "status": status,
        "project": project,
    }
    if title is not None:
        payload["title"] = title
    if agent_id is not None:
        payload["agent_id"] = agent_id
    await ws_manager.broadcast("task_update", payload)


async def emit_tasks_invalidated(task_ids: list[str], reason: str = ""):
    """Broadcast that a batch of tasks has changed and clients should
    refetch. Used for bulk mutations (e.g. defer-sweep) where emitting
    per-task `task_update` would cause N parallel refetches at the
    InboxTasksContext layer — that listener always does a full refetch,
    so a single coalesced event is all that's needed. Carries task_ids
    so per-task subscribers (e.g. TaskDetailPage) can filter precisely.
    """
    await ws_manager.broadcast("tasks_invalidated", {
        "task_ids": list(task_ids),
        "reason": reason,
    })


async def emit_worker_update(action: str, process_name: str, project: str = ""):
    await ws_manager.broadcast("worker_update", {
        "action": action,  # "created" | "destroyed"
        "process": process_name,
        "project": project,
    })


async def emit_system_alert(message: str, level: str = "warning"):
    await ws_manager.broadcast("system_alert", {
        "message": message,
        "level": level,
    })


async def emit_attention_fired(title: str, body: str, url: str,
                               meta: dict | None = None):
    """An attention job fired — the in-app twin of its push notification.

    The orb FAB subscribes to this and 'speaks' the message in its bubble,
    so a foreground user gets a light in-app surface instead of (on top of)
    the OS notification.
    """
    payload = {"title": title, "body": body, "url": url}
    for key in ("job_id", "kind"):
        if meta and meta.get(key) is not None:
            payload[key] = meta[key]
    await ws_manager.broadcast("attention_fired", payload)


async def emit_agent_update(agent_id: str, status: str, project: str,
                            insight_status: str | None = None):
    # TODO: insight_status / has_pending_suggestions belong in a separate
    # insight_update event — they're an insights-domain concern, not agent
    # lifecycle. Today they ride here for convenience, which has caused
    # repeated "forgot to pass insight_status kwarg" bugs (see commits
    # ac8fd64, 3450014). Split when the insights module is next refactored.
    data = {
        "agent_id": agent_id,
        "status": status,
        "project": project,
    }
    if insight_status is not None:
        data["insight_status"] = insight_status
    # Attach mutable list-view fields so the AgentsPage badge/preview
    # update in real-time without a follow-up refetch.  PK lookup is
    # cheap (<1ms) and always reflects latest committed state.
    # is_subagent is included so AgentsPage can skip subagents at the
    # receiver (GET /api/agents already filters them out at agents.py:1661);
    # other subscribers (e.g. AgentChatPage opened directly on a subagent
    # URL, parent-agent views) still get the event.
    try:
        from database import SessionLocal
        from models import Agent
        _db = SessionLocal()
        try:
            _a = _db.get(Agent, agent_id)
            if _a is not None:
                data["is_subagent"] = bool(_a.is_subagent)
                data["unread_count"] = _a.unread_count or 0
                data["last_message_preview"] = _a.last_message_preview or ""
                data["last_message_at"] = (
                    _a.last_message_at.isoformat()
                    if _a.last_message_at else None
                )
                data["has_pending_suggestions"] = bool(_a.has_pending_suggestions)
                data["deferred_to"] = (
                    _a.deferred_to.isoformat() if _a.deferred_to else None
                )
                data["muted"] = bool(_a.muted)
                data["name"] = _a.name or ""
        finally:
            _db.close()
    except Exception:
        logger.warning("emit_agent_update: agent lookup failed for %s",
                       agent_id[:8], exc_info=True)
    await ws_manager.broadcast("agent_update", data)


async def emit_agent_created(agent) -> None:
    """Broadcast a newly-created Agent so AgentsPage can insert it into the
    list without waiting for the next 5s poll. Carries a full AgentBrief
    payload — the frontend prepends directly, no follow-up fetch needed.

    Caller must `db.commit()` + `db.refresh(agent)` before invoking, so all
    server-default fields (e.g. created_at) are populated on the instance.
    """
    from schemas import AgentBrief
    try:
        payload = AgentBrief.model_validate(agent).model_dump(mode="json")
    except Exception:
        logger.warning("emit_agent_created: serialize failed for %s",
                       getattr(agent, "id", "?")[:8], exc_info=True)
        return
    sent = await ws_manager.broadcast("agent_created", payload)
    logger.info("emit_agent_created: agent=%s status=%s sent_to=%d clients",
                payload.get("id", "?")[:8], payload.get("status"), sent)


async def emit_new_message(agent_id: str, message_id: str,
                           agent_name: str = "", project: str = ""):
    await ws_manager.broadcast("new_message", {
        "agent_id": agent_id,
        "message_id": message_id,
        "agent_name": agent_name,
        "project": project,
    })


async def emit_project_update(name: str):
    """Signal: a project's settings/grouping changed (emoji, toggles,
    starred sessions). Frontend FoldersContext refetches the project list."""
    await ws_manager.broadcast("project_update", {"name": name})


async def emit_session_star_changed(project: str, session_id: str, starred: bool,
                                    agent_id: str | None = None):
    """Targeted signal: a single session's star flag flipped. Carries the
    new state so subscribers (chat page star button) can setState directly
    without a 250-360ms /sessions refetch.

    `agent_id` (when known) lets AgentsPage patch the shared AgentsContext
    store (which keys by agent.id) directly instead of waiting for the 5s
    /api/agents poll — bookmark felt fast for the same reason: its
    project_update signal is wired to the source of truth for its UI."""
    await ws_manager.broadcast("session_star_changed", {
        "project": project,
        "session_id": session_id,
        "starred": starred,
        "agent_id": agent_id,
    })


async def emit_assets_changed(project: str):
    """Signal: the asset index gained new refs for a project. An open
    Assets tab refetches GET /api/projects/{name}/assets."""
    await ws_manager.broadcast("assets_changed", {"project": project})


async def emit_asset_marks_changed(project: str, paths: list[str], **fields):
    """Targeted signal: star/hide/note changed for a batch of asset paths.
    Carries only the fields that were set so subscribers can patch local
    state without a refetch."""
    data = {"project": project, "paths": list(paths)}
    data.update(fields)
    await ws_manager.broadcast("asset_marks_changed", data)


# ---- Chat-message event emitters ----
#
# Signal-only by design: each event carries only {agent_id, message_id}.
# The display file (data/display/{agent_id}.jsonl) is the single source of
# truth for chat messages — frontend refetches via the split endpoints
# GET /api/agents/{id}/display/sent (byte-incremental file read) and
# GET /api/agents/{id}/display/pre-sent (full snapshot from in-memory
# index) when these signals arrive. Callers MUST flush the relevant write
# (display_writer.flush_agent / pre_sent_* / update_last) BEFORE emitting.


async def emit_message_delivered(agent_id: str, message_id: str):
    """Signal: a web-sent message was delivered to Claude."""
    await ws_manager.broadcast("message_delivered", {
        "agent_id": agent_id,
        "message_id": message_id,
    })


async def emit_pre_sent_created(agent_id: str, message_id: str):
    """Signal: a new pre-sent entry was created in the display file."""
    await ws_manager.broadcast("pre_sent_created", {
        "agent_id": agent_id,
        "message_id": message_id,
    })


async def emit_pre_sent_updated(agent_id: str, message_id: str):
    """Signal: a pre-sent entry was patched (content, scheduled_at,
    status, metadata)."""
    await ws_manager.broadcast("pre_sent_updated", {
        "agent_id": agent_id,
        "message_id": message_id,
    })


async def emit_pre_sent_tombstoned(agent_id: str, message_id: str):
    """Signal: a pre-sent entry was hard-deleted."""
    await ws_manager.broadcast("pre_sent_tombstoned", {
        "agent_id": agent_id,
        "message_id": message_id,
    })


async def emit_message_sent(agent_id: str, message_id: str):
    """Signal: a pre-sent entry was promoted to sent (file + DB)."""
    await ws_manager.broadcast("message_sent", {
        "agent_id": agent_id,
        "message_id": message_id,
    })


async def emit_message_executed(agent_id: str, message_id: str):
    """Signal: a sent/delivered message was executed (completed).

    Used for slash commands whose completion is marked by a hook
    (/compact PostCompact, /clear SessionStart source=clear, /loop and /goal SessionEnd).
    """
    await ws_manager.broadcast("message_executed", {
        "agent_id": agent_id,
        "message_id": message_id,
    })


async def emit_message_update(agent_id: str, message_id: str):
    """Signal: a message's status changed."""
    await ws_manager.broadcast("message_update", {
        "agent_id": agent_id,
        "message_id": message_id,
    })


async def emit_agent_stream_end(agent_id: str,
                                generation_id: int | None = None):
    """Signal that an agent's current generation has finished."""
    payload: dict = {"agent_id": agent_id}
    if generation_id is not None:
        payload["generation_id"] = generation_id
    await ws_manager.broadcast("agent_stream_end", payload)


async def emit_tool_activity(agent_id: str, tool_name: str, phase: str,
                              tool_input: dict | None = None,
                              tool_output: str | None = None,
                              is_error: bool = False):
    """Broadcast tool/subagent/permission events driven by CC hooks."""
    payload: dict = {
        "agent_id": agent_id,
        "tool_name": tool_name,
        "phase": phase,  # "start", "end", or "permission"
    }
    if tool_input:
        summary = _tool_input_summary(tool_name, tool_input)
        if summary:
            payload["summary"] = summary
    if phase == "end" and tool_output is not None:
        payload["output_summary"] = _tool_output_summary(tool_name, tool_output, is_error)
    if is_error:
        payload["is_error"] = True
    await ws_manager.broadcast("tool_activity", payload)


async def emit_metadata_update(agent_id: str, message_id: str):
    """Signal: a message's interactive metadata changed."""
    await ws_manager.broadcast("metadata_update", {
        "agent_id": agent_id,
        "message_id": message_id,
    })


async def emit_progress_suggestions_ready(agent_id: str, count: int, project: str):
    """Notify clients that agent insight suggestions are ready for review."""
    await ws_manager.broadcast("progress_suggestions_ready", {
        "agent_id": agent_id,
        "count": count,
        "project": project,
    })


async def emit_context_usage(agent_id: str):
    """Signal: token-budget snapshot updated for agent.

    Pushes the full breakdown (5 components + suggestions) on every
    assistant turn so the chat-header pill AND its popover share a
    single source of truth. The popover does not refetch on open.

    Side effect: persists the snapshot onto the Agent row so chat-page
    open / list endpoints serve the pill data without recomputing the
    breakdown (which scans JSONL + MCP/memory/agent files, ~100-200ms).
    """
    try:
        from context import get_context_breakdown
        snap = get_context_breakdown(agent_id)
    except Exception:
        logger.warning("emit_context_usage: snapshot failed for %s",
                       agent_id[:8], exc_info=True)
        return

    # Persist headline + full breakdown JSON onto the agent row.
    try:
        import json
        from datetime import datetime, timezone
        from database import SessionLocal
        from models import Agent
        db = SessionLocal()
        try:
            agent = db.get(Agent, agent_id)
            if agent is not None:
                agent.context_total = snap.get("total")
                agent.context_limit = snap.get("limit")
                agent.context_percent = snap.get("percent")
                agent.context_captured_at = datetime.now(timezone.utc)
                # Strip the headline keys from breakdown JSON — they're
                # already in their own columns. Keep components/suggestions/etc.
                breakdown_only = {
                    k: v for k, v in snap.items()
                    if k not in ("total", "limit", "percent", "captured_at",
                                 "model", "has_data", "session_id", "free")
                }
                agent.context_breakdown = json.dumps(breakdown_only) if breakdown_only else None
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning("emit_context_usage: persist failed for %s",
                       agent_id[:8], exc_info=True)

    payload = {"agent_id": agent_id, **snap}
    await ws_manager.broadcast("context_usage_update", payload)


def _tool_input_summary(tool_name: str, tool_input: dict) -> str:
    """Build a short human-readable summary from tool input."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:120] if cmd else ""
    if tool_name in ("Read", "Write", "Edit"):
        return tool_input.get("file_path", "")[:120]
    if tool_name in ("Glob", "Grep"):
        return tool_input.get("pattern", "")[:80]
    if tool_name == "Agent":
        return tool_input.get("description", "")[:80]
    if tool_name in ("WebFetch", "WebSearch"):
        return (tool_input.get("url") or tool_input.get("query") or "")[:120]
    return ""


def _tool_output_summary(tool_name: str, output: str, is_error: bool) -> str:
    """Build a short result summary from tool output."""
    if is_error:
        # First meaningful line of error
        for line in output.strip().splitlines():
            line = line.strip()
            if line:
                return f"error: {line[:100]}"
        return "error"

    if tool_name == "Bash":
        lines = output.strip().splitlines()
        if not lines:
            return "done (no output)"
        if len(lines) == 1:
            return lines[0][:120]
        return f"{lines[-1][:80]} ({len(lines)} lines)"
    if tool_name == "Read":
        lines = output.strip().splitlines()
        return f"{len(lines)} lines"
    if tool_name == "Grep":
        lines = output.strip().splitlines()
        if not lines:
            return "no matches"
        return f"{len(lines)} matches"
    if tool_name == "Glob":
        lines = [l for l in output.strip().splitlines() if l.strip()]
        if not lines:
            return "no files"
        return f"{len(lines)} files"
    if tool_name == "Agent":
        # Agent output can be huge — just indicate completion
        return "done"
    if tool_name in ("Edit", "Write"):
        return "done"
    # Generic fallback
    length = len(output)
    if length < 50:
        return output.strip()[:50] or "done"
    return f"done ({length} chars)"


async def emit_dropbox_update(kind: str, project: str | None = None, **extra):
    """Broadcast a Dropbox sync status change."""
    data = {"kind": kind, "project": project, **extra}
    await ws_manager.broadcast("dropbox_update", data)
