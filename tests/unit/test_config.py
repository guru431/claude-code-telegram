"""Test configuration loading and validation."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import src.config.loader as config_loader
from src.config import Settings, create_test_config, load_config
from src.config.features import FeatureFlags
from src.exceptions import ConfigurationError


def test_settings_validation_required_fields(monkeypatch):
    """Test that missing required fields raise validation errors."""
    # Clear any environment variables that might provide defaults
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    errors = exc_info.value.errors()
    required_fields = {error["loc"][0] for error in errors}
    assert "telegram_bot_token" in required_fields
    assert "telegram_bot_username" in required_fields
    assert "approved_directory" in required_fields


def test_settings_with_valid_data(tmp_path):
    """Test settings creation with valid data."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
    )

    assert settings.telegram_token_str == "test_token"
    assert settings.telegram_bot_username == "test_bot"
    assert settings.approved_directory == test_dir


def test_allowed_users_parsing():
    """Test parsing of comma-separated user IDs."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            allowed_users="123,456,789",
        )

        assert settings.allowed_users == [123, 456, 789]


def test_is_admin_falls_back_to_allowed_users():
    """When ADMIN_USERS is unset, every ALLOWED_USERS entry is an admin."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            allowed_users="123,456",
        )

        assert settings.admin_users is None
        assert settings.is_admin(123) is True
        assert settings.is_admin(456) is True
        assert settings.is_admin(999) is False


def test_is_admin_restricts_to_admin_users_when_set():
    """ADMIN_USERS narrows the privileged set below ALLOWED_USERS."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            allowed_users="123,456",
            admin_users="123",
        )

        assert settings.admin_users == [123]
        assert settings.is_admin(123) is True
        # An allowed-but-not-admin user is no longer privileged.
        assert settings.is_admin(456) is False


def test_is_admin_empty_admin_users_disables_everyone():
    """Explicit empty ADMIN_USERS= is a kill-switch distinct from unset."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            allowed_users="123,456",
            admin_users="",
        )

        assert settings.admin_users == []
        assert settings.is_admin(123) is False
        assert settings.is_admin(456) is False


def test_is_admin_false_when_no_lists_configured():
    """With neither list set (e.g. allow-all dev mode), nobody is an admin."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
        )

        assert settings.is_admin(123) is False


def test_allowed_users_parsing_with_spaces():
    """Test parsing with spaces around user IDs."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            allowed_users="123, 456 , 789",
        )

        assert settings.allowed_users == [123, 456, 789]


def test_security_relaxation_settings_defaults_and_overrides():
    """Security relaxation settings should default to False and be configurable."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        defaults = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
        )
        assert defaults.disable_security_patterns is False
        assert defaults.disable_tool_validation is False

        overridden = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            disable_security_patterns=True,
            disable_tool_validation=True,
        )
        assert overridden.disable_security_patterns is True
        assert overridden.disable_tool_validation is True


def test_approved_directory_validation_nonexistent():
    """Test validation fails for non-existent directory."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory="/nonexistent/directory",
        )

    assert "does not exist" in str(exc_info.value)


def test_approved_directory_validation_not_directory(tmp_path):
    """Test validation fails when path is not a directory."""
    test_file = tmp_path / "not_a_dir.txt"
    test_file.write_text("test")

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_file),
        )

    assert "not a directory" in str(exc_info.value)


