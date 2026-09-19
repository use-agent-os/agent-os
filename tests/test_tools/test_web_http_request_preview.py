"""``http_request`` body_preview must count characters, like the body it mirrors.

With ``output_path`` set the response body is saved and the model sees only
``body_preview``. That preview was cut out of the raw bytes while the same
``_TEXT_BODY_LIMIT`` is applied to characters on the branch that returns the
body inline, so a page in any script that is not Latin-1 previewed a third as
much text — and the character straddling the cut arrived as a ``�`` that
was never in the document.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import httpx
import pytest

from agentos.tools.builtin import web

HttpRequestCallable = Callable[..., Awaitable[str]]

_LIMIT = web._TEXT_BODY_LIMIT


def _original_http_request() -> HttpRequestCallable:
    return cast(HttpRequestCallable, web.http_request.__wrapped__.__wrapped__)


def _patch_response(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def build_request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            content: bytes | None = None,
        ) -> httpx.Request:
            return httpx.Request(method, url, headers=headers, content=content)

        async def send(self, request: httpx.Request, **kwargs: object) -> httpx.Response:
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
                request=request,
            )

    monkeypatch.setattr(web.httpx, "AsyncClient", FakeAsyncClient)


def _html(body: str) -> bytes:
    return body.encode("utf-8")


async def _fetch(*, save: bool) -> dict:
    kwargs = {"url": "https://example.test/page"}
    if save:
        kwargs["output_path"] = "page.html"
    return json.loads(await _original_http_request()(**kwargs))


def _serve(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", "https://example.test/page"),
        ),
    )


# A sentence whose every character is three UTF-8 bytes.
_JP_SENTENCE = "日本語のページです。"
_JP_DOC = _JP_SENTENCE * 2000


@pytest.mark.asyncio
async def test_preview_of_a_cjk_page_holds_the_full_character_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _serve(monkeypatch, _html(_JP_DOC))

    payload = await _fetch(save=True)

    assert _JP_DOC[:_LIMIT] in payload["body_preview"]


@pytest.mark.asyncio
async def test_preview_does_not_invent_a_replacement_character(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cutting the bytes split the character on the boundary into U+FFFD."""
    monkeypatch.chdir(tmp_path)
    _serve(monkeypatch, _html(_JP_DOC))

    payload = await _fetch(save=True)

    assert "�" not in payload["body_preview"]


@pytest.mark.asyncio
async def test_preview_matches_the_body_the_same_response_returns_inline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The only difference output_path makes is where the body went."""
    monkeypatch.chdir(tmp_path)
    _serve(monkeypatch, _html(_JP_DOC))

    inline = await _fetch(save=False)
    saved = await _fetch(save=True)

    assert saved["body_preview"] == inline["body"]


@pytest.mark.asyncio
async def test_a_cjk_page_previews_as_much_as_an_ascii_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    _serve(monkeypatch, _html("the quick brown fox. " * 2000))
    ascii_preview = (await _fetch(save=True))["body_preview"]

    (tmp_path / "second").mkdir()
    monkeypatch.chdir(tmp_path / "second")
    _serve(monkeypatch, _html(_JP_DOC))
    cjk_preview = (await _fetch(save=True))["body_preview"]

    assert len(cjk_preview) == len(ascii_preview)


@pytest.mark.asyncio
async def test_a_short_page_previews_whole(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Guard: a body under the cap is unaffected either way."""
    monkeypatch.chdir(tmp_path)
    _serve(monkeypatch, _html("<p>hello</p>"))

    payload = await _fetch(save=True)

    assert "<p>hello</p>" in payload["body_preview"]
    assert payload["body_saved"] is True
    assert payload["body"] is None


@pytest.mark.asyncio
async def test_an_ascii_page_preview_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guard: the Latin-1 case, where bytes and characters agree, must not move."""
    monkeypatch.chdir(tmp_path)
    doc = "the quick brown fox. " * 2000
    _serve(monkeypatch, _html(doc))

    payload = await _fetch(save=True)

    assert doc[:_LIMIT] in payload["body_preview"]
