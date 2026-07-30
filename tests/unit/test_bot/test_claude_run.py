"""Every user-initiated Claude run must be budgeted and persisted.

Regression: /continue, the continue button, quick actions and follow-up buttons
called run_command() directly — they spent real money the daily budget never saw
and left no message pair, tool usage or cost row behind.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.utils.claude_run import persist_interaction, run_claude_for_user


def _response(cost: float = 0.42, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="sess-1", cost=cost, is_error=is_error, content="ok"
    )


def _rate_limiter(reserve_error: str | None = None) -> AsyncMock:
    limiter = AsyncMock()
    limiter.reserve_cost = AsyncMock(
        return_value=(None if reserve_error else "res-1", reserve_error)
    )
    limiter.settle_reservation = AsyncMock()
    return limiter


class TestBudgetAccounting:
    async def test_reserves_and_settles_actual_cost(self):
        limiter = _rate_limiter()
        storage = AsyncMock()

        response, error = await run_claude_for_user(
            run=AsyncMock(return_value=_response(cost=0.42)),
            prompt="hi",
            user_id=7,
            rate_limiter=limiter,
            storage=storage,
            estimated_cost=0.05,
        )

        assert error is None
        assert response.cost == 0.42
        limiter.reserve_cost.assert_awaited_once_with(7, 0.05)
        limiter.settle_reservation.assert_awaited_once_with("res-1", 0.42)

    async def test_refused_reservation_returns_message_and_skips_run(self):
        limiter = _rate_limiter(reserve_error="Cost limit exceeded.")
        run = AsyncMock()

        response, error = await run_claude_for_user(
            run=run, prompt="hi", user_id=7, rate_limiter=limiter
        )

        assert response is None
        assert error == "Cost limit exceeded."
        run.assert_not_awaited()
        limiter.settle_reservation.assert_not_awaited()

    async def test_error_response_still_charges_reported_cost(self):
        """A flagged-error run burned real tokens; the budget must see them."""
        limiter = _rate_limiter()

        await run_claude_for_user(
            run=AsyncMock(return_value=_response(cost=0.9, is_error=True)),
            prompt="hi",
            user_id=7,
            rate_limiter=limiter,
        )

        limiter.settle_reservation.assert_awaited_once_with("res-1", 0.9)

    async def test_error_response_without_cost_settles_at_zero(self):
        """A run that produced no ResultMessage has no known cost."""
        limiter = _rate_limiter()

        await run_claude_for_user(
            run=AsyncMock(return_value=_response(cost=0.0, is_error=True)),
            prompt="hi",
            user_id=7,
            rate_limiter=limiter,
        )

        limiter.settle_reservation.assert_awaited_once_with("res-1", 0.0)

    async def test_hold_released_when_the_run_raises(self):
        limiter = _rate_limiter()

        with pytest.raises(RuntimeError):
            await run_claude_for_user(
                run=AsyncMock(side_effect=RuntimeError("boom")),
                prompt="hi",
                user_id=7,
                rate_limiter=limiter,
            )

        limiter.settle_reservation.assert_awaited_once_with("res-1", 0.0)


class TestPersistence:
    async def test_successful_run_is_stored(self):
        storage = AsyncMock()

        await run_claude_for_user(
            run=AsyncMock(return_value=_response()),
            prompt="do the thing",
            user_id=7,
            storage=storage,
        )

        storage.save_claude_interaction.assert_awaited_once()
        kwargs = storage.save_claude_interaction.await_args.kwargs
        assert kwargs["user_id"] == 7
        assert kwargs["prompt"] == "do the thing"
        assert kwargs["session_id"] == "sess-1"

    async def test_storage_failure_does_not_break_the_run(self):
        storage = AsyncMock()
        storage.save_claude_interaction = AsyncMock(side_effect=RuntimeError("db down"))

        response, error = await run_claude_for_user(
            run=AsyncMock(return_value=_response()),
            prompt="hi",
            user_id=7,
            storage=storage,
        )

        assert error is None
        assert response is not None

    async def test_missing_storage_is_a_no_op(self):
        await persist_interaction(None, 7, "hi", _response())

    async def test_missing_session_id_is_passed_as_empty_string(self):
        storage = AsyncMock()
        response = SimpleNamespace(session_id=None, cost=0.0, is_error=False)

        await persist_interaction(storage, 7, "hi", response)

        assert storage.save_claude_interaction.await_args.kwargs["session_id"] == ""
