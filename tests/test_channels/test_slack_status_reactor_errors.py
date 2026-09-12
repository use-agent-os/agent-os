"""SlackStatusReactor and benign "already there"/"already gone" API errors.

``_post`` treated any Slack API error other than ``missing_scope``/
``not_allowed_token_type`` as fatal -- raising, which the caller (`_add_state`/
`_clear_active`) turns into a *permanent* ``_disable()`` for the rest of the
process's life, with no recovery path (``_disabled`` is only ever set back to
``False`` in ``__init__``).

``already_reacted`` (adding a reaction that's already there -- e.g. a restart
retrying ``received()``) and ``no_reaction`` (removing one that's already
gone -- e.g. a user manually removed it, or a concurrent settle already did)
both mean the *desired end state* was already true. Neither indicates
anything wrong with the bot's permissions or setup, so neither should
permanently turn the feature off.

Tests go through the real ``SlackStatusReactor``/``_post`` path (a mocked
HTTP client, not a hand-rolled fake), and explicitly pin that genuinely
unexpected errors still disable -- the fix must not be broadened past the
two benign cases it's meant for.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agentos.channels._reactions import SlackStatusReactor
from agentos.channels.types import IncomingMessage


class _NullLogger:
    def warning(self, *args: object, **kwargs: object) -> None:
        pass


def _message() -> IncomingMessage:
    return IncomingMessage(
        content="hi", channel_id="C1", sender_id="U1", metadata={"ts": "123.456"}
    )


def _reactor_with_response(status_code: int, body: dict[str, Any]) -> SlackStatusReactor:
    channel = MagicMock()
    client = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    response.json.return_value = body
    client.post = AsyncMock(return_value=response)
    channel._get_client.return_value = client
    return SlackStatusReactor(channel, _NullLogger())


async def test_already_reacted_on_add_is_treated_as_success() -> None:
    reactor = _reactor_with_response(200, {"ok": False, "error": "already_reacted"})

    await reactor.received(_message())

    assert reactor._disabled is False


async def test_no_reaction_on_remove_is_treated_as_success() -> None:
    reactor = _reactor_with_response(200, {"ok": False, "error": "no_reaction"})

    # failed() calls _clear_active (removes prior tokens) then adds the X.
    # Seed one "active" token directly so there's something to remove.
    reactor._active["123.456"] = [{"channel": "C1", "timestamp": "123.456", "name": "eyes"}]

    await reactor.failed(_message())

    assert reactor._disabled is False


async def test_genuinely_unexpected_slack_errors_still_disable() -> None:
    """The fix must not be broadened past the two benign cases it's for --
    rate limiting, for example, means the call genuinely did not happen and
    must not be silently treated as success.
    """
    reactor = _reactor_with_response(200, {"ok": False, "error": "ratelimited"})

    await reactor.received(_message())

    assert reactor._disabled is True


async def test_missing_scope_still_disables_unaffected_by_this_fix() -> None:
    reactor = _reactor_with_response(200, {"ok": False, "error": "missing_scope"})

    await reactor.received(_message())

    assert reactor._disabled is True


async def test_403_status_still_disables_unaffected_by_this_fix() -> None:
    reactor = _reactor_with_response(403, {})

    await reactor.received(_message())

    assert reactor._disabled is True


async def test_failed_reaches_the_x_marker_even_when_clearing_hits_no_reaction() -> None:
    """End-to-end: the real caller-facing symptom from #1756 -- failed()
    must still successfully post the X outcome marker even when clearing a
    stale/already-gone prior reaction raises no_reaction along the way.
    """
    channel = MagicMock()
    client = MagicMock()

    def _response_for(payload: dict[str, Any]) -> MagicMock:
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        if payload["name"] == "eyes":
            response.json.return_value = {"ok": False, "error": "no_reaction"}
        else:
            response.json.return_value = {"ok": True}
        return response

    async def _post(path: str, json: dict[str, Any]) -> MagicMock:
        return _response_for(json)

    client.post = AsyncMock(side_effect=_post)
    channel._get_client.return_value = client
    reactor = SlackStatusReactor(channel, _NullLogger())
    reactor._active["123.456"] = [{"channel": "C1", "timestamp": "123.456", "name": "eyes"}]

    await reactor.failed(_message())

    assert reactor._disabled is False
    add_calls = [c for c in client.post.await_args_list if c.args[0] == "/reactions.add"]
    assert len(add_calls) == 1
    assert add_calls[0].kwargs["json"]["name"] == "x"
