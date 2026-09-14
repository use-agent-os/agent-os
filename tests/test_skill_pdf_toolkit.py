"""pdf-toolkit skill — load, eligibility, and merge→split→extract round-trip."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentos.skills.eligibility import EligibilityContext, check_eligibility
from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
SCRIPTS = BUNDLED / "pdf-toolkit" / "scripts"


def _spec() -> object:
    return SkillLoader(bundled_dir=BUNDLED).get_by_name("pdf-toolkit")


def test_skill_loads() -> None:
    spec = _spec()
    assert spec is not None
    assert spec.name == "pdf-toolkit"
    description = spec.description.lower()
    assert "nano-pdf" in description, (
        "description must explicitly distinguish from sibling nano-pdf skill"
    )


def test_eligibility_with_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentos.skills.eligibility.shutil.which",
        lambda name: "/usr/bin/python3" if name in {"python", "python3"} else None,
    )
    spec = _spec()
    assert spec is not None
    assert check_eligibility(spec, EligibilityContext.auto())


def _make_one_page_pdf(path: Path, label: str) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Helvetica", 14)
    c.drawString(72, 720, label)
    c.showPage()
    c.save()


def test_merge_split_extract_round_trip(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import extract  # type: ignore[import-not-found]
        import merge  # type: ignore[import-not-found]
        import split  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    _make_one_page_pdf(a, "ALPHA")
    _make_one_page_pdf(b, "BRAVO")

    combined = tmp_path / "combined.pdf"
    written = merge.merge([{"file": str(a)}, {"file": str(b)}], combined)
    assert written == 2
    assert combined.exists()

    out_dir = tmp_path / "split_out"
    parts = split.split(combined, "1,2", out_dir)
    assert len(parts) == 2

    payload = extract.extract(combined, tables_strategy=None)
    assert payload["pages"] == 2
    page_texts = [item["content"] for item in payload["text"]]
    full_text = "\n".join(page_texts)
    assert "ALPHA" in full_text
    assert "BRAVO" in full_text


def test_split_range_parsing() -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import split  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    assert split.split_ranges("1-3") == [[1, 2, 3]]
    assert split.split_ranges("1,3,5") == [[1], [3], [5]]
    assert split.split_ranges("1-2,4-5") == [[1, 2], [4, 5]]
    # Reverse range gets normalized.
    assert split.split_ranges("5-3") == [[3, 4, 5]]


def test_merge_range_parsing() -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import merge  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    assert merge.parse_ranges(None, 4) == [1, 2, 3, 4]
    assert merge.parse_ranges("1,3", 4) == [1, 3]
    assert merge.parse_ranges("1-3", 5) == [1, 2, 3]
    # Out-of-range pages are filtered.
    assert merge.parse_ranges("1,99", 4) == [1]


def test_extract_creates_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import extract  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    pdf_file = tmp_path / "doc.pdf"
    _make_one_page_pdf(pdf_file, "TEST")

    out_file = tmp_path / "nested" / "dir" / "out.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["extract.py", str(pdf_file), "--out", str(out_file)],
    )
    assert extract.main() == 0
    assert out_file.is_file()


def _extract_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import extract  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return extract


def _make_borderless_table_pdf(path: Path) -> None:
    """A platypus table with no ruling lines -- the layout `text` mode exists for."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.platypus import SimpleDocTemplate, Table

    doc = SimpleDocTemplate(str(path), pagesize=LETTER)
    # Four rows: pdfplumber's `text` mode needs ``min_words_vertical`` (default
    # 3) aligned words to accept a column edge, so stay clear of that threshold.
    doc.build([Table([["Name", "Qty"], ["Widget", "3"], ["Gadget", "7"], ["Gizmo", "9"]])])


def test_tables_strategy_text_detects_a_borderless_table(tmp_path: Path) -> None:
    """`--tables-strategy text` must switch both axes.

    Only `vertical_strategy` used to be set, so the horizontal axis kept
    looking for ruling lines a borderless table does not have and the flag's
    one documented use case returned no tables at all.
    """
    extract = _extract_module()
    pdf_file = tmp_path / "borderless.pdf"
    _make_borderless_table_pdf(pdf_file)

    payload = extract.extract(pdf_file, tables_strategy="text")

    cells = {cell for table in payload["tables"] for row in table["rows"] for cell in row if cell}
    assert {"Name", "Qty", "Widget", "3", "Gadget", "7", "Gizmo", "9"} <= cells


def test_tables_strategy_lines_still_the_default(tmp_path: Path) -> None:
    extract = _extract_module()
    pdf_file = tmp_path / "borderless.pdf"
    _make_borderless_table_pdf(pdf_file)

    assert extract._table_settings(None) == {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
    }
    # A borderless table has no ruling lines, so the default finds nothing.
    assert extract.extract(pdf_file, tables_strategy=None)["tables"] == []


def test_tables_strategy_explicit_is_rejected_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """pdfplumber's `explicit` mode needs line lists this script cannot supply.

    It used to be advertised in `choices` and then crash inside pdfplumber
    with `TypeError: object of type 'NoneType' has no len()` on every call.
    """
    extract = _extract_module()
    pdf_file = tmp_path / "doc.pdf"
    _make_one_page_pdf(pdf_file, "TEST")

    with pytest.raises(ValueError, match="explicit"):
        extract.extract(pdf_file, tables_strategy="explicit")

    monkeypatch.setattr(sys, "argv", ["extract.py", str(pdf_file), "--tables-strategy", "explicit"])
    with pytest.raises(SystemExit) as exc_info:
        extract.main()
    assert exc_info.value.code == 2
    assert "invalid choice: 'explicit'" in capsys.readouterr().err


def _merge_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import merge  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return merge


