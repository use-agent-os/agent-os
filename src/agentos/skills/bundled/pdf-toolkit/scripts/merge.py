"""Merge whole PDFs or page ranges from multiple PDFs.

Usage:
    merge.py a.pdf b.pdf --out combined.pdf
    merge.py manifest.json --out combined.pdf

Manifest schema:
    [{"file": "a.pdf", "pages": "1-3"},
     {"file": "b.pdf"}]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def parse_ranges(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    pages: list[int] = []
    has_tokens = False
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        has_tokens = True
        if "-" in token:
            parts = [p.strip() for p in token.split("-")]
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise ValueError(f"malformed page range: {token!r}")
            lo, hi = int(parts[0]), int(parts[1])
            if lo < 1 or hi < 1:
                raise ValueError(f"page numbers must be >= 1: {token!r}")
            if lo > hi:
                lo, hi = hi, lo
            pages.extend(range(lo, hi + 1))
        else:
            if not token.isdigit():
                raise ValueError(f"malformed page number: {token!r}")
            val = int(token)
            if val < 1:
                raise ValueError(f"page numbers must be >= 1: {token!r}")
            pages.append(val)
    if not has_tokens:
        raise ValueError("empty page spec")
    return [p for p in pages if 1 <= p <= total]


def merge(items: Iterable[dict[str, str]], out: Path) -> int:
    writer = PdfWriter()
    count = 0
    pages_to_write: list[tuple[PdfReader, list[int]]] = []
    for item in items:
        path = Path(item["file"])
        if not path.is_file():
            print(f"warn: missing {path}", file=sys.stderr)
            continue
        reader = PdfReader(str(path))
        total = len(reader.pages)
        pages = parse_ranges(item.get("pages"), total)
        pages_to_write.append((reader, pages))
    for reader, pages in pages_to_write:
        for page_num in pages:
            writer.add_page(reader.pages[page_num - 1])
            count += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        writer.write(fh)
    return count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge PDFs or page ranges.")
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Either N PDF paths, or one .json manifest path",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    items: list[dict[str, str]]
    if len(args.inputs) == 1 and args.inputs[0].endswith(".json"):
        manifest_path = Path(args.inputs[0])
        if not manifest_path.is_file():
            print(f"error: manifest {manifest_path} not found", file=sys.stderr)
            return 2
        items = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            print("error: manifest must be a JSON array", file=sys.stderr)
            return 2
    else:
        items = [{"file": p} for p in args.inputs]
    try:
        written = merge(items, args.out)
    except ValueError:
        print(
            "error: --pages must be 1-based numbers and ranges, e.g. '1-3,5'",
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"pages_written": written, "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
