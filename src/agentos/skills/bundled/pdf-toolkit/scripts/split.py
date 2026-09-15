"""Split a PDF into multiple files by page-range spec.

Each disjoint range becomes one output file: <stem>_001.pdf, _002.pdf, ...

Usage:
    split.py input.pdf --pages "1-3,5,7-9" --out out_dir/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter


class PageSpecError(ValueError):
    """A ``--pages`` value that cannot be parsed. Reported as ``error:`` / exit
    2, never as a traceback: the caller passed bad input, the script did not
    break."""


def _page_number(token: str, spec: str) -> int:
    """Parse one page number, or raise :class:`PageSpecError` naming the flag."""
    try:
        return int(token)
    except ValueError:
        hint = ""
        if any(dash in token for dash in "–—−"):
            # A model writes an en dash more often than one would like, and it
            # is invisible in a diff: the token never splits, so the whole
            # thing lands in int().
            hint = " (that looks like an en/em dash; ranges use a plain '-')"
        raise PageSpecError(
            f"invalid --pages value {spec!r}: {token.strip()!r} is not a page "
            f"number{hint}; expected 1-based numbers and ranges, e.g. '1-3,5'"
        ) from None


def split_ranges(spec: str) -> list[list[int]]:
    groups: list[list[int]] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            lo, hi = _page_number(lo_s, spec), _page_number(hi_s, spec)
            if lo > hi:
                lo, hi = hi, lo
            groups.append(list(range(lo, hi + 1)))
        else:
            groups.append([_page_number(token, spec)])
    return groups


def split(input_path: Path, pages_spec: str, out_dir: Path) -> list[Path]:
    # Parse before opening the document or creating the directory: an
    # unparseable spec should leave nothing behind at all.
    groups = split_ranges(pages_spec)
    reader = PdfReader(str(input_path))
    total = len(reader.pages)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for idx, group in enumerate(groups, start=1):
        valid_pages = [p for p in group if 1 <= p <= total]
        if not valid_pages:
            continue
        writer = PdfWriter()
        for page_num in valid_pages:
            writer.add_page(reader.pages[page_num - 1])
        out_path = out_dir / f"{input_path.stem}_{idx:03d}.pdf"
        with out_path.open("wb") as fh:
            writer.write(fh)
        written.append(out_path)
    return written


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split a PDF by page ranges.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--pages", required=True, help="e.g. '1-3,5,7-9'")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.input.is_file():
        print(f"error: input {args.input} not found", file=sys.stderr)
        return 2
    try:
        written = split(args.input, args.pages, args.out)
    except PageSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"files": [str(p) for p in written], "count": len(written)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
