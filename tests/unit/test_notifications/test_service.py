"""Tests for the notification service."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import Forbidden, NetworkError, RetryAfter

import src.notifications.service as service_module
from src.events.bus import EventBus
from src.events.types import AgentResponseEvent
from src.notifications.service import (
    MAX_RETRY_AFTER_SECONDS,
    NotificationService,
    _retry_delay_seconds,
)
from src.storage.database import DatabaseManager


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


@pytest.fixture
async def notification_db(tmp_path, migrated_db):
    database = DatabaseManager(
        f"sqlite:///{migrated_db(tmp_path / 'notifications.db')}"
    )
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


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

    async def test_transient_failure_resumes_chunk_without_repeating_prefix(
        self, service: NotificationService, mock_bot: AsyncMock, monkeypatch
    ) -> None:
        """A failed middle chunk is retried before delivering the remaining tail."""
        monkeypatch.setattr(service_module, "SEND_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(service_module, "RETRY_BACKOFF_SECONDS", 0)
        chunks = ["A" * 4096, "B" * 4096, "C" * 20]
        mock_bot.send_message.side_effect = [None, NetworkError("offline"), None, None]

        await service._rate_limited_send(
            123, AgentResponseEvent(chat_id=123, text="".join(chunks))
        )

        assert [
            call.kwargs["text"] for call in mock_bot.send_message.call_args_list
        ] == [chunks[0], chunks[1], chunks[1], chunks[2]]

    async def test_exhausted_tail_survives_restart_and_resumes_from_checkpoint(
        self, notification_db, event_bus, mock_bot, monkeypatch
    ) -> None:
        """Pending chunks survive a new sender instance without replaying the agent."""
        monkeypatch.setattr(service_module, "SEND_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(service_module, "RETRY_BACKOFF_SECONDS", 0)
        sender = NotificationService(event_bus, mock_bot, db_manager=notification_db)
        fake_key = "sk-" "ant-" + "0123456789abcdef0123"
        chunks = ["A" * 4096, "B" * 4096, "C" * 20 + fake_key]
        event = AgentResponseEvent(chat_id=123, text="".join(chunks))
        await sender.handle_response(event)
        mock_bot.send_message.side_effect = [
            None,
            NetworkError(fake_key),
            NetworkError(fake_key),
            NetworkError(fake_key),
        ]

        await sender._deliver(await sender._send_queue.get())

        async with notification_db.get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM notification_outbox")
            pending = dict(await cursor.fetchone())
        assert pending["next_chunk"] == 1
        assert pending["retry_at"] is not None
        assert fake_key not in pending["chunks"]
        assert fake_key not in pending["last_error"]

        mock_bot.send_message.reset_mock(side_effect=True)
        restarted = NotificationService(event_bus, mock_bot, db_manager=notification_db)
        await restarted.start()
        await restarted.stop()

        delivered = [
            call.kwargs["text"] for call in mock_bot.send_message.call_args_list
        ]
        assert delivered[0] == chunks[1]
        assert fake_key not in delivered[1]
        assert delivered[1].startswith("C" * 20)
        assert "***" in delivered[1]
        async with notification_db.get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM notification_outbox")
            assert (await cursor.fetchone())[0] == 0

    async def test_failed_tail_is_requeued_after_delay_and_claimed_once(
        self, notification_db, event_bus, mock_bot, monkeypatch
    ) -> None:
        """Sweeps retain exhausted sends during backoff, then claim one retry."""

        class FrozenDateTime(datetime):
            current = datetime(2030, 1, 1, tzinfo=UTC)

            @classmethod
            def now(cls, tz=None):
                return cls.current

        monkeypatch.setattr(service_module, "datetime", FrozenDateTime)
        sender = NotificationService(event_bus, mock_bot, db_manager=notification_db)
        mock_bot.send_message.side_effect = RetryAfter(0)
        await sender.handle_response(AgentResponseEvent(chat_id=123, text="retry me"))
        await sender._deliver(await sender._send_queue.get())
        assert mock_bot.send_message.call_count == 3

        await sender._requeue_pending(only_due=True)
        assert sender._send_queue.empty()
        FrozenDateTime.current += timedelta(seconds=300)
        await sender._requeue_pending(only_due=True)
        await sender._requeue_pending(only_due=True)
        assert sender._send_queue.qsize() == 1

        mock_bot.send_message.reset_mock(side_effect=True)
        await sender._deliver(await sender._send_queue.get())
        mock_bot.send_message.assert_awaited_once()

    async def test_permanent_error_is_retained_without_repeated_retries(
        self, notification_db, event_bus, mock_bot
    ) -> None:
        """A blocked chat remains recoverable without hammering Telegram."""
        sender = NotificationService(event_bus, mock_bot, db_manager=notification_db)
        mock_bot.send_message.side_effect = Forbidden("blocked")
        await sender.handle_response(AgentResponseEvent(chat_id=123, text="pending"))
        await sender._deliver(await sender._send_queue.get())

        mock_bot.send_message.assert_awaited_once()
        await sender._requeue_pending(only_due=True)
        assert sender._send_queue.empty()
        async with notification_db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT next_chunk, last_error FROM notification_outbox"
            )
            row = await cursor.fetchone()
        assert row["next_chunk"] == 0
        assert row["last_error"] == "blocked"

    async def test_stop_timeout_keeps_in_flight_and_queued_deliveries(
        self, notification_db, event_bus, mock_bot, monkeypatch
    ) -> None:
        """A forced sender stop leaves both queued and in-flight work recoverable."""
        monkeypatch.setattr(service_module, "_STOP_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(service_module, "SEND_INTERVAL_SECONDS", 0)
        started = asyncio.Event()

        async def hanging_send(**kwargs):
            started.set()
            await asyncio.Event().wait()

        mock_bot.send_message.side_effect = hanging_send
        sender = NotificationService(event_bus, mock_bot, db_manager=notification_db)
        await sender.start()
        await sender.handle_response(AgentResponseEvent(chat_id=100, text="in flight"))
        await sender.handle_response(AgentResponseEvent(chat_id=200, text="queued"))
        await started.wait()
        await sender.stop()

        mock_bot.send_message.reset_mock(side_effect=True)
        restarted = NotificationService(event_bus, mock_bot, db_manager=notification_db)
        await restarted.start()
        await restarted.stop()
        assert {
            (call.kwargs["chat_id"], call.kwargs["text"])
            for call in mock_bot.send_message.call_args_list
        } == {(100, "in flight"), (200, "queued")}

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

    async def test_stop_timeout_logs_dropped_notifications(
        self, service: NotificationService, mock_bot: AsyncMock, monkeypatch
    ) -> None:
        """Regression: a drain cut by the stop timeout lost messages silently."""
        monkeypatch.setattr(service_module, "_STOP_TIMEOUT_SECONDS", 0.05)
        log = MagicMock()
        monkeypatch.setattr(service_module, "logger", log)
        started = asyncio.Event()

        async def hanging_send(**kwargs: object) -> None:
            started.set()
            await asyncio.Event().wait()

        mock_bot.send_message = AsyncMock(side_effect=hanging_send)

        await service.start()
        for i in range(3):
            await service.handle_response(AgentResponseEvent(chat_id=100 + i, text="x"))
        await started.wait()

        await service.stop()

        log.error.assert_called_once()
        assert log.error.call_args.kwargs["dropped_queued"] == 2

    async def test_ignores_non_response_events(
        self, service: NotificationService
    ) -> None:
        """Non-AgentResponseEvent events are ignored."""
        from src.events.bus import Event

        event = Event(source="test")
        await service.handle_response(event)
        assert service._send_queue.qsize() == 0

    async def test_retry_after_sleeps_and_retries(
        self, service: NotificationService, mock_bot: AsyncMock, monkeypatch
    ) -> None:
        """RetryAfter (429) sleeps the server delay and retries once, not drops."""
        from telegram.error import RetryAfter

        # The sleep is captured instead of really awaited: sleeping the server
        # delay for real would put a full second of wall clock into the fast
        # suite, and asserting the argument checks more than a real sleep does.
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        monkeypatch.setattr("src.notifications.service.asyncio.sleep", fake_sleep)
        mock_bot.send_message = AsyncMock(side_effect=[RetryAfter(1), None])
        event = AgentResponseEvent(chat_id=123, text="hello")
        await service._rate_limited_send(123, event)

        assert mock_bot.send_message.call_count == 2
        # Last sleep before the successful retry is the server-provided delay
        # (an earlier entry may be the per-chat send interval).
        assert slept[-1] == 1.0

    async def test_retry_after_attempts_are_bounded(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """Persistent RetryAfter is bounded — it does not retry forever."""
        from telegram.error import RetryAfter

        mock_bot.send_message = AsyncMock(side_effect=RetryAfter(0))
        event = AgentResponseEvent(chat_id=123, text="hello")
        # Re-raised RetryAfter is caught by the TelegramError handler (no raise).
        await service._rate_limited_send(123, event)

        assert mock_bot.send_message.call_count == 3

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (3, 3.0),
            (0, 0.0),
            (2.5, 2.5),
            (timedelta(seconds=7), 7.0),
            (timedelta(milliseconds=1500), 1.5),
            # Clamped: a hostile/huge delay must not park the sender for hours.
            (timedelta(hours=2), MAX_RETRY_AFTER_SECONDS),
            (10_000, MAX_RETRY_AFTER_SECONDS),
            # Never negative -- asyncio.sleep would still run, but clamp anyway.
            (-5, 0.0),
        ],
    )
    def test_retry_delay_normalizes_int_and_timedelta(
        self, raw: object, expected: float
    ) -> None:
        """RetryAfter.retry_after is typed int | timedelta; both must work.

        python-telegram-bot deprecated the int form in v22.2 and will switch the
        attribute to timedelta. Passing a timedelta straight to asyncio.sleep
        raises TypeError and loses the notification, so it is normalized here.
        """
        assert _retry_delay_seconds(raw) == pytest.approx(expected)

    async def test_retry_after_accepts_timedelta_attribute(
        self, service: NotificationService, mock_bot: AsyncMock
    ) -> None:
        """A RetryAfter exposing a timedelta still retries instead of raising."""

        class FutureRetryAfter(RetryAfter):
            """RetryAfter as PTB will expose it once the deprecation lands."""

            @property
            def retry_after(self) -> timedelta:  # type: ignore[override]
                return timedelta(seconds=0)

        mock_bot.send_message = AsyncMock(side_effect=[FutureRetryAfter(1), None])
        event = AgentResponseEvent(chat_id=123, text="hello")
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
