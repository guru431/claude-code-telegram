"""Tests for event handlers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.types import AgentResponseEvent, ScheduledEvent, WebhookEvent
from src.utils.constants import AUTOMATION_USER_ID


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_claude() -> AsyncMock:
    mock = AsyncMock()
    mock.run_command = AsyncMock()
    return mock


@pytest.fixture
def agent_handler(event_bus: EventBus, mock_claude: AsyncMock) -> AgentHandler:
    handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=mock_claude,
        default_working_directory=Path("/tmp/test"),
        default_user_id=42,
    )
    handler.register()
    return handler


class TestAgentHandler:
    """Tests for AgentHandler."""

    async def test_webhook_event_triggers_claude(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Webhook events are processed through Claude."""
        mock_response = MagicMock()
        mock_response.content = "Analysis complete"
        mock_claude.run_command.return_value = mock_response

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = WebhookEvent(
            provider="github",
            event_type_name="push",
            payload={"ref": "refs/heads/main"},
            delivery_id="del-1",
        )

        await agent_handler.handle_webhook(event)
        # Agent runs are spawned as background tasks; drain them before asserting.
        await agent_handler.aclose()

        mock_claude.run_command.assert_called_once()
        call_kwargs = mock_claude.run_command.call_args
        assert "github" in call_kwargs.kwargs["prompt"].lower()

        # Should publish an AgentResponseEvent
        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        assert response_events[0].text == "Analysis complete"

    async def test_scheduled_event_triggers_claude(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Scheduled events invoke Claude with the job's prompt."""
        mock_response = MagicMock()
        mock_response.content = "Standup summary"
        mock_claude.run_command.return_value = mock_response

        published: list = []
        original_publish = event_bus.publish

        async def capture_publish(event):  # type: ignore[no-untyped-def]
            published.append(event)
            await original_publish(event)

        event_bus.publish = capture_publish  # type: ignore[assignment]

        event = ScheduledEvent(
            job_name="standup",
            prompt="Generate daily standup",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)
        await agent_handler.aclose()

        mock_claude.run_command.assert_called_once()
        assert "standup" in mock_claude.run_command.call_args.kwargs["prompt"].lower()

        response_events = [e for e in published if isinstance(e, AgentResponseEvent)]
        assert len(response_events) == 1
        assert response_events[0].chat_id == 100

    async def test_scheduled_event_with_skill(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Scheduled events with skill_name prepend the skill invocation."""
        mock_response = MagicMock()
        mock_response.content = "Done"
        mock_claude.run_command.return_value = mock_response

        event = ScheduledEvent(
            job_name="standup",
            prompt="morning report",
            skill_name="daily-standup",
            target_chat_ids=[100],
        )

        await agent_handler.handle_scheduled(event)
        await agent_handler.aclose()

        prompt = mock_claude.run_command.call_args.kwargs["prompt"]
        assert prompt.startswith("/daily-standup")
        assert "morning report" in prompt

    async def test_claude_error_does_not_propagate(
        self, event_bus: EventBus, mock_claude: AsyncMock, agent_handler: AgentHandler
    ) -> None:
        """Agent errors are logged but don't crash the handler."""
        mock_claude.run_command.side_effect = RuntimeError("SDK error")

        event = WebhookEvent(
            provider="github",
            event_type_name="push",
            payload={},
        )

        # Should not raise
        await agent_handler.handle_webhook(event)
        await agent_handler.aclose()

    def test_build_webhook_prompt(self, agent_handler: AgentHandler) -> None:
        """Webhook prompt includes provider and event info."""
        event = WebhookEvent(
            provider="github",
            event_type_name="pull_request",
            payload={"action": "opened", "number": 42},
        )

        prompt = agent_handler._build_webhook_prompt(event)
        assert "github" in prompt.lower()
        assert "pull_request" in prompt
        assert "action: opened" in prompt

    def test_payload_summary_truncation(self, agent_handler: AgentHandler) -> None:
        """Large payloads are truncated in the summary."""
        big_payload = {"key": "x" * 3000}
        summary = agent_handler._summarize_payload(big_payload)
        assert len(summary) <= 2100  # 2000 + truncation message


class TestBusRunAccounting:
    """Bus-driven runs must meet the same invariants a user run does.

    ``src/bot/utils/claude_run.py``: every Claude run holds a budget reservation
    and persists its interaction. Webhook and cron runs called ``run_command``
    directly and did neither, so automation spent money the daily limiter never
    saw and left no history, cost or audit trail.
    """

    @staticmethod
    def _handler(event_bus: EventBus, mock_claude: AsyncMock, rate_limiter, storage):
        handler = AgentHandler(
            event_bus=event_bus,
            claude_integration=mock_claude,
            default_working_directory=Path("/tmp/test"),
            rate_limiter=rate_limiter,
            storage=storage,
        )
        handler.register()
        return handler

    @staticmethod
    def _limiter(reserve_error=None):
        limiter = AsyncMock()
        limiter.reserve_cost = AsyncMock(return_value=("res-1", reserve_error))
        limiter.settle_reservation = AsyncMock()
        return limiter

    async def test_scheduled_run_reserves_settles_and_persists(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        response = MagicMock()
        response.content = "done"
        response.cost = 0.42
        response.session_id = "s-1"
        mock_claude.run_command.return_value = response

        limiter = self._limiter()
        storage = AsyncMock()
        handler = self._handler(event_bus, mock_claude, limiter, storage)

        await handler._run_scheduled(
            ScheduledEvent(job_id="j1", job_name="nightly", prompt="report")
        )

        limiter.reserve_cost.assert_awaited_once()
        assert limiter.reserve_cost.await_args.args[0] == AUTOMATION_USER_ID
        limiter.settle_reservation.assert_awaited_once_with("res-1", 0.42)
        storage.save_claude_interaction.assert_awaited_once()

    async def test_bus_run_is_ephemeral(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        """An automation run must not create a persistent session row.

        Every bus run uses force_new=True, so without ``ephemeral`` each one adds
        a session and ``max_sessions_per_user`` evicts the owner's oldest real
        session — a five-minute cron empties their /sessions list.
        """
        response = MagicMock()
        response.content = "done"
        response.cost = 0.0
        mock_claude.run_command.return_value = response

        handler = self._handler(event_bus, mock_claude, self._limiter(), AsyncMock())
        await handler._run_scheduled(
            ScheduledEvent(job_id="j1", job_name="nightly", prompt="report")
        )

        kwargs = mock_claude.run_command.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert kwargs["user_id"] == AUTOMATION_USER_ID

    async def test_budget_refusal_skips_the_run(
        self, event_bus: EventBus, mock_claude: AsyncMock
    ) -> None:
        """A refused reservation must not run Claude at all."""
        limiter = self._limiter(reserve_error="Cost limit exceeded.")
        handler = self._handler(event_bus, mock_claude, limiter, AsyncMock())

        await handler._run_scheduled(
            ScheduledEvent(job_id="j1", job_name="nightly", prompt="report")
        )

        mock_claude.run_command.assert_not_awaited()
