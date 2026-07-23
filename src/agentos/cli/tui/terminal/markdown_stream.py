"""Incremental markdown renderer for the terminal chat stream.

The chat renderer writes sanitized model output straight to the terminal
(write-once, no ``Rich.Live`` re-render — see ``stream.py`` for why that
contract exists). This module layers a *line-buffered* markdown pass on
top of that contract:

  * Tokens are accumulated until a newline arrives, so every emitted line
    is final and never needs repainting.
  * Block-level constructs (``#``/``##``/``###`` headings, ``>`` quotes,
    ``---`` rules, fenced code blocks, tables, list items) are detected
    from the line prefix and styled with the brand palette.
  * Inline spans (``**bold**``, ``*italic*``, ``~~strike~~``, inline
    ``code``, links) are styled inside the line after block styling.
  * Fenced code blocks stream their body lines immediately in a uniform
    code style — no waiting for the closing fence, no syntax-highlight
    delay, no re-render. The fence markers themselves are hidden so the
    block reads as one continuous region.
  * ``NO_COLOR`` (or a non-color console) downgrades every transform to a
    plain-text passthrough so piped output stays greppable.

The renderer is stateful and single-use per assistant turn; create one
via ``MarkdownStreamRenderer(enabled=...)`` and feed deltas through
``feed``. ``flush`` must be called at end-of-turn to emit any trailing
partial line.
"""

from __future__ import annotations

import os
import re

from rich.cells import cell_len
from rich.markup import escape as _rich_escape

from agentos.cli.ui import (
    ACCENT,
    ACCENT_DIM,
    ACCENT_SOFT,
    console,
)

__all__ = ["MarkdownStreamRenderer", "markdown_enabled", "render_markup_to_ansi"]


# ---------------------------------------------------------------------------
# Style vocabulary (brand palette, terminal-safe)
# ---------------------------------------------------------------------------

_HEADING_STYLE = f"bold {ACCENT}"
_QUOTE_STYLE = "dim"
_RULE_STYLE = ACCENT_DIM
_CODE_STYLE = f"{ACCENT_SOFT} on #1a1a1a"
_INLINE_CODE_STYLE = f"bold {ACCENT_SOFT}"
_LIST_MARKER_STYLE = ACCENT
_BOLD_STYLE = "bold"
_ITALIC_STYLE = "italic"
_STRIKE_STYLE = "strike"
_LINK_STYLE = f"underline {ACCENT_SOFT}"
_LINK_URL_STYLE = "dim"
# Think blocks (<think>…</think> from reasoning models): deliberately
# *less* prominent than quotes — no accent color at all, just a near-gray
# bar and dim italic text, so the reasoning reads as background context
# and never competes with the actual reply.
_THINK_BAR_STYLE = "#3a3a3a"
_THINK_TEXT_STYLE = "dim italic"


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_RULE_RE = re.compile(r"^(?:\*\s*){3,}$|^(?:-\s*){3,}$|^(?:_\s*){3,}$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")

# Reasoning-model think tags. The block forms own a whole line; the
# single-line form wraps content inline (``<think>…</think>``).
_THINK_OPEN_RE = re.compile(r"^\s*<think>\s*$")
_THINK_CLOSE_RE = re.compile(r"^\s*</think>\s*$")
_THINK_INLINE_RE = re.compile(r"^\s*<think>(.*?)</think>\s*$")
# Stray think-tag artifacts: models occasionally emit a bare ``<>`` (or
# ``</>``) line — a mangled ``</think>`` — which must never reach the
# screen as literal text. The artifact is hidden in every state, and while
# a think block is open it also closes the block (without that valve,
# everything after would render dim forever). Matching is anchored to the
# whole line so inline occurrences in prose, tables, and code (``a <> b``)
# are left untouched; fenced code is checked before this and always stays
# literal anyway.
_STRAY_THINK_ARTIFACT_RE = re.compile(r"^\s*</?>\s*$")


def _styled(text: str, style: str) -> str:
    return f"[{style}]{text}[/]"


# ---------------------------------------------------------------------------
# Inline span rendering
# ---------------------------------------------------------------------------


