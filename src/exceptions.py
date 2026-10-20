"""Custom exceptions for Claude Code Telegram Bot."""

from typing import Optional


class ClaudeCodeTelegramError(Exception):
    """Base exception for Claude Code Telegram Bot."""


class ConfigurationError(ClaudeCodeTelegramError):
    """Configuration-related errors."""


class MissingConfigError(ConfigurationError):
    """Required configuration is missing."""


class InvalidConfigError(ConfigurationError):
    """Configuration is invalid."""


class SecurityError(ClaudeCodeTelegramError):
    """Security-related errors."""


class AuthenticationError(SecurityError):
    """Authentication failed."""


class AuthorizationError(SecurityError):
    """Authorization failed."""


class DirectoryTraversalError(SecurityError):
    """Directory traversal attempt detected."""


class ClaudeError(ClaudeCodeTelegramError):
    """Claude Code-related errors."""


class ClaudeTimeoutError(ClaudeError):
    """Claude Code operation timed out.

    *partial_content* is the assistant text streamed before the timeout, so a
    caller can show what Claude had already written instead of dropping it.
    *session_id* is the session the CLI reported; it holds the conversation up
    to the timeout, so the next message can continue it.
    """

    def __init__(
        self,
        message: str = "",
        partial_content: str = "",
        session_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.partial_content = partial_content
        self.session_id = session_id


class ClaudeProcessError(ClaudeError):
    """Claude Code process execution failed."""


class ClaudeParsingError(ClaudeError):
    """Failed to parse Claude Code output."""


class StorageError(ClaudeCodeTelegramError):
    """Storage-related errors."""


class DatabaseConnectionError(StorageError):
    """Database connection failed."""


class DataIntegrityError(StorageError):
    """Data integrity check failed."""


class TelegramError(ClaudeCodeTelegramError):
    """Telegram API-related errors."""


class MessageTooLongError(TelegramError):
    """Message exceeds Telegram's length limit."""


class RateLimitError(TelegramError):
    """Rate limit exceeded."""


class RateLimitExceeded(RateLimitError):
    """Rate limit exceeded (alias for compatibility)."""
