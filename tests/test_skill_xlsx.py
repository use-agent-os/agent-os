"""xlsx skill — load, eligibility, and create→inspect→edit round-trip."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos.skills.eligibility import EligibilityContext, check_eligibility
from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
SCRIPTS = BUNDLED / "xlsx" / "scripts"


def _spec() -> object:
    return SkillLoader(bundled_dir=BUNDLED).get_by_name("xlsx")


def test_skill_loads() -> None:
    spec = _spec()
    assert spec is not None
    assert spec.name == "xlsx"
    assert spec.metadata is not None


def test_eligibility_with_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentos.skills.eligibility.shutil.which",
        lambda name: "/usr/bin/python3" if name in {"python", "python3"} else None,
    )
    spec = _spec()
    assert spec is not None
    assert check_eligibility(spec, EligibilityContext.auto())


@pytest.mark.parametrize(
    "merged",
    [
        pytest.param([{"range": "A1:B1"}], id="dictionary"),
        pytest.param(["A1:B1"], id="string"),
    ],
)
def test_round_trip_with_formula_and_merge(tmp_path: Path, merged: list[object]) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_xlsx  # type: ignore[import-not-found]
        import edit_xlsx  # type: ignore[import-not-found]
        import inspect_xlsx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    spec = {
        "sheets": [
            {
                "name": "Sales",
                "rows": [
                    ["Region", "Revenue"],
                    ["NA", 1_200_000],
                    ["EU", 850_000],
                    ["Total", "=SUM(B2:B3)"],
                ],
                "merged": merged,
                "freeze": "A2",
            }
        ]
    }
    src = tmp_path / "book.xlsx"
    create_xlsx.build(spec).save(str(src))
    assert src.exists()

    inspected = inspect_xlsx.inspect(src, data_only=False)
    sheet = next(s for s in inspected["sheets"] if s["name"] == "Sales")
    assert sheet["max_row"] == 4
    assert sheet["max_col"] == 2
    assert sheet["merged"] == ["A1:B1"]
    assert sheet["freeze"] == "A2"

    last_row = sheet["rows"][3]
    assert last_row[1]["type"] == "f"
    assert last_row[1]["value"] == "=SUM(B2:B3)"

    from openpyxl import load_workbook

    wb = load_workbook(str(src))
    edit_xlsx.apply_ops(
        wb,
        [
            {"op": "set_cell", "sheet": "Sales", "row": 2, "col": 1, "value": "Americas"},
            {"op": "rename_sheet", "old": "Sales", "new": "Q3"},
        ],
    )
    out = tmp_path / "out.xlsx"
    wb.save(str(out))

    re_inspected = inspect_xlsx.inspect(out, data_only=False)
    sheet_q3 = next(s for s in re_inspected["sheets"] if s["name"] == "Q3")
    assert sheet_q3["rows"][1][0]["value"] == "Americas"


@pytest.mark.parametrize(
    ("merged", "expected"),
    [
        pytest.param(["A1:B1", {"range": "A3:B3"}], ["A1:B1", "A3:B3"], id="mixed"),
        pytest.param([], [], id="empty"),
        pytest.param(None, [], id="null"),
        pytest.param([None, 42, {}, {"other": "A1:B1"}], [], id="unsupported-entries"),
    ],
)
def test_create_merge_formats(tmp_path: Path, merged: object, expected: list[str]) -> None:
    from agentos.skills.bundled.xlsx.scripts.create_xlsx import build
    from agentos.skills.bundled.xlsx.scripts.inspect_xlsx import inspect

    path = tmp_path / "merges.xlsx"
    wb = build({"sheets": [{"name": "Merges", "merged": merged}, {"name": "Plain"}]})
    wb.save(path)
    wb.close()

    sheets = inspect(path, data_only=False)["sheets"]
    assert sheets[0]["merged"] == expected
    assert sheets[1]["merged"] == []


@pytest.mark.parametrize("merged", [["not-a-range"], [{"range": "not-a-range"}]])
def test_create_rejects_invalid_merge_ranges(merged: list[object]) -> None:
    from agentos.skills.bundled.xlsx.scripts.create_xlsx import build

    with pytest.raises(ValueError, match="not a valid coordinate or range"):
        build({"sheets": [{"merged": merged}]})


def test_create_accepts_inspected_merge_ranges(tmp_path: Path) -> None:
    from openpyxl import Workbook

    from agentos.skills.bundled.xlsx.scripts.create_xlsx import build
    from agentos.skills.bundled.xlsx.scripts.inspect_xlsx import inspect

    original = Workbook()
    ws = original.active
    assert ws is not None
    ws.merge_cells("A1:C1")
    ws.merge_cells("B3:B5")
    source = tmp_path / "source.xlsx"
    original.save(source)
    original.close()
    merges = inspect(source, data_only=False)["sheets"][0]["merged"]
    assert merges == ["A1:C1", "B3:B5"]

    # Reuse only merge metadata: inspector rows have a different schema.
    rebuilt = build({"sheets": [{"merged": merges}]})
    target = tmp_path / "rebuilt.xlsx"
    rebuilt.save(target)
    rebuilt.close()
    assert inspect(target, data_only=False)["sheets"][0]["merged"] == merges


def test_text_escapes_formula(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_xlsx  # type: ignore[import-not-found]
        import edit_xlsx  # type: ignore[import-not-found]
        import inspect_xlsx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["a"]]}]}).save(str(src))

    from openpyxl import load_workbook

    wb = load_workbook(str(src))
    edit_xlsx.apply_ops(
        wb,
        [
            {
                "op": "set_cell",
                "sheet": "S",
                "row": 2,
                "col": 1,
                "value": "=hello",
                "as_text": True,
            },
        ],
    )
    out = tmp_path / "out.xlsx"
    wb.save(str(out))

    inspected = inspect_xlsx.inspect(out, data_only=False)
    sheet = inspected["sheets"][0]
    cell_value = sheet["rows"][1][0]["value"]
    assert isinstance(cell_value, str)
    assert cell_value.lstrip("'") == "=hello"


def test_inspect_xlsx_creates_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_xlsx  # type: ignore[import-not-found]
        import inspect_xlsx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["a"]]}]}).save(str(src))

    out = tmp_path / "nested" / "dir" / "out.json"
    monkeypatch.setattr(sys, "argv", ["inspect_xlsx.py", str(src), "--out", str(out)])
    assert inspect_xlsx.main() == 0
    assert out.is_file()


def _edit_and_reload(tmp_path: Path, ops: list[dict[str, object]]) -> object:
    """Run ``ops`` against a one-cell workbook and reload the saved result."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_xlsx  # type: ignore[import-not-found]
        import edit_xlsx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    from openpyxl import load_workbook

    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["a"]]}]}).save(str(src))
    wb = load_workbook(str(src))
    edit_xlsx.apply_ops(wb, ops)
    out = tmp_path / "out.xlsx"
    wb.save(str(out))
    return load_workbook(str(out))["S"]