def _render_inline(text: str) -> str:
    """Apply inline markdown styling to a single (already block-stripped)
    line of text.

    The line is tokenized into *spans* on the raw text first (links, then
    inline code, bold, italic, strike — earlier claims win on overlap).
    Plain segments are Rich-escaped; styled segments wrap their escaped
    content. Building markup this way (instead of escaping the whole line
    up front) keeps the ``\\[`` escape sequences from colliding with the
    tag-insertion regexes, which previously produced unbalanced markup
    for e.g. ``[text](url)`` links.
    """
    spans: list[tuple[int, int, str]] = []

    def _claim(pattern: re.Pattern[str], make) -> None:  # type: ignore[no-untyped-def]
        for m in pattern.finditer(text):
            if any(s < m.end() and m.start() < e for s, e, _ in spans):
                continue
            spans.append((m.start(), m.end(), make(m)))

    _claim(
        _LINK_RE,
        lambda m: (
            f"[{_LINK_STYLE}]{_rich_escape(m.group(1))}[/]"
            f" [{_LINK_URL_STYLE}]({_rich_escape(m.group(2))})[/]"
        ),
    )
    _claim(
        _INLINE_CODE_RE,
        lambda m: f"[{_INLINE_CODE_STYLE}]{_rich_escape(m.group(1))}[/]",
    )
    _claim(
        _BOLD_RE,
        lambda m: f"[{_BOLD_STYLE}]{_rich_escape(m.group(1))}[/]",
    )
    _claim(
        _ITALIC_RE,
        lambda m: f"[{_ITALIC_STYLE}]{_rich_escape(m.group(1))}[/]",
    )
    _claim(
        _STRIKE_RE,
        lambda m: f"[{_STRIKE_STYLE}]{_rich_escape(m.group(1))}[/]",
    )

    spans.sort()
    out: list[str] = []
    pos = 0
    for start, end, replacement in spans:
        out.append(_rich_escape(text[pos:start]))
        out.append(replacement)
        pos = end
    out.append(_rich_escape(text[pos:]))
    return "".join(out)


# ---------------------------------------------------------------------------
# Block-level line rendering
# ---------------------------------------------------------------------------


def _render_code_line(line: str) -> str:
    """A line inside a fenced code block: uniform code style, escaped."""
    return _styled(_rich_escape(line) or " ", _CODE_STYLE)


def _render_think_line(line: str) -> str:
    """A line inside a ``<think>`` block: near-gray bar + dim italic text.

    Intentionally plainer than the quote style — the accent palette is
    reserved for the actual reply, so the reasoning stays visibly present
    but visually recessive.
    """
    bar = _styled("▎", _THINK_BAR_STYLE)
    if not line.strip():
        return bar
    return f"{bar} {_styled(_rich_escape(line), _THINK_TEXT_STYLE)}"


# ---------------------------------------------------------------------------
# Table block rendering
# ---------------------------------------------------------------------------
#
# A markdown table cannot be aligned row-by-row during streaming (column
# widths are not known until the last row arrives), so table lines are
# buffered while the block is open and rendered as a unit when it closes.
# This is the one intentional exception to the write-once contract, and it
# is the same trade-off every streaming terminal markdown renderer makes.

_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_TABLE_MIN_COL_WIDTH = 5
_TABLE_PAD = 1  # spaces on each side of a cell


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|") and line.rstrip().endswith("|")


