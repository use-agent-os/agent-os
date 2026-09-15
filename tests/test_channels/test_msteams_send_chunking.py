"""Issue #2114: MS Teams never chunked an outbound activity.

``MSTeamsChannel.send()`` passed ``message.content`` straight to
``send_activity`` with no length check, the gap #1544 already closed for
Telegram and Discord. Teams caps an Activity payload at 40 KB and rejects an
oversized one with ``413 MessageSizeTooBig`` rather than truncating it, so the
user received *nothing* — worse than the pre-#1544 behaviour elsewhere, where
at least a truncated message arrived.

The cap is counted in **UTF-16 code units**, not characters, which is why these
tests measure that way: an emoji is one ``len()`` character but two units, so a
character-counted limit passes a reply at twice the real payload size.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from agentos.channels.msteams import (
    _MSTEAMS_TEXT_UTF16_LIMIT,
    _split_for_teams,
    _utf16_units,
)
from agentos.channels.types import OutgoingMessage

EMOJI = "\U0001f600"  # one code point, two UTF-16 units


@pytest.fixture()
def botbuilder_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Satisfy ``send_streaming``'s lazy ``botbuilder.schema`` import."""
    if "botbuilder.schema" in sys.modules:
        return

    class _Activity:
        def __init__(self, **fields: Any) -> None:
            self.__dict__.update(fields)

    package = ModuleType("botbuilder")
    schema = ModuleType("botbuilder.schema")
    schema.Activity = _Activity  # type: ignore[attr-defined]
    package.schema = schema  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botbuilder", package)
    monkeypatch.setitem(sys.modules, "botbuilder.schema", schema)


class _RecordingTurnContext:
    def __init__(self, sent: list[str], updated: list[str], next_id: list[int]) -> None:
        self._sent = sent
        self._updated = updated
        self._next_id = next_id

    async def send_activity(self, payload: Any) -> Any:
        self._sent.append(str(payload))
        self._next_id[0] += 1
        return SimpleNamespace(id=f"msg-{self._next_id[0]}")

    async def update_activity(self, activity: Any) -> Any:
        self._updated.append(str(getattr(activity, "text", "")))
        return None


class _Adapter:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.updated: list[str] = []
        self._next_id = [0]

    async def continue_conversation(self, ref: Any, callback: Any, bot_id: Any = None) -> None:
        await callback(_RecordingTurnContext(self.sent, self.updated, self._next_id))


def _channel(adapter: _Adapter) -> Any:
    from agentos.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig

    channel = MSTeamsChannel(config=MSTeamsChannelConfig(name="msteams", edit_interval_s=0.0))
    channel._references["conv-1"] = SimpleNamespace()
    channel._adapter = adapter
    return channel


async def _stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def _assert_all_fit(activities: list[str]) -> None:
    oversized = [a for a in activities if _utf16_units(a) > _MSTEAMS_TEXT_UTF16_LIMIT]
    assert not oversized, f"{len(oversized)} activity/activities exceed the Teams payload cap"


# ── the splitter ────────────────────────────────────────────────────────────


def test_a_short_reply_is_one_segment() -> None:
    assert _split_for_teams("hello") == ["hello"]


def test_text_exactly_at_the_cap_is_not_split() -> None:
    """Off-by-one at the boundary would chunk every large-but-legal reply."""
    text = "y" * _MSTEAMS_TEXT_UTF16_LIMIT

    assert _split_for_teams(text) == [text]


def test_one_character_past_the_cap_splits() -> None:
    segments = _split_for_teams("y" * (_MSTEAMS_TEXT_UTF16_LIMIT + 1))

    assert len(segments) == 2
    _assert_all_fit(segments)


def test_the_reported_50k_reply_is_split_and_lossless() -> None:
    content = "x" * 50_000

    segments = _split_for_teams(content)

    assert len(segments) > 1
    assert "".join(segments) == content
    _assert_all_fit(segments)


def test_emoji_are_measured_in_utf16_units_not_characters() -> None:
    """The case a character-counted cap gets wrong.

    18000 emoji is 18000 ``len()`` characters but 36000 UTF-16 units — past the
    20480-unit payload cap. A limit compared against ``len()`` lets it through
    and Teams answers 413.
    """
    content = EMOJI * 18_000
    assert len(content) == 18_000
    assert _utf16_units(content) == 36_000

    segments = _split_for_teams(content)

    assert len(segments) > 1
    assert "".join(segments) == content
    _assert_all_fit(segments)


