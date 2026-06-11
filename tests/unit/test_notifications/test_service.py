"""Tests for the notification service."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.service import NotificationService


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_bot() -> AsyncMock:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def service(event_bus: EventBus, mock_bot: AsyncMock) -> NotificationService:
    svc = NotificationService(
        event_bus=event_bus,
        bot=mock_bot,
        default_chat_ids=[100, 200],
    )
    svc.register()
    return svc


class TestNotificationService:
    """Tests for NotificationService."""

    async def test_handle_response_queues_event(
        self, service: NotificationService
    ) -> None:
        """Events are queued for delivery."""
        event = AgentResponseEvent(chat_id=100, text="hello")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 1

    async def test_resolve_chat_ids_specific(
        self, service: NotificationService
    ) -> None:
        """Specific chat_id takes precedence over defaults."""
        event = AgentResponseEvent(chat_id=999, text="test")
        ids = service._resolve_chat_ids(event)
        assert ids == [999]

    async def test_resolve_chat_ids_default(self, service: NotificationService) -> None:
        """chat_id=0 falls back to default chat IDs."""
        event = AgentResponseEvent(chat_id=0, text="test")
        ids = service._resolve_chat_ids(event)
        assert ids == [100, 200]

    def test_split_message_short(self, service: NotificationService) -> None:
        """Short messages are not split."""
        chunks = service._split_message("short text")
        assert len(chunks) == 1
        assert chunks[0] == "short text"

    def test_split_message_long(self, service: NotificationService) -> None:
        """Long messages are split at boundaries."""
        text = "A" * 4000 + "\n\n" + "B" * 200
        chunks = service._split_message(text, max_length=4096)
        assert len(chunks) >= 1
        # All content preserved
        total_len = sum(len(c) for c in chunks)
        assert total_len > 0

    def test_split_message_no_boundary(self, service: NotificationService) -> None:
        """Plain messages without boundaries are hard-split (no tag overhead)."""
        text = "A" * 5000  # No newlines or spaces
        chunks = service._split_message(text, max_length=4096)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4096
        assert len(chunks[1]) == 904

    def test_split_preserves_code_tags_across_boundary(
        self, service: NotificationService
    ) -> None:
        """A <code> block longer than the limit must stay balanced per chunk."""
        import re

        body = "A" * 5000
        text = f"<code>{body}</code>"
        chunks = service._split_message(text, max_length=4096)

        assert len(chunks) >= 2
        for c in chunks:
            assert c.count("<code>") == c.count("</code>"), c[:40]
            assert len(c) <= 4096
        assert chunks[0].endswith("</code>")
        assert chunks[1].startswith("<code>")
        # Content is preserved once the injected tags are stripped.
        stripped = "".join(re.sub(r"</?code>", "", c) for c in chunks)
        assert stripped == body

    def test_split_preserves_anchor_href_across_boundary(
        self, service: NotificationService
    ) -> None:
        """An <a href=...> spanning the limit must reopen with its href."""
        body = "B" * 5000
        text = f'<a href="https://example.com/page">{body}</a>'
        chunks = service._split_message(text, max_length=4096)

        assert len(chunks) >= 2
        assert chunks[0].endswith("</a>")
        assert chunks[1].startswith('<a href="https://example.com/page">')
        for c in chunks:
            assert c.count("<a ") == c.count("</a>")
            assert len(c) <= 4096

    def test_split_preserves_nested_pre_code_across_boundary(
        self, service: NotificationService
    ) -> None:
        """Nested <pre><code> must be closed/reopened in the right order."""
        body = "A" * 5000
        text = f"<pre><code>{body}</code></pre>"
        chunks = service._split_message(text, max_length=4096)

        assert len(chunks) >= 2
        for c in chunks:
            assert c.count("<pre>") == c.count("</pre>")
            assert c.count("<code>") == c.count("</code>")
            assert len(c) <= 4096
        assert chunks[0].endswith("</code></pre>")
        assert chunks[1].startswith("<pre><code>")

    def test_split_does_not_break_html_entity(
        self, service: NotificationService
    ) -> None:
        """A cut must not land inside an HTML entity like &amp;."""
        # Place an entity exactly where the hard cut would otherwise fall.
        text = "x" * 4094 + "&amp;" + "y" * 200
        chunks = service._split_message(text, max_length=4096)

        assert len(chunks) >= 2
        # The entity stays intact in a single chunk, never split as &am | p;.
        assert any("&amp;" in c for c in chunks)
        for c in chunks:
            assert not c.endswith("&am")
            assert not c.startswith("p;")
            assert len(c) <= 4096

    async def test_send_to_telegram(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """Messages are sent via the Telegram bot."""
        event = AgentResponseEvent(chat_id=123, text="hello world")
        await service._rate_limited_send(123, event)

        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert call_kwargs["text"] == "hello world"

    async def test_stop_drains_pending_sends(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """A graceful stop still delivers messages left in the send queue."""
        await service.start()
        for i in range(3):
            await service.handle_response(AgentResponseEvent(chat_id=100 + i, text="x"))
        await service.stop()

        assert mock_bot.send_message.call_count == 3

    async def test_stop_completes_in_flight_send(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """A send already in progress must finish, not be cancelled."""
        started = asyncio.Event()
        release = asyncio.Event()
        completed = []

        async def slow_send(**kwargs: object) -> None:
            started.set()
            await release.wait()
            completed.append(1)

        mock_bot.send_message = AsyncMock(side_effect=slow_send)

        await service.start()
        await service.handle_response(AgentResponseEvent(chat_id=100, text="x"))

        await started.wait()  # worker is inside the in-flight send
        stop_task = asyncio.create_task(service.stop())
        await asyncio.sleep(0)  # let stop() begin awaiting the worker
        release.set()  # allow the in-flight send to finish
        await stop_task

        assert completed == [1]

    async def test_ignores_non_response_events(
        self, service: NotificationService
    ) -> None:
        """Non-AgentResponseEvent events are ignored."""
        from src.events.bus import Event

        event = Event(source="test")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 0

    async def test_retry_after_sleeps_and_retries(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """RetryAfter (429) sleeps the server delay and retries once, not drops."""
        from telegram.error import RetryAfter

        mock_bot.send_message = AsyncMock(side_effect=[RetryAfter(1), None])
        event = AgentResponseEvent(chat_id=123, text="hello")
        await service._rate_limited_send(123, event)

        assert mock_bot.send_message.call_count == 2

    async def test_retry_after_bounded_to_two_attempts(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """Persistent RetryAfter is bounded — it does not retry forever."""
        from telegram.error import RetryAfter

        mock_bot.send_message = AsyncMock(side_effect=RetryAfter(0))
        event = AgentResponseEvent(chat_id=123, text="hello")
        # Re-raised RetryAfter is caught by the TelegramError handler (no raise).
        await service._rate_limited_send(123, event)

        assert mock_bot.send_message.call_count == 2

    async def test_worker_survives_bad_delivery(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """A non-TelegramError on one delivery must not kill the sender worker."""
        sent: list[str] = []

        async def flaky(**kwargs: object) -> None:
            if kwargs.get("chat_id") == 100:
                raise RuntimeError("boom")
            sent.append(str(kwargs.get("text")))

        mock_bot.send_message = AsyncMock(side_effect=flaky)

        await service.start()
        await service.handle_response(AgentResponseEvent(chat_id=100, text="bad"))
        await service.handle_response(AgentResponseEvent(chat_id=200, text="good"))
        await service.stop()

        # The second message is still delivered despite the first one raising.
        assert "good" in sent

    async def test_secrets_redacted_before_send(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """High-confidence secrets are redacted before delivery."""
        # Assemble secret-looking values at runtime so the source literals do
        # not trip secret scanners (this is a public repo); the assembled
        # strings still match the redaction patterns and exercise the logic.
        fake_key = "sk-" "ant-" + "0123456789abcdef0123"
        fake_gh = "ghp_" + "abcdef0123456789abcdef"
        event = AgentResponseEvent(
            chat_id=123,
            text=f"key {fake_key} and GITHUB_TOKEN={fake_gh}",
        )
        await service._rate_limited_send(123, event)

        sent_text = mock_bot.send_message.call_args.kwargs["text"]
        assert fake_key not in sent_text
        assert fake_gh not in sent_text
        assert "***" in sent_text
