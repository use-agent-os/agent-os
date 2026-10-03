"""Merge whole PDFs or page ranges from multiple PDFs.

Usage:
    merge.py a.pdf b.pdf --out combined.pdf
    merge.py manifest.json --out combined.pdf

Manifest schema:
    [{"file": "a.pdf", "pages": "1-3"},
     {"file": "b.pdf"}]

Pages past the end of an input are never dropped silently: the summary lists
them per file under ``skipped_pages``, and a merge that would write no page at
all is an error rather than a zero-page PDF.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, PdfWriter

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skill_stdio import write_stdout as _write_stdout  # noqa: E402


class PageSpecError(ValueError):
    """A manifest ``pages`` value that cannot be parsed. Reported as ``error:``
    / exit 2, never as a traceback: the caller passed bad input, the script did
    not break."""


def _page_number(token: str, spec: str) -> int:
    """Parse one page number, or raise :class:`PageSpecError` naming the spec."""
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
            f"invalid pages value {spec!r}: {token.strip()!r} is not a page "
            f"number{hint}; expected 1-based numbers and ranges, e.g. '1-3,5'"
        ) from None


def _range_bounds(token: str, spec: str) -> tuple[int, int]:
    """Both ends of ``lo-hi``, or raise :class:`PageSpecError`.

    An open-ended range (``3-``, ``-5``) is not supported and is named as such
    rather than reported as ``'' is not a page number``.
    """
    lo_s, hi_s = token.split("-", 1)
    if not lo_s.strip() or not hi_s.strip():
        raise PageSpecError(
            f"invalid pages value {spec!r}: open-ended range {token!r} is not "
            f"supported; give both ends, e.g. '3-7'"
        )
    return _page_number(lo_s, spec), _page_number(hi_s, spec)


#: How many out-of-range pages one entry lists one by one. Beyond this the rest
#: are counted rather than enumerated, so a runaway span cannot turn the summary
#: into a hundred-million-entry list.
MAX_REPORTED_SKIPPED = 1000


def page_spans(spec: str | None, total: int) -> list[tuple[int, int]]:
    """Parse ``'1-3,5,7-9'`` into inclusive ``(lo, hi)`` spans, without expanding them.

    An absent *spec* means the whole document. A span is two numbers; the pages
    it covers are only ever materialised after clamping to the document, so a
    spec naming more pages than exist costs nothing to parse. A *spec* that
    cannot be parsed raises :class:`PageSpecError`.
    """
    if not spec:
        return [(1, total)]
    spans: list[tuple[int, int]] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo, hi = _range_bounds(token, spec)
            if lo > hi:
                lo, hi = hi, lo
        else:
            lo = hi = _page_number(token, spec)
        spans.append((lo, hi))
    return spans


def requested_pages(spec: str | None, total: int) -> list[int]:
    """Every page number *spec* asks for, in order, without clamping to *total*.

    Unbounded, and no longer used by :func:`merge`: expanding a span before the
    document's length was known let ``"1-20000000"`` allocate twenty million
    ints to merge a ten-page file (#3179). Kept for callers outside this script;
    new code wants :func:`page_spans`.
    """
    return [page for lo, hi in page_spans(spec, total) for page in range(lo, hi + 1)]


def parse_ranges(spec: str | None, total: int) -> list[int]:
    """The pages *spec* names that the document actually has, in order.

    Bounded by *total*: each span is clamped to ``1..total`` before it is
    expanded, so the result can never be longer than the document, whatever the
    spec asks for.
    """
    pages: list[int] = []
    for lo, hi in page_spans(spec, total):
        low, high = max(lo, 1), min(hi, total)
        if low <= high:
            pages.extend(range(low, high + 1))
    return pages


def skipped_pages(
    spec: str | None, total: int, limit: int = MAX_REPORTED_SKIPPED
) -> tuple[list[int], int]:
    """The pages *spec* asked for that the document does not have.

    Returns the first *limit* of them and a count of how many more there were,
    so a span running far past the end is reported without being enumerated.
    """
    listed: list[int] = []
    omitted = 0
    for lo, hi in page_spans(spec, total):
        # The two parts of a span that fall outside the document: before page 1,
        # and past the last page.
        for low, high in ((lo, min(hi, 0)), (max(lo, total + 1), hi)):
            if low > high:
                continue
            room = max(0, limit - len(listed))
            stop = min(high, low + room - 1) if room else low - 1
            listed.extend(range(low, stop + 1))
            omitted += high - stop
    return listed, omitted


class ManifestError(ValueError):
    """A manifest that cannot be used. Reported as ``error:`` / exit 2, never
    as a traceback: the caller passed bad input, the script did not break."""


def load_manifest(path: Path) -> list[dict[str, str]]:
    """Read and validate a manifest file, or raise :class:`ManifestError`.

    Every shape checked here used to escape as a traceback. ``not json`` raised
    ``JSONDecodeError``; ``["a.pdf", "b.pdf"]`` — a bare list of paths, the
    obvious thing to try — raised ``TypeError: string indices must be
    integers`` from ``item["file"]``; and ``[{"pages": "1-2"}]`` raised
    ``KeyError: 'file'``. ``pages`` is type-checked too, because
    ``[{"file": "a.pdf", "pages": 3}]`` reaches ``spec.split(",")`` on an int.
    """
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
        # ``file`` is required, so ``None`` is as wrong as an int -- it must not
        # get the "absent is fine" treatment ``pages`` gets below, or a null
        # slips through to be skipped later instead of named here.
        if not isinstance(entry["file"], str):
            raise ManifestError(
                f'manifest entry {index} has a non-string "file": {entry["file"]!r}'
            )
        pages = entry.get("pages")
        if pages is not None and not isinstance(pages, str):
            raise ManifestError(f'manifest entry {index} has a non-string "pages": {pages!r}')
        items.append(entry)
    return items


@dataclass
class MergeResult:
    """What a merge actually wrote, including the pages it could not."""

    pages_written: int = 0
    skipped: list[tuple[str, list[int]]] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    #: Per entry, how many further out-of-range pages ``skipped`` did not
    #: list. Index-aligned with :attr:`skipped`, including a zero, so a file
    #: named by more than one entry keeps a count per entry.
    skipped_omitted: list[tuple[str, int]] = field(default_factory=list)


def merge(items: Iterable[dict[str, str]], out: Path) -> MergeResult:
    """Merge *items* into *out*, reporting what did not make it in.

    The output file is written only when at least one page went into it. A
    zero-page PDF is not a merge that succeeded with nothing to do -- it is a
    merge whose every input was missing or out of range, and leaving a valid
    but empty file behind lets that pass for success.
    """
    writer = PdfWriter()
    result = MergeResult()
    for item in items:
        # ``load_manifest`` rejects these shapes up front, but ``merge`` is also
        # called directly, and an unusable entry there should skip like a missing
        # file rather than raise ``TypeError``/``KeyError`` from inside the loop.
        # Skipping every entry leaves ``pages_written`` at 0, which the caller
        # already treats as a failure.
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            print(f"warn: skipping unusable manifest entry {item!r}", file=sys.stderr)
            continue
        path = Path(item["file"])
        if not path.is_file():
            print(f"warn: missing {path}", file=sys.stderr)
            result.missing_files.append(str(path))
            continue
        reader = PdfReader(str(path))
        total = len(reader.pages)
        skipped, omitted = skipped_pages(item.get("pages"), total)
        if skipped:
            # Appended together, always, so the two lists stay index-aligned.
            # Reading the count back by filename collapsed two entries naming
            # the same file -- interleaving pages of one PDF is the ordinary
            # reason to name it twice -- and reported the last entry's count
            # for both of them (#3589).
            result.skipped.append((str(path), skipped))
            result.skipped_omitted.append((str(path), omitted))
        for page_num in parse_ranges(item.get("pages"), total):
            writer.add_page(reader.pages[page_num - 1])
            result.pages_written += 1
    if result.pages_written == 0:
        return result
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        writer.write(fh)
    return result


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
    try:
        result = merge(items, args.out)
    except PageSpecError as exc:
        # A ``pages`` value that does not parse is bad input like any other
        # unusable manifest, and is reported the same way.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if result.pages_written == 0:
        print(
            f"error: no requested page exists in any input; nothing written to {args.out}",
            file=sys.stderr,
        )
        return 2
    for (file_name, pages), (_file, more) in zip(
        result.skipped, result.skipped_omitted, strict=True
    ):
        dropped = ", ".join(str(p) for p in pages)
        tail = f" (and {more:,} more)" if more else ""
        print(f"warn: {file_name} has no page {dropped}{tail}", file=sys.stderr)
    _write_stdout(
        json.dumps(
            {
                "pages_written": result.pages_written,
                "out": str(args.out),
                "skipped_pages": [
                    {
                        "file": file_name,
                        "pages": pages,
                        # Only present when the list was capped, so an ordinary
                        # summary keeps exactly the shape it had before.
                        **({"omitted": more} if more else {}),
                    }
                    for (file_name, pages), (_file, more) in zip(
                        result.skipped, result.skipped_omitted, strict=True
                    )
                ],
                "missing_files": result.missing_files,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
