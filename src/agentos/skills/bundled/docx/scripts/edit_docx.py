"""Apply run-level edits to an existing `.docx`.

Operations:
    {"op": "replace_run", "para": <int>, "run": <int>, "text": "..."}
    {"op": "replace_text", "find": "...", "with": "..."}

`replace_text` walks every paragraph and matches against the joined run texts,
so a target that spans runs is still found. The replacement is written into the
run that owns the first character of its match, and every character the match
did not touch stays in the run it came from — a run is where Word keeps
character formatting, so moving text between runs would silently restyle it.
The resulting paragraph text is still plain `str.replace` on the joined runs;
only the run layout is preserved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from docx import Document
from docx.text.paragraph import Paragraph


def _replace_run(para: Paragraph, run_idx: int, text: str) -> None:
    if 0 <= run_idx < len(para.runs):
        para.runs[run_idx].text = text


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


def _iter_all_paragraphs(doc: Document) -> list[Paragraph]:
    paragraphs: list[Paragraph] = list(doc.paragraphs)
    seen_tc: set[Any] = set()
    for table in doc.tables:
        paragraphs.extend(_iter_table_paragraphs(table, seen_tc))
    return paragraphs


def _iter_table_paragraphs(table: Any, seen_tc: set[Any]) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    for row in table.rows:
        for cell in row.cells:
            tc = getattr(cell, "_tc", None)
            if tc is not None:
                if tc in seen_tc:
                    continue
                seen_tc.add(tc)
            paragraphs.extend(cell.paragraphs)
            for nested in getattr(cell, "tables", []):
                paragraphs.extend(_iter_table_paragraphs(nested, seen_tc))
    return paragraphs


def apply_ops(doc: Document, ops: list[dict[str, Any]]) -> int:
    applied = 0
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "replace_run":
            try:
                para = doc.paragraphs[int(op["para"])]
            except (KeyError, IndexError, ValueError):
                continue
            _replace_run(para, int(op.get("run", 0)), str(op.get("text", "")))
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
    print(json.dumps({"applied": applied}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
