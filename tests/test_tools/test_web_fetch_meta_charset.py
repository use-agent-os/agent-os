"""web_fetch must honor an HTML page's own <meta> charset declaration when
the Content-Type header names none (issue #2557).

Aozora Bunko (Japanese public-domain literature) and many other sites send
a bare ``Content-Type: text/html`` and declare the encoding only via
``<meta charset=...>`` or the older ``<meta http-equiv="Content-Type"
content="...;charset=...">`` form -- ordinary, spec-sanctioned HTML that
every browser handles via the encoding prescan. Decoding as UTF-8 by
default turned a Shift_JIS page into a wall of U+FFFD replacement
characters, with a normal ``status: 200`` and no signal that anything was
wrong.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, reset_runtime
from agentos.tools.builtin import web_fetch as wf
from agentos.tools.builtin.web_fetch import _decode_response_body, _sniff_html_meta_charset

# ── _sniff_html_meta_charset ────────────────────────────────────────────────


def test_sniff_extracts_the_simple_charset_attribute() -> None:
    html = b'<html><head><meta charset="windows-1251"></head></html>'
    assert _sniff_html_meta_charset(html) == "cp1251"


def test_sniff_extracts_single_quoted_charset() -> None:
    assert _sniff_html_meta_charset(b"<meta charset='gbk'>") == "gbk"


def test_sniff_extracts_the_http_equiv_content_type_form() -> None:
    # The issue's own two repro pages, byte for byte.
    aozora = b'<meta http-equiv="Content-Type" content="text/html;charset=Shift_JIS" />'
    itmedia = b'<meta http-equiv="content-type" content="text/html;charset=shift_jis">'
    assert _sniff_html_meta_charset(aozora) == "shift_jis"
    assert _sniff_html_meta_charset(itmedia) == "shift_jis"


def test_sniff_extracts_http_equiv_regardless_of_attribute_order() -> None:
    reversed_order = b'<meta content="text/html; charset=EUC-JP" http-equiv="Content-Type">'
    assert _sniff_html_meta_charset(reversed_order) == "euc_jp"


def test_sniff_ignores_a_commented_out_meta_tag() -> None:
    html = b'<html><!-- <meta charset="utf-8"> --><meta charset="shift_jis"></html>'
    assert _sniff_html_meta_charset(html) == "shift_jis"


def test_sniff_only_looks_at_the_first_1024_bytes() -> None:
    padding = b"<!-- " + b"x" * 2000 + b" -->"
    html = padding + b'<meta charset="gbk">'
    assert _sniff_html_meta_charset(html) is None


def test_sniff_maps_utf_16_and_utf_32_labels_to_utf_8() -> None:
    """The HTML spec requires this: a genuinely UTF-16/32 document is caught
    by its byte-order mark before any ASCII <meta> tag could be parsed, so a
    *label* claiming one of these is not trustworthy on its own."""
    for label in ("utf-16", "utf-16le", "utf-32be"):
        assert _sniff_html_meta_charset(f'<meta charset="{label}">'.encode()) == "utf-8"


def test_sniff_rejects_a_non_text_codec_label() -> None:
    """base64/rot13/hex resolve via codecs.lookup but cannot decode
    arbitrary text -- using one would corrupt the page, not read it."""
    assert _sniff_html_meta_charset(b'<meta charset="base64">') is None
    assert _sniff_html_meta_charset(b'<meta charset="rot13">') is None


def test_sniff_returns_none_for_an_unknown_label() -> None:
    assert _sniff_html_meta_charset(b'<meta charset="not-a-real-codec">') is None


def test_sniff_returns_none_when_there_is_no_meta_tag() -> None:
    assert _sniff_html_meta_charset(b"<html><body>hello</body></html>") is None


# ── _decode_response_body ───────────────────────────────────────────────────


def test_decode_uses_the_meta_charset_when_the_header_names_none() -> None:
    sjis = '<html><head><meta charset="shift_jis"></head><body>夏目漱石 こころ</body></html>'
    body = sjis.encode("shift_jis")

    decoded = _decode_response_body(body, "text/html", None)

    assert "夏目漱石 こころ" in decoded
    assert "�" not in decoded


def test_decode_prefers_the_header_charset_over_a_disagreeing_meta_tag() -> None:
    body = b'<html><head><meta charset="shift_jis"></head><body>hello</body></html>'

    decoded = _decode_response_body(body, "text/html", "utf-8")

    assert "hello" in decoded


def test_decode_never_sniffs_a_non_html_body() -> None:
    body = "こんにちは".encode("shift_jis")

    decoded = _decode_response_body(body, "text/plain", None)

    assert "�" in decoded  # UTF-8 fallback still applies, as before


def test_decode_falls_back_to_utf8_when_nothing_names_a_charset() -> None:
    assert _decode_response_body(b"hello", "text/html", None) == "hello"


def test_decode_falls_back_to_utf8_when_the_meta_charset_is_unusable() -> None:
    body = b'<html><head><meta charset="not-a-real-codec"></head><body>hello</body></html>'

    decoded = _decode_response_body(body, "text/html", None)

    assert "hello" in decoded


# ── end to end: the issue's own repro shape ─────────────────────────────────


@pytest.fixture
def sandbox_off(tmp_path: Any) -> Any:
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False, allow_legacy_mode=True),
        workspace=tmp_path,
    )
    yield
    reset_runtime()


def _install_mock_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        "socket.getaddrinfo", lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    monkeypatch.setattr(wf.httpx, "AsyncClient", fake_async_client)


@pytest.mark.asyncio
async def test_web_fetch_decodes_a_meta_only_shift_jis_page(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    """The issue's own repro, end to end through the real web_fetch() tool:
    a bare ``Content-Type: text/html`` header, encoding declared only via
    <meta>, no crafted input needed."""
    html = (
        "<html><head><title>夏目漱石 こころ</title>"
        '<meta http-equiv="Content-Type" content="text/html;charset=Shift_JIS" />'
        "</head><body><p>吾輩は猫である。</p></body></html>"
    ).encode("shift_jis")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    _install_mock_transport(monkeypatch, handler)
    wf._cache.clear()

    result = json.loads(await wf.web_fetch(url="https://example.com/kokoro"))

    assert result["status"] == 200
    assert "�" not in result["title"]
    assert "夏目漱石" in result["title"]
    assert "�" not in str(result["text"])


@pytest.mark.asyncio
async def test_web_fetch_still_honors_an_explicit_header_charset(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    """Regression guard: the header-charset path fixed by an earlier commit
    (b8bd3466, cited in the issue) must be unaffected by this change."""
    body = b"caf\xe9 na\xefve r\xe9sum\xe9"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain; charset=iso-8859-1"}, content=body
        )

    _install_mock_transport(monkeypatch, handler)
    wf._cache.clear()

    result = json.loads(await wf.web_fetch(url="https://example.com/latin1"))

    assert "café" in str(result["text"])
    assert "�" not in str(result["text"])
