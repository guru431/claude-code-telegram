"""Event handlers that bridge the event bus to Claude and Telegram.

AgentHandler: translates events into ClaudeIntegration.run_command() calls.
NotificationHandler: subscribes to AgentResponseEvent and delivers to Telegram.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Coroutine, Dict, List, Optional, Set

import structlog

from ..bot.utils.html_format import markdown_to_telegram_html
from ..claude.facade import ClaudeIntegration
from ..storage.database import DatabaseManager
from .bus import Event, EventBus
from .types import AgentResponseEvent, ScheduledEvent, WebhookEvent

logger = structlog.get_logger()

# Cap on concurrent agent runs spawned from the bus. Each webhook/cron run can
# take minutes; spawning them as tasks keeps the bus worker dispatching, while
# the semaphore bounds how many run at once to avoid overwhelming the SDK.
_MAX_CONCURRENT_AGENT_RUNS = 3

# Max delivery attempts before a webhook is dead-lettered (processed=2). The
# retry sweep replays processed=0 rows below this cap; at the cap the row is
# parked and surfaced via /events.
_MAX_WEBHOOK_ATTEMPTS = 3

# Webhook payloads are untrusted external input (PR/issue bodies, etc.) that run
# unattended with no human-in-the-loop. A prompt-injection payload could turn a
# full tool set into RCE inside the approved directory. Restrict webhook-driven
# runs to read-only / analysis tools — no Bash, Write, Edit, or Task.
# WebFetch/WebSearch are intentionally excluded: they are not needed to summarize
# a payload and would give an injected prompt an outbound channel to exfiltrate
# in-boundary file contents (Read/Grep) to an attacker-controlled host / SSRF.
_WEBHOOK_READONLY_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "LS",
    "TodoRead",
    "TodoWrite",
]


class AgentHandler:
    """Translates incoming events into Claude agent executions.

    Webhook and scheduled events are converted into prompts and sent
    to ClaudeIntegration.run_command(). The response is published
    back as an AgentResponseEvent for delivery.
    """

    def __init__(
        self,
        event_bus: EventBus,
        claude_integration: ClaudeIntegration,
        default_working_directory: Path,
        default_user_id: int = 0,
        db_manager: Optional[DatabaseManager] = None,
    ) -> None:
        self.event_bus = event_bus
        self.claude = claude_integration
        self.default_working_directory = default_working_directory
        self.default_user_id = default_user_id
        self.db_manager = db_manager
        # Bound concurrent agent runs; keep references so shutdown can drain them.
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_AGENT_RUNS)
        self._tasks: Set[asyncio.Task[None]] = set()

    def register(self) -> None:
        """Subscribe to events that need agent processing."""
        self.event_bus.subscribe(WebhookEvent, self.handle_webhook)
        self.event_bus.subscribe(ScheduledEvent, self.handle_scheduled)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run an agent coroutine as a tracked background task.

        Spawning decouples the (minutes-long) Claude run from the bus worker so
        subsequent events keep dispatching. References are kept so they aren't
        garbage-collected mid-flight and so ``aclose()`` can drain them.
        """
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Await all in-flight agent runs so they aren't dropped on shutdown."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def handle_webhook(self, event: Event) -> None:
        """Dispatch a webhook event to a background agent run."""
        if not isinstance(event, WebhookEvent):
            return

        logger.info(
            "Processing webhook event through agent",
            provider=event.provider,
            event_type=event.event_type_name,
            delivery_id=event.delivery_id,
        )
        # Spawn so the bus worker returns immediately and keeps dispatching.
        self._spawn(self._run_webhook(event))

    async def _run_webhook(self, event: WebhookEvent) -> None:
        """Execute the webhook prompt through Claude (runs as a background task)."""
        prompt = self._build_webhook_prompt(event)

        try:
            async with self._semaphore:
                response = await self.claude.run_command(
                    prompt=prompt,
                    working_directory=self.default_working_directory,
                    user_id=self.default_user_id,
                    force_new=True,
                    allowed_tools_override=_WEBHOOK_READONLY_TOOLS,
                )

            if response.content:
                # We don't know which chat to send to from a webhook alone.
                # The notification service needs configured target chats.
                # Publish with chat_id=0 — the NotificationService
                # will broadcast to configured notification_chat_ids.
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=0,
                        text=markdown_to_telegram_html(response.content),
                        originating_event_id=event.id,
                    )
                )
            # Empty content: intentionally publish nothing. Unlike a scheduled
            # job, a webhook has no known target chat (chat_id=0 broadcasts to
            # all configured notification chats), so an empty-result notice would
            # be noise. Suppress it; the run is still recorded as processed below.
            # The run completed (even with empty output): mark done so the retry
            # sweep does not replay a successful delivery.
            await self._mark_webhook_processed(event.delivery_id)
        except Exception as exc:
            logger.exception(
                "Agent execution failed for webhook event",
                provider=event.provider,
                event_id=event.id,
            )
            attempts, dead = await self._mark_webhook_failed(
                event.delivery_id, str(exc)
            )
            # Notify only when retries are exhausted (dead-letter) or when there
            # is no persistence to retry from — otherwise the retry sweep will
            # try again and a notice per attempt would be noisy.
            if dead or self.db_manager is None:
                detail = f" (gave up after {attempts} attempts)" if dead else ""
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=0,
                        text=markdown_to_telegram_html(
                            f"⚠️ Webhook agent run failed for {event.provider} "
                            f"event `{event.event_type_name}`{detail}."
                        ),
                        originating_event_id=event.id,
                    )
                )

    async def handle_scheduled(self, event: Event) -> None:
        """Dispatch a scheduled event to a background agent run."""
        if not isinstance(event, ScheduledEvent):
            return

        logger.info(
            "Processing scheduled event through agent",
            job_id=event.job_id,
            job_name=event.job_name,
        )
        # Spawn so the bus worker returns immediately and keeps dispatching.
        self._spawn(self._run_scheduled(event))

    async def _run_scheduled(self, event: ScheduledEvent) -> None:
        """Execute the scheduled prompt through Claude (runs as a background task)."""
        prompt = event.prompt
        if event.skill_name:
            prompt = (
                f"/{event.skill_name}\n\n{prompt}" if prompt else f"/{event.skill_name}"
            )

        working_dir = event.working_directory or self.default_working_directory
        # Defense-in-depth: a scheduled job's working_directory comes from
        # persisted job config and is otherwise passed straight to run_command.
        # Confine it to the default (approved) working directory; fall back to
        # the default on any violation so a misconfigured/tampered job cannot
        # run the agent outside the approved tree.
        if not self._is_within_default(working_dir):
            logger.warning(
                "Scheduled job working_directory outside approved tree; "
                "using default",
                job_id=event.job_id,
                requested=str(working_dir),
                default=str(self.default_working_directory),
            )
            working_dir = self.default_working_directory

        try:
            async with self._semaphore:
                response = await self.claude.run_command(
                    prompt=prompt,
                    working_directory=working_dir,
                    user_id=self.default_user_id,
                    force_new=True,
                )

            if response.content:
                text = markdown_to_telegram_html(response.content)
                targets = event.target_chat_ids or [0]
                for chat_id in targets:
                    await self.event_bus.publish(
                        AgentResponseEvent(
                            chat_id=chat_id,
                            text=text,
                            originating_event_id=event.id,
                        )
                    )
            elif event.target_chat_ids:
                # Explicit targets expect feedback even when output is empty.
                empty_text = markdown_to_telegram_html(
                    f"ℹ️ Scheduled job `{event.job_name}` produced no output."
                )
                for chat_id in event.target_chat_ids:
                    await self.event_bus.publish(
                        AgentResponseEvent(
                            chat_id=chat_id,
                            text=empty_text,
                            originating_event_id=event.id,
                        )
                    )
        except Exception:
            logger.exception(
                "Agent execution failed for scheduled event",
                job_id=event.job_id,
                event_id=event.id,
            )
            fail_text = markdown_to_telegram_html(
                f"⚠️ Scheduled job `{event.job_name}` failed."
            )
            for chat_id in event.target_chat_ids or [0]:
                await self.event_bus.publish(
                    AgentResponseEvent(
                        chat_id=chat_id,
                        text=fail_text,
                        originating_event_id=event.id,
                    )
                )

    def _is_within_default(self, candidate: Path) -> bool:
        """Return True when ``candidate`` is inside the default working dir.

        The default working directory is the approved root for bus-driven runs.
        Resolves both paths so traversal (``..``) and symlinks can't escape it.
        Returns False on any resolution error so the caller falls back safely.
        """
        try:
            resolved = candidate.resolve()
            root = self.default_working_directory.resolve()
            resolved.relative_to(root)
            return True
        except (ValueError, OSError):
            return False

    async def _mark_webhook_processed(self, delivery_id: str) -> None:
        """Mark a webhook delivery as processed after a successful agent run.

        The row is inserted with ``processed=0`` on receipt; this flips it to 1
        only on success so a failed/empty run leaves a durable ``processed=0``.
        No-op when no db_manager is wired (back-compat).
        """
        if not self.db_manager or not delivery_id:
            return
        try:
            async with self.db_manager.get_connection() as conn:
                await conn.execute(
                    "UPDATE webhook_events SET processed=1 WHERE delivery_id=?",
                    (delivery_id,),
                )
                await conn.commit()
        except Exception:
            logger.exception(
                "Failed to mark webhook processed", delivery_id=delivery_id
            )

    async def _mark_webhook_failed(
        self, delivery_id: str, error: str
    ) -> tuple[int, bool]:
        """Record a failed webhook run; dead-letter it when retries run out.

        Increments ``attempts`` and stamps ``last_error``/``last_attempt_at``.
        Once attempts reach ``_MAX_WEBHOOK_ATTEMPTS`` the row is moved to the
        dead-letter state (``processed=2``) so the retry sweep skips it. Returns
        ``(attempts, dead)``; a no-op ``(0, False)`` when no db_manager is wired.
        """
        if not self.db_manager or not delivery_id:
            return 0, False
        now = datetime.now(UTC).isoformat()
        try:
            async with self.db_manager.get_connection() as conn:
                # Increment and read the new attempts count atomically via
                # RETURNING (SQLite 3.35+) so a concurrent retry sweep can't read
                # a stale count between a separate UPDATE and SELECT.
                cursor = await conn.execute(
                    "UPDATE webhook_events SET attempts = attempts + 1, "
                    "last_error = ?, last_attempt_at = ? WHERE delivery_id = ? "
                    "RETURNING attempts",
                    (error[:1000], now, delivery_id),
                )
                row = await cursor.fetchone()
                attempts = int(row[0]) if row else 0
                dead = attempts >= _MAX_WEBHOOK_ATTEMPTS
                if dead:
                    # Re-check the cap in the WHERE clause so the dead-letter
                    # flip only ever applies once the threshold is durably met.
                    await conn.execute(
                        "UPDATE webhook_events SET processed = 2 "
                        "WHERE delivery_id = ? AND attempts >= ?",
                        (delivery_id, _MAX_WEBHOOK_ATTEMPTS),
                    )
                await conn.commit()
                return attempts, dead
        except Exception:
            logger.exception("Failed to mark webhook failed", delivery_id=delivery_id)
            return 0, False

    def _build_webhook_prompt(self, event: WebhookEvent) -> str:
        """Build a Claude prompt from a webhook event."""
        payload_summary = self._summarize_payload(event.payload)

        return (
            f"A {event.provider} webhook event occurred.\n"
            f"Event type: {event.event_type_name}\n"
            f"Payload summary:\n{payload_summary}\n\n"
            f"Analyze this event and provide a concise summary. "
            f"Highlight anything that needs my attention."
        )

    def _summarize_payload(self, payload: Dict[str, Any], max_depth: int = 2) -> str:
        """Create a readable summary of a webhook payload."""
        lines: List[str] = []
        self._flatten_dict(payload, lines, max_depth=max_depth)
        # Cap at 2000 chars to keep prompt reasonable
        summary = "\n".join(lines)
        if len(summary) > 2000:
            summary = summary[:2000] + "\n... (truncated)"
        return summary

    def _flatten_dict(
        self,
        data: Any,
        lines: list,
        prefix: str = "",
        depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        """Flatten a nested dict into key: value lines."""
        if depth >= max_depth:
            lines.append(f"{prefix}: ...")
            return

        if isinstance(data, dict):
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    self._flatten_dict(value, lines, full_key, depth + 1, max_depth)
                else:
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    lines.append(f"{full_key}: {val_str}")
        elif isinstance(data, list):
            lines.append(f"{prefix}: [{len(data)} items]")
            for i, item in enumerate(data[:3]):  # Show first 3 items
                self._flatten_dict(item, lines, f"{prefix}[{i}]", depth + 1, max_depth)
        else:
            lines.append(f"{prefix}: {data}")
