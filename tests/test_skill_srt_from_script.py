"""srt-from-script ``build_srt.py`` — a shot missing DURATION_S is rejected,
not silently dropped.

A ``=== SHOT_N ===`` block that matched but had no ``DURATION_S`` line used
to be silently skipped: its voiceover text vanished from the output and its
would-be screen time never advanced the timestamp cursor, so every later
shot's cues started early by exactly that much — while the script still
exited 0. This contradicts SKILL.md's own contract ("drift away from that
format -> zero cues, exit 1"), which promises format drift is fatal, not
partially tolerated.
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


def test_parse_script_rejects_a_shot_missing_duration_s() -> None:
    build_srt = _build_srt_module()
    script = (
        "=== SHOT_1 ===\n"
        "DURATION_S: 5\n"
        "VOICEOVER: First line of narration.\n\n"
        "=== SHOT_2 ===\n"
        "VOICEOVER: This shot is missing its duration field.\n\n"
        "=== SHOT_3 ===\n"
        "DURATION_S: 4\n"
        "VOICEOVER: Third line of narration.\n"
    )
    with pytest.raises(ValueError, match="SHOT_2 has no DURATION_S"):
        build_srt.parse_script(script)


def test_parse_script_accepts_a_well_formed_script() -> None:
    build_srt = _build_srt_module()
    script = (
        "=== SHOT_1 ===\n"
        "DURATION_S: 5\n"
        "VOICEOVER: First line.\n\n"
        "=== SHOT_2 ===\n"
        "DURATION_S: 4\n"
        "VOICEOVER: Second line.\n"
    )
    shots = build_srt.parse_script(script)
    assert shots == [(1, 5, "First line."), (2, 4, "Second line.")]


def test_main_exits_1_with_a_clear_message_on_a_missing_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    build_srt = _build_srt_module()
    script_path = tmp_path / "script.txt"
    script_path.write_text(
        "=== SHOT_1 ===\nVOICEOVER: Missing duration from the start.\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "out.srt"
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_srt.py", "--script", str(script_path), "--output", str(out_path)],
    )

    exit_code = build_srt.main()

    assert exit_code == 1
    assert not out_path.exists()
    captured = capsys.readouterr()
    assert "SHOT_1 has no DURATION_S" in captured.err
