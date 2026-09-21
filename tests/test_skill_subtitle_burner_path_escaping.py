"""Regression tests for subtitle paths containing single quotes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    ROOT
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "subtitle-burner"
    / "scripts"
)


def _burn_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import burn  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return burn


def _av_get_token(token: str) -> str:
    """Model one ffmpeg av_get_token parsing pass."""
    out: list[str] = []
    quoted = False
    index = 0

    while index < len(token):
        char = token[index]

        if quoted:
            if char == "'":
                quoted = False
            else:
                out.append(char)
            index += 1
            continue

        if char == "'":
            quoted = True
            index += 1
            continue

        if char == "\\" and index + 1 < len(token):
            out.append(token[index + 1])
            index += 2
            continue

        out.append(char)
        index += 1

    return "".join(out)


def _filter_argument_value(escaped: str) -> str:
    """Model the two ffmpeg parsing passes used by the filter argument."""
    first_pass = _av_get_token(f"'{escaped}'")
    return _av_get_token(first_pass)


@pytest.mark.parametrize(
    "name",
    [
        "test's_cues.srt",
        "user's subtitles/cues.srt",
        "two''quotes.srt",
        "'leading.srt",
        "trailing'.srt",
    ],
)
def test_quoted_path_survives_both_parser_passes(name: str) -> None:
    burn = _burn_module()

    escaped = burn._escape_subtitle_path(name)

    assert _filter_argument_value(escaped) == name


@pytest.mark.parametrize(
    "name",
    [
        "test's_cues.srt",
        "two''quotes.srt",
        "trailing'.srt",
    ],
)
def test_single_level_escape_does_not_survive_two_passes(name: str) -> None:
    single_level = name.replace("'", r"'\'")

    assert _filter_argument_value(single_level) != name


def test_quote_uses_two_level_escape() -> None:
    burn = _burn_module()

    escaped = burn._escape_subtitle_path("test's_cues.srt")

    assert escaped == r"test'\\\''s_cues.srt"
    assert escaped != r"test'\''s_cues.srt"
    assert escaped != r"test\'s_cues.srt"


def test_path_without_quotes_is_unchanged() -> None:
    burn = _burn_module()

    assert burn._escape_subtitle_path("plain/cues.srt") == "plain/cues.srt"


def test_windows_path_handling_is_preserved() -> None:
    burn = _burn_module()

    escaped = burn._escape_subtitle_path(r"C:\Videos\Clips\cues.srt")

    assert escaped == r"C\:/Videos/Clips/cues.srt"


def test_windows_path_with_quote_gets_both_escapes() -> None:
    burn = _burn_module()

    escaped = burn._escape_subtitle_path(
        r"C:\Videos\Clips\a'b\cues.srt"
    )

    assert escaped.startswith(r"C\:/Videos/Clips/")
    assert r"'\\\''" in escaped
    assert _filter_argument_value(escaped) == "C:/Videos/Clips/a'b/cues.srt"
