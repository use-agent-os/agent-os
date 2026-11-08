"""``RouterLoop``: one long-lived loop for router steps (#3819)."""

from __future__ import annotations

import asyncio
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
async def test_steps_reuse_one_thread_off_the_callers_loop(router: RouterLoop) -> None:
    async def which_thread() -> int:
        return threading.get_ident()

    first = await router.run(which_thread())
    second = await router.run(which_thread())

    assert first == second != threading.get_ident()


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
async def test_the_loop_restarts_after_close(router: RouterLoop) -> None:
    async def value() -> int:
        return 1

    assert await router.run(value()) == 1
    router.close()
    assert await router.run(value()) == 1
