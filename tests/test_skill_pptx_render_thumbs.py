"""pptx render_thumbs.sh — what a re-render prints, and under which names.

The visual-QA loop in the pptx ``SKILL.md`` renders, fixes, and renders again.
pdftoppm pads the page number to the width of the page count, so a 9-slide
deck came out as ``deck-1.jpg`` while the documented name is ``deck-01.jpg``,
and the closing ``ls <basename>-*.jpg`` printed whatever an earlier render had
left in the directory. The second pass then listed the previous deck's slides
beside the new ones.

Neither LibreOffice nor poppler is installed on CI, so both are stubbed onto
PATH. The pdftoppm stub follows the real naming rule (pad to the page count's
width, honour ``-f``/``-l``) and writes the render generation into each image,
so a test can tell a fresh page from a leftover. Stubs work in the current
directory rather than on absolute paths, which keeps them usable under Git Bash
on the Windows job; the tests are guarded on bash being present, not on the
platform, so they run on both jobs.
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

# The "deck" is a text file: line 1 is the slide count, line 2 a generation tag.
# soffice copies it to <basename>.pdf, as the real conversion would produce a PDF.
_SOFFICE_STUB = """#!/bin/sh
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
cp "$input" "$outdir/$base.pdf"
"""

# Mirrors pdftoppm's naming: <prefix>-<page>.jpg, the page zero-padded to the
# number of digits in the document's page count, only pages -f..-l.
_PDFTOPPM_STUB = """#!/bin/sh
first=""
last=""
while [ $# -gt 2 ]; do
  case "$1" in
    -f) first="$2"; shift 2 ;;
    -l) last="$2"; shift 2 ;;
    -r) shift 2 ;;
    *) shift ;;
  esac
done
pdf="$1"
prefix="$2"
[ "${PDFTOPPM_FAIL:-}" = "1" ] && exit 99
total=$(sed -n 1p "$pdf")
tag=$(sed -n 2p "$pdf")
width=${#total}
page=${first:-1}
last=${last:-$total}
while [ "$page" -le "$last" ]; do
  num="$page"
  while [ ${#num} -lt "$width" ]; do num="0$num"; done
  printf '%s page %s\\n' "$tag" "$page" > "$prefix-$num.jpg"
  page=$((page + 1))
done
"""


def _tools(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    if not bin_dir.is_dir():
        bin_dir.mkdir()
        (bin_dir / "soffice").write_text(_SOFFICE_STUB, encoding="utf-8", newline="\n")
        (bin_dir / "pdftoppm").write_text(_PDFTOPPM_STUB, encoding="utf-8", newline="\n")
        for stub in bin_dir.iterdir():
            stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


def _render(
    tmp_path: Path, slides: int, tag: str, *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Write a ``slides``-page deck tagged ``tag``, then run the script on it.

    The deck is named relatively on purpose: an absolute Windows path would
    reach bash with backslashes in it.
    """
    (tmp_path / "deck.pptx").write_text(f"{slides}\n{tag}\n", encoding="utf-8", newline="\n")
    env = _tools(tmp_path)
    env.update(env_extra or {})
    return subprocess.run(
        [str(_BASH), str(SCRIPT), "deck.pptx", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def _printed_images(result: subprocess.CompletedProcess[str]) -> list[str]:
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    return [Path(line).name for line in lines if line.endswith(".jpg")]


def _names(count: int, width: int = 2, first: int = 1) -> list[str]:
    return [f"deck-{n:0{width}d}.jpg" for n in range(first, first + count)]


def test_a_rerender_after_the_deck_shrank_prints_only_the_new_slides(tmp_path: Path) -> None:
    """10 slides, then 9: the second pass printed 19 images, the old ones first."""
    _render(tmp_path, 10, "draft")
    result = _render(tmp_path, 9, "fixed")

    printed = _printed_images(result)
    assert printed == _names(9)
    for name in printed:
        assert (tmp_path / name).read_text(encoding="utf-8").startswith("fixed ")


def test_a_short_deck_gets_the_documented_two_digit_names(tmp_path: Path) -> None:
    """SKILL.md promises out-01.jpg, out-02.jpg; pdftoppm alone writes deck-1.jpg."""
    printed = _printed_images(_render(tmp_path, 3, "only"))

    assert printed == _names(3)
    assert sorted(p.name for p in tmp_path.glob("deck-*.jpg")) == _names(3)


def test_the_documented_first_slide_is_the_fresh_render_after_crossing_ten(
    tmp_path: Path,
) -> None:
    """``deck-01.jpg`` is the file SKILL.md names; it must be the current deck's slide."""
    _render(tmp_path, 10, "draft")
    _render(tmp_path, 9, "fixed")

    assert (tmp_path / "deck-01.jpg").read_text(encoding="utf-8") == "fixed page 1\n"


def test_a_slide_that_no_longer_exists_is_not_printed(tmp_path: Path) -> None:
    """11 slides, then 10: same padding, but deck-11.jpg was still listed."""
    _render(tmp_path, 11, "draft")
    printed = _printed_images(_render(tmp_path, 10, "fixed"))

    assert printed == _names(10)


def test_a_range_render_prints_only_the_slides_in_the_range(tmp_path: Path) -> None:
    """``--range 3-5`` after a full render listed every earlier thumbnail too."""
    _render(tmp_path, 10, "draft")
    printed = _printed_images(_render(tmp_path, 10, "fixed", "--range", "3-5"))

    assert printed == _names(3, first=3)
    assert (tmp_path / "deck-04.jpg").read_text(encoding="utf-8") == "fixed page 4\n"


def test_a_hundred_slide_deck_keeps_three_digit_names(tmp_path: Path) -> None:
    """The two-digit floor must not shorten pdftoppm's wider padding (lexical order)."""
    printed = _printed_images(_render(tmp_path, 100, "big"))

    assert printed == _names(100, width=3)
    assert printed == sorted(printed)


def test_out_dir_is_honoured_and_left_without_scratch_files(tmp_path: Path) -> None:
    printed = _render(tmp_path, 2, "only", "--out-dir", "thumbs")

    assert _printed_images(printed) == _names(2)
    assert printed.stdout.splitlines()[0] == "thumbs/deck.pdf"
    assert sorted(p.name for p in (tmp_path / "thumbs").iterdir()) == [
        "deck-01.jpg",
        "deck-02.jpg",
        "deck.pdf",
    ]


def test_files_the_script_did_not_render_are_left_alone(tmp_path: Path) -> None:
    """The fix narrows what is printed; it deletes nothing in the output directory."""
    (tmp_path / "deck-notes.jpg").write_text("mine\n", encoding="utf-8")
    _render(tmp_path, 10, "draft")
    printed = _printed_images(_render(tmp_path, 9, "fixed"))

    assert "deck-notes.jpg" not in printed
    assert (tmp_path / "deck-notes.jpg").read_text(encoding="utf-8") == "mine\n"
    assert (tmp_path / "deck-10.jpg").read_text(encoding="utf-8") == "draft page 10\n"


def test_a_pdftoppm_failure_still_exits_4_and_cleans_up(tmp_path: Path) -> None:
    result = _render(tmp_path, 3, "only", env_extra={"PDFTOPPM_FAIL": "1"})

    assert result.returncode == 4
    assert "pdftoppm failed" in result.stderr
    assert not list(tmp_path.glob(".render_thumbs.*"))
