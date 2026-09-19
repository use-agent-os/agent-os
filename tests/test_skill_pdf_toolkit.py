"""pdf-toolkit skill — load, eligibility, and merge→split→extract round-trip."""

from __future__ import annotations

import importlib
import json
import re
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
    result = merge.merge([{"file": str(a)}, {"file": str(b)}], combined)
    assert result.pages_written == 2
    assert result.skipped == []
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


SKILL_MD = BUNDLED / "pdf-toolkit" / "SKILL.md"
_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]+")


def _documented_flags() -> set[str]:
    """Every long flag SKILL.md names, in prose as well as in command blocks.

    The flag that prompted this guard was advertised in a Caveats bullet, not
    in a runnable example, so scanning only the fenced blocks would miss it.
    """
    return set(_FLAG_RE.findall(SKILL_MD.read_text(encoding="utf-8")))


def _accepted_flags(script: str, capsys: pytest.CaptureFixture[str]) -> set[str]:
    """Long flags *script* actually accepts, read off its own argparse help."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        module = importlib.import_module(script)
    finally:
        sys.path.pop(0)
    with pytest.raises(SystemExit) as exit_info:
        module._parse_args()
    assert exit_info.value.code == 0
    return set(_FLAG_RE.findall(capsys.readouterr().out))


def test_every_flag_skill_md_documents_is_accepted_by_a_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SKILL.md must not advertise a flag the scripts reject.

    An agent following the skill text runs the command verbatim, so a flag that
    is documented but never declared is a hard `argparse` exit 2 rather than a
    degraded result. Scoped to pdf-toolkit: its SKILL.md only ever invokes its
    own four scripts, so every flag it names has to come from one of them.
    """
    accepted: set[str] = set()
    for script in ("extract", "form_fill", "merge", "split"):
        monkeypatch.setattr(sys, "argv", [f"{script}.py", "--help"])
        accepted |= _accepted_flags(script, capsys)

    assert _documented_flags() <= accepted, (
        f"SKILL.md documents flags no pdf-toolkit script accepts: "
        f"{sorted(_documented_flags() - accepted)}"
    )


def test_the_flag_scan_actually_finds_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep the guard above from passing because a scan came back empty."""
    monkeypatch.setattr(sys, "argv", ["form_fill.py", "--help"])
    assert {"--list-fields", "--out"} <= _accepted_flags("form_fill", capsys)
    assert {"--list-fields", "--tables-strategy", "--pages"} <= _documented_flags()


def test_form_fill_rejects_the_flag_skill_md_used_to_advertise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins why the caveat had to change: nothing implements `--clear-signatures`.

    Passes either way by design — it documents the behaviour the doc fix had to
    match, and turns into a reminder to re-document if the flag is ever added.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        import form_fill  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    monkeypatch.setattr(
        sys, "argv", ["form_fill.py", "f.pdf", "d.json", "--out", "o.pdf", "--clear-signatures"]
    )
    with pytest.raises(SystemExit) as exit_info:
        form_fill._parse_args()
    assert exit_info.value.code == 2


# ── a malformed --pages spec is reported, not raised (#2128) ─────────────────


def _split_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import split  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return split


def _merge_module_for_pages():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import merge  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return merge


@pytest.mark.parametrize("spec", ["abc", "1,x", "1-abc", "one-two"])
def test_split_ranges_rejects_a_non_numeric_spec(spec: str) -> None:
    split = _split_module()

    with pytest.raises(split.PageSpecError, match="not a page number"):
        split.split_ranges(spec)


@pytest.mark.parametrize("spec", ["1–3", "1—3", "1−3"])
def test_a_dash_that_is_not_a_hyphen_says_so(spec: str) -> None:
    """The realistic trigger: --pages is written by the model, and an en dash
    never splits the token, so the whole thing reaches int()."""
    split = _split_module()

    with pytest.raises(split.PageSpecError, match="en/em dash"):
        split.split_ranges(spec)


@pytest.mark.parametrize("spec", ["3-", "-5", "-", "1,3-", "1, -5"])
def test_an_open_ended_range_is_named_as_such(spec: str) -> None:
    """``3-`` and ``-5`` are rejected -- there is no ``total`` here to close
    them against -- but the message names the case, not an empty string."""
    split = _split_module()

    with pytest.raises(split.PageSpecError, match="open-ended range") as exc_info:
        split.split_ranges(spec)

    assert "''" not in str(exc_info.value)
    assert repr(spec.split(",")[-1].strip()) in str(exc_info.value)


def test_the_error_names_the_offending_token_not_just_the_spec() -> None:
    split = _split_module()

    with pytest.raises(split.PageSpecError) as exc_info:
        split.split_ranges("1-3,5,x,7")

    message = str(exc_info.value)
    assert "'1-3,5,x,7'" in message
    assert "'x' is not a page number" in message


def test_split_reports_a_malformed_spec_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    split = _split_module()
    pdf_file = tmp_path / "doc.pdf"
    _make_one_page_pdf(pdf_file, "ALPHA")
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv", ["split.py", str(pdf_file), "--pages", "abc", "--out", str(out_dir)]
    )

    assert split.main() == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
    assert "--pages" in captured.err
    assert captured.out == ""
    assert not out_dir.exists(), "an unparseable spec must not create the output directory"


def test_split_reports_an_open_ended_range_on_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    split = _split_module()
    pdf_file = tmp_path / "doc.pdf"
    _make_one_page_pdf(pdf_file, "ALPHA")
    monkeypatch.setattr(
        sys, "argv", ["split.py", str(pdf_file), "--pages", "3-", "--out", str(tmp_path / "out")]
    )

    assert split.main() == 2
    assert "open-ended range '3-'" in capsys.readouterr().err


def test_split_parses_the_spec_before_opening_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad spec is the caller's mistake; it is named before the document is
    touched, so a broken PDF never masks it."""
    split = _split_module()
    opened: list[str] = []

    def reader(path: str) -> None:
        opened.append(path)
        raise AssertionError("the document must not be opened")

    monkeypatch.setattr(split, "PdfReader", reader)

    with pytest.raises(split.PageSpecError):
        split.split(tmp_path / "doc.pdf", "abc", tmp_path / "out")

    assert opened == []


