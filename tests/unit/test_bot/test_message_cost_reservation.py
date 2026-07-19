"""Budget-reservation invariants for the classic message handlers.

Every classic path that runs Claude must take a cost reservation before the
run and settle it in a ``finally``. The invariant under test is that a failed
run (exception) or an empty run (``is_error``) settles at ``0.0`` and leaves
the user's budget exactly where it started — the hold must never leak.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers.message import (
    handle_document,
    handle_photo,
    handle_text_message,
    handle_voice,
)
from src.config import create_test_config
from src.security.rate_limiter import RateLimiter

USER_ID = 4242


def _claude_response(cost: float = 0.42, is_error: bool = False) -> SimpleNamespace:
    """Minimal stand-in for a ClaudeResponse."""
    return SimpleNamespace(
        session_id="session-1",
        cost=cost,
        content="done",
        is_error=is_error,
    )


def _make_message() -> MagicMock:
    """A Telegram message whose reply_text returns an awaitable progress msg."""
    progress_msg = MagicMock()
    progress_msg.edit_text = AsyncMock()
    progress_msg.delete = AsyncMock()

    message = MagicMock()
    message.message_id = 1
    message.text = "please analyze this"
    message.caption = None
    message.reply_text = AsyncMock(return_value=progress_msg)
    message.chat.send_action = AsyncMock()
    return message


def _make_update(message: MagicMock) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message = message
    update.effective_message = message
    return update


def _make_context(
    tmp_path, rate_limiter: RateLimiter, run_command: AsyncMock
) -> MagicMock:
    settings = create_test_config(approved_directory=str(tmp_path))
    claude_integration = MagicMock()
    claude_integration.run_command = run_command

    context = MagicMock()
    context.bot_data = {
        "settings": settings,
        "rate_limiter": rate_limiter,
        "claude_integration": claude_integration,
    }
    context.user_data = {}
    return context


@pytest.fixture
def rate_limiter(tmp_path) -> RateLimiter:
    """Real rate limiter that also records what each settle was charged.

    Recording matters: without it these tests would pass vacuously against a
    handler that never reserved anything at all.
    """
    limiter = RateLimiter(create_test_config(approved_directory=str(tmp_path)))
    limiter.settled_costs = []
    real_settle = limiter.settle_reservation

    async def recording_settle(reservation_id: str, actual_cost: float = 0.0) -> None:
        limiter.settled_costs.append(actual_cost)
        await real_settle(reservation_id, actual_cost)

    limiter.settle_reservation = recording_settle
    return limiter


def _assert_settled_free(limiter: RateLimiter) -> None:
    """A hold was taken, released at zero cost, and left no budget behind."""
    assert limiter.settled_costs == [0.0]
    assert limiter.reservations == {}
    assert not limiter.user_reservations.get(USER_ID)
    assert limiter.cost_tracker[USER_ID] == pytest.approx(0.0)


# --------------------------------------------------------------------------
# 1. classic text
# --------------------------------------------------------------------------


async def _run_text(tmp_path, rate_limiter, run_command):
    message = _make_message()
    update = _make_update(message)
    context = _make_context(tmp_path, rate_limiter, run_command)
    await handle_text_message(update, context)


async def test_text_run_failure_releases_reservation(tmp_path, rate_limiter):
    """An exception inside run_command must not leak the budget hold."""
    await _run_text(tmp_path, rate_limiter, AsyncMock(side_effect=RuntimeError("boom")))
    _assert_settled_free(rate_limiter)


async def test_text_is_error_releases_reservation(tmp_path, rate_limiter):
    """A run flagged is_error produced nothing usable and is not charged."""
    await _run_text(
        tmp_path,
        rate_limiter,
        AsyncMock(return_value=_claude_response(cost=0.42, is_error=True)),
    )
    _assert_settled_free(rate_limiter)


async def test_text_success_charges_actual_cost(tmp_path, rate_limiter):
    """A successful run settles at the run's real cost, not the estimate."""
    await _run_text(
        tmp_path, rate_limiter, AsyncMock(return_value=_claude_response(cost=0.42))
    )
    assert rate_limiter.reservations == {}
    assert rate_limiter.cost_tracker[USER_ID] == pytest.approx(0.42)


# --------------------------------------------------------------------------
# 2. classic document
# --------------------------------------------------------------------------


