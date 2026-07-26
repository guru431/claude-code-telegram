"""Tests for StopAwareUpdateProcessor.

Covers:
- Stop callbacks bypass the per-user lock (run immediately)
- Regular updates from the same user are serialized (only one at a time)
- Regular updates from different users run concurrently
- Non-stop callbacks (e.g. cd:) go through the per-user lock
"""

import asyncio
from unittest.mock import MagicMock

from telegram import CallbackQuery, Update

from src.bot.update_processor import StopAwareUpdateProcessor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update(callback_data: str | None = None, user_id: int | None = 1) -> Update:
    """Build a minimal Update mock with optional callback_query data.

    ``user_id`` controls which per-user lock the update maps to; pass ``None``
    to simulate an update with no effective_user (uses the fallback lock).
    """
    update = MagicMock(spec=Update)
    if callback_data is not None:
        cb = MagicMock(spec=CallbackQuery)
        cb.data = callback_data
        update.callback_query = cb
    else:
        update.callback_query = None
    if user_id is None:
        update.effective_user = None
    else:
        user = MagicMock()
        user.id = user_id
        update.effective_user = user
    return update


# ---------------------------------------------------------------------------
# _is_priority_callback
# ---------------------------------------------------------------------------


class TestIsPriorityCallback:
    def test_stop_callback_detected(self):
        update = _make_update("stop:123")
        assert StopAwareUpdateProcessor._is_priority_callback(update) is True

    def test_cd_callback_not_priority(self):
        update = _make_update("cd:my_project")
        assert StopAwareUpdateProcessor._is_priority_callback(update) is False

    def test_no_callback_query(self):
        update = _make_update(None)
        assert StopAwareUpdateProcessor._is_priority_callback(update) is False

    def test_non_update_object(self):
        assert StopAwareUpdateProcessor._is_priority_callback("not an update") is False

    def test_callback_with_none_data(self):
        update = MagicMock(spec=Update)
        cb = MagicMock(spec=CallbackQuery)
        cb.data = None
        update.callback_query = cb
        assert StopAwareUpdateProcessor._is_priority_callback(update) is False


# ---------------------------------------------------------------------------
# do_process_update — concurrency tests
# ---------------------------------------------------------------------------


class TestStopCallbackBypassesLock:
    async def test_stop_callback_runs_while_lock_held(self):
        """A stop callback runs immediately even when sequential lock is held."""
        processor = StopAwareUpdateProcessor()

        execution_order: list[str] = []
        lock_acquired = asyncio.Event()
        stop_done = asyncio.Event()

        async def slow_coroutine():
            execution_order.append("regular_start")
            lock_acquired.set()
            # Wait for the stop callback to finish
            await stop_done.wait()
            execution_order.append("regular_end")

        async def stop_coroutine():
            execution_order.append("stop_start")
            execution_order.append("stop_end")
            stop_done.set()

        regular_update = _make_update(None)
        stop_update = _make_update("stop:42")

        # Start the regular update (acquires lock)
        regular_task = asyncio.create_task(
            processor.do_process_update(regular_update, slow_coroutine())
        )

        # Wait for the regular update to hold the lock
        await lock_acquired.wait()

        # Now fire the stop callback — should run immediately
        stop_task = asyncio.create_task(
            processor.do_process_update(stop_update, stop_coroutine())
        )

        await asyncio.gather(regular_task, stop_task)

        # Stop ran WHILE regular was still in progress
        assert execution_order == [
            "regular_start",
            "stop_start",
            "stop_end",
            "regular_end",
        ]


