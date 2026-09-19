"""Extract text and tables from a PDF using pdfplumber + pypdf metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skills.stdio import write_stdout as _write_stdout  # noqa: E402

# pdfplumber's third mode, ``explicit``, needs ``explicit_vertical_lines`` /
# ``explicit_horizontal_lines`` that this script has no way to supply, so it
# crashed inside pdfplumber on every call. It is deliberately not offered.
TABLE_STRATEGIES = ("lines", "text")


def _table_settings(tables_strategy: str | None) -> dict[str, str]:
    """pdfplumber table settings for *tables_strategy* (default ``lines``).

    The strategy applies to both axes: setting only ``vertical_strategy`` left
    the horizontal axis on ``lines``, so ``text`` never found a row in the
    borderless tables it exists for.
    """
    strategy = tables_strategy or "lines"
    if strategy not in TABLE_STRATEGIES:
        raise ValueError(
            f"unsupported --tables-strategy {strategy!r}; "
            f"choose one of: {', '.join(TABLE_STRATEGIES)}"
        )
    return {"vertical_strategy": strategy, "horizontal_strategy": strategy}


def extract(path: Path, tables_strategy: str | None) -> dict[str, Any]:
    reader = PdfReader(str(path))
    metadata: dict[str, Any] = {}
    if reader.metadata is not None:
        for key, value in reader.metadata.items():
            metadata[str(key).lstrip("/")] = str(value)

    pages_text: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    table_settings = _table_settings(tables_strategy)
    with pdfplumber.open(str(path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            content = page.extract_text() or ""
            pages_text.append({"page": idx, "content": content})
            for tbl in page.extract_tables(table_settings) or []:
                tables.append({"page": idx, "rows": tbl})

    return {
        "pages": len(pages_text),
        "metadata": metadata,
        "text": pages_text,
        "tables": tables,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract text and tables from a PDF.")
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--tables-strategy",
        choices=TABLE_STRATEGIES,
        default=None,
        help="pdfplumber table-detection strategy for both axes (default: lines)",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Force JSON output (default)")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.path.is_file():
        print(f"error: {args.path} not found", file=sys.stderr)
        return 2
    payload = extract(args.path, args.tables_strategy)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        _write_stdout(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
