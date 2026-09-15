"""``RateLimiter`` token-bucket accounting on a virtual clock.

The limiter throttles every Discord REST call, so admitting more than
``refill_rate`` per second is not a rounding error: it is exactly how the
HTTP 429s the limiter exists to prevent get produced. These tests drive the
bucket with a fake ``time.monotonic`` and a fake ``asyncio.sleep`` so the
arithmetic is exact and the suite never actually waits.
"""

from __future__ import annotations

import pytest

from agentos.channels import _util
from agentos.channels._util import RateLimiter


class _VirtualClock:
    """A monotonic clock that only advances when the limiter sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _VirtualClock:
    virtual = _VirtualClock()
    monkeypatch.setattr(_util.time, "monotonic", virtual.monotonic)
    monkeypatch.setattr(_util.asyncio, "sleep", virtual.sleep)
    return virtual


@pytest.mark.asyncio
async def test_sustained_rate_matches_refill_rate(clock: _VirtualClock) -> None:
    """Five acquisitions from a one-token bucket at 10/s must take 0.4s, not 0.2s.

    Before the fix the waiter left ``_last_refill`` at its pre-sleep reading,
    so the next caller re-credited the interval the sleep had already spent
    and every second acquisition came back for free.
    """
    limiter = RateLimiter(max_tokens=1, refill_rate=10.0)
    start = clock.now

    for _ in range(5):
        await limiter.acquire()

    assert clock.now - start == pytest.approx(0.4)
    assert clock.sleeps == pytest.approx([0.1, 0.1, 0.1, 0.1])


@pytest.mark.asyncio
async def test_waiter_does_not_leave_credit_for_the_next_caller(clock: _VirtualClock) -> None:
    limiter = RateLimiter(max_tokens=1, refill_rate=10.0)
    await limiter.acquire()  # drains the initial token
    await limiter.acquire()  # sleeps 0.1s for the next one

    await limiter.acquire()

    assert clock.sleeps == pytest.approx([0.1, 0.1])


@pytest.mark.asyncio
async def test_idle_time_refills_up_to_max_tokens(clock: _VirtualClock) -> None:
    limiter = RateLimiter(max_tokens=3, refill_rate=10.0)
    for _ in range(3):
        await limiter.acquire()
    clock.now += 10.0  # far more than needed to refill the bucket

    for _ in range(3):
        await limiter.acquire()

    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_burst_up_to_max_tokens_never_sleeps(clock: _VirtualClock) -> None:
    limiter = RateLimiter(max_tokens=30, refill_rate=30.0)

    for _ in range(30):
        await limiter.acquire()

    assert clock.sleeps == []
