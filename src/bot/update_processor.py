"""Selective-concurrency update processor for PTB.

Regular updates (messages, commands) process sequentially -- one at a time.
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
    For everything else: we acquire ``_sequential_lock`` first -- only one
    runs at a time.

    A stop callback arrives while a text handler holds the lock -> stop
    callback runs concurrently -> fires the ``asyncio.Event`` -> the watcher
    task inside ``execute_command()`` calls ``client.interrupt()`` -> Claude
    stops -> ``run_command()`` returns -> handler finishes -> lock released.

    Caveat — the priority bypass is not absolute. ``BaseUpdateProcessor``
    acquires a shared semaphore (size :attr:`_MAX_CONCURRENT`) *before*
    dispatching to ``do_process_update``, and regular updates waiting on
    ``_sequential_lock`` keep holding their semaphore slot while they wait.
    If more than :attr:`_MAX_CONCURRENT` regular updates are queued at once,
    every slot is held by a waiter and a ``stop:`` callback would have to wait
    for a slot too. The limit is therefore set very high so this only matters
    under extreme flooding; with it sized so, the bypass holds in practice.
    Regular updates are still serialized one-at-a-time by ``_sequential_lock``
    regardless of the limit, so the high value does not increase real
    concurrency — it only governs how many updates may sit queued.
    """

    _PRIORITY_PREFIXES = ("stop:",)
    # Sized far above any realistic backlog so a stop: callback is never starved
    # of a semaphore slot by regular updates queued on _sequential_lock.
    _MAX_CONCURRENT = 100_000

    def __init__(self, max_concurrent: Optional[int] = None) -> None:
        # max_concurrent is overridable mainly so tests can exercise the
        # saturation edge with a small slot pool.
        super().__init__(max_concurrent_updates=max_concurrent or self._MAX_CONCURRENT)
        self._sequential_lock = asyncio.Lock()

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
            # One at a time for everything else
            async with self._sequential_lock:
                await coroutine

    async def initialize(self) -> None:
        """Initialize the processor (no-op)."""

    async def shutdown(self) -> None:
        """Shutdown the processor (no-op)."""
