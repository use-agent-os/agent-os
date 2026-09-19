"""Issue #2779: ``DiscordChannel.send_file`` on long captions and unresolved targets.

1. Discord caps a message's ``content`` at 2000 characters, file or not, and
   400s the whole upload past it (``BASE_TYPE_MAX_LENGTH``). ``send()`` split
   long text; ``send_file`` did not, so a file with a long caption was never
   delivered. The first 2000 characters now ride with the file as its
   caption and the rest follow as ordinary channel messages through
   ``send()``, which already splits and orders them.
2. ``send_file`` built ``/channels/{channel_id}/messages`` from whatever it
   was handed: the ``<channel_id>|<message_id>`` composite gave a 404 on a URL
   containing ``|``, and ``""`` requested ``/channels//messages`` instead of
   falling back to ``default_channel_id`` the way ``send()`` does.

Every request is recorded -- URL, body, order -- so the tests assert what
Discord would have received, not just that something was called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog.testing

from agentos.channels.discord import (
    _DISCORD_MESSAGE_TEXT_LIMIT,
    DiscordChannel,
    DiscordChannelConfig,
)

_REQUEST = httpx.Request("POST", "https://discord.test/api")
LIMIT = _DISCORD_MESSAGE_TEXT_LIMIT


def _channel(default: str = "C123") -> DiscordChannel:
    channel = DiscordChannel(
        config=DiscordChannelConfig(token="bot-test", default_channel_id=default)
    )
    channel._rate_limiter.acquire = AsyncMock()  # type: ignore[method-assign]
    return channel


class Recorder:
    """A ``client.post`` that records every request and answers by script."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.statuses = list(statuses or [])
        self.next_id = 100

    async def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        files = kwargs.get("files")
        self.calls.append(
            {
                "url": url,
                "content": (kwargs.get("data") or kwargs.get("json") or {}).get("content"),
                "file": files["file"][1].read() if files else None,
            }
        )
        status = self.statuses.pop(0) if self.statuses else 200
        self.next_id += 1
        return httpx.Response(status, json={"id": str(self.next_id)}, request=_REQUEST)


def _attach(channel: DiscordChannel, recorder: Recorder) -> None:
    client = AsyncMock()
    client.post = recorder
    channel._client = client


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "report.txt"
    path.write_bytes(b"file bytes")
    return path


# ── captions within the limit are unchanged ─────────────────────────────────


async def test_a_short_caption_rides_with_the_file_and_nothing_follows(sample: Path) -> None:
    channel, recorder = _channel(), Recorder()
    _attach(channel, recorder)

    result = await channel.send_file("C1", str(sample), content="here you go")

    assert [c["url"] for c in recorder.calls] == ["/channels/C1/messages"]
    assert recorder.calls[0]["content"] == "here you go"
    assert recorder.calls[0]["file"] == b"file bytes"
    assert result.is_delivered()
    assert result.provider_message_id == "101"


async def test_no_caption_sends_no_content_field(sample: Path) -> None:
    channel, recorder = _channel(), Recorder()
    _attach(channel, recorder)

    await channel.send_file("C1", str(sample))

    assert recorder.calls[0]["content"] is None


async def test_a_caption_of_exactly_the_limit_is_not_split(sample: Path) -> None:
    channel, recorder = _channel(), Recorder()
    _attach(channel, recorder)

    await channel.send_file("C1", str(sample), content="x" * LIMIT)

    assert len(recorder.calls) == 1
    assert len(recorder.calls[0]["content"]) == LIMIT


# ── the issue: captions past the limit ──────────────────────────────────────


async def test_one_character_over_the_limit_becomes_a_caption_and_one_follow_up(
    sample: Path,
) -> None:
    channel, recorder = _channel(), Recorder()
    _attach(channel, recorder)

    result = await channel.send_file("C1", str(sample), content="x" * (LIMIT + 1))

    assert [c["url"] for c in recorder.calls] == ["/channels/C1/messages"] * 2
    assert recorder.calls[0]["file"] == b"file bytes", "the file goes with the first chunk"
    assert len(recorder.calls[0]["content"]) == LIMIT
    assert recorder.calls[1]["file"] is None
    assert recorder.calls[1]["content"] == "x"
    assert result.provider_message_id == "101", "the result is the file's message, not the tail's"


async def test_a_long_caption_is_delivered_in_full_and_in_order(sample: Path) -> None:
    """Five thousand characters: the caption, then two follow-ups from
    ``send()``'s own splitter. Reassembled, nothing is lost."""
    channel, recorder = _channel(), Recorder()
    _attach(channel, recorder)
    content = "".join(f"{i % 10}" for i in range(5000))

    await channel.send_file("C1", str(sample), content=content)

    assert len(recorder.calls) == 3
    assert all(len(c["content"]) <= LIMIT for c in recorder.calls)
    assert "".join(c["content"] for c in recorder.calls) == content
    assert [c["file"] is not None for c in recorder.calls] == [True, False, False]


async def test_the_overflow_is_split_at_a_line_boundary_like_send(sample: Path) -> None:
    """The same splitter ``send()`` uses: a caption that is many short lines
    breaks between lines, not mid-word, and every chunk stays under the cap."""
    channel, recorder = _channel(), Recorder()
    _attach(channel, recorder)
    content = "\n".join(f"line {i:04d} of the caption" for i in range(200))

    await channel.send_file("C1", str(sample), content=content)

    assert len(recorder.calls) >= 2
    for call in recorder.calls:
        assert len(call["content"]) <= LIMIT
        assert not call["content"].endswith(" of the"), "not split mid-line"


