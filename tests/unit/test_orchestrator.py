"""Tests for the MessageOrchestrator."""

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config
from src.security.secret_patterns import redact_secrets as _redact_secrets


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def agentic_settings(tmp_dir):
    return create_test_config(approved_directory=str(tmp_dir), agentic_mode=True)


@pytest.fixture
def classic_settings(tmp_dir):
    return create_test_config(approved_directory=str(tmp_dir), agentic_mode=False)


@pytest.fixture
def group_thread_settings(tmp_dir):
    project_dir = tmp_dir / "project_a"
    project_dir.mkdir()
    config_file = tmp_dir / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )
    return create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=False,
        enable_project_threads=True,
        project_threads_mode="group",
        project_threads_chat_id=-1001234567890,
        projects_config_path=str(config_file),
    )


@pytest.fixture
def private_thread_settings(tmp_dir):
    project_dir = tmp_dir / "project_a"
    project_dir.mkdir()
    config_file = tmp_dir / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )
    return create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=False,
        enable_project_threads=True,
        project_threads_mode="private",
        projects_config_path=str(config_file),
    )


@pytest.fixture
def deps():
    return {
        "claude_integration": MagicMock(),
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }


def test_agentic_registers_9_commands(agentic_settings, deps):
    """Agentic mode registers start, new, status, verbose, repo, sessions,
    schedule, events, restart."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    # Collect all CommandHandler registrations
    from telegram.ext import CommandHandler

    cmd_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CommandHandler)
    ]
    commands = [h[0][0].commands for h in cmd_handlers]

    assert len(cmd_handlers) == 9
    assert frozenset({"start"}) in commands
    assert frozenset({"new"}) in commands
    assert frozenset({"status"}) in commands
    assert frozenset({"verbose"}) in commands
    assert frozenset({"repo"}) in commands
    assert frozenset({"sessions"}) in commands
    assert frozenset({"schedule"}) in commands
    assert frozenset({"events"}) in commands
    assert frozenset({"restart"}) in commands


def test_classic_registers_14_commands(classic_settings, deps):
    """Classic mode registers all 14 commands."""
    orchestrator = MessageOrchestrator(classic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CommandHandler

    cmd_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CommandHandler)
    ]

    assert len(cmd_handlers) == 14


def test_agentic_registers_text_document_photo_handlers(agentic_settings, deps):
    """Agentic mode registers text, document, photo, and voice message handlers."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler, MessageHandler

    msg_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], MessageHandler)
    ]
    cb_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    # 5 message handlers (text, unknown-command passthrough, document, photo, voice)
    assert len(msg_handlers) == 5
    # 3 callback handlers (stop:, cd: for repo switch, resume: for /sessions)
    assert len(cb_handlers) == 3


