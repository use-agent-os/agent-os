"""Issue #2265: title-card-image render.py text wrapping with non-CJK scripts.

Scripts using spaces (Hangul, Cyrillic, Greek, Latin, emoji) must wrap on
whitespace boundaries rather than being sliced at fixed character counts by
the CJK ideograph chunker.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "title-card-image"
    / "scripts"
    / "render.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("title_card_render_under_test", _SRC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_is_cjk_ideograph_distinguishes_scripts() -> None:
    mod = _load_module()
    # True for CJK ideographs
    assert mod._is_cjk_ideograph("中") is True
    assert mod._is_cjk_ideograph("文") is True
    assert mod._is_cjk_ideograph("\u4e00") is True
    assert mod._is_cjk_ideograph("\u9fff") is True
    assert mod._is_cjk_ideograph("\u3400") is True
    assert mod._is_cjk_ideograph("\uf900") is True

    # False for Hangul, Cyrillic, Latin, and Emoji
    assert mod._is_cjk_ideograph("안") is False
    assert mod._is_cjk_ideograph("녕") is False
    assert mod._is_cjk_ideograph("П") is False
    assert mod._is_cjk_ideograph("A") is False
    assert mod._is_cjk_ideograph("🚀") is False


def test_hangul_wraps_on_whitespace() -> None:
    mod = _load_module()
    text = "안녕하세요 반갑습니다 오늘 날씨가 참 좋습니다"
    lines = mod._wrap_text(text, max_chars=12)
    # Should wrap at word boundaries, not cut mid-syllable
    for line in lines:
        assert len(line) <= 12
    assert " ".join(lines) == text


def test_cyrillic_wraps_on_whitespace() -> None:
    mod = _load_module()
    text = "Привет мир как ваши дела сегодня"
    lines = mod._wrap_text(text, max_chars=15)
    for line in lines:
        assert len(line) <= 15
    assert " ".join(lines) == text


def test_emoji_and_latin_wrap_on_whitespace() -> None:
    mod = _load_module()
    text = "🚀 Launching new product 🌟 today"
    lines = mod._wrap_text(text, max_chars=15)
    for line in lines:
        assert len(line) <= 15
    assert " ".join(lines) == text


def test_cjk_ideographs_wrap_at_character_limit() -> None:
    mod = _load_module()
    text = "这是一个很长的中文字符串用于测试换行逻辑"
    lines = mod._wrap_text(text, max_chars=6)
    assert lines == ["这是一个很长", "的中文字符串", "用于测试换行", "逻辑"]


def test_explicit_newlines_preserved() -> None:
    mod = _load_module()
    text = "Line 1\nLine 2\nLine 3"
    assert mod._wrap_text(text, max_chars=20) == ["Line 1", "Line 2", "Line 3"]
