"""Test Claude SDK integration."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk.types import StreamEvent

from src.claude.sdk_integration import (
    ClaudeResponse,
    ClaudeSDKManager,
    StreamUpdate,
    _iter_sdk_messages,
    _make_can_use_tool_callback,
)
from src.config.settings import Settings


@pytest.fixture(autouse=True)
def _patch_parse_message():
    """Patch parse_message as identity so mocks can yield typed Message objects."""
    with patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x):
        yield


def _make_assistant_message(text="Test response"):
    """Create an AssistantMessage with proper structure for current SDK version."""
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-20250514",
    )


def _make_result_message(**kwargs):
    """Create a ResultMessage with sensible defaults."""
    defaults = {
        "subtype": "success",
        "duration_ms": 1000,
        "duration_api_ms": 800,
        "is_error": False,
        "num_turns": 1,
        "session_id": "test-session",
        "total_cost_usd": 0.05,
        "result": "Success",
    }
    defaults.update(kwargs)
    return ResultMessage(**defaults)


def _mock_client(*messages):
    """Create a mock ClaudeSDKClient that yields the given messages.

    Returns a factory function suitable for patching ClaudeSDKClient.
    Uses connect()/disconnect() pattern (not async context manager).
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive_raw_messages():
        for msg in messages:
            yield msg

    query_mock = AsyncMock()
    query_mock.receive_messages = receive_raw_messages
    client._query = query_mock

    return client


def _mock_client_factory(*messages, capture_options=None):
    """Create a factory that returns a mock client, optionally capturing options."""

    def factory(options):
        if capture_options is not None:
            capture_options.append(options)
        return _mock_client(*messages)

    return factory


