"""Claude-specific exceptions.

The base classes live in :mod:`src.exceptions` (one tree under
``ClaudeCodeTelegramError``) and are re-exported here, so ``except ClaudeError``
catches the same classes whichever module it was imported from.
"""

from ..exceptions import (
    ClaudeError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)

__all__ = [
    "ClaudeError",
    "ClaudeTimeoutError",
    "ClaudeProcessError",
    "ClaudeParsingError",
    "ClaudeSessionError",
    "ClaudeMCPError",
]


class ClaudeSessionError(ClaudeError):
    """Session management error."""


class ClaudeMCPError(ClaudeError):
    """MCP server connection or configuration error."""

    def __init__(self, message: str, server_name: str = None):
        super().__init__(message)
        self.server_name = server_name
