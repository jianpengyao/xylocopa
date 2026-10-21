"""Database session management."""

import logging
import os

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from config import CC_MODEL, DB_PATH, VALID_MODELS

logger = logging.getLogger(__name__)

# Ensure directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
    poolclass=NullPool,
)


# Enable WAL mode for better concurrent read performance
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency for DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _table_columns(conn, table_name: str) -> set[str]:
    """Return the set of column names for a SQLite table."""
    result = conn.execute(text(f"PRAGMA table_info({table_name})"))
    return {row[1] for row in result}


def init_db():
    """Create all tables if they don't exist, and run lightweight migrations."""
    from models import Base
    Base.metadata.create_all(bind=engine)

    # Lightweight migrations for existing databases
    with engine.connect() as conn:
        # Add description column to projects if missing
        columns = _table_columns(conn, "projects")
        if "description" not in columns:
            conn.execute(text("ALTER TABLE projects ADD COLUMN description TEXT"))
            conn.commit()

        # Add session_id column to agents if missing
        columns = _table_columns(conn, "agents")
        if "session_id" not in columns:
            conn.execute(text("ALTER TABLE agents ADD COLUMN session_id VARCHAR(100)"))
            conn.commit()

        # Add worktree column to agents if missing
        columns = _table_columns(conn, "agents")
        if "worktree" not in columns:
            conn.execute(text("ALTER TABLE agents ADD COLUMN worktree VARCHAR(200)"))
            conn.commit()

        # Add effort column to agents if missing
        columns = _table_columns(conn, "agents")
        if "effort" not in columns:
            conn.execute(text("ALTER TABLE agents ADD COLUMN effort VARCHAR(10)"))
            conn.commit()

        # Migrate priority → mode column on agents table
        columns = _table_columns(conn, "agents")
        if "mode" not in columns:
            # Add mode column with default AUTO
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN mode VARCHAR(9) NOT NULL DEFAULT 'AUTO'"
            ))
            # Migrate existing priority values: P0/P1 → PLAN, P2 → AUTO
            if "priority" in columns:
                conn.execute(text(
                    "UPDATE agents SET mode = CASE "
                    "WHEN priority IN ('P0', 'P1') THEN 'PLAN' "
                    "ELSE 'AUTO' END"
                ))
            conn.commit()

        # Same migration for tasks table
        columns = _table_columns(conn, "tasks")
        if "mode" not in columns:
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN mode VARCHAR(9) NOT NULL DEFAULT 'AUTO'"
            ))
            if "priority" in columns:
                conn.execute(text(
                    "UPDATE tasks SET mode = CASE "
                    "WHEN priority IN ('P0', 'P1') THEN 'PLAN' "
                    "ELSE 'AUTO' END"
                ))
            conn.commit()

        # Add archived column to projects if missing
        columns = _table_columns(conn, "projects")
        if "archived" not in columns:
            conn.execute(text(
                "ALTER TABLE projects ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()

        # Add emoji column to projects if missing
        columns = _table_columns(conn, "projects")
        if "emoji" not in columns:
            conn.execute(text(
                "ALTER TABLE projects ADD COLUMN emoji VARCHAR(16)"
            ))
            conn.commit()

        # Add resume hint columns (LLM-generated mood + recap)
        columns = _table_columns(conn, "projects")
        if "resume_emoji" not in columns:
            conn.execute(text("ALTER TABLE projects ADD COLUMN resume_emoji VARCHAR(16)"))
            conn.commit()
        columns = _table_columns(conn, "projects")
        if "resume_hint" not in columns:
            conn.execute(text("ALTER TABLE projects ADD COLUMN resume_hint VARCHAR(80)"))
            conn.commit()
        columns = _table_columns(conn, "projects")
        if "resume_hint_at" not in columns:
            conn.execute(text("ALTER TABLE projects ADD COLUMN resume_hint_at DATETIME"))
            conn.commit()

        # Dropbox sync per-project settings
        columns = _table_columns(conn, "projects")
        if "dropbox_sync" not in columns:
            conn.execute(text(
                "ALTER TABLE projects ADD COLUMN dropbox_sync BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()
        columns = _table_columns(conn, "projects")
        if "dropbox_folders" not in columns:
            conn.execute(text("ALTER TABLE projects ADD COLUMN dropbox_folders TEXT"))
            conn.commit()
        columns = _table_columns(conn, "projects")
        if "dropbox_ignore" not in columns:
            conn.execute(text("ALTER TABLE projects ADD COLUMN dropbox_ignore TEXT"))
            conn.commit()

        # Add cli_sync column to agents if missing
        columns = _table_columns(conn, "agents")
        if "cli_sync" not in columns:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN cli_sync BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()

        # Add model column to agents if missing
        columns = _table_columns(conn, "agents")
        if "model" not in columns:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN model VARCHAR(100)"
            ))
            conn.commit()

        # Add default_model column to projects if missing
        columns = _table_columns(conn, "projects")
        if "default_model" not in columns:
            conn.execute(text(
                "ALTER TABLE projects ADD COLUMN default_model VARCHAR(100) NOT NULL DEFAULT 'claude-opus-4-8'"
            ))
            conn.commit()

        # Add tmux_pane column to agents if missing
        columns = _table_columns(conn, "agents")
        if "tmux_pane" not in columns:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN tmux_pane VARCHAR(100)"
            ))
            conn.commit()

        # Add scheduled_at column to messages if missing
        columns = _table_columns(conn, "messages")
        if "scheduled_at" not in columns:
            conn.execute(text(
                "ALTER TABLE messages ADD COLUMN scheduled_at DATETIME"
            ))
            conn.commit()

        # Add source column to messages if missing
        columns = _table_columns(conn, "messages")
        if "source" not in columns:
            conn.execute(text(
                "ALTER TABLE messages ADD COLUMN source VARCHAR(20)"
            ))
            conn.commit()

        # Add metadata column to messages if missing
        columns = _table_columns(conn, "messages")
        if "metadata" not in columns:
            conn.execute(text(
                "ALTER TABLE messages ADD COLUMN metadata TEXT"
            ))
            conn.commit()

        # Add skip_permissions column to agents if missing
        columns = _table_columns(conn, "agents")
        if "skip_permissions" not in columns:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN skip_permissions BOOLEAN NOT NULL DEFAULT 1"
            ))
            conn.commit()

        if "muted" not in columns:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN muted BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()

        if "deferred_to" not in columns:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN deferred_to DATETIME"
            ))
            conn.commit()

        if "parent_id" not in columns:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN parent_id VARCHAR(12)"
            ))
            conn.commit()

        # Drop old priority column now that mode has been migrated
        columns = _table_columns(conn, "agents")
        if "priority" in columns and "mode" in columns:
            conn.execute(text("ALTER TABLE agents DROP COLUMN priority"))
            conn.commit()

        columns = _table_columns(conn, "tasks")
        if "priority" in columns and "mode" in columns:
            conn.execute(text("ALTER TABLE tasks DROP COLUMN priority"))
            conn.commit()

        # Drop plan-related columns (plan mode fully removed)
        columns = _table_columns(conn, "agents")
        if "plan_approved" in columns:
            conn.execute(text("ALTER TABLE agents DROP COLUMN plan_approved"))
            conn.commit()
        # Re-read columns after potential drop
        columns = _table_columns(conn, "agents")
        if "plan" in columns:
            conn.execute(text("ALTER TABLE agents DROP COLUMN plan"))
            conn.commit()

        # Add sync pointer columns to agents (persistent JSONL position).
        agent_cols = _table_columns(conn, "agents")
        if "sync_last_offset" not in agent_cols:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN sync_last_offset INTEGER NOT NULL DEFAULT 0"
            ))
            conn.commit()
        if "sync_last_turn_count" not in agent_cols:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN sync_last_turn_count INTEGER NOT NULL DEFAULT 0"
            ))
            conn.commit()
        if "sync_last_content_hash" not in agent_cols:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN sync_last_content_hash VARCHAR(64) NOT NULL DEFAULT ''"
            ))
            conn.commit()

        # One-shot data migration: rename legacy MessageStatus.QUEUED to SENT.
        # The QUEUED enum was renamed to SENT to match its actual semantic of
        # "sent via tmux send-keys, awaiting JSONL delivery confirmation".
        # Idempotent: a clean DB makes this a no-op.
        conn.execute(text(
            "UPDATE messages SET status='SENT' WHERE status='QUEUED'"
        ))

        # One-shot cleanup: legacy PENDING / TIMEOUT MessageStatus values were
        # removed from the enum (PENDING was never written under the Phase 2
        # pre_sent architecture; TIMEOUT was dead code with no writers).
        # Existing rows must be reconciled before SQLAlchemy enum loading.
        # PENDING rows: if delivered_at set → COMPLETED (was actually delivered);
        # otherwise delete (legacy stale, user can re-send).
        conn.execute(text(
            "UPDATE messages SET status='COMPLETED', "
            "completed_at=COALESCE(completed_at, delivered_at) "
            "WHERE status='PENDING' AND delivered_at IS NOT NULL"
        ))
        conn.execute(text(
            "DELETE FROM messages WHERE status='PENDING'"
        ))
        # TIMEOUT rows: collapse to FAILED (semantic equivalent — task didn't
        # complete successfully).
        conn.execute(text(
            "UPDATE messages SET status='FAILED' WHERE status='TIMEOUT'"
        ))
        conn.commit()

        # Drop plan-related columns from tasks (plan mode fully removed)
        task_cols_pre = _table_columns(conn, "tasks")
        if "plan_approved" in task_cols_pre:
            conn.execute(text("ALTER TABLE tasks DROP COLUMN plan_approved"))
            conn.commit()
        task_cols_pre = _table_columns(conn, "tasks")
        if "plan" in task_cols_pre:
            conn.execute(text("ALTER TABLE tasks DROP COLUMN plan"))
            conn.commit()

        # --- Task v2 migrations ---
        task_cols = _table_columns(conn, "tasks")

        task_new_cols = {
            "title": "ALTER TABLE tasks ADD COLUMN title VARCHAR(300) NOT NULL DEFAULT ''",
            "description": "ALTER TABLE tasks ADD COLUMN description TEXT",
            "project_name": "ALTER TABLE tasks ADD COLUMN project_name VARCHAR(100)",
            "priority": "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
            "agent_id": "ALTER TABLE tasks ADD COLUMN agent_id VARCHAR(12) REFERENCES agents(id)",
            "worktree_name": "ALTER TABLE tasks ADD COLUMN worktree_name VARCHAR(200)",
            "branch_name": "ALTER TABLE tasks ADD COLUMN branch_name VARCHAR(200)",
            "attempt_number": "ALTER TABLE tasks ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1",
            "retry_context": "ALTER TABLE tasks ADD COLUMN retry_context TEXT",
            "review_artifacts": "ALTER TABLE tasks ADD COLUMN review_artifacts TEXT",
            "agent_summary": "ALTER TABLE tasks ADD COLUMN agent_summary TEXT",
            "rejection_reason": "ALTER TABLE tasks ADD COLUMN rejection_reason TEXT",
            "model": "ALTER TABLE tasks ADD COLUMN model VARCHAR(100)",
            "effort": "ALTER TABLE tasks ADD COLUMN effort VARCHAR(10)",
            "sort_order": "ALTER TABLE tasks ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
        }
        for col, ddl in task_new_cols.items():
            if col not in task_cols:
                conn.execute(text(ddl))
        conn.commit()

        # Backfill title from prompt, project_name from project
        conn.execute(text(
            "UPDATE tasks SET title = COALESCE(SUBSTR(prompt, 1, 300), '') "
            "WHERE title = '' AND prompt IS NOT NULL"
        ))
        conn.execute(text(
            "UPDATE tasks SET project_name = project "
            "WHERE project_name IS NULL AND project IS NOT NULL"
        ))
        # Migrate COMPLETED → COMPLETE
        conn.execute(text(
            "UPDATE tasks SET status = 'COMPLETE' WHERE status = 'COMPLETED'"
        ))
        conn.commit()

        # Add skip_permissions, sync_mode to tasks if missing
        task_cols3 = _table_columns(conn, "tasks")
        if "skip_permissions" not in task_cols3:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN skip_permissions BOOLEAN NOT NULL DEFAULT 1"))
        if "sync_mode" not in task_cols3:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN sync_mode BOOLEAN NOT NULL DEFAULT 0"))
        if "scheduled_at" not in task_cols3:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN scheduled_at DATETIME"))
        conn.commit()

        # Migrate scheduled_at → notify_at + dispatch_at on tasks
        task_cols_sched = _table_columns(conn, "tasks")
        if "notify_at" not in task_cols_sched:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN notify_at DATETIME"))
        if "dispatch_at" not in task_cols_sched:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN dispatch_at DATETIME"))
        conn.commit()

        if "scheduled_at" in _table_columns(conn, "tasks"):
            # Data migration: INBOX → notify_at, PLANNING+project → dispatch_at
            conn.execute(text(
                "UPDATE tasks SET notify_at = scheduled_at "
                "WHERE scheduled_at IS NOT NULL AND status = 'INBOX'"
            ))
            conn.execute(text(
                "UPDATE tasks SET dispatch_at = scheduled_at "
                "WHERE scheduled_at IS NOT NULL AND status = 'PLANNING' "
                "AND project_name IS NOT NULL AND project_name != ''"
            ))
            conn.execute(text(
                "UPDATE tasks SET notify_at = scheduled_at "
                "WHERE scheduled_at IS NOT NULL AND status = 'PLANNING' "
                "AND (project_name IS NULL OR project_name = '')"
            ))
            conn.commit()
            conn.execute(text("ALTER TABLE tasks DROP COLUMN scheduled_at"))
            conn.commit()

        # Add deferred_to column to tasks if missing
        if "deferred_to" not in _table_columns(conn, "tasks"):
            conn.execute(text("ALTER TABLE tasks ADD COLUMN deferred_to DATETIME"))
            conn.commit()

        # Add min_interval_seconds to attention_jobs if missing. The table
        # itself is created by create_all; this covers rows written by the
        # first build, which had no notification cooldown.
        if _table_columns(conn, "attention_jobs"):
            if "min_interval_seconds" not in _table_columns(conn, "attention_jobs"):
                conn.execute(text(
                    "ALTER TABLE attention_jobs ADD COLUMN min_interval_seconds INTEGER"
                ))
                conn.commit()
            # Backfill existing condition-driven jobs, which are the ones that
            # could spam. 300s matches DEFAULT_SIGNAL_COOLDOWN_SECONDS.
            conn.execute(text(
                "UPDATE attention_jobs SET min_interval_seconds = 300 "
                "WHERE trigger_type = 'signal' AND min_interval_seconds IS NULL"
            ))
            conn.commit()

        # Add task_id column to agents if missing
        agent_cols = _table_columns(conn, "agents")
        if "task_id" not in agent_cols:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN task_id VARCHAR(12)"
            ))
            conn.commit()

        if "is_subagent" not in agent_cols:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN is_subagent BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()
        # Backfill: any row with parent_id set is by definition a subagent.
        # The original migration above only sets the default; legacy rows
        # created before the column existed would otherwise leak into the
        # top-level agent list because list endpoints filter on is_subagent.
        conn.execute(text(
            "UPDATE agents SET is_subagent = 1 "
            "WHERE parent_id IS NOT NULL AND is_subagent = 0"
        ))
        conn.commit()
        if "claude_agent_id" not in agent_cols:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN claude_agent_id VARCHAR(30)"
            ))
            conn.commit()
        if "generating_msg_id" not in agent_cols:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN generating_msg_id VARCHAR(36)"
            ))
            conn.commit()

        # Fix invalid model names in projects and agents
        _valid_list = ", ".join(f"'{m}'" for m in VALID_MODELS)
        result = conn.execute(text(
            f"UPDATE projects SET default_model = :fallback "
            f"WHERE default_model NOT IN ({_valid_list})"
        ), {"fallback": CC_MODEL})
        if result.rowcount:
            conn.commit()
        result = conn.execute(text(
            f"UPDATE agents SET model = :fallback "
            f"WHERE model IS NOT NULL AND model NOT IN ({_valid_list})"
        ), {"fallback": CC_MODEL})
        if result.rowcount:
            conn.commit()

        task_cols = _table_columns(conn, "tasks")
        if "try_base_commit" not in task_cols:
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN try_base_commit VARCHAR(50)"
            ))
            conn.commit()

        # Add auto_progress_summary column to projects if missing
        proj_cols2 = _table_columns(conn, "projects")
        if "auto_progress_summary" not in proj_cols2:
            conn.execute(text(
                "ALTER TABLE projects ADD COLUMN auto_progress_summary BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()

        # Add ai_insights column to projects if missing
        proj_cols3 = _table_columns(conn, "projects")
        if "ai_insights" not in proj_cols3:
            conn.execute(text(
                "ALTER TABLE projects ADD COLUMN ai_insights BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()

        if "use_worktree" not in task_cols:
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN use_worktree BOOLEAN NOT NULL DEFAULT 1"
            ))
            conn.commit()

        if "use_tmux" not in task_cols:
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN use_tmux BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()

        # --- Unique index on agents.session_id ---
        # Enforces one-agent-per-session at the DB level, preventing
        # cross-agent session theft even if application logic races.
        agent_indexes = {r[1] for r in conn.execute(text(
            "PRAGMA index_list(agents)"
        )).fetchall()}
        if "uq_agents_session_id" not in agent_indexes:
            # Clean up duplicate session_ids before adding unique index.
            # Keep the most recently active agent for each session_id,
            # NULL out the rest.
            conn.execute(text("""
                UPDATE agents SET session_id = NULL
                WHERE session_id IS NOT NULL
                  AND id NOT IN (
                    SELECT id FROM (
                      SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY session_id
                        ORDER BY last_message_at DESC NULLS LAST, created_at DESC
                      ) AS rn
                      FROM agents
                      WHERE session_id IS NOT NULL
                    ) ranked WHERE rn = 1
                  )
            """))
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_agents_session_id "
                "ON agents(session_id) WHERE session_id IS NOT NULL"
            ))
            conn.commit()

        # --- Add agent_id column to progress_insights if missing ---
        if "progress_insights" in [r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()]:
            pi_cols = _table_columns(conn, "progress_insights")
            if "agent_id" not in pi_cols:
                conn.execute(text(
                    "ALTER TABLE progress_insights ADD COLUMN agent_id VARCHAR(12) "
                    "REFERENCES agents(id) ON DELETE SET NULL"
                ))
                conn.commit()

        # --- Add jsonl_uuid column to messages if missing ---
        msg_cols = _table_columns(conn, "messages")
        if "jsonl_uuid" not in msg_cols:
            conn.execute(text(
                "ALTER TABLE messages ADD COLUMN jsonl_uuid VARCHAR(50)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_messages_jsonl_uuid "
                "ON messages(jsonl_uuid) WHERE jsonl_uuid IS NOT NULL"
            ))
            conn.commit()

        # --- Add delivered_at column to messages if missing ---
        if "delivered_at" not in msg_cols:
            conn.execute(text(
                "ALTER TABLE messages ADD COLUMN delivered_at DATETIME"
            ))
            conn.commit()

        # --- Add dispatch_seq column to messages if missing ---
        msg_cols2 = _table_columns(conn, "messages")
        if "dispatch_seq" not in msg_cols2:
            conn.execute(text(
                "ALTER TABLE messages ADD COLUMN dispatch_seq INTEGER"
            ))
            conn.commit()

        # NOTE: deleted blind delivered_at backfill (was here, removed
        # 2026-04-30). It used to UPDATE messages SET delivered_at =
        # created_at WHERE delivered_at IS NULL AND status != 'PENDING'
        # on every startup, fabricating "delivered" timestamps for messages
        # that sync_engine had not yet confirmed via JSONL. Combined with
        # main._migrate_pre_sent_legacy's SENT→COMPLETED branch it produced
        # 12 ghost-delivered messages over an 8-day window. Verified zero
        # call rate via GHOST_PROBE telemetry across 52 restarts before
        # deletion. delivered_at is now exclusively written by sync_engine
        # (the original design intent).

        # --- progress_insights compound index for existing DBs ---
        if "progress_insights" in [r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()]:
            existing_indexes = {r[1] for r in conn.execute(text(
                "PRAGMA index_list(progress_insights)"
            )).fetchall()}
            if "ix_progress_project_date" not in existing_indexes:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_progress_project_date "
                    "ON progress_insights(project, date)"
                ))
                conn.commit()

        # --- Add has_pending_suggestions column to agents if missing ---
        agent_cols_suggestions = _table_columns(conn, "agents")
        if "has_pending_suggestions" not in agent_cols_suggestions:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN has_pending_suggestions BOOLEAN NOT NULL DEFAULT 0"
            ))
            conn.commit()

        # --- Add insight_status column to agents if missing ---
        if "insight_status" not in agent_cols_suggestions:
            conn.execute(text(
                "ALTER TABLE agents ADD COLUMN insight_status VARCHAR(20)"
            ))
            conn.commit()

        # --- Persisted context-usage snapshot on agents ---
        # Avoid recomputing get_context_breakdown on every chat-page open;
        # emit_context_usage writes these alongside its WS broadcast.
        agent_cols_ctx = _table_columns(conn, "agents")
        if "context_total" not in agent_cols_ctx:
            conn.execute(text("ALTER TABLE agents ADD COLUMN context_total INTEGER"))
            conn.commit()
        if "context_limit" not in agent_cols_ctx:
            conn.execute(text("ALTER TABLE agents ADD COLUMN context_limit INTEGER"))
            conn.commit()
        if "context_percent" not in agent_cols_ctx:
            conn.execute(text("ALTER TABLE agents ADD COLUMN context_percent REAL"))
            conn.commit()
        if "context_captured_at" not in agent_cols_ctx:
            conn.execute(text("ALTER TABLE agents ADD COLUMN context_captured_at DATETIME"))
            conn.commit()
        if "context_breakdown" not in agent_cols_ctx:
            conn.execute(text("ALTER TABLE agents ADD COLUMN context_breakdown TEXT"))
            conn.commit()

        # --- progress_insights FTS5 ---
        tables = [r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()]
        if "progress_insights_fts" not in tables:
            conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS progress_insights_fts "
                "USING fts5(content, content_rowid='id', tokenize='porter unicode61')"
            ))
            # Backfill existing rows if the table was just created
            if "progress_insights" in tables:
                conn.execute(text(
                    "INSERT INTO progress_insights_fts(rowid, content) "
                    "SELECT id, content FROM progress_insights"
                ))
            conn.commit()

        # --- Fix 5: Deduplicate existing rows with same (agent_id, jsonl_uuid) ---
        # Keep the best row per group: prefer non-null metadata, longer content,
        # later timestamps, smaller id as tie-breaker.
        conn.execute(text("""
            DELETE FROM messages WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                        ROW_NUMBER() OVER (
                            PARTITION BY agent_id, jsonl_uuid
                            ORDER BY
                                CASE WHEN metadata IS NOT NULL THEN 0 ELSE 1 END,
                                LENGTH(content) DESC,
                                completed_at DESC NULLS LAST,
                                delivered_at DESC NULLS LAST,
                                id ASC
                        ) AS rn
                    FROM messages
                    WHERE jsonl_uuid IS NOT NULL
                ) ranked WHERE rn > 1
            )
        """))
        conn.commit()

        # Clean up any remaining hook-* UUID messages (legacy from when
        # hooks created messages directly)
        conn.execute(text("""
            DELETE FROM messages WHERE jsonl_uuid LIKE 'hook-%'
        """))
        conn.commit()

        # --- Fix 2: Unique partial index on (agent_id, jsonl_uuid) ---
        # Prevents duplicate message rows even if application-level dedup fails.
        # Drop the old index (with hook-% exclusion) and create a simpler one.
        conn.execute(text("""
            DROP INDEX IF EXISTS uq_messages_agent_jsonl_uuid
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_agent_jsonl_uuid
            ON messages(agent_id, jsonl_uuid)
            WHERE jsonl_uuid IS NOT NULL
        """))
        conn.commit()

        # --- Add tool_use_id and session_seq columns to messages ---
        msg_cols_new = _table_columns(conn, "messages")
        if "tool_use_id" not in msg_cols_new:
            conn.execute(text("ALTER TABLE messages ADD COLUMN tool_use_id VARCHAR(100)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_messages_tool_use_id "
                "ON messages(agent_id, tool_use_id) WHERE tool_use_id IS NOT NULL"
            ))
            conn.commit()

        if "session_seq" not in msg_cols_new:
            conn.execute(text("ALTER TABLE messages ADD COLUMN session_seq INTEGER"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_messages_agent_session_seq "
                "ON messages(agent_id, session_seq) WHERE session_seq IS NOT NULL"
            ))
            conn.commit()

        if "kind" not in msg_cols_new:
            conn.execute(text("ALTER TABLE messages ADD COLUMN kind VARCHAR(20)"))
            conn.commit()
            # One-time: clear legacy cli messages so sync reimports them
            # with fine-grained kind values. JSONL is source of truth.
            _legacy = conn.execute(text(
                "SELECT COUNT(*) FROM messages WHERE source = 'cli'"
            )).scalar()
            if _legacy and _legacy > 0:
                conn.execute(text(
                    "DELETE FROM messages WHERE source = 'cli'"
                ))
                conn.commit()
                logger.info("migration: cleared %d legacy cli messages for fine-grained reimport", _legacy)

        # --- Drop legacy tool_activities table (replaced by Message pipeline) ---
        if "tool_activities" in [t[0] for t in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()]:
            conn.execute(text("DROP TABLE tool_activities"))
            conn.commit()
            logger.info("migration: dropped legacy tool_activities table")

        # --- Backfill tool_use_id from metadata JSON ---
        try:
            _unfilled_tid = conn.execute(text(
                "SELECT COUNT(*) FROM messages "
                "WHERE tool_use_id IS NULL AND metadata IS NOT NULL "
                "AND metadata LIKE '%tool_use_id%'"
            )).scalar()
            if _unfilled_tid:
                conn.execute(text("""
                    UPDATE messages
                    SET tool_use_id = json_extract(metadata, '$.interactive[0].tool_use_id')
                    WHERE tool_use_id IS NULL
                      AND metadata IS NOT NULL
                      AND json_extract(metadata, '$.interactive[0].tool_use_id') IS NOT NULL
                """))
                conn.commit()
                logger.info("Backfilled tool_use_id for %d messages", _unfilled_tid)
        except OperationalError as e:
            logger.warning("Could not backfill tool_use_id (JSON1 unavailable?): %s", e)

        # --- Backfill session_seq from existing ordering ---
        _unfilled_seq = conn.execute(text(
            "SELECT COUNT(*) FROM messages WHERE session_seq IS NULL"
        )).scalar()
        if _unfilled_seq:
            conn.execute(text("""
                UPDATE messages SET session_seq = (
                    SELECT rn - 1 FROM (
                        SELECT id,
                            ROW_NUMBER() OVER (
                                PARTITION BY agent_id
                                ORDER BY COALESCE(delivered_at, '9999-12-31'), created_at
                            ) AS rn
                        FROM messages
                    ) ranked
                    WHERE ranked.id = messages.id
                )
            """))
            conn.commit()
            logger.info("Backfilled session_seq for %d messages", _unfilled_seq)

        # --- Add display_seq column to messages if missing ---
        msg_cols_disp = _table_columns(conn, "messages")
        if "display_seq" not in msg_cols_disp:
            conn.execute(text(
                "ALTER TABLE messages ADD COLUMN display_seq INTEGER"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_messages_display_seq "
                "ON messages(agent_id, display_seq)"
            ))
            # Backfill: assign display_seq from row number ordered by created_at
            conn.execute(text("""
                UPDATE messages SET display_seq = sub.rn
                FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY agent_id
                               ORDER BY created_at ASC
                           ) AS rn
                    FROM messages
                ) sub
                WHERE messages.id = sub.id
            """))
            conn.commit()

        # --- Add note column to tasks if missing ---
        task_cols_note = _table_columns(conn, "tasks")
        if "note" not in task_cols_note:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN note TEXT"))
            conn.commit()

        # --- Add last_ack_at column to push_subscriptions if missing ---
        push_cols = _table_columns(conn, "push_subscriptions")
        if "last_ack_at" not in push_cols:
            conn.execute(text(
                "ALTER TABLE push_subscriptions ADD COLUMN last_ack_at DATETIME"
            ))
            conn.commit()

        # --- Per-device notification switches on push_subscriptions ---
        # The bell used to flip global SystemConfig flags for every device.
        # Seed existing rows from those flags (stored "0"/"1", missing =
        # enabled) so a device muted through the old switch stays muted.
        # ALTER + seed run in one explicit transaction: pysqlite autocommits
        # bare DDL, so a failed seed (SQLITE_BUSY, kill) would otherwise
        # leave the column at DEFAULT 1 and the next startup skip seeding.
        for col, key in (("agents_enabled", "notifications_agents_enabled"),
                         ("tasks_enabled", "notifications_tasks_enabled")):
            if col not in push_cols:
                conn.commit()  # end the implicit read txn before BEGIN
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    conn.execute(text(
                        f"ALTER TABLE push_subscriptions ADD COLUMN {col} "
                        "BOOLEAN NOT NULL DEFAULT 1"
                    ))
                    row = conn.execute(text(
                        "SELECT value FROM system_config WHERE key = :k"
                    ), {"k": key}).first()
                    enabled = row is None or row[0] != "0"
                    conn.execute(text(
                        f"UPDATE push_subscriptions SET {col} = :v"
                    ), {"v": 1 if enabled else 0})
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                logger.info("migration: push_subscriptions.%s seeded from %s=%s",
                            col, key, enabled)
        if "device_label" not in push_cols:
            conn.execute(text(
                "ALTER TABLE push_subscriptions ADD COLUMN device_label VARCHAR(60)"
            ))
            conn.commit()
        if "last_seen_at" not in push_cols:
            conn.execute(text(
                "ALTER TABLE push_subscriptions ADD COLUMN last_seen_at DATETIME"
            ))
            conn.commit()
        if "unacked_sends" not in push_cols:
            conn.execute(text(
                "ALTER TABLE push_subscriptions ADD COLUMN unacked_sends "
                "INTEGER NOT NULL DEFAULT 0"
            ))
            conn.commit()

        # --- Add 5m / 1h cache_creation split columns to cc_sessions ---
        # Anthropic charges 1h ephemeral cache writes at 2× input vs 1.25×
        # for 5m. JSONL exposes the split via cache_creation.ephemeral_*_input_tokens.
        cc_cols = _table_columns(conn, "cc_sessions")
        if "total_cache_creation_5m_tokens" not in cc_cols:
            conn.execute(text(
                "ALTER TABLE cc_sessions ADD COLUMN total_cache_creation_5m_tokens "
                "INTEGER NOT NULL DEFAULT 0"
            ))
            conn.commit()
        if "total_cache_creation_1h_tokens" not in cc_cols:
            conn.execute(text(
                "ALTER TABLE cc_sessions ADD COLUMN total_cache_creation_1h_tokens "
                "INTEGER NOT NULL DEFAULT 0"
            ))
            conn.commit()

        # --- Migrate SYNCING → IDLE agent status (tmux-only agents) ---
        _syncing_count = conn.execute(text(
            "SELECT COUNT(*) FROM agents WHERE status = 'SYNCING'"
        )).scalar()
        if _syncing_count:
            conn.execute(text(
                "UPDATE agents SET status = 'IDLE' WHERE status = 'SYNCING'"
            ))
            conn.commit()
            logger.info("migration: migrated %d agents from SYNCING to IDLE", _syncing_count)

        # --- Drop probes.notify_user (probe-specific push channel removed) ---
        # Probe responses now notify via the standard agent stop hook (message
        # channel, respects per-agent mute and per-device switches) instead of
        # the dedicated probe channel that bypassed all gates.
        if "probes" in [r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()]:
            probe_cols = _table_columns(conn, "probes")
            if "notify_user" in probe_cols:
                conn.execute(text("ALTER TABLE probes DROP COLUMN notify_user"))
                conn.commit()
                logger.info("migration: dropped probes.notify_user column")

        # --- Auto-expire probes when their target agent transitions to
        # STOPPED or ERROR.
        #
        # A probe to a stopped agent has no useful path (Claude isn't running,
        # tmux send-keys is a no-op, no stop hook to fire notify). Tying probe
        # lifecycle to agent liveness via SQL trigger covers ALL paths that
        # set agent.status — application code (5+ sites) doesn't need to know
        # about probes at all.
        #
        # Drop-and-recreate is idempotent and lets us evolve the trigger body
        # without a separate migration.
        conn.execute(text("DROP TRIGGER IF EXISTS expire_probes_on_agent_stop"))
        conn.execute(text("""
            CREATE TRIGGER expire_probes_on_agent_stop
            AFTER UPDATE OF status ON agents
            WHEN NEW.status IN ('STOPPED', 'ERROR')
             AND OLD.status NOT IN ('STOPPED', 'ERROR')
            BEGIN
                UPDATE probes
                SET expires_at = datetime('now')
                WHERE agent_id = NEW.id
                  AND fired_at IS NULL
                  AND expires_at > datetime('now');
            END
        """))
        conn.commit()

    # Ensure jwt_secret exists in SystemConfig
    from auth import get_jwt_secret
    db = SessionLocal()
    try:
        get_jwt_secret(db)
    finally:
        db.close()
