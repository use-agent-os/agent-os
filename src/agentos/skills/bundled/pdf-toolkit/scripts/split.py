"""Split a PDF into multiple files by page-range spec.

Each disjoint range becomes one output file: <stem>_001.pdf, _002.pdf, ...

Usage:
    split.py input.pdf --pages "1-3,5,7-9" --out out_dir/

Pages past the end of the document are never written silently: the summary
lists them under ``skipped_pages``, and a spec with no page in range is an
error rather than an empty success.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

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


def _range_bounds(token: str, spec: str) -> tuple[int, int]:
    """Both ends of ``lo-hi``, or raise :class:`PageSpecError`.

    An open-ended range (``3-``, ``-5``) is not supported -- there is no
    ``total`` here to close it against -- and is named as such rather than
    reported as ``'' is not a page number``.
    """
    lo_s, hi_s = token.split("-", 1)
    if not lo_s.strip() or not hi_s.strip():
        raise PageSpecError(
            f"invalid --pages value {spec!r}: open-ended range {token!r} is not "
            f"supported; give both ends, e.g. '3-7'"
        )
    return _page_number(lo_s, spec), _page_number(hi_s, spec)


def split_ranges(spec: str) -> list[list[int]]:
    groups: list[list[int]] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo, hi = _range_bounds(token, spec)
            if lo > hi:
                lo, hi = hi, lo
            groups.append(list(range(lo, hi + 1)))
        else:
            groups.append([_page_number(token, spec)])
    return groups


@dataclass
class SplitResult:
    """What a split actually produced, including what it could not."""

    total_pages: int
    parts: list[tuple[Path, list[int]]] = field(default_factory=list)
    skipped_pages: list[int] = field(default_factory=list)

    @property
    def files(self) -> list[Path]:
        return [path for path, _ in self.parts]


def split(input_path: Path, pages_spec: str, out_dir: Path) -> SplitResult:
    # Parse before opening the document: an unparseable spec is the caller's
    # mistake, and it should be named before any work is done on their behalf.
    groups = split_ranges(pages_spec)
    reader = PdfReader(str(input_path))
    result = SplitResult(total_pages=len(reader.pages))
    for group in groups:
        valid_pages = [p for p in group if 1 <= p <= result.total_pages]
        result.skipped_pages.extend(p for p in group if not 1 <= p <= result.total_pages)
        if not valid_pages:
            continue
        writer = PdfWriter()
        for page_num in valid_pages:
            writer.add_page(reader.pages[page_num - 1])
        # Number the files that exist, not the groups in the spec: a caller
        # globbing the output directory expects _001 to be the first part.
        out_path = out_dir / f"{input_path.stem}_{len(result.parts) + 1:03d}.pdf"
        # Created here, not up front, so a spec with no page in range leaves
        # nothing behind — not even an empty directory.
        out_dir.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as fh:
            writer.write(fh)
        result.parts.append((out_path, valid_pages))
    return result


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
        result = split(args.input, args.pages, args.out)
    except PageSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not result.parts:
        print(
            f"error: no page in {args.pages!r} exists in {args.input} ({result.total_pages} pages)",
            file=sys.stderr,
        )
        return 2
    if result.skipped_pages:
        skipped = ", ".join(str(p) for p in result.skipped_pages)
        print(
            f"warn: skipped pages outside 1-{result.total_pages} of {args.input}: {skipped}",
            file=sys.stderr,
        )
    _write_stdout(
        json.dumps(
            {
                "files": [str(p) for p in result.files],
                "count": len(result.parts),
                "parts": [{"file": str(p), "pages": pages} for p, pages in result.parts],
                "skipped_pages": result.skipped_pages,
                "total_pages": result.total_pages,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
