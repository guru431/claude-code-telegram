"""Rate limiting implementation with multiple strategies.

Two independent concerns live here and must not be conflated:

- **Request throttling** — a token bucket per user, consumed by every update
  (``check_rate_limit``). It costs no money.
- **Cost budget** — real dollars spent on Claude runs, held via an explicit
  reservation (``reserve_cost``) and settled against the run's actual cost
  (``settle_reservation``). Only a real Claude run may reserve budget.

Features:
- Token bucket algorithm
- Cost-based limiting with explicit, per-run reservations
- Per-user tracking
- Burst handling
"""

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple

import structlog

from ..config.settings import Settings

logger = structlog.get_logger()


@dataclass
class RateLimitBucket:
    """Token bucket for rate limiting."""

    capacity: int
    tokens: float
    last_update: datetime
    refill_rate: float = 1.0  # tokens per second

    def consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens from bucket."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def can_consume(self, tokens: int = 1) -> bool:
        """Return True if *tokens* are available, without consuming them."""
        self._refill()
        return self.tokens >= tokens

    def _refill(self) -> None:
        """Refill tokens based on time passed."""
        now = datetime.now(UTC)
        self.tokens = self._tokens_at(now)
        self.last_update = now

    def _tokens_at(self, now: datetime) -> float:
        """Compute available tokens at *now* without mutating state.

        Pure read helper shared by the mutating ``_refill`` and the
        non-mutating ``get_status``; it never writes ``self.tokens`` or
        ``self.last_update`` so it is safe to call outside the rate
        limiter's per-user lock.
        """
        elapsed = (now - self.last_update).total_seconds()
        # Cap elapsed at the time needed to refill to full capacity. This
        # avoids float-precision loss when a bucket sat idle for a very
        # long time (elapsed * refill_rate would otherwise dwarf
        # self.tokens and small additions would silently round away).
        if self.refill_rate > 0:
            max_useful_elapsed = (self.capacity * 2) / self.refill_rate
            if elapsed > max_useful_elapsed:
                elapsed = max_useful_elapsed
        return min(self.capacity, self.tokens + (elapsed * self.refill_rate))

    def get_wait_time(self, tokens: int = 1) -> float:
        """Get time to wait before tokens are available."""
        self._refill()
        if self.tokens >= tokens:
            return 0.0

        tokens_needed = tokens - self.tokens
        return tokens_needed / self.refill_rate

    def get_status(self) -> Dict[str, float]:
        """Get current bucket status without mutating bucket state.

        Tokens are computed on the fly via ``_tokens_at`` so this read
        path is safe to call outside the rate limiter's lock.
        """
        tokens = self._tokens_at(datetime.now(UTC))
        return {
            "capacity": self.capacity,
            "tokens": tokens,
            "utilization": (self.capacity - tokens) / self.capacity,
            "refill_rate": self.refill_rate,
        }


@dataclass
class CostReservation:
    """A budget hold placed for exactly one real Claude run.

    ``amount`` is the estimate currently counted against the user's budget.
    A daily cost reset zeroes it (the tracker it was charged to is gone) so
    settling later cannot drive the fresh window negative.
    """

    id: str
    user_id: int
    amount: float
    created_at: datetime


