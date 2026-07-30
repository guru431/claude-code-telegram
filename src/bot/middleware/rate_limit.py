"""Rate limiting middleware for Telegram bot."""

from typing import Any, Callable, Dict

import structlog

logger = structlog.get_logger()


async def rate_limit_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    """Throttle requests before processing messages.

    This middleware handles **request throttling only** — it consumes one
    token from the user's bucket per update and rejects the update if the
    user's cost budget is already too low to absorb the projected spend. It
    does **not** charge money: an update may never reach Claude (free command,
    callback, plain ack), so billing it would exhaust the budget for free.

    The cost budget is spent by the Claude run itself, which takes an explicit
    hold via ``RateLimiter.reserve_cost`` and must release it in a ``finally``
    via ``RateLimiter.settle_reservation``.

    This middleware:
    1. Checks request rate limits
    2. Checks (without charging) that the projected cost still fits the budget
    3. Logs rate limit violations
    4. Provides helpful error messages
    """
    # Stop button callbacks must pass through without consuming tokens/cost so
    # an in-progress run can always be interrupted (auth is already applied
    # upstream at group -2).
    if (
        event.callback_query
        and event.callback_query.data
        and event.callback_query.data.startswith("stop:")
    ):
        return await handler(event, data)

    user_id = event.effective_user.id if event.effective_user else None
    username = (
        getattr(event.effective_user, "username", None)
        if event.effective_user
        else None
    )

    if not user_id:
        logger.warning("No user information in update")
        return await handler(event, data)

    # Get dependencies from context
    rate_limiter = data.get("rate_limiter")
    audit_logger = data.get("audit_logger")

    if not rate_limiter:
        logger.error("Rate limiter not available in middleware context")
        # Don't block on missing rate limiter - this could be a config issue
        return await handler(event, data)

    # Projected (not charged) cost, used only to reject an update that could
    # not fit in the remaining budget even in the best case.
    estimated_cost = estimate_message_cost(event)

    # Throttle: one bucket token per update, plus a budget headroom check.
    allowed, message = await rate_limiter.check_rate_limit(
        user_id=user_id, cost=estimated_cost, tokens=1  # One token per message
    )

    if not allowed:
        logger.warning(
            "Rate limit exceeded",
            user_id=user_id,
            username=username,
            estimated_cost=estimated_cost,
            message=message,
        )

        # Log rate limit violation with the real measurement. check_rate_limit
        # only returns (bool, message); infer which limit tripped from the
        # message text and pull the matching numbers from get_user_status.
        if audit_logger:
            status = rate_limiter.get_user_status(user_id)
            if message and message.startswith("Cost limit exceeded"):
                limit_type = "cost"
                cost_usage = status["cost_usage"]
                current_usage = float(cost_usage["current"])
                limit_value = float(cost_usage["limit"])
            else:
                limit_type = "request"
                bucket = status["request_bucket"]
                # Request "usage" = tokens consumed out of capacity.
                current_usage = float(bucket["capacity"]) - float(bucket["tokens"])
                limit_value = float(bucket["capacity"])
            await audit_logger.log_rate_limit_exceeded(
                user_id=user_id,
                limit_type=limit_type,
                current_usage=current_usage,
                limit_value=limit_value,
            )

        # Send user-friendly rate limit message
        if event.effective_message:
            await event.effective_message.reply_text(f"⏱️ {message}")
        return  # Stop processing

    # Rate limit check passed
    logger.debug(
        "Rate limit check passed",
        user_id=user_id,
        username=username,
        estimated_cost=estimated_cost,
    )

    # Continue to handler
    return await handler(event, data)


def estimate_message_cost(event: Any) -> float:
    """Estimate the cost of processing a message.

    Used as a *projected* spend for the budget headroom check and as the
    initial hold amount when a Claude run reserves budget. It is never
    charged on its own.

    This is a simple heuristic - in practice, you'd want more
    sophisticated cost estimation based on:
    - Message type (text, file, command)
    - Content complexity
    - Expected Claude usage
    """
    message = event.effective_message
    message_text = (message.text or "") if message else ""

    # Higher cost for file uploads, whose real work is not in the caption text.
    if (message and message.document) or (message and message.photo):
        return 0.01 + len(message_text) * 0.0001 + 0.05

    return estimate_prompt_cost(message_text)


def estimate_prompt_cost(prompt: str) -> float:
    """Estimate the cost of one Claude run over *prompt*.

    Split out of ``estimate_message_cost`` so entry points that have a prompt
    but no Update (inline-button callbacks: continue, quick action, follow-up)
    size their budget hold the same way a typed message does.
    """
    # Base cost for any message
    base_cost = 0.01

    # Additional cost based on message length
    length_cost = len(prompt) * 0.0001

    if prompt.startswith("/"):
        # Commands cost more
        return base_cost + length_cost + 0.02

    # Check for complex operations keywords
    complex_keywords = [
        "analyze",
        "generate",
        "create",
        "build",
        "compile",
        "test",
        "debug",
        "refactor",
        "optimize",
        "explain",
    ]

    if any(keyword in prompt.lower() for keyword in complex_keywords):
        return base_cost + length_cost + 0.03

    return base_cost + length_cost
