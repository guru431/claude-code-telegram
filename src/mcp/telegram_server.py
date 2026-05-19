"""MCP server exposing Telegram-specific tools to Claude.

Runs as a stdio transport server. The ``send_image_to_user`` tool validates
file existence and extension, then returns a success string. Actual Telegram
delivery is handled by the bot's stream callback which intercepts the tool
call.

If ``APPROVED_DIRECTORY`` is set in the environment, the tool additionally
rejects paths that resolve outside of it. The bot's stream callback already
re-validates the path via :func:`bot.utils.image_extractor.validate_image_path`
before sending; this check is a defense-in-depth measure for cases where the
MCP server is reachable independently of the bot.
"""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

mcp = FastMCP("telegram")


def _approved_directory() -> Path | None:
    """Return the configured approved directory, if any."""
    value = os.environ.get("APPROVED_DIRECTORY")
    if not value:
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return None


@mcp.tool()
async def send_image_to_user(file_path: str, caption: str = "") -> str:
    """Send an image file to the Telegram user.

    Args:
        file_path: Absolute path to the image file.
        caption: Optional caption to display with the image.

    Returns:
        Confirmation string when the image is queued for delivery.
    """
    path = Path(file_path)

    if not path.is_absolute():
        return f"Error: path must be absolute, got '{file_path}'"

    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return (
            f"Error: unsupported image extension '{path.suffix}'. "
            f"Supported: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )

    # Resolve the path FIRST (follows symlinks, normalises ../) so subsequent
    # existence and boundary checks always agree on the same final path. This
    # closes the gap where is_file() and the boundary check might look at
    # different paths (e.g. if the original was a symlink).
    try:
        resolved = path.resolve(strict=False)
    except OSError as e:
        return f"Error: cannot resolve path '{file_path}': {e}"

    approved = _approved_directory()
    if approved is not None:
        try:
            resolved.relative_to(approved)
        except ValueError:
            return (
                "Error: file is outside the approved directory and cannot " "be sent."
            )

    if not resolved.is_file():
        return f"Error: file not found: {file_path}"

    return f"Image queued for delivery: {resolved.name}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