def test_mcp_config_validation(tmp_path, monkeypatch):
    """Test MCP configuration validation."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    # Clear any MCP-related environment variables
    monkeypatch.delenv("ENABLE_MCP", raising=False)
    monkeypatch.delenv("MCP_CONFIG_PATH", raising=False)

    # Should fail when MCP enabled but no config path
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path=None,
        )

    assert "mcp_config_path required" in str(exc_info.value)

    # Should fail when config file doesn't exist
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path="/nonexistent/config.json",
        )

    assert "does not exist" in str(exc_info.value)

    # Should fail when config file is not valid JSON
    bad_json_file = tmp_path / "bad.json"
    bad_json_file.write_text("not json at all")

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path=str(bad_json_file),
        )

    assert "not valid JSON" in str(exc_info.value)

    # Should fail when config file is missing mcpServers key
    no_servers_file = tmp_path / "no_servers.json"
    no_servers_file.write_text('{"test": true}')

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path=str(no_servers_file),
        )

    assert "mcpServers" in str(exc_info.value)

    # Should fail when mcpServers is empty
    empty_servers_file = tmp_path / "empty_servers.json"
    empty_servers_file.write_text('{"mcpServers": {}}')

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            enable_mcp=True,
            mcp_config_path=str(empty_servers_file),
        )

    assert "at least one server" in str(exc_info.value)

    # Should succeed with valid MCP config
    config_file = tmp_path / "mcp_config.json"
    config_file.write_text(
        '{"mcpServers": {"my-server": '
        '{"command": "npx", "args": ["-y", "my-mcp-server"]}}}'
    )

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        enable_mcp=True,
        mcp_config_path=str(config_file),
    )

    assert settings.enable_mcp is True
    assert settings.mcp_config_path == config_file


def test_log_level_validation():
    """Test log level validation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Should fail with invalid log level
        with pytest.raises(ValidationError) as exc_info:
            Settings(
                telegram_bot_token="test_token",
                telegram_bot_username="test_bot",
                approved_directory=tmp_dir,
                log_level="INVALID",
            )

        assert "must be one of" in str(exc_info.value)

        # Should succeed with valid log level
        settings = Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=tmp_dir,
            log_level="debug",  # Should be converted to uppercase
        )

        assert settings.log_level == "DEBUG"


def test_project_threads_validation_requires_chat_id_in_group_mode(tmp_path):
    """Group thread mode requires project_threads_chat_id."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    app_dir = project_dir / "app"
    app_dir.mkdir()
    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: app\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_mode="group",
            projects_config_path=str(config_file),
        )

    assert "project_threads_chat_id required" in str(exc_info.value)


def test_project_threads_validation_requires_projects_config(tmp_path):
    """Thread mode requires projects_config_path."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_chat_id=-1001234567890,
            projects_config_path=None,
        )

    assert "projects_config_path required" in str(exc_info.value)


def test_project_threads_validation_blank_projects_config_path_fails(tmp_path):
    """Blank projects_config_path should be treated as missing."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_mode="private",
            projects_config_path="",
        )

    assert "projects_config_path required" in str(exc_info.value)


def test_project_threads_validation_private_mode_no_chat_id(tmp_path):
    """Private thread mode does not require project_threads_chat_id."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    app_dir = project_dir / "app"
    app_dir.mkdir()
    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: app\n",
        encoding="utf-8",
    )

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        enable_project_threads=True,
        project_threads_mode="private",
        projects_config_path=str(config_file),
    )

    assert settings.project_threads_mode == "private"
    assert settings.project_threads_chat_id is None


def test_project_threads_validation_private_mode_empty_chat_id(tmp_path):
    """Private mode accepts blank project_threads_chat_id from env/.env."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    app_dir = project_dir / "app"
    app_dir.mkdir()
    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: app\n",
        encoding="utf-8",
    )

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        enable_project_threads=True,
        project_threads_mode="private",
        project_threads_chat_id="",
        projects_config_path=str(config_file),
    )

    assert settings.project_threads_mode == "private"
    assert settings.project_threads_chat_id is None


def test_project_threads_validation_group_mode_empty_chat_id_fails(tmp_path):
    """Group mode rejects blank project_threads_chat_id."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    app_dir = project_dir / "app"
    app_dir.mkdir()
    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: app\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_mode="group",
            project_threads_chat_id="",
            projects_config_path=str(config_file),
        )

    assert "project_threads_chat_id required" in str(exc_info.value)


def test_project_threads_sync_action_interval_validation(tmp_path):
    """Thread sync action interval should accept non-negative values only."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        project_threads_sync_action_interval_seconds=0,
    )
    assert settings.project_threads_sync_action_interval_seconds == 0

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        project_threads_sync_action_interval_seconds="1.1",
    )
    assert settings.project_threads_sync_action_interval_seconds == pytest.approx(1.1)

    with pytest.raises(ValidationError):
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            project_threads_sync_action_interval_seconds=-0.1,
        )


def test_project_threads_validation_invalid_mode(tmp_path):
    """Invalid project thread mode should fail validation."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            enable_project_threads=True,
            project_threads_mode="invalid",
        )

    assert "project_threads_mode must be one of" in str(exc_info.value)


