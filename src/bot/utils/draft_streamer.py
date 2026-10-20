"""Stream partial responses to Telegram via sendMessageDraft."""

import asyncio
import contextlib
import secrets
import time
from typing import List, Optional

import structlog
import telegram

from src.bot.utils.html_format import tg_len, utf16_cut
from src.utils.constants import TELEGRAM_MAX_MESSAGE_LENGTH

logger = structlog.get_logger()

# Max tool lines shown in the draft header
_MAX_TOOL_LINES = 10


def generate_draft_id() -> int:
    """Generate a non-zero positive draft ID.

    The same draft_id causes Telegram to animate text transitions instead of
    replacing the draft wholesale, giving a smooth streaming effect.
    """
    return secrets.randbits(30) | 1


class DraftStreamer:
    """Accumulates streamed text and sends periodic drafts to Telegram.

    The draft is composed of two sections:

    1. **Tool header** — compact lines showing tool calls and reasoning
       snippets as they arrive, e.g. ``"📖 Read  |  🔍 Grep  |  🐚 Bash"``.
    2. **Response body** — the actual assistant response text, streamed
       token-by-token.

    Both sections are combined into a single draft message and sent via
    ``sendMessageDraft``.

    Key design decisions:
    - Plain text drafts (no parse_mode) to avoid partial HTML/markdown errors.
    - Tail-truncation for messages >4096 chars: shows ``"\\u2026" + last 4093 chars``.
    - Self-disabling: any API error silently disables the streamer so the
      request continues with normal (non-streaming) delivery.
    - Non-blocking appends: the send runs as a background task, at most one
      at a time. ``append_*`` is awaited by the loop draining Claude's output,
      and a send waiting there on the network or on PTB's rate limiter held up
      every following token.
    """

    def __init__(
        self,
        bot: telegram.Bot,
        chat_id: int,
        draft_id: int,
        message_thread_id: Optional[int] = None,
        throttle_interval: float = 0.3,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = draft_id
        self.message_thread_id = message_thread_id
        self.throttle_interval = throttle_interval

        self._tool_lines: List[str] = []
        self._tool_overflow = 0
        self._accumulated_text = ""
        self._last_send_time = 0.0
        self._enabled = True
        self._send_task: Optional["asyncio.Task[None]"] = None

    @property
    def enabled(self) -> bool:
        """False once a draft send failed and the streamer switched itself off.

        Callers use this to fall back to another progress channel: in a chat
        where ``sendMessageDraft`` is not accepted (e.g. a forum topic on an
        older Bot API), the first attempt disables the streamer and progress
        must come from somewhere else.
        """
        return self._enabled

    async def append_tool(self, line: str) -> None:
        """Append a tool activity line and send a draft if throttled."""
        if not self._enabled or not line:
            return
        self._tool_lines.append(line)
        if len(self._tool_lines) > _MAX_TOOL_LINES:
            trimmed = len(self._tool_lines) - _MAX_TOOL_LINES
            self._tool_lines = self._tool_lines[-_MAX_TOOL_LINES:]
            self._tool_overflow += trimmed
        self._schedule_send()

    async def append_text(self, text: str) -> None:
        """Append streamed text and send a draft if throttle interval elapsed."""
        if not self._enabled or not text:
            return
        self._accumulated_text += text
        self._schedule_send()

    def _schedule_send(self) -> None:
        """Start a background send unless one is in flight or throttled.

        Content added meanwhile is not lost: the next send composes the draft
        from everything accumulated so far.
        """
        if self._send_task is not None and not self._send_task.done():
            return
        if (time.time() - self._last_send_time) >= self.throttle_interval:
            self._send_task = asyncio.create_task(self._send_draft())

    async def flush(self) -> None:
        """Force-send the current accumulated text as a draft."""
        if self._send_task is not None:
            # A stale send landing after this one would roll the draft back.
            self._send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._send_task
            self._send_task = None
        if not self._enabled:
            return
        if not self._accumulated_text and not self._tool_lines:
            return
        await self._send_draft()

    def _compose_draft(self) -> str:
        """Combine tool header and response body into a single draft."""
        parts: List[str] = []

        if self._tool_lines:
            visible = self._tool_lines[-_MAX_TOOL_LINES:]
            # append_tool trims the list and counts the trimmed lines in
            # _tool_overflow; the clamped second term covers a list filled
            # without it. Unclamped, it went negative below the limit.
            overflow = self._tool_overflow + max(
                0, len(self._tool_lines) - _MAX_TOOL_LINES
            )
            if overflow >= 3:
                parts.append(f"... +{overflow} more")
            parts.extend(visible)

        if self._accumulated_text:
            if parts:
                parts.append("")  # blank separator line
            parts.append(self._accumulated_text)

        return "\n".join(parts)

    async def _send_draft(self) -> None:
        """Send the composed draft (tools + text) as a message draft."""
        draft_text = self._compose_draft()
        if not draft_text.strip():
            return

        # Tail-truncate if over Telegram limit (measured in UTF-16 units).
        # Budget is computed on the reversed text so the cut lands on a code
        # point boundary in one pass: slicing by code point and then shaving a
        # character at a time cost ~one iteration per astral char in the draft.
        if tg_len(draft_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            keep = utf16_cut(draft_text[::-1], TELEGRAM_MAX_MESSAGE_LENGTH - 1)
            draft_text = "\u2026" + draft_text[len(draft_text) - keep :]

        try:
            kwargs = {
                "chat_id": self.chat_id,
                "text": draft_text,
                "draft_id": self.draft_id,
            }
            if self.message_thread_id is not None:
                kwargs["message_thread_id"] = self.message_thread_id
            await self.bot.send_message_draft(**kwargs)
            self._last_send_time = time.time()
        except Exception:
            logger.debug(
                "Draft send failed, disabling streamer",
                chat_id=self.chat_id,
            )
            self._enabled = False
