"""Line-buffered markdown rendering for the chat token stream.

Covers:
  * block styles — headings, quotes (+lazy continuation), rules, lists,
    tables, fenced code,
  * inline styles — bold, italic, strike, inline code, links,
  * streaming invariants — deltas held until newline, fence lines hidden,
    trailing partial emitted by ``flush``, raw buffer untouched,
  * safety — model text with Rich-like ``[brackets]`` cannot inject markup,
    ANSI-stripping happens before the markdown pass,
  * ``NO_COLOR`` / non-color console passthrough,
  * integration through ``StreamingRenderer`` sync + async paths.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

import agentos.cli.tui.terminal.markdown_stream as md_module
from agentos.cli.tui.terminal.markdown_stream import (
    MarkdownStreamRenderer,
    markdown_enabled,
    render_markup_to_ansi,
)
from agentos.cli.tui.terminal.stream import StreamingRenderer


@pytest.fixture
def tty_console(monkeypatch: pytest.MonkeyPatch) -> Console:
    """Force the module console to a color-capable terminal."""
    fake = Console(file=io.StringIO(), width=100, force_terminal=True, color_system="truecolor")
    monkeypatch.setattr(md_module, "console", fake)
    return fake


def _render_all(chunks: list[str], *, enabled: bool = True) -> str:
    r = MarkdownStreamRenderer(enabled=enabled)
    out = "".join(r.feed(c) for c in chunks)
    return out + r.flush()


# ---------------------------------------------------------------------------
# Block styles
# ---------------------------------------------------------------------------


def test_heading_styled() -> None:
    out = _render_all(["# Hello\n"])
    assert "[bold #CCFF00]Hello[/]" in out


def test_quote_and_lazy_continuation() -> None:
    out = _render_all(["> first\nsecond\n\n"])
    assert "[dim]▎ first[/]" in out
    assert "[dim]▎ second[/]" in out


def test_rule_replaces_dashes() -> None:
    out = _render_all(["---\n"])
    assert "─" in out and "---" not in out


def test_list_marker_accented() -> None:
    out = _render_all(["- one\n  - two\n1. three\n"])
    assert "[#CCFF00]-[/] one" in out
    assert "[#CCFF00]1.[/] three" in out


def test_table_pipes_dimmed() -> None:
    out = _render_all(["| a | b |\n"])
    assert "[#6E9000]|[/]" in out
    assert " a " in out


# ---------------------------------------------------------------------------
# Table block rendering
# ---------------------------------------------------------------------------


def _display_lines(markup: str) -> list[str]:
    """Strip Rich markup tags, returning the display text per line.

    Blank lines are kept (paragraph spacing is part of the contract);
    only the trailing artifact of the final newline is dropped.
    """
    import re as _re

    plain = _re.sub(r"\[[^\]]*\]", "", markup)
    lines = plain.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def test_table_columns_aligned() -> None:
    out = _render_all(["| a | bb |\n|---|---|\n| ccc | d |\n"])
    lines = _display_lines(out)
    # header, separator, one body row
    assert len(lines) == 3
    widths = {len(ln) for ln in lines}
    assert len(widths) == 1, f"ragged table lines: {lines!r}"


def test_table_header_bold_and_separator_dimmed() -> None:
    out = _render_all(["| H |\n|---|\n| x |\n"])
    assert "[bold]H" in out
    assert "[#6E9000]─" in out


def test_table_bold_full_span_cell_styled_with_padding() -> None:
    out = _render_all(["| **one** | b |\n|---|---|\n| **two** | c |\n"])
    # Full-span bold cells keep bold across the padded width.
    assert "[bold]one" in out
    assert "[bold]two" in out
    lines = _display_lines(out)
    assert len({len(ln) for ln in lines}) == 1


def test_table_wraps_long_cells_within_console_width(
    tty_console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tty_console, "width", 60)
    long_cell = "word " * 30
    out = _render_all([f"| id | desc |\n|---|---|\n| 1 | {long_cell.strip()} |\n"])
    from rich.cells import cell_len

    for ln in _display_lines(out):
        assert cell_len(ln) <= 60, f"line exceeds console width: {ln!r}"


def test_table_alignment_markers() -> None:
    out = _render_all(["| l | r |\n|:---|---:|\n| a | b |\n"])
    lines = _display_lines(out)
    body = lines[2]
    # Left column pads right of content; right column pads left of content.
    assert "| a " in body
    assert " b |" in body and body.index(" b") > body.index("| a")


def test_table_ragged_rows_padded() -> None:
    out = _render_all(["| a | b | c |\n|---|---|---|\n| x | y |\n"])
    lines = _display_lines(out)
    assert len({len(ln) for ln in lines}) == 1


def test_table_without_separator_falls_back_to_dim_pipes() -> None:
    out = _render_all(["| not | a | table |\n"])
    assert "[#6E9000]|[/]" in out
    assert "─" not in out


def test_table_block_closes_before_following_text() -> None:
    out = _render_all(["| a |\n|---|\n| b |\n", "after\n"])
    lines = _display_lines(out)
    assert lines[-1] == "after"
    # Table rendered as a unit before the trailing text.
    assert any("─" in ln for ln in lines[:-1])


def test_unclosed_table_flushed_at_turn_end() -> None:
    r = MarkdownStreamRenderer(enabled=True)
    out = r.feed("| a |\n|---|\n| b |\n")
    # Still buffered — nothing emitted yet beyond completed non-table lines.
    out += r.flush()
    assert "─" in out
    assert "b" in out


# ---------------------------------------------------------------------------
# Paragraph spacing (regression: blank lines must survive the table buffer)
# ---------------------------------------------------------------------------


def test_blank_lines_separate_paragraphs() -> None:
    out = _render_all(["first para\n", "\n", "second para\n"])
    lines = _display_lines(out)
    assert lines == ["first para", "", "second para"]


def test_blank_line_after_heading_preserved() -> None:
    out = _render_all(["# H\n", "\n", "body\n"])
    lines = _display_lines(out)
    assert lines[1] == ""


def test_blank_lines_around_fenced_code() -> None:
    out = _render_all(["text\n", "\n", "```\n", "x = 1\n", "```\n", "\n", "after\n"])
    lines = _display_lines(out)
    # Source blank + hidden fence marker each contribute one display line,
    # so the code block sits in a double-spaced well: text, blank, blank,
    # code, blank, blank, after.
    assert lines == ["text", "", "", "x = 1", "", "", "after"]


def test_blank_line_before_table_preserved_and_table_not_glued() -> None:
    out = _render_all(["intro\n", "\n", "| a |\n|---|\n| b |\n", "\n", "after\n"])
    lines = _display_lines(out)
    assert lines[0] == "intro"
    assert lines[1] == ""
    # table block (3 rows) then blank then text
    assert lines[5] == ""
    assert lines[6] == "after"


def test_table_cjk_cells_align_by_display_width() -> None:
    out = _render_all(["| 名前 | val |\n|---|---|\n| 世界 | 1 |\n"])
    from rich.cells import cell_len

    lines = _display_lines(out)
    widths = {cell_len(ln) for ln in lines}
    assert len(widths) == 1, f"CJK columns misaligned: {lines!r}"


# ---------------------------------------------------------------------------
# Fenced code
# ---------------------------------------------------------------------------


def test_fence_streams_body_hides_markers() -> None:
    out = _render_all(["```py\n", "x = 1\n", "y = 2\n", "```\n"])
    assert "```" not in out
    assert "[#DDFF66 on #1a1a1a]x = 1[/]" in out
    assert "[#DDFF66 on #1a1a1a]y = 2[/]" in out


def test_fence_body_is_not_inline_processed() -> None:
    out = _render_all(["```\n", "a **not bold** `raw`\n", "```\n"])
    assert "**not bold**" in out
    assert "[bold]" not in out.replace("[bold #DDFF66 on #1a1a1a]", "")


def test_tilde_fence_supported() -> None:
    out = _render_all(["~~~\n", "code\n", "~~~\n"])
    assert "~~~" not in out
    assert "[#DDFF66 on #1a1a1a]code[/]" in out


def test_unclosed_fence_flushes_as_code() -> None:
    out = _render_all(["```\n", "partial"])
    assert "[#DDFF66 on #1a1a1a]partial[/]" in out


# ---------------------------------------------------------------------------
# Inline styles
# ---------------------------------------------------------------------------


def test_inline_bold_italic_strike_code() -> None:
    out = _render_all(["a **b** *i* ~~s~~ `c`\n"])
    assert "[bold]b[/]" in out
    assert "[italic]i[/]" in out
    assert "[strike]s[/]" in out
    assert "[bold #DDFF66]c[/]" in out


def test_inline_link_renders_text_plus_dim_url() -> None:
    out = _render_all(["see [docs](https://example.com)\n"])
    assert "[underline #DDFF66]docs[/]" in out
    assert "[dim](https://example.com)[/]" in out


def test_model_brackets_cannot_inject_markup(tty_console: Console) -> None:
    out = _render_all(["literal [red]not a style[/red]\n"])
    ansi = render_markup_to_ansi(out)
    # No red SGR was emitted for the injected tag.
    assert "\x1b[31m" not in ansi and "38;2" not in ansi.split("literal")[0]
    assert "literal [red]not a style[/red]" in ansi


# ---------------------------------------------------------------------------
# Streaming invariants
# ---------------------------------------------------------------------------


def test_partial_line_held_until_newline() -> None:
    r = MarkdownStreamRenderer(enabled=True)
    assert r.feed("hello **bo") == ""
    assert r.feed("ld** wor") == ""
    out = r.feed("ld\n")
    assert "[bold]bold[/]" in out
    assert out.endswith("\n")


def test_flush_emits_trailing_partial() -> None:
    r = MarkdownStreamRenderer(enabled=True)
    r.feed("tail **b**")
    out = r.flush()
    assert "[bold]b[/]" in out
    assert not out.endswith("\n")


def test_disabled_passthrough() -> None:
    raw = "# H\n**b** `c`\n"
    out = _render_all([raw], enabled=False)
    assert out == raw


# ---------------------------------------------------------------------------
# markdown_enabled + render_markup_to_ansi
# ---------------------------------------------------------------------------


def test_markdown_enabled_respects_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert markdown_enabled() is False


def test_render_markup_to_ansi_emits_sgr(tty_console: Console) -> None:
    ansi = render_markup_to_ansi("[bold]hi[/]")
    assert "\x1b[1m" in ansi
    assert ansi.endswith("\x1b[0m")


def test_render_markup_soft_wrap_does_not_insert_newlines(tty_console: Console) -> None:
    long_line = "[bold]" + "x" * 500 + "[/]"
    ansi = render_markup_to_ansi(long_line)
    assert "\n" not in ansi


# ---------------------------------------------------------------------------
# StreamingRenderer integration
# ---------------------------------------------------------------------------


def test_streaming_renderer_styles_and_keeps_raw_buffer(
    tty_console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[str] = []
    monkeypatch.setattr(
        "agentos.cli.tui.terminal.stream.StreamingRenderer._write_payload",
        lambda self, payload: written.append(payload),
    )
    r = StreamingRenderer(title="agentos")
    r.append_text("# Head\n")
    r.append_text("body **bold**\n")
    r.finalize()

    joined = "".join(written)
    # Styled output reached the terminal…
    assert "\x1b[" in joined
    # …but the raw buffer keeps the markdown source for /save etc.
    assert r.buffer == "# Head\nbody **bold**\n"


@pytest.mark.asyncio
async def test_streaming_renderer_async_path_styles(
    tty_console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[str] = []

    async def _fake_awrite(self, payload: str) -> None:  # type: ignore[no-untyped-def]
        written.append(payload)

    monkeypatch.setattr(
        "agentos.cli.tui.terminal.stream.StreamingRenderer._awrite_payload",
        _fake_awrite,
    )
    r = StreamingRenderer(title="agentos")
    await r.aappend_text("`code` here\n")
    await r.afinalize()

    joined = "".join(written)
    assert "\x1b[" in joined
    assert r.buffer == "`code` here\n"


def test_streaming_renderer_flushes_trailing_partial_on_finalize(
    tty_console: Console, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[str] = []
    monkeypatch.setattr(
        "agentos.cli.tui.terminal.stream.StreamingRenderer._write_payload",
        lambda self, payload: written.append(payload),
    )
    r = StreamingRenderer(title="agentos")
    r.append_text("no newline **b**")
    r.finalize()

    joined = "".join(written)
    # The trailing partial line was rendered (bold SGR present) and the
    # line was terminated before the footer meta line.
    assert "\x1b[1m" in joined
    assert "no newline \x1b[1mb\x1b[0m\n" in joined
