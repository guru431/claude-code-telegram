"""Tests for /cost and the near-limit budget warning.

Everything /cost shows was already collected — ``cost_tracking`` per day,
``messages.cost`` per run — but had no way out of the database: the only visible
signal was ``· Cost: $X.XX`` in /status, so a user first learned their budget was
gone from a refusal in the middle of a task.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.config.settings import Settings
from src.security.rate_limiter import RateLimiter


@pytest.fixture
def settings(tmp_path):
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        agentic_mode=True,
        claude_max_cost_per_user=10.0,
    )


@pytest.fixture
def orchestrator(settings):
    return MessageOrchestrator(settings, {})


def _update(user_id: int = 42):
    upd = MagicMock()
    upd.effective_user = MagicMock()
    upd.effective_user.id = user_id
    upd.message = AsyncMock()
    return upd


def _context(rate_limiter=None, storage=None):
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = {
        "rate_limiter": rate_limiter,
        "storage": storage,
        "audit_logger": None,
    }
    return ctx


class TestSparkline:
    def test_flat_series_renders_lowest_block(self, orchestrator):
        assert orchestrator._sparkline([0.0, 0.0, 0.0]) == "▁▁▁"

    def test_peak_renders_highest_block(self, orchestrator):
        line = orchestrator._sparkline([0.0, 1.0])
        assert line[0] == "▁"
        assert line[-1] == "█"

    def test_empty_series_is_empty(self, orchestrator):
        assert orchestrator._sparkline([]) == ""


class TestCostCommand:
    async def test_reports_today_limit_and_remainder(self, orchestrator, settings):
        limiter = RateLimiter(settings)
        limiter.cost_tracker[42] = 3.5
        # _effective_cost reports 0 for a window that has not been opened yet.
        limiter.cost_reset_time[42] = datetime.now(UTC)
        update = _update()

        await orchestrator.agentic_cost(update, _context(rate_limiter=limiter))

        text = update.message.reply_text.await_args.args[0]
        assert "$3.50" in text
        assert "$10.00" in text
        assert "$6.50" in text

    async def test_lists_the_days_most_expensive_runs(self, orchestrator, settings):
        limiter = RateLimiter(settings)
        today = datetime.now(UTC).date()
        storage = MagicMock()
        storage.costs = AsyncMock()
        storage.costs.get_user_daily_costs.return_value = [
            SimpleNamespace(date=today.isoformat(), daily_cost=1.25),
            SimpleNamespace(
                date=(today - timedelta(days=1)).isoformat(), daily_cost=0.5
            ),
        ]
        storage.messages = AsyncMock()
        storage.messages.get_top_costly_messages.return_value = [
            SimpleNamespace(cost=0.9, prompt="refactor the parser\nsecond line"),
            SimpleNamespace(cost=0.35, prompt="fix <b>escaping</b>"),
        ]
        update = _update()

        await orchestrator.agentic_cost(
            update, _context(rate_limiter=limiter, storage=storage)
        )

        text = update.message.reply_text.await_args.args[0]
        assert "$0.90" in text
        assert "refactor the parser" in text
        # Only the first line of the prompt is shown.
        assert "second line" not in text
        # Prompts are user-controlled text in an HTML message.
        assert "<b>escaping</b>" not in text
        assert "&lt;b&gt;escaping&lt;/b&gt;" in text

    async def test_history_failure_does_not_break_the_reply(
        self, orchestrator, settings
    ):
        limiter = RateLimiter(settings)
        storage = MagicMock()
        storage.costs = AsyncMock()
        storage.costs.get_user_daily_costs.side_effect = RuntimeError("db down")
        update = _update()

        await orchestrator.agentic_cost(
            update, _context(rate_limiter=limiter, storage=storage)
        )

        text = update.message.reply_text.await_args.args[0]
        assert "Today:" in text
        assert "History is unavailable" in text


class TestBudgetWarning:
    async def test_warns_once_when_crossing_the_threshold(self, settings):
        limiter = RateLimiter(settings)

        reservation_id, error = await limiter.reserve_cost(42, 0.0)
        assert error is None
        first = await limiter.settle_reservation(reservation_id, 8.5)
        assert first is not None and "8.50" in first

        # Still over the line on the next run: no repeat notification.
        reservation_id, _ = await limiter.reserve_cost(42, 0.0)
        assert await limiter.settle_reservation(reservation_id, 0.1) is None

    async def test_no_warning_below_the_threshold(self, settings):
        limiter = RateLimiter(settings)
        reservation_id, _ = await limiter.reserve_cost(42, 0.0)
        assert await limiter.settle_reservation(reservation_id, 1.0) is None

    async def test_daily_reset_rearms_the_warning(self, settings):
        limiter = RateLimiter(settings)
        reservation_id, _ = await limiter.reserve_cost(42, 0.0)
        assert await limiter.settle_reservation(reservation_id, 9.0) is not None

        # Roll the window over the way a new UTC day would.
        limiter.cost_reset_time[42] = datetime.now(UTC) - timedelta(days=2)
        reservation_id, _ = await limiter.reserve_cost(42, 0.0)
        assert await limiter.settle_reservation(reservation_id, 9.0) is not None
