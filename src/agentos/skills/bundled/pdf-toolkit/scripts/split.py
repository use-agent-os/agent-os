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


#: How many out-of-range pages ``skipped_pages`` lists one by one. Beyond this
#: the rest are counted in ``skipped_pages_omitted`` instead of enumerated, so a
#: runaway span cannot turn the summary into a hundred-million-entry list.
MAX_REPORTED_SKIPPED = 1000


class PageSpecError(ValueError):
    """A ``--pages`` value that is not a list of ``N`` / ``N-M`` tokens."""


def page_spans(spec: str) -> list[tuple[int, int]]:
    """Parse ``'1-3,5,7-9'`` into inclusive ``(lo, hi)`` spans, without expanding them.

    Expanding each span into a list of page numbers happened before the
    document's length was known, so ``--pages 1-100000000`` allocated a hundred
    million ints (and listed every one of them as skipped) to split a five-page
    file (#2996). A span is two numbers; the pages it covers are only ever
    materialised after clamping to the document.
    """
    spans: list[tuple[int, int]] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        lo_s, sep, hi_s = token.partition("-")
        try:
            lo = int(lo_s)
            hi = int(hi_s) if sep else lo
        except ValueError:
            raise PageSpecError(
                f"invalid page range {token!r}: expected a page (5) or a range (1-3)"
            ) from None
        if lo > hi:
            lo, hi = hi, lo
        spans.append((lo, hi))
    return spans


def split_ranges(spec: str) -> list[list[int]]:
    """Every page each span of *spec* names. Unbounded -- :func:`split` does not use it."""
    return [list(range(lo, hi + 1)) for lo, hi in page_spans(spec)]


@dataclass
class SplitResult:
    """What a split actually produced, including what it could not."""

    total_pages: int
    parts: list[tuple[Path, list[int]]] = field(default_factory=list)
    skipped_pages: list[int] = field(default_factory=list)
    #: Out-of-range pages counted but not listed, past MAX_REPORTED_SKIPPED.
    skipped_pages_omitted: int = 0

    def skip(self, lo: int, hi: int) -> None:
        """Record the inclusive span ``lo..hi`` as skipped, listing at most the cap."""
        if lo > hi:
            return
        room = max(0, MAX_REPORTED_SKIPPED - len(self.skipped_pages))
        listed = min(room, hi - lo + 1)
        self.skipped_pages.extend(range(lo, lo + listed))
        self.skipped_pages_omitted += (hi - lo + 1) - listed

    @property
    def files(self) -> list[Path]:
        return [path for path, _ in self.parts]


def split(input_path: Path, pages_spec: str, out_dir: Path) -> SplitResult:
    reader = PdfReader(str(input_path))
    result = SplitResult(total_pages=len(reader.pages))
    total = result.total_pages
    for lo, hi in page_spans(pages_spec):
        result.skip(lo, min(hi, 0))  # before page 1
        valid_pages = list(range(max(lo, 1), min(hi, total) + 1))
        result.skip(max(lo, total + 1), hi)  # past the last page
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
        if result.skipped_pages_omitted:
            skipped += f" and {result.skipped_pages_omitted} more"
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
                **(
                    {"skipped_pages_omitted": result.skipped_pages_omitted}
                    if result.skipped_pages_omitted
                    else {}
                ),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
