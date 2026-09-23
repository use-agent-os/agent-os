"""``render_thumbs.sh`` validates ``--dpi`` before running ``soffice``.

``--dpi`` was handed straight to ``pdftoppm -r`` with no check. A negative value did
not error at all: ``pdftoppm`` silently produced a 1x1 pixel JPEG while the script
printed the file path and exited 0, as if the render had succeeded. A non-numeric
value did error, but only after a full LibreOffice ``--convert-to pdf`` pass had
already run, and under the wrong exit code -- 4 ("conversion failed") instead of the
script's own "bad arguments" code, 1 (see the exit-code table in the script's header
comment). ``--range`` already validates this way; ``--dpi`` had no equivalent guard.

``soffice`` and ``pdftoppm`` are stubbed as exported shell functions so these tests
run anywhere bash does, without LibreOffice or Poppler installed (CI has neither),
and can assert the guard ran before ``soffice`` was ever invoked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "pptx"
    / "scripts"
    / "render_thumbs.sh"
)

REJECTED = (
    "-5",  # negative: reached pdftoppm as a valid-looking flag value before the fix
    "0",  # not a usable resolution
    "0.0",
    ".0",
    "00",
    "abc",  # not a number at all
    "",  # `--dpi` with an empty value
    "  ",
    "1,5",  # decimal comma
    "1 5",
    "1e5",  # exponent: not the plain fp format documented (150, 300)
    "0x1",
    "150dpi",  # trailing unit
)

ACCEPTED = ("150", "72", "1", "300.5", "96")

# Stubs `soffice` and `pdftoppm` as shell functions, each counting its own
# invocations, and fakes a successful conversion so the script's own
# `[[ -f "$pdf_path" ]]` check after `soffice` still passes for the accepted cases.
_WRAPPER = """\
soffice() {
  echo x >> "$SOFFICE_CALLS"
  local outdir="" input=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --outdir) outdir="$2"; shift 2 ;;
      --convert-to) shift 2 ;;
      --headless|--norestore|--nologo|--nofirststartwizard) shift ;;
      *) input="$1"; shift ;;
    esac
  done
  touch "$outdir/$(basename "$input" .pptx).pdf"
}
export -f soffice

pdftoppm() {
  echo x >> "$PDFTOPPM_CALLS"
  return 0
}
export -f pdftoppm

exec bash "$SCRIPT" "$@"
"""


def _no_usable_bash() -> bool:
    """Whether the shell cases can run here.

    The script is bash and the skill declares ``"os": ["darwin", "linux"]``; on
    the Windows runner ``bash`` is the WSL stub, which exits non-zero without
    running anything. The source-level test at the bottom runs everywhere.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        probe = subprocess.run(
            ["bash", "-c", "printf ok"], capture_output=True, text=True, check=False
        )
    except OSError:
        return True
    return probe.returncode != 0 or probe.stdout.strip() != "ok"


@pytest.fixture
def run(tmp_path: Path):
    """Run the script against stubbed soffice/pdftoppm; returns the result and call counts."""
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(_WRAPPER, encoding="utf-8")
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"not a real pptx, never opened by the stub")
    out_dir = tmp_path / "out"
    soffice_calls = tmp_path / "soffice_calls.txt"
    pdftoppm_calls = tmp_path / "pdftoppm_calls.txt"

    def _run(*extra_args: str):
        soffice_calls.write_text("", encoding="utf-8")
        pdftoppm_calls.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "SCRIPT": str(SCRIPT),
            "SOFFICE_CALLS": str(soffice_calls),
            "PDFTOPPM_CALLS": str(pdftoppm_calls),
        }
        result = subprocess.run(
            ["bash", str(wrapper), str(deck), "--out-dir", str(out_dir), *extra_args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        soffice_n = len([ln for ln in soffice_calls.read_text(encoding="utf-8").splitlines() if ln])
        pdftoppm_n = len(
            [ln for ln in pdftoppm_calls.read_text(encoding="utf-8").splitlines() if ln]
        )
        return result, soffice_n, pdftoppm_n

    return _run


@pytest.mark.parametrize("dpi", REJECTED)
def test_a_bad_dpi_is_refused_before_soffice_ever_runs(run, dpi: str) -> None:
    if _no_usable_bash():
        return

    result, soffice_n, pdftoppm_n = run("--dpi", dpi)

    assert result.returncode == 1
    assert "Invalid --dpi" in result.stderr
    assert soffice_n == 0, "the script ran soffice before validating --dpi"
    assert pdftoppm_n == 0


@pytest.mark.parametrize("dpi", ACCEPTED)
def test_a_positive_dpi_is_accepted(run, dpi: str) -> None:
    if _no_usable_bash():
        return

    result, soffice_n, pdftoppm_n = run("--dpi", dpi)

    assert result.returncode == 0, result.stderr
    assert "Invalid --dpi" not in result.stderr
    assert soffice_n == 1
    assert pdftoppm_n == 1


def test_the_default_dpi_still_works_with_no_flag_at_all(run) -> None:
    """No ``--dpi`` at all: the documented 150 default must survive the guard."""
    if _no_usable_bash():
        return

    result, soffice_n, pdftoppm_n = run()

    assert result.returncode == 0, result.stderr
    assert soffice_n == 1
    assert pdftoppm_n == 1


def test_a_missing_file_still_fails_with_its_own_message_not_a_dpi_complaint(
    tmp_path: Path,
) -> None:
    """The guard must not shadow the existing file-not-found check."""
    if _no_usable_bash():
        return

    missing = tmp_path / "nope.pptx"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(missing), "--dpi", "150"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "File not found" in result.stderr
    assert "Invalid --dpi" not in result.stderr


def test_the_range_guard_is_still_checked_and_still_runs_after_soffice(run) -> None:
    """Guard: this PR must not change --range's existing (already-correct) behaviour."""
    if _no_usable_bash():
        return

    result, soffice_n, _pdftoppm_n = run("--range", "bogus")

    assert result.returncode == 1
    assert "Invalid --range" in result.stderr
    assert soffice_n == 1, "the pre-existing --range guard still runs after soffice"


def test_the_dpi_guard_is_checked_before_the_file_existence_check() -> None:
    """Runs on every platform, including the one that cannot execute the script.

    Pins the placement the other tests rely on: the guard has to sit with the
    argument-parsing block, above the ``soffice``/``pdftoppm`` calls, or a bad
    value is only caught after real work has already happened.
    """
    text = SCRIPT.read_text(encoding="utf-8")

    guard = text.index("Invalid --dpi")
    assert guard < text.index("File not found"), "the guard must precede the file check"
    assert guard < text.index("--convert-to pdf"), "the guard must precede the soffice call"
