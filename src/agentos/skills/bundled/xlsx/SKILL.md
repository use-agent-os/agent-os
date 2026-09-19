---
name: xlsx
description: "Read, edit, or create Microsoft Excel `.xlsx` workbooks. Trigger this skill whenever the user mentions a spreadsheet, .xlsx file, workbook, sheet, formula, pivot table, or asks to extract tabular data, modify a sheet, or build a workbook from rows. Three execution paths: structured inspection, in-place cell edits, and create-from-scratch via openpyxl. Values starting with `=` are written as formulas; everything else is a literal value with type preserved (int / float / str / datetime)."
homepage: https://openpyxl.readthedocs.io/
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/excel-xlsx
  maintained_by: AgentOS
metadata:
  {
    "platform":
      {
        "emoji": "📗",
        "requires": { "anyBins": ["python", "python3"] },
        "install":
          [
            {
              "id": "openpyxl",
              "kind": "uv",
              "package": "openpyxl",
              "label": "Install openpyxl (uv pip)",
            },
          ],
      },
  }
---

# xlsx

Work with `.xlsx` workbooks. The format is OOXML SpreadsheetML — a zip
container of XML parts. Treat each cell as a typed value: a number, a string,
a datetime, or a formula. Mixing the four causes Excel to flag the workbook
or compute incorrect totals.

## Decide the path first

| You have | Goal | Path |
|---|---|---|
| Existing `.xlsx` | Read sheets and cells | A. Inspect |
| Existing `.xlsx` | Modify specific cells | B. Edit-in-place |
| Nothing or a brief | Build a new workbook | C. Create from scratch |

If the user provides a workbook to update, default to path B and treat the
input as the formatting baseline. Choose path C only when the user says
"start fresh".

---

## Path A: Inspect

```bash
{python} {baseDir}/scripts/inspect_xlsx.py /path/to/book.xlsx
```

Output:

```json
{
  "sheets": [
    {
      "name": "Q3",
      "max_row": 10,
      "max_col": 5,
      "rows": [
        [
          {"value": "Metric", "type": "s"},
          {"value": "Value", "type": "s"}
        ],
        [
          {"value": "Revenue", "type": "s"},
          {"value": 2100000, "type": "n"}
        ]
      ]
    }
  ]
}
```

`type` follows openpyxl conventions: `n` (number), `s` (string), `d`
(datetime), `f` (formula), `b` (bool), `e` (error), `inlineStr` (inline
string). The helper script reads with `data_only=False` so formula expressions
are returned literally; pass `--data-only` to get the cached computed result
instead.

---

## Path B: Edit in place

```bash
{python} {baseDir}/scripts/edit_xlsx.py book.xlsx ops.json --out edited.xlsx
```

`ops.json`:

```json
[
  {"op": "set_cell", "sheet": "Q3", "row": 2, "col": 2, "value": "=SUM(B3:B10)"},
  {"op": "set_cell", "sheet": "Q3", "row": 5, "col": 1, "value": "Net margin"},
  {"op": "set_cell", "sheet": "Q3", "row": 6, "col": 1, "value": null},
  {"op": "rename_sheet", "old": "Sheet1", "new": "Summary"}
]
```

Rules:

- Rows and columns are 1-based (Excel convention).
- An explicit `"value": null` **clears** the cell and keeps its style. It is
  the only way to empty a cell through this op list.
- Omitting `value` entirely is a malformed operation: it is skipped and not
  counted in `applied`, so a typo cannot silently wipe a cell.
- The ops file itself must be a JSON **array** of objects, each with a known
  `op` (`set_cell`, `rename_sheet`, `merge_cells`). An unparseable file, a
  non-array, or an unknown `op` exits 2 with `error: …` and writes nothing —
  the file is validated before the workbook is opened, so a bad op list cannot
  leave a half-applied workbook or overwrite `--out` with an unchanged copy.
- `0`, `false` and `""` are values, not absence. Note that Excel has no
  empty-string cell, so `""` reads back as empty — use `null` when you mean
  "clear this cell".
- Strings starting with `=` are written as formulas (`cell.value = "=..."`),
  matching openpyxl behavior. To write a literal `=hello`, pass
  `as_text: true` — either as `{"value": "=hello", "as_text": true}` or with
  Excel's leading-apostrophe escape, `{"value": "'=hello", "as_text": true}`.
  Both produce the same cell: the value is stored as `=hello` in a text cell,
  and the apostrophe is carried on the cell's `quotePrefix` style flag rather
  than inside the value. The apostrophe is only consumed when it escapes a
  formula, so a value that genuinely begins with one (`'tis`) keeps it.
- Datetimes go in as ISO 8601 strings (`"2026-05-06T09:00:00"`); the helper
  parses them back to `datetime` objects so Excel renders the cell with date
  format. Pass `as_text: true` to keep such a string as text instead.
- Editing a cell does not recalculate dependent formulas. Excel and
  LibreOffice recalculate on open. If you need cached values immediately,
  use a calculation engine (out of scope here).

---

## Path C: Create from scratch

```bash
{python} {baseDir}/scripts/create_xlsx.py spec.json --out out.xlsx
```

Spec:

```json
{
  "sheets": [
    {
      "name": "Sales",
      "rows": [
        ["Region", "Revenue", "Growth"],
        ["NA", 1200000, "=B2/SUM($B$2:$B$4)"],
        ["EU", 850000, "=B3/SUM($B$2:$B$4)"]
      ],
      "merged": [{"range": "A1:C1"}],
      "freeze": "A2"
    }
  ]
}
```

`merged` accepts range strings (`["A1:C1"]`), objects with a `range` key
(`[{"range": "A1:C1"}]`), or a mixture of both. The string format matches
the `merged` list returned by `inspect_xlsx.py`. This compatibility applies
to merge metadata only; inspected `rows` contain cell objects rather than
the plain values required by the creation spec. Invalid range coordinates
raise an error from openpyxl.

For programmatic use:

```python
from openpyxl import Workbook
wb = Workbook()
ws = wb.active
ws.title = "Sales"
ws.append(["Region", "Revenue"])
ws.append(["NA", 1_200_000])
ws["C2"] = "=B2*1.05"          # formula
ws.merge_cells("A1:B1")
ws.freeze_panes = "A2"
wb.save("out.xlsx")
```

See [references/openpyxl.md](references/openpyxl.md) for styles, conditional
formatting, charts, and formula references.

---

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Cell shows `=SUM(...)` as text, not the result | Wrote the string with `as_text: true` or workbook lacks cached values | Open in Excel and save once; or use a calc engine |
| Date renders as a serial number (45000) | Wrote `int` instead of `datetime` | Pass an ISO string and let the helper parse; or set `cell.number_format` |
| Merged range loses borders | Borders apply to the top-left cell only after merge | Apply border to the top-left cell post-merge |
| Workbook breaks Excel after edit | Removed a defined name without updating dependent formulas | Audit `defined_names` before delete |
| Pivot tables disappear | openpyxl drops pivot caches on save | Edit pivots in Excel; programmatic edit is not supported |

---

## Boundaries

- This skill handles `.xlsx` (OOXML SpreadsheetML). It does **not** handle
  `.xls` (legacy binary), `.xlsm` (macro-enabled), or Google Sheets. Convert
  via Excel or LibreOffice export first.
- Pivot tables, slicers, and pivot caches are read-only here.
- For datasets larger than ~100k rows or 50MB workbooks, prefer pandas +
  `to_excel` with the `xlsxwriter` engine; openpyxl loads the whole workbook
  into memory.
