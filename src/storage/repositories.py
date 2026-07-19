"""Data access layer using repository pattern.

Features:
- Clean data access API
- Query optimization
- Error handling
"""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import aiosqlite
import structlog

from .database import DatabaseManager
from .models import (
    AuditLogModel,
    CostTrackingModel,
    MessageModel,
    ProjectThreadModel,
    SessionModel,
    ToolUsageModel,
    UserModel,
)

logger = structlog.get_logger()


@asynccontextmanager
async def _conn_ctx(
    db_manager: DatabaseManager, conn: Optional[aiosqlite.Connection]
) -> AsyncIterator[aiosqlite.Connection]:
    """Yield a connection, committing only when we own the borrow.

    When ``conn`` is provided the caller owns the transaction (shared
    multi-write transaction in :meth:`Storage.save_claude_interaction`), so we
    neither commit nor close. When ``conn`` is None we borrow from the pool and
    commit on success, mirroring the standalone repository behaviour.
    """
    if conn is not None:
        yield conn
        return
    async with db_manager.get_connection() as owned:
        yield owned
        await owned.commit()


class UserRepository:
    """User data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user(self, user_id: int) -> Optional[UserModel]:
        """Get user by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return UserModel.from_row(row) if row else None

    async def create_user(self, user: UserModel) -> UserModel:
        """Create new user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO users
                (user_id, telegram_username, first_seen,
                 last_active, is_allowed)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
            """,
                (
                    user.user_id,
                    user.telegram_username,
                    user.first_seen or datetime.now(UTC),
                    user.last_active or datetime.now(UTC),
                    user.is_allowed,
                ),
            )
            await conn.commit()

            if cursor.rowcount:
                logger.info(
                    "Created user",
                    user_id=user.user_id,
                    username=user.telegram_username,
                )
            return user

    async def update_user(self, user: UserModel):
        """Update user data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET telegram_username = ?, last_active = ?,
                    total_cost = ?, message_count = ?, session_count = ?
                WHERE user_id = ?
            """,
                (
                    user.telegram_username,
                    user.last_active or datetime.now(UTC),
                    user.total_cost,
                    user.message_count,
                    user.session_count,
                    user.user_id,
                ),
            )
            await conn.commit()

    async def increment_stats(
        self,
        user_id: int,
        cost: float,
        messages: int = 1,
        last_active: Optional[datetime] = None,
        conn: Optional[aiosqlite.Connection] = None,
    ):
        """Atomically increment user cost/message counters.

        Avoids the lost-update race of read-modify-write under concurrent
        interactions (mirrors CostTrackingRepository.update_daily_cost). When
        ``conn`` is supplied the write joins the caller's transaction and is
        not committed here.
        """
        async with _conn_ctx(self.db, conn) as c:
            await c.execute(
                """
                UPDATE users
                SET total_cost = total_cost + ?,
                    message_count = message_count + ?,
                    last_active = ?
                WHERE user_id = ?
            """,
                (cost, messages, last_active or datetime.now(UTC), user_id),
            )

    async def increment_session_count(
        self,
        user_id: int,
        last_active: Optional[datetime] = None,
    ):
        """Atomically increment a user's session counter.

        Owns only the ``session_count``/``last_active`` columns so it can run
        concurrently with :meth:`increment_stats` without the lost-update race
        of a read-modify-write through :meth:`update_user`.
        """
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET session_count = session_count + 1,
                    last_active = ?
                WHERE user_id = ?
            """,
                (last_active or datetime.now(UTC), user_id),
            )
            await conn.commit()

    async def get_allowed_users(self) -> List[int]:
        """Get list of allowed user IDs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE is_allowed = TRUE"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def set_user_allowed(self, user_id: int, allowed: bool):
        """Set user allowed status."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE users SET is_allowed = ? WHERE user_id = ?", (allowed, user_id)
            )
            await conn.commit()

            logger.info("Updated user permissions", user_id=user_id, allowed=allowed)

    async def get_all_users(self) -> List[UserModel]:
        """Get all users."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM users ORDER BY first_seen DESC")
            rows = await cursor.fetchall()
            return [UserModel.from_row(row) for row in rows]