async def test_agentic_bot_commands(agentic_settings, deps):
    """Agentic mode returns 9 bot commands."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    commands = await orchestrator.get_bot_commands()

    assert len(commands) == 9
    cmd_names = [c.command for c in commands]
    assert cmd_names == [
        "start",
        "new",
        "status",
        "verbose",
        "repo",
        "sessions",
        "schedule",
        "events",
        "restart",
    ]


async def test_classic_bot_commands(classic_settings, deps):
    """Classic mode returns 14 bot commands."""
    orchestrator = MessageOrchestrator(classic_settings, deps)
    commands = await orchestrator.get_bot_commands()

    assert len(commands) == 14
    cmd_names = [c.command for c in commands]
    assert "start" in cmd_names
    assert "help" in cmd_names
    assert "git" in cmd_names
    assert "restart" in cmd_names


async def test_restart_command_sends_sigterm(deps):
    """restart_command sends the platform-appropriate graceful-shutdown signal.

    On POSIX this is SIGTERM. On Windows SIGTERM does not invoke Python's
    registered signal handlers, so we send SIGBREAK (CTRL_BREAK_EVENT)
    which does.
    """
    import os
    import signal
    import sys
    from unittest.mock import patch

    from src.bot.handlers.command import restart_command

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    settings = MagicMock()
    settings.is_admin = MagicMock(return_value=True)
    context = MagicMock()
    context.bot_data = {"audit_logger": None, "settings": settings}

    with patch("src.bot.handlers.command.os.kill") as mock_kill:
        await restart_command(update, context)

    expected_sig = (
        getattr(signal, "SIGBREAK", signal.SIGTERM)
        if sys.platform == "win32"
        else signal.SIGTERM
    )
    mock_kill.assert_called_once_with(os.getpid(), expected_sig)
    # Verify confirmation message was sent
    update.message.reply_text.assert_called_once()
    msg = update.message.reply_text.call_args[0][0]
    assert "Restarting" in msg


async def test_restart_command_denied_for_non_admin(deps):
    """A non-admin user cannot restart the bot — no signal is sent."""
    from unittest.mock import patch

    from src.bot.handlers.command import restart_command

    update = MagicMock()
    update.effective_user.id = 999
    update.message.reply_text = AsyncMock()

    settings = MagicMock()
    settings.is_admin = MagicMock(return_value=False)
    audit_logger = MagicMock()
    audit_logger.log_security_violation = AsyncMock()
    context = MagicMock()
    context.bot_data = {"audit_logger": audit_logger, "settings": settings}

    with patch("src.bot.handlers.command.os.kill") as mock_kill:
        await restart_command(update, context)

    mock_kill.assert_not_called()
    settings.is_admin.assert_called_once_with(999)
    audit_logger.log_security_violation.assert_awaited_once()
    msg = update.message.reply_text.call_args[0][0]
    assert "Admin only" in msg


async def test_repo_scoped_to_thread_project_root_rejects_sibling(
    agentic_settings, deps, tmp_dir
):
    """In thread mode /repo cannot switch to a sibling outside the project root.

    The sibling lives inside the approved root but outside the topic's project
    root; without scoping, the switch would be accepted then silently reverted.
    """
    (tmp_dir / "proj").mkdir()
    (tmp_dir / "other").mkdir()
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "/repo other"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"_thread_context": {"project_root": str(tmp_dir / "proj")}}
    context.bot_data = {}

    await orchestrator.agentic_repo(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "Directory not found" in msg
    assert "current_directory" not in context.user_data


async def test_repo_scoped_to_thread_project_root_allows_child(
    agentic_settings, deps, tmp_dir
):
    """A child of the topic's project root is a valid /repo target."""
    proj = tmp_dir / "proj"
    (proj / "sub").mkdir(parents=True)
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "/repo sub"
    update.message.reply_text = AsyncMock()

    claude = MagicMock()
    claude._find_resumable_session = AsyncMock(return_value=None)
    context = MagicMock()
    context.user_data = {"_thread_context": {"project_root": str(proj)}}
    context.bot_data = {"claude_integration": claude}

    await orchestrator.agentic_repo(update, context)

    assert context.user_data["current_directory"] == (proj / "sub").resolve()
    msg = update.message.reply_text.call_args[0][0]
    assert "Switched to" in msg


async def test_agentic_start_no_keyboard(agentic_settings, deps):
    """Agentic /start sends brief message without inline keyboard."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.first_name = "Alice"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"settings": agentic_settings}
    for k, v in deps.items():
        context.bot_data[k] = v

    await orchestrator.agentic_start(update, context)

    update.message.reply_text.assert_called_once()
    call_kwargs = update.message.reply_text.call_args
    # No reply_markup argument (no keyboard)
    assert (
        "reply_markup" not in call_kwargs.kwargs
        or call_kwargs.kwargs.get("reply_markup") is None
    )
    # Contains user name
    assert "Alice" in call_kwargs.args[0]


async def test_agentic_new_resets_session(agentic_settings, deps):
    """Agentic /new clears session and sends brief confirmation."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"claude_session_id": "old-session-123"}

    await orchestrator.agentic_new(update, context)

    assert context.user_data["claude_session_id"] is None
    update.message.reply_text.assert_called_once_with("Session reset. What's next?")


async def test_agentic_status_compact(agentic_settings, deps):
    """Agentic /status returns compact one-line status."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"rate_limiter": None}

    await orchestrator.agentic_status(update, context)

    call_args = update.message.reply_text.call_args
    text = call_args.args[0]
    assert "Session: none" in text


async def test_agentic_text_calls_claude(agentic_settings, deps):
    """Agentic text handler calls Claude and returns response without keyboard."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    # Mock Claude response
    mock_response = MagicMock()
    mock_response.session_id = "session-abc"
    mock_response.content = "Hello, I can help with that!"
    mock_response.tools_used = []

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(return_value=mock_response)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "Help me with this code"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    # Progress message mock
    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
    }

    await orchestrator.agentic_text(update, context)

    # Claude was called
    claude_integration.run_command.assert_called_once()

    # Session ID updated
    assert context.user_data["claude_session_id"] == "session-abc"

    # Progress message deleted
    progress_msg.delete.assert_called_once()

    # Response sent without keyboard (reply_markup=None)
    response_calls = [
        c
        for c in update.message.reply_text.call_args_list
        if c != update.message.reply_text.call_args_list[0]
    ]
    for call in response_calls:
        assert call.kwargs.get("reply_markup") is None


