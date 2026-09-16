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
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

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
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def apply_ops(
    wb: Any,
    ops: list[dict[str, Any]],
    skipped: list[str] | None = None,
) -> int:
    """Apply *ops* to *wb* and return how many took effect.

    Operations that cannot be applied are skipped rather than raising, so one
    bad entry never costs the whole batch. That silence is the problem when
    *every* entry is bad: the caller is told ``{"applied": 0}`` with exit 0 and
    a written workbook, which reads as "the edits went through".

    Passing a list as *skipped* collects one line per skipped operation saying
    which index it was and why. The parameter is optional so the existing
    ``apply_ops(wb, ops) -> int`` calls keep working unchanged.
    """
    applied = 0

    def skip(index: int, reason: str) -> None:
        if skipped is not None:
            skipped.append(f"op {index}: {reason}")

    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            skip(index, f"is not an object, got {type(op).__name__}")
            continue
        kind = op.get("op")
        if kind == "set_cell":
            sheet_name = op.get("sheet")
            row = op.get("row")
            col = op.get("col")
            value = op.get("value", _MISSING)
            if sheet_name not in wb.sheetnames:
                skip(index, f"set_cell names sheet {sheet_name!r}, which is not in the workbook")
                continue
            if row is None or col is None:
                skip(index, "set_cell is missing row or col")
                continue
            if value is _MISSING:
                skip(index, "set_cell has no value key (use an explicit null to clear a cell)")
                continue
            ws = wb[sheet_name]
            as_text = bool(op.get("as_text"))
            coerced = _coerce(value, as_text)
            # Assign through the property, not Worksheet.cell(value=...): that
            # helper ends with `if value is not None: cell.value = value`, so an
            # explicit null only *reads* the cell and the old value survives
            # while this loop still counts the edit as applied. Fetching the
            # cell first also leaves its style untouched.
            cell = ws.cell(row=int(row), column=int(col))
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
            if old in wb.sheetnames and isinstance(new, str):
                wb[old].title = new
                applied += 1
            elif old not in wb.sheetnames:
                skip(index, f"rename_sheet names sheet {old!r}, which is not in the workbook")
            else:
                skip(index, "rename_sheet needs a string 'new' name")
        elif kind == "merge_cells":
            sheet_name = op.get("sheet")
            rng = op.get("range")
            if sheet_name in wb.sheetnames and isinstance(rng, str):
                wb[sheet_name].merge_cells(rng)
                applied += 1
            elif sheet_name not in wb.sheetnames:
                skip(index, f"merge_cells names sheet {sheet_name!r}, which is not in the workbook")
            else:
                skip(index, "merge_cells needs a string 'range'")
        elif kind is None:
            skip(index, "has no 'op' key")
        else:
            skip(index, f"has unknown op {kind!r}")
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
        raw = json.loads(args.ops.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Both mean "not a JSON document": a UTF-16 file from PowerShell's
        # Out-File is as unusable as a truncated one, and a traceback is a
        # worse answer than either.
        print(f"error: ops {args.ops} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    # A single op object is the routine slip here, and coercing it to [] used
    # to apply nothing, save the workbook and still exit 0.
    if not isinstance(raw, list):
        print(
            f"error: ops {args.ops} must be a JSON list of operations, "
            f"got {type(raw).__name__}",
            file=sys.stderr,
        )
        return 2
    ops = raw
    wb = load_workbook(filename=str(args.input))
    skipped: list[str] = []
    applied = apply_ops(wb, ops, skipped)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(args.out))
    # A well-formed list whose operations all fail reaches the same dead end
    # the ops-file check just closed: nothing applied, exit 0, a workbook on
    # disk. Naming what was dropped is what stops "applied: 0" reading as
    # success. This goes to stderr rather than into the stdout JSON because
    # that object is pinned exactly by tests/test_skill_xlsx.py, and the exit
    # code stays 0 because a partly-applied batch is a normal outcome.
    for reason in skipped:
        print(f"warning: {reason}", file=sys.stderr)
    if ops and applied == 0:
        print(
            f"warning: none of the {len(ops)} operations applied; "
            f"{args.out} is a copy of the input",
            file=sys.stderr,
        )
    print(json.dumps({"applied": applied}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
