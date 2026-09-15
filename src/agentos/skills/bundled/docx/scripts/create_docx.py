"""Create a `.docx` from a declarative JSON spec.

Spec schema:
    {
      "metadata": {"title": "...", "author": "..."},
      "body": [
        {"kind": "heading", "level": 1, "text": "..."},
        {"kind": "paragraph", "text": "...", "style": "Normal"},
        {"kind": "table", "rows": [["..."]]}
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from docx import Document

# python-docx accepts heading levels 0 (Title) through 9.
_MAX_HEADING_LEVEL = 9


def _heading_level(value: Any) -> int:
    """Clamp a spec's heading level into the range python-docx accepts."""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 1
    return max(0, min(level, _MAX_HEADING_LEVEL))


def _table_rows(value: Any) -> list[list[Any]]:
    """Normalise a spec's ``rows`` so every row is a list of cell values.

    A scalar row becomes a one-cell row rather than a ``TypeError``; anything
    that is not a list of rows yields no table at all.
    """
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[list[Any]] = []
    for row in value:
        if isinstance(row, (list, tuple)):
            rows.append(list(row))
        elif row is None:
            rows.append([])
        else:
            rows.append([row])
    return rows


def build(spec: dict[str, Any]) -> Document:
    doc = Document()
    if not isinstance(spec, dict):
        return doc

    meta = spec.get("metadata", {})
    if isinstance(meta, dict):
        core = doc.core_properties
        if "title" in meta:
            core.title = str(meta["title"])
        if "author" in meta:
            core.author = str(meta["author"])

    body = spec.get("body", [])
    if not isinstance(body, list):
        body = []
    for item in body:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "heading":
            doc.add_heading(str(item.get("text", "")), level=_heading_level(item.get("level", 1)))
        elif kind == "paragraph":
            style = item.get("style") or "Normal"
            doc.add_paragraph(str(item.get("text", "")), style=style)
        elif kind == "table":
            rows = _table_rows(item.get("rows"))
            ncols = max((len(r) for r in rows), default=0)
            # A table needs at least one column; all-empty rows are nothing to draw.
            if ncols == 0:
                continue
            table = doc.add_table(rows=len(rows), cols=ncols)
            for r_idx, row in enumerate(rows):
                for c_idx, value in enumerate(row):
                    table.rows[r_idx].cells[c_idx].text = str(value)
        elif kind == "page_break":
            doc.add_page_break()
    return doc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a .docx from a JSON spec.")
    parser.add_argument("spec", type=Path, help="Path to a JSON spec file")
    parser.add_argument("--out", type=Path, required=True, help="Output .docx path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.spec.is_file():
        print(f"error: spec {args.spec} not found", file=sys.stderr)
        return 2
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"error: could not read spec {args.spec}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict):
        print(f"error: spec {args.spec} must be a JSON object", file=sys.stderr)
        return 2
    doc = build(spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
