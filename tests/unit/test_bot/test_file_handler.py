"""Tests for FileHandler archive extraction and content truncation."""

import os
import stat
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.bot.features import file_handler as file_handler_module
from src.bot.features.file_handler import (
    MAX_INLINE_CONTENT,
    FileHandler,
    FileTooLargeError,
)


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


@pytest.mark.parametrize("suffix, mode", [(".tgz", "w:gz"), (".txz", "w:xz")])
async def test_short_tar_suffixes_are_archives(
    handler: FileHandler, tmp_path: Path, suffix: str, mode: str
):
    """.tgz/.txz were classified as binary and rejected, though the extractor
    handles them."""
    source = tmp_path / "main.py"
    source.write_text("print('hello')\n", encoding="utf-8")
    archive = tmp_path / f"proj{suffix}"
    with tarfile.open(archive, mode) as tf:
        tf.add(source, arcname="pkg/main.py")

    assert handler._detect_file_type(archive) == "archive"
    result = await handler._process_archive(archive, "Review:")
    assert result.metadata["code_files"] == 1


def test_file_tree_is_cut_off_at_depth_limit(
    handler: FileHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An arbitrarily deep archive must not recurse past the depth cap
    (a ~1000-level path would otherwise raise RecursionError)."""
    monkeypatch.setattr(file_handler_module, "MAX_FILE_TREE_DEPTH", 3)
    deep = tmp_path.joinpath(*(f"d{i}" for i in range(6)))
    deep.mkdir(parents=True)
    (deep / "leaf.py").write_text("x = 1\n", encoding="utf-8")

    tree = handler._build_file_tree(tmp_path)

    assert "d2/" in tree
    assert "d3" not in tree and "leaf.py" not in tree
    assert "deeper levels omitted" in tree


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


@pytest.mark.parametrize(
    "length, truncated",
    [(MAX_INLINE_CONTENT, False), (MAX_INLINE_CONTENT + 1, True)],
)
def test_read_inline_marks_only_a_real_cut(
    tmp_path: Path, length: int, truncated: bool
) -> None:
    """The read stops at the cap; the one extra char tells a cut from a fit."""
    path = tmp_path / "edge.txt"
    path.write_text("я" * length, encoding="utf-8")

    content = FileHandler._read_inline(path)

    assert content.count("я") == MAX_INLINE_CONTENT
    assert content.endswith("...[truncated]") is truncated


class TestUploadSizeLimit:
    """The enhanced handler must enforce MAX_FILE_UPLOAD_SIZE_MB itself.

    Regression: it downloaded straight to disk and processed the file without
    ever comparing a size to the configured limit, so a missing or understated
    Document.file_size bypassed the policy the basic fallback enforces.
    """

    @pytest.fixture
    def sized_handler(self, tmp_path: Path) -> FileHandler:
        config = Mock()
        config.max_file_upload_size_mb = 1
        config.max_file_upload_size_bytes = 1024 * 1024
        handler = FileHandler(config=config, security=Mock())
        handler.temp_dir = tmp_path / "downloads"
        handler.temp_dir.mkdir()
        return handler

    def _document(self, payload: bytes, declared_size) -> Mock:
        """Telegram Document whose download writes ``payload`` to disk."""

        async def download_to_drive(path: str) -> None:
            Path(path).write_bytes(payload)

        file_obj = Mock()
        file_obj.file_size = declared_size
        file_obj.download_to_drive = AsyncMock(side_effect=download_to_drive)

        document = Mock()
        document.file_name = "payload.py"
        document.get_file = AsyncMock(return_value=file_obj)
        return document

    async def test_rejects_declared_oversize_before_downloading(self, sized_handler):
        document = self._document(b"x", declared_size=5 * 1024 * 1024)

        with pytest.raises(FileTooLargeError):
            await sized_handler._download_file(document)

        document.get_file.return_value.download_to_drive.assert_not_awaited()

    async def test_rejects_understated_size_after_download(self, sized_handler):
        """A lying/absent file_size must not get an over-limit payload through."""
        document = self._document(b"x" * (1024 * 1024 + 1), declared_size=None)

        with pytest.raises(FileTooLargeError):
            await sized_handler._download_file(document)

        # The oversized temp file is not left behind.
        assert list(sized_handler.temp_dir.rglob("payload.py")) == []

    async def test_accepts_file_within_limit(self, sized_handler):
        document = self._document(b"print(1)\n", declared_size=None)

        path = await sized_handler._download_file(document)

        assert path.read_bytes() == b"print(1)\n"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    async def test_download_dir_is_private(self, sized_handler):
        """The shared parent sits in the system temp dir; the upload must not
        be readable by other local users."""
        document = self._document(b"secret\n", declared_size=None)

        path = await sized_handler._download_file(document)

        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
