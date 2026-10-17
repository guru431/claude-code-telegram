"""Tests for webhook-mode startup wiring in bot core.

The secret token is the only thing standing between the public listener and a
forged ``Update`` carrying an allowed user's ID, so these tests pin that it
actually reaches PTB.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.core import ClaudeCodeBot
from src.config import create_test_config
from src.exceptions import ClaudeCodeTelegramError


def _make_bot(settings) -> tuple[ClaudeCodeBot, MagicMock]:
    """Build a bot whose ``initialize()`` is stubbed out with a mock app."""
    bot = ClaudeCodeBot(settings, {"storage": MagicMock(), "security": MagicMock()})

    app = MagicMock()
    app.start = AsyncMock()
    app.updater = MagicMock()

    async def stop_loop(*args: object, **kwargs: object) -> None:
        # Break out of start()'s `while self.is_running` keepalive loop.
        bot.is_running = False

    app.updater.start_webhook = AsyncMock(side_effect=stop_loop)
    app.updater.start_polling = AsyncMock(side_effect=stop_loop)

    bot.app = app  # initialize() short-circuits when app is already set
    return bot, app


@pytest.mark.asyncio
async def test_start_webhook_passes_secret_token():
    """The configured secret is forwarded to PTB's start_webhook."""
    settings = create_test_config(
        webhook_url="https://example.com/webhook",
        telegram_webhook_secret="test-secret_value",
    )
    bot, app = _make_bot(settings)

    await bot.start()

    kwargs = app.updater.start_webhook.call_args.kwargs
    assert kwargs["secret_token"] == "test-secret_value"
    assert kwargs["webhook_url"] == "https://example.com/webhook"


@pytest.mark.asyncio
async def test_start_webhook_binds_loopback_by_default():
    """Default bind address is loopback, not 0.0.0.0."""
    settings = create_test_config(
        webhook_url="https://example.com/webhook",
        telegram_webhook_secret="test-secret_value",
    )
    bot, app = _make_bot(settings)

    await bot.start()

    assert app.updater.start_webhook.call_args.kwargs["listen"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_start_webhook_honours_explicit_listen_override():
    """An operator can still expose the listener deliberately."""
    settings = create_test_config(
        webhook_url="https://example.com/webhook",
        telegram_webhook_secret="test-secret_value",
        webhook_listen="0.0.0.0",
    )
    bot, app = _make_bot(settings)

    await bot.start()

    assert app.updater.start_webhook.call_args.kwargs["listen"] == "0.0.0.0"


@pytest.mark.asyncio
async def test_start_polling_when_no_webhook_url():
    """Without a webhook URL the bot polls and never opens a listener."""
    settings = create_test_config()
    bot, app = _make_bot(settings)

    await bot.start()

    app.updater.start_polling.assert_awaited_once()
    app.updater.start_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_releases_app_even_if_updater_stop_fails():
    """A failing updater.stop() must not skip app.stop() and app.shutdown()."""
    bot, app = _make_bot(create_test_config())
    app.updater.running = True
    app.updater.stop = AsyncMock(side_effect=RuntimeError("network down"))
    app.running = True
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()

    with pytest.raises(ClaudeCodeTelegramError):
        await bot.stop()

    app.stop.assert_awaited_once()
    app.shutdown.assert_awaited_once()
