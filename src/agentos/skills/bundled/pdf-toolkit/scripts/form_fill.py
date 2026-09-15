"""Fill AcroForm fields in a PDF.

Usage:
    form_fill.py form.pdf data.json --out filled.pdf
    form_fill.py form.pdf --list-fields
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter


def list_fields(path: Path) -> dict[str, Any]:
    reader = PdfReader(str(path))
    raw = reader.get_fields() or {}
    out: dict[str, Any] = {}
    for name, field in raw.items():
        ft = field.get("/FT")
        out[name] = {
            "type": str(ft) if ft is not None else "",
            "value": field.get("/V"),
            "default": field.get("/DV"),
        }
    return out


class NoFieldsError(ValueError):
    """The input has no form to fill. Reported as ``error:`` / exit 2 rather
    than written out: a PDF with no AcroForm is the wrong input, not an empty
    edit."""


def fill(path: Path, data: dict[str, str], out: Path) -> int:
    """Fill *data* into the form at *path* and write *out*, returning the number
    of pages updated.

    Writing is the last step and only happens when at least one page accepted
    the update. A PDF with no AcroForm raises on every page, and writing anyway
    produced a complete, valid, entirely unfilled copy reported as a success --
    which, pointed at an existing filled form, replaced it (Issue #2131).
    """
    reader = PdfReader(str(path))
    writer = PdfWriter(clone_from=reader)
    filled = 0
    failures: list[str] = []
    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, data)
            filled += 1
        except Exception as exc:  # pragma: no cover — defensive against pypdf API drift
            print(f"warn: page update failed: {exc}", file=sys.stderr)
            failures.append(str(exc))
    if filled == 0:
        detail = f": {failures[0]}" if failures else ""
        raise NoFieldsError(f"no page of {path} accepted a form fill{detail}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        writer.write(fh)
    return filled


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill AcroForm fields in a PDF.")
    parser.add_argument("input", type=Path)
    parser.add_argument("data", type=Path, nargs="?", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--list-fields", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.input.is_file():
        print(f"error: input {args.input} not found", file=sys.stderr)
        return 2
    if args.list_fields:
        print(json.dumps(list_fields(args.input), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.data is None or args.out is None:
        print("error: data and --out are required unless --list-fields", file=sys.stderr)
        return 2
    if not args.data.is_file():
        print(f"error: data {args.data} not found", file=sys.stderr)
        return 2
    raw = json.loads(args.data.read_text(encoding="utf-8"))
    data = {str(k): str(v) for k, v in (raw.items() if isinstance(raw, dict) else [])}
    try:
        pages = fill(args.input, data, args.out)
    except NoFieldsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            f"hint: run `form_fill.py {args.input} --list-fields` to see whether "
            "this document has an AcroForm at all",
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"pages_processed": pages, "fields": len(data)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
