"""A code block split across Discord messages stays a code block.

``split_text_for_limit`` backs a cut off a half-open fence, but it cannot when
the block starts at the segment's own beginning — backing up there would return
an empty chunk. That is the shape of every chunk after the first of a long
block, so one block arrived as an unterminated fence, then messages that
rendered as prose, then a stray fence.

Discord's chunks are independent messages and nothing maps them back onto
offsets in the source, so the seam closes and reopens the block. The shared
splitter stays byte-for-byte lossless for Telegram's streaming watermark.
"""

from __future__ import annotations

import pytest

from agentos.channels._util import split_text_for_limit
from agentos.channels.discord import _DISCORD_MESSAGE_TEXT_LIMIT as LIMIT
from agentos.channels.discord import DiscordChannel

split = DiscordChannel._split_content_for_send


def _long_block(lines: int = 300, lang: str = "py") -> str:
    body = "\n".join(f"line_{index} = compute({index})" for index in range(lines))
    return f"Here is the module:\n```{lang}\n{body}\n```\nDone."


def _payload(text: str) -> str:
    """The text with every fence *line* dropped, for comparing the content itself."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("```"))


def test_every_chunk_of_a_long_block_is_balanced() -> None:
    chunks = split(_long_block())

    assert len(chunks) > 3, "the fixture must actually span several messages"
    for index, chunk in enumerate(chunks):
        assert chunk.count("```") % 2 == 0, f"chunk {index} left a fence open"


def test_no_chunk_exceeds_the_message_limit() -> None:
    """The closing and reopening markers must be paid for out of the budget."""
    chunks = split(_long_block())

    assert max(len(chunk) for chunk in chunks) <= LIMIT


def test_the_middle_of_a_long_block_is_still_a_block() -> None:
    """Regression: middle messages used to carry no fence at all and rendered as prose."""
    chunks = split(_long_block())

    middle = chunks[2]
    assert middle.startswith("```")
    assert middle.endswith("```")


def test_the_language_is_carried_into_the_reopened_fence() -> None:
    chunks = split(_long_block(lang="python"))

    assert chunks[2].startswith("```python\n")


def test_the_code_survives_the_split() -> None:
    content = _long_block()

    assert _payload("".join(split(content))) == _payload(content)


@pytest.mark.parametrize("size", [50, 5_000, 12_000])
def test_content_without_a_fence_splits_exactly_as_the_shared_splitter_does(size: int) -> None:
    """No fence, no reserved room: the common case is untouched."""
    content = " ".join(f"word{index}" for index in range(size))

    expected: list[str] = []
    remaining = content
    while True:
        head, tail = split_text_for_limit(remaining, LIMIT)
        expected.append(head)
        if not tail:
            break
        remaining = tail

    assert split(content) == expected


def test_a_block_that_fits_one_message_is_untouched() -> None:
    content = "Look:\n```py\nx = 1\n```\nthanks"

    assert split(content) == [content]
