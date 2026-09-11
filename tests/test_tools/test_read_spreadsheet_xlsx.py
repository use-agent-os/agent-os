from __future__ import annotations

import zipfile
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


def _build_xlsx_bytes(sheets: dict[str, str], shared_strings: list[str] | None = None) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # xl/workbook.xml
        sheet_tags = []
        rels_tags = []
        for idx, (name, _) in enumerate(sheets.items(), start=1):
            r_id = f"rId{idx}"
            sheet_path = f"worksheets/sheet{idx}.xml"
            sheet_tags.append(
                f'<sheet name="{name}" sheetId="{idx}" '
                f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                f'r:id="{r_id}"/>'
            )
            type_uri = (
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
            )
            rels_tags.append(f'<Relationship Id="{r_id}" Type="{type_uri}" Target="{sheet_path}"/>')

        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheets>{''.join(sheet_tags)}</sheets>"
            "</workbook>"
        )
        zf.writestr("xl/workbook.xml", workbook_xml)

        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{''.join(rels_tags)}"
            "</Relationships>"
        )
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)

        if shared_strings:
            si_nodes = "".join(f"<si><t>{s}</t></si>" for s in shared_strings)
            shared_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{si_nodes}</sst>'
            )
            zf.writestr("xl/sharedStrings.xml", shared_xml)

        for idx, (_, sheet_xml) in enumerate(sheets.items(), start=1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml)

    return buf.getvalue()


def test_read_xlsx_worksheet_sparse_rows() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <sheetData>
            <row r="1">
                <c r="A1" t="inlineStr"><is><t>Header A</t></is></c>
                <c r="B1" t="inlineStr"><is><t>Header B</t></is></c>
            </row>
            <row r="4">
                <c r="A4" t="inlineStr"><is><t>Row 4</t></is></c>
            </row>
            <row r="6">
                <c r="B6" t="inlineStr"><is><t>Row 6 Col B</t></is></c>
            </row>
        </sheetData>
    </worksheet>"""

    rows, total_rows = fs._read_xlsx_worksheet(xml, [])
    assert total_rows == 6
    # Only rows actually present in the XML are keyed -- omitted rows 2, 3,
    # and 5 are real empty rows, not materialised placeholders.
    assert set(rows) == {1, 4, 6}
    assert rows[1] == ["Header A", "Header B"]
    assert rows[4] == ["Row 4"]
    assert rows[6] == ["", "Row 6 Col B"]


def test_read_xlsx_worksheet_leading_empty_rows() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <sheetData>
            <row r="3">
                <c r="A3" t="inlineStr"><is><t>Starts at row 3</t></is></c>
            </row>
        </sheetData>
    </worksheet>"""

    rows, total_rows = fs._read_xlsx_worksheet(xml, [])
    assert total_rows == 3
    assert set(rows) == {3}
    assert rows[3] == ["Starts at row 3"]


