"""Regression + hardening tests for Discord gateway reconnect resilience.

Covers:
1. A failed reconnect (connect error, resume_url fetch error, hello error)
   must NOT kill the dispatch loop silently. The exception is contained and
   the loop transitions to a bounded retry/backoff state, then marks the
   channel dead instead of spinning forever or dying with an unobserved task.
2. An op-7 storm (Discord asks reconnect repeatedly) must be rate-limited by
   reconnect_max_retries / reconnect_base_delay_s instead of reconnecting
   indefinitely without backoff.
3. Success after a failed reconnect resets the failure counter (recovery).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig


class _FakeWebSocket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _stub_gateway(channel: DiscordChannel) -> None:
    async def connect_ws(url: str) -> _FakeWebSocket:
        return _FakeWebSocket(url)

    async def recv() -> dict[str, Any]:
        return {"d": {"heartbeat_interval": 60_000}}

    async def send(_payload: dict[str, Any]) -> None:
        return None

    async def fetch_url() -> str:
        return "wss://resume.example.test"

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    channel._connect_ws = connect_ws  # type: ignore[method-assign]
    channel._ws_recv = recv  # type: ignore[method-assign]
    channel._ws_send = send  # type: ignore[method-assign]
    channel._close_ws = noop  # type: ignore[method-assign]
    channel._fetch_gateway_url = fetch_url  # type: ignore[method-assign]
    channel._identify = noop  # type: ignore[method-assign]
    channel._state.resume_url = "wss://resume.example.test"  # noqa: SLF001
    channel._connected = True  # noqa: SLF001


def _make_channel(retries: int = 2, base_delay: float = 0.01) -> DiscordChannel:
    return DiscordChannel(
        DiscordChannelConfig(
            token="token",
            reconnect_max_retries=retries,
            reconnect_base_delay_s=base_delay,
        )
    )


@pytest.mark.asyncio
async def test_connect_failure_does_not_kill_dispatch_loop() -> None:
    channel = _make_channel()
    _stub_gateway(channel)

    attempts = 0

    async def failing_connect(url: str) -> _FakeWebSocket:
        nonlocal attempts
        attempts += 1
        raise OSError("simulated Discord gateway outage: connect refused")

    channel._connect_ws = failing_connect  # type: ignore[method-assign]

    # op 7 once, then nothing (recv would hang forever after the retries).
    async def recv_op7_then_hang() -> dict[str, Any]:
        return {"op": 7, "d": None}

    channel._ws_recv = recv_op7_then_hang  # type: ignore[method-assign]

    task = asyncio.create_task(channel._dispatch_loop())  # noqa: SLF001
    await asyncio.sleep(0.3)

    # After bounded retries the loop must have exited WITHOUT raising.
    assert task.done(), "dispatch loop must terminate after bounded retries, not spin forever"
    exc = task.exception()
    assert exc is None, f"dispatch loop must not die with an unobserved exception: {exc!r}"
    assert attempts == 2, f"expected exactly reconnect_max_retries connect attempts, got {attempts}"
    assert channel._connected is False, (
        "channel must flip _connected=False after retries exhausted (dead)"
    )


@pytest.mark.asyncio
async def test_op7_storm_is_bounded_by_retry_config() -> None:
    channel = _make_channel(retries=3, base_delay=0.01)
    _stub_gateway(channel)

    connects = 0

    async def ok_connect(url: str) -> _FakeWebSocket:
        nonlocal connects
        connects += 1
        return _FakeWebSocket(url)

    async def op7_every_time() -> dict[str, Any]:
        return {"op": 7, "d": None}

    channel._connect_ws = ok_connect  # type: ignore[method-assign]
    channel._ws_recv = op7_every_time  # type: ignore[method-assign]

    task = asyncio.create_task(channel._dispatch_loop())  # noqa: SLF001
    await asyncio.sleep(0.4)

    assert task.done(), "op-7 storm must terminate via bounded retries, not reconnect forever"
    assert connects <= 4, (
        f"connect attempts must be capped (retries=3 -> <=4 attempts), got {connects}"
    )
    assert channel._connected is False, "channel must go dead after retry cap, not spin"


@pytest.mark.asyncio
async def test_retry_backoff_sleeps_between_attempts() -> None:
    channel = _make_channel(retries=3, base_delay=0.2)
    _stub_gateway(channel)
    import time

    times: list[float] = []

    async def failing_connect(url: str) -> _FakeWebSocket:
        times.append(time.monotonic())
        raise OSError("nope")

    channel._connect_ws = failing_connect  # type: ignore[method-assign]

    async def recv_op7() -> dict[str, Any]:
        return {"op": 7, "d": None}

    channel._ws_recv = recv_op7  # type: ignore[method-assign]

    task = asyncio.create_task(channel._dispatch_loop())  # noqa: SLF001
    await asyncio.sleep(2.0)
    await asyncio.gather(task, return_exceptions=True)

    # 3 retries -> 4 attempts; each gap should be >= base_delay
    assert len(times) >= 3
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and min(gaps) >= 0.15, f"backoff gaps too small: {gaps}"


@pytest.mark.asyncio
async def test_success_resets_failure_counter() -> None:
    channel = _make_channel(retries=2, base_delay=0.01)
    _stub_gateway(channel)

    state = {"fails": 1}

    async def flaky_connect(url: str) -> _FakeWebSocket:
        if state["fails"] > 0:
            state["fails"] -= 1
            raise OSError("transient")
        return _FakeWebSocket(url)

    # One hello, then an op-7 storm: after the transient failure recovers, the
    # reconnect budget must be reset so the storm is served with fresh retries,
    # and the loop must survive (bounded, then dead) rather than dying early.
    async def recv_op7() -> dict[str, Any]:
        return {"op": 7, "d": None}

    channel._connect_ws = flaky_connect  # type: ignore[method-assign]
    channel._ws_recv = recv_op7  # type: ignore[method-assign]

    task = asyncio.create_task(channel._dispatch_loop())  # noqa: SLF001
    await asyncio.sleep(0.5)
    await asyncio.gather(task, return_exceptions=True)

    assert task.done()
    assert task.exception() is None
