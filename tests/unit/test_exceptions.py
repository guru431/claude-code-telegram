"""Test custom exceptions."""

import pytest

from src.exceptions import (
    AuthenticationError,
    ClaudeCodeTelegramError,
    ConfigurationError,
    SecurityError,
)


def test_base_exception():
    """Test base exception."""
    with pytest.raises(ClaudeCodeTelegramError):
        raise ClaudeCodeTelegramError("Test error")


def test_configuration_error():
    """Test configuration error inheritance."""
    with pytest.raises(ClaudeCodeTelegramError):
        raise ConfigurationError("Config error")

    with pytest.raises(ConfigurationError):
        raise ConfigurationError("Config error")


def test_security_error():
    """Test security error inheritance."""
    with pytest.raises(ClaudeCodeTelegramError):
        raise SecurityError("Security error")

    with pytest.raises(SecurityError):
        raise AuthenticationError("Auth error")


def test_claude_exceptions_single_tree():
    """Both modules expose the same Claude classes, so either except catches."""
    import src.claude.exceptions as claude_exc
    import src.exceptions as root_exc

    for name in (
        "ClaudeError",
        "ClaudeTimeoutError",
        "ClaudeProcessError",
        "ClaudeParsingError",
    ):
        assert getattr(claude_exc, name) is getattr(root_exc, name)

    for cls in (claude_exc.ClaudeSessionError, claude_exc.ClaudeMCPError):
        with pytest.raises(root_exc.ClaudeError):
            raise cls("x")
    with pytest.raises(ClaudeCodeTelegramError):
        raise claude_exc.ClaudeTimeoutError("x")
