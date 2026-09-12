"""Regression tests for GatewayClient.discard_pending_events() (#1790).

``_recv_queue`` is one connection-wide queue shared by every session this
client has ever subscribed to. ``abort_session()``'s RPC response only
confirms the abort request was received, not that the turn's own async
``session.event.done(reason="aborted")`` has already arrived over the
socket, so that event can still be sitting in the queue when the caller
starts a new turn on the same connection. ``discard_pending_events()`` lets
an abort caller drop it before that happens.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.cli.gateway_client import GatewayClient

pytestmark = pytest.mark.asyncio


async def test_discard_pending_events_drops_a_stale_event_left_by_an_earlier_call() -> None:
    """A leftover ``session.event.done`` (reason="aborted") from a previous
    turn must not be mistaken for the new call's own completion once
    discarded, and the new turn's real output must not be lost."""
    client = GatewayClient()

    async def fake_call(method: str, params: dict | None = None) -> Any:
        if method == "sessions.send":
            # The server only starts emitting events for THIS message once it
            # actually receives it -- i.e. once this RPC is dispatched.
            await client._recv_queue.put(
                {
                    "type": "event",
                    "event": "session.event.text_delta",
                    "payload": {"text": "actual answer"},
                }
            )
            await client._recv_queue.put(
                {"type": "event", "event": "session.event.done", "payload": {"reason": "stop"}}
            )
        return {}

    client._call = fake_call  # type: ignore[method-assign]

    # Leftover terminal event from a prior (aborted) turn on the same
    # connection, still queued when the caller discards and starts a new one.
    await client._recv_queue.put(
        {"type": "event", "event": "session.event.done", "payload": {"reason": "aborted"}}
    )

    client.discard_pending_events()
    events = [ev async for ev in client.send_message("session-key", "second message")]

    assert events == [
        {"event": "session.event.text_delta", "text": "actual answer"},
        {"event": "session.event.done", "reason": "stop"},
    ]


async def test_discard_pending_events_is_a_noop_on_an_empty_queue() -> None:
    client = GatewayClient()

    client.discard_pending_events()

    assert client._recv_queue.empty()


async def test_send_message_still_sees_events_queued_before_the_call() -> None:
    """send_message() itself must not discard anything -- only an explicit
    discard_pending_events() call (made by an abort caller) does. Many
    callers/tests legitimately queue a session's events before calling
    send_message(); that pattern must keep working."""
    client = GatewayClient()

    async def fake_call(method: str, params: dict | None = None) -> Any:
        return {}

    client._call = fake_call  # type: ignore[method-assign]
    client._recv_queue.put_nowait(
        {"type": "event", "event": "session.event.done", "payload": {}}
    )

    events = [ev async for ev in client.send_message("session-key", "hi")]

    assert events == [{"event": "session.event.done"}]
