"""Issue #2322: a shot with no usable DURATION_S was dropped, not reported.

``parse_script`` hit ``if not dur_m: continue``, so a ``=== SHOT_N ===`` block
whose ``DURATION_S`` was missing vanished from the shot list. Two things went
wrong at once, and neither left a trace:

* its VOICEOVER text was lost entirely -- never written, never reported;
* its screen time never advanced the timestamp cursor, so every later cue
  started early by exactly that much, silently misaligning the whole tail of
  the file against a video that *does* contain the shot.

``main()`` only produced the documented "zero cues, exit 1" when the shot list
came back completely empty, so one bad block among good ones still exited 0.
SKILL.md already promises the opposite: "Drift away from that format -> zero
cues, exit 1."

The block is now rejected, and the message distinguishes a field that is
absent from one that is present but unusable -- telling someone their block
"has no DURATION_S field" when a ``DURATION_S: none`` line is sitting right
there sends them looking in the wrong place.

Scope note: which *values* parse is deliberately unchanged here. ``5.5``
still truncates to ``5``; that is issue #2070, and changing it in this PR
would collide with the fix for it.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "srt-from-script"
    / "scripts"
)


def _load() -> ModuleType:
    """Load build_srt.py by path, without leaving it on sys.path."""
    spec = importlib.util.spec_from_file_location("build_srt_under_test", SCRIPTS / "build_srt.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def build_srt() -> ModuleType:
    return _load()


ISSUE_SCRIPT = (
    "=== SHOT_1 ===\n"
    "DURATION_S: 5\n"
    "VOICEOVER: First line of narration.\n\n"
    "=== SHOT_2 ===\n"
    "VOICEOVER: This shot is missing its duration field.\n\n"
    "=== SHOT_3 ===\n"
    "DURATION_S: 4\n"
    "VOICEOVER: Third line of narration.\n"
)

GOOD_SCRIPT = (
    "=== SHOT_1 ===\n"
    "DURATION_S: 5\n"
    "VOICEOVER: First line.\n\n"
    "=== SHOT_2 ===\n"
    "DURATION_S: 4\n"
    "VOICEOVER: Second line.\n"
)


# --------------------------------------------------------------------------
# The reported defect.
# --------------------------------------------------------------------------


def test_the_issues_own_script_is_rejected(build_srt: ModuleType) -> None:
    with pytest.raises(build_srt.ScriptFormatError, match="SHOT_2"):
        build_srt.parse_script(ISSUE_SCRIPT)


def test_the_lost_voiceover_is_not_silently_dropped(build_srt: ModuleType) -> None:
    """The old behaviour returned shots 1 and 3 and lost shot 2 entirely."""
    try:
        shots = build_srt.parse_script(ISSUE_SCRIPT)
    except build_srt.ScriptFormatError:
        return
    pytest.fail(f"malformed script parsed anyway: {shots}")


def test_a_single_bad_shot_among_good_ones_is_fatal(build_srt: ModuleType) -> None:
    """``main()`` used to error only when *every* shot was bad."""
    script = "=== SHOT_1 ===\nDURATION_S: 5\nVOICEOVER: a\n=== SHOT_2 ===\nVOICEOVER: b\n"

    with pytest.raises(build_srt.ScriptFormatError):
        build_srt.parse_script(script)


def test_the_error_is_a_value_error(build_srt: ModuleType) -> None:
    """Subclassing keeps any caller that catches ValueError working."""
    assert issubclass(build_srt.ScriptFormatError, ValueError)

    with pytest.raises(ValueError):
        build_srt.parse_script(ISSUE_SCRIPT)


# --------------------------------------------------------------------------
# An absent field and an unusable value are different problems.
# --------------------------------------------------------------------------


ABSENT_CASES = {
    "no DURATION_S line": "=== SHOT_7 ===\nVOICEOVER: hi\n",
    "empty shot block": "=== SHOT_7 ===\n",
    "only a voiceover and a stray field": "=== SHOT_7 ===\nMOOD: tense\nVOICEOVER: hi\n",
}


@pytest.mark.parametrize("script", ABSENT_CASES.values(), ids=list(ABSENT_CASES))
def test_an_absent_field_is_reported_as_missing(build_srt: ModuleType, script: str) -> None:
    with pytest.raises(build_srt.ScriptFormatError, match="SHOT_7 is missing its DURATION_S field"):
        build_srt.parse_script(script)


UNUSABLE_VALUES = ["none", "N/A", "abc", "-5", "+5", "five", "TBD", "?"]


@pytest.mark.parametrize("value", UNUSABLE_VALUES)
def test_a_present_but_unusable_value_says_so_and_quotes_it(
    build_srt: ModuleType, value: str
) -> None:
    """Reporting "has no DURATION_S field" here would be false -- the line is
    right there, holding a value that cannot be used."""
    script = f"=== SHOT_7 ===\nDURATION_S: {value}\nVOICEOVER: hi\n"

    with pytest.raises(build_srt.ScriptFormatError) as caught:
        build_srt.parse_script(script)

    message = str(caught.value)
    assert repr(value) in message
    assert "not a whole number of seconds" in message
    assert "missing" not in message


def test_an_empty_value_is_called_empty_not_missing(build_srt: ModuleType) -> None:
    script = "=== SHOT_7 ===\nDURATION_S:\nVOICEOVER: hi\n"

    with pytest.raises(build_srt.ScriptFormatError, match="SHOT_7 has an empty DURATION_S field"):
        build_srt.parse_script(script)


def test_a_whitespace_only_value_is_called_empty(build_srt: ModuleType) -> None:
    script = "=== SHOT_7 ===\nDURATION_S:    \nVOICEOVER: hi\n"

    with pytest.raises(build_srt.ScriptFormatError, match="empty DURATION_S field"):
        build_srt.parse_script(script)


def test_every_bad_shot_is_reported_in_one_run(build_srt: ModuleType) -> None:
    """Three malformed shots should take one run to diagnose, not three."""
    script = (
        "=== SHOT_1 ===\nVOICEOVER: a\n"
        "=== SHOT_2 ===\nDURATION_S: abc\nVOICEOVER: b\n"
        "=== SHOT_3 ===\nDURATION_S:\nVOICEOVER: c\n"
    )

    with pytest.raises(build_srt.ScriptFormatError) as caught:
        build_srt.parse_script(script)

    message = str(caught.value)
    assert "SHOT_1" in message
    assert "SHOT_2" in message
    assert "SHOT_3" in message


def test_the_message_is_ascii_only(build_srt: ModuleType) -> None:
    """This script already decodes stdin by hand because it meets Windows
    console code pages; its error text should survive one too."""
    with pytest.raises(build_srt.ScriptFormatError) as caught:
        build_srt.parse_script(ISSUE_SCRIPT)

    str(caught.value).encode("ascii")


# --------------------------------------------------------------------------
# Well-formed scripts must be completely unaffected.
# --------------------------------------------------------------------------


def test_a_well_formed_script_still_parses(build_srt: ModuleType) -> None:
    assert build_srt.parse_script(GOOD_SCRIPT) == [(1, 5, "First line."), (2, 4, "Second line.")]


@pytest.mark.parametrize("value", ["0", "5", "05", "  7  ", "120"])
def test_usable_values_still_parse(build_srt: ModuleType, value: str) -> None:
    script = f"=== SHOT_1 ===\nDURATION_S: {value}\nVOICEOVER: hi\n"

    assert build_srt.parse_script(script) == [(1, int(value.strip()), "hi")]


@pytest.mark.parametrize("value", ["none", "None", "-", "--", ""])
def test_voiceover_normalisation_is_unchanged(build_srt: ModuleType, value: str) -> None:
    """A blank voiceover is documented behaviour: no cue, but the duration
    still advances the cursor. It must not start erroring."""
    script = f"=== SHOT_1 ===\nDURATION_S: 5\nVOICEOVER: {value}\n"

    assert build_srt.parse_script(script) == [(1, 5, "")]


def test_a_shot_with_no_voiceover_line_is_still_accepted(build_srt: ModuleType) -> None:
    """Only DURATION_S is required; VOICEOVER may be absent."""
    assert build_srt.parse_script("=== SHOT_1 ===\nDURATION_S: 5\n") == [(1, 5, "")]


def test_a_blank_shot_still_advances_the_timeline(build_srt: ModuleType) -> None:
    script = (
        "=== SHOT_1 ===\nDURATION_S: 5\nVOICEOVER: none\n"
        "=== SHOT_2 ===\nDURATION_S: 4\nVOICEOVER: after the silence\n"
    )

    srt = build_srt.build_srt(build_srt.parse_script(script), gap_ms=200)

    assert "00:00:05,000 --> 00:00:08,800" in srt


def test_the_timeline_of_a_good_script_is_unchanged(build_srt: ModuleType) -> None:
    """The regression guard for the cursor arithmetic this issue is about."""
    srt = build_srt.build_srt(build_srt.parse_script(GOOD_SCRIPT), gap_ms=200)

    assert srt == (
        "1\n"
        "00:00:00,000 --> 00:00:04,800\n"
        "First line.\n"
        "\n"
        "2\n"
        "00:00:05,000 --> 00:00:08,800\n"
        "Second line.\n"
    )


def test_a_float_duration_still_truncates(build_srt: ModuleType) -> None:
    """Pinning the boundary: which values parse is issue #2070's subject, and
    this change deliberately leaves it alone."""
    assert build_srt.parse_script("=== SHOT_1 ===\nDURATION_S: 5.5\nVOICEOVER: hi\n") == [
        (1, 5, "hi")
    ]


# --------------------------------------------------------------------------
# main(): exit 1, a message, and nothing written.
# --------------------------------------------------------------------------


def _run(
    build_srt: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    script: str,
    *,
    via_stdin: bool = False,
) -> tuple[int, Path]:
    out_path = tmp_path / "out.srt"
    argv = ["build_srt.py", "--output", str(out_path)]
    if via_stdin:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(script.encode("utf-8"))))
    else:
        script_path = tmp_path / "script.txt"
        script_path.write_text(script, encoding="utf-8")
        argv += ["--script", str(script_path)]
    monkeypatch.setattr(sys, "argv", argv)
    return build_srt.main(), out_path


@pytest.mark.parametrize("via_stdin", [False, True], ids=["--script", "stdin"])
def test_main_exits_1_and_writes_nothing(
    build_srt: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    via_stdin: bool,
) -> None:
    code, out_path = _run(build_srt, monkeypatch, tmp_path, ISSUE_SCRIPT, via_stdin=via_stdin)

    assert code == 1
    assert not out_path.exists(), "a partial SRT was written for a rejected script"
    assert "SHOT_2" in capsys.readouterr().err


def test_main_names_the_problem_on_stderr_not_stdout(
    build_srt: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stdout carries the output path and is consumed by the orchestrator."""
    _run(build_srt, monkeypatch, tmp_path, ISSUE_SCRIPT)

    captured = capsys.readouterr()
    assert "malformed script" in captured.err
    assert captured.out == ""


