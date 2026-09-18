"""srt-from-script ``build_srt.py`` — fractional ``DURATION_S`` values keep their fraction.

``ai-video-script`` and hand-written shooting scripts routinely give a shot a
duration such as ``3.5``. The parser matched only the integer prefix, so
``3.5`` became ``3`` and every cue after it started half a second early — the
error accumulates across shots, so a 10-shot drama ended with subtitles
several seconds ahead of the picture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "srt-from-script" / "scripts"


def _build_srt_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import build_srt  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return build_srt


def _script(*durations: str) -> str:
    blocks = []
    for index, duration in enumerate(durations, start=1):
        blocks.append(f"=== SHOT_{index} ===\nDURATION_S: {duration}\nVOICEOVER: line {index}\n")
    return "\n".join(blocks)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3.5", 3.5),
        ("2.25", 2.25),
        ("0.8", 0.8),
        ("4", 4.0),
        ("4.0", 4.0),
        ("  3.5  ", 3.5),
    ],
)
def test_parse_script_keeps_fractional_duration(raw: str, expected: float) -> None:
    mod = _build_srt_module()

    shots = mod.parse_script(_script(raw))

    assert shots == [(1, expected, "line 1")]


def test_fractional_durations_accumulate_without_drift() -> None:
    """Three 3.5 s shots: the cursor must land on 3500 / 7000 / 10500 ms."""

    mod = _build_srt_module()

    srt = mod.build_srt(mod.parse_script(_script("3.5", "3.5", "3.5")), gap_ms=0)

    assert srt == (
        "1\n00:00:00,000 --> 00:00:03,500\nline 1\n\n"
        "2\n00:00:03,500 --> 00:00:07,000\nline 2\n\n"
        "3\n00:00:07,000 --> 00:00:10,500\nline 3\n"
    )


def test_sub_second_duration_rounds_to_whole_milliseconds() -> None:
    """``1.1 * 1000`` is ``1100.0000000000002`` in binary float; the cue must
    still land on 1100 ms rather than a truncated neighbour."""

    mod = _build_srt_module()

    srt = mod.build_srt(mod.parse_script(_script("1.1", "2.675")), gap_ms=0)

    assert "00:00:00,000 --> 00:00:01,100" in srt
    assert "00:00:01,100 --> 00:00:03,775" in srt


def test_integer_durations_are_unchanged() -> None:
    mod = _build_srt_module()

    srt = mod.build_srt(mod.parse_script(_script("3", "4")), gap_ms=200)

    assert srt == (
        "1\n00:00:00,000 --> 00:00:02,800\nline 1\n\n2\n00:00:03,000 --> 00:00:06,800\nline 2\n"
    )


def test_silent_shot_with_fractional_duration_still_advances_cursor() -> None:
    mod = _build_srt_module()
    text = (
        "=== SHOT_1 ===\nDURATION_S: 2.5\nVOICEOVER: none\n\n"
        "=== SHOT_2 ===\nDURATION_S: 3\nVOICEOVER: hello\n"
    )

    srt = mod.build_srt(mod.parse_script(text), gap_ms=0)

    assert srt == "1\n00:00:02,500 --> 00:00:05,500\nhello\n"


def test_cli_writes_fractional_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _build_srt_module()
    script_path = tmp_path / "script.txt"
    script_path.write_text(_script("3.5", "2.5"), encoding="utf-8")
    out_path = tmp_path / "out.srt"
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_srt.py", "--script", str(script_path), "--output", str(out_path), "--gap-ms", "0"],
    )

    assert mod.main() == 0

    assert out_path.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:03,500\nline 1\n\n2\n00:00:03,500 --> 00:00:06,000\nline 2\n"
    )


def test_sub_second_shots_do_not_bleed_into_subsequent_shot_cues() -> None:
    """Two 0.5s shots: cue 1 must not bleed past 500ms into shot 2.

    An unconstrained start+800ms minimum display forced cue 1 to end at 800ms,
    overlapping with shot 2 (which starts at 500ms). The cue end time must be
    capped at the shot boundary (500ms).
    """
    mod = _build_srt_module()

    srt = mod.build_srt(mod.parse_script(_script("0.5", "0.5")), gap_ms=200)

    assert srt == (
        "1\n00:00:00,000 --> 00:00:00,500\nline 1\n\n2\n00:00:00,500 --> 00:00:01,000\nline 2\n"
    )


@pytest.mark.parametrize(
    ("durations", "gap_ms", "expected_end_1"),
    [
        (("0.3", "1.0"), 200, "00:00:00,300"),
        (("0.6", "1.0"), 200, "00:00:00,600"),
        (("0.9", "1.0"), 200, "00:00:00,800"),  # 900ms - 200ms = 700ms, boosted to 800ms <= 900ms
        (("1.2", "1.0"), 200, "00:00:01,000"),  # 1200ms - 200ms = 1000ms
        (("0.5", "1.0"), 0, "00:00:00,500"),
    ],
)
def test_cue_end_time_never_exceeds_shot_boundary(
    durations: tuple[str, ...],
    gap_ms: int,
    expected_end_1: str,
) -> None:
    mod = _build_srt_module()

    srt = mod.build_srt(mod.parse_script(_script(*durations)), gap_ms=gap_ms)

    assert f"00:00:00,000 --> {expected_end_1}" in srt


def test_cli_sub_second_shots_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _build_srt_module()
    script_path = tmp_path / "script.txt"
    script_path.write_text(_script("0.5", "0.5"), encoding="utf-8")
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
            "200",
        ],
    )

    assert mod.main() == 0

    assert out_path.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:00,500\nline 1\n\n2\n00:00:00,500 --> 00:00:01,000\nline 2\n"
    )
