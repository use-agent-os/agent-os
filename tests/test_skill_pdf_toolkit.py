"""pdf-toolkit skill — load, eligibility, and merge→split→extract round-trip."""

from __future__ import annotations

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
    assert len(parts.files) == 2

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


@pytest.mark.parametrize("spec", ["abc", "1–3", "-5", "0", "1-0"])
def test_merge_fails_on_malformed_pages_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], spec: str
) -> None:
    import json

    sys.path.insert(0, str(SCRIPTS))
    try:
        import merge  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    pdf = tmp_path / "test.pdf"
    _make_one_page_pdf(pdf, "Page 1")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"file": str(pdf), "pages": spec}]), encoding="utf-8")
    out_file = tmp_path / "merged.pdf"

    monkeypatch.setattr(sys, "argv", ["merge.py", str(manifest), "--out", str(out_file)])
    assert merge.main() == 2

    captured = capsys.readouterr()
    assert captured.err == "error: --pages must be 1-based numbers and ranges, e.g. '1-3,5'\n"
    assert not out_file.exists()