class TestClaudeSDKManager:
    """Test Claude SDK manager."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config without API key."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,  # Short timeout for testing
            enable_mcp=False,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_sdk_manager_initialization_with_api_key(self, tmp_path):
        """SDK manager must NOT write the API key into ``os.environ``.

        Writing it would leak the key to every subprocess of this bot
        (including MCP servers and shell tools). Instead, the key is
        passed to the Claude CLI via ``ClaudeAgentOptions.env`` at
        command-execution time.
        """
        from src.config.settings import Settings

        # Test with API key provided
        config_with_key = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            anthropic_api_key="test-api-key",
            claude_timeout_seconds=2,
        )

        # Snapshot pre-test state so we can assert it is unchanged.
        pre_value = os.environ.get("ANTHROPIC_API_KEY")

        try:
            manager = ClaudeSDKManager(config_with_key)

            # The key MUST NOT be pushed into the global environment.
            assert os.environ.get("ANTHROPIC_API_KEY") == pre_value
            # It MUST be available via the config for the CLI subprocess.
            assert manager.config.anthropic_api_key_str == "test-api-key"

        finally:
            # Defensive: restore pre-test state in case a future regression
            # starts writing to os.environ again.
            if pre_value is None and "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]
            elif pre_value is not None:
                os.environ["ANTHROPIC_API_KEY"] = pre_value

    async def test_sdk_manager_initialization_without_api_key(self, config):
        """Test SDK manager initialization without API key (uses CLI auth)."""
        # Store original env var
        original_api_key = os.environ.get("ANTHROPIC_API_KEY")

        try:
            # Remove any existing API key
            if "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

            ClaudeSDKManager(config)

            # Check that no API key was set (should use CLI auth)
            assert config.anthropic_api_key_str is None

        finally:
            # Restore original env var
            if original_api_key:
                os.environ["ANTHROPIC_API_KEY"] = original_api_key

    async def test_execute_command_success(self, sdk_manager):
        """Test successful command execution."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="test-session", total_cost_usd=0.05),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="test-session",
            )

        # Verify response
        assert isinstance(response, ClaudeResponse)
        assert response.session_id == "test-session"
        assert response.duration_ms >= 0
        assert not response.is_error
        assert response.cost == 0.05

    async def test_execute_command_uses_result_content(self, sdk_manager):
        """Test that ResultMessage.result is used for content when available."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Assistant text"),
            _make_result_message(result="Final result from ResultMessage"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.content == "Final result from ResultMessage"

    async def test_execute_command_falls_back_to_messages(self, sdk_manager):
        """Test fallback to message extraction when result is None."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Extracted from messages"),
            _make_result_message(result=None),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.content == "Extracted from messages"

    async def test_execute_command_with_streaming(self, sdk_manager):
        """Test command execution with streaming callback."""
        stream_updates = []

        async def stream_callback(update: StreamUpdate):
            stream_updates.append(update)

        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                stream_callback=stream_callback,
            )

        # Verify streaming was called
        assert len(stream_updates) > 0
        assert any(update.type == "assistant" for update in stream_updates)

    async def test_execute_command_timeout(self, sdk_manager):
        """Test command execution timeout."""
        from src.claude.exceptions import ClaudeTimeoutError

        # 50 ms instead of the fixture's 2 s: what matters is that the hang is
        # cut off, not how long the suite waits for it. ``model_copy(update=)``
        # is the way in — ``Settings`` validates assignment and the field is an
        # ``int``, so a sub-second timeout cannot be set with plain setattr.
        sdk_manager.config = sdk_manager.config.model_copy(
            update={"claude_timeout_seconds": 0.05}
        )

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()

        async def hanging_receive():
            await asyncio.sleep(5)  # Far past the 50 ms timeout above
            yield  # Never reached

        query_mock = AsyncMock()
        query_mock.receive_messages = hanging_receive
        client._query = query_mock

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeTimeoutError):
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

    async def test_execute_command_passes_mcp_config(self, tmp_path):
        """Test that MCP config is passed to ClaudeAgentOptions when enabled."""
        # Create a valid MCP config file
        mcp_config_file = tmp_path / "mcp_config.json"
        mcp_config_file.write_text(
            '{"mcpServers": {"test-server": {"command": "echo", "args": ["hello"]}}}'
        )

        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            enable_mcp=True,
            mcp_config_path=str(mcp_config_file),
        )

        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        # Verify MCP config was parsed and passed as dict to options
        assert len(captured_options) == 1
        assert captured_options[0].mcp_servers == {
            "test-server": {"command": "echo", "args": ["hello"]}
        }

    async def test_execute_command_no_mcp_when_disabled(self, sdk_manager):
        """Test that MCP config is NOT passed when MCP is disabled."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        # Verify MCP config was NOT set (should be empty default)
        assert len(captured_options) == 1
        assert captured_options[0].mcp_servers == {}

    async def test_execute_command_passes_resume_session(self, sdk_manager):
        """Test that session_id is passed as options.resume for continuation."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="test-session"),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Continue working",
                working_directory=Path("/test"),
                session_id="existing-session-id",
                continue_session=True,
            )

        assert len(captured_options) == 1
        assert captured_options[0].resume == "existing-session-id"

    async def test_execute_command_passes_max_budget_usd(self, sdk_manager, config):
        """Test that max_budget_usd is passed from config to ClaudeAgentOptions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert len(captured_options) == 1
        assert captured_options[0].max_budget_usd == config.claude_max_cost_per_request

    async def test_execute_command_no_resume_for_new_session(self, sdk_manager):
        """Test that resume is not set for new sessions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id="new-session"),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="New prompt",
                working_directory=Path("/test"),
                session_id=None,
                continue_session=False,
            )

        assert len(captured_options) == 1
        assert (
            not hasattr(captured_options[0], "resume") or not captured_options[0].resume
        )

    async def test_retry_on_transient_cli_connection_error(self, sdk_manager):
        """Test that transient CLIConnectionError triggers retry and succeeds."""
        from claude_agent_sdk import CLIConnectionError

        call_count = 0

        async def flaky_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise CLIConnectionError("connection reset")
            # Second attempt succeeds - yield a ResultMessage
            yield

        # Use a config with 2 attempts
        sdk_manager.config.claude_retry_max_attempts = 2

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        query_mock = AsyncMock()
        query_mock.receive_messages = flaky_receive
        client._query = query_mock

        # Should not raise - second attempt succeeds
        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                try:
                    await sdk_manager.execute_command(
                        prompt="Test",
                        working_directory=Path("/test"),
                    )
                except Exception:
                    pass  # Response parsing may fail - what matters is retry happened
        assert call_count == 2

    async def test_no_retry_after_partial_progress(self, sdk_manager):
        """A connection error AFTER a message arrived must NOT retry — tool calls
        may already have executed, so replaying could double-run a mutation."""
        from claude_agent_sdk import AssistantMessage, CLIConnectionError, TextBlock

        sdk_manager.config.claude_retry_max_attempts = 3
        call_count = 0

        async def progressed_then_fail():
            nonlocal call_count
            call_count += 1
            # One message streams in successfully (prompt reached Claude)...
            yield AssistantMessage(content=[TextBlock(text="hi")], model="m")
            # ...then the connection drops mid-flight.
            raise CLIConnectionError("connection reset")

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        query_mock = AsyncMock()
        query_mock.receive_messages = progressed_then_fail
        client._query = query_mock

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with patch(
                "src.claude.sdk_integration.parse_message", side_effect=lambda x: x
            ):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    with pytest.raises(Exception):
                        await sdk_manager.execute_command(
                            prompt="Test",
                            working_directory=Path("/test"),
                        )
        # Exactly one attempt — no retry once a message had been received.
        assert call_count == 1

    async def test_no_retry_on_mcp_connection_error(self, sdk_manager):
        """Test that MCP CLIConnectionError is NOT retried."""
        from claude_agent_sdk import CLIConnectionError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(side_effect=CLIConnectionError("mcp server failed"))

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises((ClaudeMCPError, Exception)):
                await sdk_manager.execute_command(
                    prompt="Test",
                    working_directory=Path("/test"),
                )
        # Only called once - no retry for MCP errors
        assert client.query.call_count == 1

    async def test_retry_disabled_when_max_attempts_zero(self, sdk_manager):
        """Test that setting max_attempts=0 effectively disables retries (1 attempt)."""
        sdk_manager.config.claude_retry_max_attempts = 0
        assert max(1, sdk_manager.config.claude_retry_max_attempts) == 1

    def test_is_retryable_error_transient(self, sdk_manager):
        """Test _is_retryable_error returns True for transient connection errors."""
        from claude_agent_sdk import CLIConnectionError

        assert (
            sdk_manager._is_retryable_error(CLIConnectionError("connection reset"))
            is True
        )

    def test_is_retryable_error_mcp(self, sdk_manager):
        """Test _is_retryable_error returns False for MCP errors."""
        from claude_agent_sdk import CLIConnectionError

        assert (
            sdk_manager._is_retryable_error(CLIConnectionError("mcp server failed"))
            is False
        )

    def test_is_retryable_error_timeout(self, sdk_manager):
        """Test _is_retryable_error returns False for timeout errors."""
        assert sdk_manager._is_retryable_error(asyncio.TimeoutError()) is False


