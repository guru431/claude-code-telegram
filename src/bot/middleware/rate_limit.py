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

    # Base cost for any message
    base_cost = 0.01

    # Additional cost based on message length
    length_cost = len(message_text) * 0.0001

    # Higher cost for certain types of messages
    if (message and message.document) or (message and message.photo):
        # File uploads cost more
        return base_cost + length_cost + 0.05

    if message_text.startswith("/"):
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

    if any(keyword in message_text.lower() for keyword in complex_keywords):
        return base_cost + length_cost + 0.03

    return base_cost + length_cost


async def cost_tracking_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    """Track actual costs after processing.

    This middleware runs after the main handler to track
    actual costs incurred during processing.
    """
    effective_user = getattr(event, "effective_user", None)
    if not effective_user:
        return await handler(event, data)
    user_id = effective_user.id
    rate_limiter = data.get("rate_limiter")

    # Store start time for duration tracking
    import time

    start_time = time.time()

    try:
        # Execute the handler
        result = await handler(event, data)

        # Calculate processing time
        processing_time = time.time() - start_time

        # Get actual cost from context if available
        actual_cost = data.get("actual_cost", 0.0)

        if actual_cost > 0 and rate_limiter:
            # Update cost tracking with actual cost
            # Note: This would require extending the rate limiter
            # to support post-processing cost updates
            logger.debug(
                "Actual cost tracked",
                user_id=user_id,
                actual_cost=actual_cost,
                processing_time=processing_time,
            )

        return result

    except Exception as e:
        # Log error but don't update costs for failed operations
        processing_time = time.time() - start_time
        logger.error(
            "Handler execution failed",
            user_id=user_id,
            processing_time=processing_time,
            error=str(e),
        )
        raise


async def burst_protection_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    """Additional burst protection for high-frequency requests.

    This middleware provides an additional layer of protection
    against burst attacks that might bypass normal rate limiting.
    """
    # ``event.from_user`` is missing for non-message updates (e.g. inline
    # callback queries on channel posts); fall back to ``effective_user``
    # so we never raise AttributeError on such updates.
    effective_user = getattr(event, "effective_user", None)
    if not effective_user:
        return await handler(event, data)
    user_id = effective_user.id

    import time

    current_time = time.time()

    # Get or create burst tracker. The tracker accumulates one entry per user
    # ever seen, so we periodically evict users that have gone idle to keep
    # memory bounded under sustained traffic from many distinct users.
    burst_tracker = data.setdefault("burst_tracker", {})
    last_gc = data.get("_burst_tracker_last_gc", 0.0)
    # GC at most once every 60s, and only when the tracker has grown.
    if current_time - last_gc > 60 and len(burst_tracker) > 100:
        idle_cutoff = current_time - 600  # 10 minutes idle → evict
        stale_users = [
            uid
            for uid, ud in burst_tracker.items()
            if not ud.get("recent_requests") or max(ud["recent_requests"]) < idle_cutoff
        ]
        for uid in stale_users:
            burst_tracker.pop(uid, None)
        data["_burst_tracker_last_gc"] = current_time

    user_burst_data = burst_tracker.setdefault(
        user_id, {"recent_requests": [], "warnings_sent": 0}
    )

    # Clean old requests (older than 10 seconds)
    user_burst_data["recent_requests"] = [
        req_time
        for req_time in user_burst_data["recent_requests"]
        if current_time - req_time < 10
    ]

    # Add current request
    user_burst_data["recent_requests"].append(current_time)
    # Hard cap: never let a single user's list grow beyond a sane bound even
    # if the time-window filter glitches; we only need the last few entries.
    if len(user_burst_data["recent_requests"]) > 200:
        user_burst_data["recent_requests"] = user_burst_data["recent_requests"][-50:]

    # Check for burst (more than 5 requests in 10 seconds)
    if len(user_burst_data["recent_requests"]) > 5:
        user_burst_data["warnings_sent"] += 1

        logger.warning(
            "Burst protection triggered",
            user_id=user_id,
            requests_in_window=len(user_burst_data["recent_requests"]),
            warnings_sent=user_burst_data["warnings_sent"],
        )

        # Progressive response based on warning count
        if user_burst_data["warnings_sent"] == 1:
            if event.effective_message:
                await event.effective_message.reply_text(
                    "⚠️ <b>Slow down!</b>\n\n"
                    "You're sending requests too quickly. "
                    "Please wait a moment between messages.",
                    parse_mode="HTML",
                )
        elif user_burst_data["warnings_sent"] <= 3:
            if event.effective_message:
                await event.effective_message.reply_text(
                    "🛑 <b>Rate limit warning</b>\n\n"
                    "Please reduce your request frequency to avoid being temporarily blocked.",
                    parse_mode="HTML",
                )
        else:
            if event.effective_message:
                await event.effective_message.reply_text(
                    "🚫 <b>Temporarily blocked</b>\n\n"
                    "Too many rapid requests. Please wait 30 seconds before trying again.",
                    parse_mode="HTML",
                )
            return  # Block this request

    return await handler(event, data)