class RateLimiter:
    """Main rate limiting system with request and cost-based limits."""

    # An unsettled reservation is a bug in the caller (a missing ``finally``).
    # Sweep it after this long so a leaked hold cannot block a user forever.
    RESERVATION_MAX_AGE = timedelta(hours=1)

    def __init__(
        self,
        config: Settings,
        daily_cost_loader: Optional[Callable[[int], Awaitable[float]]] = None,
    ):
        self.config = config
        self.request_buckets: Dict[int, RateLimitBucket] = {}
        self.cost_tracker: Dict[int, float] = defaultdict(float)
        self.cost_reset_time: Dict[int, datetime] = {}
        # Reads today's already-spent amount from persistent storage the first
        # time a user touches the budget in this process. Without it a restart
        # hands every user a full daily budget again, because cost_tracker is
        # in-memory only while the real spend lives in SQLite.
        self._daily_cost_loader = daily_cost_loader
        self._hydrated_users: Set[int] = set()
        # Outstanding budget holds, keyed by reservation id, plus a per-user
        # index for reset/cleanup. A hold is created only by reserve_cost (a
        # real Claude run) and removed only by settle_reservation or the
        # stale sweep — never by an ordinary throttle check.
        self.reservations: Dict[str, CostReservation] = {}
        self.user_reservations: Dict[int, Set[str]] = defaultdict(set)
        self.locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Calculate refill rate from config
        self.refill_rate = (
            self.config.rate_limit_requests / self.config.rate_limit_window
        )

        logger.info(
            "Rate limiter initialized",
            requests_per_window=self.config.rate_limit_requests,
            window_seconds=self.config.rate_limit_window,
            burst_capacity=self.config.rate_limit_burst,
            max_cost_per_user=self.config.claude_max_cost_per_user,
            refill_rate=self.refill_rate,
        )

    async def check_rate_limit(
        self, user_id: int, cost: float = 0.0, tokens: int = 1
    ) -> Tuple[bool, Optional[str]]:
        """Throttle a request: consume bucket tokens, verify budget headroom.

        This is the **request throttling** path and it spends no money. It is
        safe to call for every update, including free commands and callbacks.

        ``cost`` is a *projected* spend used only to reject a request that
        could not possibly fit in the remaining budget; it is checked but
        never charged. Money is charged only by ``reserve_cost`` /
        ``settle_reservation``, which a real Claude run must use.
        """
        async with self.locks[user_id]:
            await self._ensure_cost_hydrated(user_id)

            # Check request rate limit
            rate_allowed, rate_message = self._check_request_rate(user_id, tokens)
            if not rate_allowed:
                logger.warning(
                    "Request rate limit exceeded",
                    user_id=user_id,
                    tokens_requested=tokens,
                )
                return False, rate_message

            # Check cost headroom without charging it.
            cost_allowed, cost_message = self._check_cost_limit(user_id, cost)
            if not cost_allowed:
                logger.warning(
                    "Cost limit exceeded",
                    user_id=user_id,
                    cost_projected=cost,
                    current_usage=self.cost_tracker[user_id],
                )
                return False, cost_message

            # Only the request token is actually consumed here.
            self._consume_request_tokens(user_id, tokens)

            logger.debug(
                "Rate limit check passed",
                user_id=user_id,
                projected_cost=cost,
                tokens=tokens,
            )
            return True, None

    async def reserve_cost(
        self, user_id: int, estimated_cost: float
    ) -> Tuple[Optional[str], Optional[str]]:
        """Place a budget hold for one real Claude run.

        Returns ``(reservation_id, None)`` on success, or ``(None, message)``
        when the user's remaining budget cannot absorb *estimated_cost*.

        The caller **must** settle the returned id in a ``finally`` block via
        ``settle_reservation`` — for success, zero cost, error and cancel
        alike. An unsettled hold keeps blocking budget until it is swept as
        stale (``RESERVATION_MAX_AGE``).
        """
        amount = max(0.0, estimated_cost)

        async with self.locks[user_id]:
            await self._ensure_cost_hydrated(user_id)
            self._maybe_reset_cost_tracker(user_id)
            self._sweep_stale_reservations(user_id)

            allowed, message = self._check_cost_limit(user_id, amount)
            if not allowed:
                logger.warning(
                    "Cost reservation refused",
                    user_id=user_id,
                    estimated_cost=amount,
                    current_usage=self.cost_tracker[user_id],
                )
                return None, message

            reservation_id = uuid.uuid4().hex
            self.reservations[reservation_id] = CostReservation(
                id=reservation_id,
                user_id=user_id,
                amount=amount,
                created_at=datetime.now(UTC),
            )
            self.user_reservations[user_id].add(reservation_id)
            self._track_cost(user_id, amount)

            logger.debug(
                "Cost reserved",
                user_id=user_id,
                reservation_id=reservation_id,
                estimated_cost=amount,
            )
            return reservation_id, None

    async def settle_reservation(
        self, reservation_id: str, actual_cost: float = 0.0
    ) -> None:
        """Release a specific hold and charge that run's real cost.

        ``actual_cost`` of ``0.0`` is a valid outcome (cancelled run, cached
        reply, failed run) and releases the hold **in full**. Unknown or
        already-settled ids are ignored, so a ``finally`` block may call this
        unconditionally without guarding against double-settlement.
        """
        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            logger.debug(
                "Unknown or already-settled cost reservation",
                reservation_id=reservation_id,
            )
            return

        user_id = reservation.user_id
        async with self.locks[user_id]:
            # Re-read under the lock: a concurrent settle/sweep may have won.
            # The reset must run before the pop so it can zero this hold's
            # amount if the daily window rolled over.
            self._maybe_reset_cost_tracker(user_id)
            reservation = self.reservations.pop(reservation_id, None)
            if reservation is None:
                return
            self.user_reservations[user_id].discard(reservation_id)

            # Back out exactly this run's hold, then charge what it really
            # cost. Never drive the tracker negative.
            self.cost_tracker[user_id] = max(
                0.0, self.cost_tracker[user_id] - reservation.amount
            )
            if actual_cost > 0:
                self._track_cost(user_id, actual_cost)

            logger.debug(
                "Cost reservation settled",
                user_id=user_id,
                reservation_id=reservation_id,
                reserved=reservation.amount,
                actual_cost=actual_cost,
                total_usage=self.cost_tracker[user_id],
            )

    async def _ensure_cost_hydrated(self, user_id: int) -> None:
        """Seed this user's daily tracker from storage, once per process.

        Caller must hold ``self.locks[user_id]``. Runs exactly once per user:
        every cost settled afterwards is added both to the in-memory tracker and
        to ``cost_tracking``, so re-reading storage later would double-count.
        A loader failure is not retried for the same reason — a retry could land
        after some spend had already accumulated in memory.
        """
        if self._daily_cost_loader is None or user_id in self._hydrated_users:
            return

        self._hydrated_users.add(user_id)
        try:
            already_spent = await self._daily_cost_loader(user_id)
        except Exception as e:
            logger.warning(
                "Failed to hydrate daily cost from storage",
                user_id=user_id,
                error=str(e),
            )
            return

        if already_spent <= 0:
            return

        # Run the (first, always-due) reset before seeding so it cannot wipe
        # the value we just loaded, then anchor the window to now.
        self._maybe_reset_cost_tracker(user_id)
        self.cost_tracker[user_id] += already_spent
        self.cost_reset_time[user_id] = datetime.now(UTC)
        logger.info(
            "Hydrated daily cost from storage",
            user_id=user_id,
            already_spent=already_spent,
        )

    def _sweep_stale_reservations(self, user_id: int) -> None:
        """Release this user's holds that were never settled (caller bug)."""
        now = datetime.now(UTC)
        stale = [
            rid
            for rid in self.user_reservations.get(user_id, set())
            if rid in self.reservations
            and now - self.reservations[rid].created_at > self.RESERVATION_MAX_AGE
        ]
        for rid in stale:
            reservation = self.reservations.pop(rid)
            self.user_reservations[user_id].discard(rid)
            self.cost_tracker[user_id] = max(
                0.0, self.cost_tracker[user_id] - reservation.amount
            )
            logger.warning(
                "Cost reservation expired unsettled",
                user_id=user_id,
                reservation_id=rid,
                amount=reservation.amount,
                age_seconds=(now - reservation.created_at).total_seconds(),
            )

    def _drop_user_reservations(self, user_id: int) -> None:
        """Forget a user's holds entirely (cleanup / admin reset)."""
        for rid in self.user_reservations.pop(user_id, set()):
            self.reservations.pop(rid, None)

    def _check_request_rate(
        self, user_id: int, tokens: int
    ) -> Tuple[bool, Optional[str]]:
        """Check request rate limit without consuming tokens.

        Consumption happens later via ``_consume_request_tokens`` only
        after the cost check has also succeeded — otherwise a cost-limit
        rejection would still bill the user a rate-limit token.
        """
        bucket = self._get_or_create_bucket(user_id)

        if bucket.can_consume(tokens):
            return True, None

        wait_time = bucket.get_wait_time(tokens)
        status = bucket.get_status()

        message = (
            f"Rate limit exceeded. Please wait {wait_time:.1f} seconds "
            f"before making more requests. "
            f"Bucket: {status['tokens']:.1f}/{status['capacity']} tokens available."
        )
        return False, message

    def _check_cost_limit(
        self, user_id: int, cost: float
    ) -> Tuple[bool, Optional[str]]:
        """Check cost-based limit."""
        # Reset cost tracker if enough time has passed
        self._maybe_reset_cost_tracker(user_id)

        current_cost = self.cost_tracker[user_id]
        if current_cost + cost > self.config.claude_max_cost_per_user:
            remaining = max(0, self.config.claude_max_cost_per_user - current_cost)
            message = (
                f"Cost limit exceeded. Remaining budget: ${remaining:.2f}. "
                f"Current usage: ${current_cost:.2f}/"
                f"${self.config.claude_max_cost_per_user:.2f}"
            )
            return False, message

        return True, None

    def _consume_request_tokens(self, user_id: int, tokens: int) -> None:
        """Consume tokens from request bucket."""
        bucket = self._get_or_create_bucket(user_id)
        bucket.consume(tokens)

    def _track_cost(self, user_id: int, cost: float) -> None:
        """Track cost usage for user."""
        self.cost_tracker[user_id] += cost

        logger.debug(
            "Cost tracked",
            user_id=user_id,
            cost=cost,
            total_usage=self.cost_tracker[user_id],
        )

    def _get_or_create_bucket(self, user_id: int) -> RateLimitBucket:
        """Get or create rate limit bucket for user."""
        if user_id not in self.request_buckets:
            self.request_buckets[user_id] = RateLimitBucket(
                capacity=self.config.rate_limit_burst,
                tokens=self.config.rate_limit_burst,
                last_update=datetime.now(UTC),
                refill_rate=self.refill_rate,
            )
            logger.debug("Created rate limit bucket", user_id=user_id)

        return self.request_buckets[user_id]

    def _maybe_reset_cost_tracker(self, user_id: int) -> None:
        """Reset cost tracker if reset period has passed."""
        now = datetime.now(UTC)
        last_reset = self.cost_reset_time.get(user_id, now - timedelta(days=1))

        # Reset daily (configurable)
        reset_interval = timedelta(hours=24)
        if now - last_reset >= reset_interval:
            old_cost = self.cost_tracker[user_id]
            self.cost_tracker[user_id] = 0
            self.cost_reset_time[user_id] = now
            # Holds charged before the reset are no longer in the (now zeroed)
            # tracker. Keep the reservations so a late settle still charges the
            # real cost, but zero their amounts so settling cannot back out
            # money that the fresh window never counted.
            for rid in self.user_reservations.get(user_id, set()):
                reservation = self.reservations.get(rid)
                if reservation is not None:
                    reservation.amount = 0.0

            if old_cost > 0:
                logger.info(
                    "Cost tracker reset",
                    user_id=user_id,
                    old_cost=old_cost,
                    reset_time=now.isoformat(),
                )

    def _effective_cost(self, user_id: int) -> Tuple[float, datetime]:
        """Compute current cost and reset time as if a due reset had run.

        Non-mutating counterpart of ``_maybe_reset_cost_tracker`` for the
        read path: it reports the cost the user *would* have after any due
        daily reset, plus the effective reset timestamp, without touching
        ``self.cost_tracker`` or ``self.cost_reset_time``.
        """
        now = datetime.now(UTC)
        last_reset = self.cost_reset_time.get(user_id, now - timedelta(days=1))
        reset_interval = timedelta(hours=24)
        if now - last_reset >= reset_interval:
            return 0.0, now
        return self.cost_tracker[user_id], self.cost_reset_time.get(user_id, now)

    async def record_actual_cost(
        self, user_id: int, cost: float, reservation_id: Optional[str] = None
    ) -> None:
        """Record a completed run's *actual* cost.

        Prefer ``reserve_cost`` + ``settle_reservation`` for anything that
        runs Claude: the reservation makes the hold and its release refer to
        one specific run.

        When *reservation_id* is given this delegates to
        ``settle_reservation``, so a zero cost still releases the hold. Without
        it the actual cost is simply added — correct, because ``check_rate_limit``
        no longer charges an unreconciled estimate that would need backing out.
        """
        if reservation_id is not None:
            await self.settle_reservation(reservation_id, cost)
            return

        if cost <= 0:
            return

        async with self.locks[user_id]:
            self._maybe_reset_cost_tracker(user_id)
            self._track_cost(user_id, cost)

    async def reset_user_limits(self, user_id: int) -> None:
        """Reset all limits for a user (admin function)."""
        async with self.locks[user_id]:
            # Reset cost tracking
            old_cost = self.cost_tracker[user_id]
            self.cost_tracker[user_id] = 0
            self.cost_reset_time[user_id] = datetime.now(UTC)
            self._drop_user_reservations(user_id)

            # Reset request bucket
            if user_id in self.request_buckets:
                self.request_buckets[user_id].tokens = self.request_buckets[
                    user_id
                ].capacity
                self.request_buckets[user_id].last_update = datetime.now(UTC)

            logger.info("User limits reset", user_id=user_id, old_cost=old_cost)

    def get_user_status(self, user_id: int) -> Dict[str, Any]:
        """Get current rate limit status for user.

        Pure read path: neither the bucket nor the cost tracker is
        mutated here (``get_status``/``_effective_cost`` compute values on
        the fly), so it is safe to call without holding ``self.locks``.
        """
        # Get request bucket status
        bucket = self._get_or_create_bucket(user_id)
        bucket_status = bucket.get_status()

        # Get cost status (computed without mutating the tracker)
        current_cost, effective_reset = self._effective_cost(user_id)
        cost_remaining = max(0, self.config.claude_max_cost_per_user - current_cost)
        # Portion of ``current`` that is an in-flight hold rather than spend.
        reserved = sum(
            self.reservations[rid].amount
            for rid in self.user_reservations.get(user_id, set())
            if rid in self.reservations
        )

        return {
            "request_bucket": bucket_status,
            "cost_usage": {
                "current": current_cost,
                "reserved": reserved,
                "limit": self.config.claude_max_cost_per_user,
                "remaining": cost_remaining,
                "utilization": current_cost / self.config.claude_max_cost_per_user,
            },
            "last_reset": effective_reset.isoformat(),
        }

    def get_global_status(self) -> Dict[str, Any]:
        """Get global rate limiter statistics."""
        return {
            "active_users": len(self.request_buckets),
            "total_cost_tracked": sum(self.cost_tracker.values()),
            "config": {
                "requests_per_window": self.config.rate_limit_requests,
                "window_seconds": self.config.rate_limit_window,
                "burst_capacity": self.config.rate_limit_burst,
                "max_cost_per_user": self.config.claude_max_cost_per_user,
                "refill_rate": self.refill_rate,
            },
        }

    async def cleanup_inactive_users(
        self, inactive_threshold: timedelta = timedelta(hours=24)
    ) -> int:
        """Clean up rate limit data for inactive users."""
        now = datetime.now(UTC)
        inactive_users = []

        # Find users with old buckets
        for user_id, bucket in self.request_buckets.items():
            if now - bucket.last_update > inactive_threshold:
                inactive_users.append(user_id)

        # Clean up data
        for user_id in inactive_users:
            self.request_buckets.pop(user_id, None)
            self.cost_tracker.pop(user_id, None)
            self.cost_reset_time.pop(user_id, None)
            self._drop_user_reservations(user_id)
            self.locks.pop(user_id, None)
            # Forget the hydration marker too: the tracker this user's spend
            # lived in is gone, so a later request must re-read it from storage.
            self._hydrated_users.discard(user_id)

        if inactive_users:
            logger.info(
                "Cleaned up inactive users",
                count=len(inactive_users),
                threshold_hours=inactive_threshold.total_seconds() / 3600,
            )

        return len(inactive_users)
