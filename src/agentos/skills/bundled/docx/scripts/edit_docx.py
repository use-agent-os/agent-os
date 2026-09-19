"""Apply run-level edits to an existing `.docx`.

Operations:
    {"op": "replace_run", "para": <int>, "run": <int>, "text": "..."}
    {"op": "replace_text", "find": "...", "with": "..."}

`replace_text` walks every paragraph -- body paragraphs, the cells of every
table (nested tables included) and each section's headers and footers -- and
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
from docx.section import _BaseHeaderFooter
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import write_stdout as _write_stdout  # noqa: E402


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


def _iter_all_paragraphs(doc: Document) -> Iterator[Paragraph]:
    """Body paragraphs, every table-cell paragraph, then headers and footers.

    ``doc.paragraphs`` is body-only in python-docx, yet contracts, reports and
    invoices keep most of their placeholders inside tables, and letterheads
    or confidentiality banners live in the section headers and footers.
    """
    yield from doc.paragraphs
    yield from _iter_table_paragraphs(doc.tables)
    yield from _iter_header_footer_paragraphs(doc)


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
            if _replace_run(paragraphs[para_idx], run_idx, str(op.get("text", ""))):
                applied += 1
        elif kind == "replace_text":
            find = str(op.get("find", ""))
            replacement = str(op.get("with", ""))
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
    raw = json.loads(args.ops.read_text(encoding="utf-8"))
    ops = raw if isinstance(raw, list) else []
    doc = Document(str(args.input))
    applied = apply_ops(doc, ops)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    _write_stdout(json.dumps({"applied": applied}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
