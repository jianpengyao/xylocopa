"""Database models for Xylocopa."""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import DEFAULT_CLAUDE_MODEL
from utils import utcnow as _utcnow


class Base(DeclarativeBase):
    pass


class AgentMode(str, enum.Enum):
    INTERVIEW = "INTERVIEW"    # Chat only — no auto-execution
    AUTO = "AUTO"              # Execute immediately


class TaskStatus(str, enum.Enum):
    INBOX = "INBOX"
    PLANNING = "PLANNING"
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    REVIEW = "REVIEW"
    MERGING = "MERGING"
    CONFLICT = "CONFLICT"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"  # displayed as "Dropped" in the UI
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class AgentStatus(str, enum.Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    EXECUTING = "EXECUTING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


class MessageRole(str, enum.Enum):
    USER = "USER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class MessageStatus(str, enum.Enum):
    SENT = "SENT"           # sent via tmux send-keys, awaiting JSONL delivery confirmation
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"  # soft-cancelled by user before dispatch; bubble stays visible (greyed)


def _new_uuid():
    return uuid.uuid4().hex[:12]


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_uuid)
    # New first-class fields
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_name: Mapped[str | None] = mapped_column(
        String(100), ForeignKey("projects.name", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, default=0)  # 0=normal, 1=high
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), nullable=False, default=TaskStatus.INBOX, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True,
    )
    worktree_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    retry_context: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: previous failure info
    agent_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    effort: Mapped[str | None] = mapped_column(String(10), nullable=True)
    skip_permissions: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    use_worktree: Mapped[bool] = mapped_column(Boolean, default=True)
    use_tmux: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    notify_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deferred_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # V1 legacy columns — kept because SQLite NOT NULL without DEFAULT.
    # Not used by any code; safe to drop once a migration rewrites the table.
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    project: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    mode: Mapped[AgentMode] = mapped_column(Enum(AgentMode), nullable=False, default=AgentMode.AUTO)
    retries: Mapped[int] = mapped_column(Integer, default=0)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_uuid)
    project: Mapped[str] = mapped_column(
        String(100), ForeignKey("projects.name", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[AgentMode] = mapped_column(
        Enum(AgentMode), nullable=False, default=AgentMode.AUTO
    )
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus), nullable=False, default=AgentStatus.STARTING, index=True
    )
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    worktree: Mapped[str | None] = mapped_column(String(200), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    cli_sync: Mapped[bool] = mapped_column(Boolean, default=True)
    tmux_pane: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    effort: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    skip_permissions: Mapped[bool] = mapped_column(Boolean, default=True)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    deferred_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True,
    )
    task_id: Mapped[str | None] = mapped_column(
        String(12), ForeignKey("tasks.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    is_subagent: Mapped[bool] = mapped_column(Boolean, default=False)
    claude_agent_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    generating_msg_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    has_pending_suggestions: Mapped[bool] = mapped_column(Boolean, default=False)
    insight_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # JSONL sync pointer — persisted so restart resumes where the last
    # sync stopped, instead of re-traversing the entire file (which
    # would re-fire all historical signals: stop_hook, push notify,
    # interactive-card dismissal, etc).
    sync_last_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sync_last_turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sync_last_content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # Persisted context-usage snapshot. Avoids re-scanning the JSONL +
    # MCP/memory/agent files on every chat-page open. Updated each time
    # `emit_context_usage` runs (post-turn, post-stop, sync_engine wake).
    # Headline columns are queryable for sort/filter; the JSON column
    # holds the full breakdown for the popover.
    context_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_captured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    context_breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_generating(self) -> bool:
        return self.generating_msg_id is not None

    # Parent → child subagent relationship (self-referential)
    subagents: Mapped[list["Agent"]] = relationship(
        "Agent",
        back_populates="parent_agent",
        foreign_keys="[Agent.parent_id]",
        viewonly=True,
        lazy="select",
    )
    parent_agent: Mapped["Agent | None"] = relationship(
        "Agent",
        back_populates="subagents",
        foreign_keys="[Agent.parent_id]",
        remote_side="[Agent.id]",
        viewonly=True,
        lazy="select",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_uuid)
    agent_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus), nullable=False, default=MessageStatus.COMPLETED
    )
    stream_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "web" | "cli" | None
    jsonl_uuid: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)  # JSONL entry uuid for dedup
    meta_json: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)  # JSON string for interactive data
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatch_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)  # dispatch order per agent
    tool_use_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    session_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "text" | "tool_use" | None (legacy)
    display_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)  # display file sequence number


