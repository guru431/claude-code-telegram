"""Selective-concurrency update processor for PTB.

Regular updates (messages, commands) process sequentially *per user* -- one at
a time for a given user, but different users run concurrently so one user's
long Claude run never blocks everyone else.
Priority callbacks (stop:*) bypass the queue and run immediately so they can
interrupt the currently-running handler.
"""

import asyncio
from typing import Any, Awaitable, Optional

from telegram import Update
from telegram.ext._baseupdateprocessor import BaseUpdateProcessor


class StopAwareUpdateProcessor(BaseUpdateProcessor):
    """Update processor that lets priority callbacks bypass sequential processing.

    PTB calls ``process_update(update, coroutine)`` for every incoming update.
    The base class holds a semaphore (max 256) then calls our
    ``do_process_update()``.

    For priority callbacks (``stop:*``): we just ``await coroutine`` -- runs
    immediately.
    For everything else: we acquire that user's lock first -- only one update
    per user runs at a time, while distinct users proceed concurrently.

    A stop callback arrives while a text handler holds the lock -> stop
    callback runs concurrently -> fires the ``asyncio.Event`` -> the watcher
    task inside ``execute_command()`` calls ``client.interrupt()`` -> Claude
    stops -> ``run_command()`` returns -> handler finishes -> lock released.

    Caveat — the priority bypass is not absolute. ``BaseUpdateProcessor``
    acquires a shared semaphore (size :attr:`_MAX_CONCURRENT`) *before*
    dispatching to ``do_process_update``, and regular updates waiting on
    their per-user lock keep holding their semaphore slot while they wait.
    If more than :attr:`_MAX_CONCURRENT` regular updates are queued at once,
    every slot is held by a waiter and a ``stop:`` callback would have to wait
    for a slot too. The limit is therefore set very high so this only matters
    under extreme flooding; with it sized so, the bypass holds in practice.
    Regular updates are still serialized one-at-a-time per user by their
    per-user lock regardless of the limit, so the high value does not increase
    real per-user concurrency — it only governs how many updates may sit
    queued.
    """

    _PRIORITY_PREFIXES = ("stop:",)
    # Sized far above any realistic backlog so a stop: callback is never starved
    # of a semaphore slot by regular updates queued on _sequential_lock.
    _MAX_CONCURRENT = 100_000
    # Above this many tracked per-user locks, opportunistically drop the ones
    # that are provably idle (not currently held) so the map stays bounded under
    # traffic from many distinct ids — including rejected senders, since this
    # processor runs before auth.
    _MAX_USER_LOCKS = 10_000

    def __init__(self, max_concurrent: Optional[int] = None) -> None:
        # max_concurrent is overridable mainly so tests can exercise the
        # saturation edge with a small slot pool.
        super().__init__(max_concurrent_updates=max_concurrent or self._MAX_CONCURRENT)
        # One lock per user id so distinct users run concurrently; a single
        # fallback lock serializes updates that carry no effective_user.
        self._user_locks: dict[int, asyncio.Lock] = {}
        # In-flight updates per user id, counted across the whole
        # acquire/release window. Eviction consults this instead of
        # Lock.locked() — see _evict_idle_locks.
        self._lock_refs: dict[int, int] = {}
        self._fallback_lock = asyncio.Lock()

    @classmethod
    def _is_priority_callback(cls, update: object) -> bool:
        """Return True if the update is a priority callback query."""
        if not isinstance(update, Update):
            return False
        cb = update.callback_query
        return (
            cb is not None
            and cb.data is not None
            and cb.data.startswith(cls._PRIORITY_PREFIXES)
        )

    async def do_process_update(
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        """Process an update, applying sequential lock for non-priority updates."""
        if self._is_priority_callback(update):
            # Run immediately -- no sequential lock
            await coroutine
        else:
            # One at a time per user; distinct users run concurrently.
            user_key, lock = self._acquire_lock_slot(update)
            try:
                async with lock:
                    await coroutine
            finally:
                self._release_lock_slot(user_key)

    def _acquire_lock_slot(self, update: object) -> tuple[Optional[int], asyncio.Lock]:
        """Return ``(user_key, lock)`` and register an in-flight reference.

        The reference must be dropped with :meth:`_release_lock_slot` once the
        update is done with the lock. Updates without an effective_user share a
        single fallback lock and register no reference (``user_key`` is None).
        """
        if isinstance(update, Update) and update.effective_user:
            user_id = update.effective_user.id
            lock = self._user_locks.get(user_id)
            if lock is None:
                if len(self._user_locks) >= self._MAX_USER_LOCKS:
                    self._evict_idle_locks()
                    # If every tracked lock is currently referenced, eviction
                    # freed nothing; fall back to the shared lock rather than
                    # grow the map past the cap. This hard-bounds _user_locks at
                    # _MAX_USER_LOCKS even under flood.
                    if len(self._user_locks) >= self._MAX_USER_LOCKS:
                        return None, self._fallback_lock
                lock = asyncio.Lock()
                self._user_locks[user_id] = lock
            self._lock_refs[user_id] = self._lock_refs.get(user_id, 0) + 1
            return user_id, lock
        return None, self._fallback_lock

    def _release_lock_slot(self, user_key: Optional[int]) -> None:
        """Drop this update's reference to its per-user lock."""
        if user_key is None:
            return
        remaining = self._lock_refs.get(user_key, 0) - 1
        if remaining > 0:
            self._lock_refs[user_key] = remaining
        else:
            self._lock_refs.pop(user_key, None)

    def _evict_idle_locks(self) -> None:
        """Drop per-user locks that no in-flight update is referencing.

        ``asyncio.Lock.locked()`` is NOT a safe idleness test here: between the
        owner's ``release()`` and the woken waiter's ``acquire()`` the lock
        briefly reads unlocked even though a waiter is already queued on it.
        Evicting there lets the next update for that user create a *second*
        lock, so two handlers for the same user enter the critical section at
        once. The reference count spans the whole acquire/release window,
        including that gap. Runs synchronously (no await) so the map is not
        mutated concurrently mid-iteration.
        """
        idle = [uid for uid in self._user_locks if not self._lock_refs.get(uid)]
        for uid in idle:
            self._user_locks.pop(uid, None)

    async def initialize(self) -> None:
        """Initialize the processor (no-op)."""

    async def shutdown(self) -> None:
        """Shutdown the processor (no-op)."""
