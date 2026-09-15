"""Two occurrences that share bytes are still two occurrences.

``edit_file`` promises that "text appearing more than once is rejected rather
than guessed". ``_find_all`` advanced its cursor past the whole needle, so an
occurrence starting inside the previous one was never seen: three identical
adjacent lines contain a two-line pattern twice, and the edit landed on the
first pair silently, reported as a clean ``exact`` match.

The replacement pass still de-overlaps — only the count needed the truth.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.fuzzy_match import AmbiguousMatchError, fuzzy_find_and_replace
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

_TRIPLE = "x = 1\nx = 1\nx = 1\n"
_PAIR = "x = 1\nx = 1"


def test_a_pattern_that_overlaps_itself_is_ambiguous() -> None:
    with pytest.raises(AmbiguousMatchError) as excinfo:
        fuzzy_find_and_replace(_TRIPLE, _PAIR, "y = 2\ny = 2")

    assert excinfo.value.match_count == 2
    assert excinfo.value.lines == (1, 2)


def test_a_short_self_overlapping_pattern_is_ambiguous() -> None:
    with pytest.raises(AmbiguousMatchError) as excinfo:
        fuzzy_find_and_replace("aaa", "aa", "b")

    assert excinfo.value.match_count == 2


def test_separate_duplicates_are_still_ambiguous() -> None:
    """Control: the non-overlapping case already worked and must keep working."""
    content = "x = 1\nx = 1\nSEP\nx = 1\nx = 1\n"

    with pytest.raises(AmbiguousMatchError) as excinfo:
        fuzzy_find_and_replace(content, _PAIR, "y")

    assert excinfo.value.match_count == 2
    assert excinfo.value.lines == (1, 4)


def test_a_single_occurrence_still_edits() -> None:
    """Control: the common case is untouched."""
    result = fuzzy_find_and_replace("a = 1\nx = 1\nx = 1\nb = 2\n", _PAIR, "y = 2")

    assert result.strategy == "exact"
    assert result.match_count == 1
    assert result.updated == "a = 1\ny = 2\nb = 2\n"


def test_replace_all_still_splices_non_overlapping_regions() -> None:
    """``replace_all`` must not splice two regions that share bytes."""
    result = fuzzy_find_and_replace("aaa", "aa", "b", replace_all=True)

    assert result.updated == "ba"


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


edit_file = _original_async(fs.edit_file)


@contextmanager
def tool_context(workspace: Path) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_edit_file_refuses_an_overlapping_duplicate(tmp_path: Path) -> None:
    """The public path: the file must be left alone, not edited at a guess."""
    target = tmp_path / "sample.py"
    target.write_text(_TRIPLE, encoding="utf-8")

    with tool_context(tmp_path), pytest.raises(ValueError, match="matches 2 locations"):
        await edit_file(str(target), _PAIR, "y = 2\ny = 2")

    assert target.read_text(encoding="utf-8") == _TRIPLE
