"""Apply cell edits to an existing `.xlsx`.

Operations:
    {"op": "set_cell", "sheet": "Q3", "row": 1, "col": 1, "value": "..."}
    {"op": "set_cell", "sheet": "Q3", "row": 2, "col": 2, "value": "=SUM(B3:B10)"}
    {"op": "set_cell", "sheet": "Q3", "row": 3, "col": 3, "value": "=hello", "as_text": true}
    {"op": "set_cell", "sheet": "Q3", "row": 4, "col": 1, "value": null}
    {"op": "rename_sheet", "old": "Sheet1", "new": "Summary"}
    {"op": "merge_cells", "sheet": "Q3", "range": "A1:C1"}

`value` semantics for `set_cell`:

* An explicit ``null`` **clears** the cell. It is the only way to express that
  in this op schema, and the cell's style is left alone.
* A **missing** ``value`` key is a malformed operation: it is skipped and not
  counted in ``applied``, so a typo cannot silently wipe data.
* ``0``, ``false`` and ``""`` are values, not absence, and are written as given.

``rename_sheet`` lands the sheet on exactly the name asked for, or does nothing.
A name another sheet already holds -- Excel compares sheet names without regard
to case -- is refused and not counted in ``applied``, because openpyxl would
otherwise store ``Summary1`` and report success.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.cell_range import CellRange

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skill_stdio import write_stdout as _write_stdout  # noqa: E402

# Distinguishes {"value": null} from an op with no "value" key at all.
# ``op.get("value")`` collapses both to None, which would make a malformed
# operation indistinguishable from a deliberate clear.
_MISSING = object()


def _coerce(value: Any, as_text: bool) -> Any:
    """Return the value to assign, honouring an explicit ``as_text`` request.

    ``as_text`` means "store exactly what I passed", so it suppresses the
    ISO-8601 coercion below as well as the formula interpretation. The cell
    *type* is what carries the distinction and that needs the cell object, so
    :func:`apply_ops` applies it after assignment; nothing is prepended to the
    data here. Excel's leading apostrophe is an input-mode escape rather than
    content, and writing it into the string left the cell holding ``'=hello``
    where the caller asked for ``=hello``.
    """
    if as_text:
        if isinstance(value, str) and value.startswith("'="):
            # ``SKILL.md`` offers ``'=hello`` and ``as_text: true`` as two
            # spellings of one request, so the two have to land on one cell.
            # Excel's leading apostrophe is the input escape for a
            # formula-looking value, so it is consumed here and carried as the
            # ``quotePrefix`` style flag by :func:`apply_ops` instead of being
            # stored as data. Scoped to ``'=``: a value that legitimately opens
            # with an apostrophe (``'tis``) keeps it.
            return value[1:]
        return value
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


#: Every op kind ``apply_ops`` knows. An op outside this set is a caller
#: mistake, not a no-op: the ops file is written by the agent one step before
#: the call, so ``set-cell`` for ``set_cell`` is a routine slip.
OP_KINDS = ("set_cell", "rename_sheet", "merge_cells")


class OpsError(ValueError):
    """An ops file that cannot be used. Reported as ``error:`` / exit 2, never
    as a traceback: the caller passed bad input, the script did not break."""


class CoordinateError(ValueError):
    """A ``set_cell`` row/column that is not a 1-based whole number.

    ``0`` and negatives reached ``Worksheet.cell`` and raised ``ValueError``
    from there, an unparseable one raised from ``int()``, and a fractional one
    was silently floored to a neighbouring cell. All are bad caller input and
    belong on the same ``error:`` / exit 2 path as :class:`OpsError`.
    """


def _cell_coord(value: Any, name: str) -> int:
    """Validate one 1-based row/column number, or raise :class:`CoordinateError`.

    ``bool`` is refused outright: ``True`` is an ``int`` (value 1), so
    ``"row": true`` would quietly edit row 1. A float is accepted only when it
    is whole (``1.0``), never floored (``1.7`` must not become ``1``).
    """
    if isinstance(value, bool):
        raise CoordinateError(f"{name} must be a whole number >= 1, got {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise CoordinateError(f"{name} must be a whole number >= 1, got {value!r}")
        value = int(value)
    if isinstance(value, int):
        if value < 1:
            raise CoordinateError(f"{name} must be at least 1, got {value}")
        return value
    raise CoordinateError(f"{name} must be a whole number >= 1, got {value!r}")


def _save_atomic(wb: Any, out: Path) -> None:
    """Save *wb* to *out* without ever leaving *out* half-written.

    ``wb.save`` truncates its destination up front, so a failure partway
    through leaves a broken zip behind. When ``--out`` points at the input
    file -- the in-place edit -- that broken zip is the caller's only copy.
    Writing a sibling temp file first and renaming it over the destination
    means an aborted save leaves *out* exactly as it was.
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


