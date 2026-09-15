"""Issue #2070: srt-from-script truncated a fractional DURATION_S.

``_DUR_RE`` matched only ``\\d+`` and the value was read with ``int()``, so
``DURATION_S: 3.5`` became 3. Shot timestamps accumulate, so the error is not
confined to its own cue: every later shot starts early by the running total of
everything truncated before it, and the subtitles slide off the video.

Two ways the same drift got in that the reported regex does not cover:

* ``.5`` and ``+3.5`` match neither ``\\d+`` nor ``\\d+(?:\\.\\d+)?``, and a
  block whose DURATION_S does not match is skipped entirely — so the shot's
  whole screen time leaves the timeline, which is a bigger jump than
  truncating it.
* ``int(duration_s * 1000)`` truncates a boundary that binary float lands just
  under. ``1.001`` is one: ``int(1.001 * 1000)`` is 1000, not 1001, and 187 of
  the 20,000 three-decimal durations up to 20s do the same. Summing per-shot
  milliseconds lets that error march in one direction, so boundaries are
  rounded from the cumulative elapsed time instead.

A shot whose DURATION_S line is missing altogether is a separate defect,
addressed by #2323; the two changes compose.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT / "src" / "agentos" / "skills" / "bundled" / "srt-from-script" / "scripts" / "build_srt.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("srt_build_srt", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_srt_mod = _load()
parse_script = build_srt_mod.parse_script
build_srt = build_srt_mod.build_srt
fmt_ts = build_srt_mod.fmt_ts


def _script(*durations: str) -> str:
    blocks = []
    for index, duration in enumerate(durations, start=1):
        blocks.append(f"=== SHOT_{index} ===\nDURATION_S: {duration}\nVOICEOVER: line {index}\n")
    return "\n".join(blocks)


def _starts(srt: str) -> list[str]:
    return [line.split(" --> ")[0] for line in srt.splitlines() if " --> " in line]


def _ends(srt: str) -> list[str]:
    return [line.split(" --> ")[1] for line in srt.splitlines() if " --> " in line]


# ── the reported bug ────────────────────────────────────────────────────────


def test_a_fractional_duration_is_not_truncated() -> None:
    assert parse_script(_script("3.5")) == [(1, 3.5, "line 1")]


def test_fractional_durations_do_not_drift_across_shots() -> None:
    """The issue's own example. Each shot's start is the exact sum of the
    durations before it; truncation made shot 3 start a full second early."""
    srt = build_srt(parse_script(_script("3.5", "2.5", "4.25")), gap_ms=200)

    assert _starts(srt) == ["00:00:00,000", "00:00:03,500", "00:00:06,000"]
    assert _ends(srt)[-1] == "00:00:10,050"


def test_the_timeline_ends_at_the_true_total() -> None:
    """3.5 + 2.5 + 4.25 = 10.25s. Truncation ended it at 9s."""
    srt = build_srt(parse_script(_script("3.5", "2.5", "4.25")), gap_ms=0)

    assert _ends(srt)[-1] == "00:00:10,250"


# ── every spelling of a decimal ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("3", 3.0),
        ("3.5", 3.5),
        ("0", 0.0),
        ("0.25", 0.25),
        ("03.50", 3.5),
        ("12.125", 12.125),
        (".5", 0.5),
        ("3.", 3.0),
        ("+3.5", 3.5),
        ("3.5   ", 3.5),
    ],
    ids=[
        "int",
        "decimal",
        "zero",
        "sub-second",
        "leading-zero",
        "three-decimals",
        "no-integer-part",
        "trailing-point",
        "explicit-plus",
        "trailing-space",
    ],
)
def test_duration_spellings_that_must_parse(written: str, expected: float) -> None:
    """``.5`` and ``+3.5`` are the two the reported ``\\d+(?:\\.\\d+)?`` misses,
    and missing them drops the shot rather than shortening it."""
    assert parse_script(_script(written)) == [(1, expected, "line 1")]


@pytest.mark.parametrize("written", ["-2", "-0.5", "abc", "", "s3"])
def test_durations_that_must_not_parse(written: str) -> None:
    """A negative duration would run the cursor backwards and emit cues out of
    order. Leaving these unmatched keeps the existing skip rather than
    inventing a meaning for them."""
    assert parse_script(_script(written)) == []


def test_a_decimal_comma_is_read_up_to_the_comma() -> None:
    """Pinned, not endorsed: ``3,5`` is ambiguous (European decimal, or a
    list), and this documents which reading the script takes."""
    assert parse_script(_script("3,5")) == [(1, 3.0, "line 1")]


def test_a_unit_suffix_is_ignored() -> None:
    assert parse_script(_script("3.5s")) == [(1, 3.5, "line 1")]


# ── rounding, and why boundaries come from the cumulative total ─────────────


def test_a_duration_that_binary_float_lands_under_does_not_lose_a_millisecond() -> None:
    """``int(1.001 * 1000)`` is 1000. Truncating each shot that way loses a
    millisecond per shot, always downward."""
    srt = build_srt([(i, 1.001, f"line {i}") for i in range(1, 101)], gap_ms=0)

    assert _ends(srt)[-1] == "00:01:40,100"


def test_a_thousand_tenth_second_shots_land_exactly() -> None:
    """0.1 is not representable in binary, so summing it a thousand times is
    0.9999... per second. Rounding the cumulative total absorbs that; summing
    rounded per-shot values would not."""
    srt = build_srt([(i, 0.1, f"line {i}") for i in range(1, 1001)], gap_ms=0)

    # 999 shots of 0.1s precede the last one: exactly 99.900s, no drift.
    assert _starts(srt)[-1] == "00:01:39,900"
    # Its end is the 800ms readability floor, not the shot's 100ms length.
    assert _ends(srt)[-1] == "00:01:40,700"


def test_no_cue_start_drifts_from_its_true_position() -> None:
    """The invariant behind the fix, asserted directly across an awkward mix."""
    durations = [0.333, 1.667, 2.5, 0.1, 4.05, 3.999, 0.001]
    shots = [(i, d, f"line {i}") for i, d in enumerate(durations, start=1)]

    srt = build_srt(shots, gap_ms=0)

    elapsed = 0.0
    for start, duration in zip(_starts(srt), durations):
        assert start == fmt_ts(round(elapsed * 1000))
        elapsed += duration


# ── behaviour that must not change ──────────────────────────────────────────


def test_integer_scripts_produce_the_same_output_as_before() -> None:
    """The regression pin: whole-second scripts are the common case and their
    output must be byte-identical."""
    srt = build_srt(parse_script(_script("4", "5")), gap_ms=200)

    assert srt == (
        "1\n00:00:00,000 --> 00:00:03,800\nline 1\n\n2\n00:00:04,000 --> 00:00:08,800\nline 2\n"
    )


@pytest.mark.parametrize("total_ms", [0, 1500, 3_661_050, 59_999])
def test_fmt_ts_is_unchanged(total_ms: int) -> None:
    expected = {
        0: "00:00:00,000",
        1500: "00:00:01,500",
        3_661_050: "01:01:01,050",
        59_999: "00:00:59,999",
    }
    assert fmt_ts(total_ms) == expected[total_ms]


def test_a_silent_shot_still_advances_the_cursor_by_its_fraction() -> None:
    """A shot with no voiceover emits no cue but still occupies the timeline.
    Its duration is exactly where a truncated fraction would hide."""
    script = (
        "=== SHOT_1 ===\nDURATION_S: 1.5\nVOICEOVER: first\n\n"
        "=== SHOT_2 ===\nDURATION_S: 2.25\nVOICEOVER: none\n\n"
        "=== SHOT_3 ===\nDURATION_S: 1.5\nVOICEOVER: third\n"
    )

    srt = build_srt(parse_script(script), gap_ms=0)

    assert _starts(srt) == ["00:00:00,000", "00:00:03,750"]


@pytest.mark.parametrize("empty", ["none", "NONE", "-", "--", ""])
def test_empty_voiceover_markers_still_suppress_a_cue(empty: str) -> None:
    script = f"=== SHOT_1 ===\nDURATION_S: 2.5\nVOICEOVER: {empty}\n"

    assert parse_script(script) == [(1, 2.5, "")]


def test_cue_indices_count_cues_not_shots() -> None:
    script = (
        "=== SHOT_1 ===\nDURATION_S: 1.5\nVOICEOVER: none\n\n"
        "=== SHOT_2 ===\nDURATION_S: 1.5\nVOICEOVER: spoken\n"
    )

    srt = build_srt(parse_script(script), gap_ms=0)

    assert srt.splitlines()[0] == "1"


def test_leading_offset_shifts_fractional_cues() -> None:
    srt = build_srt(parse_script(_script("2.5", "1.5")), gap_ms=0, leading_offset_ms=1250)

    assert _starts(srt) == ["00:00:01,250", "00:00:03,750"]


def test_gap_ms_is_subtracted_from_the_shot_end() -> None:
    srt = build_srt(parse_script(_script("3.5")), gap_ms=500)

    assert _ends(srt) == ["00:00:03,000"]


def test_a_sub_second_shot_keeps_the_800ms_readability_floor() -> None:
    """Pinned deliberately. Fractional durations make sub-second shots
    expressible for the first time, and the floor then holds the cue past the
    shot's own end. That is the existing trade — a 300 ms cue would flash by
    unreadably — so it is documented rather than changed here.
    """
    srt = build_srt(parse_script(_script("0.5", "5")), gap_ms=200)

    assert _starts(srt) == ["00:00:00,000", "00:00:00,500"]
    assert _ends(srt)[0] == "00:00:00,800"


# ── end to end ──────────────────────────────────────────────────────────────


def test_main_writes_a_fractional_timeline_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entrypoint SKILL.md declares: script on stdin, path on stdout."""
    out_path = tmp_path / "drama.srt"
    monkeypatch.setattr(sys, "argv", ["build_srt.py", "--output", str(out_path), "--gap-ms", "0"])

    class _Stdin:
        buffer = type(
            "_B", (), {"read": staticmethod(lambda: _script("3.5", "2.5").encode("utf-8"))}
        )()

    monkeypatch.setattr(sys, "stdin", _Stdin())

    assert build_srt_mod.main() == 0
    written = out_path.read_text(encoding="utf-8")
    assert "00:00:03,500 --> 00:00:06,000" in written


def test_main_reads_a_fractional_script_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_path = tmp_path / "script.txt"
    script_path.write_text(_script("1.25", "2.75"), encoding="utf-8")
    out_path = tmp_path / "out.srt"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_srt.py",
            "--script",
            str(script_path),
            "--output",
            str(out_path),
            "--gap-ms",
            "0",
        ],
    )

    assert build_srt_mod.main() == 0
    assert "00:00:01,250 --> 00:00:04,000" in out_path.read_text(encoding="utf-8")
