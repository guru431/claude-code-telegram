"""Shared session bookkeeping for the classic handlers.

Ending a session has two halves: clearing the Telegram-side context and
deactivating the persisted session row. Handlers used to do only the first, so
``/end`` told the user the session was terminated while it stayed active in
SQLite — still listed by ``/sessions``, still counted against
``max_sessions_per_user`` and still resumable by its stored id.

After a run, every classic entry point records the run's session the same way
(:func:`remember_run_session`).
"""

from typing import Any, Optional

import structlog

logger = structlog.get_logger()


def remember_run_session(context: Any, response: Any, force_new: bool) -> None:
    """Store a run's session ID and retire the one-shot ``/new`` flag.

    Callers read ``force_new_session`` before the run and pass it to
    ``run_command``; only the text handler used to, so a file, photo, voice
    note or button right after ``/new`` auto-resumed the session the user had
    just reset. The flag is cleared only on a run the SDK did not flag as an
    error, so the retry after a failed first message still starts fresh.
    """
    if force_new and not response.is_error:
        context.user_data["force_new_session"] = False
    context.user_data["claude_session_id"] = response.session_id


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
