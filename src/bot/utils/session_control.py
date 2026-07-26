"""Shared session-termination path for the classic handlers.

Ending a session has two halves: clearing the Telegram-side context and
deactivating the persisted session row. Handlers used to do only the first, so
``/end`` told the user the session was terminated while it stayed active in
SQLite — still listed by ``/sessions``, still counted against
``max_sessions_per_user`` and still resumable by its stored id.
"""

from typing import Any, Optional

import structlog

logger = structlog.get_logger()


async def terminate_user_session(context: Any, user_id: int) -> Optional[str]:
    """End the user's current session and clear its Telegram context.

    Returns the id of the terminated session, or ``None`` when there was none.
    The context is cleared even if the storage deactivation fails, so the user
    never keeps talking to a session the bot told them was over.
    """
    raw_session_id = context.user_data.get("claude_session_id")
    session_id: Optional[str] = str(raw_session_id) if raw_session_id else None

    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = False
    context.user_data["last_message"] = None
    context.user_data["force_new_session"] = True

    if not session_id:
        return None

    claude_integration = context.bot_data.get("claude_integration")
    session_manager = getattr(claude_integration, "session_manager", None)
    if session_manager is None:
        return session_id

    try:
        await session_manager.remove_session(session_id)
    except Exception as e:
        logger.warning(
            "Failed to deactivate session in storage",
            user_id=user_id,
            session_id=session_id,
            error=str(e),
        )

    return session_id