class TestRegularUpdatesSequential:
    async def test_two_regular_updates_same_user_do_not_overlap(self):
        """Two regular updates from the same user are serialized."""
        processor = StopAwareUpdateProcessor()

        execution_log: list[str] = []

        async def coroutine_a():
            execution_log.append("a_start")
            await asyncio.sleep(0.05)
            execution_log.append("a_end")

        async def coroutine_b():
            execution_log.append("b_start")
            await asyncio.sleep(0.05)
            execution_log.append("b_end")

        update_a = _make_update(None, user_id=7)
        update_b = _make_update(None, user_id=7)

        task_a = asyncio.create_task(
            processor.do_process_update(update_a, coroutine_a())
        )
        # Yield so task_a starts and acquires the lock
        await asyncio.sleep(0)

        task_b = asyncio.create_task(
            processor.do_process_update(update_b, coroutine_b())
        )

        await asyncio.gather(task_a, task_b)

        # b should not start until a has finished
        assert execution_log == ["a_start", "a_end", "b_start", "b_end"]

    async def test_different_users_run_concurrently(self):
        """Updates from different users are not serialized against each other."""
        processor = StopAwareUpdateProcessor()

        execution_log: list[str] = []
        a_started = asyncio.Event()

        async def coroutine_a():
            execution_log.append("a_start")
            a_started.set()
            await asyncio.sleep(0.05)
            execution_log.append("a_end")

        async def coroutine_b():
            execution_log.append("b_start")
            execution_log.append("b_end")

        update_a = _make_update(None, user_id=1)
        update_b = _make_update(None, user_id=2)

        task_a = asyncio.create_task(
            processor.do_process_update(update_a, coroutine_a())
        )
        await a_started.wait()

        task_b = asyncio.create_task(
            processor.do_process_update(update_b, coroutine_b())
        )

        await asyncio.gather(task_a, task_b)

        # b ran while a was still in progress (different user -> different lock).
        assert execution_log == ["a_start", "b_start", "b_end", "a_end"]

    async def test_updates_without_user_share_fallback_lock(self):
        """Updates lacking effective_user serialize on the fallback lock."""
        processor = StopAwareUpdateProcessor()

        execution_log: list[str] = []

        async def coroutine_a():
            execution_log.append("a_start")
            await asyncio.sleep(0.05)
            execution_log.append("a_end")

        async def coroutine_b():
            execution_log.append("b_start")
            execution_log.append("b_end")

        update_a = _make_update(None, user_id=None)
        update_b = _make_update(None, user_id=None)

        task_a = asyncio.create_task(
            processor.do_process_update(update_a, coroutine_a())
        )
        await asyncio.sleep(0)

        task_b = asyncio.create_task(
            processor.do_process_update(update_b, coroutine_b())
        )

        await asyncio.gather(task_a, task_b)

        assert execution_log == ["a_start", "a_end", "b_start", "b_end"]


class TestNonStopCallbackSequential:
    async def test_cd_callback_goes_through_sequential_lock(self):
        """Non-stop callbacks (cd:*) are treated as regular updates."""
        processor = StopAwareUpdateProcessor()

        execution_log: list[str] = []

        async def regular_coroutine():
            execution_log.append("regular_start")
            await asyncio.sleep(0.05)
            execution_log.append("regular_end")

        async def cd_coroutine():
            execution_log.append("cd_start")
            execution_log.append("cd_end")

        regular_update = _make_update(None)
        cd_update = _make_update("cd:my_project")

        task_regular = asyncio.create_task(
            processor.do_process_update(regular_update, regular_coroutine())
        )
        await asyncio.sleep(0)

        task_cd = asyncio.create_task(
            processor.do_process_update(cd_update, cd_coroutine())
        )

        await asyncio.gather(task_regular, task_cd)

        # cd callback waited for regular to finish
        assert execution_log == [
            "regular_start",
            "regular_end",
            "cd_start",
            "cd_end",
        ]


