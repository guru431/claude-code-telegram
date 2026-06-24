"""Notification service for delivering proactive agent responses to Telegram.

Subscribes to AgentResponseEvent on the event bus and delivers messages
through the Telegram bot API with rate limiting (1 msg/sec per chat).
"""

import asyncio
import re
from typing import List, Optional, Tuple

import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from ..bot.utils.html_format import tg_len
from ..events.bus import Event, EventBus
from ..events.types import AgentResponseEvent
from ..security.secret_patterns import redact_secrets

logger = structlog.get_logger()

# Telegram rate limit: ~30 msgs/sec globally, ~1 msg/sec per chat
SEND_INTERVAL_SECONDS = 1.1


# Sentinel enqueued by stop() to wake an idle sender promptly without
# cancelling it (cancellation would drop an in-flight, already-dequeued send).
# A real event instance so the typed queue accepts it; compared by identity.
_SHUTDOWN: AgentResponseEvent = AgentResponseEvent(source="__shutdown_sentinel__")

# Matches an HTML start/end tag (Telegram's subset: b, i, code, pre, a, …).
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(\s[^>]*)?>")

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

# Void elements never have a matching close tag, so they must not be pushed onto
# the open-tag stack — otherwise they accumulate forever (carry_open grows, the
# split budget goes <= 0) and _split_message loops endlessly.
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "wbr"})


def _open_tags_at(html: str) -> List[Tuple[str, str]]:
    """Return formatting tags left open at the end of *html*.

    Each item is ``(full_opening_tag, tag_name)`` in nesting order, so callers
    can close them in reverse and reopen them verbatim (preserving attributes
    such as an anchor's ``href``).
    """
    stack: List[Tuple[str, str]] = []
    for m in _TAG_RE.finditer(html):
        name = m.group(2).lower()
        if m.group(1) == "/":  # closing tag: pop the nearest matching open
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][1] == name:
                    del stack[i]
                    break
        elif name in _VOID_TAGS or (m.group(3) or "").rstrip().endswith("/"):
            # Void (<br>) or self-closing (<x/>) tag: not an open tag, skip it.
            continue
        else:
            stack.append((m.group(0), name))
    return stack


def _utf16_cut(text: str, max_length: int) -> int:
    """Largest code-point index whose UTF-16 length is <= *max_length*.

    Telegram counts message length in UTF-16 units (emoji = 2), so a code-point
    slice can exceed the byte budget. This finds the index where the UTF-16
    length first reaches the limit.
    """
    if tg_len(text) <= max_length:
        return len(text)
    units = 0
    for i, ch in enumerate(text):
        units += len(ch.encode("utf-16-le")) // 2
        if units > max_length:
            return i
    return len(text)


def _choose_split_point(text: str, max_length: int) -> int:
    """Pick an index <= max_length (UTF-16 units) to cut *text*.

    Prefers a paragraph break, then a newline, then a space; falls back to a
    hard cut at the UTF-16 limit. If the chosen point lands inside a ``<...>``
    tag, it is moved back to just before that tag.
    """
    pos = _utf16_cut(text, max_length)
    window = text[:pos]
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep)
        if idx != -1:
            pos = idx
            break

    # Never cut in the middle of a tag: if the last '<' before pos is not yet
    # closed by a '>', back up to that '<'.
    last_open = text.rfind("<", 0, pos)
    if last_open != -1 and text.find(">", last_open, pos) == -1:
        pos = last_open

    # Likewise, never cut inside an HTML entity (&...;). Entities are short, so
    # only look back a small window for an unterminated '&'.
    amp = text.rfind("&", max(0, pos - 12), pos)
    if amp != -1 and text.find(";", amp, pos) == -1:
        pos = amp

    return pos if pos > 0 else max_length


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
        for chat_id in self._resolve_chat_ids(event):
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
                logger.warning(
                    "Telegram flood control, retrying after delay",
                    chat_id=chat_id,
                    retry_after=e.retry_after,
                )
                await asyncio.sleep(e.retry_after)

    def _split_message(self, text: str, max_length: int = 4096) -> List[str]:
        """Split long messages without breaking Telegram HTML tags.

        Messages are sent with ``ParseMode.HTML``; a naive positional split can
        cut a ``<code>``/``<a>`` tag or leave one unbalanced, which Telegram
        rejects (``TelegramError``) and the notification is lost. Each emitted
        chunk is kept well-formed: tags still open at a cut are closed at the
        chunk's end and reopened (with their attributes) at the next chunk's
        start.
        """
        if tg_len(text) <= max_length:
            return [text]

        chunks: List[str] = []
        carry_open: List[str] = []  # opening tags to reopen on the next chunk
        rest = text

        while rest:
            prefix = "".join(carry_open)
            if tg_len(prefix) + tg_len(rest) <= max_length:
                chunks.append(prefix + rest)
                break

            budget = max_length - tg_len(prefix)
            split_len = _choose_split_point(rest, budget)
            # Guard: a non-positive split point would never advance ``rest``
            # (infinite loop) or yield a negative slice. Force progress by
            # dropping the carried-over open tags and cutting at the raw limit.
            if split_len <= 0:
                carry_open = []
                prefix = ""
                budget = max_length
                split_len = _utf16_cut(rest, budget) or len(rest)
            segment = rest[:split_len]
            open_now = _open_tags_at(prefix + segment)
            closing = "".join(f"</{name}>" for _, name in reversed(open_now))

            # If the closing tags push the chunk past the limit, re-cut leaving
            # room for them. Plain text has no closing tags, so the exact cut is
            # preserved.
            if (
                closing
                and tg_len(prefix) + tg_len(segment) + tg_len(closing) > max_length
            ):
                budget = max_length - tg_len(prefix) - tg_len(closing)
                split_len = _choose_split_point(rest, budget)
                segment = rest[:split_len]
                open_now = _open_tags_at(prefix + segment)
                closing = "".join(f"</{name}>" for _, name in reversed(open_now))

            chunks.append(prefix + segment + closing)
            carry_open = [full for full, _ in open_now]
            # Drop leading whitespace of the next part (matches old behavior).
            rest = rest[split_len:].lstrip()

        return chunks
