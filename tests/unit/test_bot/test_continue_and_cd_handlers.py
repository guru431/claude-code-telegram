"""Tests for /continue, /cd and the cd/continue inline-button handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.features.git_integration import GitError
from src.bot.handlers import callback, command
from src.bot.handlers.message import _format_error_message
from src.claude.sdk_integration import ClaudeResponse
from src.config import create_test_config
from src.security.validators import SecurityValidator

# A key-shaped string that ``redact_secrets`` masks (same as test_orchestrator).
# Split so the pre-commit secret scanner does not flag the literal.
_FAKE_KEY = "sk-" + "1234567890abcdefghijklmnop"


@pytest.fixture
def settings(tmp_path: Path):
    approved = tmp_path / "projects"
    approved.mkdir()
    return create_test_config(approved_directory=str(approved))


def _response(content, is_error=False, error_type=None) -> ClaudeResponse:
    return ClaudeResponse(
        content=content,
        session_id="session-123456",
        cost=0.0,
        duration_ms=1,
        num_turns=1,
        is_error=is_error,
        error_type=error_type,
    )


def _continue_update():
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock(return_value=AsyncMock())
    return update


def _continue_context(settings):
    context = MagicMock()
    context.args = []
    context.bot_data = {
        "settings": settings,
        "claude_integration": MagicMock(),
        "audit_logger": None,
        "rate_limiter": None,
        "storage": None,
    }
    context.user_data = {"claude_session_id": "abcdefgh1234"}
    return context


def _sent_texts(update) -> str:
    return "\n".join(c.args[0] for c in update.message.reply_text.await_args_list)


async def test_continue_redacts_secrets(settings, monkeypatch):
    monkeypatch.setattr(
        command,
        "run_claude_for_user",
        AsyncMock(return_value=(_response(f"TOKEN={_FAKE_KEY}"), None)),
    )
    update = _continue_update()

    await command.continue_session(update, _continue_context(settings))

    assert _FAKE_KEY not in _sent_texts(update)


async def test_continue_shows_error_for_sdk_flagged_error(settings, monkeypatch):
    monkeypatch.setattr(
        command,
        "run_claude_for_user",
        AsyncMock(
            return_value=(
                _response("raw error payload", True, "error_max_turns"),
                None,
            )
        ),
    )
    update = _continue_update()

    await command.continue_session(update, _continue_context(settings))

    text = _sent_texts(update)
    assert "raw error payload" not in text
    assert _format_error_message("error_max_turns") in text


async def test_cd_dotdot_with_unnormalised_current_dir_still_confirms(settings):
    """``..`` from a path that is not lexically under the root must not error
    after the directory was already switched."""
    approved = settings.approved_directory
    (approved.parent / "other").mkdir()
    (approved / "sub").mkdir()
    current = approved.parent / "other" / ".." / approved.name / "sub"

    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = [".."]
    context.bot_data = {
        "settings": settings,
        "security_validator": None,
        "audit_logger": None,
        "claude_integration": AsyncMock(
            find_resumable_session=AsyncMock(return_value=None)
        ),
    }
    context.user_data = {"current_directory": current}

    await command.change_directory(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "Directory Changed" in text
    assert "Error changing directory" not in text


async def test_continue_button_handles_empty_content(settings, monkeypatch):
    monkeypatch.setattr(
        callback,
        "run_claude_for_user",
        AsyncMock(return_value=(_response(None), None)),
    )
    query = MagicMock()
    query.from_user.id = 1
    query.edit_message_text = AsyncMock()
    query.message = None
    context = _continue_context(settings)
    context.bot.send_message = AsyncMock()

    await callback._handle_continue_action(query, context)

    text = context.bot.send_message.await_args.args[1]
    assert "Session Continued" in text
    assert "(no output)" in text


def _cd_query():
    query = MagicMock()
    query.from_user.id = 1
    query.edit_message_text = AsyncMock()
    return query


async def test_cd_button_error_does_not_leak_host_paths(settings):
    query = _cd_query()
    context = MagicMock()
    context.bot_data = {
        "settings": settings,
        "security_validator": None,
        "audit_logger": None,
        "claude_integration": AsyncMock(
            find_resumable_session=AsyncMock(
                side_effect=OSError("cannot stat /srv/secret/host/path")
            )
        ),
    }
    context.user_data = {}

    await callback.handle_cd_callback(query, "/", context)

    text = query.edit_message_text.await_args.args[0]
    assert "Error changing directory" in text
    assert "/srv/secret/host/path" not in text


async def test_cd_button_dotdot_at_root_with_validator_stays_at_root(settings):
    """``..`` at the root clamps before validate_path, so the validator does
    not turn it into Access Denied."""
    approved = settings.approved_directory
    query = _cd_query()
    context = MagicMock()
    context.bot_data = {
        "settings": settings,
        "security_validator": SecurityValidator(approved),
        "audit_logger": None,
        "claude_integration": None,
    }
    context.user_data = {"current_directory": approved}

    await callback.handle_cd_callback(query, "..", context)

    assert context.user_data["current_directory"] == approved
    assert "Directory Changed" in query.edit_message_text.await_args.args[0]


async def test_followup_button_redacts_secrets(settings, monkeypatch):
    monkeypatch.setattr(
        callback,
        "run_claude_for_user",
        AsyncMock(return_value=(_response(f"TOKEN={_FAKE_KEY}"), None)),
    )
    query = _cd_query()
    query.message = None
    context = _continue_context(settings)
    context.user_data["followup_suggestions"] = {"abc123": "show the env"}
    context.bot.send_message = AsyncMock()

    await callback.handle_followup_callback(query, "abc123", context)

    text = context.bot.send_message.await_args.args[1]
    assert _FAKE_KEY not in text


async def test_quick_action_runs_in_the_users_session(settings, monkeypatch):
    """Quick actions dropped the selected session and a pending /new."""

    async def run_inline(run, **kwargs):
        return await run(), None

    monkeypatch.setattr(callback, "run_claude_for_user", run_inline)
    claude = MagicMock()
    claude.run_command = AsyncMock(return_value=_response("tests pass"))
    action = MagicMock(prompt="run the tests", icon="T")
    action.name = "Test"
    features = MagicMock()
    features.get_quick_actions.return_value = MagicMock(actions={"test": action})
    context = _continue_context(settings)
    context.bot_data.update(claude_integration=claude, features=features)
    context.user_data["force_new_session"] = True
    context.bot.send_message = AsyncMock()
    query = _cd_query()
    query.message = None

    await callback.handle_quick_action_callback(query, "test", context)

    kwargs = claude.run_command.await_args.kwargs
    assert kwargs["session_id"] == "abcdefgh1234"
    assert kwargs["force_new"] is True
    assert context.user_data["claude_session_id"] == "session-123456"
    assert context.user_data["force_new_session"] is False


async def test_continue_exception_does_not_leak_raw_error(settings, monkeypatch):
    monkeypatch.setattr(
        command,
        "run_claude_for_user",
        AsyncMock(side_effect=OSError("cannot open /srv/secret/host/path")),
    )
    update = _continue_update()

    await command.continue_session(update, _continue_context(settings))

    assert "/srv/secret/host/path" not in _sent_texts(update)


def _git_context(settings, error: Exception):
    git = MagicMock()
    git.get_status = AsyncMock(side_effect=error)
    features = MagicMock()
    features.is_enabled.return_value = True
    features.get_git_integration.return_value = git
    context = MagicMock()
    context.bot_data = {"settings": settings, "features": features}
    context.user_data = {}
    return context


async def test_git_command_hides_unexpected_error(settings):
    (settings.approved_directory / ".git").mkdir()
    update = _continue_update()

    await command.git_command(
        update, _git_context(settings, RuntimeError("/srv/secret/host/path"))
    )

    text = _sent_texts(update)
    assert "Git Error" in text
    assert "/srv/secret/host/path" not in text


async def test_git_command_shows_git_error_message(settings):
    (settings.approved_directory / ".git").mkdir()
    update = _continue_update()

    await command.git_command(
        update, _git_context(settings, GitError("Git command failed: bad revision"))
    )

    assert "bad revision" in _sent_texts(update)
