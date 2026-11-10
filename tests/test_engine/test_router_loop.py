"""``RouterLoop``: router steps run off the caller's loop, each in isolation (#3819)."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Iterator

import pytest

from agentos.engine.router_loop import RouterLoop, RouterStepsStuckError, _Step


@pytest.fixture
def router() -> Iterator[RouterLoop]:
    yield RouterLoop(name="test-router")


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


def test_a_step_cancelled_before_its_thread_starts_never_runs() -> None:
    # The caller gave up before the step's loop existed: the coroutine is
    # closed unrun and the caller's future is cancelled, not settled.
    ran = threading.Event()

    async def step() -> str:
        ran.set()
        return "ran"

    async def scenario() -> asyncio.Future[str]:
        caller_loop = asyncio.get_running_loop()
        result: asyncio.Future[str] = caller_loop.create_future()
        router_step = _Step(step(), result)
        router_step.cancel()
        await asyncio.to_thread(router_step.serve, contextvars.copy_context())
        await asyncio.sleep(0)  # let the delivered cancel land
        return result

    result = asyncio.run(scenario())

    assert not ran.is_set()
    assert result.cancelled()


def test_a_step_finishing_after_its_callers_loop_closed_does_not_raise() -> None:
    # Gateway shutdown: the caller's loop is gone when the step delivers.
    caller_loop = asyncio.new_event_loop()
    result: asyncio.Future[int] = caller_loop.create_future()
    caller_loop.close()

    async def step() -> int:
        return 1

    _Step(step(), result).serve(contextvars.copy_context())

    assert not result.done()


@pytest.mark.asyncio
async def test_stuck_steps_are_capped_and_the_router_is_skipped_past_the_cap() -> None:
    # A router that hangs in synchronous code leaves its thread running after
    # each timeout; past the cap new steps are refused instead of piling up.
    router = RouterLoop(name="test-router", max_stuck_steps=2)
    release = threading.Event()

    async def hung() -> None:
        release.wait(5)

    try:
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(router.run(hung()), timeout=0.05)
        assert router.stuck_steps == 2

        refused_ran = threading.Event()

        async def refused() -> None:
            refused_ran.set()

        threads_before = threading.active_count()
        coro = refused()
        with pytest.raises(RouterStepsStuckError):
            router.run(coro)
        assert threading.active_count() == threads_before
        assert coro.cr_frame is None  # closed, never started
        assert not refused_ran.is_set()
    finally:
        release.set()

    # Once the stuck threads return, routing resumes.
    deadline = time.monotonic() + 2
    while router.stuck_steps and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert router.stuck_steps == 0

    async def value() -> int:
        return 7

    assert await asyncio.wait_for(router.run(value()), timeout=2) == 7


@pytest.mark.asyncio
async def test_concurrent_running_steps_do_not_count_as_stuck() -> None:
    # The cap counts abandoned steps only; turns whose router is merely
    # running concurrently are never refused.
    router = RouterLoop(name="test-router", max_stuck_steps=1)
    release = threading.Event()

    async def slow() -> str:
        await asyncio.to_thread(release.wait, 5)
        return "done"

    futures = [router.run(slow()) for _ in range(3)]
    assert router.stuck_steps == 0
    release.set()
    assert await asyncio.wait_for(asyncio.gather(*futures), timeout=5) == ["done"] * 3
