"""pptx render_thumbs.sh — the ``--range`` selector.

The selector is translated into pdftoppm's two independent bounds, ``-f`` and
``-l``, so the only honest way to test it is to look at what pdftoppm was
actually handed. Neither LibreOffice nor poppler is installed on CI, so both
are stubbed onto PATH; the stubs record their argv into the working directory
rather than an absolute path, which keeps them working under Git Bash on the
Windows job as well as on Ubuntu.

Guarded on bash being present rather than on the platform: both CI runners
have it (Git for Windows ships bash), so these run on both jobs rather than
only on Ubuntu.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "agentos" / "skills" / "bundled" / "pptx" / "scripts" / "render_thumbs.sh"

_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(_BASH is None, reason="render_thumbs.sh is a bash script")

_SOFFICE_STUB = """#!/bin/sh
# Stand-in for LibreOffice: leave the PDF the script expects, and a marker
# proving the conversion was reached at all.
: > soffice_ran
outdir="."
input=""
while [ $# -gt 0 ]; do
  case "$1" in
    --outdir) outdir="$2"; shift 2 ;;
    --*) shift ;;
    *) input="$1"; shift ;;
  esac
done
base=$(basename "$input" .pptx)
: > "$outdir/$base.pdf"
"""

_PDFTOPPM_STUB = """#!/bin/sh
printf '%s\\n' "$@" > pdftoppm_argv
"""


def _workspace(tmp_path: Path) -> Path:
    """A deck to render, plus stubbed tools on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "soffice").write_text(_SOFFICE_STUB, encoding="utf-8", newline="\n")
    (bin_dir / "pdftoppm").write_text(_PDFTOPPM_STUB, encoding="utf-8", newline="\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    (tmp_path / "deck.pptx").write_bytes(b"")
    return bin_dir


def _env_with_stubs(bin_dir: Path) -> dict[str, str]:
    """The stubs first, then the real PATH -- bash still needs cat and basename."""
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script on ``deck.pptx`` from inside tmp_path.

    The deck is named relatively on purpose: an absolute Windows path would
    reach bash with backslashes in it.
    """
    bin_dir = _workspace(tmp_path)
    return subprocess.run(
        [str(_BASH), str(SCRIPT), "deck.pptx", *args],
        cwd=tmp_path,
        env=_env_with_stubs(bin_dir),
        capture_output=True,
        text=True,
    )


def _pdftoppm_argv(tmp_path: Path) -> list[str]:
    recorded = tmp_path / "pdftoppm_argv"
    assert recorded.is_file(), "pdftoppm was never reached"
    return recorded.read_text(encoding="utf-8").split("\n")[:-1]


def _bounds(argv: list[str]) -> list[str]:
    """Just the page-range flags, so dpi and paths do not fix the assertion."""
    out: list[str] = []
    for index, token in enumerate(argv):
        if token in {"-f", "-l"}:
            out += [token, argv[index + 1]]
    return out


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        pytest.param("3", ["-f", "3", "-l", "3"], id="single_slide"),
        pytest.param("3-", ["-f", "3"], id="open_end"),
        pytest.param("-5", ["-l", "5"], id="open_start"),
        pytest.param("1-5", ["-f", "1", "-l", "5"], id="explicit_from_one"),
        pytest.param("3-5", ["-f", "3", "-l", "5"], id="closed_range"),
        pytest.param("7-7", ["-f", "7", "-l", "7"], id="closed_range_of_one"),
    ],
)
def test_a_selector_becomes_the_pdftoppm_bounds_it_describes(
    selector: str, expected: list[str], tmp_path: Path
) -> None:
    """pdftoppm takes -f and -l separately and either may stand alone."""
    result = _run(tmp_path, "--range", selector)

    assert result.returncode == 0, result.stderr
    assert _bounds(_pdftoppm_argv(tmp_path)) == expected


def test_a_reversed_range_reads_as_the_range_it_describes(tmp_path: Path) -> None:
    """``5-3`` passed straight through rendered nothing and still exited 0.

    merge.py and split.py both swap a reversed range rather than rejecting it.
    """
    result = _run(tmp_path, "--range", "5-3")

    assert result.returncode == 0, result.stderr
    assert _bounds(_pdftoppm_argv(tmp_path)) == ["-f", "3", "-l", "5"]


def test_no_range_passes_no_bounds(tmp_path: Path) -> None:
    """Guard: passes either way, and pins that every slide is still the default."""
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _bounds(_pdftoppm_argv(tmp_path)) == []


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param("3", id="single_slide"),
        pytest.param("3-", id="open_end"),
        pytest.param("-5", id="open_start"),
        pytest.param("3-5", id="closed_range"),
    ],
)
def test_an_accepted_selector_is_not_reported_as_invalid(selector: str, tmp_path: Path) -> None:
    """The issue's headline, asserted without depending on the stubs at all."""
    result = _run(tmp_path, "--range", selector)

    assert "Invalid --range" not in result.stderr


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param("-", id="bare_dash"),
        pytest.param("abc", id="letters"),
        pytest.param("3-5-7", id="three_bounds"),
        pytest.param("3,5", id="comma_list"),
        pytest.param("3 - 5", id="spaces_around_dash"),
        pytest.param("3.5", id="decimal"),
        pytest.param("-3-", id="open_at_both_ends"),
        pytest.param("+3", id="signed"),
        pytest.param("3–5", id="en_dash"),
    ],
)
def test_a_selector_that_means_nothing_is_rejected(selector: str, tmp_path: Path) -> None:
    """The widening must not turn into "accept anything"."""
    result = _run(tmp_path, "--range", selector)

    assert result.returncode == 1
    assert "Invalid --range" in result.stderr
    assert not (tmp_path / "pdftoppm_argv").exists()


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param("0", id="slide_zero"),
        pytest.param("0-5", id="from_zero"),
        pytest.param("-0", id="open_start_at_zero"),
        pytest.param("0-", id="open_end_from_zero"),
    ],
)
def test_slide_numbers_start_at_one(selector: str, tmp_path: Path) -> None:
    """Zero reached pdftoppm and failed there as a conversion error instead."""
    result = _run(tmp_path, "--range", selector)

    assert result.returncode == 1
    assert "slide numbers start at 1" in result.stderr


