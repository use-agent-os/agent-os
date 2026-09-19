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


#: Every op kind ``apply_ops`` knows. An op outside this set is a caller
#: mistake, not a no-op: the ops file is written by the agent one step before
#: the call, so ``set-cell`` for ``set_cell`` is a routine slip.
OP_KINDS = ("set_cell", "rename_sheet", "merge_cells")


class OpsError(ValueError):
    """An ops file that cannot be used. Reported as ``error:`` / exit 2, never
    as a traceback: the caller passed bad input, the script did not break."""


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
        elif kind == "merge_cells":
            sheet_name = op.get("sheet")
            rng = op.get("range")
            if sheet_name in wb.sheetnames and isinstance(rng, str):
                wb[sheet_name].merge_cells(rng)
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
    wb.save(str(args.out))
    _write_stdout(json.dumps({"applied": applied}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
