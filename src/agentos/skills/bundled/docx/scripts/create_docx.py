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


def build(spec: Any) -> Document:
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

    body = spec.get("body")
    if not isinstance(body, (list, tuple)):
        return doc

    for item in body:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "heading":
            try:
                level = int(item.get("level", 1))
            except (TypeError, ValueError):
                level = 1
            level = max(0, min(9, level))
            doc.add_heading(str(item.get("text", "")), level=level)
        elif kind == "paragraph":
            style = item.get("style") or "Normal"
            doc.add_paragraph(str(item.get("text", "")), style=style)
        elif kind == "table":
            raw_rows = item.get("rows")
            if not isinstance(raw_rows, (list, tuple)):
                continue
            # A scalar row entry (str/int/None/...) becomes a single-cell row
            # rather than being iterated -- a bare string would otherwise be
            # split into one cell per character, and len() on a non-sequence
            # scalar like an int would raise TypeError outright.
            rows = [r if isinstance(r, (list, tuple)) else [r] for r in raw_rows]
            ncols = max((len(r) for r in rows), default=0)
            if ncols <= 0:
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
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"error: spec {args.spec} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict):
        print(
            f'error: spec {args.spec} must be a JSON object with a "body" array',
            file=sys.stderr,
        )
        return 2
    doc = build(spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
