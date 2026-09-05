"""Small asyncio helpers for test-friendly background task spawning."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


def create_background_task(coro: Coroutine[Any, Any, Any]) -> Any:
    """Create a background task and close unconsumed coroutines in tests."""
    task = asyncio.create_task(coro)
    frame = getattr(coro, "cr_frame", None)
    if frame is not None and not isinstance(task, asyncio.Task):
        coro.close()
    return task


_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def retain_task(
    task: asyncio.Task[Any],
    tasks: set[asyncio.Task[Any]] | None = None,
) -> asyncio.Task[Any]:
    """Retain a strong reference to *task* until completion to prevent premature GC.

    When *tasks* is omitted, the module-level retention set is used.
    """
    target = tasks if tasks is not None else _BACKGROUND_TASKS
    target.add(task)
    task.add_done_callback(target.discard)
    return task
