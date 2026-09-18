"""Slack status reactions must survive Slack's benign ``already_reacted`` /
``no_reaction`` replies (#1756).

Slack redelivers events on timeout, so ``reactions.add`` routinely answers
``{"ok": false, "error": "already_reacted"}`` for a mark that is already on
the message, and ``reactions.remove`` answers ``no_reaction`` when a user
took the emoji off first. ``_post`` raised on both, and the lifecycle wrappers
turn any exception into a permanent ``_disable(...)`` -- one retry silenced
status reactions for the rest of the adapter's lifetime.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.channels._reactions import SlackStatusReactor
from agentos.channels.types import IncomingMessage


class _Response:
    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


class _Client:
    def __init__(self, replies: dict[str, list[dict[str, Any]]]) -> None:
        self._replies = replies
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def post(self, path: str, json: dict[str, str]) -> _Response:
        self.calls.append((path, json))
        queue = self._replies.get(path) or [{"ok": True}]
        data = queue.pop(0) if len(queue) > 1 else queue[0]
        return _Response(data)


class _Channel:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def _get_client(self) -> _Client:
        return self._client


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def _reactor(
    replies: dict[str, list[dict[str, Any]]],
) -> tuple[SlackStatusReactor, _Client, _RecordingLog]:
    client = _Client(replies)
    log = _RecordingLog()
    return SlackStatusReactor(_Channel(client), log), client, log


def _message(ts: str = "1700000000.000100") -> IncomingMessage:
    return IncomingMessage(sender_id="U1", channel_id="C1", content="hi", metadata={"ts": ts})


def _disabled_events(log: _RecordingLog) -> list[dict[str, Any]]:
    return [fields for event, fields in log.events if event == "channel.status_reaction_disabled"]


@pytest.mark.asyncio
async def test_already_reacted_on_add_is_a_no_op_and_keeps_the_reactor_enabled() -> None:
    reactor, client, log = _reactor({"/reactions.add": [{"ok": False, "error": "already_reacted"}]})
    message = _message()

    await reactor.received(message)

    assert reactor._disabled is False
    assert _disabled_events(log) == []
    # The mark *is* on the message, so it is tracked and reclaimed like any other.
    await reactor.completed(message)
    assert [path for path, _ in client.calls] == ["/reactions.add", "/reactions.remove"]
    assert client.calls[1][1]["name"] == "white_check_mark"


@pytest.mark.asyncio
async def test_no_reaction_on_remove_is_a_no_op_and_keeps_the_reactor_enabled() -> None:
    reactor, client, log = _reactor({"/reactions.remove": [{"ok": False, "error": "no_reaction"}]})
    message = _message()

    await reactor.received(message)
    await reactor.running(message)
    await reactor.completed(message)

    assert reactor._disabled is False
    assert _disabled_events(log) == []
    # Later messages still get their marks.
    await reactor.received(_message("1700000000.000200"))
    assert [path for path, _ in client.calls].count("/reactions.add") == 3


@pytest.mark.asyncio
async def test_benign_error_on_failed_mark_keeps_the_reactor_enabled() -> None:
    reactor, _client, log = _reactor(
        {"/reactions.add": [{"ok": False, "error": "already_reacted"}]}
    )

    await reactor.failed(_message())

    assert reactor._disabled is False
    assert _disabled_events(log) == []


@pytest.mark.parametrize("error", ["missing_scope", "not_allowed_token_type"])
@pytest.mark.asyncio
async def test_auth_shaped_errors_still_disable_the_reactor(error: str) -> None:
    reactor, _client, log = _reactor({"/reactions.add": [{"ok": False, "error": error}]})

    await reactor.received(_message())

    assert reactor._disabled is True
    assert [fields["reason"] for fields in _disabled_events(log)] == ["missing_oauth_scope"]


@pytest.mark.asyncio
async def test_other_slack_errors_still_disable_the_reactor() -> None:
    reactor, _client, log = _reactor({"/reactions.add": [{"ok": False, "error": "invalid_name"}]})

    await reactor.received(_message())

    assert reactor._disabled is True
    assert [fields["reason"] for fields in _disabled_events(log)] == ["add_failed:RuntimeError"]


@pytest.mark.asyncio
async def test_message_with_none_metadata_does_not_disable_the_reactor() -> None:
    reactor, client, log = _reactor({})
    message = IncomingMessage(sender_id="U1", channel_id="C1", content="hi")
    message.metadata = None  # type: ignore[assignment]

    await reactor.received(message)

    assert reactor._disabled is False
    assert _disabled_events(log) == []
    assert client.calls == []
