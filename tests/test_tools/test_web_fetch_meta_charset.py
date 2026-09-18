"""Tests: web_fetch decodes an HTML page with the charset its <meta> declares.

When Content-Type carries no charset, httpx reports UTF-8 as a default, not
as something the server said. Pages such as aozora.gr.jp and itmedia.co.jp
serve plain ``text/html`` and name Shift_JIS only in a <meta> tag, so
decoding them as UTF-8 handed the model a page of U+FFFD.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, reset_runtime
from agentos.tools.builtin import web_fetch as wf

_JA = "私はその人を常に先生と呼んでいた。だからここでもただ先生と書くだけで本名は打ち明けない。"
_RU = "Все счастливые семьи похожи друг на друга, каждая несчастливая семья несчастлива по-своему."
_ZH = "学而时习之，不亦说乎？有朋自远方来，不亦乐乎？人不知而不愠，不亦君子乎？"
_KO = "나는 나의 조국을 사랑한다. 모든 사람은 자유롭고 평등하게 태어났다."


@pytest.fixture
def sandbox_off(tmp_path: Path) -> Any:
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False, allow_legacy_mode=True),
        workspace=tmp_path,
    )
    wf._cache.clear()
    yield
    wf._cache.clear()
    reset_runtime()


class _StreamingBody(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> Any:
        yield self._data


def _serve(monkeypatch: pytest.MonkeyPatch, content_type: str, body: bytes) -> None:
    """Answer every web_fetch request with ``body`` under ``content_type``."""

    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": content_type}, stream=_StreamingBody(body)
            )

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(*args, transport=_Transport(), **kwargs)

    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    monkeypatch.setattr(wf.httpx, "AsyncClient", fake_async_client)


def _page(head: str, sentence: str, title: str = "") -> str:
    paragraphs = "".join(f"<p>{sentence}</p>" for _ in range(8))
    return (
        f"<html><head>{head}<title>{title}</title></head>"
        f"<body><article>{paragraphs}</article></body></html>"
    )


async def _fetch(**kwargs: Any) -> dict[str, Any]:
    return json.loads(await wf.web_fetch(url="https://example.com/page", **kwargs))


# --- Fails on main: the page's <meta> charset was ignored ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("head", "sentence", "codec"),
    [
        (
            '<meta http-equiv="Content-Type" content="text/html;charset=Shift_JIS" />',
            _JA,
            "shift_jis",
        ),
        ('<meta charset="windows-1251">', _RU, "cp1251"),
        ("<meta charset='gb2312'>", _ZH, "gb2312"),
        ("<meta http-equiv=Content-Type content=text/html;charset=euc-kr>", _KO, "euc_kr"),
        ('<META HTTP-EQUIV="content-type" CONTENT="text/html; charset = EUC-JP">', _JA, "euc_jp"),
        ('<meta name="viewport" content="width=device-width"><meta charset=gbk>', _ZH, "gbk"),
        ('<meta charset="base64"><meta charset="shift_jis">', _JA, "shift_jis"),
    ],
    ids=[
        "http-equiv-shift_jis",
        "charset-cp1251",
        "single-quoted-gb2312",
        "unquoted-euc-kr",
        "uppercase-euc-jp",
        "second-meta-unquoted-gbk",
        "unusable-label-then-shift_jis",
    ],
)
async def test_meta_charset_decodes_page_when_header_names_none(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any, head: str, sentence: str, codec: str
) -> None:
    _serve(monkeypatch, "text/html", _page(head, sentence).encode(codec))

    result = await _fetch()

    assert "\ufffd" not in result["text"]
    assert sentence in result["text"]


@pytest.mark.asyncio
async def test_meta_charset_decodes_the_title(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    head = '<meta http-equiv="content-type" content="text/html;charset=shift_jis">'
    title = "こころ 夏目漱石"
    _serve(monkeypatch, "text/html", _page(head, _JA, title).encode("shift_jis"))

    result = await _fetch()

    assert result["title"] == title


@pytest.mark.asyncio
async def test_meta_charset_applies_to_text_extract_mode(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    _serve(monkeypatch, "text/html", _page('<meta charset="shift_jis">', _JA).encode("shift_jis"))

    result = await _fetch(extract_mode="text")

    assert "\ufffd" not in result["text"]
    assert "先生" in result["text"]


@pytest.mark.asyncio
async def test_commented_out_meta_does_not_hide_the_real_one(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    head = '<!-- <meta charset="utf-8"> --><meta charset="euc-jp">'
    _serve(monkeypatch, "text/html", _page(head, _JA).encode("euc_jp"))

    result = await _fetch()

    assert "\ufffd" not in result["text"]
    assert _JA in result["text"]


@pytest.mark.asyncio
async def test_xhtml_content_type_is_sniffed_too(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    body = _page('<meta charset="shift_jis" />', _JA).encode("shift_jis")
    _serve(monkeypatch, "application/xhtml+xml", body)

    result = await _fetch()

    assert _JA in result["text"]


# --- Guards: pass on main and with the fix, by design ----------------------


@pytest.mark.asyncio
async def test_header_charset_still_wins_over_meta(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    """Guard: the server's charset keeps precedence, as b8bd3466 set up."""
    body = _page('<meta charset="shift_jis">', _JA).encode("utf-8")
    _serve(monkeypatch, "text/html; charset=utf-8", body)

    result = await _fetch()

    assert _JA in result["text"]


