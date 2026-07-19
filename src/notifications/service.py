"""Notification service for delivering proactive agent responses to Telegram.

Subscribes to AgentResponseEvent on the event bus and delivers messages
through the Telegram bot API with rate limiting (1 msg/sec per chat).
"""

import asyncio
from datetime import timedelta
from typing import List, Optional, Union

import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from ..bot.utils.html_format import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_telegram_html,
)
from ..events.bus import Event, EventBus
from ..events.types import AgentResponseEvent
from ..security.secret_patterns import redact_secrets

logger = structlog.get_logger()

# Telegram rate limit: ~30 msgs/sec globally, ~1 msg/sec per chat
SEND_INTERVAL_SECONDS = 1.1

# Upper bound for a server-provided flood-control delay. A hostile or buggy
# value (or a misparsed unit) must not park the sender worker for hours and
# stall every other queued notification behind it.
MAX_RETRY_AFTER_SECONDS = 60.0


# Sentinel enqueued by stop() to wake an idle sender promptly without
# cancelling it (cancellation would drop an in-flight, already-dequeued send).
# A real event instance so the typed queue accepts it; compared by identity.
_SHUTDOWN: AgentResponseEvent = AgentResponseEvent(source="__shutdown_sentinel__")

# Maps an event's ``parse_mode`` string to Telegram's ParseMode enum. ``None``
# is plain text. An unrecognized value is rejected explicitly rather than being
# silently downgraded to plain text (which would strip all formatting).
_PARSE_MODES: dict[Optional[str], Optional[str]] = {
    None: None,
    "": None,
    "HTML": ParseMode.HTML,
    "Markdown": ParseMode.MARKDOWN,
    "MarkdownV2": ParseMode.MARKDOWN_V2,
}


def _retry_delay_seconds(retry_after: Union[int, float, timedelta]) -> float:
    """Normalize a Telegram ``RetryAfter`` delay to bounded seconds.

    ``RetryAfter.retry_after`` is typed ``int | timedelta``: python-telegram-bot
    stores it internally as a ``timedelta`` and currently down-converts it to an
    ``int`` while emitting a ``PTBDeprecationWarning`` (deprecated in v22.2).
    Once that default flips, passing the raw attribute to ``asyncio.sleep``
    would raise ``TypeError`` and the notification would be lost, so accept both
    shapes here and clamp the result to a sane ceiling.
    """
    seconds = (
        retry_after.total_seconds()
        if isinstance(retry_after, timedelta)
        else float(retry_after)
    )
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


