"""Notification service for delivering proactive agent responses to Telegram.

Subscribes to AgentResponseEvent on the event bus and delivers messages
through the Telegram bot API with rate limiting (1 msg/sec per chat).

With a database, responses accepted by handle_response are stored durably and
resume from the first unconfirmed chunk. The event bus itself is in memory;
this does not make the preceding agent-to-bus handoff durable. Delivery is
at least once: a crash after Telegram accepts a chunk but before its checkpoint
commits can repeat that chunk on recovery.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import List, Optional, Union

import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from ..bot.utils.html_format import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_telegram_html,
)
from ..events.bus import Event, EventBus
from ..events.types import AgentResponseEvent
from ..security.secret_patterns import redact_secrets
from ..storage.database import DatabaseManager

logger = structlog.get_logger()

# Telegram rate limit: ~30 msgs/sec globally, ~1 msg/sec per chat
SEND_INTERVAL_SECONDS = 1.1

# Upper bound for a server-provided flood-control delay. A hostile or buggy
# value (or a misparsed unit) must not park the sender worker for hours and
# stall every other queued notification behind it.
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
# Exhausted transient deliveries wait between bounded attempts. The durable
# outbox is retried without rerunning the agent or resending completed chunks.
DELIVERY_RETRY_SECONDS = 300.0
RETRY_SWEEP_INTERVAL_SECONDS = 60.0


# Sentinel enqueued by stop() to wake an idle sender promptly without
# cancelling it (cancellation would drop an in-flight, already-dequeued send).
# A real event instance so the typed queue accepts it; compared by identity.
_SHUTDOWN: AgentResponseEvent = AgentResponseEvent(source="__shutdown_sentinel__")

# How long stop() waits for the sender to finish the in-flight send and drain.
_STOP_TIMEOUT_SECONDS = 30.0

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
        db_manager: Optional[DatabaseManager] = None,
    ) -> None:
        self.event_bus = event_bus
        self.bot = bot
        self.default_chat_ids = default_chat_ids or []
        self.db_manager = db_manager
        self._send_queue: asyncio.Queue[AgentResponseEvent] = asyncio.Queue()
        self._last_send_per_chat: dict[int, float] = {}
        self._running = False
        self._sender_task: Optional[asyncio.Task[None]] = None
        self._retry_task: Optional[asyncio.Task[None]] = None

    def register(self) -> None:
        """Subscribe to agent response events."""
        self.event_bus.subscribe(AgentResponseEvent, self.handle_response)

    async def start(self) -> None:
        """Start the send queue processor."""
        if self._running:
            return
        # Recover exact chunks and their progress before accepting new events.
        if self.db_manager:
            # Pre-start responses are already durable. Rebuild the queue from
            # that canonical state rather than enqueueing them a second time.
            self._send_queue = asyncio.Queue()
        await self._requeue_pending(only_due=False)
        self._running = True
        self._sender_task = asyncio.create_task(self._process_send_queue())
        if self.db_manager:
            self._retry_task = asyncio.create_task(self._retry_pending())
        logger.info("Notification service started")

    async def stop(self) -> None:
        """Stop the sender, trying to finish the in-flight send and any backlog.

        The worker is not cancelled up front: cancelling it mid-send would drop
        a message already taken off the queue. We clear the running flag and
        wake the worker with a sentinel; it finishes the current send, drains
        the queue, and exits.

        The whole drain is bounded by ``_STOP_TIMEOUT_SECONDS`` (at ~1 msg/sec
        per chat a large backlog does not fit). Past it the worker is
        cancelled and the remaining queue count is logged. With a database,
        pending deliveries remain in the outbox for startup recovery.
        """
        if not self._running:
            return
        self._running = False
        if self._retry_task:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
        task = self._sender_task
        if task:
            self._send_queue.put_nowait(_SHUTDOWN)
            try:
                await asyncio.wait_for(task, timeout=_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                dropped = 0
                while not self._send_queue.empty():
                    if self._send_queue.get_nowait() is not _SHUTDOWN:
                        dropped += 1
                logger.error(
                    "Notification service stop timed out",
                    timeout_seconds=_STOP_TIMEOUT_SECONDS,
                    dropped_queued=dropped,
                    retained=bool(self.db_manager),
                )
        logger.info("Notification service stopped")

    async def handle_response(self, event: Event) -> None:
        """Queue an agent response for delivery."""
        if not isinstance(event, AgentResponseEvent):
            return
        if self.db_manager:
            # The outbox owns delivery once this handler accepts the response.
            # Persist before enqueueing so the sender never takes an undurable
            # delivery off its queue.
            text = redact_secrets(event.text)
            chunks = json.dumps(self._split_message(text))
            async with self.db_manager.get_connection() as conn:
                for chat_id in self._resolve_chat_ids(event):
                    await conn.execute(
                        "INSERT OR IGNORE INTO notification_outbox "
                        "(event_id, chat_id, chunks, parse_mode, source, "
                        "originating_event_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            event.id,
                            chat_id,
                            chunks,
                            event.parse_mode,
                            event.source,
                            event.originating_event_id,
                        ),
                    )
                await conn.commit()
        await self._send_queue.put(event)

    async def _requeue_pending(self, *, only_due: bool) -> None:
        """Claim failed tails, or all pending deliveries during startup recovery."""
        if not self.db_manager:
            return
        where = (
            " WHERE retry_at IS NOT NULL AND datetime(retry_at) <= datetime(?)"
            if only_due
            else ""
        )
        params = (datetime.now(UTC).isoformat(),) if only_due else ()
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "UPDATE notification_outbox SET retry_at = NULL"
                + where
                + " RETURNING event_id, chat_id, parse_mode, source, "
                "originating_event_id",
                params,
            )
            rows = await cursor.fetchall()
            await conn.commit()
        for row in rows:
            await self._send_queue.put(
                AgentResponseEvent(
                    id=row["event_id"],
                    chat_id=row["chat_id"],
                    parse_mode=row["parse_mode"],
                    source=row["source"],
                    originating_event_id=row["originating_event_id"],
                )
            )

    async def _retry_pending(self) -> None:
        """Retry exhausted transient deliveries at a bounded cadence."""
        while self._running:
            await asyncio.sleep(RETRY_SWEEP_INTERVAL_SECONDS)
            try:
                await self._requeue_pending(only_due=True)
            except Exception:
                logger.exception("Failed to recover pending notifications")

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
                    error=redact_secrets(str(e)),
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
                    error=redact_secrets(str(e)),
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
        if self.db_manager:
            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT chunks, next_chunk FROM notification_outbox "
                    "WHERE event_id = ? AND chat_id = ?",
                    (event.id, chat_id),
                )
                pending = await cursor.fetchone()
            if pending is None:
                # An already-queued duplicate may reach us after completion.
                return
            chunks = json.loads(pending["chunks"])
            chunk_index = pending["next_chunk"]

        try:
            for chunk_index in range(chunk_index, len(chunks)):
                await self._send_chunk(chat_id, chunks[chunk_index], event)
                self._last_send_per_chat[chat_id] = loop.time()
                if self.db_manager:
                    async with self.db_manager.get_connection() as conn:
                        if chunk_index + 1 == len(chunks):
                            await conn.execute(
                                "DELETE FROM notification_outbox "
                                "WHERE event_id = ? AND chat_id = ?",
                                (event.id, chat_id),
                            )
                        else:
                            await conn.execute(
                                "UPDATE notification_outbox SET next_chunk = ?, "
                                "last_error = NULL WHERE event_id = ? AND chat_id = ?",
                                (chunk_index + 1, event.id, chat_id),
                            )
                        await conn.commit()

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
            retry_at = None
            if not isinstance(e, (BadRequest, Forbidden)):
                retry_at = (
                    datetime.now(UTC) + timedelta(seconds=DELIVERY_RETRY_SECONDS)
                ).isoformat()
            if self.db_manager:
                async with self.db_manager.get_connection() as conn:
                    await conn.execute(
                        "UPDATE notification_outbox SET retry_at = ?, last_error = ? "
                        "WHERE event_id = ? AND chat_id = ?",
                        (retry_at, redact_secrets(str(e))[:1000], event.id, chat_id),
                    )
                    await conn.commit()
            logger.error(
                "Failed to send notification",
                chat_id=chat_id,
                error=redact_secrets(str(e)),
                event_id=event.id,
                originating_event=event.originating_event_id,
                failed_chunk=chunk_index,
                total_chunks=len(chunks),
                pending_chunks=len(chunks) - chunk_index,
                retained=bool(self.db_manager),
                retry_at=retry_at if self.db_manager else None,
            )

    async def _send_chunk(
        self, chat_id: int, chunk: str, event: AgentResponseEvent
    ) -> None:
        """Retry a transient chunk failure without replaying successful chunks."""
        try:
            parse_mode = _PARSE_MODES[event.parse_mode]
        except KeyError:
            raise ValueError(f"Unsupported parse_mode: {event.parse_mode!r}") from None
        for attempt in range(MAX_SEND_ATTEMPTS):
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=parse_mode,
                )
                return
            except (BadRequest, Forbidden):
                raise
            except TelegramError as e:
                if attempt + 1 >= MAX_SEND_ATTEMPTS:
                    raise
                delay = (
                    _retry_delay_seconds(e.retry_after)
                    if isinstance(e, RetryAfter)
                    else RETRY_BACKOFF_SECONDS * 2**attempt
                )
                logger.warning(
                    "Telegram delivery failed, retrying after delay",
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