def test_split_still_splits_a_valid_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    split = _split_module()
    pdf_file = tmp_path / "doc.pdf"
    _make_one_page_pdf(pdf_file, "ALPHA")
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv", ["split.py", str(pdf_file), "--pages", "1", "--out", str(out_dir)]
    )

    assert split.main() == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


@pytest.mark.parametrize("spec", ["abc", "1,x", "1–3"])
def test_merge_parse_ranges_rejects_a_non_numeric_spec(spec: str) -> None:
    merge = _merge_module_for_pages()

    with pytest.raises(merge.PageSpecError, match="not a page number"):
        merge.parse_ranges(spec, 5)


@pytest.mark.parametrize("spec", ["3-", "-5", "-"])
def test_merge_names_an_open_ended_range(spec: str) -> None:
    merge = _merge_module_for_pages()

    with pytest.raises(merge.PageSpecError, match=f"open-ended range '{spec}'"):
        merge.requested_pages(spec, 5)


def test_merge_requested_pages_and_parse_ranges_reject_the_same_specs() -> None:
    """``parse_ranges`` is ``requested_pages`` clamped; both must refuse."""
    merge = _merge_module_for_pages()

    for spec in ("abc", "3-"):
        with pytest.raises(merge.PageSpecError):
            merge.requested_pages(spec, 5)
        with pytest.raises(merge.PageSpecError):
            merge.parse_ranges(spec, 5)


def test_merge_reports_a_malformed_manifest_pages_value_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    merge = _merge_module_for_pages()
    source = tmp_path / "a.pdf"
    _make_one_page_pdf(source, "ALPHA")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"file": str(source), "pages": "abc"}]), encoding="utf-8")
    out = tmp_path / "out.pdf"
    monkeypatch.setattr(sys, "argv", ["merge.py", str(manifest), "--out", str(out)])

    assert merge.main() == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
    assert "not a page number" in captured.err
    assert captured.out == ""
    assert not out.exists()


def test_merge_reports_a_bad_pages_value_like_any_other_manifest_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same prefix, same exit code as ``ManifestError``: a caller matching on
    ``error:`` / 2 sees one kind of failure, not two."""
    merge = _merge_module_for_pages()
    source = tmp_path / "a.pdf"
    _make_one_page_pdf(source, "ALPHA")
    out = tmp_path / "out.pdf"

    manifest = tmp_path / "bad_shape.json"
    manifest.write_text(json.dumps([{"pages": "1"}]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["merge.py", str(manifest), "--out", str(out)])
    shape_code = merge.main()
    shape_err = capsys.readouterr().err

    manifest = tmp_path / "bad_pages.json"
    manifest.write_text(json.dumps([{"file": str(source), "pages": "2-"}]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["merge.py", str(manifest), "--out", str(out)])
    pages_code = merge.main()
    pages_err = capsys.readouterr().err

    assert (shape_code, pages_code) == (2, 2)
    assert shape_err.startswith("error: ") and pages_err.startswith("error: ")
    assert "open-ended range '2-'" in pages_err


def test_merge_still_honours_a_valid_pages_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    merge = _merge_module_for_pages()
    source = tmp_path / "a.pdf"
    _make_one_page_pdf(source, "ALPHA")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"file": str(source), "pages": "1"}]), encoding="utf-8")
    out = tmp_path / "out.pdf"
    monkeypatch.setattr(sys, "argv", ["merge.py", str(manifest), "--out", str(out)])

    assert merge.main() == 0
    assert json.loads(capsys.readouterr().out)["pages_written"] == 1
