"""Regression tests for issue #1569: read_spreadsheet exact sheet name match over numeric index."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.builtin.filesystem import _select_spreadsheet_sheets
from agentos.tools.types import CallerKind, ToolContext, ToolError, current_tool_context

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "xlsx" / "scripts"


@contextmanager
def tool_context(workspace: Path) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
            workspace_strict=True,
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


def test_select_spreadsheet_sheets_exact_numeric_name_match() -> None:
    """A requested sheet name of '1' must match the sheet named '1', not the 1st sheet."""
    sheets: list[tuple[str, list[list[str]]]] = [
        ("Summary", [["ColA", "ColB"], ["val1", "val2"]]),
        ("1", [["DataA", "DataB"], ["item1", "item2"]]),
        ("2024", [["Year", "Total"], ["2024", "100"]]),
    ]

    selected_1 = _select_spreadsheet_sheets(sheets, "1")
    assert len(selected_1) == 1
    assert selected_1[0][0] == "1"
    assert selected_1[0][1][1] == ["item1", "item2"]

    selected_2024 = _select_spreadsheet_sheets(sheets, "2024")
    assert len(selected_2024) == 1
    assert selected_2024[0][0] == "2024"
    assert selected_2024[0][1][1] == ["2024", "100"]


def test_select_spreadsheet_sheets_positional_index_fallback() -> None:
    """If no sheet has the exact name '1', a numeric string or int falls back to 1-based index."""
    sheets: list[tuple[str, list[list[str]]]] = [
        ("Overview", [["OverviewData"]]),
        ("Details", [["DetailsData"]]),
    ]

    # 1-based index as int
    assert _select_spreadsheet_sheets(sheets, 1)[0][0] == "Overview"
    assert _select_spreadsheet_sheets(sheets, 2)[0][0] == "Details"

    # 1-based index as string
    assert _select_spreadsheet_sheets(sheets, "1")[0][0] == "Overview"
    assert _select_spreadsheet_sheets(sheets, "2")[0][0] == "Details"


def test_select_spreadsheet_sheets_case_insensitive_fallback() -> None:
    """Case-insensitive name matching works when exact match and positional index do not match."""
    sheets: list[tuple[str, list[list[str]]]] = [
        ("SheetSummary", [["Data"]]),
    ]
    assert _select_spreadsheet_sheets(sheets, "sheetsummary")[0][0] == "SheetSummary"


def test_select_spreadsheet_sheets_not_found_raises_tool_error() -> None:
    sheets: list[tuple[str, list[list[str]]]] = [
        ("Summary", [["Data"]]),
        ("1", [["Data1"]]),
    ]
    with pytest.raises(
        ToolError, match="Sheet not found: MissingSheet. Available sheets: Summary, 1"
    ):
        _select_spreadsheet_sheets(sheets, "MissingSheet")


@pytest.mark.asyncio
async def test_read_spreadsheet_xlsx_numeric_sheet_name_e2e(tmp_path: Path) -> None:
    """End-to-end read_spreadsheet prioritizing sheet named '1' over 1st sheet 'Summary'."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_xlsx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    spec = {
        "sheets": [
            {
                "name": "Summary",
                "rows": [["Header", "Overview"], ["Row1", "OverviewValue"]],
            },
            {
                "name": "1",
                "rows": [["Header", "NumericSheetData"], ["Row1", "TargetValue"]],
            },
        ]
    }
    xlsx_path = tmp_path / "workbook.xlsx"
    create_xlsx.build(spec).save(str(xlsx_path))

    with tool_context(tmp_path):
        result = await fs.read_spreadsheet(str(xlsx_path), sheet="1")
        assert "Sheet: 1" in result
        assert "TargetValue" in result
        assert "OverviewValue" not in result