def test_a_split_never_lands_inside_a_surrogate_pair() -> None:
    """Python slices whole code points, so a cut cannot halve an astral
    character — pinned because the limit is now counted in UTF-16 units, where
    such a character spans two, and an off-by-one there would be invisible
    until a reply came back mojibaked."""
    content = EMOJI * 20_000

    segments = _split_for_teams(content)

    for segment in segments:
        assert segment.encode("utf-16-le").decode("utf-16-le") == segment
        assert set(segment) == {EMOJI}
    assert "".join(segments) == content


def test_a_short_code_block_is_not_left_half_open() -> None:
    """The shared splitter's guarantee: a cut lands before an opening fence, so
    a chunk never ends mid-block. Asserted here because a Teams reply is
    Markdown too and an unbalanced fence renders as literal backticks."""
    content = ("filler line\n" * 3_000) + "```\nshort block\n```\n" + ("tail line\n" * 100)

    segments = _split_for_teams(content)

    assert len(segments) > 1
    for segment in segments:
        assert segment.count("```") % 2 == 0
    assert "".join(segments) == content


def test_a_code_block_longer_than_the_cap_is_still_delivered_intact() -> None:
    """The limit of that guarantee, stated rather than assumed: a block bigger
    than one activity has to be cut somewhere inside itself, so the middle
    chunks carry an unbalanced fence. Losing the text would be the real
    failure; imperfect fence rendering across a 40 KB block is not."""
    content = "intro\n```\n" + ("code line\n" * 4_000) + "```\n"

    segments = _split_for_teams(content)

    assert len(segments) > 1
    assert "".join(segments) == content
    _assert_all_fit(segments)
    assert segments[0].count("```") % 2 == 0, "the first chunk is still balanced"


# ── send() ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_splits_an_oversized_reply_across_activities() -> None:
    adapter = _Adapter()

    await _channel(adapter).send(
        OutgoingMessage(channel_id="conv-1", content="x" * 50_000, reply_to="conv-1")
    )

    assert len(adapter.sent) > 1
    assert "".join(adapter.sent) == "x" * 50_000
    _assert_all_fit(adapter.sent)


@pytest.mark.asyncio
async def test_send_of_a_short_reply_is_still_one_activity() -> None:
    """Chunking must not turn every ordinary reply into a multi-message burst."""
    adapter = _Adapter()

    await _channel(adapter).send(
        OutgoingMessage(channel_id="conv-1", content="hello", reply_to="conv-1")
    )

    assert adapter.sent == ["hello"]


@pytest.mark.asyncio
async def test_send_remembers_the_last_activity_for_edit_and_delete() -> None:
    """A later edit or delete addresses the end of the reply, matching what the
    other chunking adapters do."""
    adapter = _Adapter()
    channel = _channel(adapter)

    await channel.send(
        OutgoingMessage(channel_id="conv-1", content="x" * 50_000, reply_to="conv-1")
    )

    assert channel._message_conversation_keys
    last_id = f"msg-{len(adapter.sent)}"
    assert last_id in channel._message_conversation_keys


# ── send_streaming() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_never_edits_an_activity_past_the_cap(
    botbuilder_schema: None,
) -> None:
    """The activity that was small at the first chunk is the one that outgrows
    the cap, because the stream keeps editing it in place."""
    adapter = _Adapter()
    big = "y" * 12_000

    await _channel(adapter).send_streaming(_stream(big, big, big), reply_to="conv-1")

    _assert_all_fit(adapter.sent)
    _assert_all_fit(adapter.updated)


@pytest.mark.asyncio
async def test_streaming_delivers_the_whole_reply(botbuilder_schema: None) -> None:
    """Rolling overflow into new activities must not drop or duplicate text:
    the final activity plus every closed one reconstruct the stream."""
    adapter = _Adapter()
    big = "y" * 12_000

    await _channel(adapter).send_streaming(_stream(big, big, big), reply_to="conv-1")

    delivered = "".join(adapter.sent[:-1]) + (adapter.updated or adapter.sent)[-1]
    assert len(delivered) >= len(big) * 3 - _MSTEAMS_TEXT_UTF16_LIMIT


@pytest.mark.asyncio
async def test_streaming_a_short_reply_still_uses_one_activity(
    botbuilder_schema: None,
) -> None:
    adapter = _Adapter()

    await _channel(adapter).send_streaming(_stream("first", "second"), reply_to="conv-1")

    assert len(adapter.sent) == 1