def _split_table_row(line: str) -> list[str]:
    """Split a ``| a | b |`` row into trimmed cell strings."""
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _is_table_separator_row(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(_TABLE_SEPARATOR_CELL_RE.match(c) for c in cells)


def _parse_table_alignment(line: str, ncols: int) -> list[str]:
    """Parse the ``:---`` / ``:--:`` / ``---:`` separator row into per-column
    alignment (``left``/``center``/``right``)."""
    aligns: list[str] = []
    for cell in _split_table_row(line):
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        else:
            aligns.append("left")
    while len(aligns) < ncols:
        aligns.append("left")
    return aligns[:ncols]


def _cell_plain(text: str) -> str:
    """Strip inline markdown markers for width measurement."""
    out = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    out = _INLINE_CODE_RE.sub(lambda m: m.group(1), out)
    out = _BOLD_RE.sub(lambda m: m.group(1), out)
    out = _ITALIC_RE.sub(lambda m: m.group(1), out)
    out = _STRIKE_RE.sub(lambda m: m.group(1), out)
    return out


def _cell_width(text: str) -> int:
    """Display width of a cell with inline markdown removed (CJK-aware)."""
    return cell_len(_cell_plain(text))


def _full_span_style(text: str) -> str | None:
    """If the whole cell is one inline span (``**x**``, ``*x*``, ``~~x~~``,
    ```x```), return its style so the padded cell can be wrapped uniformly
    — this keeps alignment intact instead of leaving the padding unstyled.
    """
    for pattern, style in (
        (_INLINE_CODE_RE, _INLINE_CODE_STYLE),
        (_BOLD_RE, _BOLD_STYLE),
        (_ITALIC_RE, _ITALIC_STYLE),
        (_STRIKE_RE, _STRIKE_STYLE),
    ):
        m = pattern.fullmatch(text)
        if m:
            return style
    return None


def _render_cell_content(text: str, width: int, align: str, *, header: bool) -> str:
    """Render one table cell padded to ``width`` display columns.

    When the entire cell is a single inline span the span's style wraps the
    *padded* content so the alignment padding is styled too; otherwise
    inline spans are styled individually and padding is added as plain
    space around the rendered content.
    """
    natural = _cell_width(text)
    overflow = max(0, width - natural)
    if align == "right":
        left_pad, right_pad = overflow, 0
    elif align == "center":
        left_pad, right_pad = overflow // 2, overflow - overflow // 2
    else:
        left_pad, right_pad = 0, overflow
    lpad = " " * left_pad
    rpad = " " * right_pad

    span_style = _full_span_style(text)
    if span_style is not None:
        m = next(
            p.fullmatch(text)
            for p in (_INLINE_CODE_RE, _BOLD_RE, _ITALIC_RE, _STRIKE_RE)
            if p.fullmatch(text)
        )
        assert m is not None
        inner = lpad + _rich_escape(m.group(1)) + rpad
        rendered = f"[{span_style}]{inner}[/]"
    else:
        rendered = lpad + _render_inline(text) + rpad
    if header:
        return f"[bold]{rendered}[/]"
    return rendered


def _wrap_cell_text(text: str, width: int) -> list[str]:
    """Wrap a *plain* cell string to at most ``width`` display columns.

    Splits on spaces; a token longer than the width is hard-split by
    display columns. Returns at least one (possibly empty) line. Cells are
    wrapped on the plain text (markers stripped) so wrapping never cuts a
    markdown span in half — the wrapped lines render as plain text.
    """
    if width < 1:
        width = 1
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if cell_len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # Hard-split an over-long token by display columns.
        while cell_len(word) > width:
            cut = 0
            acc = 0
            for ch in word:
                w = cell_len(ch)
                if acc + w > width:
                    break
                acc += w
                cut += 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current or not lines:
        lines.append(current)
    return lines


def _allocate_table_widths(
    rows: list[list[str]],
    aligns: list[str],
    max_total: int,
) -> list[int]:
    """Compute per-column display widths, shrinking to fit ``max_total``.

    Natural widths win until the budget is exhausted; the widest columns
    are then shrunk one column at a time (never below
    ``_TABLE_MIN_COL_WIDTH``) so narrow columns keep their full width.
    """
    ncols = len(aligns)
    widths = [
        max(
            [_TABLE_MIN_COL_WIDTH]
            + [_cell_width(r[i]) for r in rows if i < len(r)]
        )
        for i in range(ncols)
    ]

    def total() -> int:
        # total = sum(widths) + padding (2 per cell) + separators (ncols+1)
        return sum(widths) + ncols * 2 * _TABLE_PAD + (ncols + 1)

    while total() > max_total:
        widest = max(range(ncols), key=lambda i: widths[i])
        if widths[widest] <= _TABLE_MIN_COL_WIDTH:
            break
        widths[widest] -= 1
    return widths


def _render_table_block(table_lines: list[str]) -> str:
    """Render a buffered table block as an aligned, styled unit.

    The block starts with a header row and separator row; remaining rows
    are body rows. Columns are width-allocated against the console width
    and cells are padded (alignment-aware) so every column lines up.
    """
    if len(table_lines) < 2:
        # Degenerate block (no separator row) — render as plain lines.
        return "\n".join(_render_table_line(ln) for ln in table_lines)

    header = _split_table_row(table_lines[0])
    ncols = len(header)
    aligns = _parse_table_alignment(table_lines[1], ncols)
    body = [
        _split_table_row(ln)
        for ln in table_lines[2:]
        if not _is_table_separator_row(ln)
    ]
    all_rows = [header, *body]

    # Budget: console width, minus a small safety margin so the table never
    # touches the terminal edge (which would cause an unwanted wrap).
    budget = max(40, console.width - 2)
    widths = _allocate_table_widths(all_rows, aligns, budget)

    pipe = f"[{_RULE_STYLE}]|[/]"
    pad = " " * _TABLE_PAD

    def _render_row(cells: list[str], *, is_header: bool) -> list[str]:
        # Normalize ragged rows to the column count.
        normalized = [(cells[i] if i < len(cells) else "") for i in range(ncols)]
        # Wrap any cell whose plain width exceeds its column budget.
        wrapped = [
            _wrap_cell_text(_cell_plain(normalized[i]), widths[i])
            if _cell_width(normalized[i]) > widths[i]
            else [normalized[i]]
            for i in range(ncols)
        ]
        height = max(len(w) for w in wrapped)
        out_lines: list[str] = []
        for sub in range(height):
            parts: list[str] = []
            for i in range(ncols):
                if sub < len(wrapped[i]):
                    cell_text = wrapped[i][sub]
                    # Wrapped continuation lines are plain (markers were
                    # stripped by the wrapper); style the header row only.
                    if len(wrapped[i]) > 1 and cell_len(_cell_plain(cell_text)) > widths[i]:
                        # A hard-split fragment — render plain.
                        natural = cell_len(_cell_plain(cell_text))
                        content = _rich_escape(cell_text) + " " * max(0, widths[i] - natural)
                    else:
                        content = _render_cell_content(
                            cell_text,
                            widths[i],
                            aligns[i],
                            header=is_header and len(wrapped[i]) == 1,
                        )
                    parts.append(pad + content + pad)
                else:
                    parts.append(pad + " " * widths[i] + pad)
            out_lines.append(pipe + pipe.join(parts) + pipe)
        return out_lines

    lines: list[str] = []
    lines.extend(_render_row(header, is_header=True))
    # Separator: dashes fill each column (padding included), dimmed.
    sep_parts = [
        _styled("─" * (widths[i] + 2 * _TABLE_PAD), _RULE_STYLE) for i in range(ncols)
    ]
    lines.append(pipe + pipe.join(sep_parts) + pipe)
    for row in body:
        lines.extend(_render_row(row, is_header=False))
    return "\n".join(lines)


def _render_table_line(line: str) -> str:
    """Fallback for a degenerate (unparseable) table line: dim the pipes."""
    stripped = line.rstrip()
    escaped = _rich_escape(stripped)
    escaped = escaped.replace("|", f"[{_RULE_STYLE}]|[/]")
    return escaped


def _render_list_line(line: str) -> str:
    m = _LIST_RE.match(line)
    if not m:
        return _render_inline(line)
    indent, marker, body = m.groups()
    styled_marker = f"[{_LIST_MARKER_STYLE}]{_rich_escape(marker)}[/]"
    return f"{_rich_escape(indent)}{styled_marker} {_render_inline(body)}"


def _render_quote_line(line: str) -> str:
    m = _QUOTE_RE.match(line)
    if not m:
        return _render_inline(line)
    body = m.group(1)
    return f"[{_QUOTE_STYLE}]▎ {_render_inline(body)}[/]"


def _render_heading_line(line: str) -> str:
    m = _HEADING_RE.match(line)
    if not m:
        return _render_inline(line)
    _hashes, body = m.groups()
    return _styled(_render_inline(body), _HEADING_STYLE)


def _render_rule_line(_line: str) -> str:
    # Replace any --- / *** / ___ run with a full-width brand rule. Keep it
    # short so narrow terminals don't wrap it.
    return _styled("─" * 40, _RULE_STYLE)


# ---------------------------------------------------------------------------
# Streaming state machine
# ---------------------------------------------------------------------------


class MarkdownStreamRenderer:
    """Line-buffered markdown renderer for one assistant turn.

    Feed sanitized model deltas through :meth:`feed`; each call returns the
    styled markup for any lines completed by that delta. Call :meth:`flush`
    at end-of-turn to emit the final partial line.

    When ``enabled`` is False every transform is bypassed and input is
    returned verbatim — this is the ``NO_COLOR`` / piped-output path.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._pending = ""
        self._in_fence = False
        # Inside a ``<think>`` block (reasoning models). Body lines render
        # dim-italic with a near-gray bar until the closing tag.
        self._in_think = False
        # Think-opener guard. A bare ``<think>`` line is only treated as a
        # reasoning block opener (a) before the first content line of the
        # turn, or (b) right after a block-level boundary (tool row, status
        # row, fence close, table block, previous think close). Genuine
        # reasoning streams only ever open think blocks in those positions;
        # a ``<think>`` line appearing after prose is the model *quoting*
        # the tag (e.g. answering "what tag is used for X?") and must
        # render literally instead of swallowing the reply.
        self._seen_content = False
        self._block_boundary = True
        # Blockquote continuation state: a `>` line opens a quote block;
        # consecutive non-empty lines keep it so multiline quotes style
        # uniformly. Cleared by a blank line or a non-quote block line.
        self._in_quote = False
        # Buffered table lines while a table block is open. Tables render
        # as a unit when the block closes (see _render_table_block).
        self._table_lines: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when the markdown transform is active.

        Callers must bypass *all* post-processing (including the
        markup→ANSI render) when this is False so raw text reaches the
        terminal byte-for-byte.
        """
        return self._enabled

    def feed(self, delta: str) -> str:
        """Consume a delta; return styled markup for completed lines."""
        if not self._enabled:
            return delta
        self._pending += delta
        out: list[str] = []
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            rendered = self._render_line(line)
            if rendered is None:
                # Table line buffered for the block render — emit nothing.
                continue
            # Empty string is a real display line (blank separator between
            # paragraphs, or a hidden fence marker): keep the newline so
            # paragraph spacing is preserved.
            out.append(rendered + "\n")
        return "".join(out)

    def mark_boundary(self) -> None:
        """Mark a block-level boundary in the prose stream.

        Called by the streaming renderer whenever a non-markdown payload
        (tool row, status row) is emitted inline. Genuine think blocks only
        ever open right after such boundaries (or at turn start), so this
        is what lets the opener guard accept them while rejecting
        ``<think>`` lines quoted inside prose.
        """
        self._block_boundary = True

    def flush(self) -> str:
        """Emit any trailing partial line at end-of-turn."""
        if not self._enabled:
            pending, self._pending = self._pending, ""
            return pending
        out: list[str] = []
        if self._pending:
            line, self._pending = self._pending, ""
            rendered = self._render_line(line)
            if rendered is not None and rendered:
                out.append(rendered)
        # A table block that never saw its closing boundary still renders.
        table_out = self._flush_table()
        if table_out:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(table_out)
        return "".join(out)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _flush_table(self) -> str:
        """Render and clear any buffered table block."""
        if not self._table_lines:
            return ""
        lines, self._table_lines = self._table_lines, []
        # A table block is a block-level boundary: a think block opening
        # right after it is a genuine reasoning block, not a quoted tag.
        self._block_boundary = True
        return _render_table_block(lines)

    def _can_open_think(self) -> bool:
        """Think-opener guard: accept a bare ``<think>`` line only at turn
        start or right after a block-level boundary (see ``__init__``)."""
        return self._block_boundary or not self._seen_content

    def _render_line(self, line: str) -> str | None:
        """Render one logical line, maintaining the think-opener guard flags.

        Returns ``None`` when the line was buffered for a table block
        (emit nothing), otherwise the display text for the line — which
        may be an empty string for blank separator lines and hidden fence
        markers (the caller still emits the newline so paragraph spacing
        is preserved).
        """
        was_in_think = self._in_think
        rendered = self._render_line_inner(line)
        stripped = line.strip()
        if not stripped or rendered is None:
            # Blank lines and buffered table rows leave the guard flags
            # untouched: a blank line after prose must NOT re-arm the
            # think-opener guard (that's the quoted-tag case).
            return rendered
        if was_in_think or self._in_think:
            # Think body, an accepted opener, or a genuine close — none of
            # these are prose content. A close re-arms the boundary so a
            # following think block opens.
            if was_in_think and not self._in_think:
                self._block_boundary = True
            return rendered
        if _FENCE_RE.match(line) or _STRAY_THINK_ARTIFACT_RE.match(line):
            self._block_boundary = True
            return rendered
        # A genuine content line: the turn has prose and the boundary is
        # gone, so a following bare ``<think>`` is quoted text.
        self._seen_content = True
        self._block_boundary = False
        return rendered

    def _render_line_inner(self, line: str) -> str | None:
        # Fence open/close toggles state; the fence line itself is hidden
        # so the block reads as one continuous region.
        if _FENCE_RE.match(line):
            self._in_fence = not self._in_fence
            table_out = self._flush_table()
            return (table_out + "\n") if table_out else ""
        if self._in_fence:
            return _render_code_line(line)
        # Stray think-tag artifacts (a bare ``<>`` / ``</>`` line) are
        # hidden in every state; inside a think block they also close it.
        # Checked after fences so code stays literal, and anchored to the
        # whole line so prose/table content like ``a <> b`` is untouched.
        if _STRAY_THINK_ARTIFACT_RE.match(line):
            if self._in_think:
                self._in_think = False
            return ""
        # Think blocks: tags own their line and are hidden; body lines
        # render dim-italic with a near-gray bar. Checked after fences so
        # a ``<think>`` inside a code fence stays literal code. The opener
        # guard rejects tags quoted inside prose (they render literally).
        if self._can_open_think():
            inline_think = _THINK_INLINE_RE.match(line)
            if inline_think:
                return _render_think_line(inline_think.group(1))
            if _THINK_OPEN_RE.match(line):
                self._in_think = True
                table_out = self._flush_table()
                return (table_out + "\n") if table_out else ""
        if self._in_think:
            if _THINK_CLOSE_RE.match(line):
                self._in_think = False
                return ""
            return _render_think_line(line)
        # Table lines buffer until the block closes.
        if _is_table_line(line):
            self._table_lines.append(line)
            return None
        table_out = self._flush_table()
        prefix = (table_out + "\n") if table_out else ""
        stripped = line.strip()
        if not stripped:
            # Blank line ends any open quote block.
            self._in_quote = False
            return prefix
        if _RULE_RE.match(stripped):
            self._in_quote = False
            return prefix + _render_rule_line(line)
        if _HEADING_RE.match(line):
            self._in_quote = False
            return prefix + _render_heading_line(line)
        if _QUOTE_RE.match(line):
            self._in_quote = True
            return prefix + _render_quote_line(line)
        # Lazily-wrapped quote continuation: inside a quote block, treat a
        # plain-text line as a continuation; a list/table/heading line
        # breaks out of the quote.
        if self._in_quote and not _LIST_RE.match(line) and not line.lstrip().startswith("|"):
            return prefix + _render_quote_line(f"> {line}")
        self._in_quote = False
        if _LIST_RE.match(line):
            return prefix + _render_list_line(line)
        return prefix + _render_inline(line)


# ---------------------------------------------------------------------------
# Module-level helpers used by stream.py
# ---------------------------------------------------------------------------


def markdown_enabled() -> bool:
    """True when terminal markdown styling should be applied.

    Disabled by ``NO_COLOR`` or when the console has no color system
    (piped output, dumb terminal), so the raw stream stays greppable.
    """
    if os.environ.get("NO_COLOR"):
        return False
    return console.color_system is not None


def render_markup_to_ansi(markup: str) -> str:
    """Render a Rich markup string to ANSI, with no Rich-side wrapping.

    Uses ``soft_wrap=True`` so the terminal (or prompt_toolkit pane) owns
    line wrapping — the same contract the raw stream relied on before this
    renderer existed. ``highlight=False`` and ``emoji=False`` keep the
    model's text byte-faithful (no auto-linkification, no ``:emoji:``
    expansion) so the only styling is what this module explicitly added.
    """
    if not markup:
        return ""
    with console.capture() as capture:
        console.print(markup, end="", soft_wrap=True, highlight=False, emoji=False)
    return capture.get()
