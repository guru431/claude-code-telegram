"""Persistent session storage implementation.

Replaces the in-memory session storage with SQLite persistence.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

import aiosqlite
import structlog

from ..claude.session import ClaudeSession, SessionStorage
from .database import DatabaseManager
from .models import SessionModel

logger = structlog.get_logger()


class SQLiteSessionStorage(SessionStorage):
    """SQLite-based session storage."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize with database manager."""
        self.db_manager = db_manager

    async def _ensure_user_exists(
        self,
        conn: aiosqlite.Connection,
        user_id: int,
        username: Optional[str] = None,
    ) -> None:
        """Ensure user exists in database before creating session.

        Runs on the caller-supplied ``conn`` so the user-insert and the
        session-upsert share a single transaction; the caller owns the commit.
        This prevents a committed orphan user row when the session upsert fails.
        """
        # Check if user exists
        cursor = await conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        )
        user_exists = await cursor.fetchone()

        if not user_exists:
            # Create user record. ``is_allowed`` defaults to FALSE in
            # the schema — authorization is granted by the
            # ``AuthenticationManager`` (whitelist/token providers),
            # not by the act of creating a session record. Leaving it
            # FALSE here prevents this code path from acting as an
            # implicit allowlist bypass.
            now = datetime.now(UTC)
            cursor = await conn.execute(
                """
                INSERT INTO users
                (user_id, telegram_username, first_seen, last_active)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (
                    user_id,
                    username,
                    now,
                    now,
                ),
            )

            # Only log a genuine insert: ON CONFLICT DO NOTHING reports
            # rowcount 0 when a concurrent insert already created the row.
            if cursor.rowcount:
                logger.info(
                    "Created user record for session",
                    user_id=user_id,
                    username=username,
                )

    async def save_session(self, session: ClaudeSession) -> None:
        """Save session to database."""
        session_model = SessionModel(
            session_id=session.session_id,
            user_id=session.user_id,
            project_path=str(session.project_path),
            created_at=session.created_at,
            last_used=session.last_used,
            total_cost=session.total_cost,
            total_turns=session.total_turns,
            message_count=session.message_count,
        )

        async with self.db_manager.get_connection() as conn:
            # Ensure the user row exists in the SAME transaction as the session
            # upsert so a failed upsert cannot leave a committed orphan user.
            await self._ensure_user_exists(conn, session.user_id)

            # Single race-safe upsert: avoids the UPDATE-then-INSERT PK race of
            # two separate pooled connections, and restores is_active=TRUE so a
            # session marked inactive by cleanup/eviction becomes visible to
            # listings and the max_sessions count again when it is re-saved
            # (it is still resumable).
            await conn.execute(
                """
                INSERT INTO sessions
                (session_id, user_id, project_path, created_at, last_used,
                 total_cost, total_turns, message_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    last_used = excluded.last_used,
                    total_cost = excluded.total_cost,
                    total_turns = excluded.total_turns,
                    message_count = excluded.message_count,
                    is_active = TRUE
            """,
                (
                    session_model.session_id,
                    session_model.user_id,
                    session_model.project_path,
                    session_model.created_at,
                    session_model.last_used,
                    session_model.total_cost,
                    session_model.total_turns,
                    session_model.message_count,
                ),
            )

            await conn.commit()

        logger.debug(
            "Session saved to database",
            session_id=session.session_id,
            user_id=session.user_id,
        )

    async def load_session(
        self, session_id: str, user_id: int
    ) -> Optional[ClaudeSession]:
        """Load session from database, filtered by user ownership."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            )
            row = await cursor.fetchone()

            if not row:
                return None

            session_model = SessionModel.from_row(row)

            # Convert to ClaudeSession
            claude_session = ClaudeSession(
                session_id=session_model.session_id,
                user_id=session_model.user_id,
                project_path=Path(session_model.project_path),
                created_at=session_model.created_at,
                last_used=session_model.last_used,
                total_cost=session_model.total_cost,
                total_turns=session_model.total_turns,
                message_count=session_model.message_count,
                tools_used=[],  # Tools are tracked separately in tool_usage table
            )

            logger.debug(
                "Session loaded from database",
                session_id=session_id,
                user_id=claude_session.user_id,
            )

            return claude_session

    async def delete_session(self, session_id: str) -> None:
        """Delete session from database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE sessions SET is_active = FALSE WHERE session_id = ?",
                (session_id,),
            )
            await conn.commit()

        logger.debug("Session marked as inactive", session_id=session_id)

    async def get_user_sessions(self, user_id: int) -> List[ClaudeSession]:
        """Get all active sessions for a user."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM sessions
                WHERE user_id = ? AND is_active = TRUE
                ORDER BY last_used DESC
            """,
                (user_id,),
            )
            rows = await cursor.fetchall()

            sessions = []
            for row in rows:
                session_model = SessionModel.from_row(row)
                claude_session = ClaudeSession(
                    session_id=session_model.session_id,
                    user_id=session_model.user_id,
                    project_path=Path(session_model.project_path),
                    created_at=session_model.created_at,
                    last_used=session_model.last_used,
                    total_cost=session_model.total_cost,
                    total_turns=session_model.total_turns,
                    message_count=session_model.message_count,
                    tools_used=[],  # Tools are tracked separately
                )
                sessions.append(claude_session)

            return sessions

    async def get_all_sessions(self) -> List[ClaudeSession]:
        """Get all active sessions."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE is_active = TRUE ORDER BY last_used DESC"
            )
            rows = await cursor.fetchall()

            sessions = []
            for row in rows:
                session_model = SessionModel.from_row(row)
                claude_session = ClaudeSession(
                    session_id=session_model.session_id,
                    user_id=session_model.user_id,
                    project_path=Path(session_model.project_path),
                    created_at=session_model.created_at,
                    last_used=session_model.last_used,
                    total_cost=session_model.total_cost,
                    total_turns=session_model.total_turns,
                    message_count=session_model.message_count,
                    tools_used=[],  # Tools are tracked separately
                )
                sessions.append(claude_session)

            return sessions

    async def cleanup_expired_sessions(self, timeout_hours: int) -> int:
        """Mark expired sessions as inactive."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET is_active = FALSE
                WHERE datetime(last_used) < datetime('now', '-' || ? || ' hours')
                  AND is_active = TRUE
            """,
                (timeout_hours,),
            )
            await conn.commit()

            affected = cursor.rowcount
            logger.info(
                "Cleaned up expired sessions",
                count=affected,
                timeout_hours=timeout_hours,
            )
            return affected
