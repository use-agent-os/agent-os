"""Unit tests for srt-from-script build_srt script."""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Dynamically import build_srt from bundled skill script path
_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "srt-from-script"
    / "scripts"
    / "build_srt.py"
)

_spec = importlib.util.spec_from_file_location("build_srt_mod", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
build_srt_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_srt_mod)

parse_script = build_srt_mod.parse_script
build_srt = build_srt_mod.build_srt
fmt_ts = build_srt_mod.fmt_ts


def test_fmt_ts() -> None:
    assert fmt_ts(0) == "00:00:00,000"
    assert fmt_ts(1500) == "00:00:01,500"
    assert fmt_ts(3661050) == "01:01:01,050"


def test_parse_script_integer_durations() -> None:
    text = """
=== SHOT_1 ===
DURATION_S: 4
VOICEOVER: Hello world
=== SHOT_2 ===
DURATION_S: 5
VOICEOVER: Second shot
"""
    shots = parse_script(text)
    assert len(shots) == 2
    assert shots[0] == (1, 4.0, "Hello world")
    assert shots[1] == (2, 5.0, "Second shot")


def test_parse_script_float_durations() -> None:
    text = """
=== SHOT_1 ===
DURATION_S: 3.5
VOICEOVER: First float shot
=== SHOT_2 ===
DURATION_S: 4.2
VOICEOVER: Second float shot
"""
    shots = parse_script(text)
    assert len(shots) == 2
    assert shots[0] == (1, 3.5, "First float shot")
    assert shots[1] == (2, 4.2, "Second float shot")


def test_build_srt_float_timestamps() -> None:
    shots = [
        (1, 3.5, "First float shot"),
        (2, 4.0, "Second float shot"),
    ]
    srt = build_srt(shots, gap_ms=200, leading_offset_ms=0)
    lines = [line for line in srt.splitlines() if line.strip()]

    # First cue: 0ms to 3300ms (3500ms - 200ms gap)
    assert "1" in lines[0]
    assert "00:00:00,000 --> 00:00:03,300" in lines[1]
    assert lines[2] == "First float shot"

    # Second cue: starts at 3500ms (3.5s) to 7300ms (3.5s + 4.0s - 0.2s)
    assert "2" in lines[3]
    assert "00:00:03,500 --> 00:00:07,300" in lines[4]
    assert lines[5] == "Second float shot"
