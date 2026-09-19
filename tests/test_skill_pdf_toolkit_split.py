"""pdf-toolkit ``split.py`` — out-of-range pages are reported, never dropped silently.

A range that runs past the last page used to yield a shorter PDF than asked
for, and a range entirely past the end yielded no file at all, both with exit 0
and a summary that named only the files written (#1902).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "pdf-toolkit" / "scripts"


def _split_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import split  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return split


def _make_pdf(path: Path, pages: int) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=LETTER)
    for number in range(1, pages + 1):
        c.setFont("Helvetica", 14)
        c.drawString(72, 720, f"PAGE {number}")
        c.showPage()
    c.save()


def _page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


@pytest.fixture
def five_pages(tmp_path: Path) -> Path:
    pdf = tmp_path / "five.pdf"
    _make_pdf(pdf, 5)
    return pdf


def test_in_range_split_reports_every_page_as_written(five_pages: Path, tmp_path: Path) -> None:
    split = _split_module()

    result = split.split(five_pages, "1-3", tmp_path / "out")

    assert [p.name for p in result.files] == ["five_001.pdf"]
    assert result.parts == [(result.files[0], [1, 2, 3])]
    assert result.skipped_pages == []
    assert result.total_pages == 5


def test_range_past_the_end_reports_the_pages_it_could_not_write(
    five_pages: Path, tmp_path: Path
) -> None:
    split = _split_module()

    result = split.split(five_pages, "3-7", tmp_path / "out")

    assert _page_count(result.files[0]) == 3
    assert result.parts == [(result.files[0], [3, 4, 5])]
    assert result.skipped_pages == [6, 7]


def test_written_files_are_numbered_without_holes(five_pages: Path, tmp_path: Path) -> None:
    """A skipped group must not leave a gap for callers that glob for ``_001``."""
    split = _split_module()

    result = split.split(five_pages, "7-9,1-2", tmp_path / "out")

    assert [p.name for p in result.files] == ["five_001.pdf"]
    assert result.parts == [(result.files[0], [1, 2])]
    assert result.skipped_pages == [7, 8, 9]


def test_main_summary_names_written_and_skipped_pages(
    five_pages: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    split = _split_module()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv", ["split.py", str(five_pages), "--pages", "3-7", "--out", str(out_dir)]
    )

    assert split.main() == 0

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["files"] == [str(out_dir / "five_001.pdf")]
    assert summary["count"] == 1
    assert summary["parts"] == [{"file": str(out_dir / "five_001.pdf"), "pages": [3, 4, 5]}]
    assert summary["skipped_pages"] == [6, 7]
    assert summary["total_pages"] == 5
    assert "6" in captured.err and "7" in captured.err, "dropped pages must be warned on stderr"


def test_main_fails_when_every_requested_page_is_out_of_range(
    five_pages: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    split = _split_module()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv", ["split.py", str(five_pages), "--pages", "7-9", "--out", str(out_dir)]
    )

    assert split.main() == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert "5 pages" in captured.err
    assert not out_dir.exists(), "nothing may be written, not even the output directory"


def test_main_stays_quiet_on_stderr_when_nothing_was_dropped(
    five_pages: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    split = _split_module()
    monkeypatch.setattr(
        sys,
        "argv",
        ["split.py", str(five_pages), "--pages", "1-2,4", "--out", str(tmp_path / "out")],
    )

    assert split.main() == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    summary = json.loads(captured.out)
    assert summary["count"] == 2
    assert summary["skipped_pages"] == []


# ---------------------------------------------------------------------------
# #2996: a span is two numbers until it is clamped to the document
# ---------------------------------------------------------------------------


def test_a_runaway_range_splits_the_real_pages_without_expanding_the_span(
    five_pages: Path, tmp_path: Path
) -> None:
    """``1-100000000`` used to allocate a hundred million ints -- and list
    every one of them as skipped -- before the page count was consulted."""
    split = _split_module()

    result = split.split(five_pages, "1-100000000", tmp_path / "out")

    assert result.parts == [(result.files[0], [1, 2, 3, 4, 5])]
    assert len(result.skipped_pages) == split.MAX_REPORTED_SKIPPED
    assert result.skipped_pages[:2] == [6, 7]
    assert result.skipped_pages_omitted == 100_000_000 - 5 - split.MAX_REPORTED_SKIPPED


def test_a_runaway_range_summary_counts_what_it_does_not_list(
    five_pages: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    split = _split_module()
    monkeypatch.setattr(
        sys,
        "argv",
        ["split.py", str(five_pages), "--pages", "4-100000", "--out", str(tmp_path / "out")],
    )

    assert split.main() == 0

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["parts"][0]["pages"] == [4, 5]
    assert len(summary["skipped_pages"]) == split.MAX_REPORTED_SKIPPED
    assert summary["skipped_pages_omitted"] == 100_000 - 5 - split.MAX_REPORTED_SKIPPED
    assert "more" in captured.err


def test_an_ordinary_summary_has_no_omitted_count(
    five_pages: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The additive field appears only when something was actually omitted."""
    split = _split_module()
    monkeypatch.setattr(
        sys, "argv", ["split.py", str(five_pages), "--pages", "1-7", "--out", str(tmp_path / "o")]
    )

    assert split.main() == 0

    assert "skipped_pages_omitted" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("spec", ["1-", "-5", "a-b", "3,x"])
def test_a_malformed_page_spec_is_an_error_not_a_traceback(
    spec: str,
    five_pages: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    split = _split_module()
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv", ["split.py", str(five_pages), "--pages", spec, "--out", str(out_dir)]
    )

    assert split.main() == 2

    assert "error: invalid page range" in capsys.readouterr().err
    assert not out_dir.exists()
