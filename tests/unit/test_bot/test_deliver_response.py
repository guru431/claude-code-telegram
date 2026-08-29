"""Regression tests for ``MessageOrchestrator._deliver_response``.

The delivery path had no test coverage at all, and it duplicated attachments:
``_send_images`` returns whether the response text was embedded as the photo
*caption*, and ``_deliver_response`` read that ``False`` as "the images were not
sent". Every case where the caption legitimately cannot ride along — a photo
carrying its own MCP caption, a GIF, an SVG sent as a document — therefore sent
the whole batch a second time.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.bot.utils.formatting import FormattedMessage
from src.bot.utils.image_extractor import ImageAttachment
from src.config.settings import Settings


@pytest.fixture
def orchestrator(tmp_path):
    settings = Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        agentic_mode=True,
    )
    return MessageOrchestrator(settings, {})


@pytest.fixture
def update():
    upd = MagicMock()
    upd.message = AsyncMock()
    upd.message.message_id = 7
    upd.message.chat = AsyncMock()
    return upd


def _png(tmp_path: Path, name: str, reference: str | None = None) -> ImageAttachment:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return ImageAttachment(
        path=path,
        mime_type="image/png",
        original_reference=reference or str(path),
    )


class TestImagesAreSentExactlyOnce:
    async def test_photo_with_its_own_caption_is_not_resent(
        self, orchestrator, update, tmp_path
    ):
        """A photo carrying an MCP caption blocks the text caption, not the send."""
        image = _png(tmp_path, "chart.png", reference="Quarterly revenue")

        await orchestrator._deliver_response(
            update,
            [FormattedMessage("Here is the chart.", parse_mode="HTML")],
            [image],
        )

        assert update.message.reply_photo.await_count == 1
        # The response text still reaches the user, as its own message.
        assert update.message.reply_text.await_count == 1

    async def test_gif_is_not_resent(self, orchestrator, update, tmp_path):
        """Animations always send separately from the caption."""
        path = tmp_path / "demo.gif"
        path.write_bytes(b"GIF89a" + b"0" * 64)
        gif = ImageAttachment(
            path=path, mime_type="image/gif", original_reference=str(path)
        )

        await orchestrator._deliver_response(
            update, [FormattedMessage("A demo.", parse_mode="HTML")], [gif]
        )

        assert update.message.reply_animation.await_count == 1

    async def test_document_attachment_is_not_resent(
        self, orchestrator, update, tmp_path
    ):
        """SVGs go out as documents, which cannot carry the response caption."""
        path = tmp_path / "diagram.svg"
        path.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        svg = ImageAttachment(
            path=path, mime_type="image/svg+xml", original_reference=str(path)
        )

        await orchestrator._deliver_response(
            update, [FormattedMessage("A diagram.", parse_mode="HTML")], [svg]
        )

        assert update.message.reply_document.await_count == 1

    async def test_caption_embedded_sends_no_separate_text(
        self, orchestrator, update, tmp_path
    ):
        """The happy path is unchanged: one photo, text embedded as its caption."""
        image = _png(tmp_path, "plot.png")

        await orchestrator._deliver_response(
            update, [FormattedMessage("Short text.", parse_mode="HTML")], [image]
        )

        assert update.message.reply_photo.await_count == 1
        assert update.message.reply_text.await_count == 0

    async def test_long_text_sends_text_then_images_once(
        self, orchestrator, update, tmp_path
    ):
        """Text too long to be a caption: messages first, images once after."""
        image = _png(tmp_path, "plot.png")

        await orchestrator._deliver_response(
            update, [FormattedMessage("x" * 2000, parse_mode="HTML")], [image]
        )

        assert update.message.reply_text.await_count == 1
        assert update.message.reply_photo.await_count == 1
