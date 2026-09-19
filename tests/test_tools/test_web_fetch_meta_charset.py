from __future__ import annotations

from agentos.tools.builtin.web_fetch import _decode_html_bytes, _sniff_meta_charset


def test_sniff_meta_charset_extracts_shift_jis_http_equiv() -> None:
    html_bytes = (
        b'<html><head><meta http-equiv="Content-Type" '
        b'content="text/html;charset=Shift_JIS" /></head></html>'
    )
    assert _sniff_meta_charset(html_bytes) == "shift_jis"


def test_sniff_meta_charset_extracts_windows_1251_charset_attr() -> None:
    html_bytes = b'<html><head><meta charset="windows-1251"></head></html>'
    assert _sniff_meta_charset(html_bytes) == "cp1251"


def test_sniff_meta_charset_maps_iso_8859_1_to_windows_1252() -> None:
    html_bytes = b'<html><head><meta charset="iso-8859-1"></head></html>'
    assert _sniff_meta_charset(html_bytes) == "cp1252"


def test_sniff_meta_charset_maps_utf_16_to_utf_8() -> None:
    html_bytes = b'<html><head><meta charset="utf-16"></head></html>'
    assert _sniff_meta_charset(html_bytes) == "utf-8"


def test_sniff_meta_charset_ignores_commented_out_tags() -> None:
    html_bytes = b'<html><!-- <meta charset="utf-8"> --><meta charset="shift_jis"></html>'
    assert _sniff_meta_charset(html_bytes) == "shift_jis"


def test_sniff_meta_charset_rejects_non_text_codecs() -> None:
    html_bytes = b'<html><head><meta charset="base64"></head></html>'
    assert _sniff_meta_charset(html_bytes) is None


def test_decode_html_bytes_decodes_shift_jis_page_without_header_charset() -> None:
    # "夏目漱石" encoded in Shift_JIS
    sjis_bytes = '<html><head><meta charset="shift_jis"></head><body>夏目漱石</body></html>'.encode(
        "shift_jis"
    )
    decoded = _decode_html_bytes(sjis_bytes, content_type="text/html", header_charset=None)
    assert "夏目漱石" in decoded


def test_decode_html_bytes_strips_utf8_bom() -> None:
    bom_utf8 = b"\xef\xbb\xbf<html><body>Hello</body></html>"
    decoded = _decode_html_bytes(bom_utf8, content_type="text/html", header_charset=None)
    assert not decoded.startswith("\ufeff")
    assert decoded.startswith("<html>")


def test_decode_html_bytes_header_charset_takes_precedence() -> None:
    # Body claims Shift_JIS, but header specifies UTF-8
    utf8_bytes = b'<html><head><meta charset="shift_jis"></head><body>UTF-8 content</body></html>'
    decoded = _decode_html_bytes(utf8_bytes, content_type="text/html", header_charset="utf-8")
    assert "UTF-8 content" in decoded
