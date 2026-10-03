"""Issue #3571: memory.show reported a totalLines its own fromLine could not reach.

The two branches of ``_read_memory_content`` counted differently. Without
``fromLine`` it used ``str.splitlines()``, which breaks on eleven characters;
with ``fromLine`` it iterated the file handle, which in universal-newline
mode ends a line on ``\\n``, ``\\r\\n`` and a lone ``\\r`` and on nothing else.
So a memory file carrying a form feed, a vertical tab, NEL or U+2028/9 was
summarised as having more lines than the pager could address, and one
carrying a lone ``\\r`` as having fewer.

Memory files quote tool output, so both arrive routinely: a form feed is the
conventional page break in a Python source, and a lone ``\\r`` is what a
progress bar leaves behind.

#3176 settled the newline-only rule for ``read_file``/``grep_search`` and
#3369 applied it to ``memory_get``, the agent-facing tool. ``memory.show`` is
the gateway RPC the web UI calls and has its own implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.gateway.rpc_memory import _read_memory_content
from agentos.tools.builtin._lines import split_lines

FORM_FEED = "\x0c"
VERTICAL_TAB = "\x0b"
CARRIAGE_RETURN = "\r"
NEL = "\u0085"
LINE_SEPARATOR = " "
PARAGRAPH_SEPARATOR = " "

BODIES = [
    pytest.param("alpha\nbeta\ngamma\ndelta\n", id="plain"),
    pytest.param(f"alpha\nbeta{FORM_FEED}gamma\ndelta\n", id="form-feed"),
    pytest.param(f"alpha\nbeta{VERTICAL_TAB}gamma\ndelta\n", id="vertical-tab"),
    pytest.param(f"alpha\nbeta{CARRIAGE_RETURN}gamma\ndelta\n", id="lone-cr"),
    pytest.param(f"alpha\nbeta{NEL}gamma\ndelta\n", id="nel"),
    pytest.param(f"alpha\nbeta{LINE_SEPARATOR}gamma\ndelta\n", id="u2028"),
    pytest.param(f"alpha\nbeta{PARAGRAPH_SEPARATOR}gamma\ndelta\n", id="u2029"),
    pytest.param("alpha\r\nbeta\r\ngamma\r\n", id="crlf"),
    pytest.param("no trailing newline", id="no-trailing-newline"),
    pytest.param("", id="empty"),
]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "note.md"
    path.write_text(text, encoding="utf-8", newline="")
    return path


# ── the issue's reproduction ───────────────────────────────────────────────


@pytest.mark.parametrize("body", BODIES)
def test_the_two_branches_report_the_same_total(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path, body)

    _, total_whole, _ = _read_memory_content(path, from_line=None, lines=None)
    _, total_paged, _ = _read_memory_content(path, from_line=1, lines=10_000)

    assert total_whole == total_paged


@pytest.mark.parametrize("body", BODIES)
def test_the_total_is_the_newline_count_every_other_tool_uses(
    tmp_path: Path, body: str
) -> None:
    """Pins the rule, not the branch: ``split_lines`` is what ``read_file``,
    ``grep_search``, ``apply_patch`` and ``memory_get`` all count by."""
    path = _write(tmp_path, body)

    _, total, _ = _read_memory_content(path, from_line=None, lines=None)

    assert total == len(split_lines(body))


@pytest.mark.parametrize("body", BODIES)
def test_every_line_the_total_promises_can_be_fetched(tmp_path: Path, body: str) -> None:
    """The symptom: the UI was told line N existed and got nothing back."""
    path = _write(tmp_path, body)
    _, total, _ = _read_memory_content(path, from_line=None, lines=None)

    for line_no in range(1, total + 1):
        text, count, _ = _read_memory_content(path, from_line=line_no, lines=1)
        assert count == 1, f"line {line_no} of {total} came back empty"
        assert text == split_lines(body)[line_no - 1]


def test_the_form_feed_case_from_the_issue(tmp_path: Path) -> None:
    path = _write(tmp_path, f"alpha\nbeta{FORM_FEED}gamma\ndelta\n")

    _, total, _ = _read_memory_content(path, from_line=None, lines=None)
    text, count, _ = _read_memory_content(path, from_line=2, lines=1)

    assert total == 3
    assert (text, count) == (f"beta{FORM_FEED}gamma", 1)


def test_a_lone_cr_is_content_not_a_line_break(tmp_path: Path) -> None:
    """The other direction: the handle used to end a line here and the count
    above did not, so the pager found a line the summary had not counted."""
    path = _write(tmp_path, f"alpha\nprogress 10%{CARRIAGE_RETURN}progress 20%\n")

    _, total, _ = _read_memory_content(path, from_line=None, lines=None)
    text, _, _ = _read_memory_content(path, from_line=2, lines=1)

    assert total == 2
    assert text == f"progress 10%{CARRIAGE_RETURN}progress 20%"


# ── what must not change ───────────────────────────────────────────────────


def test_a_plain_file_is_counted_and_paged_as_before(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha\nbeta\ngamma\ndelta\n")

    body, total, truncated = _read_memory_content(path, from_line=None, lines=None)

    assert (body, total, truncated) == ("alpha\nbeta\ngamma\ndelta\n", 4, False)


def test_a_range_still_selects_that_range(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha\nbeta\ngamma\ndelta\n")

    text, count, truncated = _read_memory_content(path, from_line=2, lines=2)

    assert (text, count, truncated) == ("beta\ngamma", 2, False)


def test_a_crlf_file_still_comes_back_without_the_carriage_returns(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha\r\nbeta\r\n")

    text, count, _ = _read_memory_content(path, from_line=1, lines=2)

    assert (text, count) == ("alpha\nbeta", 2)


def test_an_empty_file_is_zero_lines(tmp_path: Path) -> None:
    path = _write(tmp_path, "")

    assert _read_memory_content(path, from_line=None, lines=None) == ("", 0, False)


def test_a_from_line_past_the_end_returns_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path, "alpha\nbeta\n")

    assert _read_memory_content(path, from_line=9, lines=1) == ("", 0, False)


def test_a_large_file_is_still_reported_as_truncated(tmp_path: Path) -> None:
    path = _write(tmp_path, "x" * 200_000)

    body, _, truncated = _read_memory_content(path, from_line=None, lines=None)

    assert truncated is True
    assert len(body) < 200_000
