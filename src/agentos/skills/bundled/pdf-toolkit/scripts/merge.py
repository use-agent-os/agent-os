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
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            pages.extend(range(lo, hi + 1))
        else:
            pages.append(int(token))
    return [p for p in pages if 1 <= p <= total]


class ManifestError(ValueError):
    """A manifest that cannot be used. Reported as ``error:`` / exit 2, never
    as a traceback: the caller passed bad input, the script did not break."""


def load_manifest(path: Path) -> list[dict[str, str]]:
    """Read and validate a manifest file, or raise :class:`ManifestError`."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ManifestError("manifest must be a JSON array")
    items: list[dict[str, str]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ManifestError(
                f"manifest entry {index} must be an object with a "
                f'"file" key, got {type(entry).__name__}'
            )
        if "file" not in entry:
            raise ManifestError(f'manifest entry {index} is missing the "file" key')
        for key in ("file", "pages"):
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                raise ManifestError(f'manifest entry {index} has a non-string "{key}": {value!r}')
        items.append(entry)
    return items


def merge(items: Iterable[dict[str, str]], out: Path) -> int:
    """Merge the selected pages into *out* and return how many were written.

    Writing is the last step and only happens when there is something to write.
    A run that merges nothing leaves *out* alone rather than replacing it with a
    valid-but-empty 0-page PDF -- the caller reads that file as the merge result,
    and an empty one silently destroys whatever was there before (Issue #1921).
    """
    writer = PdfWriter()
    count = 0
    for item in items:
        # ``load_manifest`` rejects these shapes up front, but ``merge`` is also
        # called directly, and an unusable entry there should skip like a missing
        # file rather than raise ``TypeError``/``KeyError`` from inside the loop.
        # Skipping every entry leaves ``count`` at 0, which the caller already
        # treats as a failure.
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            print(f"warn: skipping unusable manifest entry {item!r}", file=sys.stderr)
            continue
        path = Path(item["file"])
        if not path.is_file():
            print(f"warn: missing {path}", file=sys.stderr)
            continue
        reader = PdfReader(str(path))
        total = len(reader.pages)
        for page_num in parse_ranges(item.get("pages"), total):
            writer.add_page(reader.pages[page_num - 1])
            count += 1
    if count == 0:
        return 0
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
        try:
            items = load_manifest(manifest_path)
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        items = [{"file": p} for p in args.inputs]
    written = merge(items, args.out)
    if written == 0:
        print(
            "error: nothing was written; check input paths and page ranges",
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"pages_written": written, "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
