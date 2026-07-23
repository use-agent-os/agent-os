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
    # CI runners and agent shells may export NO_COLOR=1 / TERM=dumb. Those
    # ambient values must not override a fixture whose purpose is explicitly
    # to model a color-capable, width-controlled TTY.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    fake = Console(
        file=io.StringIO(),
        width=100,
        height=25,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
    )
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


# ---------------------------------------------------------------------------
# Think blocks (<think>…</think> from reasoning models)
# ---------------------------------------------------------------------------


def test_think_tags_hidden_and_body_dim_italic() -> None:
    out = _render_all(["<think>\n", "reasoning here\n", "</think>\n"])
    assert "<think>" not in out and "</think>" not in out
    assert "[dim italic]reasoning here[/]" in out
    assert "▎" in out


def test_think_body_does_not_compete_with_reply() -> None:
    out = _render_all(["<think>\n", "drafting\n", "</think>\n", "answer **bold**\n"])
    # Think body carries no accent color; the reply keeps its styling.
    think_part, _, reply_part = out.partition("[/]\n\n")  # end of think body
    assert "#CCFF00" not in think_part and "#DDFF66" not in think_part
    assert "[bold]bold[/]" in reply_part


def test_think_single_line_form() -> None:
    out = _render_all(["<think>quick thought</think>\n"])
    assert "<think>" not in out
    assert "[dim italic]quick thought[/]" in out


def test_empty_think_block_produces_no_body_lines() -> None:
    out = _render_all(["<think>\n", "</think>\n", "after\n"])
    lines = _display_lines(out)
    assert lines == ["", "", "after"]


def test_think_stray_angle_bracket_closes_block() -> None:
    """A bare ``<>`` line (mangled ``</think>``) must not leave the stream
    stuck in think state forever."""
    out = _render_all(["<think>\n", "reasoning\n", "<>\n", "real answer\n"])
    assert "[dim italic]reasoning[/]" in out
    assert "[dim italic]real answer[/]" not in out
    assert "real answer" in _display_lines(out)[-1]


def test_think_blank_line_keeps_bar_but_no_text() -> None:
    out = _render_all(["<think>\n", "first\n", "\n", "second\n", "</think>\n"])
    lines = _display_lines(out)
    # Layout: blank (hidden open tag), bar+text, bare bar, bar+text,
    # blank (hidden close tag).
    assert lines[0] == ""
    assert "▎" in lines[1] and "first" in lines[1]
    assert lines[2] == "▎"
    assert "▎" in lines[3] and "second" in lines[3]
    assert lines[4] == ""


def test_think_inside_fence_stays_literal_code() -> None:
    out = _render_all(["```\n", "<think>\n", "```\n"])
    assert "[#DDFF66 on #1a1a1a]<think>[/]" in out


def test_unclosed_think_flushes_dim() -> None:
    out = _render_all(["<think>\n", "trailing thought"])
    assert "[dim italic]trailing thought[/]" in out


def test_think_state_resets_between_blocks() -> None:
    r = MarkdownStreamRenderer(enabled=True)
    out = r.feed("<think>\n")
    out += r.feed("one\n")
    out += r.feed("</think>\n")
    out += r.feed("main content\n")
    # A tool row (or any block-level interruption) lands here in the real
    # stream — that's what re-arms the think-opener guard.
    r.mark_boundary()
    out += r.feed("<think>\n")
    out += r.feed("two\n")
    out += r.feed("</think>\n")
    out += r.feed("more content\n")
    out += r.flush()
    assert "[dim italic]one[/]" in out
    assert "[dim italic]two[/]" in out
    assert "[dim italic]main content[/]" not in out
    assert "[dim italic]more content[/]" not in out


# ---------------------------------------------------------------------------
# Think-opener guard: quoted tags in prose must NOT open a think block
# ---------------------------------------------------------------------------


def test_bare_think_tag_after_prose_renders_literally() -> None:
    """The user asks "what tag is used for X?" — the agent's reply quotes
    the tag on its own line. It must stay visible, not open a think block."""
    out = _render_all(
        [
            "The tag to use is:\n",
            "<think>\n",
            "and the closing tag is\n",
            "</think>\n",
        ]
    )
    lines = _display_lines(out)
    assert "<think>" in lines
    assert "</think>" in lines
    assert "and the closing tag is" in lines
    assert "[dim italic]" not in out