def _set_cell(row: int, value: object, **extra: object) -> dict[str, object]:
    return {"op": "set_cell", "sheet": "S", "row": row, "col": 1, "value": value, **extra}


def test_as_text_keeps_the_formula_string_out_of_the_cell_value(tmp_path: Path) -> None:
    """``as_text: true`` stores ``=hello``, not Excel's input-mode apostrophe.

    The apostrophe is an escape typed into Excel's formula bar, not cell
    content. Prepending it left the cell holding ``'=hello`` — seven characters
    where the caller passed six — so a round-trip comparison against the
    original string failed and both Excel and LibreOffice rendered a stray
    apostrophe.
    """
    sheet = _edit_and_reload(tmp_path, [_set_cell(2, "=hello", as_text=True)])
    cell = sheet.cell(row=2, column=1)

    assert cell.value == "=hello"
    assert cell.data_type == "s"
    assert cell.quotePrefix is True


def test_inspect_reports_the_escaped_formula_without_an_apostrophe(
    tmp_path: Path,
) -> None:
    """The skill's own inspector is where a caller reads the value back.

    ``inspect_xlsx`` reported ``'=hello``, so the corruption was visible
    through the documented read path and not only through openpyxl directly.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_xlsx  # type: ignore[import-not-found]
        import edit_xlsx  # type: ignore[import-not-found]
        import inspect_xlsx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    from openpyxl import load_workbook

    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["a"]]}]}).save(str(src))
    wb = load_workbook(str(src))
    edit_xlsx.apply_ops(wb, [_set_cell(2, "=hello", as_text=True)])
    out = tmp_path / "out.xlsx"
    wb.save(str(out))

    reported = inspect_xlsx.inspect(out, data_only=False)["sheets"][0]["rows"][1][0]

    assert reported["value"] == "=hello"
    assert reported["type"] == "s"


def test_without_as_text_a_formula_string_stays_a_formula(tmp_path: Path) -> None:
    """The default path is unchanged: openpyxl still records a formula cell."""
    sheet = _edit_and_reload(tmp_path, [_set_cell(2, "=SUM(A1:A1)")])
    cell = sheet.cell(row=2, column=1)

    assert cell.value == "=SUM(A1:A1)"
    assert cell.data_type == "f"
    assert cell.quotePrefix is False


def test_as_text_suppresses_the_iso_datetime_coercion(tmp_path: Path) -> None:
    """An explicit ``as_text`` request must win over the ISO-8601 heuristic.

    ``_coerce`` only consulted the flag on the formula branch, so there was no
    way to store a timestamp as text: every 19-character string with ``T`` at
    index 10 became a ``datetime`` cell regardless.
    """
    sheet = _edit_and_reload(tmp_path, [_set_cell(2, "2026-05-06T09:00:00", as_text=True)])
    cell = sheet.cell(row=2, column=1)

    assert cell.value == "2026-05-06T09:00:00"
    assert isinstance(cell.value, str)
    assert cell.data_type == "s"


def test_without_as_text_an_iso_string_still_becomes_a_datetime(tmp_path: Path) -> None:
    """The documented datetime convenience keeps working untouched."""
    from datetime import datetime

    sheet = _edit_and_reload(tmp_path, [_set_cell(2, "2026-05-06T09:00:00")])
    cell = sheet.cell(row=2, column=1)

    assert cell.value == datetime(2026, 5, 6, 9, 0)
    assert cell.data_type == "d"


def test_as_text_does_not_flag_an_ordinary_string(tmp_path: Path) -> None:
    """``quotePrefix`` only stands in for the escape a formula string needs.

    A plain string must not pick up the flag, or every text cell written with
    ``as_text`` would carry a style Excel attributes to manual escaping.
    """
    sheet = _edit_and_reload(tmp_path, [_set_cell(2, "plain", as_text=True)])
    cell = sheet.cell(row=2, column=1)

    assert cell.value == "plain"
    assert cell.data_type == "s"
    assert cell.quotePrefix is False


def test_as_text_leaves_non_string_values_alone(tmp_path: Path) -> None:
    """Numbers and booleans keep their own cell types; the flag is about text."""
    sheet = _edit_and_reload(
        tmp_path, [_set_cell(2, 42, as_text=True), _set_cell(3, True, as_text=True)]
    )

    assert sheet.cell(row=2, column=1).value == 42
    assert sheet.cell(row=2, column=1).data_type == "n"
    assert sheet.cell(row=3, column=1).value is True
    assert sheet.cell(row=3, column=1).data_type == "b"


def test_the_apostrophe_escape_and_as_text_produce_the_same_cell(tmp_path: Path) -> None:
    """``SKILL.md`` offers two spellings of one request; they must agree.

    Excel's leading apostrophe is the input escape for a formula-looking
    value, so ``{"value": "'=hello", "as_text": true}`` asks for exactly what
    ``{"value": "=hello", "as_text": true}`` asks for. Storing the apostrophe
    as data left the two spellings on different cells -- one holding ``=hello``
    with ``quotePrefix`` set, the other holding a literal ``'=hello``.
    """
    sheet = _edit_and_reload(
        tmp_path,
        [_set_cell(2, "=hello", as_text=True), _set_cell(3, "'=hello", as_text=True)],
    )
    plain = sheet.cell(row=2, column=1)
    escaped = sheet.cell(row=3, column=1)

    assert (escaped.value, escaped.data_type, escaped.quotePrefix) == (
        plain.value,
        plain.data_type,
        plain.quotePrefix,
    )
    assert escaped.value == "=hello"
    assert escaped.quotePrefix is True


def test_a_value_that_genuinely_starts_with_an_apostrophe_keeps_it(tmp_path: Path) -> None:
    """The escape is only consumed when it escapes a formula.

    Stripping every leading apostrophe would turn ``'tis`` into ``tis`` --
    trading the reported bug for a quieter one.
    """
    sheet = _edit_and_reload(tmp_path, [_set_cell(2, "'tis", as_text=True)])
    cell = sheet.cell(row=2, column=1)

    assert cell.value == "'tis"
    assert cell.data_type == "s"
    assert cell.quotePrefix is False


def test_the_apostrophe_escape_is_not_consumed_without_as_text(tmp_path: Path) -> None:
    """Without the flag nothing is interpreted; the value is stored verbatim."""
    sheet = _edit_and_reload(tmp_path, [_set_cell(2, "'=hello")])

    assert sheet.cell(row=2, column=1).value == "'=hello"



def _import_scripts() -> tuple[Any, Any, Any]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_xlsx  # type: ignore[import-not-found]
        import edit_xlsx  # type: ignore[import-not-found]
        import inspect_xlsx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return create_xlsx, edit_xlsx, inspect_xlsx


def _run_cli(
    edit_xlsx: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    src: Path,
    out: Path,
    ops: list[dict[str, Any]],
    tmp_path: Path,
) -> dict[str, Any]:
    """Drive the script the way a caller does: ops file in, workbook out.

    In-process rather than a subprocess, matching how the other bundled-skill
    tests are run on Windows. Going through ``main`` is what matters here: the
    reported ``applied`` count is part of the contract being fixed, and reading
    the saved file back is the only way to see whether a write really happened
    -- the in-memory workbook would look right either way.
    """
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(json.dumps(ops), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["edit_xlsx.py", str(src), str(ops_path), "--out", str(out)])
    assert edit_xlsx.main() == 0
    return dict(json.loads(capsys.readouterr().out.strip()))


def _cell(path: Path, row: int, col: int, sheet: str = "S") -> Any:
    """Read one cell from the saved file.

    Addressed directly rather than through ``inspect``: clearing the only
    populated cell leaves the sheet with no used range at all, so a row-indexed
    read would raise IndexError instead of reporting the value as empty.
    """
    from openpyxl import load_workbook

    return load_workbook(str(path))[sheet].cell(row=row, column=col).value


def test_set_cell_explicit_null_clears_the_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    create_xlsx, edit_xlsx, inspect_xlsx = _import_scripts()
    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["keep me"]]}]}).save(str(src))

    out = tmp_path / "out.xlsx"
    report = _run_cli(
        edit_xlsx,
        monkeypatch,
        capsys,
        src,
        out,
        [{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": None}],
        tmp_path,
    )

    assert report == {"applied": 1}
    assert _cell(out, 1, 1) is None


def test_set_cell_without_a_value_key_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A malformed op must not read as "clear this cell", and must not be
    # counted -- otherwise a typo silently wipes data and reports success.
    create_xlsx, edit_xlsx, inspect_xlsx = _import_scripts()
    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["keep me"]]}]}).save(str(src))

    out = tmp_path / "out.xlsx"
    report = _run_cli(
        edit_xlsx,
        monkeypatch,
        capsys,
        src,
        out,
        [{"op": "set_cell", "sheet": "S", "row": 1, "col": 1}],
        tmp_path,
    )

    assert report == {"applied": 0}
    assert _cell(out, 1, 1) == "keep me"


@pytest.mark.parametrize(("value", "expected"), [(0, 0), (False, False)])
def test_set_cell_writes_falsy_values(
    value: Any,
    expected: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Guards the fix: these are values, not absence. The type assertion is the
    # point -- False must stay a boolean rather than collapse to 0.
    create_xlsx, edit_xlsx, _ = _import_scripts()
    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["old"]]}]}).save(str(src))

    out = tmp_path / "out.xlsx"
    report = _run_cli(
        edit_xlsx,
        monkeypatch,
        capsys,
        src,
        out,
        [{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": value}],
        tmp_path,
    )

    assert report == {"applied": 1}
    written = _cell(out, 1, 1)
    assert written == expected
    assert isinstance(written, type(expected))


def test_set_cell_empty_string_counts_as_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Excel has no empty-string cell, so openpyxl round-trips "" as None. That
    # is unchanged by this fix and is why "" is not a substitute for an
    # explicit null: the op still counts as applied either way, but only the
    # null path is documented as clearing.
    create_xlsx, edit_xlsx, _ = _import_scripts()
    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["old"]]}]}).save(str(src))

    out = tmp_path / "out.xlsx"
    report = _run_cli(
        edit_xlsx,
        monkeypatch,
        capsys,
        src,
        out,
        [{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": ""}],
        tmp_path,
    )

    assert report == {"applied": 1}
    assert _cell(out, 1, 1) is None


def test_clearing_a_cell_keeps_its_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from openpyxl import load_workbook

    create_xlsx, edit_xlsx, _ = _import_scripts()
    src = tmp_path / "book.xlsx"
    wb = create_xlsx.build({"sheets": [{"name": "S", "rows": [["styled"]]}]})
    wb["S"].cell(row=1, column=1).number_format = "0.00%"
    wb.save(str(src))

    out = tmp_path / "out.xlsx"
    _run_cli(
        edit_xlsx,
        monkeypatch,
        capsys,
        src,
        out,
        [{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": None}],
        tmp_path,
    )

    cell = load_workbook(str(out))["S"].cell(row=1, column=1)
    assert cell.value is None
    assert cell.number_format == "0.00%"


def _write_zip(path: Path, entries: dict[str, str]) -> Path:
    """Write a real ZIP whose contents are not a workbook."""
    import zipfile

    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def _corrupt_shapes(tmp_path: Path) -> dict[str, Path]:
    """Every way a caller can hand these scripts something unopenable.

    openpyxl answers with four unrelated exception classes across these six
    files, which is what made the failure impossible to guard with one
    ``except``: BadZipFile, KeyError, an XML parse error and
    InvalidFileException.
    """
    from openpyxl import Workbook

    good = tmp_path / "good_for_bytes.xlsx"
    Workbook().save(str(good))
    real = good.read_bytes()

    raw_shapes = {
        "plain_text": (tmp_path / "text.xlsx", b"this is not a real xlsx file"),
        "empty_file": (tmp_path / "empty.xlsx", b""),
        "truncated_workbook": (tmp_path / "cut.xlsx", real[: len(real) // 2]),
        "html_saved_as_xlsx": (tmp_path / "page.xlsx", b"<html><body><table></table></body>"),
        "old_xls_format": (tmp_path / "legacy.xls", b"\xd0\xcf\x11\xe0anything"),
    }
    out: dict[str, Path] = {}
    for name, (path, data) in raw_shapes.items():
        path.write_bytes(data)
        out[name] = path
    out["zip_without_ooxml_part"] = _write_zip(tmp_path / "bare.xlsx", {"hello.txt": "hi"})
    out["zip_with_malformed_xml"] = _write_zip(
        tmp_path / "broken.xlsx",
        {"[Content_Types].xml": "<not xml", "xl/workbook.xml": "<<<"},
    )
    return out


_CORRUPT_SHAPE_IDS = [
    "plain_text",
    "empty_file",
    "truncated_workbook",
    "html_saved_as_xlsx",
    "old_xls_format",
    "zip_without_ooxml_part",
    "zip_with_malformed_xml",
]


@pytest.mark.parametrize("shape", _CORRUPT_SHAPE_IDS)
def test_inspect_reports_an_unreadable_workbook(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file openpyxl cannot open is a caller error, not a skill crash."""
    _, _, inspect_xlsx = _import_scripts()
    path = _corrupt_shapes(tmp_path)[shape]

    monkeypatch.setattr(sys, "argv", ["inspect_xlsx.py", str(path)])
    assert inspect_xlsx.main() == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert str(path) in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("shape", _CORRUPT_SHAPE_IDS)
