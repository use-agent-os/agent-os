"""Issue #2303: the xlsx scripts accepted input files they could not use.

``edit_xlsx.py`` loaded its ops file as ``raw if isinstance(raw, list) else []``.
A caller who wrote a single operation object -- the routine slip -- got the
whole batch coerced to ``[]``: the workbook was loaded, zero operations were
applied, the output was saved anyway, and the tool printed ``{"applied": 0}``
and exited 0. Nothing distinguished that from a successful edit. Malformed
JSON, or a UTF-16 file from PowerShell's ``Out-File``, instead raised a raw
``JSONDecodeError`` / ``UnicodeDecodeError`` traceback and exited 1.

This is the sibling of #1903, fixed for ``pdf-toolkit/form_fill.py`` in #2034,
and the contract here is deliberately the same: exit 2, a message on stderr,
no output file.

Two things are covered here beyond the ops file itself.

*``create_xlsx.py`` has the same defect in a worse form.* Its spec file is
loaded with a bare ``json.loads`` and handed to ``build``, which calls
``spec.get`` -- so ``null`` or ``42`` died with an ``AttributeError``
traceback. Fixing one script in a two-script skill leaves the other hole open.

*A well-formed list can still apply nothing.* Six op shapes pass the list check
and are then silently skipped, landing on exactly the outcome the issue
describes: ``applied: 0``, exit 0, a workbook written. Those skips are now
named on stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "xlsx" / "scripts"


def _import_scripts() -> tuple[Any, Any]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_xlsx  # type: ignore[import-not-found]
        import edit_xlsx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return create_xlsx, edit_xlsx


@pytest.fixture
def scripts() -> tuple[Any, Any]:
    return _import_scripts()


@pytest.fixture
def book(scripts: tuple[Any, Any], tmp_path: Path) -> Path:
    create_xlsx, _ = scripts
    src = tmp_path / "book.xlsx"
    create_xlsx.build({"sheets": [{"name": "S", "rows": [["initial"]]}]}).save(str(src))
    return src


def _run_edit(
    edit_xlsx: Any,
    monkeypatch: pytest.MonkeyPatch,
    src: Path,
    ops_path: Path,
    out: Path,
) -> int:
    monkeypatch.setattr(sys, "argv", ["edit_xlsx.py", str(src), str(ops_path), "--out", str(out)])
    return int(edit_xlsx.main())


def _run_create(
    create_xlsx: Any, monkeypatch: pytest.MonkeyPatch, spec_path: Path, out: Path
) -> int:
    monkeypatch.setattr(sys, "argv", ["create_xlsx.py", str(spec_path), "--out", str(out)])
    return int(create_xlsx.main())


# --------------------------------------------------------------------------
# edit_xlsx: the ops file is not a list.
# --------------------------------------------------------------------------

NON_LIST_OPS = {
    "a single op object (the issue's own case)": (
        '{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": "test"}',
        "dict",
    ),
    "an object wrapping the list": ('{"ops": []}', "dict"),
    "null": ("null", "NoneType"),
    "a bare string": ('"set_cell"', "str"),
    "a number": ("42", "int"),
    "a float": ("4.5", "float"),
    "true": ("true", "bool"),
    "false": ("false", "bool"),
}


@pytest.mark.parametrize(("ops_text", "type_name"), NON_LIST_OPS.values(), ids=list(NON_LIST_OPS))
def test_a_non_list_ops_file_is_refused(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ops_text: str,
    type_name: str,
) -> None:
    _, edit_xlsx = scripts
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(ops_text, encoding="utf-8")
    out = tmp_path / "out.xlsx"

    code = _run_edit(edit_xlsx, monkeypatch, book, ops_path, out)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == "", "a refused run must print no success JSON"
    assert f"error: ops {ops_path} must be a JSON list of operations" in captured.err
    assert f"got {type_name}" in captured.err
    assert not out.exists(), "a refused ops file must not write an output workbook"


def test_the_input_workbook_is_left_alone_when_ops_are_refused(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = book.read_bytes()
    ops_path = tmp_path / "ops.json"
    ops_path.write_text('{"op": "set_cell"}', encoding="utf-8")

    _run_edit(scripts[1], monkeypatch, book, ops_path, tmp_path / "out.xlsx")

    assert book.read_bytes() == before


def test_a_refused_ops_file_does_not_create_the_output_directory(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main`` mkdirs the parent before saving; refusing must happen first."""
    ops_path = tmp_path / "ops.json"
    ops_path.write_text("null", encoding="utf-8")
    out = tmp_path / "nested" / "deep" / "out.xlsx"

    assert _run_edit(scripts[1], monkeypatch, book, ops_path, out) == 2
    assert not out.parent.exists()


