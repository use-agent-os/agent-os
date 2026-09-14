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
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook


def _coerce(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 19 and value[10] == "T":
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def build(spec: dict[str, Any]) -> Workbook:
    wb = Workbook()
    if not isinstance(spec, dict):
        return wb

    default_sheet = wb.active
    raw_sheets = spec.get("sheets")
    if not isinstance(raw_sheets, list) or not raw_sheets:
        return wb

    valid_sheets = [s for s in raw_sheets if isinstance(s, dict)]
    if not valid_sheets:
        return wb

    for idx, sheet_spec in enumerate(valid_sheets):
        sheet_name = sheet_spec.get("name")
        title = (
            str(sheet_name)
            if sheet_name is not None and str(sheet_name).strip()
            else (f"Sheet{idx + 1}" if idx > 0 else "Sheet1")
        )
        if idx == 0:
            ws = default_sheet if default_sheet is not None else wb.create_sheet()
            ws.title = title
        else:
            ws = wb.create_sheet(title=title)

        raw_rows = sheet_spec.get("rows")
        if isinstance(raw_rows, list):
            for row in raw_rows:
                if isinstance(row, (list, tuple)):
                    ws.append([_coerce(v) for v in row])
                elif row is not None:
                    ws.append([_coerce(row)])
                else:
                    ws.append([])

        raw_merged = sheet_spec.get("merged")
        if isinstance(raw_merged, list):
            for merged in raw_merged:
                if isinstance(merged, str):
                    ws.merge_cells(merged)
                elif (
                    isinstance(merged, dict)
                    and "range" in merged
                    and isinstance(merged["range"], str)
                ):
                    ws.merge_cells(str(merged["range"]))

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
    args = _parse_args()
    if not args.spec.is_file():
        print(f"error: spec {args.spec} not found", file=sys.stderr)
        return 2
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        print(f"error: invalid JSON spec: {err}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict):
        print("error: JSON spec must be an object", file=sys.stderr)
        return 2
    wb = build(spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