class TestSemaphoreSaturation:
    """Documents the bypass limit: regular updates waiting on their per-user
    lock keep holding their semaphore slot, so when every slot is occupied a
    stop: callback is starved until one frees. This is why _MAX_CONCURRENT is
    sized far above any realistic backlog in production.
    """

    async def test_stop_starved_when_all_slots_held(self):
        # Tiny slot pool so we can saturate it deterministically.
        processor = StopAwareUpdateProcessor(max_concurrent=2)

        release = asyncio.Event()

        async def blocker():
            await release.wait()

        # r1 takes a slot + the sequential lock and runs (blocked on release).
        # r2 takes the other slot and waits on the lock (still holding its slot).
        r1 = asyncio.create_task(
            processor.process_update(_make_update(None), blocker())
        )
        r2 = asyncio.create_task(
            processor.process_update(_make_update(None), blocker())
        )
        await asyncio.sleep(0.02)  # let both grab their slots

        stop_ran = asyncio.Event()

        async def stop_coro():
            stop_ran.set()

        stop_task = asyncio.create_task(
            processor.process_update(_make_update("stop:1"), stop_coro())
        )
        await asyncio.sleep(0.02)

        # Both slots are held (r1 running, r2 waiting on lock) -> stop is starved.
        assert not stop_ran.is_set()

        # Releasing frees the slots; the stop callback then runs.
        release.set()
        await asyncio.gather(r1, r2, stop_task)
        assert stop_ran.is_set()

    async def test_stop_bypasses_with_headroom(self):
        # One regular update holding the lock, but spare slots remain ->
        # the stop callback bypasses immediately.
        processor = StopAwareUpdateProcessor(max_concurrent=8)

        release = asyncio.Event()

        async def blocker():
            await release.wait()

        r1 = asyncio.create_task(
            processor.process_update(_make_update(None), blocker())
        )
        await asyncio.sleep(0.02)

        stop_ran = asyncio.Event()

        async def stop_coro():
            stop_ran.set()

        stop_task = asyncio.create_task(
            processor.process_update(_make_update("stop:1"), stop_coro())
        )
        await asyncio.sleep(0.02)

        # Spare semaphore slots -> stop ran without waiting for r1.
        assert stop_ran.is_set()

        release.set()
        await asyncio.gather(r1, stop_task)


class TestInitializeShutdown:
    async def test_initialize_and_shutdown_are_noop(self):
        """initialize() and shutdown() should not raise."""
        processor = StopAwareUpdateProcessor()
        await processor.initialize()
        await processor.shutdown()


class TestLockEviction:
    """Eviction must not split one user across two locks.

    Regression: eviction used Lock.locked(), which reads False in the gap
    between the owner's release and the woken waiter's acquire. Dropping the
    lock there let the next update for that user create a second one, so two
    handlers for the same user ran concurrently.
    """

    async def test_lock_with_waiter_is_not_evicted(self):
        processor = StopAwareUpdateProcessor()
        processor._MAX_USER_LOCKS = 1

        overlap: list[str] = []
        first_holding = asyncio.Event()
        release_first = asyncio.Event()

        async def first():
            overlap.append("first_start")
            first_holding.set()
            await release_first.wait()
            overlap.append("first_end")

        async def second():
            overlap.append("second_start")
            overlap.append("second_end")

        update_a = _make_update(None, user_id=42)
        update_b = _make_update(None, user_id=42)

        task_a = asyncio.create_task(processor.do_process_update(update_a, first()))
        await first_holding.wait()

        # Queues on the same lock while the owner still holds it.
        task_b = asyncio.create_task(processor.do_process_update(update_b, second()))
        await asyncio.sleep(0)

        # A third user id arrives at the cap and triggers eviction while user 42
        # has both an owner and a waiter.
        processor._evict_idle_locks()
        assert 42 in processor._user_locks

        release_first.set()
        await asyncio.gather(task_a, task_b)

        assert overlap == ["first_start", "first_end", "second_start", "second_end"]

    async def test_idle_lock_is_evicted_after_release(self):
        processor = StopAwareUpdateProcessor()

        async def noop():
            return None

        await processor.do_process_update(_make_update(None, user_id=9), noop())
        assert 9 in processor._user_locks

        processor._evict_idle_locks()

        assert processor._user_locks == {}
        assert processor._lock_refs == {}

    async def test_falls_back_to_shared_lock_when_cap_is_saturated(self):
        processor = StopAwareUpdateProcessor()
        processor._MAX_USER_LOCKS = 1

        holding = asyncio.Event()
        release = asyncio.Event()

        async def blocker():
            holding.set()
            await release.wait()

        task = asyncio.create_task(
            processor.do_process_update(_make_update(None, user_id=1), blocker())
        )
        await holding.wait()

        # User 1's lock is referenced, so eviction frees nothing and a new user
        # must reuse the fallback lock rather than grow the map past the cap.
        key, lock = processor._acquire_lock_slot(_make_update(None, user_id=2))
        assert key is None
        assert lock is processor._fallback_lock
        assert len(processor._user_locks) == 1

        release.set()
        await task