# --------------------------------------------------------------------------
# edit_xlsx: the ops file is not JSON at all.
# --------------------------------------------------------------------------

INVALID_JSON_OPS = {
    "truncated": b'[{"op": "set_cell",',
    "empty file": b"",
    "whitespace only": b"   \n  ",
    "trailing comma": b'[{"op": "set_cell"},]',
    "single quotes": b"[{'op': 'set_cell'}]",
    "bare word": b"ops",
    # UTF-16LE with BOM -- what PowerShell 5.1's Out-File writes by default.
    "UTF-16 from PowerShell Out-File": '[{"op": "set_cell"}]'.encode("utf-16"),
    "UTF-16BE": '[{"op": "set_cell"}]'.encode("utf-16-be"),
    "invalid UTF-8 bytes": b"\xff\xfe\x00[",
}


@pytest.mark.parametrize("ops_bytes", INVALID_JSON_OPS.values(), ids=list(INVALID_JSON_OPS))
def test_an_unparseable_ops_file_is_refused_without_a_traceback(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ops_bytes: bytes,
) -> None:
    _, edit_xlsx = scripts
    ops_path = tmp_path / "ops.json"
    ops_path.write_bytes(ops_bytes)
    out = tmp_path / "out.xlsx"

    code = _run_edit(edit_xlsx, monkeypatch, book, ops_path, out)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert f"error: ops {ops_path} is not valid JSON" in captured.err
    assert "Traceback" not in captured.err
    assert not out.exists()


