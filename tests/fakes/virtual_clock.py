"""An event loop whose clock moves only when every task is waiting.

Run a scenario with :func:`run_on_virtual_clock`. The clock then stands still
while any task can run and jumps straight to the next timer once none can, so
intervals and timeouts elapse in the order a test sets them up, never because
the runner was slow, and a production-length timeout costs no real time.

Only for scenarios with no real I/O to wait on: the loop still polls its
selector, but never blocks on it, and a wait with no timer scheduled raises.
"""

from __future__ import annotations

import asyncio
import selectors
from collections.abc import Callable, Coroutine
from typing import Any


class _VirtualSelector(selectors.DefaultSelector):
    """Polls for I/O without blocking; a wait for a timer moves the clock instead."""

    def __init__(self, loop: VirtualClockLoop) -> None:
        super().__init__()
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = super().select(0)
        if not ready:
            if timeout is None:
                raise RuntimeError("every task is waiting and no timer is scheduled")
            self._loop.now += timeout
        return ready


class VirtualClockLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock stands still while any task can run."""

    def __init__(self) -> None:
        self.now = 0.0
        super().__init__(selector=_VirtualSelector(self))
        # The loop runs every timer due within one clock tick of now. Pin the
        # tick, or a virtual run fires timers early on Windows' 3.12 clock.
        self._clock_resolution = 1e-9

    def time(self) -> float:
        return self.now


def run_on_virtual_clock[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run *scenario* to completion on a fresh :class:`VirtualClockLoop`, starting at 0."""
    loop = VirtualClockLoop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
