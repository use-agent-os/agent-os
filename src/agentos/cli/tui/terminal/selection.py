"""Mouse-driven text selection for the full-screen transcript pane.

The full-screen chat surface enables terminal mouse reporting so the
transcript pane can handle wheel scrolls. That reporting also blocks the
emulator's native drag-select, which previously made it impossible to
copy chat text. This module adds an in-app selection model:

  * ``MOUSE_DOWN`` (left) on the transcript starts a selection.
  * ``MOUSE_MOVE`` extends it.
  * ``MOUSE_UP`` finalizes the selection and copies the plain text
    (ANSI-stripped, width-aware slice) to the system clipboard.

The pane re-renders the transcript with a ``reverse`` style applied to
the selected column ranges so the user sees the highlight. Scroll events
continue to pass through to the existing wheel handler.

Coordinates: prompt-toolkit's Window mouse wrapper already converts the
absolute screen ``(x, y)`` into content ``(row, col)`` coordinates, where
``row`` is the logical transcript line index and ``col`` is the display
column within that line (CJK-aware). Selection math works in that
content-coordinate space, so no additional scroll math is required here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.utils import get_cwidth

__all__ = [
    "Selection",
    "extract_selection_text",
    "highlight_fragments",
]


# Match CSI/OSC/DCS/2-char escape sequences. Identical to the pattern in
# ``stream.py`` — kept as a local copy so this module does not import the
# streaming renderer (avoiding the Rich/UI import chain at startup).
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|P[^\x1b]*\x1b\\"  # DCS ... ST
    r"|[@-Z\\-_]"  # 2-char ESC (RIS, IND, NEL, ...)
    r")"
)


@dataclass(frozen=True)
class Selection:
    """A contiguous span across transcript logical lines.

    ``anchor``/``cursor`` are ``(row, col)`` content-coordinate tuples
    where ``row`` indexes ``transcript.split("\\n")`` and ``col`` is a
    display column (CJK-aware). ``anchor`` is where the drag started;
    ``cursor`` is where the mouse is now. The two may be in either order.
    """

    anchor: tuple[int, int]
    cursor: tuple[int, int]

    def normalized(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Return ``(start, end)`` in reading order."""
        if self.anchor <= self.cursor:
            return self.anchor, self.cursor
        return self.cursor, self.anchor


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _slice_by_columns(plain_line: str, start_col: int, end_col: int) -> str:
    """Slice ``plain_line`` by display columns (CJK-aware).

    Returns the substring whose characters occupy columns in
    ``[start_col, end_col)``. Characters wider than one column are
    included only when they fit fully inside the window.
    """
    if end_col <= start_col:
        return ""
    out: list[str] = []
    col = 0
    for ch in plain_line:
        w = get_cwidth(ch)
        next_col = col + w
        if col >= end_col:
            break
        if next_col > start_col and col < end_col:
            # Include when the character overlaps the window.
            if col >= start_col and next_col <= end_col:
                out.append(ch)
        col = next_col
    return "".join(out)


def extract_selection_text(transcript: str, selection: Selection) -> str:
    """Return the plain-text content of ``selection`` from ``transcript``.

    ANSI styling is stripped first; the column slicing is applied to the
    plain lines. Multi-line selections preserve the newline separators.
    """
    plain = _strip_ansi(transcript)
    lines = plain.split("\n")
    (r1, c1), (r2, c2) = selection.normalized()
    if r1 == r2:
        if r1 >= len(lines):
            return ""
        return _slice_by_columns(lines[r1], c1, c2)
    if r1 >= len(lines):
        return ""
    r2 = min(r2, len(lines) - 1)
    pieces: list[str] = [_slice_by_columns(lines[r1], c1, 1 << 30)]
    for row in range(r1 + 1, r2):
        pieces.append(lines[row])
    pieces.append(_slice_by_columns(lines[r2], 0, c2))
    # Trim trailing whitespace introduced by column slicing on the tail.
    return "\n".join(pieces).rstrip()


def _highlight_line(
    line: StyleAndTextTuples,
    start_col: int,
    end_col: int,
    style_suffix: str,
) -> StyleAndTextTuples:
    """Return a copy of ``line`` with ``style_suffix`` appended to the style
    of every fragment whose characters overlap ``[start_col, end_col)``.

    Fragment tuples from prompt-toolkit are ``(style, text)`` or
    ``(style, text, mouse_handler)``. We rebuild them preserving any
    third element.
    """
    out: StyleAndTextTuples = []
    col = 0
    for item in line:
        style = item[0]
        text = item[1]
        rest = item[2:] if len(item) > 2 else ()
        w = get_cwidth(text)
        next_col = col + w
        overlaps = next_col > start_col and col < end_col
        new_style = f"{style}{style_suffix}" if overlaps else style
        new_item: tuple[Any, ...] = (new_style, text, *rest)
        out.append(new_item)  # type: ignore[arg-type]
        col = next_col
    return out


def highlight_fragments(
    transcript: str,
    selection: Selection,
    style_suffix: str = " reverse",
) -> StyleAndTextTuples:
    """Return a fragment list of ``transcript`` with the selection
    highlighted.

    The transcript is parsed once with ``ANSI`` and the fragment list is
    walked per line; ``style_suffix`` is appended to the existing style
    string of overlapping fragments so the original colors are preserved
    and only the reverse-video attribute is layered on top.
    """
    fragments = ANSI(transcript).__pt_formatted_text__()
    lines = list(split_lines(fragments))
    (r1, c1), (r2, c2) = selection.normalized()
    highlighted: StyleAndTextTuples = []
    for row, line in enumerate(lines):
        if row < r1 or row > r2:
            highlighted.extend(line)
        elif r1 == r2:
            highlighted.extend(_highlight_line(line, c1, c2, style_suffix))
        elif row == r1:
            highlighted.extend(_highlight_line(line, c1, 1 << 30, style_suffix))
        elif row == r2:
            highlighted.extend(_highlight_line(line, 0, c2, style_suffix))
        else:
            highlighted.extend(_highlight_line(line, 0, 1 << 30, style_suffix))
        # ``split_lines`` drops the trailing newline; re-insert between
        # logical lines so the rendered row count matches the source.
        if row < len(lines) - 1:
            highlighted.append(("", "\n"))
    return highlighted
