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


def _write_stdout(text: str) -> None:
    """Write *text* to stdout as UTF-8, surviving a non-UTF-8 stdout encoding.

    ``print`` encodes through ``sys.stdout.encoding``, which on Windows is the
    console code page (cp1252, cp936, cp932) and not UTF-8, so a character
    outside that page raises ``UnicodeEncodeError`` before a byte is written —
    the document decides whether the skill runs. The binary buffer is therefore
    the primary path, matching the ``--out`` branch, which already passes
    ``encoding="utf-8"``. A stream without a usable ``buffer`` — a wrapper, or a
    captured stdout — still gets the text, escaped rather than lost.
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(text.encode("utf-8"))
            buffer.flush()
            return
        except (AttributeError, OSError, ValueError):
            # Buffer closed or not writable — fall through to the text layer.
            pass

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    # Lossless: unencodable chars become \\uXXXX escapes, not "?".
    sys.stdout.write(text.encode(encoding, errors="backslashreplace").decode(encoding))
    sys.stdout.flush()


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
    """The input has no form for ``data`` to land in. Reported as ``error:`` /
    exit 2, like the other refused inputs, rather than written out: a PDF
    with no AcroForm is the wrong input, not an empty edit."""


def fill(path: Path, data: dict[str, str], out: Path) -> int:
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
        # Every page raised (no AcroForm at all) -- writing anyway produced a
        # complete, valid, entirely unfilled copy of the input reported as a
        # successful fill, including when `out` pointed at an existing filled
        # form (#2131).
        detail = f": {failures[0]}" if failures else ""
        raise NoFieldsError(f"{path} has no AcroForm fields to fill{detail}")
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
        _write_stdout(
            json.dumps(list_fields(args.input), ensure_ascii=False, indent=2, default=str) + "\n"
        )
        return 0
    if args.data is None or args.out is None:
        print("error: data and --out are required unless --list-fields", file=sys.stderr)
        return 2
    if not args.data.is_file():
        print(f"error: data {args.data} not found", file=sys.stderr)
        return 2
    try:
        raw = json.loads(args.data.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Both are "not a JSON document": a UTF-16 file from PowerShell's
        # Out-File is as unusable as a truncated one.
        print(f"error: data {args.data} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    # A list of field objects is a routine slip for the caller that writes the
    # data file; coercing it to {} used to fill nothing and still exit 0.
    if not isinstance(raw, dict):
        print(
            f"error: data {args.data} must be a JSON object of field -> value, "
            f"got {type(raw).__name__}",
            file=sys.stderr,
        )
        return 2
    data = {str(k): str(v) for k, v in raw.items()}
    try:
        pages = fill(args.input, data, args.out)
    except NoFieldsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            f"hint: run `form_fill.py {args.input} --list-fields` to inspect "
            "the form before filling",
            file=sys.stderr,
        )
        return 2
    _write_stdout(
        json.dumps({"pages_processed": pages, "fields": len(data)}, ensure_ascii=False) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
