"""``create_pdf_report`` must not drop newlines or tabs when TTF font is active."""

from __future__ import annotations

from agentos.tools.builtin.file_authoring import (
    _pdf_markup_text,
    _register_pdf_fonts,
)

CJK_FONT = "STSong-Light"


def test_pdf_markup_text_preserves_newlines_tabs_and_carriagereturns_on_ttf_font() -> None:
    """_pdf_markup_text must preserve \\n, \\r, and \\t when TTFont base font is used."""
    base_font, _bold_font, _cjk_font = _register_pdf_fonts()

    text = "Line 1\nLine 2\tTabbed\rLine 3"
    markup = _pdf_markup_text(text, base_font=base_font, cjk_font=CJK_FONT)

    assert "\n" in markup
    assert "\t" in markup
    assert "\r" in markup
    assert markup == "Line 1\nLine 2\tTabbed\rLine 3"
