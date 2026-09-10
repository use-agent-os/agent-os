"""docx skill — load, eligibility, and create→inspect round-trip."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentos.skills.eligibility import EligibilityContext, check_eligibility
from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
DOCX_DIR = BUNDLED / "docx"
SCRIPTS = DOCX_DIR / "scripts"


def _spec_to_loader() -> object:
    return SkillLoader(bundled_dir=BUNDLED).get_by_name("docx")


def test_skill_loads() -> None:
    spec = _spec_to_loader()
    assert spec is not None
    assert spec.name == "docx"
    assert spec.metadata is not None
    assert spec.provenance.origin == "clawhub-mit0"
    assert spec.provenance.license == "MIT-0"


def test_eligibility_with_python_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentos.skills.eligibility.shutil.which",
        lambda name: "/usr/bin/python3" if name in {"python", "python3"} else None,
    )
    spec = _spec_to_loader()
    assert spec is not None
    assert check_eligibility(spec, EligibilityContext.auto())


def test_eligibility_without_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentos.skills.eligibility.shutil.which",
        lambda name: None,
    )
    spec = _spec_to_loader()
    assert spec is not None
    assert not check_eligibility(spec, EligibilityContext.auto())


def test_create_then_inspect_round_trip(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_docx  # type: ignore[import-not-found]
        import inspect_docx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    spec = {
        "metadata": {"title": "Round-trip", "author": "Tester"},
        "body": [
            {"kind": "heading", "level": 1, "text": "Hello"},
            {"kind": "paragraph", "text": "World."},
            {"kind": "table", "rows": [["A", "B"], ["1", "2"]]},
        ],
    }
    out_path = tmp_path / "out.docx"
    doc = create_docx.build(spec)
    doc.save(str(out_path))
    assert out_path.exists()

    inspected = inspect_docx.inspect(out_path)
    assert inspected["sections"] >= 1
    texts = [p["text"] for p in inspected["paragraphs"]]
    assert "Hello" in texts
    assert "World." in texts
    assert inspected["tables"] and inspected["tables"][0][0] == ["A", "B"]
    assert inspected["has_tracked_changes"] is False


def test_edit_replace_text(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_docx  # type: ignore[import-not-found]
        import edit_docx  # type: ignore[import-not-found]
        import inspect_docx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    src = tmp_path / "src.docx"
    create_docx.build({"body": [{"kind": "paragraph", "text": "Hello {{NAME}}, welcome."}]}).save(
        str(src)
    )

    from docx import Document

    doc = Document(str(src))
    ops = [{"op": "replace_text", "find": "{{NAME}}", "with": "Wei"}]
    edit_docx.apply_ops(doc, ops)
    out = tmp_path / "out.docx"
    doc.save(str(out))

    inspected = inspect_docx.inspect(out)
    text = " ".join(p["text"] for p in inspected["paragraphs"])
    assert "{{NAME}}" not in text
    assert "Wei" in text


def test_inspect_cli_outputs_json(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_docx  # type: ignore[import-not-found]
        import inspect_docx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    src = tmp_path / "src.docx"
    create_docx.build({"body": [{"kind": "paragraph", "text": "x"}]}).save(str(src))

    payload = inspect_docx.inspect(src)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "paragraphs" in encoded
    assert "tables" in encoded


def test_inspect_docx_creates_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_docx  # type: ignore[import-not-found]
        import inspect_docx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    src = tmp_path / "src.docx"
    create_docx.build({"body": [{"kind": "paragraph", "text": "x"}]}).save(str(src))

    out = tmp_path / "nested" / "dir" / "out.json"
    monkeypatch.setattr(sys, "argv", ["inspect_docx.py", str(src), "--out", str(out)])
    assert inspect_docx.main() == 0
    assert out.is_file()


def _edit_docx_module() -> object:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import edit_docx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return edit_docx


def _paragraph(runs: list[tuple[str, bool]]) -> object:
    """Build a one-paragraph document whose runs carry the given bold flags."""
    from docx import Document

    document = Document()
    paragraph = document.add_paragraph()
    for text, bold in runs:
        run = paragraph.add_run(text)
        if bold:
            run.bold = True
    return paragraph


def test_replace_text_keeps_the_formatting_of_untouched_runs() -> None:
    """A run the match never touched must survive byte-identical.

    Runs are where Word stores character formatting, so collapsing a paragraph
    into `runs[0]` gave every character run 0's formatting and left the rest as
    empty shells -- bold, italic, font and colour discarded for the whole
    paragraph even though one word changed.
    """
    edit_docx = _edit_docx_module()
    paragraph = _paragraph([("Hello ", False), ("world", True), (" and more", False)])

    assert edit_docx._replace_text_in_paragraph(paragraph, "Hello", "Hi") is True

    assert [(run.text, run.bold) for run in paragraph.runs] == [
        ("Hi ", None),
        ("world", True),
        (" and more", None),
    ]


def test_replace_text_across_a_run_boundary_keeps_the_second_run() -> None:
    """A `find` spanning runs still leaves the surrounding text in place.

    The replacement lands in the run owning the match's first character; the
    rest of the second run keeps its own text and formatting.
    """
    edit_docx = _edit_docx_module()
    paragraph = _paragraph([("Hel", False), ("lo world", True)])

    assert edit_docx._replace_text_in_paragraph(paragraph, "Hello", "Hi") is True

    assert [(run.text, run.bold) for run in paragraph.runs] == [("Hi", None), (" world", True)]


def test_replace_text_handles_several_matches_without_moving_text() -> None:
    """Each match is replaced where it starts, so runs keep their own share."""
    edit_docx = _edit_docx_module()
    paragraph = _paragraph([("aXa", False), ("Xa", True)])

    assert edit_docx._replace_text_in_paragraph(paragraph, "X", "-") is True

    assert [(run.text, run.bold) for run in paragraph.runs] == [("a-a", None), ("-a", True)]


@pytest.mark.parametrize(
    ("runs", "find", "replacement"),
    [
        ([("Hello ", False), ("world", True), (" and more", False)], "Hello", "Hi"),
        ([("Hel", False), ("lo world", True)], "Hello", "Hi"),
        ([("a", False), ("b", True), ("c", False)], "abc", "X"),
        ([("x{{N}}y", False)], "{{N}}", "Wei"),
        ([("aXa", False), ("Xa", True)], "X", "-"),
        ([("keep ", False), ("me", True)], "me", ""),
        ([("aa", False), ("aa", True)], "aa", "b"),
    ],
)
def test_replace_text_matches_str_replace_on_the_joined_text(
    runs: list[tuple[str, bool]], find: str, replacement: str
) -> None:
    """Whatever the run layout, the resulting text is plain `str.replace`.

    The old code already got the text right -- it was the run layout that was
    wrong -- so this pins the half that must not change while the fix moves
    characters back into their own runs.
    """
    edit_docx = _edit_docx_module()
    paragraph = _paragraph(runs)
    original = "".join(text for text, _ in runs)

    edit_docx._replace_text_in_paragraph(paragraph, find, replacement)

    assert "".join(run.text for run in paragraph.runs) == original.replace(find, replacement)


def test_replace_text_reports_false_when_the_needle_is_absent() -> None:
    """No match means no edit and no reported change."""
    edit_docx = _edit_docx_module()
    paragraph = _paragraph([("Hello ", False), ("world", True)])

    assert edit_docx._replace_text_in_paragraph(paragraph, "absent", "x") is False
    assert [(run.text, run.bold) for run in paragraph.runs] == [("Hello ", None), ("world", True)]


def test_replace_run_still_touches_only_its_own_run() -> None:
    """`replace_run` was never affected; keep it that way."""
    edit_docx = _edit_docx_module()
    paragraph = _paragraph([("Hello ", False), ("world", True)])

    edit_docx._replace_run(paragraph, 1, "there")

    assert [(run.text, run.bold) for run in paragraph.runs] == [("Hello ", None), ("there", True)]


def test_replace_text_in_tables(tmp_path: Path) -> None:
    """replace_text must walk paragraphs in tables and preserve formatting."""
    edit_docx = _edit_docx_module()
    from docx import Document

    doc = Document()
    doc.add_paragraph("Agreement for {{CLIENT}}")

    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "Client Name:"
    table.cell(0, 1).text = "{{CLIENT}}"

    # Merged row for summary
    merged_cell = table.cell(1, 0)
    merged_cell.text = "Notes for {{CLIENT}}"
    merged_cell.merge(table.cell(1, 1))

    table.cell(2, 0).text = "Amount:"
    table.cell(2, 1).text = "$5,000"

    ops = [{"op": "replace_text", "find": "{{CLIENT}}", "with": "Acme Corp"}]
    applied = edit_docx.apply_ops(doc, ops)

    assert applied == 3
    assert doc.paragraphs[0].text == "Agreement for Acme Corp"
    assert table.cell(0, 1).text == "Acme Corp"
    assert table.cell(1, 0).text == "Notes for Acme Corp"
    assert table.cell(1, 1).text == "Notes for Acme Corp"
    assert table.cell(2, 1).text == "$5,000"


def test_apply_ops_handles_non_dict_elements() -> None:
    """Malformed or non-dict items in ops must be safely skipped."""
    edit_docx = _edit_docx_module()
    from docx import Document

    doc = Document()
    doc.add_paragraph("Hello World")

    ops = [
        None,
        "not-a-dict",
        123,
        [],
        {"op": "replace_text", "find": "World", "with": "AgentOS"},
    ]
    applied = edit_docx.apply_ops(doc, ops)
    assert applied == 1
    assert doc.paragraphs[0].text == "Hello AgentOS"