class TestClaudeSandboxSettings:
    """Test sandbox and system_prompt settings on ClaudeAgentOptions."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config with sandbox enabled."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            sandbox_enabled=True,
            sandbox_excluded_commands=["git", "npm"],
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_sandbox_settings_passed_to_options(self, sdk_manager, tmp_path):
        """Test that sandbox settings are set on ClaudeAgentOptions."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        opts = captured_options[0]
        assert opts.sandbox == {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "excludedCommands": ["git", "npm"],
        }

    async def test_system_prompt_set_with_working_directory(
        self, sdk_manager, tmp_path
    ):
        """Test that system_prompt references the working directory."""
        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        opts = captured_options[0]
        assert str(tmp_path) in opts.system_prompt
        assert "relative paths" in opts.system_prompt.lower()

    async def test_disallowed_tools_passed_to_options(self, tmp_path):
        """Test that disallowed_tools from config are passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_disallowed_tools=["WebFetch", "WebSearch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].disallowed_tools == ["WebFetch", "WebSearch"]

    async def test_allowed_tools_passed_to_options(self, tmp_path):
        """Test that allowed_tools from config are passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_allowed_tools=["Read", "Write", "Bash"],
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].allowed_tools == ["Read", "Write", "Bash"]

    async def test_disable_tool_validation_sets_allowed_tools_none(self, tmp_path):
        """allowed_tools=None when DISABLE_TOOL_VALIDATION=true."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            disable_tool_validation=True,
            claude_allowed_tools=["Read", "Write", "Bash"],
            claude_disallowed_tools=["WebFetch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].allowed_tools is None
        assert captured_options[0].disallowed_tools is None

    async def test_tool_validation_enabled_passes_configured_tools(self, tmp_path):
        """allowed/disallowed_tools passed when DISABLE_TOOL_VALIDATION=false."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            disable_tool_validation=False,
            claude_allowed_tools=["Read", "Write"],
            claude_disallowed_tools=["WebFetch"],
        )
        manager = ClaudeSDKManager(config)

        captured_options: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].allowed_tools == ["Read", "Write"]
        assert captured_options[0].disallowed_tools == ["WebFetch"]

    async def test_empty_cli_path_coerced_to_none(self, tmp_path):
        """Empty CLAUDE_CLI_PATH ('') is coerced to None so SDK auto-discovers the CLI."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_cli_path="",
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].cli_path is None

    async def test_sandbox_disabled_when_config_false(self, tmp_path):
        """Test sandbox is disabled when sandbox_enabled=False."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            sandbox_enabled=False,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].sandbox["enabled"] is False

    async def test_claude_model_passed_to_options(self, tmp_path):
        """Test that claude_model from config is passed to ClaudeAgentOptions."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            claude_model="claude-sonnet-4-20250514",
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].model == "claude-sonnet-4-20250514"

    async def test_claude_model_none_when_unset(self, tmp_path):
        """Test that model is None when claude_model is not configured."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(
                prompt="Test prompt",
                working_directory=tmp_path,
            )

        assert len(captured_options) == 1
        assert captured_options[0].model is None


