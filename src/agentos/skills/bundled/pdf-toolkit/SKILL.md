---
name: pdf-toolkit
description: "Structured `.pdf` operations: extract text/tables, merge pages from multiple PDFs, split a PDF by page ranges, fill PDF form fields, and generate fresh PDFs from JSON. Trigger when the user wants programmatic PDF work without natural-language rewriting — examples: pull tables from a report, combine three PDFs, extract pages 5-12, fill a tax form, or build a new PDF from data. Distinct from `nano-pdf`, which uses an LLM to rewrite a page from a sentence; this skill is deterministic byte-level work via pypdf, pdfplumber, and reportlab."
homepage: https://pypdf.readthedocs.io/
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/pdf
  maintained_by: AgentOS
metadata:
  {
    "platform":
      {
        "emoji": "📕",
        "requires": { "anyBins": ["python", "python3"] },
        "install":
          [
            {
              "id": "pypdf",
              "kind": "uv",
              "package": "pypdf",
              "label": "Install pypdf (uv pip)",
            },
            {
              "id": "reportlab",
              "kind": "uv",
              "package": "reportlab",
              "label": "Install reportlab (uv pip)",
            },
          ],
      },
  }
---

# pdf-toolkit

Deterministic, structural PDF operations. Use this skill for programmatic
work where you know exactly what you want done. Use the sibling `nano-pdf`
skill instead when the task is "rewrite this page to say X" — `nano-pdf`
applies a natural-language edit; `pdf-toolkit` applies an explicit operation.

## Decide the operation

| Goal | Script |
|---|---|
| Get text or tables out of a PDF | `extract.py` |
| Combine pages from multiple PDFs | `merge.py` |
| Split a PDF by page ranges | `split.py` |
| Fill `/Tx` form fields in a PDF | `form_fill.py` |
| Build a new PDF from data | inline `reportlab` snippet, see Path C below |

---

## Path A: Extract

```bash
{python} {baseDir}/scripts/extract.py /path/to/doc.pdf --json
```

Output:

```json
{
  "pages": 12,
  "metadata": {"title": "...", "author": "..."},
  "text": [
    {"page": 1, "content": "..."},
    {"page": 2, "content": "..."}
  ],
  "tables": [
    {"page": 3, "rows": [["..."], ["..."]]}
  ]
}
```

Text uses `pdfplumber` (already in default dependencies) which preserves
column layout better than naive PDF text extraction. Tables use
`pdfplumber.extract_tables()` with the `lines` strategy (ruling lines on both
axes); for borderless tables pass `--tables-strategy text` to detect rows and
columns from word positions instead. pdfplumber's `explicit` mode is not
offered because it needs line coordinates this script cannot supply.

For OCR (scanned PDFs), this skill does not include Tesseract — use the
sibling skill that wraps an OCR engine (out of scope here).

---

## Path B: Merge / Split

Merge full files:

```bash
{python} {baseDir}/scripts/merge.py a.pdf b.pdf c.pdf --out combined.pdf
```

Or merge specific page ranges with the manifest form:

```bash
{python} {baseDir}/scripts/merge.py manifest.json --out combined.pdf
```

`manifest.json`:

```json
[
  {"file": "a.pdf", "pages": "1-3"},
  {"file": "b.pdf", "pages": "5,7,9-11"},
  {"file": "c.pdf"}
]
```

Page ranges are 1-based, comma-separated, hyphen for ranges. Omit `pages` to
include the whole file. The JSON summary reports `pages_written` together with
any requested page the input does not have under `skipped_pages` (one entry per
file) and any input that was not found under `missing_files` — check both before
reporting the merge as done. A merge in which no requested page exists exits 2
and writes no file, rather than leaving a zero-page PDF behind.

Splits use the same syntax in reverse:

```bash
{python} {baseDir}/scripts/split.py input.pdf --pages "1-3,7,10-12" --out output_dir/
```

Each range writes one output file: `output_dir/input_001.pdf`,
`output_dir/input_002.pdf`, … The JSON summary lists each file with the pages
it holds under `parts`, and any requested page past the end of the document
under `skipped_pages` — check it before reporting the split as done. A spec
with no page in range exits 2 and writes nothing.