async def test_agentic_callbacks_scoped_to_cd_and_resume_patterns(
    agentic_settings, deps
):
    """Agentic callback handlers are scoped to cd: and resume: pattern filters."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler

    cb_handlers = [
        call[0][0]
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    assert len(cb_handlers) == 3
    # Every handler is scoped to a specific prefix (no catch-all).
    assert all(h.pattern is not None for h in cb_handlers)
    assert any(h.pattern.match("cd:my_project") for h in cb_handlers)
    assert any(h.pattern.match("resume:abc123") for h in cb_handlers)
    assert any(h.pattern.match("stop:123") for h in cb_handlers)


async def test_agentic_document_rejects_large_files(agentic_settings, deps):
    """Agentic document handler rejects files over 10MB."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.document.file_name = "big.bin"
    update.message.document.file_size = 20 * 1024 * 1024  # 20MB
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"security_validator": None}

    await orchestrator.agentic_document(update, context)

    call_args = update.message.reply_text.call_args
    assert "too large" in call_args.args[0].lower()


async def test_agentic_voice_calls_claude(agentic_settings, deps):
    """Agentic voice handler transcribes and routes prompt to Claude."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    mock_response = MagicMock()
    mock_response.session_id = "voice-session-123"
    mock_response.content = "Voice response from Claude"
    mock_response.tools_used = []
    mock_response.is_error = False

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(return_value=mock_response)

    processed_voice = MagicMock()
    processed_voice.prompt = "Voice prompt text"

    voice_handler = MagicMock()
    voice_handler.process_voice_message = AsyncMock(return_value=processed_voice)

    features = MagicMock()
    features.get_voice_handler.return_value = voice_handler

    update = MagicMock()
    update.effective_user.id = 123
    update.message.voice = MagicMock()
    update.message.caption = "please summarize"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.edit_text = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "features": features,
        "claude_integration": claude_integration,
    }

    await orchestrator.agentic_voice(update, context)

    voice_handler.process_voice_message.assert_awaited_once_with(
        update.message.voice, "please summarize"
    )
    claude_integration.run_command.assert_awaited_once()
    assert context.user_data["claude_session_id"] == "voice-session-123"


async def test_agentic_voice_missing_handler_is_provider_aware(tmp_path, deps):
    """Missing voice handler guidance references the configured provider key."""
    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        voice_provider="openai",
    )
    orchestrator = MessageOrchestrator(settings, deps)

    features = MagicMock()
    features.get_voice_handler.return_value = None

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"features": features}
    context.user_data = {}

    await orchestrator.agentic_voice(update, context)

    call_args = update.message.reply_text.call_args
    assert "OPENAI_API_KEY" in call_args.args[0]


async def test_agentic_voice_transcription_failure_surfaces_user_error(
    agentic_settings, deps
):
    """Transcription failures are shown to users and do not call Claude."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    voice_handler = MagicMock()
    voice_handler.process_voice_message = AsyncMock(
        side_effect=RuntimeError("Mistral transcription request failed: boom")
    )

    features = MagicMock()
    features.get_voice_handler.return_value = voice_handler

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.message.voice = MagicMock()
    update.message.caption = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()
    # The except block replies fresh via effective_message (the progress
    # message may already be deleted by the media handler).
    update.effective_message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.edit_text = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "features": features,
        "claude_integration": claude_integration,
    }

    await orchestrator.agentic_voice(update, context)

    update.effective_message.reply_text.assert_awaited_once()
    error_text = update.effective_message.reply_text.call_args.args[0]
    assert "Mistral transcription request failed" in error_text
    assert update.effective_message.reply_text.call_args.kwargs["parse_mode"] == "HTML"
    claude_integration.run_command.assert_not_awaited()


