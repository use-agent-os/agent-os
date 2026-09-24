"""``memory_get``'s ``from``/``lines`` counted lines nobody else counts.

#3176 established that a tool naming a line number must count newlines only:
``str.splitlines()`` also breaks on a form feed, a lone CR, NEL and U+2028,
and ``read_file`` (which numbers by iterating the binary handle), ``git diff``
and every editor do not. It added ``split_lines`` / ``split_lines_keepends``
and moved ``grep_search`` and ``apply_patch`` onto them. ``memory_get`` kept
``str.splitlines()``.

Two things went wrong with it:

* **The line numbers disagree.** ``grep_search`` names line 3 of a memory file;
  ``memory_get(path, **{"from": 3})`` returns a different line, because the two
  split the same file differently.
* **The slice rewrote the file's own text.** The lines were rejoined with
  ``"\n"``, so each form feed, NEL and U+2028 came back as a newline. The same
  tool therefore returned *different text for the same file* depending on
  whether ``from``/``lines`` was passed -- a full-file slice was not the file.

Both are fixed by slicing ``split_lines_keepends`` and joining on ``""``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.tools.builtin._lines import split_lines
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry


class _FakeRetriever:
    pass


@pytest.fixture
def memory_get(tmp_path: Path):
    registry = ToolRegistry()
    create_memory_tools(
        stores=SimpleNamespace(),
        retrievers=_FakeRetriever(),
        memory_dir=str(tmp_path),
        registry=registry,
    )
    tool = registry.get("memory_get")
    assert tool is not None
    return tool.handler


#: Characters ``str.splitlines()`` breaks on that survive the read. ``\r\n``
#: and a lone ``\r`` are deliberately absent: ``read_text`` applies universal
#: newline translation, so they are already ``\n`` before the slice ever runs
#: and neither path can disagree about them. (That translation is itself a
#: difference from ``read_file``, which preserves the file's terminators --
#: a separate question about the open mode, not about this slice.)
SAMPLES = {
    "form_feed": "alpha\nbeta\x0cstill beta\ngamma\n",
    "u2028": "alpha\nbeta\u2028still beta\ngamma\n",
    "nel": "alpha\nbeta\x85still beta\ngamma\n",
}


# ── the report ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(SAMPLES))
@pytest.mark.asyncio
async def test_a_full_slice_returns_the_file_unchanged(tmp_path, memory_get, name):
    """`memory_get(path, from=1)` must not differ from `memory_get(path)`."""
    content = SAMPLES[name]
    (tmp_path / "MEMORY.md").write_text(content, encoding="utf-8")

    whole = await memory_get(path="MEMORY.md")
    sliced = await memory_get(path="MEMORY.md", **{"from": 1})

    assert whole == content
    assert sliced == whole, "from=1 must not rewrite the text the file holds"


@pytest.mark.parametrize("name", sorted(SAMPLES))
@pytest.mark.asyncio
async def test_the_line_count_matches_the_newline_count(tmp_path, memory_get, name):
    """Three logical lines in every sample — `splitlines()` saw four."""
    content = SAMPLES[name]
    (tmp_path / "MEMORY.md").write_text(content, encoding="utf-8")
    assert len(split_lines(content)) == 3

    fourth = await memory_get(path="MEMORY.md", **{"from": 4})
    assert fourth == "", f"{name}: there is no fourth line"


@pytest.mark.asyncio
async def test_a_requested_line_is_the_line_grep_search_would_name(tmp_path, memory_get):
    """The cross-tool agreement #3176 is about, on a memory file."""
    content = SAMPLES["form_feed"]
    (tmp_path / "MEMORY.md").write_text(content, encoding="utf-8")

    # `grep_search` numbers with split_lines; line 3 is "gamma".
    assert split_lines(content)[2] == "gamma"

    third = await memory_get(path="MEMORY.md", **{"from": 3, "lines": 1})
    assert third.rstrip("\n") == "gamma"


# ── the visible behaviour change ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_slice_now_keeps_its_line_terminators(tmp_path, memory_get):
    """The one change an ordinary LF file sees, stated rather than hidden.

    Rejoining on ``"\n"`` dropped the terminator of the last line in the range,
    so a slice was never quite the text the file holds. Keeping the endings is
    what makes a full-file slice equal the file; the cost is that every range
    now ends with the newline it ends with on disk.
    """
    (tmp_path / "MEMORY.md").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    assert (await memory_get(path="MEMORY.md", **{"from": 2, "lines": 2})) == "two\nthree\n"
    assert (await memory_get(path="MEMORY.md", **{"lines": 1})) == "one\n"
    assert (await memory_get(path="MEMORY.md")) == "one\ntwo\nthree\nfour\n"


@pytest.mark.asyncio
async def test_a_file_with_no_final_newline_gains_none(tmp_path, memory_get):
    """Guard: the endings are copied, not added."""
    (tmp_path / "MEMORY.md").write_text("one\ntwo", encoding="utf-8")
    assert (await memory_get(path="MEMORY.md", **{"from": 2})) == "two"


@pytest.mark.asyncio
async def test_the_compatibility_alias_behaves_the_same(tmp_path, memory_get):
    (tmp_path / "MEMORY.md").write_text("one\ntwo\nthree\n", encoding="utf-8")

    by_alias = await memory_get(path="MEMORY.md", from_line=2, lines=1)
    by_name = await memory_get(path="MEMORY.md", **{"from": 2, "lines": 1})
    assert by_alias == by_name == "two\n"


@pytest.mark.asyncio
async def test_a_range_past_the_end_is_empty_not_an_error(tmp_path, memory_get):
    (tmp_path / "MEMORY.md").write_text("one\ntwo\n", encoding="utf-8")
    assert (await memory_get(path="MEMORY.md", **{"from": 99})) == ""
