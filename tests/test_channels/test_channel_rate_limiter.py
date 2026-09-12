"""``RateLimiter`` token-bucket accounting at the utility boundary.

The limiter guards every Discord REST call, so the contract that matters is
the sustained rate it admits under contention — not just that a waiter sleeps.
Time is virtualised rather than measured: a wall-clock assertion on a 0.1s
interval is exactly the shape that turns into a Windows-only flake when the
system clock ticks at ~15.6ms.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.channels import _util
from agentos.channels._util import RateLimiter


class _VirtualClock:
    """A monotonic clock that only advances when a sleeper asks it to."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _VirtualClock:
    virtual = _VirtualClock()
    monkeypatch.setattr(_util, "time", SimpleNamespace(monotonic=virtual.monotonic))
    monkeypatch.setattr(_util, "asyncio", SimpleNamespace(sleep=virtual.sleep))
    return virtual


async def test_acquire_holds_the_configured_sustained_rate(clock: _VirtualClock) -> None:
    """Every acquisition past the burst costs a full refill interval.

    Regression: the waiter used to leave ``_last_refill`` at its pre-sleep
    reading, so the interval it had just slept through was credited a second
    time to the next caller and the bucket sustained ~2x ``refill_rate``.
    """
    limiter = RateLimiter(max_tokens=1, refill_rate=10.0)
    start = clock.now

    for _ in range(5):
        await limiter.acquire()

    # One token was in the bucket; the other four each cost 1/10s.
    assert clock.now - start == pytest.approx(0.4)


async def test_initial_burst_is_admitted_without_waiting(clock: _VirtualClock) -> None:
    limiter = RateLimiter(max_tokens=3, refill_rate=10.0)
    start = clock.now

    for _ in range(3):
        await limiter.acquire()

    assert clock.now == start


async def test_idle_refill_is_capped_at_max_tokens(clock: _VirtualClock) -> None:
    limiter = RateLimiter(max_tokens=3, refill_rate=10.0)
    clock.now += 100.0  # far more refill than the bucket can hold

    for _ in range(3):
        await limiter.acquire()
    idle_end = clock.now

    await limiter.acquire()

    assert clock.now - idle_end == pytest.approx(0.1)