class SessionRepository:
    """Session data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_session(self, session_id: str) -> Optional[SessionModel]:
        """Get session by ID."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = await cursor.fetchone()
            return SessionModel.from_row(row) if row else None

    async def create_session(self, session: SessionModel) -> SessionModel:
        """Create new session."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO sessions
                (session_id, user_id, project_path, created_at, last_used)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    session.session_id,
                    session.user_id,
                    session.project_path,
                    session.created_at,
                    session.last_used,
                ),
            )
            await conn.commit()

            logger.info(
                "Created session",
                session_id=session.session_id,
                user_id=session.user_id,
            )
            return session

    async def update_session(self, session: SessionModel):
        """Update session data."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                SET last_used = ?, total_cost = ?, total_turns = ?,
                    message_count = ?, is_active = ?
                WHERE session_id = ?
            """,
                (
                    session.last_used,
                    session.total_cost,
                    session.total_turns,
                    session.message_count,
                    session.is_active,
                    session.session_id,
                ),
            )
            await conn.commit()

    async def get_user_sessions(
        self, user_id: int, active_only: bool = True
    ) -> List[SessionModel]:
        """Get sessions for user."""
        async with self.db.get_connection() as conn:
            query = "SELECT * FROM sessions WHERE user_id = ?"
            params = [user_id]

            if active_only:
                query += " AND is_active = TRUE"

            query += " ORDER BY last_used DESC"

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [SessionModel.from_row(row) for row in rows]

    async def cleanup_old_sessions(self, days: int = 30) -> int:
        """Mark old sessions as inactive."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET is_active = FALSE
                WHERE datetime(last_used) < datetime('now', '-' || ? || ' days')
                  AND is_active = TRUE
            """,
                (days,),
            )
            await conn.commit()

            affected = cursor.rowcount
            logger.info("Cleaned up old sessions", count=affected, days=days)
            return affected

    async def get_sessions_by_project(self, project_path: str) -> List[SessionModel]:
        """Get sessions for a specific project."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM sessions
                WHERE project_path = ? AND is_active = TRUE
                ORDER BY last_used DESC
            """,
                (project_path,),
            )
            rows = await cursor.fetchall()
            return [SessionModel.from_row(row) for row in rows]


class ProjectThreadRepository:
    """Project-thread mapping data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_by_chat_thread(
        self, chat_id: int, message_thread_id: int
    ) -> Optional[ProjectThreadModel]:
        """Find active mapping by chat+thread."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM project_threads
                WHERE chat_id = ? AND message_thread_id = ? AND is_active = TRUE
            """,
                (chat_id, message_thread_id),
            )
            row = await cursor.fetchone()
            return ProjectThreadModel.from_row(row) if row else None

    async def get_by_chat_project(
        self, chat_id: int, project_slug: str
    ) -> Optional[ProjectThreadModel]:
        """Find mapping by chat+project slug."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM project_threads
                WHERE chat_id = ? AND project_slug = ?
            """,
                (chat_id, project_slug),
            )
            row = await cursor.fetchone()
            return ProjectThreadModel.from_row(row) if row else None

    async def upsert_mapping(
        self,
        project_slug: str,
        chat_id: int,
        message_thread_id: int,
        topic_name: str,
        is_active: bool = True,
    ) -> ProjectThreadModel:
        """Create or update mapping by unique chat+project key."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO project_threads (
                    project_slug, chat_id, message_thread_id, topic_name, is_active
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, project_slug) DO UPDATE SET
                    message_thread_id = excluded.message_thread_id,
                    topic_name = excluded.topic_name,
                    is_active = excluded.is_active,
                    updated_at = CURRENT_TIMESTAMP
            """,
                (project_slug, chat_id, message_thread_id, topic_name, is_active),
            )
            await conn.commit()

        mapping = await self.get_by_chat_project(
            chat_id=chat_id, project_slug=project_slug
        )
        if not mapping:
            raise RuntimeError("Failed to upsert project thread mapping")
        return mapping

    async def deactivate_missing_projects(
        self, chat_id: int, active_project_slugs: List[str]
    ) -> int:
        """Deactivate mappings for projects no longer enabled/present."""
        async with self.db.get_connection() as conn:
            if active_project_slugs:
                placeholders = ",".join("?" for _ in active_project_slugs)
                query = f"""
                    UPDATE project_threads
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ?
                      AND project_slug NOT IN ({placeholders})
                      AND is_active = TRUE
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    UPDATE project_threads
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ? AND is_active = TRUE
                """,
                    (chat_id,),
                )
            await conn.commit()
            return cursor.rowcount

    async def list_stale_active_mappings(
        self, chat_id: int, active_project_slugs: List[str]
    ) -> List[ProjectThreadModel]:
        """List active mappings that are no longer enabled/present."""
        async with self.db.get_connection() as conn:
            if active_project_slugs:
                placeholders = ",".join("?" for _ in active_project_slugs)
                query = f"""
                    SELECT * FROM project_threads
                    WHERE chat_id = ?
                      AND is_active = TRUE
                      AND project_slug NOT IN ({placeholders})
                    ORDER BY project_slug ASC
                """
                params = [chat_id] + active_project_slugs
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(
                    """
                    SELECT * FROM project_threads
                    WHERE chat_id = ? AND is_active = TRUE
                    ORDER BY project_slug ASC
                """,
                    (chat_id,),
                )
            rows = await cursor.fetchall()
            return [ProjectThreadModel.from_row(row) for row in rows]

    async def set_active(self, chat_id: int, project_slug: str, is_active: bool) -> int:
        """Set active flag for a mapping by chat+project."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE project_threads
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND project_slug = ?
            """,
                (is_active, chat_id, project_slug),
            )
            await conn.commit()
            return cursor.rowcount

    async def list_by_chat(
        self, chat_id: int, active_only: bool = True
    ) -> List[ProjectThreadModel]:
        """List mappings for a chat."""
        async with self.db.get_connection() as conn:
            query = "SELECT * FROM project_threads WHERE chat_id = ?"
            params = [chat_id]
            if active_only:
                query += " AND is_active = TRUE"
            query += " ORDER BY project_slug ASC"
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [ProjectThreadModel.from_row(row) for row in rows]