def test_voice_provider_validation_and_normalization(tmp_path):
    """VOICE_PROVIDER accepts only mistral/openai and normalizes casing."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        voice_provider="OPENAI",
    )

    assert settings.voice_provider == "openai"
    assert settings.voice_provider_api_key_env == "OPENAI_API_KEY"
    assert settings.voice_provider_display_name == "OpenAI Whisper"

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            voice_provider="google",
        )

    assert "voice_provider must be one of" in str(exc_info.value)


def test_voice_max_file_size_configuration(tmp_path):
    """Voice max file size should be configurable and validated."""
    project_dir = tmp_path / "projects"
    project_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(project_dir),
        voice_max_file_size_mb=32,
    )

    assert settings.voice_max_file_size_mb == 32
    assert settings.voice_max_file_size_bytes == 32 * 1024 * 1024

    with pytest.raises(ValidationError):
        Settings(
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(project_dir),
            voice_max_file_size_mb=0,
        )


def test_computed_properties(tmp_path):
    """Test computed properties."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    # Test production mode detection
    dev_settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        debug=True,
    )
    assert dev_settings.is_production is False

    prod_settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        debug=False,
        development_mode=False,
    )
    assert prod_settings.is_production is True

    # Test database path extraction
    sqlite_settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        database_url="sqlite:///data/bot.db",
    )
    assert sqlite_settings.database_path == Path("data/bot.db").resolve()

    # In-memory SQLite has no on-disk path.
    memory_settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        database_url="sqlite:///:memory:",
    )
    assert memory_settings.database_path is None


def test_webhook_mode_requires_secret(tmp_path):
    """Webhook mode is fail-closed: no secret means the bot refuses to start."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            webhook_url="https://example.com/webhook",
        )

    assert "telegram_webhook_secret required" in str(exc_info.value)


def test_webhook_mode_accepts_valid_secret(tmp_path):
    """A well-formed secret is accepted and readable in webhook mode."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    settings = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        webhook_url="https://example.com/webhook",
        telegram_webhook_secret="valid-secret_123",
    )

    assert settings.telegram_webhook_secret is not None
    assert settings.telegram_webhook_secret.get_secret_value() == "valid-secret_123"


@pytest.mark.parametrize(
    "bad_secret",
    [
        "has spaces",  # space is outside Telegram's charset
        "has$dollar",  # punctuation outside [A-Za-z0-9_-]
        "x" * 257,  # exceeds Telegram's 256-char limit
    ],
)
def test_webhook_secret_rejects_malformed_values(tmp_path, bad_secret):
    """Secrets Telegram's setWebhook would reject fail at config time."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            telegram_bot_token="test_token",
            telegram_bot_username="test_bot",
            approved_directory=str(test_dir),
            webhook_url="https://example.com/webhook",
            telegram_webhook_secret=bad_secret,
        )

    assert "telegram_webhook_secret must be" in str(exc_info.value)


def test_polling_mode_does_not_require_webhook_secret(tmp_path):
    """Without WEBHOOK_URL the bot polls and needs no secret."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    settings = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
    )

    assert settings.webhook_url is None
    assert settings.telegram_webhook_secret is None


def test_webhook_listen_defaults_to_loopback(tmp_path):
    """The listener binds to loopback unless explicitly widened."""
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    settings = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
    )
    assert settings.webhook_listen == "127.0.0.1"

    exposed = Settings(
        _env_file=None,
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        webhook_listen="0.0.0.0",
    )
    assert exposed.webhook_listen == "0.0.0.0"


