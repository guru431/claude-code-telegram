"""Ending a session must deactivate the stored session, not just the context.

Regression: /end and the two end-session buttons cleared context.user_data only,
so the session stayed active in SQLite — still listed by /sessions, still counted
against max_sessions_per_user and still resumable by its stored id.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot.utils.session_control import terminate_user_session


def _context(session_id: str | None, session_manager=None) -> SimpleNamespace:
    return SimpleNamespace(
        user_data={"claude_session_id": session_id, "session_started": True},
        bot_data={
            "claude_integration": SimpleNamespace(session_manager=session_manager)
        },
    )


async def test_deactivates_stored_session():
    manager = AsyncMock()
    context = _context("sess-1", manager)

    terminated = await terminate_user_session(context, user_id=7)

    assert terminated == "sess-1"
    manager.remove_session.assert_awaited_once_with("sess-1")
    assert context.user_data["claude_session_id"] is None
    assert context.user_data["session_started"] is False
    assert context.user_data["force_new_session"] is True


async def test_no_active_session_is_a_no_op():
    manager = AsyncMock()
    context = _context(None, manager)

    assert await terminate_user_session(context, user_id=7) is None
    manager.remove_session.assert_not_awaited()


async def test_storage_failure_still_clears_context():
    """The user was told the session ended; never keep talking to it."""
    manager = AsyncMock()
    manager.remove_session = AsyncMock(side_effect=RuntimeError("db down"))
    context = _context("sess-1", manager)

    assert await terminate_user_session(context, user_id=7) == "sess-1"
    assert context.user_data["claude_session_id"] is None
    assert context.user_data["force_new_session"] is True
