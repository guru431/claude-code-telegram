"""Single entry point for user-initiated Claude runs.

Two things must happen for *every* Claude run a user triggers, and both were
previously copy-pasted (or forgotten) per handler:

- **Cost accounting** — the rate-limit middleware only throttles; it explicitly
  expects the run path to hold the budget via ``reserve_cost`` and release it
  via ``settle_reservation``. A run that skips this spends real money the daily
  limiter never sees.
- **History persistence** — ``Storage.save_claude_interaction`` writes the
  message pair, tool usage, daily cost and audit row. A run that skips it is
  missing from exports, ``/history``, cost reporting and the audit trail.

Handlers call :func:`run_claude_for_user` so both happen in one place. Handlers
that already own their reservation (they need the estimate before building the
progress message) call :func:`persist_interaction` directly.
"""

from typing import Any, Awaitable, Callable, Optional, Tuple

import structlog

logger = structlog.get_logger()


async def persist_interaction(
    storage: Any,
    user_id: int,
    prompt: str,
    response: Any,
) -> None:
    """Store one completed Claude interaction, best effort.

    Persistence failures are logged and swallowed: the user already has their
    answer, and losing the history row must not turn a successful run into a
    visible error.
    """
    if storage is None or response is None:
        return

    try:
        await storage.save_claude_interaction(
            user_id=user_id,
            session_id=getattr(response, "session_id", "") or "",
            prompt=prompt,
            response=response,
            ip_address=None,  # Telegram doesn't provide IP
        )
    except Exception as e:
        logger.warning(
            "Failed to log interaction to storage",
            user_id=user_id,
            error=str(e),
        )


async def run_claude_for_user(
    *,
    run: Callable[[], Awaitable[Any]],
    prompt: str,
    user_id: int,
    rate_limiter: Any = None,
    storage: Any = None,
    estimated_cost: float = 0.0,
) -> Tuple[Optional[Any], Optional[str]]:
    """Run Claude on the user's behalf with budget accounting and persistence.

    ``run`` is a zero-argument coroutine function performing the actual call, so
    both ``ClaudeIntegration.run_command`` and ``.continue_session`` fit.

    Returns ``(response, budget_error)``. ``budget_error`` is set (with
    ``response`` None) only when the reservation was refused — the caller should
    show it to the user. Failures inside ``run`` propagate to the caller, which
    already renders them; the hold is released either way.
    """
    reservation_id: Optional[str] = None
    if rate_limiter is not None:
        reservation_id, reserve_error = await rate_limiter.reserve_cost(
            user_id, estimated_cost
        )
        if reserve_error:
            return None, reserve_error

    actual_cost = 0.0
    try:
        response = await run()
        if response is not None:
            # Charge the reported cost even when the run is flagged is_error:
            # error_max_turns and the max_budget_usd cap burn real tokens before
            # failing. A run with no ResultMessage reports 0 and settles at 0.
            actual_cost = getattr(response, "cost", 0.0) or 0.0
            await persist_interaction(storage, user_id, prompt, response)
        return response, None
    finally:
        # Always release the hold: success, soft error, exception and cancel
        # alike. An unsettled hold blocks the user's budget until it is swept.
        if rate_limiter is not None and reservation_id:
            await rate_limiter.settle_reservation(reservation_id, actual_cost)
