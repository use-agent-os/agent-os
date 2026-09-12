"""Regression tests for turn/session correlation in ``GatewayClient.send_message``.

``_recv_queue`` is connection-wide. A Ctrl-C leaves the gateway's asynchronous
``session.event.done(reason="aborted")`` in it, and a connection subscribed to
more than one session mixes their frames. ``send_message`` must only observe
frames belonging to its own turn.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.cli.gateway_client import GatewayClient

SESSION = "agent:main:abc123"
OTHER_SESSION = "agent:main:def456"


def _client(current_stream_seq: int | None) -> tuple[GatewayClient, list[tuple[str, dict]]]:
    """Client whose ``sessions.messages.subscribe`` reports ``current_stream_seq``."""

    client = GatewayClient()
    calls: list[tuple[str, dict]] = []

    async def fake_call(method: str, params: dict | None = None) -> dict[str, Any]:
        calls.append((method, params or {}))
        if method == "sessions.messages.subscribe" and current_stream_seq is not None:
            return {"subscribed": True, "key": SESSION, "current_stream_seq": current_stream_seq}
        return {}

    client._call = fake_call  # type: ignore[method-assign]
    return client, calls


async def _drain(client: GatewayClient) -> list[dict[str, Any]]:
    return [event async for event in client.send_message(SESSION, "second message")]


@pytest.mark.asyncio
async def test_stale_aborted_done_from_a_previous_turn_is_discarded() -> None:
    """The issue's repro: a Ctrl-C's `done` must not terminate the next turn."""

    client, _ = _client(current_stream_seq=7)
    for frame in (
        # Left over from the aborted turn (recorded before this turn subscribed).
        {
            "event": "session.event.done",
            "payload": {"reason": "aborted", "session_key": SESSION, "stream_seq": 7},
        },
        {
            "event": "session.event.text_delta",
            "payload": {"text": "actual answer", "session_key": SESSION, "stream_seq": 8},
        },
        {
            "event": "session.event.done",
            "payload": {"reason": "stop", "session_key": SESSION, "stream_seq": 9},
        },
    ):
        client._recv_queue.put_nowait(frame)

    events = await _drain(client)

    assert [event["event"] for event in events] == [
        "session.event.text_delta",
        "session.event.done",
    ]
    assert events[0]["text"] == "actual answer"
    assert events[-1]["reason"] == "stop"


@pytest.mark.asyncio
async def test_stale_frame_queued_behind_a_fresh_one_is_still_discarded() -> None:
    """Correlation is by ``stream_seq``, not by queue position.

    ``sessions.abort`` only confirms the abort was requested, so the aborted
    turn's ``done`` can be delivered after this turn's first frames. Discarding
    on arrival order alone would let it through.
    """

    client, _ = _client(current_stream_seq=4)
    for frame in (
        {
            "event": "session.event.text_delta",
            "payload": {"text": "fresh", "session_key": SESSION, "stream_seq": 5},
        },
        {
            "event": "session.event.done",
            "payload": {"reason": "aborted", "session_key": SESSION, "stream_seq": 4},
        },
        {
            "event": "session.event.done",
            "payload": {"reason": "stop", "session_key": SESSION, "stream_seq": 6},
        },
    ):
        client._recv_queue.put_nowait(frame)

    events = await _drain(client)

    assert [event.get("reason") for event in events if event["event"] == "session.event.done"] == [
        "stop"
    ]
    assert [event["event"] for event in events] == [
        "session.event.text_delta",
        "session.event.done",
    ]


@pytest.mark.asyncio
async def test_stale_text_delta_is_not_misattributed_to_the_new_turn() -> None:
    """Dropped output is the other half of the bug: stale text must not leak in."""

    client, _ = _client(current_stream_seq=2)
    for frame in (
        {
            "event": "session.event.text_delta",
            "payload": {"text": "previous answer", "session_key": SESSION, "stream_seq": 2},
        },
        {
            "event": "session.event.text_delta",
            "payload": {"text": "this answer", "session_key": SESSION, "stream_seq": 3},
        },
        {
            "event": "session.event.done",
            "payload": {"reason": "stop", "session_key": SESSION, "stream_seq": 4},
        },
    ):
        client._recv_queue.put_nowait(frame)

    events = await _drain(client)

    assert [event.get("text") for event in events if "text" in event] == ["this answer"]


@pytest.mark.asyncio
async def test_frames_for_another_subscribed_session_are_skipped() -> None:
    """One connection can hold subscriptions for several sessions."""

    client, _ = _client(current_stream_seq=0)
    for frame in (
        {
            "event": "session.event.done",
            "payload": {"reason": "stop", "session_key": OTHER_SESSION, "stream_seq": 1},
        },
        {
            "event": "session.event.text_delta",
            "payload": {"text": "mine", "session_key": SESSION, "stream_seq": 1},
        },
        {
            "event": "session.event.done",
            "payload": {"reason": "stop", "session_key": SESSION, "stream_seq": 2},
        },
    ):
        client._recv_queue.put_nowait(frame)

    events = await _drain(client)

    assert [event["event"] for event in events] == [
        "session.event.text_delta",
        "session.event.done",
    ]
    assert events[0]["text"] == "mine"


@pytest.mark.asyncio
async def test_another_session_is_filtered_even_without_a_seq_baseline() -> None:
    """Session-key correlation does not depend on the subscribe response."""

    client, _ = _client(current_stream_seq=None)
    client._recv_queue.put_nowait(
        {"event": "session.event.done", "payload": {"reason": "stop", "session_key": OTHER_SESSION}}
    )
    client._recv_queue.put_nowait(
        {"event": "session.event.done", "payload": {"reason": "stop", "session_key": SESSION}}
    )

    events = await _drain(client)

    assert [event["session_key"] for event in events] == [SESSION]
    assert [event["event"] for event in events] == ["session.event.done"]


@pytest.mark.asyncio
async def test_first_frame_of_this_turn_is_kept() -> None:
    """Boundary guard: ``stream_seq == baseline + 1`` is this turn's own output."""

    client, _ = _client(current_stream_seq=11)
    client._recv_queue.put_nowait(
        {
            "event": "session.event.done",
            "payload": {"reason": "stop", "session_key": SESSION, "stream_seq": 12},
        }
    )

    events = await _drain(client)

    assert [event["event"] for event in events] == ["session.event.done"]
    assert events[0]["stream_seq"] == 12


@pytest.mark.asyncio
async def test_uncorrelated_frames_still_stream_through() -> None:
    """A server that stamps neither field behaves exactly as before the fix."""

    client, calls = _client(current_stream_seq=None)
    client._recv_queue.put_nowait(
        {"event": "session.event.text_delta", "payload": {"text": "hello"}}
    )
    client._recv_queue.put_nowait({"event": "session.event.done", "payload": {}})

    events = await _drain(client)

    assert [event["event"] for event in events] == [
        "session.event.text_delta",
        "session.event.done",
    ]
    assert [method for method, _ in calls] == ["sessions.messages.subscribe", "sessions.send"]


@pytest.mark.asyncio
async def test_task_terminal_fallback_survives_correlation() -> None:
    """Task-runtime terminals carry no session stamp and must keep ending the turn."""

    client, _ = _client(current_stream_seq=3)
    client._recv_queue.put_nowait(
        {"event": "task.cancelled", "payload": {"task_id": "task-1"}},
    )

    events = await _drain(client)

    assert [event["event"] for event in events] == ["session.event.done"]
    assert events[0]["reason"] == "aborted"