class TestClaudeMCPErrors:
    """Test MCP-specific error handling."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create test config."""
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        """Create SDK manager."""
        return ClaudeSDKManager(config)

    async def test_mcp_connection_error_raises_mcp_error(self, sdk_manager):
        """Test that MCP connection errors raise ClaudeMCPError."""
        from claude_agent_sdk import CLIConnectionError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(
            side_effect=CLIConnectionError("MCP server failed to start")
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeMCPError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "MCP server" in str(exc_info.value)

    async def test_mcp_process_error_raises_mcp_error(self, sdk_manager):
        """Test that MCP process errors raise ClaudeMCPError."""
        from claude_agent_sdk import ProcessError

        from src.claude.exceptions import ClaudeMCPError

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock(
            side_effect=ProcessError("Failed to start MCP server: connection refused")
        )

        with patch("src.claude.sdk_integration.ClaudeSDKClient", return_value=client):
            with pytest.raises(ClaudeMCPError) as exc_info:
                await sdk_manager.execute_command(
                    prompt="Test prompt",
                    working_directory=Path("/test"),
                )

        assert "MCP" in str(exc_info.value)


class TestCanUseToolCallback:
    """Test the _make_can_use_tool_callback factory and its behavior."""

    @pytest.fixture
    def approved_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def working_dir(self, tmp_path):
        return tmp_path / "project"

    @pytest.fixture
    def security_validator(self):
        """Create a mock SecurityValidator."""
        validator = MagicMock()
        validator.validate_path = MagicMock(return_value=(True, Path("/ok"), None))
        validator.validate_filename = MagicMock(return_value=(True, None))
        validator.is_forbidden_secret_file = MagicMock(return_value=(False, None))
        return validator

    @pytest.fixture
    def callback(self, security_validator, working_dir, approved_dir):
        return _make_can_use_tool_callback(
            security_validator=security_validator,
            working_directory=working_dir,
            approved_directory=approved_dir,
        )

    @pytest.fixture
    def context(self):
        return ToolPermissionContext()

    async def test_allows_safe_file_read(self, callback, context, security_validator):
        """File read with a valid path is allowed."""
        result = await callback("Read", {"file_path": "src/main.py"}, context)
        assert isinstance(result, PermissionResultAllow)
        security_validator.validate_path.assert_called_once()

    async def test_denies_invalid_file_path(
        self, callback, context, security_validator
    ):
        """File write with a path that fails validation is denied."""
        security_validator.validate_path.return_value = (
            False,
            None,
            "Path traversal detected",
        )
        result = await callback("Write", {"file_path": "../../etc/passwd"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "Path traversal" in result.message

    async def test_allows_bash_inside_boundary(
        self, callback, context, working_dir, approved_dir
    ):
        """Bash command targeting inside approved dir is allowed."""
        result = await callback(
            "Bash", {"command": f"mkdir -p {approved_dir}/subdir"}, context
        )
        assert isinstance(result, PermissionResultAllow)

    async def test_denies_bash_outside_boundary(self, callback, context):
        """Bash command targeting outside approved dir is denied."""
        result = await callback("Bash", {"command": "mkdir -p /tmp/evil"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "boundary violation" in result.message.lower()

    async def test_allows_unknown_tool(self, callback, context):
        """Tools not in file/bash sets are allowed through."""
        result = await callback("Grep", {"pattern": "foo"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_allows_bash_read_only_command(self, callback, context):
        """Read-only bash commands with no filesystem path pass through."""
        result = await callback("Bash", {"command": "whoami"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_denies_read_only_command_outside_boundary(self, callback, context):
        """Read-only commands reading outside the approved root are denied."""
        result = await callback("Bash", {"command": "cat /etc/hosts"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "boundary violation" in result.message.lower()

    async def test_file_tool_without_path_allowed(self, callback, context):
        """File tool call without a path key is allowed (no path to validate)."""
        result = await callback("Read", {"content": "something"}, context)
        assert isinstance(result, PermissionResultAllow)

    async def test_denies_forbidden_basename_in_boundary(
        self, callback, context, security_validator
    ):
        """A Read of an in-boundary secret (.env) is denied via the blocklist.

        validate_path enforces only the directory boundary, so the basename
        secret-blocklist must be re-applied to catch secrets inside the
        approved dir. We use is_forbidden_secret_file (not validate_filename)
        to avoid over-blocking legitimate in-repo files.
        """
        security_validator.is_forbidden_secret_file.return_value = (
            True,
            "Forbidden filename: .env",
        )
        result = await callback("Read", {"file_path": ".env"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "Forbidden filename" in result.message
        security_validator.is_forbidden_secret_file.assert_called_once_with(
            ".env", within_approved=True
        )

    async def test_wired_into_sdk_manager(self, tmp_path):
        """SecurityValidator is wired into options.can_use_tool by execute_command."""
        validator = MagicMock()
        validator.validate_path = MagicMock(return_value=(True, tmp_path, None))

        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config, security_validator=validator)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(prompt="Test", working_directory=tmp_path)

        assert len(captured_options) == 1
        assert captured_options[0].can_use_tool is not None

    async def test_no_callback_without_security_validator(self, tmp_path):
        """Verify can_use_tool is None when no SecurityValidator is provided."""
        config = Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )
        manager = ClaudeSDKManager(config)

        captured_options = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(total_cost_usd=0.01),
            capture_options=captured_options,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await manager.execute_command(prompt="Test", working_directory=tmp_path)

        assert len(captured_options) == 1
        assert captured_options[0].can_use_tool is None


class TestIterSdkMessages:
    """Pin the raw-stream path and its public-API fallback.

    ``client._query`` is private SDK API: if a release renames or drops it, the
    fallback must keep the bot running instead of AttributeError-ing on every
    message.
    """

    async def test_uses_raw_query_stream_when_available(self):
        client = _mock_client(_make_assistant_message("from raw"))
        client.receive_messages = MagicMock(
            side_effect=AssertionError("public API must not be used")
        )

        messages = [m async for m in _iter_sdk_messages(client)]

        assert len(messages) == 1
        assert messages[0].content[0].text == "from raw"

    async def test_falls_back_to_public_receive_messages(self):
        client = AsyncMock()
        # Simulate an SDK version without the private query object.
        client._query = None

        async def receive_messages():
            yield _make_assistant_message("from public")

        client.receive_messages = receive_messages

        messages = [m async for m in _iter_sdk_messages(client)]

        assert len(messages) == 1
        assert messages[0].content[0].text == "from public"

    async def test_unparseable_message_does_not_stop_the_stream(self):
        client = _mock_client("bad", _make_result_message())

        with patch(
            "src.claude.sdk_integration.parse_message",
            side_effect=lambda x: (
                (_ for _ in ()).throw(MessageParseError("nope", x)) if x == "bad" else x
            ),
        ):
            messages = [m async for m in _iter_sdk_messages(client)]

        # The unparseable message is skipped; the ResultMessage still arrives.
        assert len(messages) == 1
        assert isinstance(messages[0], ResultMessage)


class TestSessionIdFallback:
    """Test fallback session ID extraction from StreamEvent messages."""

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_session_id_from_stream_event_fallback(self, sdk_manager):
        """Test that session_id is extracted from StreamEvent when ResultMessage has None."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-123",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "stream-session-123"

    async def test_session_id_from_stream_event_empty_string(self, sdk_manager):
        """Test fallback triggers when ResultMessage session_id is empty string."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-456",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id="", result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "stream-session-456"

    async def test_no_fallback_when_result_has_session_id(self, sdk_manager):
        """Test that ResultMessage session_id takes priority over StreamEvent."""
        stream_event = StreamEvent(
            uuid="evt-1",
            session_id="stream-session-999",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event,
            _make_assistant_message("Test response"),
            _make_result_message(session_id="result-session-abc", result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        # ResultMessage session_id should win
        assert response.session_id == "result-session-abc"

    async def test_fallback_skips_stream_events_without_session_id(self, sdk_manager):
        """Test that StreamEvents without session_id are skipped in fallback."""
        stream_event_no_id = StreamEvent(
            uuid="evt-1",
            session_id=None,
            event={"type": "content_block_start"},
        )
        stream_event_with_id = StreamEvent(
            uuid="evt-2",
            session_id="found-session",
            event={"type": "content_block_delta"},
        )
        mock_factory = _mock_client_factory(
            stream_event_no_id,
            stream_event_with_id,
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
            )

        assert response.session_id == "found-session"

    async def test_no_session_id_anywhere_falls_back_to_input(self, sdk_manager):
        """Test that input session_id is used when neither ResultMessage nor StreamEvent provide one."""
        mock_factory = _mock_client_factory(
            _make_assistant_message("Test response"),
            _make_result_message(session_id=None, result="Done"),
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            response = await sdk_manager.execute_command(
                prompt="Test prompt",
                working_directory=Path("/test"),
                session_id="input-session-id",
            )

        # Should fall back to the input session_id
        assert response.session_id == "input-session-id"


class TestClaudeMdLoading:
    """Tests for CLAUDE.md loading from working directory."""

    @pytest.fixture
    def config(self, tmp_path):
        return Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="test_bot",
            approved_directory=str(tmp_path),
        )

    @pytest.fixture
    def sdk_manager(self, config):
        return ClaudeSDKManager(config)

    async def test_claude_md_appended_to_system_prompt(self, sdk_manager, tmp_path):
        """CLAUDE.md content is appended to system prompt when present."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Project Rules\nAlways use type hints.")

        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert "# Project Rules" in opts.system_prompt
        assert "Always use type hints." in opts.system_prompt

    async def test_system_prompt_unchanged_without_claude_md(
        self, sdk_manager, tmp_path
    ):
        """System prompt is just the base when no CLAUDE.md exists."""
        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        opts = captured[0]
        assert "Use relative paths." in opts.system_prompt
        assert "# Project Rules" not in opts.system_prompt

    async def test_project_settings_not_trusted_by_default(self, sdk_manager, tmp_path):
        """<cwd>/.claude/settings.json is not loaded unless explicitly trusted.

        Its hooks execute arbitrary commands, which makes it a stronger vector
        than the CLAUDE.md the same run already wraps as untrusted data.
        """
        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        assert captured[0].setting_sources == []

    async def test_project_settings_loaded_when_trusted(self, sdk_manager, tmp_path):
        """TRUST_PROJECT_SETTINGS=true opts back in to ['project']."""
        sdk_manager.config.trust_project_settings = True
        captured: list = []
        mock_factory = _mock_client_factory(
            _make_assistant_message("ok"),
            _make_result_message(),
            capture_options=captured,
        )

        with patch(
            "src.claude.sdk_integration.ClaudeSDKClient", side_effect=mock_factory
        ):
            await sdk_manager.execute_command(prompt="test", working_directory=tmp_path)

        assert captured[0].setting_sources == ["project"]


class TestToolPathBoundary:
    """TOOL_PATH_BOUNDARY=working confines tools to the run's own project.

    ``validate_path``'s second argument only resolves relative paths — the
    boundary was always APPROVED_DIRECTORY. In project-thread mode that made the
    per-topic project isolation UI-deep: a run pinned to one project could read
    and write a sibling project under the same approved root.
    """

    @staticmethod
    def _callback(tmp_path, *, working: bool):
        from src.claude.sdk_integration import _make_can_use_tool_callback
        from src.security.validators import SecurityValidator

        approved = tmp_path / "projects"
        (approved / "mine").mkdir(parents=True)
        (approved / "theirs").mkdir(parents=True)
        return (
            _make_can_use_tool_callback(
                security_validator=SecurityValidator(approved),
                working_directory=approved / "mine",
                approved_directory=approved,
                boundary_is_working_directory=working,
            ),
            approved,
        )

    async def test_sibling_project_allowed_under_approved_boundary(self, tmp_path):
        callback, approved = self._callback(tmp_path, working=False)
        result = await callback(
            "Read", {"file_path": str(approved / "theirs" / "notes.md")}, None
        )
        assert isinstance(result, PermissionResultAllow)

    async def test_sibling_project_denied_under_working_boundary(self, tmp_path):
        callback, approved = self._callback(tmp_path, working=True)
        result = await callback(
            "Read", {"file_path": str(approved / "theirs" / "notes.md")}, None
        )
        assert isinstance(result, PermissionResultDeny)

    async def test_own_project_still_allowed_under_working_boundary(self, tmp_path):
        callback, approved = self._callback(tmp_path, working=True)
        result = await callback(
            "Read", {"file_path": str(approved / "mine" / "notes.md")}, None
        )
        assert isinstance(result, PermissionResultAllow)

    async def test_bash_boundary_narrows_too(self, tmp_path):
        callback, approved = self._callback(tmp_path, working=True)
        # POSIX separators: shlex treats a backslash as an escape, exactly as the
        # shell does, so a Windows-style path is not a meaningful input here.
        target = (approved / "theirs" / "notes.md").as_posix()
        result = await callback("Bash", {"command": f"cat {target}"}, None)
        assert isinstance(result, PermissionResultDeny)

    async def test_bash_inside_own_project_still_allowed(self, tmp_path):
        callback, approved = self._callback(tmp_path, working=True)
        target = (approved / "mine" / "notes.md").as_posix()
        result = await callback("Bash", {"command": f"cat {target}"}, None)
        assert isinstance(result, PermissionResultAllow)