async def test_agentic_start_escapes_html_in_name(agentic_settings, deps):
    """Names with HTML-special characters are escaped safely."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.first_name = "A<B>&C"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}

    await orchestrator.agentic_start(update, context)

    call_kwargs = update.message.reply_text.call_args
    text = call_kwargs.args[0]
    # HTML-special characters should be escaped
    assert "A&lt;B&gt;&amp;C" in text
    # parse_mode is HTML
    assert call_kwargs.kwargs.get("parse_mode") == "HTML"


async def test_agentic_text_logs_failure_on_error(agentic_settings, deps):
    """Failed Claude runs are logged with success=False."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(side_effect=Exception("Claude broke"))

    audit_logger = AsyncMock()
    audit_logger.log_command = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "do something"
    update.message.message_id = 1
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": audit_logger,
    }

    await orchestrator.agentic_text(update, context)

    # Audit logged with success=False
    audit_logger.log_command.assert_called_once()
    call_kwargs = audit_logger.log_command.call_args
    assert call_kwargs.kwargs["success"] is False


# --- _redact_secrets / _summarize_tool_input tests ---


class TestRedactSecrets:
    """Ensure sensitive substrings are redacted from Bash command summaries."""

    def test_safe_command_unchanged(self):
        assert (
            _redact_secrets("poetry run pytest tests/ -v")
            == "poetry run pytest tests/ -v"
        )

    def test_anthropic_api_key_redacted(self):
        key = "sk-ant-api03-abc123def456ghi789jkl012mno345"
        cmd = f"ANTHROPIC_API_KEY={key}"
        result = _redact_secrets(cmd)
        assert key not in result
        assert "***" in result

    def test_sk_key_redacted(self):
        cmd = "curl -H 'Authorization: Bearer sk-1234567890abcdefghijklmnop'"
        result = _redact_secrets(cmd)
        assert "sk-1234567890abcdefghijklmnop" not in result
        assert "***" in result

    def test_github_pat_redacted(self):
        cmd = "git clone https://ghp_abcdefghijklmnop1234@github.com/user/repo"
        result = _redact_secrets(cmd)
        assert "ghp_abcdefghijklmnop1234" not in result
        assert "***" in result

    def test_aws_key_redacted(self):
        cmd = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = _redact_secrets(cmd)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "***" in result

    def test_flag_token_redacted(self):
        cmd = "mycli --token=supersecretvalue123"
        result = _redact_secrets(cmd)
        assert "supersecretvalue123" not in result
        assert "--token=" in result or "--token" in result

    def test_password_env_redacted(self):
        cmd = "PASSWORD=MyS3cretP@ss! ./run.sh"
        result = _redact_secrets(cmd)
        assert "MyS3cretP@ss!" not in result
        assert "***" in result

    def test_bearer_token_redacted(self):
        cmd = "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig'"
        result = _redact_secrets(cmd)
        assert "eyJhbGciOiJIUzI1NiJ9.payload.sig" not in result

    def test_connection_string_redacted(self):
        cmd = "psql postgresql://admin:secret_password@db.host:5432/mydb"
        result = _redact_secrets(cmd)
        assert "secret_password" not in result

    def test_summarize_tool_input_bash_redacts(self, agentic_settings, deps):
        """_summarize_tool_input applies redaction to Bash commands."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        result = orchestrator._summarize_tool_input(
            "Bash",
            {"command": "curl --token=mysupersecrettoken123 https://api.example.com"},
        )
        assert "mysupersecrettoken123" not in result
        assert "***" in result

    def test_summarize_tool_input_non_bash_unchanged(self, agentic_settings, deps):
        """Non-Bash tools don't go through redaction."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        result = orchestrator._summarize_tool_input(
            "Read", {"file_path": "/home/user/.env"}
        )
        assert result == ".env"


# --- Typing heartbeat tests ---


