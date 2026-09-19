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

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import write_stdout as _write_stdout  # noqa: E402


def split_ranges(spec: str) -> list[list[int]]:
    groups: list[list[int]] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            groups.append(list(range(lo, hi + 1)))
        else:
            groups.append([int(token)])
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
    reader = PdfReader(str(input_path))
    result = SplitResult(total_pages=len(reader.pages))
    for group in split_ranges(pages_spec):
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
    result = split(args.input, args.pages, args.out)
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