def test_an_empty_selector_still_means_every_slide(tmp_path: Path) -> None:
    """Guard: ``--range ""`` skipped the block before this change and still does."""
    result = _run(tmp_path, "--range", "")

    assert result.returncode == 0, result.stderr
    assert _bounds(_pdftoppm_argv(tmp_path)) == []


def test_a_bad_selector_is_rejected_before_libreoffice_runs(tmp_path: Path) -> None:
    """The direction the issue did not report.

    The selector used to be parsed *after* the pptx had been converted, so a
    rejected one threw away a full LibreOffice render.
    """
    result = _run(tmp_path, "--range", "bogus")

    assert result.returncode == 1
    assert not (tmp_path / "soffice_ran").exists(), "soffice ran before the selector was checked"


def test_a_good_selector_still_reaches_libreoffice(tmp_path: Path) -> None:
    """Guard for the test above: the marker does appear when the range is fine."""
    result = _run(tmp_path, "--range", "2-4")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "soffice_ran").is_file()


def test_usage_documents_every_accepted_spelling(tmp_path: Path) -> None:
    """``--help`` is the only place a caller learns the selector grammar."""
    bin_dir = _workspace(tmp_path)
    result = subprocess.run(
        [str(_BASH), str(SCRIPT), "--help"],
        cwd=tmp_path,
        env=_env_with_stubs(bin_dir),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    for spelling in ("3", "-5", "3-", "3-5"):
        assert spelling in result.stderr
