"""Apply run-level edits to an existing `.docx`.

Operations:
    {"op": "replace_run", "para": <int>, "run": <int>, "text": "..."}
    {"op": "replace_text", "find": "...", "with": "..."}

`replace_text` walks every paragraph -- body paragraphs, the cells of every
table (nested tables included), each section's headers and footers, and the
paragraphs inside every text box -- and
matches against the joined run texts, so a target that spans runs is still
found. The replacement is written into the run that owns the first character
of its match, and every character the match did not touch stays in the run it
came from — a run is where Word keeps character formatting, so moving text
between runs would silently restyle it. The resulting paragraph text is still
plain `str.replace` on the joined runs; only the run layout is preserved.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.section import _BaseHeaderFooter
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skill_stdio import write_stdout as _write_stdout  # noqa: E402


def _replace_run(para: Paragraph, run_idx: int, text: str) -> bool:
    """Overwrite one run's text; return whether the run existed."""
    if not 0 <= run_idx < len(para.runs):
        return False
    para.runs[run_idx].text = text
    return True


def _replace_text_in_paragraph(para: Paragraph, find: str, replacement: str) -> bool:
    """Replace ``find`` in a paragraph without moving text between runs.

    A run is where Word keeps character formatting, so which run a character
    ends up in is not cosmetic. Collapsing the paragraph into ``runs[0]`` and
    emptying the rest -- what this did before -- gave every character run 0's
    formatting and left the other runs as empty shells: bold, italic, font,
    size and colour were discarded for the whole paragraph, silently, even when
    a single word needed changing.

    Every character therefore stays with the run it came from. A replacement is
    written into the run that owns the first character of its match, which also
    covers a ``find`` spanning several runs: the matched characters leave the
    runs they spanned and the surrounding runs are untouched.
    """
    runs = para.runs
    if not runs:
        return False
    texts = [run.text or "" for run in runs]
    full = "".join(texts)
    if not find or find not in full:
        return False

    # Character position -> owning run index.
    owner: list[int] = []
    for index, text in enumerate(texts):
        owner.extend([index] * len(text))

    pieces: list[list[str]] = [[] for _ in runs]
    cursor = 0
    while cursor < len(full):
        if full.startswith(find, cursor):
            pieces[owner[cursor]].append(replacement)
            cursor += len(find)
            continue
        pieces[owner[cursor]].append(full[cursor])
        cursor += 1

    for run, parts in zip(runs, pieces, strict=True):
        rebuilt = "".join(parts)
        if run.text != rebuilt:
            run.text = rebuilt
    return True


def _iter_table_paragraphs(tables: Iterable[Table]) -> Iterator[Paragraph]:
    """Yield the paragraphs of every cell in *tables*, recursing into nested tables.

    Cells are taken straight from the ``<w:tc>`` elements rather than through
    ``row.cells``: that API repeats a merged cell once per grid column it spans
    (so a replacement would hit the same text several times) and resolves
    vertically merged cells against the row above, which raises ``ValueError``
    on the irregular grids other generators produce. Each ``<w:tc>`` is visited
    exactly once either way.
    """
    for table in tables:
        for tc in table._tbl.iter_tcs():
            cell = _Cell(tc, table)
            yield from cell.paragraphs
            yield from _iter_table_paragraphs(cell.tables)


def _iter_header_footer_paragraphs(doc: Document) -> Iterator[Paragraph]:
    """Yield the paragraphs of every header and footer part the document defines.

    A header or footer that ``is_linked_to_previous`` has no part of its own:
    on the first section that means "none", on later sections it means the
    previous section's part, which was already visited. Skipping those keeps
    the walk to one visit per part -- and matters for a second reason:
    python-docx materialises a header part the moment its paragraphs are
    read, so touching a linked one would add empty headers to the package.
    """
    for section in doc.sections:
        parts: tuple[_BaseHeaderFooter, ...] = (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        )
        for part in parts:
            if part.is_linked_to_previous:
                continue
            yield from part.paragraphs
            yield from _iter_table_paragraphs(part.tables)


_P = qn("w:p")
_TBL = qn("w:tbl")
_TXBX_CONTENT = qn("w:txbxContent")


def _iter_textbox_paragraphs(para: Paragraph) -> Iterator[Paragraph]:
    """Yield the paragraphs held by every text box anywhere inside *para*.

    A text box is not a paragraph of the story it sits in: Word parks its
    content in a ``<w:txbxContent>`` nested inside a run, so python-docx reports
    the host paragraph with empty text and the box's own words are reached by no
    paragraph walk at all. Pull quotes, callouts, letterhead banners and the
    "CONFIDENTIAL" stamps that templates ship are text boxes almost by default.

    ``iter`` sweeps every depth in one pass, so a box nested inside another box
    -- or inside a table that is itself inside a box -- is found without
    recursing here. Both OOXML spellings land on the same element: the modern
    DrawingML shape (``<w:drawing>``) and the legacy VML one (``<w:pict>``) each
    wrap a ``<w:txbxContent>``. A shape written as ``<mc:AlternateContent>``
    carries both spellings of the same box, and both are visited on purpose --
    Word may render either, so replacing only one leaves the other stale.
    """
    for content in para._p.iter(_TXBX_CONTENT):
        for child in content.iterchildren():
            if child.tag == _P:
                yield Paragraph(child, para)
            elif child.tag == _TBL:
                yield from _iter_table_paragraphs([Table(child, para)])