def test_a_missing_ops_file_still_reports_not_found(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The pre-existing check must not be shadowed by the new ones."""
    ops_path = tmp_path / "absent.json"

    code = _run_edit(scripts[1], monkeypatch, book, ops_path, tmp_path / "out.xlsx")

    assert code == 2
    assert f"error: ops {ops_path} not found" in capsys.readouterr().err


def test_a_missing_input_workbook_still_reports_not_found(
    scripts: tuple[Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ops_path = tmp_path / "ops.json"
    ops_path.write_text("[]", encoding="utf-8")

    code = _run_edit(
        scripts[1], monkeypatch, tmp_path / "absent.xlsx", ops_path, tmp_path / "o.xlsx"
    )

    assert code == 2
    assert "not found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# edit_xlsx: a valid list still succeeds, and the stdout contract holds.
# --------------------------------------------------------------------------


def test_a_valid_ops_list_still_applies(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, edit_xlsx = scripts
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(
        json.dumps([{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": "x"}]),
        encoding="utf-8",
    )
    out = tmp_path / "out.xlsx"

    code = _run_edit(edit_xlsx, monkeypatch, book, ops_path, out)

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {"applied": 1}
    assert out.exists()


def test_the_stdout_object_is_unchanged(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """tests/test_skill_xlsx.py pins this dict exactly, so the new reporting
    goes to stderr rather than adding a key here."""
    _, edit_xlsx = scripts
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(json.dumps([{"op": "nope"}]), encoding="utf-8")

    _run_edit(edit_xlsx, monkeypatch, book, ops_path, tmp_path / "out.xlsx")

    assert set(json.loads(capsys.readouterr().out)) == {"applied"}


def test_an_empty_ops_list_is_accepted(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty list is a well-formed request for no edits, unlike a non-list."""
    _, edit_xlsx = scripts
    ops_path = tmp_path / "ops.json"
    ops_path.write_text("[]", encoding="utf-8")
    out = tmp_path / "out.xlsx"

    code = _run_edit(edit_xlsx, monkeypatch, book, ops_path, out)

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {"applied": 0}
    assert out.exists()
    assert "none of the" not in captured.err


# --------------------------------------------------------------------------
# A well-formed list whose operations are all skipped.
# --------------------------------------------------------------------------

SKIPPED_OPS = {
    "unknown op name": (
        [{"op": "set_sell", "sheet": "S", "row": 1, "col": 1, "value": "x"}],
        "unknown op",
    ),
    "no op key": ([{"sheet": "S", "row": 1, "col": 1, "value": "x"}], "no 'op' key"),
    "sheet not in workbook": (
        [{"op": "set_cell", "sheet": "Nope", "row": 1, "col": 1, "value": 1}],
        "not in the workbook",
    ),
    "missing row": (
        [{"op": "set_cell", "sheet": "S", "col": 1, "value": "x"}],
        "missing row or col",
    ),
    "missing value key": ([{"op": "set_cell", "sheet": "S", "row": 1, "col": 1}], "no value key"),
    "not an object": (["oops"], "is not an object, got str"),
    "rename of an absent sheet": (
        [{"op": "rename_sheet", "old": "Nope", "new": "X"}],
        "not in the workbook",
    ),
    "rename with a non-string name": (
        [{"op": "rename_sheet", "old": "S", "new": 5}],
        "string 'new' name",
    ),
    "merge of an absent sheet": (
        [{"op": "merge_cells", "sheet": "Nope", "range": "A1:B1"}],
        "not in the workbook",
    ),
    "merge with a non-string range": (
        [{"op": "merge_cells", "sheet": "S", "range": 5}],
        "string 'range'",
    ),
}


@pytest.mark.parametrize(("ops", "fragment"), SKIPPED_OPS.values(), ids=list(SKIPPED_OPS))
def test_a_skipped_operation_is_named_on_stderr(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ops: list[Any],
    fragment: str,
) -> None:
    _, edit_xlsx = scripts
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(json.dumps(ops), encoding="utf-8")

    code = _run_edit(edit_xlsx, monkeypatch, book, ops_path, tmp_path / "out.xlsx")

    captured = capsys.readouterr()
    assert code == 0, "a skipped op is not fatal; one bad entry must not cost the batch"
    assert json.loads(captured.out) == {"applied": 0}
    assert "warning: op 0:" in captured.err
    assert fragment in captured.err


def test_applying_nothing_at_all_says_so(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``applied: 0`` on its own is what the issue calls falsely reporting
    success. The run now also says the output is just a copy."""
    _, edit_xlsx = scripts
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(json.dumps([{"op": "a"}, {"op": "b"}]), encoding="utf-8")
    out = tmp_path / "out.xlsx"

    _run_edit(edit_xlsx, monkeypatch, book, ops_path, out)

    err = capsys.readouterr().err
    assert "none of the 2 operations applied" in err
    assert str(out) in err


def test_a_partly_applied_batch_reports_only_the_bad_entry(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, edit_xlsx = scripts
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(
        json.dumps(
            [
                {"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": "x"},
                {"op": "typo"},
            ]
        ),
        encoding="utf-8",
    )

    code = _run_edit(edit_xlsx, monkeypatch, book, ops_path, tmp_path / "out.xlsx")

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {"applied": 1}
    assert "warning: op 1:" in captured.err
    assert "warning: op 0:" not in captured.err
    assert "none of the" not in captured.err


def test_apply_ops_keeps_its_two_argument_form(scripts: tuple[Any, Any]) -> None:
    """The reporting list is optional so existing callers are untouched."""
    create_xlsx, edit_xlsx = scripts
    wb = create_xlsx.build({"sheets": [{"name": "S", "rows": [["initial"]]}]})

    applied = edit_xlsx.apply_ops(
        wb, [{"op": "set_cell", "sheet": "S", "row": 1, "col": 1, "value": "x"}]
    )

    assert applied == 1


def test_apply_ops_reports_one_line_per_skipped_op(scripts: tuple[Any, Any]) -> None:
    create_xlsx, edit_xlsx = scripts
    wb = create_xlsx.build({"sheets": [{"name": "S", "rows": [["initial"]]}]})
    skipped: list[str] = []

    applied = edit_xlsx.apply_ops(wb, [{"op": "a"}, "b", {"op": "c"}], skipped)

    assert applied == 0
    assert len(skipped) == 3
    assert skipped[1].startswith("op 1:")


# --------------------------------------------------------------------------
# create_xlsx: the same defect, in the same skill.
# --------------------------------------------------------------------------

NON_OBJECT_SPECS = {
    "a list of sheets": ('[{"name": "S"}]', "list"),
    "null": ("null", "NoneType"),
    "a number": ("42", "int"),
    "a bare string": ('"sheets"', "str"),
    "true": ("true", "bool"),
}


@pytest.mark.parametrize(
    ("spec_text", "type_name"), NON_OBJECT_SPECS.values(), ids=list(NON_OBJECT_SPECS)
)
def test_a_non_object_spec_is_refused(
    scripts: tuple[Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spec_text: str,
    type_name: str,
) -> None:
    """On main these reached ``build``, which calls ``spec.get`` and died with
    an AttributeError traceback."""
    create_xlsx, _ = scripts
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec_text, encoding="utf-8")
    out = tmp_path / "out.xlsx"

    code = _run_create(create_xlsx, monkeypatch, spec_path, out)

    captured = capsys.readouterr()
    assert code == 2
    assert f"error: spec {spec_path} must be a JSON object" in captured.err
    assert f"got {type_name}" in captured.err
    assert "Traceback" not in captured.err
    assert not out.exists()


@pytest.mark.parametrize(
    "spec_bytes",
    [b'{"sheets": ', b"", b"   ", '{"sheets": []}'.encode("utf-16"), b"\xff\xfe\x00{"],
    ids=["truncated", "empty", "whitespace", "UTF-16", "invalid UTF-8"],
)
def test_an_unparseable_spec_is_refused_without_a_traceback(
    scripts: tuple[Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spec_bytes: bytes,
) -> None:
    create_xlsx, _ = scripts
    spec_path = tmp_path / "spec.json"
    spec_path.write_bytes(spec_bytes)
    out = tmp_path / "out.xlsx"

    code = _run_create(create_xlsx, monkeypatch, spec_path, out)

    captured = capsys.readouterr()
    assert code == 2
    assert f"error: spec {spec_path} is not valid JSON" in captured.err
    assert "Traceback" not in captured.err
    assert not out.exists()


def test_a_valid_spec_still_creates_a_workbook(
    scripts: tuple[Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_xlsx, _ = scripts
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps({"sheets": [{"name": "S", "rows": [["a", "b"]]}]}), encoding="utf-8"
    )
    out = tmp_path / "out.xlsx"

    assert _run_create(create_xlsx, monkeypatch, spec_path, out) == 0
    assert out.exists()


def test_an_empty_object_spec_is_still_accepted(
    scripts: tuple[Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build`` already handles a spec with no sheets; only the *type* check
    is new, so this must not start failing."""
    create_xlsx, _ = scripts
    spec_path = tmp_path / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.xlsx"

    assert _run_create(create_xlsx, monkeypatch, spec_path, out) == 0
    assert out.exists()


def test_a_missing_spec_file_still_reports_not_found(
    scripts: tuple[Any, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_xlsx, _ = scripts
    spec_path = tmp_path / "absent.json"

    code = _run_create(create_xlsx, monkeypatch, spec_path, tmp_path / "out.xlsx")

    assert code == 2
    assert f"error: spec {spec_path} not found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Both scripts answer the same way, and the way #2034 established.
# --------------------------------------------------------------------------


def test_both_scripts_use_exit_2_for_a_bad_input_file(
    scripts: tuple[Any, Any],
    book: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One skill should not answer two ways for the same class of mistake."""
    create_xlsx, edit_xlsx = scripts
    bad = tmp_path / "bad.json"
    bad.write_text("null", encoding="utf-8")

    assert _run_edit(edit_xlsx, monkeypatch, book, bad, tmp_path / "a.xlsx") == 2
    assert _run_create(create_xlsx, monkeypatch, bad, tmp_path / "b.xlsx") == 2


def test_the_skill_doc_states_the_contract() -> None:
    """SKILL.md is what the model reads before calling either script."""
    text = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")

    assert "must be a JSON **list** of operations" in text
    assert "must be a JSON **object** with a `sheets` list" in text