@pytest.mark.asyncio
async def test_utf8_meta_page_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    """Guard: the common case (sina.com.cn, kanji.org) still decodes as UTF-8."""
    head = '<meta http-equiv="Content-type" content="text/html; charset=utf-8" />'
    _serve(monkeypatch, "text/html", _page(head, _ZH).encode("utf-8"))

    result = await _fetch()

    assert _ZH in result["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "head",
    [
        '<meta charset="no-such-codec">',
        '<meta charset="utf-16">',
        '<meta charset="utf-32">',
        '<meta charset="base64">',
        '<meta charset="rot13">',
        '<!-- <meta charset="shift_jis"> -->',
        '<!-- <meta charset="shift_jis">' + " " * 1100 + "-->",
        '<meta name="description" content="charset=shift_jis">',
        '<meta charset="">',
        "",
    ],
    ids=[
        "unknown-label",
        "utf-16-label",
        "utf-32-label",
        "bytes-codec-base64",
        "str-codec-rot13",
        "only-in-comment",
        "comment-closing-past-window",
        "content-without-http-equiv",
        "empty-label",
        "no-meta",
    ],
)
async def test_unusable_declaration_keeps_utf8(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any, head: str
) -> None:
    """Guard: anything the sniffer must not trust leaves the UTF-8 fallback."""
    _serve(monkeypatch, "text/html", _page(head, _JA).encode("utf-8"))

    result = await _fetch()

    assert "\ufffd" not in result["text"]
    assert _JA in result["text"]


@pytest.mark.asyncio
async def test_meta_past_the_prescan_window_is_ignored(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    """Guard: only the first 1024 bytes are scanned, as the HTML spec says."""
    head = "<style>" + " " * 1100 + '</style><meta charset="shift_jis">'
    _serve(monkeypatch, "text/html", _page(head, _JA).encode("utf-8"))

    result = await _fetch()

    assert _JA in result["text"]


@pytest.mark.asyncio
async def test_non_html_body_is_never_sniffed(
    monkeypatch: pytest.MonkeyPatch, sandbox_off: Any
) -> None:
    """Guard: a text/plain body that quotes a <meta> tag stays UTF-8."""
    body = f'<meta charset="shift_jis">\n{_JA}'.encode()
    _serve(monkeypatch, "text/plain", body)

    result = await _fetch()

    assert result["extractor"] == "raw"
    assert _JA in result["text"]