class TestTypingHeartbeat:
    """Verify typing indicator stays alive independently of stream events."""

    async def test_heartbeat_sends_typing_action(self, agentic_settings, deps):
        """Heartbeat sends typing actions at the configured interval."""
        chat = AsyncMock()
        chat.send_action = AsyncMock()

        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        # Let the heartbeat fire a few times
        await asyncio.sleep(0.2)
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Should have been called multiple times
        assert chat.send_action.call_count >= 2
        chat.send_action.assert_called_with("typing")

    async def test_heartbeat_cancels_cleanly(self, agentic_settings, deps):
        """Cancelling the heartbeat task does not raise."""
        chat = AsyncMock()
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        heartbeat.cancel()
        # Should not raise
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        assert heartbeat.cancelled() or heartbeat.done()

    async def test_heartbeat_survives_send_action_errors(self, agentic_settings, deps):
        """Heartbeat keeps running even if send_action raises."""
        chat = AsyncMock()
        call_count = [0]

        async def flaky_send_action(action: str) -> None:
            call_count[0] += 1
            if call_count[0] <= 2:
                raise Exception("Network error")

        chat.send_action = flaky_send_action

        orchestrator = MessageOrchestrator(agentic_settings, deps)
        heartbeat = orchestrator._start_typing_heartbeat(chat, interval=0.05)

        await asyncio.sleep(0.3)
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Should have called send_action more than 2 times (survived errors)
        assert call_count[0] >= 3

    async def test_stream_callback_independent_of_typing(self, agentic_settings, deps):
        """Stream callback no longer sends typing — that's the heartbeat's job."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        progress_msg = AsyncMock()
        tool_log: list = []  # type: ignore[type-arg]
        callback = orchestrator._make_stream_callback(
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=0.0,
        )
        assert callback is not None

        # Verify the callback signature doesn't accept a 'chat' parameter
        # (typing is no longer handled by the stream callback)
        import inspect

        sig = inspect.signature(orchestrator._make_stream_callback)
        assert "chat" not in sig.parameters


class TestRequestLock:
    """Per-user request serialization lock."""

    async def test_returns_same_lock_for_same_user(self, agentic_settings, deps):
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        assert orchestrator._get_request_lock(7) is orchestrator._get_request_lock(7)
        assert orchestrator._get_request_lock(7) is not orchestrator._get_request_lock(
            8
        )

    async def test_eviction_keeps_held_locks(self, agentic_settings, deps):
        """A held lock survives the eviction sweep; idle ones are dropped."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        orchestrator._MAX_REQUEST_LOCKS = 3

        held = orchestrator._get_request_lock(1)
        await held.acquire()
        orchestrator._get_request_lock(2)
        orchestrator._get_request_lock(3)

        orchestrator._get_request_lock(4)  # triggers eviction

        assert orchestrator._request_locks.get(1) is held
        assert 2 not in orchestrator._request_locks
        held.release()

    async def test_acquire_returns_canonical_lock_after_eviction(
        self, agentic_settings, deps
    ):
        """If the lock is evicted while awaited, acquisition retries the new one."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        stale = orchestrator._get_request_lock(42)
        # Simulate an eviction sweep racing the ``await lock.acquire()``:
        # the mapped lock is replaced while the caller holds only a reference.
        original_get = orchestrator._get_request_lock
        swapped = [False]

        def _get_and_evict(user_id: int) -> asyncio.Lock:
            lock = original_get(user_id)
            if not swapped[0]:
                swapped[0] = True
                orchestrator._request_locks[user_id] = asyncio.Lock()
            return lock

        orchestrator._get_request_lock = _get_and_evict  # type: ignore[method-assign]

        acquired = await orchestrator._acquire_request_lock(42)

        assert acquired is orchestrator._request_locks[42]
        assert acquired is not stale
        assert acquired.locked()
        assert not stale.locked()  # the stale lock was released, not leaked
        acquired.release()

    async def test_acquire_serializes_same_user(self, agentic_settings, deps):
        """Two concurrent acquisitions for one user do not overlap."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)
        order: list = []  # type: ignore[type-arg]

        async def run(tag: str) -> None:
            lock = await orchestrator._acquire_request_lock(5)
            order.append(f"{tag}-in")
            await asyncio.sleep(0.01)
            order.append(f"{tag}-out")
            lock.release()

        await asyncio.gather(run("a"), run("b"))

        assert order in (
            ["a-in", "a-out", "b-in", "b-out"],
            ["b-in", "b-out", "a-in", "a-out"],
        )


async def test_group_thread_mode_rejects_non_forum_chat(group_thread_settings, deps):
    """Strict thread mode rejects updates outside configured forum chat."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project thread"
    deps["project_threads_manager"] = project_threads_manager

    called = {"value": False}

    async def dummy_handler(update, context):
        called["value"] = True

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1002222222
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is False
    update.effective_message.reply_text.assert_called_once()


async def test_thread_mode_loads_and_persists_thread_state(group_thread_settings, deps):
    """Thread mode loads per-thread context and writes updates back."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    project_path = group_thread_settings.approved_directory / "project_a"
    project = SimpleNamespace(
        slug="project_a",
        name="Project A",
        absolute_path=project_path,
    )

    project_threads_manager = MagicMock()
    project_threads_manager.resolve_project = AsyncMock(return_value=project)
    project_threads_manager.guidance_message.return_value = "Use project thread"
    deps["project_threads_manager"] = project_threads_manager

    async def dummy_handler(update, context):
        assert context.user_data["claude_session_id"] == "old-session"
        context.user_data["claude_session_id"] = "new-session"

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1001234567890
    update.effective_message.message_thread_id = 777
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {
        "thread_state": {
            "-1001234567890:777": {
                "current_directory": str(project_path),
                "claude_session_id": "old-session",
            }
        }
    }

    await wrapped(update, context)

    assert (
        context.user_data["thread_state"]["-1001234567890:777"]["claude_session_id"]
        == "new-session"
    )