# ── a merge that merges nothing is a failure, not a 0-page PDF (Issue #1921) ──


def test_merge_writes_nothing_when_no_pages_are_selected(tmp_path: Path) -> None:
    """`PdfWriter.write` happily produces a valid 311-byte 0-page PDF. Writing
    one told the caller the merge succeeded and left an unusable file behind."""
    merge = _merge_module()
    out = tmp_path / "out.pdf"

    written = merge.merge([{"file": str(tmp_path / "missing.pdf")}], out)

    assert written == 0
    assert not out.exists()


def test_merge_does_not_touch_an_existing_output(tmp_path: Path) -> None:
    """The destructive case: the old code overwrote a real PDF with an empty one."""
    merge = _merge_module()
    out = tmp_path / "out.pdf"
    _make_one_page_pdf(out, "KEEP ME")
    before = out.read_bytes()

    assert merge.merge([{"file": str(tmp_path / "missing.pdf")}], out) == 0
    assert out.read_bytes() == before


def test_merge_does_not_create_the_parent_directory_for_nothing(tmp_path: Path) -> None:
    merge = _merge_module()
    out = tmp_path / "nested" / "dir" / "out.pdf"

    assert merge.merge([{"file": str(tmp_path / "missing.pdf")}], out) == 0
    assert not out.parent.exists()


def test_merge_called_directly_skips_an_unusable_entry(tmp_path: Path) -> None:
    """`merge` is public, not only reached through `load_manifest`. A caller that
    hands it a bad entry should get the missing-file treatment, not a TypeError
    from `item["file"]` half way through a partly-built document."""
    merge = _merge_module()
    good = tmp_path / "a.pdf"
    _make_one_page_pdf(good, "ALPHA")
    out = tmp_path / "out.pdf"

    written = merge.merge(
        ["not-a-dict", {"pages": "1"}, {"file": 7}, {"file": str(good)}],  # type: ignore[list-item]
        out,
    )

    assert written == 1
    assert out.is_file()


def test_merge_called_directly_with_only_unusable_entries_writes_nothing(
    tmp_path: Path,
) -> None:
    merge = _merge_module()
    out = tmp_path / "out.pdf"

    assert merge.merge(["not-a-dict", {"pages": "1"}], out) == 0  # type: ignore[list-item]
    assert not out.exists()


def test_merge_exits_2_when_nothing_was_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    merge = _merge_module()
    out = tmp_path / "out.pdf"
    monkeypatch.setattr(sys, "argv", ["merge.py", str(tmp_path / "missing.pdf"), "--out", str(out)])

    assert merge.main() == 2

    captured = capsys.readouterr()
    assert "error: nothing was written" in captured.err
    assert "pages_written" not in captured.out
    assert not out.exists()


def test_merge_still_reports_success_when_pages_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The empty-merge guard must not disturb the working path."""
    merge = _merge_module()
    source = tmp_path / "a.pdf"
    _make_one_page_pdf(source, "ALPHA")
    out = tmp_path / "nested" / "out.pdf"
    monkeypatch.setattr(sys, "argv", ["merge.py", str(source), "--out", str(out)])

    assert merge.main() == 0

    assert '"pages_written": 1' in capsys.readouterr().out
    assert out.is_file()


def test_merge_out_of_range_pages_are_a_failure_not_an_empty_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page ranges that match nothing reach the same guard as missing files."""
    merge = _merge_module()
    source = tmp_path / "a.pdf"
    _make_one_page_pdf(source, "ALPHA")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"file": str(source), "pages": "50-60"}]), encoding="utf-8")
    out = tmp_path / "out.pdf"
    monkeypatch.setattr(sys, "argv", ["merge.py", str(manifest), "--out", str(out)])

    assert merge.main() == 2
    assert not out.exists()


# ── unusable manifests are reported, not raised (Issue #1921) ────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("not json at all", "is not valid JSON"),
        ('{"file": "a.pdf"}', "must be a JSON array"),
        ('["a.pdf", "b.pdf"]', 'entry 0 must be an object with a "file" key'),
        ('[{"file": "a.pdf"}, "b.pdf"]', 'entry 1 must be an object with a "file" key'),
        ('[{"pages": "1-2"}]', 'entry 0 is missing the "file" key'),
        ('[{"file": 7}]', 'entry 0 has a non-string "file"'),
        ('[{"file": "a.pdf", "pages": 3}]', 'entry 0 has a non-string "pages"'),
    ],
)
def test_an_unusable_manifest_exits_2_with_a_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
    expected: str,
) -> None:
    """Each of these used to escape as a traceback -- JSONDecodeError, TypeError
    ('string indices must be integers') or KeyError('file')."""
    merge = _merge_module()
    manifest = tmp_path / "m.json"
    manifest.write_text(body, encoding="utf-8")
    out = tmp_path / "out.pdf"
    monkeypatch.setattr(sys, "argv", ["merge.py", str(manifest), "--out", str(out)])

    assert merge.main() == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert expected in captured.err
    assert not out.exists()


def test_a_valid_manifest_still_loads(tmp_path: Path) -> None:
    merge = _merge_module()
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps([{"file": "a.pdf", "pages": "1-3"}, {"file": "b.pdf"}]), encoding="utf-8"
    )

    assert merge.load_manifest(manifest) == [
        {"file": "a.pdf", "pages": "1-3"},
        {"file": "b.pdf"},
    ]


def test_a_missing_manifest_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    merge = _merge_module()
    monkeypatch.setattr(
        sys,
        "argv",
        ["merge.py", str(tmp_path / "nope.json"), "--out", str(tmp_path / "out.pdf")],
    )

    assert merge.main() == 2
    assert "not found" in capsys.readouterr().err
