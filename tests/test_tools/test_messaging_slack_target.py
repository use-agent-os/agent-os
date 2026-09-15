"""The ``message`` tool's ``send`` action honors ``target`` for Slack (#2217).

``_outgoing_metadata``/``_reply_to_target`` are how the ``message`` tool
turns ``channel``/``target``/``thread_id`` into what a channel adapter's
``send()`` actually receives. For Slack, neither ever passed ``target``
through -- ``_reply_to_target`` returned only ``thread_id``, and
``_outgoing_metadata`` only ever extracted ``thread_ts`` from it. A caller
asking to send to a specific channel (``target="C99999999"``) would always
land in the adapter's configured default instead, while the tool's response
still echoed back the requested target as if it had worked.

``SlackChannel.send()`` already supports an explicit channel via
``metadata["channel"]`` (which wins over whatever ``reply_to`` produced) --
this fix uses exactly that path, applying the same C/G/D shape check
(stripped first, matching ``send()``'s own ``rt = (message.reply_to or
"").strip()``) ``send()`` itself already uses for ``reply_to``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos.tools.builtin import messaging
from agentos.tools.builtin.messaging import _outgoing_metadata, _reply_to_target

# --- _outgoing_metadata / _reply_to_target ----------------------------------


def test_channel_shaped_target_becomes_the_slack_channel_override() -> None:
    assert _outgoing_metadata("slack", "C99999999", thread_id=None) == {"channel": "C99999999"}


@pytest.mark.parametrize("target", ["", "U12345", "general", "not-a-channel-id"])
def test_non_channel_shaped_target_does_not_override_the_default(target: str) -> None:
    """A bare user id, a channel name, or an empty target must not clobber
    the adapter's default channel -- only C/G/D-shaped ids are Slack
    conversation ids `SlackChannel.send()` itself would recognize.
    """
    assert "channel" not in _outgoing_metadata("slack", target, thread_id=None)


def test_target_and_thread_id_both_carry_through_together() -> None:
    metadata = _outgoing_metadata("slack", "C99999999", thread_id="171234.5678")

    assert metadata == {"channel": "C99999999", "thread_ts": "171234.5678"}


def test_group_and_dm_prefixes_also_override() -> None:
    assert _outgoing_metadata("slack", "G12345", thread_id=None) == {"channel": "G12345"}
    assert _outgoing_metadata("slack", "D12345", thread_id=None) == {"channel": "D12345"}


def test_whitespace_padded_target_is_stripped_before_the_shape_check() -> None:
    """Matches SlackChannel.send()'s own `rt = (message.reply_to or
    "").strip()` -- a shape check on an un-stripped string would reject a
    channel id with incidental leading/trailing whitespace that send()
    itself would have accepted."""
    assert _outgoing_metadata("slack", "  C99999999  ", thread_id=None) == {
        "channel": "C99999999"
    }


def test_reply_to_target_is_unaffected_by_this_fix() -> None:
    """metadata["channel"] wins unconditionally in SlackChannel.send(), so
    _reply_to_target doesn't need to change -- pinned here so a future
    "fix" doesn't duplicate the routing in two places.
    """
    assert _reply_to_target("slack", "C99999999", thread_id=None) is None
    assert _reply_to_target("slack", "C99999999", thread_id="171234.5678") == "171234.5678"


def test_telegram_and_generic_channels_are_unaffected() -> None:
    assert _outgoing_metadata("telegram", "12345", thread_id=None) == {"chat_id": "12345"}
    assert _outgoing_metadata("discord", "999", thread_id=None) == {"recipient": "999"}


# --- end-to-end through the public tool -------------------------------------


async def test_message_tool_send_routes_to_the_requested_slack_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []
    adapter = AsyncMock()
    adapter.send = AsyncMock(side_effect=lambda msg: captured.append(msg))
    monkeypatch.setattr(messaging, "_channels", {"slack": adapter})

    out = json.loads(
        await messaging.message(
            channel="slack",
            target="C99999999",
            text="Deployment complete",
            action="send",
        )
    )

    assert out == {"status": "sent", "channel": "slack", "target": "C99999999"}
    sent = captured[0]
    assert sent.metadata.get("channel") == "C99999999"


async def test_message_tool_send_to_default_channel_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: omitting target (or a non-channel-shaped one) must
    keep behaving exactly as before -- no channel override at all.
    """
    captured: list[Any] = []
    adapter = AsyncMock()
    adapter.send = AsyncMock(side_effect=lambda msg: captured.append(msg))
    monkeypatch.setattr(messaging, "_channels", {"slack": adapter})

    await messaging.message(channel="slack", target="", text="hello", action="send")

    assert captured[0].metadata == {}
