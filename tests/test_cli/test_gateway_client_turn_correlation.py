"""Regression tests for ``GatewayClient.send_message`` turn correlation (#1790).

The client keeps one connection-wide ``_recv_queue``. On Ctrl-C the CLI
abandons the event generator and calls ``sessions.abort``; the server's
``session.event.done(reason="aborted")`` -- and any in-flight deltas -- then
sit in the queue. The *next* ``send_message`` used to pop that stale ``done``
first, report the new message as cancelled, and leave the real answer for
whoever read the queue next.

``send_message`` now (1) discards whatever is already queued when the turn
starts, (2) ignores frames tagged with another session key, and (3) ignores
frames whose ``stream_seq`` is at or below the watermark the subscribe
response reported for this session.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.cli.gateway_client import GatewayClient

SESSION = "agent:main:abc123"


def _event(name: str, *, seq: int | None = None, key: str | None = SESSION, **payload: Any) -> dict:
    body: dict[str, Any] = dict(payload)
    if key is not None:
        body["session_key"] = key
    if seq is not None:
        body["stream_seq"] = seq
    return {"type": "event", "event": name, "payload": body}


def _client(
    *,
    on_send: list[dict] | None = None,
    subscribe_response: dict[str, Any] | None = None,
    send_response: dict[str, Any] | None = None,
) -> tuple[GatewayClient, list[tuple[str, dict]]]:
    """A client whose fake server delivers ``on_send`` frames when the turn is
    accepted -- the earliest a real gateway can emit anything for it."""
    client = GatewayClient()
    calls: list[tuple[str, dict]] = []

    async def fake_call(method: str, params: dict | None = None) -> dict:
        calls.append((method, params or {}))
        if method == "sessions.messages.subscribe":
            return dict(subscribe_response or {})
        if method == "sessions.send":
            for frame in on_send or []:
                client._recv_queue.put_nowait(frame)
            return dict(send_response or {})
        return {}

    client._call = fake_call  # type: ignore[method-assign]
    return client, calls


async def _collect(client: GatewayClient, key: str = SESSION) -> list[dict]:
    return [event async for event in client.send_message(key, "second message")]


@pytest.mark.asyncio
async def test_stale_aborted_turn_does_not_terminate_the_next_turn() -> None:
    """The issue's reproduction: leftovers from an aborted turn are queued
    before the next ``send_message`` starts."""
    client, _ = _client(
        on_send=[
            _event("session.event.text_delta", seq=12, text="actual answer"),
            _event("session.event.done", seq=13, reason="stop"),
        ],
        subscribe_response={"key": SESSION, "current_stream_seq": 11},
    )
    # Left behind by the Ctrl-C'd turn.
    client._recv_queue.put_nowait(_event("session.event.text_delta", seq=10, text="old"))
    client._recv_queue.put_nowait(_event("session.event.done", seq=11, reason="aborted"))

    events = await _collect(client)

    assert [(e["event"], e.get("text", e.get("reason"))) for e in events] == [
        ("session.event.text_delta", "actual answer"),
        ("session.event.done", "stop"),
    ]
    assert client._recv_queue.empty()


@pytest.mark.asyncio
async def test_frames_at_or_below_the_subscribe_watermark_are_ignored() -> None:
    """A pre-turn frame that arrives *after* the drain (late on the wire) is
    still recognisable by its ``stream_seq``."""
    client, _ = _client(
        on_send=[
            _event("session.event.done", seq=11, reason="aborted"),  # late stale frame
            _event("session.event.text_delta", seq=12, text="answer"),
            _event("session.event.done", seq=13, reason="stop"),
        ],
        subscribe_response={"key": SESSION, "current_stream_seq": 11},
    )

    events = await _collect(client)

    assert [e["event"] for e in events] == ["session.event.text_delta", "session.event.done"]
    assert events[-1]["reason"] == "stop"


@pytest.mark.asyncio
async def test_frames_from_another_session_are_ignored() -> None:
    """After ``/new`` or ``/resume`` the connection is still subscribed to the
    previous session; its events must not leak into the new session's turn."""
    client, _ = _client(
        on_send=[
            _event("session.event.done", seq=99, key="agent:main:old-session", reason="aborted"),
            _event("session.event.text_delta", seq=1, text="answer"),
            _event("session.event.done", seq=2, reason="stop"),
        ],
        subscribe_response={"key": SESSION, "current_stream_seq": 0},
    )

    events = await _collect(client)

    assert [e["event"] for e in events] == ["session.event.text_delta", "session.event.done"]
    assert all(e["session_key"] == SESSION for e in events)


