"""``create_pdf_report`` must not silently drop non-Latin-1 scripts (issue #2187).

On hosts without a local TTF the base font falls back to Helvetica, which only
covers U+0000-U+00FF.  CJK ideographs were already routed to ``STSong-Light``,
but every other non-Latin-1 script (Cyrillic, Arabic, Hebrew, Thai, etc.) was
silently deleted by the ``continue`` guard in ``_pdf_markup_text``.
"""

from __future__ import annotations

import pytest

from agentos.tools.builtin.file_authoring import (
    _pdf_markup_text,
    _register_pdf_fonts,
)

CJK_FONT = "STSong-Light"


def _markup(text: str) -> str:
    _register_pdf_fonts()
    return _pdf_markup_text(text, base_font="Helvetica", cjk_font=CJK_FONT)


@pytest.mark.parametrize(
    "text,expected_chars",
    [
        ("Hello Привет World", "Привет"),
        ("café résumé naïve", "café résumé naïve"),
        ("مرحبا", "مرحبا"),
        ("שלום", "שלום"),
        ("สวัสดี", "สวัสดี"),
    ],
)
def test_non_latin1_characters_are_not_dropped_with_cjk_fallback(
    text: str, expected_chars: str
) -> None:
    """Non-Latin-1 characters must survive when a CJK fallback font is available."""
    markup = _markup(text)
    for char in expected_chars:
        assert char in markup, f"{char!r} (U+{ord(char):04X}) missing from {markup!r}"


def test_mixed_latin_cyrillic_preserves_all() -> None:
    """Exact repro from issue #2187: 'Hello Привет World' → 'Hello  World'."""
    markup = _markup("Hello Привет World")
    assert "Hello" in markup
    assert "World" in markup
    assert "Привет" in markup


def test_non_latin1_dropped_when_no_fallback_font_available() -> None:
    """Without any fallback font, unsupported chars are still dropped (no crash)."""
    _register_pdf_fonts()
    markup = _pdf_markup_text("Hello Привет World", base_font="Helvetica", cjk_font=None)
    assert "Hello" in markup
    assert "World" in markup
    # Cyrillic cannot be rendered by Helvetica and there is no fallback
    assert "Привет" not in markup