---

## Path C: Form fill

```bash
{python} {baseDir}/scripts/form_fill.py form.pdf data.json --out filled.pdf
```

`data.json` maps field name → string value. It must be a single JSON object —
a list of objects, or any other shape, is refused with exit 2 and no output
file is written:

```json
{
  "applicant_name": "Wei E.",
  "submission_date": "2026-05-06",
  "agreed": "Yes"
}
```

The script discovers fields via `pypdf.PdfReader.get_fields()` and updates
them with `update_page_form_field_values()`. Fields not present in the JSON
are left untouched. Run with `--list-fields` to enumerate the form's fields
without filling. If `form.pdf` has no AcroForm at all (a generated report, a
scan, anything that is not a form), the fill is refused with exit 2 and no
output file is written or overwritten — `--list-fields` is the way to check
first, and correctly returns `{}` for these.

Caveats:

- `/Btn` checkbox fields take the export value (often `Yes`, `On`, or `1`)
  rather than `true` — inspect with `--list-fields` to discover.
- AcroForm fills only. XFA forms (used by some legal templates) require
  Adobe-specific tooling and are out of scope.
- Filling a signed PDF invalidates its digital signature: `pypdf` rewrites
  the document, and the signature covers the bytes it replaces. `form_fill.py`
  has no flag to strip signatures first — signature operations are out of
  scope (see Boundaries) — so fill an unsigned copy when the signature has to
  survive.

---

## Path D: Generate from scratch

Use `reportlab` directly when you need a new PDF:

```python
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import LETTER
from pathlib import Path

c = canvas.Canvas(str(Path("out.pdf")), pagesize=LETTER)
c.setFont("Helvetica-Bold", 18)
c.drawString(72, 720, "Q3 Review")
c.setFont("Helvetica", 11)
c.drawString(72, 696, "Revenue grew 18% year over year.")
c.showPage()
c.save()
```

For tables, headers/footers, and multi-column layouts, switch to
`reportlab.platypus` (`SimpleDocTemplate`, `Paragraph`, `Table`,
`PageBreak`). See [references/reportlab.md](references/reportlab.md).

---

## Boundary with `nano-pdf`

`nano-pdf` (sibling bundled skill) wraps an LLM that takes a page index and
a natural-language instruction. Use it when the change is "fix the typo on
page 1" or "make the title shorter". Use **this** skill when the change is
"merge these three PDFs", "extract the tables", or "fill the form". The two
do not overlap: if you find yourself reaching for `nano-pdf` to do a merge,
switch to `pdf-toolkit`; if you reach here to "rewrite page 5 to be friendlier",
switch back.

---

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Extracted text is empty | Scanned PDF, no text layer | OCR is out of scope; use a separate OCR skill |
| Garbled characters in extract | PDF uses a custom font encoding | Try `pdfplumber.open(path, laparams={...})` with `char_margin` adjustments |
| Merged PDF is huge | Underlying PDFs include large embedded fonts | Subset fonts via `pypdf` `compress_content_streams()` |
| `merge.py` exits 2 with `error: manifest entry N ...` | The manifest is not an array of `{"file": ..., "pages": ...}` objects — a bare `["a.pdf"]` list of paths is the usual cause | Wrap each path: `[{"file": "a.pdf"}]`. `pages` is optional but must be a string when present (`"1-3"`, not `3`) |
| Form fill silently no-ops | Field name in JSON does not match PDF field name | Run with `--list-fields` first to see exact names |
| Pages out of order after split | Range overlap collapsed unexpectedly | Use disjoint ranges, e.g. `1-3,4-6` not `1-5,3-6` |

---

## Boundaries

- This skill works with text-based and form-based PDFs. Scanned image PDFs
  need OCR before any text path produces results.
- Encrypted PDFs are read-only here. Decryption requires the user-supplied
  password and is out of scope for this skill.
- For PDF-to-image rendering, use a separate skill that wraps Poppler or
  PyMuPDF.
- Digital signature operations (signing, verifying, revoking) are out of
  scope.