@pytest.mark.asyncio
async def test_frames_tagged_with_the_canonical_key_are_accepted() -> None:
    """The server canonicalises session keys; the payload carries the canonical
    form, and the subscribe response reports it. Filtering must use that,
    not a byte-for-byte comparison with what the caller passed."""
    canonical = "agent:main:abc123"
    client, _ = _client(
        on_send=[
            _event("session.event.text_delta", seq=1, key=canonical, text="answer"),
            _event("session.event.done", seq=2, key=canonical, reason="stop"),
        ],
        subscribe_response={"key": canonical, "current_stream_seq": 0},
    )

    events = await _collect(client, key="agent:Main:abc123")

    assert [e["event"] for e in events] == ["session.event.text_delta", "session.event.done"]


@pytest.mark.asyncio
async def test_untagged_frames_still_flow() -> None:
    """Frames without ``session_key``/``stream_seq`` (task.* terminals, older
    servers, minimal fakes) are not subject to correlation."""
    client, _ = _client(
        on_send=[
            {"event": "session.event.text_delta", "payload": {"text": "answer"}},
            {"event": "session.event.done", "payload": {}},
        ],
    )

    events = await _collect(client)

    assert [e["event"] for e in events] == ["session.event.text_delta", "session.event.done"]


def _task_event(name: str, task_id: str) -> dict:
    """Task-runtime frames carry ``session_key`` and ``task_id`` but no ``stream_seq``."""
    return {"type": "event", "event": name, "payload": {"session_key": SESSION, "task_id": task_id}}


@pytest.mark.asyncio
async def test_task_terminal_for_another_task_does_not_end_the_turn() -> None:
    """``sessions.abort`` returns before the aborted task unwinds, so its
    ``task.cancelled`` can land after the new turn subscribed. It names a
    different task than the one ``sessions.send`` just accepted."""
    client, _ = _client(
        on_send=[
            _task_event("task.cancelled", "task-old"),
            _event("session.event.text_delta", seq=12, text="answer"),
            _event("session.event.done", seq=13, reason="stop"),
        ],
        subscribe_response={"key": SESSION, "current_stream_seq": 11},
        send_response={"status": "accepted", "key": SESSION, "task_id": "task-new"},
    )

    events = await _collect(client)

    assert [e["event"] for e in events] == ["session.event.text_delta", "session.event.done"]
    assert events[-1]["reason"] == "stop"


@pytest.mark.asyncio
async def test_task_terminal_for_the_accepted_task_still_ends_the_turn() -> None:
    client, _ = _client(
        on_send=[_task_event("task.cancelled", "task-new")],
        subscribe_response={"key": SESSION, "current_stream_seq": 11},
        send_response={"status": "accepted", "key": SESSION, "task_id": "task-new"},
    )

    events = await _collect(client)

    assert events == [{"event": "session.event.done", "reason": "aborted"}]


@pytest.mark.asyncio
async def test_task_terminal_without_an_accepted_task_id_still_ends_the_turn() -> None:
    """Older servers do not return ``task_id`` from ``sessions.send``; the
    task-terminal fallback must keep working for them."""
    client, _ = _client(on_send=[_task_event("task.failed", "task-whatever")])

    events = await _collect(client)

    assert [e["event"] for e in events] == ["session.event.error"]
