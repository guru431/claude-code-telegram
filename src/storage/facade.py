"""Unified storage interface.

Provides simple API for the rest of the application.
"""

from datetime import UTC, datetime
from typing import Any, Dict, Optional

import structlog

from ..claude.sdk_integration import ClaudeResponse
from .database import DatabaseManager
from .models import (
    AuditLogModel,
    MessageModel,
    SessionModel,
    ToolUsageModel,
    UserModel,
)
from .repositories import (
    AnalyticsRepository,
    AuditLogRepository,
    CostTrackingRepository,
    MessageRepository,
    ProjectThreadRepository,
    SessionRepository,
    ToolUsageRepository,
    UserRepository,
    WebhookEventRepository,
)

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(self, database_url: str):
        """Initialize storage with database URL."""
        self.db_manager = DatabaseManager(database_url)
        self.users = UserRepository(self.db_manager)
        self.sessions = SessionRepository(self.db_manager)
        self.project_threads = ProjectThreadRepository(self.db_manager)
        self.messages = MessageRepository(self.db_manager)
        self.tools = ToolUsageRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)
        self.costs = CostTrackingRepository(self.db_manager)
        self.analytics = AnalyticsRepository(self.db_manager)
        self.webhooks = WebhookEventRepository(self.db_manager)

    async def initialize(self):
        """Initialize storage system."""
        logger.info("Initializing storage system")
        await self.db_manager.initialize()
        logger.info("Storage system initialized")

    async def close(self):
        """Close storage connections."""
        logger.info("Closing storage system")
        await self.db_manager.close()

    async def health_check(self) -> bool:
        """Check storage system health."""
        return await self.db_manager.health_check()

    # High-level operations

    async def save_claude_interaction(
        self,
        user_id: int,
        session_id: str,
        prompt: str,
        response: ClaudeResponse,
        ip_address: Optional[str] = None,
    ):
        """Save complete Claude interaction in a single transaction.

        All writes (message, tool usage, daily cost, user stats, audit) are
        committed atomically on one borrowed connection so a crash mid-sequence
        cannot desync the counters.

        When ``session_id`` is empty/missing (a new session was created but no
        ``ResultMessage`` arrived) the ``messages``/``tool_usage`` rows have a
        FOREIGN KEY to ``sessions`` that cannot be satisfied, so they are
        skipped. The session-independent cost and audit writes (cost_tracking
        and audit_log are not FK-bound to sessions) still run, and the dropped
        message is logged at error level.
        """
        logger.info(
            "Saving Claude interaction",
            user_id=user_id,
            session_id=session_id,
            cost=response.cost,
        )

        now = datetime.now(UTC)
        has_session = bool(session_id)
        if not has_session:
            logger.error(
                "Empty session_id for Claude interaction; skipping FK-bound "
                "message/tool rows, persisting cost and audit only",
                user_id=user_id,
            )

        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type="claude_interaction",
            event_data={
                "session_id": session_id,
                "cost": response.cost,
                "duration_ms": response.duration_ms,
                "num_turns": response.num_turns,
                "is_error": response.is_error,
                "tools_used": [t["name"] for t in response.tools_used],
            },
            success=not response.is_error,
            timestamp=now,
            ip_address=ip_address,
        )

        async with self.db_manager.get_connection() as conn:
            try:
                if has_session:
                    # Save message (FK-bound to sessions).
                    message = MessageModel(
                        message_id=None,
                        session_id=session_id,
                        user_id=user_id,
                        timestamp=now,
                        prompt=prompt,
                        response=response.content,
                        cost=response.cost,
                        duration_ms=response.duration_ms,
                        error=response.error_type if response.is_error else None,
                    )
                    message_id = await self.messages.save_message(message, conn=conn)

                    # Save tool usage (FK-bound to sessions/messages).
                    if response.tools_used:
                        tool_usages = [
                            ToolUsageModel(
                                id=None,
                                session_id=session_id,
                                message_id=message_id,
                                tool_name=tool["name"],
                                tool_input=tool.get("input", {}),
                                timestamp=now,
                                success=not response.is_error,
                                error_message=(
                                    response.error_type if response.is_error else None
                                ),
                            )
                            for tool in response.tools_used
                        ]
                        await self.tools.save_tool_usages(tool_usages, conn=conn)

                # Update cost tracking (no session FK).
                await self.costs.update_daily_cost(user_id, response.cost, conn=conn)

                # Update user stats (atomic increment to avoid lost updates
                # under concurrent interactions). Session counters are owned by
                # SessionManager/SQLiteSessionStorage and must NOT be
                # incremented here.
                await self.users.increment_stats(
                    user_id, response.cost, messages=1, last_active=now, conn=conn
                )

                # Log audit event (no session FK).
                await self.audit.log_event(audit_event, conn=conn)

                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def get_or_create_user(
        self, user_id: int, username: Optional[str] = None
    ) -> UserModel:
        """Get or create user."""
        user = await self.users.get_user(user_id)

        if not user:
            logger.info("Creating new user", user_id=user_id, username=username)
            user = UserModel(
                user_id=user_id,
                telegram_username=username,
                first_seen=datetime.now(UTC),
                last_active=datetime.now(UTC),
                is_allowed=False,  # Default to not allowed
            )
            await self.users.create_user(user)
            # create_user is INSERT ON CONFLICT DO NOTHING and returns the
            # passed-in (stale) object; re-read to reflect actual DB state
            # (e.g. an existing row when a concurrent insert won the race).
            user = await self.users.get_user(user_id) or user

        return user

    async def create_session(
        self, user_id: int, project_path: str, session_id: str
    ) -> SessionModel:
        """Create new session."""
        session = SessionModel(
            session_id=session_id,
            user_id=user_id,
            project_path=project_path,
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )

        await self.sessions.create_session(session)

        # Atomically bump the session counter without a read-modify-write that
        # could clobber a concurrent increment_stats (which owns total_cost /
        # message_count). increment_session_count touches only the
        # create_session-owned columns.
        await self.users.increment_session_count(user_id, last_active=datetime.now(UTC))

        return session

    async def log_security_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
        ip_address: Optional[str] = None,
    ):
        """Log security-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def log_bot_event(
        self,
        user_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        success: bool = True,
    ):
        """Log bot-related event."""
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
            success=success,
            timestamp=datetime.now(UTC),
        )
        await self.audit.log_event(audit_event)

    # Convenience methods

    async def is_user_allowed(self, user_id: int) -> bool:
        """Check if user is allowed."""
        user = await self.users.get_user(user_id)
        return user.is_allowed if user else False

    async def get_user_session_summary(self, user_id: int) -> Dict[str, Any]:
        """Get user session summary."""
        sessions = await self.sessions.get_user_sessions(user_id, active_only=False)
        active_sessions = [s for s in sessions if s.is_active]

        return {
            "total_sessions": len(sessions),
            "active_sessions": len(active_sessions),
            "total_cost": sum(s.total_cost for s in sessions),
            "total_messages": sum(s.message_count for s in sessions),
            "projects": list(set(s.project_path for s in sessions)),
        }

    async def get_session_history(
        self, session_id: str, limit: int = 50
    ) -> Dict[str, Any]:
        """Get session history with messages and tools."""
        session = await self.sessions.get_session(session_id)
        if not session:
            return None

        messages = await self.messages.get_session_messages(session_id, limit)
        tools = await self.tools.get_session_tool_usage(session_id)

        return {
            "session": session.to_dict(),
            "messages": [m.to_dict() for m in messages],
            "tool_usage": [t.to_dict() for t in tools],
        }

    async def cleanup_old_data(
        self, days: int = 90, audit_days: int = 365
    ) -> Dict[str, int]:
        """Purge old data past the retention window.

        ``days`` governs sessions (marked inactive), and the hard DELETE purge
        of messages, tool_usage and webhook_events. ``audit_days`` governs the
        audit_log purge separately (audit rows are usually kept longer). A
        ``0`` value for either arg disables that purge (keep forever).
        Returns the count removed per table.
        """
        logger.info("Starting data cleanup", days=days, audit_days=audit_days)

        result: Dict[str, int] = {
            "sessions_cleaned": 0,
            "messages_purged": 0,
            "tool_usage_purged": 0,
            "webhook_events_purged": 0,
            "audit_log_purged": 0,
        }

        if days > 0:
            # Sessions are only flipped inactive (kept resumable), not deleted.
            result["sessions_cleaned"] = await self.sessions.cleanup_old_sessions(days)
            result["messages_purged"] = await self.messages.purge_old_messages(days)
            result["tool_usage_purged"] = await self.tools.purge_old_tool_usage(days)
            result["webhook_events_purged"] = (
                await self.webhooks.purge_old_webhook_events(days)
            )

        if audit_days > 0:
            result["audit_log_purged"] = await self.audit.purge_old_audit_log(
                audit_days
            )

        logger.info("Data cleanup complete", **result)

        return result

    async def get_user_dashboard(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user dashboard data."""
        # Get user info
        user = await self.users.get_user(user_id)
        if not user:
            return None

        # Get user stats
        stats = await self.analytics.get_user_stats(user_id)

        # Get recent sessions
        sessions = await self.sessions.get_user_sessions(user_id, active_only=True)

        # Get recent messages
        messages = await self.messages.get_user_messages(user_id, limit=10)

        # Get recent audit log
        audit_logs = await self.audit.get_user_audit_log(user_id, limit=20)

        # Get daily costs
        daily_costs = await self.costs.get_user_daily_costs(user_id, days=30)

        return {
            "user": user.to_dict(),
            "stats": stats,
            "recent_sessions": [s.to_dict() for s in sessions[:5]],
            "recent_messages": [m.to_dict() for m in messages],
            "recent_audit": [a.to_dict() for a in audit_logs],
            "daily_costs": [c.to_dict() for c in daily_costs],
        }

    async def get_admin_dashboard(self) -> Dict[str, Any]:
        """Get admin dashboard data."""
        # Get system stats
        system_stats = await self.analytics.get_system_stats()

        # Get all users
        users = await self.users.get_all_users()

        # Get recent audit log
        recent_audit = await self.audit.get_recent_audit_log(hours=24)

        # Get total costs
        total_costs = await self.costs.get_total_costs(days=30)

        # Get tool stats
        tool_stats = await self.tools.get_tool_stats()

        return {
            "system_stats": system_stats,
            "users": [u.to_dict() for u in users],
            "recent_audit": [a.to_dict() for a in recent_audit],
            "total_costs": total_costs,
            "tool_stats": tool_stats,
        }