class NotificationService:
    """Delivers agent responses to Telegram chats with rate limiting."""

    def __init__(
        self,
        event_bus: EventBus,
        bot: Bot,
        default_chat_ids: Optional[List[int]] = None,
    ) -> None:
        self.event_bus = event_bus
        self.bot = bot
        self.default_chat_ids = default_chat_ids or []
        self._send_queue: asyncio.Queue[AgentResponseEvent] = asyncio.Queue()
        self._last_send_per_chat: dict[int, float] = {}
        self._running = False
        self._sender_task: Optional[asyncio.Task[None]] = None

    def register(self) -> None:
        """Subscribe to agent response events."""
        self.event_bus.subscribe(AgentResponseEvent, self.handle_response)

    async def start(self) -> None:
        """Start the send queue processor."""
        if self._running:
            return
        self._running = True
        self._sender_task = asyncio.create_task(self._process_send_queue())
        logger.info("Notification service started")

    async def stop(self) -> None:
        """Stop the sender, finishing the in-flight send and any backlog.

        The worker is not cancelled up front: cancelling it mid-send would drop
        a message already taken off the queue. We clear the running flag and
        wake the worker with a sentinel; it finishes the current send, drains
        the queue, and exits. Cancellation is only a last-resort guard if a send
        hangs past the timeout.
        """
        if not self._running:
            return
        self._running = False
        task = self._sender_task
        if task:
            self._send_queue.put_nowait(_SHUTDOWN)
            try:
                await asyncio.wait_for(task, timeout=30.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("Notification service stopped")

    async def handle_response(self, event: Event) -> None:
        """Queue an agent response for delivery."""
        if not isinstance(event, AgentResponseEvent):
            return
        await self._send_queue.put(event)

    async def _process_send_queue(self) -> None:
        """Process queued messages with rate limiting."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if event is _SHUTDOWN:
                break
            # Never let one bad delivery kill the worker — that would stall all
            # future notifications (they would queue forever). TelegramError is
            # already handled inside _rate_limited_send; this guards anything else.
            try:
                await self._deliver(event)
            except Exception as e:
                logger.error(
                    "Notification delivery failed, continuing",
                    error=str(e),
                    event_id=getattr(event, "id", None),
                )

        # Graceful drain: deliver messages still queued at shutdown. The
        # in-flight send above always completes (the worker is never cancelled
        # mid-send).
        drained = 0
        while not self._send_queue.empty():
            event = self._send_queue.get_nowait()
            if event is _SHUTDOWN:
                continue
            try:
                await self._deliver(event)
            except Exception as e:
                logger.error(
                    "Notification delivery failed during drain",
                    error=str(e),
                    event_id=getattr(event, "id", None),
                )
            drained += 1
        if drained:
            logger.info("Drained queued notifications on shutdown", count=drained)

    async def _deliver(self, event: AgentResponseEvent) -> None:
        """Send one event to all its resolved chats with rate limiting."""
        chat_ids = self._resolve_chat_ids(event)
        if not chat_ids:
            # No chat_id on the event and no NOTIFICATION_CHAT_IDS configured:
            # the response has nowhere to go. Log it so an operator who enabled
            # the scheduler/API but forgot default chats sees a diagnostic
            # instead of silent zero output.
            logger.warning(
                "No chat to deliver notification to; dropping response",
                event_id=event.id,
                source=event.source,
                originating_event=event.originating_event_id,
            )
            return
        for chat_id in chat_ids:
            await self._rate_limited_send(chat_id, event)

    def _resolve_chat_ids(self, event: AgentResponseEvent) -> List[int]:
        """Determine which chats to send to."""
        if event.chat_id and event.chat_id != 0:
            return [event.chat_id]
        return list(self.default_chat_ids)

    async def _rate_limited_send(self, chat_id: int, event: AgentResponseEvent) -> None:
        """Send message with per-chat rate limiting."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        last_send = self._last_send_per_chat.get(chat_id, 0.0)
        wait_time = SEND_INTERVAL_SECONDS - (now - last_send)

        if wait_time > 0:
            await asyncio.sleep(wait_time)

        # Redact high-confidence secrets before delivery (untrusted/verbatim
        # webhook + scheduled responses), then split (Telegram limit: 4096 chars).
        text = redact_secrets(event.text)
        chunks = self._split_message(text)
        chunk_index = 0

        try:
            for chunk_index, chunk in enumerate(chunks):
                await self._send_chunk(chat_id, chunk, event)
                self._last_send_per_chat[chat_id] = loop.time()

                # Rate limit between chunks too — but not after the final one.
                if chunk_index < len(chunks) - 1:
                    await asyncio.sleep(SEND_INTERVAL_SECONDS)

            logger.info(
                "Notification sent",
                chat_id=chat_id,
                text_length=len(text),
                chunks=len(chunks),
                originating_event=event.originating_event_id,
            )
        except TelegramError as e:
            # Record which chunk failed so a partial multi-part delivery is
            # diagnosable (and which agent run it belonged to).
            logger.error(
                "Failed to send notification",
                chat_id=chat_id,
                error=str(e),
                event_id=event.id,
                originating_event=event.originating_event_id,
                failed_chunk=chunk_index,
                total_chunks=len(chunks),
            )

    async def _send_chunk(
        self, chat_id: int, chunk: str, event: AgentResponseEvent
    ) -> None:
        """Send one chunk, honouring Telegram's explicit RetryAfter (429) delay.

        On RetryAfter (flood control), sleep the server-provided delay and retry
        once rather than dropping the message. Bounded to 2 attempts total.
        """
        try:
            parse_mode = _PARSE_MODES[event.parse_mode]
        except KeyError:
            raise ValueError(f"Unsupported parse_mode: {event.parse_mode!r}") from None
        for attempt in range(2):
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=parse_mode,
                )
                return
            except RetryAfter as e:
                if attempt + 1 >= 2:
                    raise
                delay = _retry_delay_seconds(e.retry_after)
                logger.warning(
                    "Telegram flood control, retrying after delay",
                    chat_id=chat_id,
                    retry_after=delay,
                )
                await asyncio.sleep(delay)

    def _split_message(
        self, text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
    ) -> List[str]:
        """Split long messages without breaking Telegram HTML tags.

        Thin wrapper over the shared :func:`split_telegram_html` splitter so
        notifications and the classic-mode formatter cut text identically.
        """
        return split_telegram_html(text, max_length)