def test_read_xlsx_worksheet_contiguous_rows() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <sheetData>
            <row r="1">
                <c r="A1" t="inlineStr"><is><t>Row 1</t></is></c>
            </row>
            <row r="2">
                <c r="A2" t="inlineStr"><is><t>Row 2</t></is></c>
            </row>
        </sheetData>
    </worksheet>"""

    rows, total_rows = fs._read_xlsx_worksheet(xml, [])
    assert total_rows == 2
    assert rows[1] == ["Row 1"]
    assert rows[2] == ["Row 2"]


@pytest.mark.asyncio
async def test_read_spreadsheet_xlsx_sparse_rows_and_pagination(tmp_path: Path) -> None:
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1">'
        '<c r="A1" t="inlineStr"><is><t>Header</t></is></c>'
        "</row>"
        '<row r="5">'
        '<c r="A5" t="inlineStr"><is><t>Value 5</t></is></c>'
        "</row>"
        '<row r="8">'
        '<c r="A8" t="inlineStr"><is><t>Value 8</t></is></c>'
        "</row>"
        "</sheetData>"
        "</worksheet>"
    )

    xlsx_bytes = _build_xlsx_bytes({"DataSheet": sheet_xml})
    target = tmp_path / "test.xlsx"
    target.write_bytes(xlsx_bytes)

    with tool_context(tmp_path):
        # 1. Reading from beginning (offset=1, limit=5)
        out_page1 = await fs.read_spreadsheet(str(target), offset=1, limit=5)
        assert "Sheet: DataSheet (8 rows x 1 columns)" in out_page1
        assert "1\tHeader" in out_page1
        assert "2\t" in out_page1
        assert "3\t" in out_page1
        assert "4\t" in out_page1
        assert "5\tValue 5" in out_page1
        assert "Value 8" not in out_page1

        # 2. Reading with offset=5 (should show row 5 and following rows up to limit)
        out_page2 = await fs.read_spreadsheet(str(target), offset=5, limit=5)
        assert "5\tValue 5" in out_page2
        assert "6\t" in out_page2
        assert "7\t" in out_page2
        assert "8\tValue 8" in out_page2
        assert "1\tHeader" not in out_page2

        # 3. Reading with offset=8
        out_page3 = await fs.read_spreadsheet(str(target), offset=8, limit=5)
        assert "8\tValue 8" in out_page3
        assert "5\tValue 5" not in out_page3


@pytest.mark.asyncio
async def test_read_spreadsheet_pagination_crosses_a_gap_wider_than_limit(
    tmp_path: Path,
) -> None:
    """A legitimate sparse sheet with data at row 1 and row 5000 and nothing
    between must still be reachable through the tool's own continuation
    offsets, at the default limit (200) -- a window that measures "how many
    rows did this call materialise" instead of "how far did the real row
    numbers get to" stalls here: every page in the gap materialises zero
    rows, so the suggested next offset stops advancing and row 5000 is
    never reached (#1149 follow-ups)."""
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1"><c r="A1" t="inlineStr"><is><t>first</t></is></c></row>'
        '<row r="5000"><c r="A5000" t="inlineStr"><is><t>last</t></is></c></row>'
        "</sheetData>"
        "</worksheet>"
    )
    xlsx_bytes = _build_xlsx_bytes({"Data": sheet_xml})
    target = tmp_path / "gap.xlsx"
    target.write_bytes(xlsx_bytes)

    with tool_context(tmp_path):
        seen_offsets = [1]
        out = await fs.read_spreadsheet(str(target), offset=1)
        for _ in range(50):
            assert "Sheet: Data (5000 rows x 1 columns)" in out
            if "last" in out:
                break
            marker = "Use offset="
            idx = out.rindex(marker) + len(marker)
            next_offset = int(out[idx:].split(" ", 1)[0].rstrip(".)"))
            assert next_offset > seen_offsets[-1], (
                f"continuation offset did not advance past {seen_offsets[-1]} "
                f"(pagination stalled in the gap)"
            )
            seen_offsets.append(next_offset)
            out = await fs.read_spreadsheet(str(target), offset=next_offset)
        else:
            raise AssertionError("never reached row 5000 within 50 pages")

        assert "5000\tlast" in out
        assert seen_offsets == [1, 201, 401, 601, 801, 1001, 1201, 1401, 1601, 1801, 2001,
                                 2201, 2401, 2601, 2801, 3001, 3201, 3401, 3601, 3801, 4001,
                                 4201, 4401, 4601, 4801]


# ---------------------------------------------------------------------------
# _format_spreadsheet: offset normalisation and multi-sheet pagination (#1149
# scope note folding in #1402's non-positive-offset and multi-sheet reports)
# ---------------------------------------------------------------------------


def _sheet(name: str, n: int, *, label: str) -> tuple[str, dict[int, list[str]], int]:
    rows = {i: [f"{label}{i}"] for i in range(1, n + 1)}
    return (name, rows, len(rows))


def test_format_spreadsheet_offset_one_is_unchanged() -> None:
    """Currently-correct case: a plain offset=1 read must keep its present
    output through the offset-normalisation change."""
    out = fs._format_spreadsheet(
        path=Path("book.xlsx"), sheets=[_sheet("Sheet1", 3, label="r")], offset=1, limit=10
    )
    assert "1\tr1" in out
    assert "2\tr2" in out
    assert "3\tr3" in out
    assert "Sheet1 (3 rows x 1 columns)" in out


