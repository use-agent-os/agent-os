from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


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


def test_format_spreadsheet_non_positive_offset_normalization() -> None:
    """Non-positive offsets (0, negative) normalize row range to 1-indexed in footer."""
    sheets = [("Sheet1", [[f"val_{i}"] for i in range(1, 51)])]

    output_zero = fs._format_spreadsheet(
        path=Path("test.xlsx"),
        sheets=sheets,
        offset=0,
        limit=10,
    )
    assert "1\tval_1" in output_zero
    assert "10\tval_10" in output_zero
    assert "(Showing rows 1-10 of 50. Use offset=11 to continue.)" in output_zero
    assert "Showing rows 0-" not in output_zero

    output_negative = fs._format_spreadsheet(
        path=Path("test.xlsx"),
        sheets=sheets,
        offset=-5,
        limit=10,
    )
    assert "(Showing rows 1-10 of 50. Use offset=11 to continue.)" in output_negative
    assert "Showing rows -5-" not in output_negative


def test_format_spreadsheet_multi_sheet_starvation_annotation() -> None:
    """Multi-sheet workbooks annotate sheets where offset exceeds sheet row count."""
    sheets = [
        ("Sheet1", [[f"s1_r{i}"] for i in range(1, 51)]),
        ("Sheet2", [[f"s2_r{i}"] for i in range(1, 11)]),
        ("SheetEmpty", []),
    ]

    output = fs._format_spreadsheet(
        path=Path("data.xlsx"),
        sheets=sheets,
        offset=25,
        limit=10,
    )

    # Sheet1 has 50 rows, so rows 25-34 are displayed
    assert "Sheet: Sheet1 (50 rows x 1 columns)" in output
    assert "25\ts1_r25" in output
    assert "(Showing rows 25-34 of 50. Use offset=35 to continue.)" in output

    # Sheet2 has 10 rows, offset=25 is beyond row count
    assert "Sheet: Sheet2 (10 rows x 1 columns)" in output
    assert "(Offset 25 is beyond sheet row count of 10.)" in output

    # Empty sheet (0 rows) does not falsely report offset beyond rows
    assert "Sheet: SheetEmpty (0 rows x 0 columns)" in output
    assert "beyond sheet row count of 0" not in output


@pytest.mark.asyncio
async def test_read_spreadsheet_csv_pagination_e2e(tmp_path: Path) -> None:
    """read_spreadsheet correctly paginates and normalizes offset on CSV files."""
    csv_file = tmp_path / "records.csv"
    csv_file.write_text(
        "\n".join(f"colA_{i},colB_{i}" for i in range(1, 31)),
        encoding="utf-8",
    )

    with tool_context(tmp_path):
        res_page1 = await fs.read_spreadsheet(str(csv_file), offset=0, limit=5)
        assert "(Showing rows 1-5 of 30. Use offset=6 to continue.)" in res_page1
        assert "1\tcolA_1\tcolB_1" in res_page1
        assert "5\tcolA_5\tcolB_5" in res_page1

        res_page2 = await fs.read_spreadsheet(str(csv_file), offset=26, limit=10)
        assert "26\tcolA_26\tcolB_26" in res_page2
        assert "30\tcolA_30\tcolB_30" in res_page2
        assert "Use offset=" not in res_page2
