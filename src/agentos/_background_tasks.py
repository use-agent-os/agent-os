"""Background task retention helpers.

Python's asyncio only retains weak references to ``asyncio.Task`` objects. A
task that is not referenced anywhere else can be garbage collected mid-flight,
even before it completes:

    https://docs.python.org/3/library/asyncio-task.html#creating-tasks
    > Save a reference to the result of this function, to avoid a task
    > disappearing mid-execution. The event loop only keeps weak references
    > to tasks.

``BackgroundTaskTracker`` retains strong references for the lifetime of a
component, registers a done-callback to drop completed tasks automatically,
and offers a ``cancel_and_await`` shutdown hook so components can drain
in-flight work before closing.

The tracker is intentionally a small, allocation-light class. It does not own
the event loop or expose coroutines to the caller; callers drive their own
asyncio scheduler. The class is safe to share across async tasks because the
underlying set operations are atomic in CPython and the done-callback runs in
the loop's ``call_soon`` queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class BackgroundTaskTracker:
    """Retain strong references to background ``asyncio.Task`` objects.

    Usage::

        tracker = BackgroundTaskTracker()

        async def _do_work():
            ...

        # Spawn a tracked task:
        tracker.create(_do_work(), name="do-work")

        # On shutdown, cancel and await remaining tasks:
        await tracker.cancel_and_await(timeout=5.0)

    The tracker is not thread-safe. It is intended for use within a single
    asyncio event loop.
    """

    __slots__ = ("_tasks", "_label")

    def __init__(self, *, label: str = "background") -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._label = label

    def __len__(self) -> int:
        return len(self._tasks)

    def __contains__(self, task: object) -> bool:
        return task in self._tasks

    @property
    def pending(self) -> tuple[asyncio.Task[Any], ...]:
        """Snapshot of currently tracked, not-yet-completed tasks."""
        return tuple(t for t in self._tasks if not t.done())

    def create(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Create a tracked task and register a done-callback to discard it.

        Returns the task so callers can ``await`` it directly when needed
        (e.g. when awaiting with a timeout in shutdown paths). The task is
        also retained in the tracker so it cannot be garbage collected while
        it is in flight.
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        # ``add_done_callback`` runs synchronously after the task transitions
        # to a finished state, so the discard happens on the same loop without
        # a yield point. ``discard`` (vs ``remove``) is intentional: the task
        # may already be absent if the tracker was reset between scheduling
        # and completion.
        task.add_done_callback(self._discard)
        return task

    def add(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Register an externally-created task for retention.

        Useful when a caller already needs the task handle (e.g. to read
        ``task.result()`` later) and just wants the tracker to keep it alive.
        """
        self._tasks.add(task)
        task.add_done_callback(self._discard)
        return task

    def _discard(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)

    def discard(self, task: asyncio.Task[Any]) -> None:
        """Manually drop a task from the tracker (e.g. on component reset).

        Equivalent to the implicit done-callback, but callable from user
        code (e.g. tests that want to assert reference cleanup without
        awaiting task completion).
        """
        self._tasks.discard(task)

    async def cancel_and_await(
        self,
        *,
        timeout: float = 5.0,
    ) -> tuple[asyncio.Task[Any], ...]:
        """Cancel all tracked tasks and await their completion.

        Tasks already in a finished state are skipped (they have already
        had their done-callback fire and been discarded). Tasks that do not
        finish cancelling within ``timeout`` seconds are left to the event
        loop; their references remain in the tracker so the caller can
        decide whether to escalate (e.g. log a warning).

        Returns the snapshot of tasks that were awaited. The returned tuple
        is empty when the tracker was empty.
        """
        # Snapshot first: the done-callback runs synchronously and would
        # mutate the underlying set during iteration otherwise.
        snapshot = [t for t in self._tasks if not t.done()]
        if not snapshot:
            return ()

        for task in snapshot:
            if not task.done():
                task.cancel()

        try:
            await asyncio.wait_for(
                asyncio.shield(
                    asyncio.gather(*snapshot, return_exceptions=True),
                ),
                timeout=timeout,
            )
        except TimeoutError:
            # Leave the un-cancelled tasks in the tracker. They are
            # cancelled but may still be running their cleanup; the caller
            # can choose to escalate or accept the leak.
            pass
        return tuple(snapshot)

    def drain(self) -> tuple[asyncio.Task[Any], ...]:
        """Forget all tracked tasks without cancelling them.

        Intended for tests and teardown paths where the caller has already
        finished or no longer cares about in-flight tasks. Returns the
        snapshot that was dropped so callers can log/inspect.
        """
        snapshot = tuple(self._tasks)
        self._tasks.clear()
        return snapshot