async def test_sync_threads_bypasses_thread_gate(group_thread_settings, deps):
    """sync_threads command bypasses strict thread routing gate."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    called = {"value": False}

    async def sync_threads(update, context):
        called["value"] = True

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project thread"
    deps["project_threads_manager"] = project_threads_manager

    wrapped = orchestrator._inject_deps(sync_threads)

    update = MagicMock()
    update.effective_chat.id = -1002222222
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is True


async def test_private_mode_start_bypasses_thread_gate(private_thread_settings, deps):
    """Private mode allows /start outside topics."""
    orchestrator = MessageOrchestrator(private_thread_settings, deps)
    called = {"value": False}

    async def start_command(update, context):
        called["value"] = True

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project topic"
    deps["project_threads_manager"] = project_threads_manager

    wrapped = orchestrator._inject_deps(start_command)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.effective_chat.is_forum = False
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is True
    project_threads_manager.resolve_project.assert_not_called()


async def test_private_mode_start_inside_topic_uses_thread_context(
    private_thread_settings, deps
):
    """/start in private topic should load mapped thread context."""
    orchestrator = MessageOrchestrator(private_thread_settings, deps)
    project_path = private_thread_settings.approved_directory / "project_a"
    project = SimpleNamespace(
        slug="project_a",
        name="Project A",
        absolute_path=project_path,
    )
    project_threads_manager = MagicMock()
    project_threads_manager.resolve_project = AsyncMock(return_value=project)
    project_threads_manager.guidance_message.return_value = "Use project topic"
    deps["project_threads_manager"] = project_threads_manager

    captured = {"dir": None}

    async def start_command(update, context):
        captured["dir"] = context.user_data.get("current_directory")

    wrapped = orchestrator._inject_deps(start_command)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.effective_message.message_thread_id = 777
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {
        "thread_state": {
            "12345:777": {
                "current_directory": str(project_path),
                "claude_session_id": "old",
            }
        }
    }

    await wrapped(update, context)

    project_threads_manager.resolve_project.assert_awaited_once_with(12345, 777)
    assert captured["dir"] == project_path


async def test_private_mode_rejects_help_outside_topics(private_thread_settings, deps):
    """Private mode rejects non-allowed commands outside mapped topics."""
    orchestrator = MessageOrchestrator(private_thread_settings, deps)
    called = {"value": False}

    async def help_command(update, context):
        called["value"] = True

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project topic"
    deps["project_threads_manager"] = project_threads_manager

    wrapped = orchestrator._inject_deps(help_command)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.effective_chat.is_forum = False
    update.effective_message.message_thread_id = None
    update.effective_message.direct_messages_topic = None
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is False
    update.effective_message.reply_text.assert_called_once()


# --- /sessions user + project isolation -------------------------------------


@pytest.fixture
def isolation_settings(tmp_dir):
    """Two Telegram users allowed, neither of them an admin."""
    return create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=True,
        allowed_users=[111, 222],
        admin_users=[],
    )


@pytest.fixture
def admin_isolation_settings(tmp_dir):
    return create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=True,
        allowed_users=[111, 222],
        admin_users=[111],
    )


def _sessions_update(user_id: int = 111) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def _resume_update(user_id: int = 111, session_id: str = "s-1") -> MagicMock:
    update = MagicMock()
    update.callback_query.data = f"resume:{session_id}"
    update.callback_query.from_user.id = user_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _storage_mock() -> MagicMock:
    storage = MagicMock()
    storage.sessions.get_user_sessions = AsyncMock(return_value=[])
    storage.sessions.get_session = AsyncMock(return_value=None)
    return storage


def _session_record(session_id: str, user_id: int, project_path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        user_id=user_id,
        project_path=str(project_path),
        last_used=datetime.now(UTC),
    )


def _ctx(storage) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"storage": storage}
    context.user_data = {}
    return context


async def test_sessions_only_queries_callers_own_sessions(
    isolation_settings, deps, tmp_dir
):
    """A regular user's list is built from sessions keyed to their own ID."""
    orchestrator = MessageOrchestrator(isolation_settings, deps)
    storage = _storage_mock()
    storage.sessions.get_user_sessions.return_value = [
        _session_record("own-session-1", 111, tmp_dir)
    ]

    update = _sessions_update(user_id=111)
    await orchestrator.agentic_sessions(update, _ctx(storage))

    storage.sessions.get_user_sessions.assert_awaited_once_with(111, active_only=False)
    assert "own-sess" in update.message.reply_text.call_args[0][0]