def test_format_spreadsheet_offset_past_end_of_single_sheet_is_unchanged() -> None:
    """Currently-correct case: a single-sheet workbook with an offset past
    its own row count must keep rendering an empty section with no extra
    starvation note -- the header's own row count already explains it, and
    there is no second sheet for a shared offset to starve."""
    out = fs._format_spreadsheet(
        path=Path("book.xlsx"), sheets=[_sheet("Sheet1", 3, label="r")], offset=50, limit=10
    )
    assert "Sheet1 (3 rows x 1 columns)" in out
    assert "exceeds" not in out
    assert "r1" not in out and "r2" not in out and "r3" not in out


def test_format_spreadsheet_workbook_with_no_empty_rows_is_unchanged() -> None:
    """Currently-correct case: a fully contiguous workbook (no padded gaps)
    must keep its present output."""
    out = fs._format_spreadsheet(
        path=Path("book.xlsx"), sheets=[_sheet("Sheet1", 2, label="r")], offset=1, limit=10
    )
    assert out.splitlines()[-2:] == ["1\tr1", "2\tr2"]


@pytest.mark.parametrize("bad_offset", [0, -1, -25])
def test_format_spreadsheet_non_positive_offset_is_clamped_in_the_message(
    bad_offset: int,
) -> None:
    """A non-positive offset must not leak into the continuation message --
    the slice already floors at row 1, but the message used to echo the raw
    offset, printing e.g. "Showing rows 0-10" (#1149 / #1402)."""
    out = fs._format_spreadsheet(
        path=Path("book.xlsx"),
        sheets=[_sheet("Sheet1", 20, label="r")],
        offset=bad_offset,
        limit=10,
    )
    assert "1\tr1" in out
    assert "Showing rows 1-10 of 20" in out
    assert "Showing rows 0-" not in out
    assert "Showing rows -" not in out


def test_format_spreadsheet_multi_sheet_starvation_is_explained() -> None:
    """One offset is shared across every sheet in a multi-sheet read; a
    sheet smaller than that offset must say so explicitly instead of
    silently rendering an empty table with no explanation (#1402)."""
    out = fs._format_spreadsheet(
        path=Path("book.xlsx"),
        sheets=[
            _sheet("Big", 50, label="b"),
            _sheet("Small", 10, label="s"),
        ],
        offset=25,
        limit=10,
    )
    # The larger sheet paginates normally at the shared offset.
    assert "25\tb25" in out
    # The smaller sheet gets an explicit note, not a silent empty table.
    assert "(Offset 25 exceeds this sheet's 10 rows; no rows shown.)" in out
    assert not any(f"s{i}" in out for i in range(1, 11))


def test_format_spreadsheet_multi_sheet_empty_sheet_is_not_reported_as_starved() -> None:
    """A genuinely empty sheet (0 rows) is not a starvation symptom of a
    shared offset -- it must render like any other empty sheet, not with
    the "offset exceeds" note meant for a sheet that has rows the offset
    simply skipped past."""
    out = fs._format_spreadsheet(
        path=Path("book.xlsx"),
        sheets=[
            _sheet("Big", 50, label="b"),
            ("Empty", {}, 0),
        ],
        offset=25,
        limit=10,
    )
    assert "Empty (0 rows x 0 columns)" in out
    assert "exceeds" not in out