class MessageRepository:
    """Message data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_message(
        self, message: MessageModel, conn: Optional[aiosqlite.Connection] = None
    ) -> int:
        """Save message and return ID.

        When ``conn`` is supplied the insert joins the caller's transaction and
        is not committed here.
        """
        async with _conn_ctx(self.db, conn) as c:
            cursor = await c.execute(
                """
                INSERT INTO messages
                (session_id, user_id, timestamp, prompt,
                 response, cost, duration_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    message.session_id,
                    message.user_id,
                    message.timestamp,
                    message.prompt,
                    message.response,
                    message.cost,
                    message.duration_ms,
                    message.error,
                ),
            )
            return cursor.lastrowid

    async def purge_old_messages(self, days: int) -> int:
        """Delete messages older than ``days`` and return the count removed."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM messages
                WHERE datetime(timestamp) < datetime('now', '-' || ? || ' days')
            """,
                (days,),
            )
            await conn.commit()
            affected = cursor.rowcount
            logger.info("Purged old messages", count=affected, days=days)
            return affected

    async def get_session_messages(
        self, session_id: str, limit: int = 50
    ) -> List[MessageModel]:
        """Get messages for session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_user_messages(
        self, user_id: int, limit: int = 100
    ) -> List[MessageModel]:
        """Get messages for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]

    async def get_recent_messages(
        self, hours: int = 24, limit: int = 1000
    ) -> List[MessageModel]:
        """Get recent messages."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM messages
                WHERE datetime(timestamp) > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (hours, limit),
            )
            rows = await cursor.fetchall()
            return [MessageModel.from_row(row) for row in rows]


class ToolUsageRepository:
    """Tool usage data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def save_tool_usage(
        self, tool_usage: ToolUsageModel, conn: Optional[aiosqlite.Connection] = None
    ) -> int:
        """Save tool usage and return ID.

        When ``conn`` is supplied the insert joins the caller's transaction and
        is not committed here.
        """
        tool_input_json = (
            json.dumps(tool_usage.tool_input) if tool_usage.tool_input else None
        )
        async with _conn_ctx(self.db, conn) as c:
            cursor = await c.execute(
                """
                INSERT INTO tool_usage
                (session_id, message_id, tool_name, tool_input,
                 timestamp, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    tool_usage.session_id,
                    tool_usage.message_id,
                    tool_usage.tool_name,
                    tool_input_json,
                    tool_usage.timestamp,
                    tool_usage.success,
                    tool_usage.error_message,
                ),
            )
            return cursor.lastrowid

    async def save_tool_usages(
        self,
        tool_usages: List[ToolUsageModel],
        conn: Optional[aiosqlite.Connection] = None,
    ) -> None:
        """Batch-insert tool usage rows in a single executemany.

        When ``conn`` is supplied the inserts join the caller's transaction and
        are not committed here.
        """
        if not tool_usages:
            return
        params = [
            (
                tool_usage.session_id,
                tool_usage.message_id,
                tool_usage.tool_name,
                json.dumps(tool_usage.tool_input) if tool_usage.tool_input else None,
                tool_usage.timestamp,
                tool_usage.success,
                tool_usage.error_message,
            )
            for tool_usage in tool_usages
        ]
        async with _conn_ctx(self.db, conn) as c:
            await c.executemany(
                """
                INSERT INTO tool_usage
                (session_id, message_id, tool_name, tool_input,
                 timestamp, success, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                params,
            )

    async def purge_old_tool_usage(self, days: int) -> int:
        """Delete tool-usage rows older than ``days`` and return the count."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM tool_usage
                WHERE datetime(timestamp) < datetime('now', '-' || ? || ' days')
            """,
                (days,),
            )
            await conn.commit()
            affected = cursor.rowcount
            logger.info("Purged old tool usage", count=affected, days=days)
            return affected

    async def get_session_tool_usage(
        self, session_id: str, limit: int = 1000
    ) -> List[ToolUsageModel]:
        """Get tool usage for session."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM tool_usage
                WHERE session_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()
            return [ToolUsageModel.from_row(row) for row in rows]

    async def get_user_tool_usage(
        self, user_id: int, limit: int = 1000
    ) -> List[ToolUsageModel]:
        """Get tool usage for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT tu.* FROM tool_usage tu
                JOIN sessions s ON tu.session_id = s.session_id
                WHERE s.user_id = ?
                ORDER BY tu.timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [ToolUsageModel.from_row(row) for row in rows]

    async def get_tool_stats(self) -> List[Dict[str, Any]]:
        """Get tool usage statistics."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT
                    tool_name,
                    COUNT(*) as usage_count,
                    COUNT(DISTINCT session_id) as sessions_used,
                    SUM(CASE WHEN success = TRUE THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN success = FALSE THEN 1 ELSE 0 END) as error_count
                FROM tool_usage
                GROUP BY tool_name
                ORDER BY usage_count DESC
            """)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class AuditLogRepository:
    """Audit log data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def log_event(
        self, audit_log: AuditLogModel, conn: Optional[aiosqlite.Connection] = None
    ) -> int:
        """Log audit event and return ID.

        When ``conn`` is supplied the insert joins the caller's transaction and
        is not committed here.
        """
        event_data_json = (
            json.dumps(audit_log.event_data) if audit_log.event_data else None
        )
        async with _conn_ctx(self.db, conn) as c:
            cursor = await c.execute(
                """
                INSERT INTO audit_log
                (user_id, event_type, event_data, success, timestamp, ip_address,
                 risk_level, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    audit_log.user_id,
                    audit_log.event_type,
                    event_data_json,
                    audit_log.success,
                    audit_log.timestamp,
                    audit_log.ip_address,
                    audit_log.risk_level,
                    audit_log.session_id,
                ),
            )
            return cursor.lastrowid

    async def purge_old_audit_log(self, days: int) -> int:
        """Delete audit-log rows older than ``days`` and return the count."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM audit_log
                WHERE datetime(timestamp) < datetime('now', '-' || ? || ' days')
            """,
                (days,),
            )
            await conn.commit()
            affected = cursor.rowcount
            logger.info("Purged old audit log", count=affected, days=days)
            return affected

    async def get_user_audit_log(
        self, user_id: int, limit: int = 100
    ) -> List[AuditLogModel]:
        """Get audit log for user."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]

    async def get_recent_audit_log(self, hours: int = 24) -> List[AuditLogModel]:
        """Get recent audit log entries."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM audit_log
                WHERE datetime(timestamp) > datetime('now', '-' || ? || ' hours')
                ORDER BY timestamp DESC
            """,
                (hours,),
            )
            rows = await cursor.fetchall()
            return [AuditLogModel.from_row(row) for row in rows]


class CostTrackingRepository:
    """Cost tracking data access."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def update_daily_cost(
        self,
        user_id: int,
        cost: float,
        date: Optional[str] = None,
        conn: Optional[aiosqlite.Connection] = None,
    ):
        """Update daily cost for user.

        When ``conn`` is supplied the write joins the caller's transaction and
        is not committed here.
        """
        if not date:
            date = datetime.now(UTC).strftime("%Y-%m-%d")

        async with _conn_ctx(self.db, conn) as c:
            await c.execute(
                """
                INSERT INTO cost_tracking (user_id, date, daily_cost, request_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id, date)
                DO UPDATE SET
                    daily_cost = daily_cost + ?,
                    request_count = request_count + 1
            """,
                (user_id, date, cost, cost),
            )

    async def get_user_daily_costs(
        self, user_id: int, days: int = 30
    ) -> List[CostTrackingModel]:
        """Get user's daily costs."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM cost_tracking
                WHERE user_id = ? AND date >= date('now', '-' || ? || ' days')
                ORDER BY date DESC
            """,
                (user_id, days),
            )
            rows = await cursor.fetchall()
            return [CostTrackingModel.from_row(row) for row in rows]

    async def get_total_costs(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get total costs by day."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    date,
                    SUM(daily_cost) as total_cost,
                    SUM(request_count) as total_requests,
                    COUNT(DISTINCT user_id) as active_users
                FROM cost_tracking
                WHERE date >= date('now', '-' || ? || ' days')
                GROUP BY date
                ORDER BY date DESC
            """,
                (days,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class AnalyticsRepository:
    """Analytics and reporting."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Get user statistics."""
        async with self.db.get_connection() as conn:
            # User summary
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_messages,
                    COALESCE(SUM(cost), 0) as total_cost,
                    COALESCE(AVG(cost), 0) as avg_cost,
                    MAX(timestamp) as last_activity,
                    AVG(duration_ms) as avg_duration
                FROM messages
                WHERE user_id = ?
            """,
                (user_id,),
            )

            summary = dict(await cursor.fetchone())

            # Daily usage (last 30 days)
            cursor = await conn.execute(
                """
                SELECT
                    date(timestamp) as date,
                    COUNT(*) as messages,
                    SUM(cost) as cost,
                    COUNT(DISTINCT session_id) as sessions
                FROM messages
                WHERE user_id = ?
                  AND datetime(timestamp) >= datetime('now', '-30 days')
                GROUP BY date(timestamp)
                ORDER BY date DESC
            """,
                (user_id,),
            )

            daily_usage = [dict(row) for row in await cursor.fetchall()]

            # Most used tools
            cursor = await conn.execute(
                """
                SELECT
                    tu.tool_name,
                    COUNT(*) as usage_count
                FROM tool_usage tu
                JOIN sessions s ON tu.session_id = s.session_id
                WHERE s.user_id = ?
                GROUP BY tu.tool_name
                ORDER BY usage_count DESC
                LIMIT 10
            """,
                (user_id,),
            )

            top_tools = [dict(row) for row in await cursor.fetchall()]

            return {
                "summary": summary,
                "daily_usage": daily_usage,
                "top_tools": top_tools,
            }

    async def get_system_stats(self) -> Dict[str, Any]:
        """Get system-wide statistics."""
        async with self.db.get_connection() as conn:
            # Overall stats
            cursor = await conn.execute("""
                SELECT
                    COUNT(DISTINCT user_id) as total_users,
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_messages,
                    COALESCE(SUM(cost), 0) as total_cost,
                    AVG(duration_ms) as avg_duration
                FROM messages
            """)

            overall = dict(await cursor.fetchone())

            # Active users (last 7 days)
            cursor = await conn.execute("""
                SELECT COUNT(DISTINCT user_id) as active_users
                FROM messages
                WHERE datetime(timestamp) > datetime('now', '-7 days')
            """)

            active_users = (await cursor.fetchone())[0]
            overall["active_users_7d"] = active_users

            # Top users by cost
            cursor = await conn.execute("""
                SELECT
                    u.user_id,
                    u.telegram_username,
                    SUM(m.cost) as total_cost,
                    COUNT(m.message_id) as total_messages
                FROM messages m
                JOIN users u ON m.user_id = u.user_id
                GROUP BY u.user_id
                ORDER BY total_cost DESC
                LIMIT 10
            """)

            top_users = [dict(row) for row in await cursor.fetchall()]

            # Tool usage stats
            cursor = await conn.execute("""
                SELECT
                    tool_name,
                    COUNT(*) as usage_count,
                    COUNT(DISTINCT session_id) as sessions_used
                FROM tool_usage
                GROUP BY tool_name
                ORDER BY usage_count DESC
                LIMIT 10
            """)

            tool_stats = [dict(row) for row in await cursor.fetchall()]

            # Daily activity (last 30 days)
            cursor = await conn.execute("""
                SELECT
                    date(timestamp) as date,
                    COUNT(DISTINCT user_id) as active_users,
                    COUNT(*) as total_messages,
                    SUM(cost) as total_cost
                FROM messages
                WHERE datetime(timestamp) >= datetime('now', '-30 days')
                GROUP BY date(timestamp)
                ORDER BY date DESC
            """)

            daily_activity = [dict(row) for row in await cursor.fetchall()]

            return {
                "overall": overall,
                "top_users": top_users,
                "tool_stats": tool_stats,
                "daily_activity": daily_activity,
            }


class WebhookEventRepository:
    """Webhook-event data access (dedup/audit rows)."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize repository."""
        self.db = db_manager

    async def purge_old_webhook_events(self, days: int) -> int:
        """Delete webhook events older than ``days`` and return the count.

        The webhook_events table records ingestion time in ``received_at``.
        """
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM webhook_events
                WHERE datetime(received_at) < datetime('now', '-' || ? || ' days')
            """,
                (days,),
            )
            await conn.commit()
            affected = cursor.rowcount
            logger.info("Purged old webhook events", count=affected, days=days)
            return affected