async def test_sessions_excludes_sessions_outside_the_scoping_root(
    isolation_settings, deps, tmp_dir
):
    """Own sessions whose project_path escapes the root are not listed."""
    orchestrator = MessageOrchestrator(isolation_settings, deps)
    storage = _storage_mock()
    storage.sessions.get_user_sessions.return_value = [
        _session_record("elsewhere-1", 111, tmp_dir.parent / "somewhere_else")
    ]

    update = _sessions_update(user_id=111)
    await orchestrator.agentic_sessions(update, _ctx(storage))

    assert update.message.reply_text.call_args[0][0] == "No sessions found."


async def test_sessions_hides_local_sessions_from_non_admin(
    isolation_settings, deps, tmp_dir, monkeypatch
):
    """Local ~/.claude/projects sessions are never scanned for a non-admin."""
    import src.claude.local_sessions as local_sessions_mod

    called = {"value": False}

    def _fake_list(**kwargs):
        called["value"] = True
        return []

    monkeypatch.setattr(local_sessions_mod, "list_all_local_sessions", _fake_list)

    orchestrator = MessageOrchestrator(isolation_settings, deps)
    update = _sessions_update(user_id=222)
    await orchestrator.agentic_sessions(update, _ctx(_storage_mock()))

    assert called["value"] is False
    assert update.message.reply_text.call_args[0][0] == "No sessions found."


async def test_sessions_shows_local_sessions_to_admin(
    admin_isolation_settings, deps, tmp_dir, monkeypatch
):
    """An admin still sees CLI/VS Code sessions discovered on disk."""
    import time

    import src.claude.local_sessions as local_sessions_mod
    from src.claude.local_sessions import LocalSession

    def _fake_list(**kwargs):
        return [
            LocalSession(
                session_id="local-abc12345",
                cwd=str(tmp_dir),
                timestamp=datetime.now(UTC),
                jsonl_path=tmp_dir / "x.jsonl",
                first_message="hello from vscode",
                mtime=time.time(),
            )
        ]

    monkeypatch.setattr(local_sessions_mod, "list_all_local_sessions", _fake_list)

    orchestrator = MessageOrchestrator(admin_isolation_settings, deps)
    update = _sessions_update(user_id=111)
    await orchestrator.agentic_sessions(update, _ctx(_storage_mock()))

    text = update.message.reply_text.call_args[0][0]
    assert "local-ab" in text
    assert "hello from vscode" in text


async def test_resume_rejects_another_users_session(isolation_settings, deps, tmp_dir):
    """Owner check is server-side: a forged callback for a foreign session fails."""
    orchestrator = MessageOrchestrator(isolation_settings, deps)
    storage = _storage_mock()
    storage.sessions.get_session.return_value = _session_record(
        "victim-session", 999, tmp_dir
    )

    update = _resume_update(user_id=111, session_id="victim-session")
    context = _ctx(storage)
    await orchestrator._agentic_resume_callback(update, context)

    assert "another user" in update.callback_query.edit_message_text.call_args[0][0]
    assert "claude_session_id" not in context.user_data
    assert "current_directory" not in context.user_data


async def test_resume_rejects_session_from_another_project_root(
    isolation_settings, deps, tmp_dir
):
    """A session the user owns but that lives outside the topic root is refused."""
    project_a = tmp_dir / "project_a"
    project_b = tmp_dir / "project_b"
    project_a.mkdir()
    project_b.mkdir()

    orchestrator = MessageOrchestrator(isolation_settings, deps)
    storage = _storage_mock()
    storage.sessions.get_session.return_value = _session_record(
        "b-session", 111, project_b
    )

    update = _resume_update(user_id=111, session_id="b-session")
    context = _ctx(storage)
    # Pin the caller to project_a's topic.
    context.user_data["_thread_context"] = {"project_root": str(project_a)}

    await orchestrator._agentic_resume_callback(update, context)

    assert (
        "current project root"
        in update.callback_query.edit_message_text.call_args[0][0]
    )
    assert "claude_session_id" not in context.user_data


