"""Stream wrapper tests.

Every test runs on :class:`_VirtualClockLoop`, whose clock moves only when
every task is waiting, and then straight to the next timer. Intervals and
timeouts elapse in the order a test sets up, never because the runner was
slow: on a loaded Windows runner, whose CPython 3.12 loop clock ticks in
15.6 ms steps, a 20 ms heartbeat used to fall due before the upstream's first
event had been queued.
"""

import asyncio
import selectors
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import Any

import pytest

from agentos.engine.stream_wrappers import heartbeat_stream, idle_timeout_stream
from agentos.engine.types import AgentEvent, RunHeartbeatEvent, TextDeltaEvent


class _VirtualSelector(selectors.DefaultSelector):
    """Polls for I/O without blocking; a wait for a timer moves the clock instead."""

    def __init__(self, loop: "_VirtualClockLoop") -> None:
        super().__init__()
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = super().select(0)
        if not ready:
            if timeout is None:
                raise RuntimeError("every task is waiting and no timer is scheduled")
            self._loop.now += timeout
        return ready


class _VirtualClockLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock stands still while any task can run."""

    def __init__(self) -> None:
        self.now = 0.0
        super().__init__(selector=_VirtualSelector(self))
        # The loop runs every timer due within one clock tick of now. Pin the
        # tick, or a virtual run fires timers early on Windows' 3.12 clock.
        self._clock_resolution = 1e-9

    def time(self) -> float:
        return self.now


def _run_on_virtual_clock[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    loop = _VirtualClockLoop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def test_heartbeat_stream_emits_while_upstream_is_quiet() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def source():
            await release.wait()
            yield TextDeltaEvent(text="done")

        wrapped = heartbeat_stream(source(), interval=0.02)

        heartbeat = await anext(wrapped)
        assert isinstance(heartbeat, RunHeartbeatEvent)
        assert asyncio.get_running_loop().time() == pytest.approx(0.02)

        release.set()
        done = await anext(wrapped)
        assert isinstance(done, TextDeltaEvent)
        assert done.text == "done"
        with pytest.raises(StopAsyncIteration):
            await anext(wrapped)

    _run_on_virtual_clock(scenario)


def test_heartbeat_stream_preserves_upstream_idle_timeout() -> None:
    async def scenario() -> list[AgentEvent]:
        async def source():
            await asyncio.Event().wait()  # upstream never produces
            yield TextDeltaEvent(text="never")

        wrapped = heartbeat_stream(idle_timeout_stream(source(), timeout=0.05), interval=0.02)
        events: list[AgentEvent] = []
        with pytest.raises(TimeoutError, match="idle for more than 0.05s"):
            async for event in wrapped:
                events.append(event)
        return events

    # Heartbeats at 0.02 and 0.04; the idle timeout at 0.05 ends the stream.
    assert [event.kind for event in _run_on_virtual_clock(scenario)] == ["run_heartbeat"] * 2


def test_heartbeat_stream_preserves_upstream_generator_context() -> None:
    owner: ContextVar[str | None] = ContextVar("owner", default=None)

    async def scenario() -> list[AgentEvent]:
        async def source():
            token = owner.set("turn")
            try:
                yield TextDeltaEvent(text="first")
                yield TextDeltaEvent(text="second")
            finally:
                owner.reset(token)

        return [event async for event in heartbeat_stream(source(), interval=0.02)]

    # Upstream is never quiet, so no heartbeat falls due.
    events = _run_on_virtual_clock(scenario)
    assert [event.kind for event in events] == ["text_delta", "text_delta"]
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "first",
        "second",
    ]


def test_heartbeat_due_with_the_first_event_keeps_upstream_events_in_order() -> None:
    """The interleaving the Windows runner hit: the interval runs out in the
    same loop tick the upstream produces its first event. The heartbeat goes
    out first, and the upstream events follow intact, in order, from the
    generator's own context."""
    owner: ContextVar[str | None] = ContextVar("owner", default=None)

    async def scenario() -> list[AgentEvent]:
        async def source():
            token = owner.set("turn")
            try:
                await asyncio.sleep(0.02)
                yield TextDeltaEvent(text="first")
                yield TextDeltaEvent(text="second")
            finally:
                owner.reset(token)

        return [event async for event in heartbeat_stream(source(), interval=0.02)]

    events = _run_on_virtual_clock(scenario)
    assert [event.kind for event in events] == ["run_heartbeat", "text_delta", "text_delta"]
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "first",
        "second",
    ]


def test_upstream_run_heartbeat_resets_idle_timeout_stream() -> None:
    async def scenario() -> list[AgentEvent]:
        async def source():
            for _ in range(5):
                await asyncio.sleep(0.03)
                yield RunHeartbeatEvent(phase="tool", message="tool still running")
            await asyncio.sleep(0.03)
            yield TextDeltaEvent(text="done")

        # 0.18 s in all, but never 0.15 s without an event.
        return [event async for event in idle_timeout_stream(source(), timeout=0.15)]

    events = _run_on_virtual_clock(scenario)
    assert [event.kind for event in events] == ["run_heartbeat"] * 5 + ["text_delta"]