def test_read_xlsx_worksheet_crafted_row_beyond_ceiling() -> None:
    """A crafted or corrupt r="99999999999" row index must not cost
    anything proportional to that number -- keying by real row number
    (instead of padding a list up to it) makes this cheap regardless of
    how large the declared index is, since only rows the XML actually
    contains are ever stored."""
    xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <sheetData>
            <row r="1">
                <c r="A1" t="inlineStr"><is><t>Header</t></is></c>
            </row>
            <row r="20000000">
                <c r="A20000000" t="inlineStr"><is><t>Huge Row</t></is></c>
            </row>
            <row r="99999999999">
                <c r="A99999999999" t="inlineStr"><is><t>Gigantic Row</t></is></c>
            </row>
            <row r="0">
                <c r="A0" t="inlineStr"><is><t>Zero Row</t></is></c>
            </row>
        </sheetData>
    </worksheet>"""

    import time

    start = time.perf_counter()
    rows, total_rows = fs._read_xlsx_worksheet(xml, [])
    elapsed = time.perf_counter() - start

    # r=20000000, r=99999999999, and r=0 are all outside the format's real
    # row range (1..1,048,576) and stay fully disregarded.
    assert set(rows) == {1}
    assert rows[1] == ["Header"]
    assert total_rows == 1
    assert elapsed < 0.5


def _sparse_far_row_sheet_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Header</t></is></c></row>'
        '<row r="1048576"><c r="A1048576" t="inlineStr"><is><t>Last</t></is></c></row>'
        "</sheetData>"
        "</worksheet>"
    )


@pytest.mark.asyncio
async def test_read_spreadsheet_selecting_one_sheet_does_not_pad_to_declared_row(
    tmp_path: Path,
) -> None:
    """_read_xlsx_sheets parses every sheet in the workbook before a single
    sheet is selected, so a crafted multi-sheet workbook where every sheet
    declares a near-max row index must not cost anything proportional to
    sheet count -- a 6.6 KB, 20-sheet file previously cost ~1.6 GB / ~19s
    to read one sheet from, even though only one sheet was ever going to
    be rendered. Keying rows by their real row number instead of padding a
    list up to it means each sheet costs O(2), its actual <row> count."""
    sheets = {f"S{i}": _sparse_far_row_sheet_xml() for i in range(1, 21)}
    xlsx_bytes = _build_xlsx_bytes(sheets)
    target = tmp_path / "huge.xlsx"
    target.write_bytes(xlsx_bytes)

    import time

    with tool_context(tmp_path):
        start = time.perf_counter()
        out = await fs.read_spreadsheet(str(target), sheet="S1", offset=1, limit=10)
        elapsed = time.perf_counter() - start

    assert "1\tHeader" in out
    assert "1048576 rows" in out
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_read_spreadsheet_large_limit_renders_the_whole_sheet(tmp_path: Path) -> None:
    """A caller may legitimately ask for the whole sheet via a large
    `limit`; that must still work and return real output -- rendering
    ~1M lines is genuine requested work, not amplification, and is
    covered separately by test_read_xlsx_sheets_reading_does_not_scale_
    with_sheet_count for the (much cheaper) read side."""
    sheets = {f"S{i}": _sparse_far_row_sheet_xml() for i in range(1, 21)}
    target = tmp_path / "huge.xlsx"
    target.write_bytes(_build_xlsx_bytes(sheets))

    with tool_context(tmp_path):
        out = await fs.read_spreadsheet(str(target), sheet="S1", offset=1, limit=1_048_576)

    assert "1\tHeader" in out
    assert "1048576\tLast" in out
    assert "1048576 rows" in out


# ---------------------------------------------------------------------------
# limit=0 semantics (headers-only) vs the falsy-zero fallback.
# ---------------------------------------------------------------------------


def _write_rich_csv(tmp_path: Path, rows: int) -> Path:
    target = tmp_path / "data.csv"
    lines = ["c1,c2"] + [f"r{i},v{i}" for i in range(1, rows + 1)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


async def test_read_spreadsheet_limit_zero_returns_headers_only(tmp_path: Path) -> None:
    """limit=0 must return 0 data rows (metadata only), consistent with
    read_file's limit handling -- it used to hit the falsy-zero fallback
    and silently dump the 200-row default into the LLM context."""
    target = _write_rich_csv(tmp_path, 250)

    with tool_context(tmp_path):
        out = await fs.read_spreadsheet(str(target), limit=0)

    assert "251 rows" in out  # sheet metadata is still present
    assert "r1\t" not in out  # no data row rendered at all
    assert "(limit=0: headers only; pass a higher limit to read rows.)" in out
    assert "Showing rows 1-0" not in out


async def test_read_spreadsheet_omitted_limit_still_defaults_to_200(tmp_path: Path) -> None:
    """Anti-drift: omitting limit keeps the 200-row default."""
    target = _write_rich_csv(tmp_path, 250)

    with tool_context(tmp_path):
        out = await fs.read_spreadsheet(str(target))

    assert "\n200\tr199\tv199" in out
    assert "\n201\tr200\tv200" not in out
    assert "Showing rows 1-200 of 251" in out


async def test_read_spreadsheet_negative_limit_keeps_default_fallback(tmp_path: Path) -> None:
    """Anti-drift: a negative limit keeps the old 200-row fallback."""
    target = _write_rich_csv(tmp_path, 250)

    with tool_context(tmp_path):
        out = await fs.read_spreadsheet(str(target), limit=-5)

    assert "\n200\tr199\tv199" in out
    assert "\n201\tr200\tv200" not in out


async def test_read_spreadsheet_small_positive_limit_still_honored(tmp_path: Path) -> None:
    """Anti-drift: small positive limits behave exactly as before."""
    target = _write_rich_csv(tmp_path, 250)

    with tool_context(tmp_path):
        out = await fs.read_spreadsheet(str(target), limit=2)

    assert "\n1\tc1\tc2" in out
    assert "\n2\tr1\tv1" in out
    assert "\n3\tr2\tv2" not in out
    assert "Showing rows 1-2 of 251" in out


def test_read_xlsx_sheets_reading_does_not_scale_with_sheet_count(tmp_path: Path) -> None:
    """_read_xlsx_sheets parses every sheet before a single one is selected,
    so its own cost -- independent of whatever `limit` a caller later
    renders with -- must not depend on how many sheets a workbook has, nor
    on what row numbers they declare. Keying rows by their real row number
    (rather than padding a list up to it) means each sheet here costs O(2),
    its actual <row> count, regardless of sheet count -- isolating this
    from _format_spreadsheet's separate, legitimate large-limit rendering
    cost, which does scale with `limit` (that is the caller's own request,
    not amplification; see the large-limit case above)."""
    import time

    def _time_read(sheet_count: int) -> float:
        sheets = {f"S{i}": _sparse_far_row_sheet_xml() for i in range(1, sheet_count + 1)}
        target = tmp_path / f"wide-{sheet_count}.xlsx"
        target.write_bytes(_build_xlsx_bytes(sheets))
        start = time.perf_counter()
        parsed = fs._read_xlsx_sheets(target)
        elapsed = time.perf_counter() - start
        assert len(parsed) == sheet_count
        return elapsed

    one_sheet = _time_read(1)
    twenty_sheets = _time_read(20)

    # A generous absolute ceiling and a generous ratio: this only needs to
    # rule out the old O(sheet_count x declared_row_number) blowup (which
    # was several seconds and ~1.6 GB for 20 sheets), not pin an exact
    # ratio on a shared, noisy CI box.
    assert twenty_sheets < 1.0
    assert twenty_sheets < max(one_sheet * 5, one_sheet + 0.5)


def _single_cell_sheet_xml(text: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        f'<row r="1"><c r="A1" t="inlineStr"><is><t>{text}</t></is></c></row>'
        "</sheetData>"
        "</worksheet>"
    )


@pytest.mark.asyncio
async def test_read_spreadsheet_reaches_a_sheet_named_like_an_index(tmp_path: Path) -> None:
    """A workbook may name a sheet "1" (a year, a step, a code). Selecting it
    by that name must return that sheet, not whichever sheet happens to sit
    at 1-based position 1."""
    target = tmp_path / "numeric-names.xlsx"
    target.write_bytes(
        _build_xlsx_bytes(
            {
                "Summary": _single_cell_sheet_xml("summary-cell"),
                "1": _single_cell_sheet_xml("one-cell"),
            }
        )
    )

    with tool_context(tmp_path):
        out = await fs.read_spreadsheet(str(target), sheet="1")

    assert "one-cell" in out
    assert "summary-cell" not in out


@pytest.mark.asyncio
async def test_read_spreadsheet_selects_by_position_when_no_sheet_has_that_name(
    tmp_path: Path,
) -> None:
    """The positional reading of a numeric argument stays available -- it is
    only outranked by an exact name match, never removed."""
    target = tmp_path / "numeric-names.xlsx"
    target.write_bytes(
        _build_xlsx_bytes(
            {
                "Summary": _single_cell_sheet_xml("summary-cell"),
                "1": _single_cell_sheet_xml("one-cell"),
            }
        )
    )

    with tool_context(tmp_path):
        out = await fs.read_spreadsheet(str(target), sheet="2")

    assert "one-cell" in out
    assert "summary-cell" not in out