async def test_the_follow_ups_go_to_the_resolved_channel(sample: Path) -> None:
    channel, recorder = _channel(default="DEF"), Recorder()
    _attach(channel, recorder)

    await channel.send_file("", str(sample), content="y" * (LIMIT + 5))

    assert {c["url"] for c in recorder.calls} == {"/channels/DEF/messages"}


async def test_the_file_message_is_cached_under_the_resolved_channel(sample: Path) -> None:
    channel, recorder = _channel(), Recorder()
    _attach(channel, recorder)

    await channel.send_file("C1|M9", str(sample), content="z" * (LIMIT + 1))

    assert channel._sent_messages["101"] == "C1"


# ── the issue: target resolution ────────────────────────────────────────────


async def test_a_composite_target_uses_only_its_channel_component(sample: Path) -> None:
    channel, recorder = _channel(), Recorder()
    _attach(channel, recorder)

    result = await channel.send_file("C77|M42", str(sample), content="hi")

    assert recorder.calls[0]["url"] == "/channels/C77/messages"
    assert result.target_id == "C77"


async def test_an_empty_target_falls_back_to_the_default_channel(sample: Path) -> None:
    channel, recorder = _channel(default="DEF"), Recorder()
    _attach(channel, recorder)

    result = await channel.send_file("", str(sample), content="hi")

    assert recorder.calls[0]["url"] == "/channels/DEF/messages"
    assert result.target_id == "DEF"


async def test_a_composite_with_an_empty_channel_falls_back_to_the_default(sample: Path) -> None:
    channel, recorder = _channel(default="DEF"), Recorder()
    _attach(channel, recorder)

    await channel.send_file("|M42", str(sample))

    assert recorder.calls[0]["url"] == "/channels/DEF/messages"


async def test_no_target_and_no_default_is_refused_before_any_request(sample: Path) -> None:
    channel, recorder = _channel(default=""), Recorder()
    _attach(channel, recorder)

    with pytest.raises(ValueError, match="discord.send_file requires a channel_id"):
        await channel.send_file("", str(sample), content="hi")

    assert recorder.calls == []


async def test_the_target_is_resolved_before_the_file_is_checked(tmp_path: Path) -> None:
    """An unresolvable target fails on the target, not on a file it will never send."""
    channel = _channel(default="")

    with pytest.raises(ValueError, match="channel_id"):
        await channel.send_file("", str(tmp_path / "missing.txt"))


def test_resolution_mirrors_send_and_the_edit_delete_helper() -> None:
    channel = _channel(default="DEF")

    assert channel._resolve_file_target("C1") == "C1"
    assert channel._resolve_file_target("C1|M2") == "C1"
    assert channel._resolve_file_target("") == "DEF"
    assert channel._resolve_file_target("|M2") == "DEF"
    assert channel._split_message_ref("C1|M2") == ("C1", "M2"), "same composite convention"


# ── failure semantics ───────────────────────────────────────────────────────


async def test_a_lost_overflow_does_not_turn_a_delivered_file_into_a_failure(
    sample: Path,
) -> None:
    """Once the upload has succeeded the file is in the channel. A failed
    follow-up is logged with the ids and the file's result stands -- raising
    would have the caller retry and upload the file a second time."""
    channel, recorder = _channel(), Recorder(statuses=[200, 400])
    _attach(channel, recorder)

    with structlog.testing.capture_logs() as logs:
        result = await channel.send_file("C1", str(sample), content="x" * (LIMIT + 1))

    assert result.is_delivered()
    assert result.provider_message_id == "101"
    events = [
        entry for entry in logs if entry["event"] == "discord.send_file_caption_overflow_failed"
    ]
    assert len(events) == 1
    assert events[0]["message_id"] == "101"
    assert events[0]["channel_id"] == "C1"
    assert events[0]["overflow_chars"] == 1


async def test_a_runtime_error_exhausted_overflow_does_not_fail_delivered_file(
    sample: Path,
) -> None:
    """Issue #3051: When retry_request exhausts retries on follow-up overflow,
    it raises RuntimeError. The already-uploaded file delivery must stand."""
    channel, recorder = _channel(), Recorder(statuses=[200])
    _attach(channel, recorder)
    channel.send = AsyncMock(side_effect=RuntimeError("retry_request exhausted"))  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as logs:
        result = await channel.send_file("C1", str(sample), content="x" * (LIMIT + 1))

    assert result.is_delivered()
    assert result.provider_message_id == "101"
    events = [
        entry for entry in logs if entry["event"] == "discord.send_file_caption_overflow_failed"
    ]
    assert len(events) == 1
    assert events[0]["error"] == "retry_request exhausted"


async def test_a_failed_upload_still_raises_and_sends_no_follow_up(sample: Path) -> None:
    channel, recorder = _channel(), Recorder(statuses=[400])
    _attach(channel, recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await channel.send_file("C1", str(sample), content="x" * (LIMIT + 1))

    assert len(recorder.calls) == 1, "the overflow is never sent for a file that did not go"


async def test_the_caption_limit_is_the_one_send_uses() -> None:
    """One constant for both paths, so they cannot drift apart again."""
    assert LIMIT == 2000
    assert DiscordChannel._split_content_for_send("a" * 4001) == ["a" * 2000, "a" * 2000, "a"]
