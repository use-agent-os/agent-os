"""docx ``edit_docx.py`` — JSON ``null`` was written as "None" (#3417).

``op.get("with", "")`` returns the default only when the key is *absent*. A
key present with a ``null`` value returns ``None``, and ``str(None)`` is the
four-letter word "None". So an ops file saying "clear this placeholder" --

    [{"op": "replace_text", "find": "{{notes}}", "with": null}]

-- put the word **None** into the document instead of emptying it, in a file
that then gets printed, signed or sent.

Same shape as #3160 for ``form_fill.py`` (fixed in #3227), in the other
document script. The assertions read the written ``.docx`` back: what matters
is what the reader sees, not what the ops dict held.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "docx" / "scripts"


def _scripts():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import create_docx  # type: ignore[import-not-found]
        import edit_docx  # type: ignore[import-not-found]
        import inspect_docx  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return create_docx, edit_docx, inspect_docx


def _document_text(path: Path) -> str:
    _, _, inspect_docx = _scripts()
    inspected = inspect_docx.inspect(path)
    return " ".join(p["text"] for p in inspected["paragraphs"])


def _apply(tmp_path: Path, body_text: str, ops: list[dict[str, object]]) -> str:
    create_docx, edit_docx, _ = _scripts()
    from docx import Document

    src = tmp_path / "src.docx"
    create_docx.build({"body": [{"kind": "paragraph", "text": body_text}]}).save(str(src))

    doc = Document(str(src))
    edit_docx.apply_ops(doc, ops)
    out = tmp_path / "out.docx"
    doc.save(str(out))
    return _document_text(out)


def test_a_null_replacement_clears_the_placeholder(tmp_path: Path) -> None:
    """The issue's repro."""
    text = _apply(
        tmp_path,
        "Notes: {{notes}} end",
        [{"op": "replace_text", "find": "{{notes}}", "with": None}],
    )

    assert "{{notes}}" not in text
    assert "None" not in text
    assert "Notes:  end" in text


def test_a_null_run_text_clears_the_run(tmp_path: Path) -> None:
    text = _apply(
        tmp_path,
        "original text",
        [{"op": "replace_run", "para": 0, "run": 0, "text": None}],
    )

    assert "None" not in text
    assert "original" not in text


def test_the_word_none_never_reaches_the_document(tmp_path: Path) -> None:
    """Stated as the property, across both op kinds at once."""
    text = _apply(
        tmp_path,
        "a {{x}} b",
        [
            {"op": "replace_text", "find": "{{x}}", "with": None},
            {"op": "replace_text", "find": "nothing-here", "with": None},
        ],
    )

    assert "None" not in text


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Wei", "Wei"), (0, "0"), (12, "12"), (False, "False"), (True, "True"), (3.5, "3.5")],
)
def test_other_values_are_still_stringified(tmp_path: Path, value: object, expected: str) -> None:
    """Only ``null`` means "no value". A zero or a false is an answer, and
    blanking it would be the same bug pointing the other way."""
    text = _apply(
        tmp_path,
        "value: {{v}}",
        [{"op": "replace_text", "find": "{{v}}", "with": value}],
    )

    assert expected in text


def test_a_null_find_is_still_skipped(tmp_path: Path) -> None:
    """``find`` resolving to empty means the op names no target, which
    ``apply_ops`` already skips -- an empty ``find`` must not match every
    position in the paragraph."""
    text = _apply(
        tmp_path,
        "untouched text",
        [{"op": "replace_text", "find": None, "with": "X"}],
    )

    assert text.strip() == "untouched text"


def test_an_absent_key_behaves_as_before(tmp_path: Path) -> None:
    """The path that already worked: a missing ``with`` clears the match."""
    text = _apply(
        tmp_path,
        "Notes: {{notes}} end",
        [{"op": "replace_text", "find": "{{notes}}"}],
    )

    assert "{{notes}}" not in text
    assert "None" not in text