class Project(Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    git_remote: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=8)
    default_model: Mapped[str] = mapped_column(
        String(100), default=DEFAULT_CLAUDE_MODEL
    )
    archived: Mapped[bool] = mapped_column(default=False)
    auto_progress_summary: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_insights: Mapped[bool] = mapped_column(Boolean, default=False)
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resume_emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resume_hint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resume_hint_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dropbox_sync: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSON array of selected top-level entries ("." = files in project root);
    # NULL = everything not excluded by ignore rules.
    dropbox_folders: Mapped[str | None] = mapped_column(Text, nullable=True)
    dropbox_ignore: Mapped[str | None] = mapped_column(Text, nullable=True)


class StarredSession(Base):
    __tablename__ = "starred_sessions"

    session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    project: Mapped[str] = mapped_column(
        String(100), ForeignKey("projects.name", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class BookmarkedMessage(Base):
    __tablename__ = "bookmarked_messages"
    __table_args__ = (
        Index("ix_bookmarks_project_created", "project", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project: Mapped[str] = mapped_column(
        String(100), ForeignKey("projects.name", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary_emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="message")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AssetRef(Base):
    """Derived index (asset_index.py): one row per (message, path, source)
    an agent mentioned / wrote / read / sent / presented. Rebuildable from
    messages + webapps, so message_id/agent_id carry no FK — the file (and
    its row) outlives purged messages and deleted agents.

    path is the canonical project-relative posix path, or
    "webapp:port:<port>" / "webapp:url:<url>" for non-file web apps, or
    "artifact:<url>" for a claude.ai artifact (see ArtifactCall).
    source: "text" | "write" | "read" | "send" | "webapp" | "artifact".
    """
    __tablename__ = "asset_refs"
    __table_args__ = (
        UniqueConstraint("project", "path", "source", "message_id", name="uq_asset_refs"),
        Index("ix_asset_refs_project_path", "project", "path"),
        Index("ix_asset_refs_project_created", "project", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(
        String(100), ForeignKey("projects.name", ondelete="CASCADE"), nullable=False,
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    webapp_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AssetMark(Base):
    """User state per project asset (star / hide / note). Keyed by the same
    canonical path as AssetRef; never touched by reindexing."""
    __tablename__ = "asset_marks"
    __table_args__ = (
        Index("ix_asset_marks_project", "project"),
    )

    project: Mapped[str] = mapped_column(
        String(100), ForeignKey("projects.name", ondelete="CASCADE"), primary_key=True,
    )
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ArtifactCall(Base):
    """Memo of Artifact tool calls read from session transcripts (artifact_index.py).
    One row per CALL (tool_use_id); refs are per message row, since forked agents
    and sibling subagents carry copies of one call. NOT wiped by an
    ASSET_INDEX_VERSION bump: transcripts can be deleted, so these rows are the
    only lasting copy of url / title / type per call. message_id and agent_id
    (the lowest-rowid row seen, i.e. the original) carry no FK, same reasoning
    as AssetRef. Written only through artifact_index (raw DBAPI), which lists
    every column and formats timestamps with _db_ts."""
    __tablename__ = "artifact_calls"
    __table_args__ = (
        Index("ix_artifact_calls_key", "artifact_key"),
        Index("ix_artifact_calls_outcome", "outcome"),
    )

    tool_use_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    message_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)   # message time
    indexed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)   # first registered
    # publish | deleted | asset | ignored | error | pending | unavailable
    outcome: Mapped[str] = mapped_column(String(12), nullable=False)
    action: Mapped[str | None] = mapped_column(String(20), nullable=True)   # input.action or "publish"
    artifact_key: Mapped[str | None] = mapped_column(Text, nullable=True)   # publish / deleted only
    url: Mapped[str | None] = mapped_column(Text, nullable=True)            # canonical url this call returned
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)          # <=300 chars
    description: Mapped[str | None] = mapped_column(Text, nullable=True)    # input.description, <=1000 chars
    type_url: Mapped[str | None] = mapped_column(Text, nullable=True)       # this artifact's own type
    type_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    types_json: Mapped[str | None] = mapped_column(Text, nullable=True)     # [[type_url, title], ...] seen in the result
    version: Mapped[str | None] = mapped_column(String(40), nullable=True)  # opaque tur.version
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)         # "Version N" / tur.seq
    extract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_uuid)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    p256dh_key: Mapped[str] = mapped_column(String(200), nullable=False)
    auth_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_ack_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Per-device switches: a client's bell controls only its own row.
    # agents_enabled gates the "message" channel (which also carries task
    # completions and permission/question/plan cards); tasks_enabled gates
    # "task_complete", which nothing in production sends today. The
    # always-send channels ignore both.
    agents_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tasks_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    device_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Diagnostic only: sends since the last SW ack. Never used to delete.
    unacked_sends: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ProgressInsight(Base):
    __tablename__ = "progress_insights"
    __table_args__ = (
        Index("ix_progress_project_date", "project", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(
        String(100), ForeignKey("projects.name", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Link insight to the agent that produced it (NULL for cross-agent daily summaries)
    agent_id: Mapped[str | None] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True,
    )
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AgentInsightSuggestion(Base):
    __tablename__ = "agent_insight_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    edited_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending/accepted/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class SystemConfig(Base):
    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


def _new_probe_token() -> str:
    import secrets
    return secrets.token_hex(16)  # 32 hex chars


class Probe(Base):
    __tablename__ = "probes"
    __table_args__ = (
        Index("ix_probes_agent_active", "agent_id", "fired_at", "expires_at"),
    )

    # Envelope markers — must be backend-controlled and never appear in
    # user-supplied `message`, otherwise an attacker (or a chat self-attack)
    # could synthesize a fake nested envelope and confuse the wake target
    # about where the trigger ends.
    ENVELOPE_PREFIX: str = "\U0001F514 Probe "
    ENVELOPE_FOOTER: str = "— end of probe trigger —"
    MAX_MESSAGE_LEN: int = 4000

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_uuid)
    token: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True, default=_new_probe_token,
    )
    agent_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @classmethod
    def validate_message(cls, text: str | None) -> str | None:
        """Reject probe messages that could break the envelope contract.

        Returns an error reason (str) if invalid, or None if valid.
        Used at BOTH probe_create (input gate) AND trigger time (defense
        in depth — guards against direct DB tampering or future code paths
        that mutate `message` post-create).
        """
        if not text or not text.strip():
            return "message is empty"
        if len(text) > cls.MAX_MESSAGE_LEN:
            return (
                f"message exceeds {cls.MAX_MESSAGE_LEN} chars (got {len(text)})"
            )
        if cls.ENVELOPE_PREFIX in text:
            return (
                f"message must not contain the envelope prefix "
                f"{cls.ENVELOPE_PREFIX!r} (would synthesize a nested envelope)"
            )
        if cls.ENVELOPE_FOOTER in text:
            return (
                f"message must not contain the envelope footer "
                f"{cls.ENVELOPE_FOOTER!r} (would forge envelope termination)"
            )
        # Reject control characters except newline and tab — protects tmux
        # send-keys + Claude's prompt parsing from terminal escape injection.
        for i, ch in enumerate(text):
            cp = ord(ch)
            if cp < 0x20 and ch not in ("\n", "\t"):
                return (
                    f"message contains control char U+{cp:04X} at offset {i}"
                )
            if cp == 0x7F:  # DEL
                return f"message contains DEL char at offset {i}"
        return None


class SessionViewEvent(Base):
    """A single continuous interval during which a user was actively
    viewing one agent session. Written by the view-tracking tick loop.
    """
    __tablename__ = "session_view_events"
    __table_args__ = (
        Index("ix_view_events_started", "started_at"),
        Index("ix_view_events_project_started", "project", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)


class WebApp(Base):
    """Registry of presentable web apps — interactive deliverables agents
    surface in chat via the MCP webapp_present tool.

    kind:
      static — agent-built static app; target is a project-relative path
               to the entry .html, served sandboxed via /api/preview/t/
      port   — local service (TensorBoard, vite dev) on 127.0.0.1:target,
               reverse-proxied via /api/preview/p/
      url    — external dashboard (e.g. wandb); opens in a new tab since
               most external sites forbid framing
    """
    __tablename__ = "webapps"
    __table_args__ = (
        UniqueConstraint("project", "kind", "target", name="uq_webapps_target"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_uuid)
    project: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # "static" | "port" | "url"
    target: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_by_agent: Mapped[str | None] = mapped_column(String(12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_presented_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CCSession(Base):
    """A single Claude Code conversation session — either top-level (the
    xylo agent's own CLI session, possibly one of many across /compact
    /clear rotations) or a sub-session spawned by Task tool invocation
    inside another CC session.

    Top-level: parent_session_id IS NULL, is_subagent_session = False
    Sub-session: parent_session_id points to the calling CC session,
                 is_subagent_session = True

    agent_id always points to the OWNING xylo agent. For sub-sessions
    that's the same xylo agent that issued the Task tool call (xylo
    "subagent" rows themselves have session_id=None — they're metadata,
    not session owners).

    Token totals are populated either:
    - On rotation/end (writer in agent_dispatcher / sync_engine)
    - By reconcile sweep (cc_session_discovery scanning JSONL files)
    Updated rows on subsequent writes if usage grew.
    """
    __tablename__ = "cc_sessions"
    __table_args__ = (
        Index("ix_cc_sessions_agent_started", "agent_id", "started_at"),
        Index("ix_cc_sessions_parent", "parent_session_id"),
    )

    session_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Self-referential link to the CC session that spawned this one via
    # Task tool. NULL for top-level sessions.
    parent_session_id: Mapped[str | None] = mapped_column(
        String(50),
        ForeignKey("cc_sessions.session_id", ondelete="SET NULL"),
        nullable=True,
    )
    # The JSONL `parentUuid` field — points back to a specific entry in
    # the parent's JSONL (the tool_use that spawned us). Lets reconcile
    # find unmatched sub-sessions.
    parent_jsonl_uuid: Mapped[str | None] = mapped_column(String(50), nullable=True)

    project_path: Mapped[str] = mapped_column(String(500), nullable=False)
    worktree: Mapped[str | None] = mapped_column(String(200), nullable=True)

    is_subagent_session: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # active | rotation | compact | clear | subagent_done | reconciled | stopped
    end_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)

    model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Sum of 5m + 1h. Maintained as a denormalized rollup for back-compat
    # with code paths that read this column directly.
    total_cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Anthropic charges 5m and 1h ephemeral cache writes at different rates
    # (1.25× input vs 2× input). JSONL `cache_creation` block exposes the
    # split. Legacy rows have 0 in both columns; cost calc falls back to
    # total_cache_creation_tokens at the 5m rate.
    total_cache_creation_5m_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cache_creation_1h_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


class AttentionJob(Base):
    """A single unit of work the attention assistant is holding for the user.

    Deliberately ONE table for every capability rather than separate
    "reminder" / "watcher" / "digest" tables: a job is a (trigger, action)
    pair, and both sides are pluggable via the registries in
    ``attention/registry.py``. Adding a capability later means registering
    one more trigger or action — the scheduler, the API and the UI stay
    untouched.

    ``trigger_type`` selects a ``attention.triggers`` entry:
      at      — fire once at trigger_config["at"] (ISO datetime)
      every   — fire repeatedly on an interval / time-of-day
      signal  — fire when a condition over named SIGNALS becomes true
      probe   — fired externally via /api/probe-trigger (placeholder for
                webhook-driven jobs; the Probe table owns the token)

    ``action_type`` selects a ``attention.actions`` entry:
      notify        — web push through the "attention" notify channel
      message_agent — inject a message into an agent's chat
      dispatch_task — create (and optionally dispatch) a Task
      run_prompt    — one headless `claude -p` call, result pushed to user

    Cost contract: the LLM runs ONCE, at creation time, to compile natural
    language into (trigger_config, action_config). Evaluation on the
    dispatcher tick is pure Python over indexed columns — a job costs
    nothing to keep alive and keeps working if the LLM is unavailable.
    """
    __tablename__ = "attention_jobs"
    __table_args__ = (
        # The scheduler's only query: active jobs whose next_run_at is due.
        Index("ix_attn_jobs_due", "status", "next_run_at"),
        Index("ix_attn_jobs_agent", "agent_id"),
    )

    # Status values are plain strings (not an Enum) so a newly registered
    # trigger/action can introduce states without a schema migration.
    STATUS_ACTIVE: str = "active"
    STATUS_PAUSED: str = "paused"
    STATUS_DONE: str = "done"
    STATUS_ERROR: str = "error"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_new_uuid)

    # Informational grouping for the UI only — never branched on in the
    # scheduler. The (trigger_type, action_type) pair is the real behavior.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="reminder")
    # Human-readable one-liner rendered in the attention panel.
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # The user's original phrasing, kept for audit + "edit what I said".
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    action_type: Mapped[str] = mapped_column(String(20), nullable=False)
    action_config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_ACTIVE)

    # Indexed due-time. For `signal` triggers this is the next *evaluation*
    # time (throttle), not a guaranteed fire time. NULL = never runs again.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Edge-trigger memory for `signal` conditions. Without this, "notify me
    # when agent X makes progress" would re-fire on every 2s tick for as
    # long as the condition stays true. JSON blob owned by the trigger.
    last_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    fire_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Hard stop for recurring jobs — a runaway `every`/`signal` job can't
    # push forever. NULL = unbounded (one-shot `at` jobs set this to 1).
    max_fires: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Floor on the gap between two fires of this job, enforced by the
    # scheduler for every trigger/action pair. This is the backstop for
    # notification spam: even a correctly edge-triggered watch can burst if
    # the underlying state flaps, and a mis-compiled condition should be
    # annoying rather than unusable. Extra edges inside the window are
    # coalesced (dropped), not queued. NULL = no floor.
    min_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional scope. agent_id uses SET NULL (not CASCADE) so deleting an
    # agent leaves an auditable orphan the scheduler can retire cleanly
    # instead of silently vanishing mid-flight.
    agent_id: Mapped[str | None] = mapped_column(
        String(12), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True,
    )
    project_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