def test_edit_reports_an_unreadable_workbook(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, edit_xlsx, _ = _import_scripts()
    path = _corrupt_shapes(tmp_path)[shape]
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": 1}]))
    out = tmp_path / "written.xlsx"

    monkeypatch.setattr(sys, "argv", ["edit_xlsx.py", str(path), str(ops), "--out", str(out)])
    assert edit_xlsx.main() == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
    # An edit that could not read its input must not leave a half-written file.
    assert not out.exists()


def test_an_unreadable_workbook_exits_like_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both are bad caller input, so both are 2 -- not 1, which means failure."""
    _, _, inspect_xlsx = _import_scripts()
    corrupt = _corrupt_shapes(tmp_path)["plain_text"]

    monkeypatch.setattr(sys, "argv", ["inspect_xlsx.py", str(tmp_path / "absent.xlsx")])
    missing_code = inspect_xlsx.main()
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["inspect_xlsx.py", str(corrupt)])
    corrupt_code = inspect_xlsx.main()
    capsys.readouterr()

    assert (missing_code, corrupt_code) == (2, 2)


def test_inspect_still_reads_a_good_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guard: passes either way, and proves the guard catches nothing valid."""
    create_xlsx, _, inspect_xlsx = _import_scripts()
    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["a"]]}]}).save(str(src))

    monkeypatch.setattr(sys, "argv", ["inspect_xlsx.py", str(src)])
    assert inspect_xlsx.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert [sheet["name"] for sheet in payload["sheets"]] == ["S"]


