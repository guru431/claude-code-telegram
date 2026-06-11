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
    return ImageHandler(config=Mock())


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
