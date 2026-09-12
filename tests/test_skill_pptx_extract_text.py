"""pptx skill extract_text unit tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

from agentos.skills.bundled.pptx.scripts import extract_text


def test_shape_text_and_table_text_extraction(tmp_path: Path) -> None:
    """extract_text extracts paragraph text and multi-paragraph table cells cleanly."""
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    # 1. Textbox shape with paragraph text
    tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    tb.text_frame.text = "Slide Headline"
    p_body = tb.text_frame.add_paragraph()
    p_body.text = "Secondary point"

    # 2. Table with multi-paragraph cell
    table_shape = slide.shapes.add_table(2, 2, Inches(1), Inches(2.5), Inches(5), Inches(2))
    table = table_shape.table
    table.cell(0, 0).text_frame.text = "Metric"
    table.cell(0, 1).text_frame.text = "Value"

    c1 = table.cell(1, 0)
    c1.text_frame.text = "Q1 Revenue"
    p_extra = c1.text_frame.add_paragraph()
    p_extra.text = "(USD)"

    c2 = table.cell(1, 1)
    c2.text_frame.text = "$100M"

    # Test helpers directly
    shape_lines = extract_text._shape_text(tb)
    assert shape_lines == ["Slide Headline", "Secondary point"]

    table_lines = extract_text._table_text(table_shape)
    assert table_lines == ["Metric | Value", "Q1 Revenue (USD) | $100M"]

    # Save presentation and test CLI entrypoint
    pptx_path = tmp_path / "deck.pptx"
    prs.save(str(pptx_path))

    # Test main with --json
    stdout_capture = io.StringIO()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "stdout", stdout_capture)
    try:
        ret = extract_text.main([str(pptx_path), "--json"])
        assert ret == 0
    finally:
        monkeypatch.undo()

    payload = json.loads(stdout_capture.getvalue())
    assert len(payload) == 1
    slide_data = payload[0]
    assert slide_data["slide"] == 1
    assert "Slide Headline" in slide_data["text"]
    assert "Secondary point" in slide_data["text"]
    assert "Metric | Value" in slide_data["text"]
    assert "Q1 Revenue (USD) | $100M" in slide_data["text"]


#: "季度回顾 🎉" — CJK plus an astral emoji: cp936 fails on the emoji, cp1252
#: on every character.
_NON_ASCII = "季度回顾 \U0001f389"

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "pptx"
    / "scripts"
    / "extract_text.py"
)


def _deck_with_non_ascii(tmp_path: Path) -> Path:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(2))
    box.text_frame.text = _NON_ASCII
    path = tmp_path / "deck.pptx"
    prs.save(str(path))
    return path


def _run_script(deck: Path, *args: str, encoding: str) -> subprocess.CompletedProcess[bytes]:
    # A child process is the only way to give the script a stdout whose
    # encoding cannot represent the slide text, which is what a piped stdout
    # on a Windows code page does (Issue #1834).
    env = {**os.environ, "PYTHONIOENCODING": encoding}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(deck), *args],
        capture_output=True,
        env=env,
    )


def test_non_ascii_slide_text_survives_a_non_utf8_stdout_encoding(tmp_path: Path) -> None:
    deck = _deck_with_non_ascii(tmp_path)

    proc = _run_script(deck, encoding="cp936")

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert _NON_ASCII.encode() in proc.stdout


def test_non_ascii_json_output_survives_a_non_utf8_stdout_encoding(tmp_path: Path) -> None:
    # ``--json`` is a second write path: json.dump wrote straight to the text
    # stream, so it failed independently of the plain listing.
    deck = _deck_with_non_ascii(tmp_path)

    proc = _run_script(deck, "--json", encoding="cp1252")

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload[0]["text"] == [_NON_ASCII]


def test_write_falls_back_when_stdout_has_no_buffer() -> None:
    # Reviewer note on #764: the writer must stay correct when ``buffer`` is
    # absent or the stream is wrapped, rather than raising. The existing tests
    # in this file patch sys.stdout with a bare StringIO, so this path is live.
    class NoBufferStream(io.StringIO):
        encoding = "cp936"

    stream = NoBufferStream()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "stdout", stream)
    try:
        extract_text._write(f"{_NON_ASCII}\n")
    finally:
        monkeypatch.undo()

    written = stream.getvalue()
    assert "季度回顾" in written
    # The emoji has no cp936 form, so it is escaped rather than dropped.
    assert "?" not in written