def test_a_failure_after_the_load_is_not_disguised_as_unreadable_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard covers opening the file, not everything the script then does.

    ``KeyError`` is in the tuple because openpyxl raises it for a ZIP missing
    an OOXML part, and it is the one class a bug of our own could also raise.
    Scoping the ``try`` to ``load_workbook`` keeps that bug loud.
    """
    create_xlsx, edit_xlsx, _ = _import_scripts()
    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["a"]]}]}).save(str(src))
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": 1}]))

    def _boom(*_args: Any, **_kwargs: Any) -> int:
        raise KeyError("a bug in apply_ops, not a corrupt file")

    monkeypatch.setattr(edit_xlsx, "apply_ops", _boom)
    monkeypatch.setattr(
        sys, "argv", ["edit_xlsx.py", str(src), str(ops), "--out", str(tmp_path / "o.xlsx")]
    )
    with pytest.raises(KeyError):
        edit_xlsx.main()
    capsys.readouterr()


def test_both_scripts_agree_on_what_unreadable_means() -> None:
    """The tuple is duplicated because bundled scripts stand alone; pin it."""
    _, edit_xlsx, inspect_xlsx = _import_scripts()
    assert edit_xlsx._UNREADABLE_WORKBOOK == inspect_xlsx._UNREADABLE_WORKBOOK
    # SyntaxError, not lxml's XMLSyntaxError: it is the base both XML backends
    # share, so the guard holds whether or not openpyxl found lxml.
    assert SyntaxError in inspect_xlsx._UNREADABLE_WORKBOOK


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("malformed", b"{not json"),
        ("truncated", b'{"sheets": ['),
        ("empty", b""),
        ("utf16_from_powershell", '{"sheets": []}'.encode("utf-16")),
    ],
)
def test_create_reports_a_spec_that_is_not_json(
    label: str,
    payload: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The issue's second request: the spec file is caller input too."""
    create_xlsx, _, _ = _import_scripts()
    spec = tmp_path / "spec.json"
    spec.write_bytes(payload)
    out = tmp_path / "made.xlsx"

    monkeypatch.setattr(sys, "argv", ["create_xlsx.py", str(spec), "--out", str(out)])
    assert create_xlsx.main() == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "is not valid JSON" in captured.err
    assert "Traceback" not in captured.err
    assert not out.exists()


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("malformed", b"{not json"),
        ("truncated", b"[{"),
        ("empty", b""),
        ("utf16_from_powershell", "[]".encode("utf-16")),
    ],
)
def test_edit_reports_ops_that_are_not_json(
    label: str,
    payload: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same class as the spec file, one script over, and unreported."""
    create_xlsx, edit_xlsx, _ = _import_scripts()
    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["a"]]}]}).save(str(src))
    ops = tmp_path / "ops.json"
    ops.write_bytes(payload)
    out = tmp_path / "out.xlsx"

    monkeypatch.setattr(sys, "argv", ["edit_xlsx.py", str(src), str(ops), "--out", str(out)])
    assert edit_xlsx.main() == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "is not valid JSON" in captured.err
    assert not out.exists()


def test_a_valid_json_spec_still_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guard: the JSON gate only rejects what json.loads itself rejects."""
    create_xlsx, _, _ = _import_scripts()
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"sheets": [{"name": "S", "rows": [["a"]]}]}), encoding="utf-8")
    out = tmp_path / "made.xlsx"

    monkeypatch.setattr(sys, "argv", ["create_xlsx.py", str(spec), "--out", str(out)])
    assert create_xlsx.main() == 0
    capsys.readouterr()
    assert out.is_file()
