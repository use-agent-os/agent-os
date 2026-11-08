"""A long-lived event loop for the Pilot Router step (#3819).

The router step may block (a Pilot model load, ONNX inference, or a
strategy that does synchronous I/O), yet a turn has to fail open when the
router exceeds ``routing_timeout_seconds``. So the step cannot run on the
gateway's loop: a blocked step would block the timeout too.

It used to run as ``asyncio.to_thread(lambda: asyncio.run(step()))``, which
built and tore down a new event loop on a new worker thread for every turn.
:class:`RouterLoop` keeps one loop on one daemon thread instead, and
:meth:`RouterLoop.run` returns an awaitable future the caller can bound
with ``asyncio.wait_for``. Cancelling that future (a timeout) cancels the
step's task on the router loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")


class RouterLoop:
    """One daemon thread running an event loop for router steps."""

    def __init__(self, name: str = "agentos-router") -> None:
        self._name = name
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def run(self, coro: Coroutine[Any, Any, _T]) -> asyncio.Future[_T]:
        """Schedule ``coro`` on the router loop; await the returned future."""
        loop = self._ensure_loop()
        return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    def close(self) -> None:
        """Stop the loop and join its thread (tests, shutdown)."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _serve() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            thread = threading.Thread(target=_serve, name=self._name, daemon=True)
            thread.start()
            ready.wait()
            self._loop, self._thread = loop, thread
            return loop


_ROUTER_LOOP = RouterLoop()


def router_loop() -> RouterLoop:
    """The process's router loop."""
    return _ROUTER_LOOP


__all__ = ["RouterLoop", "router_loop"]
