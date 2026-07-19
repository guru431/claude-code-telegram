"""Telegram bot authentication middleware."""

import time
from datetime import UTC, datetime
from typing import Any, Callable, Dict

import structlog

logger = structlog.get_logger()

# Throttle for auth-rejection replies: at most one reply per user per window.
# The rate-limit middleware (group -1) is never reached for rejected users, so
# we throttle here to avoid spamming a reply + audit row per inbound message.
_REJECTION_REPLY_WINDOW = 60.0
_last_rejection_reply: Dict[int, float] = {}
# Timestamp of the last eviction sweep over ``_last_rejection_reply``. The sweep
# is O(len(map)), so it is throttled to once per window: without throttling a
# flood from many distinct unauthorized senders would scan the whole (growing)
# map on every inbound message, making rejection handling quadratic.
_last_rejection_gc: float = 0.0


async def auth_middleware(handler: Callable, event: Any, data: Dict[str, Any]) -> Any:
    """Check authentication before processing messages.

    This middleware:
    1. Checks if user is authenticated
    2. Attempts authentication if not authenticated
    3. Updates session activity
    4. Logs authentication events
    """
    # Extract user information
    user_id = event.effective_user.id if event.effective_user else None
    username = (
        getattr(event.effective_user, "username", None)
        if event.effective_user
        else None
    )

    if not user_id:
        logger.warning("No user information in update")
        return

    # Get dependencies from context
    auth_manager = data.get("auth_manager")
    audit_logger = data.get("audit_logger")

    if not auth_manager:
        logger.error("Authentication manager not available in middleware context")
        if event.effective_message:
            await event.effective_message.reply_text(
                "🔒 Authentication system unavailable. Please try again later."
            )
        return

    # Check if user is already authenticated
    if auth_manager.is_authenticated(user_id):
        # Update session activity
        if auth_manager.refresh_session(user_id):
            session = auth_manager.get_session(user_id)
            logger.debug(
                "Session refreshed",
                user_id=user_id,
                username=username,
                auth_provider=session.auth_provider if session else None,
            )

        # Continue to handler
        return await handler(event, data)

    # User not authenticated - attempt authentication
    logger.info(
        "Attempting authentication for user", user_id=user_id, username=username
    )

    # Try to authenticate. No credentials are collected on the message path,
    # so this always passes empty credentials -- whitelist auth (the only
    # provider today) does not need any.
    authentication_successful = await auth_manager.authenticate_user(user_id)

    # Log authentication attempt
    if audit_logger:
        await audit_logger.log_auth_attempt(
            user_id=user_id,
            success=authentication_successful,
            method="automatic",
            reason="message_received",
        )

    if authentication_successful:
        session = auth_manager.get_session(user_id)
        logger.info(
            "User authenticated successfully",
            user_id=user_id,
            username=username,
            auth_provider=session.auth_provider if session else None,
        )

        # Welcome message for new session
        if event.effective_message:
            await event.effective_message.reply_text(
                f"🔓 Welcome! You are now authenticated.\n"
                f"Session started at {datetime.now(UTC).strftime('%H:%M:%S UTC')}"
            )

        # Continue to handler
        return await handler(event, data)

    else:
        # Authentication failed
        logger.warning("Authentication failed", user_id=user_id, username=username)

        # Throttle the rejection reply to at most once per window per user. The
        # audit attempt above is still recorded for every message.
        global _last_rejection_gc
        now = time.monotonic()
        # Evict entries older than the throttle window so the map stays bounded
        # under a stream of distinct unauthorized senders (mirrors the GC in
        # burst_protection_middleware). Entries past the window are no longer
        # throttling anything, so dropping them is safe. The sweep itself runs
        # at most once per window to keep per-message cost O(1).
        if now - _last_rejection_gc >= _REJECTION_REPLY_WINDOW:
            _last_rejection_gc = now
            stale_cutoff = now - _REJECTION_REPLY_WINDOW
            stale = [u for u, t in _last_rejection_reply.items() if t < stale_cutoff]
            for uid in stale:
                _last_rejection_reply.pop(uid, None)
        last_reply = _last_rejection_reply.get(user_id, 0.0)
        if event.effective_message and now - last_reply >= _REJECTION_REPLY_WINDOW:
            _last_rejection_reply[user_id] = now
            await event.effective_message.reply_text(
                "🔒 <b>Authentication Required</b>\n\n"
                "You are not authorized to use this bot.\n"
                "Please contact the administrator for access.\n\n"
                f"Your Telegram ID: <code>{user_id}</code>\n"
                "Share this ID with the administrator to request access.",
                parse_mode="HTML",
            )
        return  # Stop processing
