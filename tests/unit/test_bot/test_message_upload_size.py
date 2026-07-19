"""Upload size policy for the classic document handler.

Telegram's ``Document.file_size`` is optional and attacker-influenced. The
handler must never treat an absent size as 0 (which silently passes the limit),
must not raise on ``None``, and must re-check the bytes actually downloaded.
"""

from unittest.mock import AsyncMock, MagicMock

from src.bot.handlers.message import handle_document
from src.config import create_test_config

USER_ID = 4242


def _make_update(file_name="notes.txt", file_size=1024):
    progress_msg = MagicMock()
    progress_msg.edit_text = AsyncMock()
    progress_msg.delete = AsyncMock()

    message = MagicMock()
    message.message_id = 1
    message.caption = None
    message.document.file_name = file_name
    message.document.file_size = file_size
    message.reply_text = AsyncMock(return_value=progress_msg)
    message.chat.send_action = AsyncMock()

    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message = message
    update.effective_message = message
    return update


def _make_context(tmp_path, update, downloaded=b"hello", max_mb=10):
    """Context with no file_handler, so the basic download path is used."""
    mock_file = AsyncMock()
    mock_file.file_size = None
    mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(downloaded))
    update.message.document.get_file = AsyncMock(return_value=mock_file)

    context = MagicMock()
    context.bot_data = {
        "settings": create_test_config(
            approved_directory=str(tmp_path),
            agentic_mode=False,
            max_file_upload_size_mb=max_mb,
        ),
        "security_validator": None,
        "audit_logger": None,
        "rate_limiter": None,
        "features": None,
        "claude_integration": None,
    }
    context.user_data = {}
    return context


def _replies(update):
    return [c.args[0] for c in update.message.reply_text.call_args_list if c.args]


async def test_none_file_size_does_not_raise_and_does_not_bypass(tmp_path):
    """A missing file_size must not TypeError, and must not wave through
    an oversized payload."""
    update = _make_update(file_size=None)
    oversized = b"x" * (10 * 1024 * 1024 + 1)
    context = _make_context(tmp_path, update, downloaded=oversized)

    await handle_document(update, context)

    assert any("too large" in r.lower() for r in _replies(update))


async def test_understated_metadata_size_is_caught_after_download(tmp_path):
    """Small declared size, oversized real bytes -> rejected."""
    update = _make_update(file_size=1024)
    oversized = b"x" * (10 * 1024 * 1024 + 1)
    context = _make_context(tmp_path, update, downloaded=oversized)

    await handle_document(update, context)

    assert any("too large" in r.lower() for r in _replies(update))


async def test_oversized_metadata_size_is_rejected_before_download(tmp_path):
    """An honestly oversized declared size is rejected without downloading."""
    update = _make_update(file_size=20 * 1024 * 1024)
    context = _make_context(tmp_path, update)

    await handle_document(update, context)

    assert any("too large" in r.lower() for r in _replies(update))
    update.message.document.get_file.assert_not_called()


async def test_limit_comes_from_settings(tmp_path):
    """The limit is MAX_FILE_UPLOAD_SIZE_MB, not a hardcoded 10MB."""
    update = _make_update(file_size=2 * 1024 * 1024)  # 2MB
    context = _make_context(tmp_path, update, max_mb=1)

    await handle_document(update, context)

    replies = _replies(update)
    assert any("too large" in r.lower() for r in replies)
    assert any("1MB" in r for r in replies)