def _iter_all_paragraphs(doc: Document) -> Iterator[Paragraph]:
    """Body paragraphs, every table-cell paragraph, then headers and footers.

    ``doc.paragraphs`` is body-only in python-docx, yet contracts, reports and
    invoices keep most of their placeholders inside tables, and letterheads
    or confidentiality banners live in the section headers and footers.

    Each of those may host text boxes, so every paragraph is followed by the
    paragraphs of the boxes it contains.
    """
    for para in _iter_story_paragraphs(doc):
        yield para
        yield from _iter_textbox_paragraphs(para)


def _iter_story_paragraphs(doc: Document) -> Iterator[Paragraph]:
    yield from doc.paragraphs
    yield from _iter_table_paragraphs(doc.tables)
    yield from _iter_header_footer_paragraphs(doc)


#: Every op kind ``apply_ops`` knows. An op outside this set is a caller
#: mistake, not a no-op: the ops file is written by the agent one step before
#: the call, so ``replace-text`` for ``replace_text`` is a routine slip.
OP_KINDS = ("replace_run", "replace_text")


class OpsError(ValueError):
    """An ops file that cannot be used. Reported as ``error:`` / exit 2, never
    as a traceback: the caller passed bad input, the script did not break."""


def load_ops(path: Path) -> list[dict[str, Any]]:
    """Read and validate the ops file, or raise :class:`OpsError`.

    Validation happens before the document is opened, so an unusable ops file
    cannot leave a half-applied document behind, and ``--out`` is never touched.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpsError(f"ops {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise OpsError(f"ops {path} must be a JSON array of operations, got {type(raw).__name__}")
    for index, op in enumerate(raw):
        if not isinstance(op, dict):
            raise OpsError(f"op {index} must be an object, got {type(op).__name__}")
        kind = op.get("op")
        if kind not in OP_KINDS:
            raise OpsError(
                f"op {index} has unknown kind {kind!r}; expected one of {', '.join(OP_KINDS)}"
            )
    return raw


def _op_text(op: dict[str, Any], key: str) -> str:
    """The text an op carries under *key*, with JSON ``null`` meaning empty.

    ``dict.get(key, "")`` returns the default only when the key is *absent*.
    A key present with a ``null`` value returns ``None``, and ``str(None)`` is
    the four-letter word "None" -- which is what got written into the document
    where the caller meant to clear the text. Every other type keeps ``str``,
    so a ``0`` or a ``false`` still prints as itself rather than vanishing.
    """
    value = op.get(key)
    return "" if value is None else str(value)


def apply_ops(doc: Document, ops: list[dict[str, Any]]) -> int:
    applied = 0
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "replace_run":
            # Bounds are checked explicitly rather than by catching IndexError:
            # a negative index would otherwise wrap round to the end of the
            # document and edit a paragraph the op never named.
            try:
                para_idx = int(op["para"])
                run_idx = int(op.get("run", 0))
            except (KeyError, TypeError, ValueError):
                continue
            paragraphs = doc.paragraphs
            if not 0 <= para_idx < len(paragraphs):
                continue
            if _replace_run(paragraphs[para_idx], run_idx, _op_text(op, "text")):
                applied += 1
        elif kind == "replace_text":
            find = _op_text(op, "find")
            replacement = _op_text(op, "with")
            if not find:
                continue
            for para in _iter_all_paragraphs(doc):
                if _replace_text_in_paragraph(para, find, replacement):
                    applied += 1
    return applied


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edit a .docx in place via run-level ops.")
    parser.add_argument("input", type=Path, help="Path to the source .docx")
    parser.add_argument("ops", type=Path, help="JSON file containing a list of ops")
    parser.add_argument("--out", type=Path, required=True, help="Output .docx path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.input.is_file():
        print(f"error: input {args.input} not found", file=sys.stderr)
        return 2
    if not args.ops.is_file():
        print(f"error: ops {args.ops} not found", file=sys.stderr)
        return 2
    try:
        ops = load_ops(args.ops)
    except OpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    doc = Document(str(args.input))
    # Deliberately still writes when `applied` is 0: a valid op that matches
    # nothing is a different question from an unusable ops file, and a skipped
    # op must not fail the run (see the sibling xlsx script's `value` rule).
    applied = apply_ops(doc, ops)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    _write_stdout(json.dumps({"applied": applied}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
