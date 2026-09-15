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

#: Every body entry kind ``build`` renders. Anything else is a caller mistake:
#: silently skipping it produced an empty document reported as a success.
BODY_KINDS = ("heading", "paragraph", "table", "page_break")


class SpecError(ValueError):
    """A spec that cannot be used. Reported as ``error:`` / exit 2, never as a
    traceback: the caller passed bad input, the script did not break."""


def load_spec(path: Path) -> dict[str, Any]:
    """Read and validate the spec file, or raise :class:`SpecError`."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"spec {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        # Passing the body array directly instead of {"body": [...]} is the
        # obvious slip, and `spec.get` raised AttributeError on it.
        raise SpecError(
            f'spec {path} must be a JSON object with a "body" array, got {type(raw).__name__}'
        )
    body = raw.get("body", [])
    if not isinstance(body, list):
        raise SpecError(f'spec {path}: "body" must be an array, got {type(body).__name__}')
    if not body:
        raise SpecError(f'spec {path}: "body" is empty, so there is nothing to create')
    for index, item in enumerate(body):
        if not isinstance(item, dict):
            raise SpecError(f"body entry {index} must be an object, got {type(item).__name__}")
        kind = item.get("kind")
        if kind not in BODY_KINDS:
            raise SpecError(
                f"body entry {index} has unknown kind {kind!r}; "
                f"expected one of {', '.join(BODY_KINDS)}"
            )
    return raw


def build(spec: dict[str, Any]) -> Document:
    doc = Document()

    meta = spec.get("metadata", {})
    if isinstance(meta, dict):
        core = doc.core_properties
        if "title" in meta:
            core.title = str(meta["title"])
        if "author" in meta:
            core.author = str(meta["author"])

    for item in spec.get("body", []):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "heading":
            doc.add_heading(str(item.get("text", "")), level=int(item.get("level", 1)))
        elif kind == "paragraph":
            style = item.get("style") or "Normal"
            doc.add_paragraph(str(item.get("text", "")), style=style)
        elif kind == "table":
            rows = item.get("rows") or []
            if not rows:
                continue
            ncols = max(len(r) for r in rows)
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
        spec = load_spec(args.spec)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    doc = build(spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(args.out))
    # The sibling scripts all print a summary; this one printed nothing at all,
    # so a caller had no signal beyond the exit code.
    print(
        json.dumps({"entries": len(spec.get("body", [])), "out": str(args.out)}, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