async def test_resume_local_session_denied_for_non_admin(
    isolation_settings, deps, tmp_dir, monkeypatch
):
    """An unknown (local-only) session ID is not resumable by a non-admin."""
    import src.claude.local_sessions as local_sessions_mod

    scanned = {"value": False}

    def _fake_head(path):
        scanned["value"] = True
        return {"cwd": str(tmp_dir)}

    monkeypatch.setattr(local_sessions_mod, "_parse_session_head", _fake_head)

    orchestrator = MessageOrchestrator(isolation_settings, deps)
    update = _resume_update(user_id=222, session_id="cli-session")
    context = _ctx(_storage_mock())

    await orchestrator._agentic_resume_callback(update, context)

    assert "not available to you" in (
        update.callback_query.edit_message_text.call_args[0][0]
    )
    assert scanned["value"] is False
    assert "claude_session_id" not in context.user_data


async def test_resume_allows_own_session_in_root(isolation_settings, deps, tmp_dir):
    """The happy path still works for a user's own in-root session."""
    orchestrator = MessageOrchestrator(isolation_settings, deps)
    storage = _storage_mock()
    storage.sessions.get_session.return_value = _session_record("mine-1", 111, tmp_dir)

    update = _resume_update(user_id=111, session_id="mine-1")
    context = _ctx(storage)
    await orchestrator._agentic_resume_callback(update, context)

    assert context.user_data["claude_session_id"] == "mine-1"
    assert context.user_data["current_directory"] == Path(str(tmp_dir))


# --- Cost reservations are always settled -----------------------------------
#
# The budget hold is taken before the Claude run and must be released on every
# exit path. A leaked hold is invisible until the user's budget is exhausted by
# runs that never cost anything, which is exactly the bug this guards.


def _reserving_limiter():
    limiter = MagicMock()
    limiter.reserve_cost = AsyncMock(return_value=("rid-1", None))
    limiter.settle_reservation = AsyncMock()
    return limiter


def _cost_update():
    update = MagicMock()
    update.effective_user.id = 123
    update.message.text = "run something"
    update.message.message_id = 1
    update.message.chat.type = "private"
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_text.return_value = AsyncMock()
    return update


def _cost_context(agentic_settings, claude_integration, limiter):
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "claude_integration": claude_integration,
        "storage": None,
        "rate_limiter": limiter,
        "audit_logger": None,
    }
    return context


async def test_agentic_text_settles_reservation_with_actual_cost(
    agentic_settings, deps
):
    """A successful run settles the hold at its real cost."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    response = MagicMock()
    response.session_id = "s-1"
    response.content = "done"
    response.tools_used = []
    response.is_error = False
    response.cost = 1.23

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(return_value=response)
    limiter = _reserving_limiter()

    await orchestrator.agentic_text(
        _cost_update(), _cost_context(agentic_settings, claude_integration, limiter)
    )

    limiter.settle_reservation.assert_awaited_once_with("rid-1", 1.23)


async def test_agentic_text_settles_at_zero_on_soft_error(agentic_settings, deps):
    """A run the SDK flagged as an error produced nothing usable — charge 0."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    response = MagicMock()
    response.session_id = "s-1"
    response.is_error = True
    response.error_type = "no_result_message"
    response.cost = 9.99  # must NOT be charged

    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(return_value=response)
    limiter = _reserving_limiter()

    await orchestrator.agentic_text(
        _cost_update(), _cost_context(agentic_settings, claude_integration, limiter)
    )

    limiter.settle_reservation.assert_awaited_once_with("rid-1", 0.0)


async def test_agentic_text_settles_at_zero_when_run_raises(agentic_settings, deps):
    """An exception mid-run must still release the hold."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock(side_effect=RuntimeError("boom"))
    limiter = _reserving_limiter()

    await orchestrator.agentic_text(
        _cost_update(), _cost_context(agentic_settings, claude_integration, limiter)
    )

    limiter.settle_reservation.assert_awaited_once_with("rid-1", 0.0)


async def test_agentic_text_does_not_run_when_reservation_refused(
    agentic_settings, deps
):
    """No budget, no run — and nothing to settle."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    claude_integration = AsyncMock()
    claude_integration.run_command = AsyncMock()
    limiter = MagicMock()
    limiter.reserve_cost = AsyncMock(return_value=(None, "Cost limit exceeded"))
    limiter.settle_reservation = AsyncMock()

    await orchestrator.agentic_text(
        _cost_update(), _cost_context(agentic_settings, claude_integration, limiter)
    )

    claude_integration.run_command.assert_not_called()
    limiter.settle_reservation.assert_not_awaited()
