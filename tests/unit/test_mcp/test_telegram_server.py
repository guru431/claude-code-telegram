"""Tests for the Telegram MCP server tool functions."""

from pathlib import Path

import pytest

from src.mcp.telegram_server import (
    _approved_directory,
    _UnresolvableApprovedDirectory,
    send_image_to_user,
)


@pytest.fixture
def image_file(tmp_path: Path) -> Path:
    """Create a sample image file."""
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return img


class TestSendImageToUser:
    async def test_valid_image(self, image_file: Path) -> None:
        result = await send_image_to_user(str(image_file))
        assert "Image queued for delivery" in result
        assert "chart.png" in result

    async def test_valid_image_with_caption(self, image_file: Path) -> None:
        result = await send_image_to_user(str(image_file), caption="My chart")
        assert "Image queued for delivery" in result

    async def test_relative_path_rejected(self, image_file: Path) -> None:
        result = await send_image_to_user("relative/path/chart.png")
        assert "Error" in result
        assert "absolute" in result

    async def test_missing_file_rejected(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.png"
        result = await send_image_to_user(str(missing))
        assert "Error" in result
        assert "not found" in result

    async def test_non_image_extension_rejected(self, tmp_path: Path) -> None:
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("hello")
        result = await send_image_to_user(str(txt_file))
        assert "Error" in result
        assert "unsupported" in result

    async def test_all_supported_extensions(self, tmp_path: Path) -> None:
        for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]:
            img = tmp_path / f"test{ext}"
            img.write_bytes(b"\x00" * 10)
            result = await send_image_to_user(str(img))
            assert "Image queued for delivery" in result, f"Failed for {ext}"

    async def test_case_insensitive_extension(self, tmp_path: Path) -> None:
        img = tmp_path / "photo.JPG"
        img.write_bytes(b"\x00" * 10)
        result = await send_image_to_user(str(img))
        assert "Image queued for delivery" in result


class TestApprovedDirectoryResolution:
    """The approved-root lookup must distinguish 'unset' from 'unresolvable'."""

    def test_unset_returns_none(self, monkeypatch) -> None:
        monkeypatch.delenv("APPROVED_DIRECTORY", raising=False)
        assert _approved_directory() is None

    def test_empty_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("APPROVED_DIRECTORY", "")
        assert _approved_directory() is None

    def test_configured_returns_resolved_path(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("APPROVED_DIRECTORY", str(tmp_path))
        assert _approved_directory() == tmp_path.resolve()

    def test_unresolvable_raises_instead_of_none(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Regression: an OSError used to collapse into None (read as 'unset')."""
        monkeypatch.setenv("APPROVED_DIRECTORY", str(tmp_path))

        def _boom(self, *args, **kwargs):
            raise OSError("resolve failed")

        monkeypatch.setattr(Path, "resolve", _boom)

        with pytest.raises(_UnresolvableApprovedDirectory):
            _approved_directory()


class TestApprovedDirectoryBoundary:
    async def test_file_inside_approved_directory_accepted(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        approved = tmp_path / "approved"
        approved.mkdir()
        img = approved / "chart.png"
        img.write_bytes(b"\x00" * 10)
        monkeypatch.setenv("APPROVED_DIRECTORY", str(approved))

        result = await send_image_to_user(str(img))
        assert "Image queued for delivery" in result

    async def test_file_outside_approved_directory_rejected(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"\x00" * 10)
        monkeypatch.setenv("APPROVED_DIRECTORY", str(approved))

        result = await send_image_to_user(str(outside))
        assert "Error" in result
        assert "outside the approved directory" in result

    async def test_unresolvable_root_fails_closed(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A configured-but-unresolvable root must refuse, not send.

        Regression: the containment check was skipped whenever the root came
        back as None, so an OSError on a configured root silently accepted any
        absolute path with an image extension.
        """
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"\x00" * 10)
        monkeypatch.setenv("APPROVED_DIRECTORY", str(tmp_path / "approved"))

        real_resolve = Path.resolve

        def _selective_resolve(self, *args, **kwargs):
            if self.name == "approved":
                raise OSError("resolve failed")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _selective_resolve)

        result = await send_image_to_user(str(outside))
        assert "Error" in result
        assert "APPROVED_DIRECTORY" in result
        assert "Image queued for delivery" not in result