def test_main_still_succeeds_on_a_good_script(
    build_srt: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out_path = _run(build_srt, monkeypatch, tmp_path, GOOD_SCRIPT)

    assert code == 0
    assert out_path.exists()
    assert "First line." in out_path.read_text(encoding="utf-8")
    assert capsys.readouterr().out.strip() == str(out_path)


def test_main_still_reports_a_script_with_no_shot_blocks(
    build_srt: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The pre-existing empty-list path must not be shadowed by the new one."""
    code, out_path = _run(build_srt, monkeypatch, tmp_path, "OVERVIEW: no shots here\n")

    assert code == 1
    assert not out_path.exists()
    assert "no SHOT_N blocks found" in capsys.readouterr().err


def test_main_still_reports_empty_input(
    build_srt: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out_path = _run(build_srt, monkeypatch, tmp_path, "   \n")

    assert code == 1
    assert not out_path.exists()
    assert "empty script input" in capsys.readouterr().err


def test_main_does_not_create_the_output_directory_for_a_bad_script(
    build_srt: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``main`` mkdirs the parent before writing; a rejected script must not
    leave an empty directory behind."""
    out_path = tmp_path / "nested" / "deep" / "out.srt"
    script_path = tmp_path / "script.txt"
    script_path.write_text(ISSUE_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_srt.py", "--script", str(script_path), "--output", str(out_path)],
    )

    assert build_srt.main() == 1
    assert not out_path.parent.exists()
