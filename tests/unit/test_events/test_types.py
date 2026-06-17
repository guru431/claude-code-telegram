"""Tests for event types."""

from pathlib import Path

from src.events.types import (
    AgentResponseEvent,
    ScheduledEvent,
    WebhookEvent,
)


class TestEventTypes:
    """Tests for concrete event dataclasses."""

    def test_webhook_event_defaults(self) -> None:
        event = WebhookEvent(
            provider="github",
            event_type_name="push",
            payload={"ref": "refs/heads/main"},
            delivery_id="abc-123",
        )
        assert event.source == "webhook"
        assert event.provider == "github"
        assert event.payload["ref"] == "refs/heads/main"

    def test_scheduled_event_defaults(self) -> None:
        event = ScheduledEvent(
            job_id="j1",
            job_name="daily-standup",
            prompt="Generate standup",
            target_chat_ids=[100, 200],
        )
        assert event.source == "scheduler"
        assert event.target_chat_ids == [100, 200]

    def test_agent_response_event(self) -> None:
        event = AgentResponseEvent(
            chat_id=789,
            text="Here's your summary",
            originating_event_id="orig-1",
        )
        assert event.source == "agent"
        assert event.parse_mode == "HTML"
        assert event.originating_event_id == "orig-1"

    def test_scheduled_event_with_skill(self) -> None:
        event = ScheduledEvent(
            job_name="standup",
            prompt="",
            skill_name="daily-standup",
            working_directory=Path("/projects/myapp"),
        )
        assert event.skill_name == "daily-standup"
        assert event.working_directory == Path("/projects/myapp")
