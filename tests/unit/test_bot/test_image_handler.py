"""Tests for ImageHandler size/format validation wiring."""

from unittest.mock import AsyncMock, Mock

import pytest

from src.bot.features.image_handler import ImageHandler

# Minimal valid PNG header + padding so _detect_format returns "png".
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _photo_returning(data: bytes) -> Mock:
    """Build a mock PhotoSize whose download yields ``data``."""
    file_obj = Mock()
    file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    photo = Mock()
    photo.get_file = AsyncMock(return_value=file_obj)
    return photo


@pytest.fixture
def handler() -> ImageHandler:
    # The size limit is the shared upload policy (MAX_FILE_UPLOAD_SIZE_MB),
    # not a hard-coded constant inside the handler.
    config = Mock()
    config.max_file_upload_size_mb = 10
    config.max_file_upload_size_bytes = 10 * 1024 * 1024
    return ImageHandler(config=config)


async def test_process_image_accepts_valid(handler: ImageHandler):
    result = await handler.process_image(_photo_returning(PNG_BYTES))
    assert result.metadata["format"] == "png"
    assert result.size == len(PNG_BYTES)


async def test_process_image_rejects_oversized(handler: ImageHandler):
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="too large"):
        await handler.process_image(_photo_returning(oversized))


async def test_process_image_rejects_unknown_format(handler: ImageHandler):
    with pytest.raises(ValueError, match="Unsupported"):
        await handler.process_image(_photo_returning(b"NOTANIMAGE" + b"\x00" * 200))


class TestCaptionDrivenType:
    """The analysis prompt follows the caption.

    Regression: _detect_image_type() unconditionally returned "screenshot", so
    the documented diagram and UI-mockup prompts were unreachable dead code.
    """

    @pytest.mark.parametrize(
        "caption,expected",
        [
            ("here is the architecture diagram", "diagram"),
            ("Check this UML", "diagram"),
            ("review my wireframe", "ui_mockup"),
            ("the new mockup", "ui_mockup"),
            ("screenshot of the crash", "screenshot"),
            ("what is this", "screenshot"),
            (None, "screenshot"),
        ],
    )
    def test_detect_image_type(self, handler, caption, expected):
        assert handler._detect_image_type(caption) == expected

    async def test_diagram_caption_selects_diagram_prompt(self, handler):
        result = await handler.process_image(
            _photo_returning(PNG_BYTES), caption="our service architecture diagram"
        )

        assert result.image_type == "diagram"
        assert "diagram" in result.prompt.lower()

    async def test_default_prompt_is_the_screenshot_one(self, handler):
        result = await handler.process_image(
            _photo_returning(PNG_BYTES), caption="what do you think?"
        )

        assert result.image_type == "screenshot"
        assert "screenshot" in result.prompt.lower()


class TestConfiguredSizeLimit:
    async def test_declared_oversize_is_rejected_before_download(self, handler):
        photo = _photo_returning(PNG_BYTES)
        photo.file_size = 20 * 1024 * 1024

        with pytest.raises(ValueError, match="too large"):
            await handler.process_image(photo)

        photo.get_file.assert_not_awaited()

    async def test_limit_follows_configuration(self):
        config = Mock()
        config.max_file_upload_size_mb = 1
        config.max_file_upload_size_bytes = 1024 * 1024
        small_limit = ImageHandler(config=config)
        payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024 + 1)

        with pytest.raises(ValueError, match="max 1MB"):
            await small_limit.process_image(_photo_returning(payload))
