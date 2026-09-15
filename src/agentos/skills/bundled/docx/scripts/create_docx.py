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


def build(spec: dict[str, Any]) -> Document:
    doc = Document()
    if not isinstance(spec, dict):
        return doc

    meta = spec.get("metadata")
    if isinstance(meta, dict):
        core = doc.core_properties
        if "title" in meta and meta["title"] is not None:
            core.title = str(meta["title"])
        if "author" in meta and meta["author"] is not None:
            core.author = str(meta["author"])

    body = spec.get("body")
    if not isinstance(body, list):
        return doc

    for item in body:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "heading":
            raw_level = item.get("level", 1)
            try:
                level = int(raw_level)
            except (TypeError, ValueError):
                level = 1
            level = max(0, min(9, level))
            doc.add_heading(str(item.get("text", "")), level=level)
        elif kind == "paragraph":
            style = item.get("style") or "Normal"
            if not isinstance(style, str):
                style = "Normal"
            doc.add_paragraph(str(item.get("text", "")), style=style)
        elif kind == "table":
            raw_rows = item.get("rows")
            if not isinstance(raw_rows, list) or not raw_rows:
                continue
            valid_rows: list[list[Any]] = []
            for r in raw_rows:
                if isinstance(r, (list, tuple)):
                    valid_rows.append(list(r))
                else:
                    valid_rows.append([r])
            if not valid_rows:
                continue
            ncols = max(len(r) for r in valid_rows)
            if ncols <= 0:
                continue
            table = doc.add_table(rows=len(valid_rows), cols=ncols)
            for r_idx, row in enumerate(valid_rows):
                for c_idx, value in enumerate(row):
                    if c_idx < ncols:
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
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        print(f"error: invalid JSON spec: {err}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict):
        print("error: JSON spec must be an object", file=sys.stderr)
        return 2
    doc = build(spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
