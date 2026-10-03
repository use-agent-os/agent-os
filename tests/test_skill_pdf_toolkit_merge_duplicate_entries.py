"""pdf-toolkit ``merge.py`` — a file named twice keeps a count per entry (#3589).

``MergeResult`` records skipped pages and omitted counts per manifest entry,
correctly. ``main`` then read the count back with ``dict(result.skipped_omitted)``,
and ``dict`` keeps the last value for a repeated key -- so two entries naming
the same PDF, which is the ordinary way to interleave pages of one document,
were both reported with the second entry's count.

The merged PDF was never wrong; the number telling the caller how much a
runaway range dropped was.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "pdf-toolkit" / "scripts"


def _merge_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import merge  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return merge


def _make_pdf(path: Path, pages: int, tag: str) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=LETTER)
    for number in range(1, pages + 1):
        c.setFont("Helvetica", 14)
        c.drawString(72, 720, f"{tag} PAGE {number}")
        c.showPage()
    c.save()


@pytest.fixture
def five_pages(tmp_path: Path) -> Path:
    pdf = tmp_path / "report.pdf"
    _make_pdf(pdf, 5, "REPORT")
    return pdf


def _run(tmp_path: Path, manifest: list[dict], monkeypatch) -> tuple[int, str, str]:
    mg = _merge_module()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "combined.pdf"
    monkeypatch.setattr(
        sys, "argv", ["merge.py", str(manifest_path), "--out", str(out)]
    )
    code = mg.main()
    return code, out.as_posix(), manifest_path.as_posix()


# ── the issue's reproduction ───────────────────────────────────────────────


def test_each_entry_reports_its_own_omitted_count(
    five_pages: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest = [
        {"file": str(five_pages), "pages": "1-2,9"},
        {"file": str(five_pages), "pages": "5,100-5000"},
    ]

    code, _out, _mf = _run(tmp_path, manifest, monkeypatch)
    captured = capsys.readouterr()

    assert code == 0
    summary = json.loads(captured.out.strip().splitlines()[-1])
    entries = summary["skipped_pages"]

    assert len(entries) == 2, "one entry per manifest entry, not one per file"
    # The first entry asks for page 9 only: one page past the end, nothing omitted.
    assert entries[0]["pages"] == [9]
    assert "omitted" not in entries[0], entries[0]
    # The second asks for 100-5000: 1000 listed, the rest counted.
    assert entries[1]["pages"][0] == 100
    assert entries[1]["omitted"] == 5000 - 100 + 1 - len(entries[1]["pages"])


def test_the_warning_lines_do_not_share_one_count(
    five_pages: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest = [
        {"file": str(five_pages), "pages": "1,9"},
        {"file": str(five_pages), "pages": "2,100-5000"},
    ]

    _run(tmp_path, manifest, monkeypatch)
    warnings = [line for line in capsys.readouterr().err.splitlines() if line.startswith("warn:")]

    assert len(warnings) == 2
    assert "more" not in warnings[0], warnings[0]
    assert "more" in warnings[1], warnings[1]


def test_the_merged_pdf_is_unaffected(
    five_pages: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """The output was always right; only the reporting was not."""
    from pypdf import PdfReader

    manifest = [
        {"file": str(five_pages), "pages": "1-2,9"},
        {"file": str(five_pages), "pages": "5,100-5000"},
    ]

    _run(tmp_path, manifest, monkeypatch)
    capsys.readouterr()

    assert len(PdfReader(str(tmp_path / "combined.pdf")).pages) == 3  # 1, 2, 5


# ── what must not change ───────────────────────────────────────────────────


def test_two_different_files_still_report_separately(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    _make_pdf(a, 2, "ALPHA")
    _make_pdf(b, 2, "BETA")
    manifest = [{"file": str(a), "pages": "1,9"}, {"file": str(b), "pages": "1,7"}]

    _run(tmp_path, manifest, monkeypatch)
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert [e["pages"] for e in summary["skipped_pages"]] == [[9], [7]]


def test_an_entry_with_nothing_skipped_is_not_reported(
    five_pages: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest = [{"file": str(five_pages), "pages": "1-2"}]

    _run(tmp_path, manifest, monkeypatch)
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert summary["skipped_pages"] == []
    assert summary["pages_written"] == 2


def test_an_uncapped_skip_still_omits_the_omitted_key(
    five_pages: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """An ordinary summary keeps exactly the shape it had before."""
    manifest = [{"file": str(five_pages), "pages": "1,9"}]

    _run(tmp_path, manifest, monkeypatch)
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert summary["skipped_pages"] == [{"file": str(five_pages), "pages": [9]}]


def test_the_two_result_lists_stay_index_aligned(five_pages: Path) -> None:
    """The invariant the fix rests on, asserted directly."""
    mg = _merge_module()

    result = mg.merge(
        [
            {"file": str(five_pages), "pages": "9"},
            {"file": str(five_pages), "pages": "1"},
            {"file": str(five_pages), "pages": "100-5000"},
        ],
        five_pages.parent / "out.pdf",
    )

    assert len(result.skipped) == len(result.skipped_omitted)
    assert [f for f, _ in result.skipped] == [f for f, _ in result.skipped_omitted]
    assert [n for _, n in result.skipped_omitted] == [0, 3901]
