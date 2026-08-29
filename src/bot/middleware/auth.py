"""Telegram bot authentication middleware."""

import time
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Optional, Tuple

import structlog

logger = structlog.get_logger()

# Throttle window for rejected senders: at most one reply *and* one audit row per
# user per window. The rate-limit middleware (group -1) is never reached for
# rejected users, so without this a single unauthorized sender spamming the
# bot's public @username produced one reply attempt and one audit INSERT per
# message — an unbounded write stream into a table pruned once a year
# (AUDIT_LOG_RETENTION_DAYS=365), for rows that all say the same thing.
_REJECTION_REPLY_WINDOW = 60.0
# user_id -> (window start, attempts suppressed since the window's logged row).
# The suppressed count is not discarded: it is written out with the next window's
# row, so the audit trail still shows the true volume of a flood.
_rejection_state: Dict[int, Tuple[float, int]] = {}
# Timestamp of the last eviction sweep over ``_rejection_state``. The sweep
# is O(len(map)), so it is throttled to once per window: without throttling a
# flood from many distinct unauthorized senders would scan the whole (growing)
# map on every inbound message, making rejection handling quadratic.
_last_rejection_gc: float = 0.0
# Users whose identity has already been written this process. Recording it is a
# once-per-session need, not a per-message one.
_identity_recorded: set[int] = set()


def _should_report_rejection(user_id: int, now: float) -> Tuple[bool, int]:
    """Decide whether to report this rejection, and how many were suppressed.

    Returns ``(report, suppressed)``. ``report`` is True once per window; the
    accompanying ``suppressed`` count covers the attempts silently absorbed since
    the previous reported one.
    """
    window_start, suppressed = _rejection_state.get(user_id, (0.0, 0))
    if now - window_start >= _REJECTION_REPLY_WINDOW:
        _rejection_state[user_id] = (now, 0)
        return True, suppressed
    _rejection_state[user_id] = (window_start, suppressed + 1)
    return False, 0


async def _record_identity(
    data: Dict[str, Any], user_id: int, username: Optional[str]
) -> None:
    """Persist the sender's Telegram username, once per process per user."""
    if user_id in _identity_recorded:
        return
    storage = data.get("storage")
    if storage is None:
        return
    _identity_recorded.add(user_id)
    try:
        await storage.users.record_identity(user_id, username)
    except Exception as exc:
        # Never let a bookkeeping write block an authorized message.
        _identity_recorded.discard(user_id)
        logger.warning(
            "Failed to record user identity", user_id=user_id, error=str(exc)
        )


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

    if authentication_successful:
        # Every success is audited: it happens once per session, not per message.
        if audit_logger:
            await audit_logger.log_auth_attempt(
                user_id=user_id,
                success=True,
                method="automatic",
                reason="message_received",
            )

        session = auth_manager.get_session(user_id)
        logger.info(
            "User authenticated successfully",
            user_id=user_id,
            username=username,
            auth_provider=session.auth_provider if session else None,
        )
        await _record_identity(data, user_id, username)

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

        global _last_rejection_gc
        now = time.monotonic()
        # Evict entries older than the throttle window so the map stays bounded
        # under a stream of distinct unauthorized senders. Entries past the
        # window are no longer throttling anything, so dropping them is safe.
        # The sweep itself runs at most once per window to keep per-message
        # cost O(1).
        if now - _last_rejection_gc >= _REJECTION_REPLY_WINDOW:
            _last_rejection_gc = now
            stale_cutoff = now - _REJECTION_REPLY_WINDOW
            stale = [u for u, (t, _) in _rejection_state.items() if t < stale_cutoff]
            for uid in stale:
                _rejection_state.pop(uid, None)

        report, suppressed = _should_report_rejection(user_id, now)
        if report:
            if audit_logger:
                await audit_logger.log_auth_attempt(
                    user_id=user_id,
                    success=False,
                    method="automatic",
                    reason="message_received",
                    suppressed_attempts=suppressed,
                )
            if event.effective_message:
                await event.effective_message.reply_text(
                    "🔒 <b>Authentication Required</b>\n\n"
                    "You are not authorized to use this bot.\n"
                    "Please contact the administrator for access.\n\n"
                    f"Your Telegram ID: <code>{user_id}</code>\n"
                    "Share this ID with the administrator to request access.",
                    parse_mode="HTML",
                )
        return  # Stop processing
