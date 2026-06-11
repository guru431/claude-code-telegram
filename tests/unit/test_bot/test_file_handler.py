"""Tests for FileHandler archive extraction and content truncation."""

import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.bot.features.file_handler import MAX_INLINE_CONTENT, FileHandler


@pytest.fixture
def handler() -> FileHandler:
    """FileHandler with mocked config/security.

    The archive/code/text processing paths under test only rely on
    ``temp_dir`` and ``code_extensions``, set up in ``__init__``.
    """
    return FileHandler(config=Mock(), security=Mock())


async def test_zip_with_directory_entries_extracts(
    handler: FileHandler, tmp_path: Path
):
    """A zip containing explicit directory entries (e.g. 'pkg/') must not
    crash extraction. Standard zip tooling emits dir members, and writing
    one as a file would break the nested member's parent mkdir."""
    archive = tmp_path / "proj.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        # Explicit directory entry, like Windows "Send to compressed folder".
        zf.writestr("pkg/", "")
        zf.writestr("pkg/main.py", "print('hello')\n")
        zf.writestr("pkg/sub/", "")
        zf.writestr("pkg/sub/util.py", "x = 1\n")

    result = await handler._process_archive(archive, "Review:")

    assert result.type == "archive"
    assert "pkg" in result.prompt
    # Both real code files should be discoverable (dir entries skipped).
    assert result.metadata["code_files"] == 2


async def test_process_code_file_truncates(handler: FileHandler, tmp_path: Path):
    """Oversized code content is capped before inlining into the prompt."""
    big = tmp_path / "big.py"
    big.write_text("x" * (MAX_INLINE_CONTENT + 5000), encoding="utf-8")

    result = await handler._process_code_file(big, "Review:")

    assert "...[truncated]" in result.prompt
    # Body holds at most the cap plus the marker, not the full file.
    assert result.prompt.count("x") <= MAX_INLINE_CONTENT


async def test_process_text_file_truncates(handler: FileHandler, tmp_path: Path):
    """Oversized text content is capped before inlining into the prompt."""
    big = tmp_path / "big.txt"
    big.write_text("y" * (MAX_INLINE_CONTENT + 5000), encoding="utf-8")

    result = await handler._process_text_file(big, "Review:")

    assert "...[truncated]" in result.prompt
    assert result.prompt.count("y") <= MAX_INLINE_CONTENT


async def test_small_file_not_truncated(handler: FileHandler, tmp_path: Path):
    """Files under the cap are inlined verbatim with no marker."""
    small = tmp_path / "small.py"
    small.write_text("print('ok')\n", encoding="utf-8")

    result = await handler._process_code_file(small, "Review:")

    assert "...[truncated]" not in result.prompt
    assert "print('ok')" in result.prompt