def load_ops(path: Path) -> list[dict[str, Any]]:
    """Read and validate the ops file, or raise :class:`OpsError`.

    Validation happens before the workbook is opened, so an unusable ops file
    cannot leave a half-applied workbook behind, and ``--out`` is never touched.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpsError(f"ops {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise OpsError(f"ops {path} must be a JSON array of operations, got {type(raw).__name__}")
    for index, op in enumerate(raw):
        if not isinstance(op, dict):
            raise OpsError(f"op {index} must be an object, got {type(op).__name__}")
        kind = op.get("op")
        if kind not in OP_KINDS:
            raise OpsError(
                f"op {index} has unknown kind {kind!r}; expected one of {', '.join(OP_KINDS)}"
            )
    return raw


def _free_temp_title(wb: Any) -> str:
    """A sheet title no sheet in *wb* currently holds, in any capitalisation."""
    taken = {name.casefold() for name in wb.sheetnames}
    index = 0
    while True:
        candidate = f"_rename_{index}"
        if candidate.casefold() not in taken:
            return candidate
        index += 1


def _rename_sheet(wb: Any, old: str, new: str) -> bool:
    """Rename *old* to exactly *new*; return whether that happened.

    openpyxl runs an assigned title through ``avoid_duplicate_name``, which
    compares case-insensitively against **every** sheet name -- the renamed
    sheet's own included -- and on a hit stores *new* with a number glued on
    rather than refusing. So renaming onto a name another sheet already held
    wrote ``Summary1``, and merely correcting a sheet's own capitalisation
    (``data`` -> ``Data``) wrote ``Data1``. Both reported ``applied``, and every
    later op addressing ``Summary`` then read and wrote the *other* sheet.

    A name another sheet holds is refused here, uncounted, the way this op list
    already treats a ``set_cell`` with no ``value``. A name only the renamed
    sheet itself holds is a legitimate request, so it goes through a free
    intermediate title: that clears the old spelling before the new one is
    claimed, leaving the title exactly as asked.
    """
    sheet = wb[old]
    if any(name != old and name.casefold() == new.casefold() for name in wb.sheetnames):
        return False
    if new == old:
        return True
    if new.casefold() == old.casefold():
        sheet.title = _free_temp_title(wb)
    sheet.title = new
    return True


def apply_ops(wb: Any, ops: list[dict[str, Any]]) -> int:
    applied = 0
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "set_cell":
            sheet_name = op.get("sheet")
            row = op.get("row")
            col = op.get("col")
            value = op.get("value", _MISSING)
            if sheet_name not in wb.sheetnames or row is None or col is None:
                continue
            if value is _MISSING:
                continue
            ws = wb[sheet_name]
            as_text = bool(op.get("as_text"))
            coerced = _coerce(value, as_text)
            try:
                row = _cell_coord(row, "row")
                col = _cell_coord(col, "col")
            except CoordinateError:
                # Unusable coordinates are refused input like a missing
                # ``value``: the op is skipped and uncounted, so the rest of
                # the batch still lands.
                continue
            # Assign through the property, not Worksheet.cell(value=...): that
            # helper ends with `if value is not None: cell.value = value`, so an
            # explicit null only *reads* the cell and the old value survives
            # while this loop still counts the edit as applied. Fetching the
            # cell first also leaves its style untouched.
            cell = ws.cell(row=row, column=col)
            cell.value = coerced
            if as_text and isinstance(coerced, str):
                # Assigning a string that starts with ``=`` makes openpyxl mark
                # the cell as a formula, so the string type has to be restored
                # afterwards. ``quotePrefix`` is the stored form of Excel's
                # apostrophe escape, which is why it belongs on the style and
                # not in the value.
                cell.data_type = "s"
                if coerced.startswith("="):
                    cell.quotePrefix = True
            applied += 1
        elif kind == "rename_sheet":
            old = op.get("old")
            new = op.get("new")
            if old in wb.sheetnames and isinstance(new, str) and _rename_sheet(wb, old, new):
                applied += 1
        elif kind == "merge_cells":
            sheet_name = op.get("sheet")
            rng = op.get("range")
            if sheet_name in wb.sheetnames and isinstance(rng, str):
                _merge_checked(wb[sheet_name], rng)
                applied += 1
    return applied


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edit an .xlsx via JSON op list.")
    parser.add_argument("input", type=Path)
    parser.add_argument("ops", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.input.is_file():
        print(f"error: input {args.input} not found", file=sys.stderr)
        return 2
    if not args.ops.is_file():
        print(f"error: ops {args.ops} not found", file=sys.stderr)
        return 2
    try:
        ops = load_ops(args.ops)
    except OpsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    wb = load_workbook(filename=str(args.input))
    applied = apply_ops(wb, ops)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        _save_atomic(wb, args.out)
    except Exception as exc:
        # A writer-side failure is refused input like any other: named on
        # stderr and exit 2, never a traceback -- and with ``_save_atomic``
        # the destination (which an in-place edit points at the input) is
        # left byte-for-byte as it was. ``apply_ops`` above is deliberately
        # outside this block: its refusals (an overlapping merge, a malformed
        # range) already surface as ``ValueError`` and must keep doing so.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _write_stdout(json.dumps({"applied": applied}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
