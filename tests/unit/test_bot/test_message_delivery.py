"""Delivery and audit invariants for the classic message handlers.

- A run that succeeded must reach the user even when Telegram rejects the HTML
  or a progress-message cleanup fails; neither may turn it into an error reply.
- The progress message is dropped only after the responses went out, so the
  error path never edits a message that no longer exists.
- Photo and voice runs leave an audit record, like text and document runs.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.handlers.message import (
    handle_document,
    handle_photo,
    handle_text_message,
    handle_voice,
)
from src.config import create_test_config

USER_ID = 4242


def _claude_response() -> SimpleNamespace:
    return SimpleNamespace(session_id="s-1", cost=0.0, content="done", is_error=False)


def _make_update(events: list) -> MagicMock:
    """Message whose replies and progress-message deletes are recorded in order."""
    progress_msg = MagicMock()
    progress_msg.edit_text = AsyncMock()

    async def _delete() -> None:
        events.append("delete")

    progress_msg.delete = AsyncMock(side_effect=_delete)

    async def _reply(text, **kwargs):
        events.append(("reply", text, kwargs.get("parse_mode")))
        return progress_msg

    message = MagicMock()
    message.message_id = 1
    message.text = "please analyze this"
    message.caption = None
    message.reply_text = AsyncMock(side_effect=_reply)
    message.chat.send_action = AsyncMock()

    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message = message
    update.effective_message = message
    return update


def _make_context(tmp_path, run_command: AsyncMock, features=None) -> MagicMock:
    claude_integration = MagicMock()
    claude_integration.run_command = run_command
    audit_logger = MagicMock()
    audit_logger.log_command = AsyncMock()
    audit_logger.log_file_access = AsyncMock()

    context = MagicMock()
    context.bot_data = {
        "settings": create_test_config(approved_directory=str(tmp_path)),
        "claude_integration": claude_integration,
        "audit_logger": audit_logger,
        "features": features,
    }
    context.user_data = {}
    return context


def _replies(events: list) -> list:
    return [e[1] for e in events if isinstance(e, tuple)]


def _photo_features() -> MagicMock:
    image_handler = MagicMock()
    image_handler.process_image = AsyncMock(
        return_value=SimpleNamespace(prompt="describe this image")
    )
    features = MagicMock()
    features.get_image_handler.return_value = image_handler
    return features


def _voice_features() -> MagicMock:
    voice_handler = MagicMock()
    voice_handler.process_voice_message = AsyncMock(
        return_value=SimpleNamespace(prompt="transcribed prompt")
    )
    features = MagicMock()
    features.get_voice_handler.return_value = voice_handler
    return features


async def test_text_progress_delete_failure_keeps_answer(tmp_path):
    events: list = []
    update = _make_update(events)
    context = _make_context(tmp_path, AsyncMock(return_value=_claude_response()))
    progress_msg = await update.message.reply_text("seed")
    events.clear()
    progress_msg.delete.side_effect = RuntimeError("message to delete not found")

    await handle_text_message(update, context)

    replies = _replies(events)
    assert any("done" in r for r in replies)
    assert not any("Unexpected Error" in r for r in replies)
    context.bot_data["audit_logger"].log_command.assert_awaited_once()
    assert context.bot_data["audit_logger"].log_command.await_args.kwargs["success"]


async def test_document_html_rejection_retries_as_plain_text(tmp_path):
    events: list = []
    update = _make_update(events)
    telegram_file = MagicMock()
    telegram_file.file_size = None
    telegram_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"x=1"))
    update.message.document.file_name = "snippet.py"
    update.message.document.file_size = 3
    update.message.document.get_file = AsyncMock(return_value=telegram_file)
    context = _make_context(tmp_path, AsyncMock(return_value=_claude_response()))

    real_reply = update.message.reply_text.side_effect

    async def reject_html_answer(text, **kwargs):
        if "done" in text and kwargs.get("parse_mode"):
            raise RuntimeError("Can't parse entities")
        return await real_reply(text, **kwargs)

    update.message.reply_text.side_effect = reject_html_answer

    await handle_document(update, context)

    assert ("reply", "done", None) in events
    audit = context.bot_data["audit_logger"].log_file_access
    assert audit.await_args.kwargs["action"] == "upload_processed"
    assert audit.await_args.kwargs["success"] is True


async def test_photo_progress_deleted_after_responses(tmp_path):
    events: list = []
    update = _make_update(events)
    update.message.photo = [MagicMock()]
    context = _make_context(
        tmp_path, AsyncMock(return_value=_claude_response()), _photo_features()
    )

    await handle_photo(update, context)

    answer = next(
        i for i, e in enumerate(events) if isinstance(e, tuple) and e[1] == "done"
    )
    last_delete = max(i for i, e in enumerate(events) if e == "delete")
    assert last_delete > answer
    audit = context.bot_data["audit_logger"].log_command
    assert audit.await_args.kwargs["command"] == "photo"
    assert audit.await_args.kwargs["success"] is True


async def test_photo_failure_is_audited(tmp_path):
    events: list = []
    update = _make_update(events)
    update.message.photo = [MagicMock()]
    context = _make_context(
        tmp_path, AsyncMock(side_effect=RuntimeError("boom")), _photo_features()
    )

    await handle_photo(update, context)

    audit = context.bot_data["audit_logger"].log_command
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["command"] == "photo"
    assert audit.await_args.kwargs["success"] is False


async def test_voice_success_and_failure_are_audited(tmp_path):
    for run_command, expected in (
        (AsyncMock(return_value=_claude_response()), True),
        (AsyncMock(side_effect=RuntimeError("boom")), False),
    ):
        events: list = []
        update = _make_update(events)
        update.message.voice = MagicMock()
        context = _make_context(tmp_path, run_command, _voice_features())

        await handle_voice(update, context)

        audit = context.bot_data["audit_logger"].log_command
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["command"] == "voice"
        assert audit.await_args.kwargs["success"] is expected


async def test_photo_and_voice_html_rejection_retry_as_plain_text(tmp_path):
    for handler, attr, features, command in (
        (handle_photo, "photo", _photo_features, "photo"),
        (handle_voice, "voice", _voice_features, "voice"),
    ):
        events: list = []
        update = _make_update(events)
        setattr(update.message, attr, [MagicMock()])
        context = _make_context(
            tmp_path, AsyncMock(return_value=_claude_response()), features()
        )
        real_reply = update.message.reply_text.side_effect

        async def reject_html_answer(text, _real=real_reply, **kwargs):
            if "done" in text and kwargs.get("parse_mode"):
                raise RuntimeError("Can't parse entities")
            return await _real(text, **kwargs)

        update.message.reply_text.side_effect = reject_html_answer

        await handler(update, context)

        assert ("reply", "done", None) in events, command
        audit = context.bot_data["audit_logger"].log_command
        assert audit.await_args.kwargs["success"] is True, command


def _document_update(events: list) -> MagicMock:
    update = _make_update(events)
    telegram_file = MagicMock()
    telegram_file.file_size = None
    telegram_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"x=1"))
    update.message.document.file_name = "snippet.py"
    update.message.document.file_size = 3
    update.message.document.get_file = AsyncMock(return_value=telegram_file)
    return update


async def test_plain_text_fallback_drops_html_markup(tmp_path):
    """The retry without parse_mode used to show raw <b> tags and &lt;."""
    events: list = []
    update = _document_update(events)
    response = _claude_response()
    response.content = "**done** a < b"
    context = _make_context(tmp_path, AsyncMock(return_value=response))
    real_reply = update.message.reply_text.side_effect

    async def reject_html_answer(text, **kwargs):
        if "done" in text and kwargs.get("parse_mode"):
            raise RuntimeError("Can't parse entities")
        return await real_reply(text, **kwargs)

    update.message.reply_text.side_effect = reject_html_answer

    await handle_document(update, context)

    plain = [e[1] for e in events if isinstance(e, tuple) and e[2] is None]
    assert any("done a < b" in text for text in plain)
    assert not any("<b>" in text or "&lt;" in text for text in plain)


async def test_media_runs_after_new_skip_auto_resume(tmp_path):
    """Only the text path honoured /new; media auto-resumed the reset session."""
    for handler, attr, features in (
        (handle_document, None, None),
        (handle_photo, "photo", _photo_features),
        (handle_voice, "voice", _voice_features),
    ):
        events: list = []
        update = _document_update(events)
        if attr:
            setattr(update.message, attr, [MagicMock()])
        run_command = AsyncMock(return_value=_claude_response())
        context = _make_context(tmp_path, run_command, features() if features else None)
        context.user_data["force_new_session"] = True

        await handler(update, context)

        assert run_command.await_args.kwargs["force_new"] is True, handler.__name__
        assert context.user_data["force_new_session"] is False, handler.__name__
        assert context.user_data["claude_session_id"] == "s-1", handler.__name__


async def test_document_audit_failure_after_answer_is_not_an_error(tmp_path):
    """An audit write failing after the reply must not reach the error path."""
    events: list = []
    update = _document_update(events)
    context = _make_context(tmp_path, AsyncMock(return_value=_claude_response()))
    context.bot_data["audit_logger"].log_file_access.side_effect = RuntimeError(
        "database is locked"
    )

    await handle_document(update, context)

    replies = _replies(events)
    assert any("done" in r for r in replies)
    assert not any("Error" in r for r in replies)


async def test_text_failure_with_failing_audit_does_not_escape(tmp_path):
    """The outer error path audits through the guard, like the success path."""
    events: list = []
    update = _make_update(events)
    # A failure outside the Claude run is what reaches the outer except.
    update.message.chat.send_action = AsyncMock(side_effect=RuntimeError("net"))
    context = _make_context(tmp_path, AsyncMock(return_value=_claude_response()))
    context.bot_data["audit_logger"].log_command.side_effect = RuntimeError(
        "database is locked"
    )

    await handle_text_message(update, context)

    assert _replies(events), "the user still gets the error reply"
    audit = context.bot_data["audit_logger"].log_command
    assert audit.await_args.kwargs["success"] is False


async def test_stuck_progress_edit_does_not_block_stream(tmp_path):
    """A progress edit stuck in Telegram must not hold up the Claude run.

    The stream handler is awaited by the loop draining Claude's output, and in
    a group topic each edit waits for PTB's 20-calls-per-minute group budget.
    """
    from src.claude.sdk_integration import StreamUpdate

    events: list = []
    update = _make_update(events)
    progress_msg = await update.message.reply_text("seed")
    events.clear()
    release = asyncio.Event()

    async def stuck_edit(*args, **kwargs):
        await release.wait()

    progress_msg.edit_text = AsyncMock(side_effect=stuck_edit)

    async def run_command(**kwargs):
        await kwargs["on_stream"](StreamUpdate(type="assistant", content="reading"))
        return _claude_response()

    context = _make_context(tmp_path, AsyncMock(side_effect=run_command))

    await asyncio.wait_for(handle_text_message(update, context), timeout=2)

    assert any("done" in r for r in _replies(events))
    release.set()
    await asyncio.sleep(0)
