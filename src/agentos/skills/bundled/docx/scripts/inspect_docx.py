"""Dump `.docx` structure as JSON for LLM consumption.

Stdlib + python-docx only. Cross-platform, stateless, exits 0 on success.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table, _Cell


def _table_rows(tbl: Table) -> list[list[str]]:
    """Read a table row by row, straight from its ``<w:tc>`` elements.

    ``row.cells`` maps each row onto the table grid: it repeats a
    horizontally merged cell once per column it spans and resolves a
    vertically merged one against the row above, raising ``ValueError`` on
    the irregular grids other generators emit. ``edit_docx`` avoids the same
    API for the same reason. Reading ``<w:tc>`` directly visits each cell
    exactly once and cannot raise.
    """
    return [[_Cell(tc, tbl).text for tc in row._tr.tc_lst] for row in tbl.rows]


def inspect(path: Path) -> dict[str, Any]:
    doc = Document(str(path))

    paragraphs: list[dict[str, Any]] = []
    for idx, para in enumerate(doc.paragraphs):
        paragraphs.append(
            {
                "index": idx,
                "text": para.text,
                "style": para.style.name if para.style is not None else "",
                "runs": [
                    {"text": run.text, "bold": bool(run.bold), "italic": bool(run.italic)}
                    for run in para.runs
                ],
            }
        )

    tables: list[list[list[str]]] = [_table_rows(tbl) for tbl in doc.tables]

    body_xml = doc.element.body.xml if doc.element is not None else ""
    has_tracked_changes = "<w:ins" in body_xml or "<w:del" in body_xml

    return {
        "paragraphs": paragraphs,
        "tables": tables,
        "sections": len(doc.sections),
        "has_tracked_changes": has_tracked_changes,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dump .docx structure as JSON.")
    parser.add_argument("path", type=Path, help="Path to a .docx file")
    parser.add_argument(
        "--out", type=Path, default=None, help="Optional output JSON path; default stdout"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.path.is_file():
        print(f"error: {args.path} not found", file=sys.stderr)
        return 2
    try:
        payload = inspect(args.path)
    except (PackageNotFoundError, BadZipFile) as exc:
        # Anything that is not a readable .docx package: a renamed file, a
        # truncated download, a zip that never finished writing.
        print(f"error: {args.path} is not a readable .docx file ({exc})", file=sys.stderr)
        return 2
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