def test_bare_think_tag_after_blank_prose_line_still_literal() -> None:
    """Even with a paragraph break before it, a quoted tag after prose is
    not a reasoning block (blanks do not re-arm the guard)."""
    out = _render_all(["some explanation\n", "\n", "<think>\n", "body\n"])
    lines = _display_lines(out)
    assert "<think>" in lines
    assert "[dim italic]" not in out


def test_single_line_think_after_prose_renders_literally() -> None:
    out = _render_all(["example format:\n", "<think>some content</think>\n"])
    lines = _display_lines(out)
    assert "<think>some content</think>" in lines


def test_think_opens_after_tool_row_boundary() -> None:
    """The genuine reasoning pattern: prose, tool call, then think."""
    r = MarkdownStreamRenderer(enabled=True)
    out = r.feed("Let me check the balance.\n")
    r.mark_boundary()  # tool row emitted at this point in the real stream
    out += r.feed("<think>\n")
    out += r.feed("user has 0.897 ETH\n")
    out += r.feed("</think>\n")
    out += r.flush()
    assert "[dim italic]user has 0.897 ETH[/]" in out
    assert "<think>" not in out


def test_think_opens_after_fence_close() -> None:
    out = _render_all(["```\n", "code\n", "```\n", "<think>\n", "reasoning\n", "</think>\n"])
    assert "[dim italic]reasoning[/]" in out


def test_think_tag_in_backticks_untouched() -> None:
    out = _render_all(["The tags are `<think>` and `</think>`.\n"])
    assert "[bold #DDFF66]<think>[/]" in out
    assert "[bold #DDFF66]</think>[/]" in out


def test_think_tag_question_full_flow() -> None:
    """End-to-end: a tag-explanation answer loses nothing."""
    r = MarkdownStreamRenderer(enabled=True)
    answer = (
        "The tag for code snippets is `<code>` or a fenced block:\n"
        "\n"
        "```\n"
        "<think>\n"
        "```\n"
        "\n"
        "The reasoning tag is <think> (rarely seen in prose).\n"
    )
    out = r.feed(answer) + r.flush()
    lines = _display_lines(out)
    assert any("<code>" in ln for ln in lines)
    # The fenced example stays literal code.
    assert "[#DDFF66 on #1a1a1a]<think>[/]" in out
    # The inline prose mention stays visible and undimmed.
    assert any("<think> (rarely seen in prose)." in ln for ln in lines)
    assert "[dim italic]" not in out


# ---------------------------------------------------------------------------
# Stray think-tag artifacts (bare ``<>`` / ``</>`` lines from the model)
# ---------------------------------------------------------------------------


def test_stray_angle_bracket_line_hidden_outside_think() -> None:
    out = _render_all(["some answer\n", "<>\n", "next line\n"])
    lines = _display_lines(out)
    assert lines == ["some answer", "", "next line"]


def test_stray_closing_angle_bracket_line_hidden() -> None:
    out = _render_all(["text\n", "</>\n"])
    lines = _display_lines(out)
    assert lines == ["text", ""]


def test_stray_artifact_between_think_blocks_hidden() -> None:
    """The observed pattern: ``</think>`` followed by an extra bare ``<>``
    line — neither may reach the screen."""
    out = _render_all(
        [
            "<think>\n",
            "reasoning\n",
            "</think>\n",
            "<>\n",
            "real answer\n",
        ]
    )
    lines = _display_lines(out)
    assert "<>" not in "".join(lines)
    assert lines[-1] == "real answer"


def test_inline_angle_brackets_in_prose_preserved() -> None:
    out = _render_all(["use Vec<> or a <> b syntax\n"])
    assert "use Vec&lt;&gt; or a &lt;&gt; b syntax" in _display_lines(out)[0] or (
        "use Vec<> or a <> b syntax" in _display_lines(out)[0]
    )


def test_angle_bracket_line_inside_fence_preserved() -> None:
    out = _render_all(["```\n", "<>\n", "```\n"])
    assert "[#DDFF66 on #1a1a1a]<>[/]" in out


def test_angle_bracket_in_table_cell_preserved() -> None:
    out = _render_all(["| a | b |\n|---|---|\n| <> | x |\n"])
    assert "<>" in "".join(_display_lines(out))


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
