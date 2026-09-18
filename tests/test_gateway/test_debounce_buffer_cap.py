"""Regression tests for the debounce buffer cap (issue #796).

Before the fix, ``_DefaultDebounceCoordinator.schedule`` appended every
message to ``state.buffer`` with no cap: a sender who posts faster than
``window_s`` expires grows the buffer unboundedly (memory DoS). The fix caps
the batch at ``MAX_COALESCED_MESSAGES`` and delivers the batch early when the
cap is hit, so the buffer stays bounded regardless of post rate.
"""

from __future__ import annotations

import asyncio

from agentos.channels.types import IncomingMessage
from agentos.gateway._debounce import MAX_COALESCED_MESSAGES, _DefaultDebounceCoordinator


def _msg(content: str = "x") -> IncomingMessage:
    return IncomingMessage(
        sender_id="spammer",
        channel_id="telegram",
        content=content,
        attachments=[],
        metadata={},
    )


async def test_debounce_buffer_capped_at_max_coalesced_messages():
    """The buffer never exceeds MAX_COALESCED_MESSAGES, no matter the post rate."""
    coord = _DefaultDebounceCoordinator()
    fired: list[object] = []

    async def on_fire(combined: object) -> None:
        fired.append(combined)

    spam = MAX_COALESCED_MESSAGES * 4
    for i in range(spam):
        await coord.schedule("tg:chat1", _msg(f"m{i}"), window_s=3600.0, on_fire=on_fire)

    state = coord._pending.get("tg:chat1")
    buffered = len(state.buffer) if state is not None else 0
    assert buffered <= MAX_COALESCED_MESSAGES

    # Let cap-triggered delivery tasks complete.
    for _ in range(spam):
        await asyncio.sleep(0)

    delivered = sum(getattr(c, "coalesced_count", 0) for c in fired)
    state_after = coord._pending.get("tg:chat1")
    buffered_after = len(state_after.buffer) if state_after is not None else 0
    # Every scheduled message must be accounted for: currently buffered + delivered
    # in earlier cap-flushes. The latest buffer (if any) is a fresh window.
    assert buffered_after + delivered == spam


async def test_debounce_cap_hit_delivers_batch_immediately():
    """Hitting the cap flushes the whole batch through on_fire right away."""
    coord = _DefaultDebounceCoordinator()
    fired: list[object] = []
    delivered_event = asyncio.Event()

    async def on_fire(combined: object) -> None:
        fired.append(combined)
        delivered_event.set()

    for i in range(MAX_COALESCED_MESSAGES):
        await coord.schedule(
            "tg:chat1", _msg(f"m{i}"), window_s=3600.0, on_fire=on_fire
        )

    await asyncio.wait_for(delivered_event.wait(), timeout=2.0)
    assert len(fired) == 1
    assert getattr(fired[0], "coalesced_count") == MAX_COALESCED_MESSAGES
    contents = getattr(fired[0], "content")
    assert contents.count("m") == MAX_COALESCED_MESSAGES
    # The window slot was cleared so a fresh one can start.
    assert "tg:chat1" not in coord._pending


async def test_debounce_normal_coalesce_below_cap_still_windows():
    """Below the cap, messages still coalesce across the window as before."""
    coord = _DefaultDebounceCoordinator()
    fired: list[object] = []

    async def on_fire(combined: object) -> None:
        fired.append(combined)

    await coord.schedule("tg:chat1", _msg("a"), window_s=0.05, on_fire=on_fire)
    await coord.schedule("tg:chat1", _msg("b"), window_s=0.05, on_fire=on_fire)
    await coord.schedule("tg:chat1", _msg("c"), window_s=0.05, on_fire=on_fire)

    await asyncio.sleep(0.15)
    assert len(fired) == 1
    assert getattr(fired[0], "coalesced_count") == 3
    assert getattr(fired[0], "content") == "a\nb\nc"


async def test_debounce_cap_flush_isolates_next_window():
    """After a cap flush, a new message starts a fresh window — the orphan
    timer of the flushed batch must not steal the next window's state."""
    coord = _DefaultDebounceCoordinator()
    fired: list[object] = []

    async def on_fire(combined: object) -> None:
        fired.append(combined)

    for i in range(MAX_COALESCED_MESSAGES):
        await coord.schedule("tg:chat1", _msg(f"old{i}"), window_s=3600.0, on_fire=on_fire)
    await asyncio.sleep(0)  # cap-triggered delivery runs

    # Fresh window with one message and a short window.
    await coord.schedule("tg:chat1", _msg("new"), window_s=0.05, on_fire=on_fire)
    await asyncio.sleep(0.2)

    counts = [getattr(c, "coalesced_count", 0) for c in fired]
    assert counts.count(MAX_COALESCED_MESSAGES) == 1
    assert counts.count(1) == 1


async def test_debounce_deliver_handles_on_fire_exception():
    """An exception in on_fire is logged and does not crash the debounce coordinator."""
    coord = _DefaultDebounceCoordinator()

    async def failing_on_fire(combined: object) -> None:
        raise RuntimeError("Database connection failed")

    # Scheduling and triggering delivery should not raise uncaught exception
    await coord.schedule("tg:chat1", _msg("hello"), window_s=0.01, on_fire=failing_on_fire)
    await asyncio.sleep(0.05)

    # Coordinator remains functional for subsequent messages
    fired: list[object] = []

    async def ok_on_fire(combined: object) -> None:
        fired.append(combined)

    await coord.schedule("tg:chat1", _msg("next"), window_s=0.01, on_fire=ok_on_fire)
    await asyncio.sleep(0.05)
    assert len(fired) == 1
    assert getattr(fired[0], "content") == "next"