async def _run_document(tmp_path, rate_limiter, run_command):
    message = _make_message()
    telegram_file = MagicMock()
    telegram_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"print(1)"))
    message.document = MagicMock()
    message.document.file_name = "snippet.py"
    message.document.file_size = 2048
    message.document.get_file = AsyncMock(return_value=telegram_file)

    update = _make_update(message)
    context = _make_context(tmp_path, rate_limiter, run_command)
    await handle_document(update, context)


async def test_document_run_failure_releases_reservation(tmp_path, rate_limiter):
    await _run_document(
        tmp_path, rate_limiter, AsyncMock(side_effect=RuntimeError("boom"))
    )
    _assert_settled_free(rate_limiter)


async def test_document_is_error_releases_reservation(tmp_path, rate_limiter):
    await _run_document(
        tmp_path,
        rate_limiter,
        AsyncMock(return_value=_claude_response(cost=0.42, is_error=True)),
    )
    _assert_settled_free(rate_limiter)


async def test_document_does_not_double_throttle(tmp_path, rate_limiter):
    """The handler must not burn a second bucket token for the same update.

    rate_limit_middleware already throttled this update at group -1; the
    handler only reserves budget.
    """
    tokens_before = rate_limiter._get_or_create_bucket(USER_ID).tokens
    await _run_document(
        tmp_path, rate_limiter, AsyncMock(return_value=_claude_response(cost=0.0))
    )
    tokens_after = rate_limiter._get_or_create_bucket(USER_ID).tokens
    assert tokens_after >= tokens_before


# --------------------------------------------------------------------------
# 3. photo
# --------------------------------------------------------------------------


async def _run_photo(tmp_path, rate_limiter, run_command):
    message = _make_message()
    message.photo = [MagicMock()]

    image_handler = MagicMock()
    image_handler.process_image = AsyncMock(
        return_value=SimpleNamespace(prompt="describe this image")
    )
    features = MagicMock()
    features.get_image_handler.return_value = image_handler

    update = _make_update(message)
    context = _make_context(tmp_path, rate_limiter, run_command)
    context.bot_data["features"] = features
    await handle_photo(update, context)


async def test_photo_run_failure_releases_reservation(tmp_path, rate_limiter):
    await _run_photo(
        tmp_path, rate_limiter, AsyncMock(side_effect=RuntimeError("boom"))
    )
    _assert_settled_free(rate_limiter)


async def test_photo_is_error_releases_reservation(tmp_path, rate_limiter):
    await _run_photo(
        tmp_path,
        rate_limiter,
        AsyncMock(return_value=_claude_response(cost=0.42, is_error=True)),
    )
    _assert_settled_free(rate_limiter)


# --------------------------------------------------------------------------
# 4. voice
# --------------------------------------------------------------------------


async def _run_voice(tmp_path, rate_limiter, run_command):
    message = _make_message()
    message.voice = MagicMock()

    voice_handler = MagicMock()
    voice_handler.process_voice_message = AsyncMock(
        return_value=SimpleNamespace(prompt="transcribed prompt")
    )
    features = MagicMock()
    features.get_voice_handler.return_value = voice_handler

    update = _make_update(message)
    context = _make_context(tmp_path, rate_limiter, run_command)
    context.bot_data["features"] = features
    await handle_voice(update, context)


async def test_voice_run_failure_releases_reservation(tmp_path, rate_limiter):
    await _run_voice(
        tmp_path, rate_limiter, AsyncMock(side_effect=RuntimeError("boom"))
    )
    _assert_settled_free(rate_limiter)


async def test_voice_is_error_releases_reservation(tmp_path, rate_limiter):
    await _run_voice(
        tmp_path,
        rate_limiter,
        AsyncMock(return_value=_claude_response(cost=0.42, is_error=True)),
    )
    _assert_settled_free(rate_limiter)


# --------------------------------------------------------------------------
# reservation refusal
# --------------------------------------------------------------------------


async def test_text_reservation_refused_skips_claude_run(tmp_path, rate_limiter):
    """When the budget is exhausted the handler must not call Claude at all."""
    # Mark the window as freshly reset so reserve_cost does not zero it first.
    rate_limiter.cost_reset_time[USER_ID] = datetime.now(UTC)
    rate_limiter.cost_tracker[USER_ID] = rate_limiter.config.claude_max_cost_per_user

    run_command = AsyncMock(return_value=_claude_response())
    await _run_text(tmp_path, rate_limiter, run_command)

    run_command.assert_not_called()
    assert rate_limiter.reservations == {}
