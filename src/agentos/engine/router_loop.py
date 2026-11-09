"""Isolated event loops for the Pilot Router step (#3819).

The router step may block (a Pilot model load, ONNX inference, or a
strategy that does synchronous I/O), yet a turn has to fail open when the
router exceeds ``routing_timeout_seconds``. So the step cannot run on the
gateway's loop: a blocked step would block the timeout too.

Each step runs on its own daemon thread and event loop. Steps of concurrent
turns must not share one: synchronous router work on a shared loop holds up
every other turn's step behind it, so their timeouts fire and they fail open
to the unrouted model although their own router was instant. A daemon thread
also never holds up gateway shutdown, unlike the default executor that
``asyncio.to_thread`` uses.

:meth:`RouterLoop.run` returns an awaitable future the caller bounds with
``asyncio.wait_for``. Cancelling that future (a timeout) cancels the step's
task on its own loop; synchronous code in the step cannot be interrupted,
but it only ever delays its own thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")


class _Step:
    """One router step on its own thread: hands its outcome to the caller."""

    def __init__(self, coro: Coroutine[Any, Any, Any], result: asyncio.Future[Any]) -> None:
        self._coro = coro
        self._result = result
        self._caller_loop = result.get_loop()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._cancelled = False

    def serve(self, context: contextvars.Context) -> None:
        try:
            with asyncio.Runner() as runner:
                value = runner.run(self._main(), context=context)
        except asyncio.CancelledError:
            self._deliver(self._result.cancel)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            # Bound now: ``exc`` is unset once this clause ends.
            self._deliver(functools.partial(self._result.set_exception, exc))
        else:
            self._deliver(functools.partial(self._result.set_result, value))

    def cancel(self) -> None:
        """Cancel the step's task on its own loop (the caller timed out)."""
        with self._lock:
            self._cancelled = True
            loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:  # the step's loop already closed
                pass

    async def _main(self) -> Any:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            cancelled = self._cancelled
        if cancelled:
            self._coro.close()
            raise asyncio.CancelledError
        return await self._coro

    def _deliver(self, settle: Callable[[], object]) -> None:
        def _settle() -> None:
            if not self._result.done():
                settle()

        try:
            self._caller_loop.call_soon_threadsafe(_settle)
        except RuntimeError:  # the caller's loop is gone (shutdown)
            pass


class RouterLoop:
    """Runs each router step on its own daemon thread and event loop."""

    def __init__(self, name: str = "agentos-router") -> None:
        self._name = name
        self._lock = threading.Lock()
        self._steps: set[_Step] = set()

    def run(self, coro: Coroutine[Any, Any, _T]) -> asyncio.Future[_T]:
        """Start ``coro`` on a fresh router thread; await the returned future."""
        result: asyncio.Future[_T] = asyncio.get_running_loop().create_future()
        step = _Step(coro, result)
        context = contextvars.copy_context()

        def _serve() -> None:
            try:
                step.serve(context)
            finally:
                with self._lock:
                    self._steps.discard(step)

        def _on_done(future: asyncio.Future[_T]) -> None:
            if future.cancelled():
                step.cancel()

        result.add_done_callback(_on_done)
        with self._lock:
            self._steps.add(step)
        threading.Thread(target=_serve, name=self._name, daemon=True).start()
        return result

    def close(self) -> None:
        """Cancel every step still running (tests, shutdown)."""
        with self._lock:
            steps = list(self._steps)
        for step in steps:
            step.cancel()


_ROUTER_LOOP = RouterLoop()


def router_loop() -> RouterLoop:
    """The process's router step runner."""
    return _ROUTER_LOOP


__all__ = ["RouterLoop", "router_loop"]