def test_feature_flags(tmp_path):
    """Test feature flag system."""
    # Create test MCP config file with valid structure before creating settings.
    # It must live under tmp_path: a fixed path like /tmp/test_mcp.json is shared
    # by every process on the machine, so two concurrent runs of this suite raced
    # (one unlinked the file while the other was validating it) and Settings blew
    # up with "MCP config file does not exist".
    mcp_config = (
        '{"mcpServers": {"test-server": {"command": "echo", "args": ["hello"]}}}'
    )
    mcp_config_file = tmp_path / "test_mcp.json"
    mcp_config_file.write_text(mcp_config)

    settings = create_test_config(
        enable_mcp=True,
        mcp_config_path=str(mcp_config_file),
        enable_git_integration=True,
        enable_file_uploads=False,
    )

    features = FeatureFlags(settings)

    assert features.mcp_enabled is True
    assert features.git_enabled is True
    assert features.file_uploads_enabled is False

    enabled_features = features.get_enabled_features()
    assert "mcp" in enabled_features
    assert "git" in enabled_features
    assert "file_uploads" not in enabled_features

    # Test generic feature check
    assert features.is_feature_enabled("git") is True
    assert features.is_feature_enabled("nonexistent") is False


def test_environment_loading(monkeypatch, tmp_path):
    """Environment overrides apply when env vars do not explicitly set them.

    With the env-override-precedence fix (see ``loader._apply_environment_overrides``),
    fields appearing in ``model_fields_set`` (i.e. set via env / .env / kwargs)
    are preserved against environment-class overrides. The test needs to run
    in a directory that contains no ``.env`` file — Pydantic Settings auto-
    loads ``.env`` from cwd via its ``model_config``, separately from
    ``load_config``'s own ``load_dotenv``.
    """
    approved_dir = tmp_path / "projects"
    approved_dir.mkdir()
    monkeypatch.chdir(tmp_path)  # No .env file here
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "test_bot")
    monkeypatch.setenv("APPROVED_DIRECTORY", str(approved_dir))
    # Fields under test must NOT be env-set so environment-class overrides
    # apply. Clear anything that might leak from the OS env.
    for var in ("DEBUG", "DEVELOPMENT_MODE", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)

    config = load_config(env="development")
    assert config.debug is True
    assert config.development_mode is True
    assert config.log_level == "DEBUG"

    config = load_config(env="production")
    assert config.debug is False
    assert config.development_mode is False
    assert config.log_level == "INFO"


def test_load_config_does_not_log_api_keys(tmp_path):
    """Startup/error logs should not include raw provider API keys."""
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-api03-sensitive-anthropic-token-value",
        "MISTRAL_API_KEY": "mistral-sensitive-token-value-123",
        "OPENAI_API_KEY": "sk-sensitive-openai-token-value-456",
    }

    os.environ["TELEGRAM_BOT_TOKEN"] = "test_token"
    os.environ["TELEGRAM_BOT_USERNAME"] = "test_bot"
    os.environ["APPROVED_DIRECTORY"] = str(tmp_path)
    for key, value in secrets.items():
        os.environ[key] = value

    try:
        with patch.object(config_loader, "logger") as mock_logger:
            load_config(env="development", config_file=tmp_path / "missing.env")
            logged_text = " ".join(str(call) for call in mock_logger.mock_calls)

        for value in secrets.values():
            assert value not in logged_text
    finally:
        for key in [
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_USERNAME",
            "APPROVED_DIRECTORY",
            "ANTHROPIC_API_KEY",
            "MISTRAL_API_KEY",
            "OPENAI_API_KEY",
        ]:
            os.environ.pop(key, None)


def test_create_test_config():
    """Test test configuration creation."""
    config = create_test_config()

    assert config.telegram_token_str == "test_token_123"
    assert config.telegram_bot_username == "test_bot"
    assert str(config.approved_directory).endswith("test_projects")
    assert config.debug is True
    assert config.database_url == "sqlite:///:memory:"

    # Test with overrides
    config = create_test_config(
        log_level="ERROR",
        claude_max_turns=5,
    )

    assert config.log_level == "ERROR"
    assert config.claude_max_turns == 5


def test_configuration_error_handling():
    """Test configuration error handling."""
    # Test with invalid directory permissions (simulate by using a file)
    with tempfile.NamedTemporaryFile() as tmp_file:
        os.environ["TELEGRAM_BOT_TOKEN"] = "test_token"
        os.environ["TELEGRAM_BOT_USERNAME"] = "test_bot"
        os.environ["APPROVED_DIRECTORY"] = tmp_file.name  # File instead of directory

        try:
            with pytest.raises(ConfigurationError):
                load_config()
        finally:
            for key in [
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_BOT_USERNAME",
                "APPROVED_DIRECTORY",
            ]:
                os.environ.pop(key, None)
