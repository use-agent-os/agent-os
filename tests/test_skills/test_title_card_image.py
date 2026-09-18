"""Tests for bundled title-card-image script and line wrapping."""

import subprocess
import sys
from pathlib import Path

import pytest

# Import the render module from bundled skill
SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "title-card-image"
    / "scripts"
    / "render.py"
)


@pytest.fixture
def render_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("title_card_render", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wrap_text_ascii_words(render_module):
    wrap = render_module._wrap_text
    text = "The quick brown fox jumps over the lazy dog"
    lines = wrap(text, max_chars=15)
    assert lines == ["The quick brown", "fox jumps over", "the lazy dog"]


def test_wrap_text_japanese_hiragana(render_module):
    wrap = render_module._wrap_text
    # 20 hiragana characters (no spaces, codepoints in 0x3040..0x309F)
    text = "あいうえおかきくけこさしすせそたちつてと"
    lines = wrap(text, max_chars=10)
    assert lines == ["あいうえおかきくけこ", "さしすせそたちつてと"]


def test_wrap_text_japanese_katakana(render_module):
    wrap = render_module._wrap_text
    # Katakana (codepoints in 0x30A0..0x30FF)
    text = "アイウエオカキクケコサシスセソタチツテト"
    lines = wrap(text, max_chars=10)
    assert lines == ["アイウエオカキクケコ", "サシスセソタチツテト"]


def test_wrap_text_cjk_ideographs(render_module):
    wrap = render_module._wrap_text
    # Chinese / Kanji (16 chars total -> 8 + 8)
    text = "第一回東京国際映画祭開催記念公演"
    # "第一回東京国際映" is 8 chars, "画祭開催記念公演" is 8 chars
    lines = wrap(text, max_chars=8)
    assert lines == ["第一回東京国際映", "画祭開催記念公演"]


def test_wrap_text_multiline_with_long_lines(render_module):
    wrap = render_module._wrap_text
    text = "Line 1 is short\nLine 2 is somewhat longer and needs wrapping\nLine 3"
    lines = wrap(text, max_chars=20)
    assert lines == [
        "Line 1 is short",
        "Line 2 is somewhat",
        "longer and needs",
        "wrapping",
        "Line 3",
    ]


def test_wrap_text_empty(render_module):
    wrap = render_module._wrap_text
    assert wrap("", max_chars=10) == [""]


def test_render_cli_smoke(tmp_path):
    out_file = tmp_path / "card.png"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--text",
            "あいうえおかきくけこさしすせそたちつてと",
            "--subtitle",
            "日本語サブタイトル",
            "--output",
            str(out_file),
            "--width",
            "720",
            "--height",
            "1280",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"CLI stderr: {result.stderr}"
    assert out_file.exists()
    assert out_file.stat().st_size > 0
