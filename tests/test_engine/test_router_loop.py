"""``RouterLoop``: router steps run off the caller's loop, each in isolation (#3819)."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Iterator

import pytest

from agentos.engine.router_loop import RouterLoop


@pytest.fixture
def router() -> Iterator[RouterLoop]:
    loop = RouterLoop(name="test-router")
    yield loop
    loop.close()


@pytest.mark.asyncio
async def test_steps_run_off_the_callers_loop_each_on_its_own_thread(router: RouterLoop) -> None:
    async def which_thread() -> int:
        return threading.get_ident()

    first, second = await asyncio.gather(router.run(which_thread()), router.run(which_thread()))

    assert threading.get_ident() not in (first, second)


@pytest.mark.asyncio
async def test_a_blocking_step_does_not_hold_up_another_turns_step(router: RouterLoop) -> None:
    # Synchronous router work (ONNX inference, a model load) in one turn must
    # not delay a concurrent turn's router: on a shared loop the second step
    # waited behind the first and timed out, failing open to the unrouted model.
    async def blocking() -> str:
        time.sleep(0.5)
        return "slow"

    async def instant() -> str:
        return "fast"

    slow = router.run(blocking())
    await asyncio.sleep(0.02)
    started = time.monotonic()
    assert await asyncio.wait_for(router.run(instant()), timeout=0.3) == "fast"
    assert time.monotonic() - started < 0.3
    assert await slow == "slow"


@pytest.mark.asyncio
async def test_a_step_error_reaches_the_caller(router: RouterLoop) -> None:
    async def broken() -> None:
        raise ValueError("router broke")

    with pytest.raises(ValueError, match="router broke"):
        await asyncio.wait_for(router.run(broken()), timeout=5)


@pytest.mark.asyncio
async def test_the_step_sees_the_callers_context(router: RouterLoop) -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("router_test_var", default="unset")
    var.set("caller")

    async def read() -> str:
        return var.get()

    assert await router.run(read()) == "caller"


@pytest.mark.asyncio
async def test_a_blocking_step_still_times_out_for_the_caller(router: RouterLoop) -> None:
    async def blocking() -> str:
        time.sleep(0.2)
        return "late"

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(router.run(blocking()), timeout=0.05)

    assert time.monotonic() - started < 0.15


@pytest.mark.asyncio
async def test_a_timeout_cancels_the_step_on_the_router_loop(router: RouterLoop) -> None:
    cancelled = threading.Event()

    async def slow() -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(router.run(slow()), timeout=0.05)

    assert await asyncio.to_thread(cancelled.wait, 2)


@pytest.mark.asyncio
async def test_steps_still_run_after_close(router: RouterLoop) -> None:
    async def value() -> int:
        return 1

    assert await router.run(value()) == 1
    router.close()
    assert await router.run(value()) == 1


@pytest.mark.asyncio
async def test_close_cancels_a_running_step(router: RouterLoop) -> None:
    cancelled = threading.Event()
    started = threading.Event()

    async def slow() -> None:
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    future = router.run(slow())
    assert await asyncio.to_thread(started.wait, 2)
    router.close()

    assert await asyncio.to_thread(cancelled.wait, 2)
    with pytest.raises(asyncio.CancelledError):
        await future
