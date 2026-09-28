"""Create a `.xlsx` workbook from a JSON spec.

Spec:
    {
      "sheets": [
        {
          "name": "Sales",
          "rows": [["A", "B"], [1, "=B1*2"]],
          "merged": [{"range": "A1:B1"}],
          "freeze": "A2"
        }
      ]
    }
Entries in "merged" may also be range strings, e.g. "A1:B1", as returned by
inspect_xlsx. Strings and {"range": ...} objects can be mixed in the same list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.worksheet.cell_range import CellRange

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skill_stdio import configure_utf8_stdio  # noqa: E402


def _coerce(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 19 and value[10] == "T":
        try:
            parsed = datetime.fromisoformat(value)
            # ``fromisoformat`` keeps a trailing ``Z`` / ``±HH:MM`` as an
            # offset-aware datetime, and openpyxl refuses to serialise those
            # (``TypeError: Excel does not support timezones in datetimes``).
            # Excel has no timezone type at all, so the offset is dropped and
            # the value is kept as the wall-clock time it spells out -- exactly
            # what the naive spelling already stored.
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            return value
    return value


def _save_atomic(wb: Workbook, out: Path) -> None:
    """Save *wb* to *out* without ever leaving *out* half-written.

    ``wb.save`` truncates its destination up front, so a failure partway
    leaves a broken 3-entry zip behind -- and if *out* is where the caller's
    only copy lived, that copy is gone. A sibling temp file plus rename means
    an aborted save leaves *out* exactly as it was.
    """
    tmp = out.with_name(out.name + ".tmp")
    try:
        wb.save(str(tmp))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, out)


def _merge_checked(ws: Any, rng: str) -> None:
    """Merge *rng*, refusing one that intersects a merge the sheet already has.

    openpyxl accepts an intersecting range and writes a workbook with
    overlapping ``mergeCell`` entries, which Excel reports as corrupt and
    repairs on open, while the run reported success. A malformed range already
    failed loudly and left nothing written (#1993); an overlapping one now
    fails the same way, naming both ranges so the caller can correct it.
    ``CellRange`` raises the very error ``merge_cells`` would for a malformed
    range, so that path is unchanged.

    An *identical* range is not an overlap: it produces the same workbook,
    openpyxl already dedupes it, and re-applying an ops file to a workbook
    that has the merge must stay the no-op it has always been. ``CellRange``
    equality normalises the spelling, so ``a1:b1`` matches ``A1:B1``.
    """
    target = CellRange(rng)
    existing_ranges = list(ws.merged_cells.ranges)
    if any(target == existing for existing in existing_ranges):
        return
    for existing in existing_ranges:
        if not target.isdisjoint(existing):
            raise ValueError(
                f"cannot merge {rng} on sheet {ws.title!r}: it overlaps the existing "
                f"merged range {existing.coord}"
            )
    ws.merge_cells(rng)


def build(spec: Any) -> Workbook:
    wb = Workbook()
    if not isinstance(spec, dict):
        return wb

    default_sheet = wb.active
    raw_sheets = spec.get("sheets")
    if not isinstance(raw_sheets, (list, tuple)):
        return wb
    sheets = [s for s in raw_sheets if isinstance(s, dict)]
    if not sheets:
        return wb

    for idx, sheet_spec in enumerate(sheets):
        if idx == 0:
            ws = default_sheet
            ws.title = str(sheet_spec.get("name") or "Sheet1")
        else:
            ws = wb.create_sheet(title=str(sheet_spec.get("name") or f"Sheet{idx + 1}"))

        raw_rows = sheet_spec.get("rows")
        if isinstance(raw_rows, (list, tuple)):
            for row in raw_rows:
                # A scalar row (str/int/None/...) becomes a single-cell row
                # rather than being iterated -- a bare string would otherwise
                # be split into one cell per character, and a non-iterable
                # scalar like an int would raise TypeError outright.
                values = row if isinstance(row, (list, tuple)) else [row]
                ws.append([_coerce(v) for v in values])

        raw_merged = sheet_spec.get("merged")
        if isinstance(raw_merged, (list, tuple)):
            for merged in raw_merged:
                if isinstance(merged, str):
                    _merge_checked(ws, merged)
                elif (
                    isinstance(merged, dict)
                    and "range" in merged
                    and isinstance(merged["range"], str)
                ):
                    _merge_checked(ws, merged["range"])

        freeze = sheet_spec.get("freeze")
        if isinstance(freeze, str) and freeze:
            ws.freeze_panes = freeze

    return wb


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a .xlsx from a JSON spec.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    configure_utf8_stdio()
    args = _parse_args()
    if not args.spec.is_file():
        print(f"error: spec {args.spec} not found", file=sys.stderr)
        return 2
    try:
        raw_spec = args.spec.read_text(encoding="utf-8")
        spec = json.loads(raw_spec)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"error: invalid JSON spec: {exc}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict):
        print("error: JSON spec must be an object", file=sys.stderr)
        return 2
    wb = build(spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        _save_atomic(wb, args.out)
    except Exception as exc:
        # A writer-side failure is refused input like any other: named on
        # stderr and exit 2, never a traceback -- and ``_save_atomic`` has
        # already removed the temp file, so ``args.out`` is untouched (or
        # absent). ``build`` above is deliberately outside this block: its
        # refusals (an overlapping merge, a malformed range) already surface
        # as ``ValueError`` and must keep doing so.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
