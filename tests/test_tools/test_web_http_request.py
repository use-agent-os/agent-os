from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import httpx
import pytest

from agentos.tools.builtin import web
from agentos.tools.types import ToolError

HttpRequestCallable = Callable[..., Awaitable[str]]


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
            # Re-bind the pre-built response to the request actually in flight,
            # preserving any pre-seeded body as an async stream so the streaming
            # download path can iterate over it.
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
                request=request,
            )

    monkeypatch.setattr(web.httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_http_request_returns_body_base64_for_octet_stream_invalid_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"\xff\xfe\x00PDF"
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/octet-stream"},
            request=httpx.Request("GET", "https://example.test/file"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/file"))

    assert payload["content_type"] == "application/octet-stream"
    assert payload["body"] is None
    assert base64.b64decode(payload["body_base64"]) == raw
    assert payload["body_base64_truncated"] is False


@pytest.mark.asyncio
async def test_http_request_returns_text_body_for_json_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=b'{"ok":true}',
            headers={"content-type": "application/json; charset=utf-8"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/data"))

    # Text bodies are external content: delivered inside the <untrusted>
    # envelope, payload verbatim, source naming the fetched URL.
    assert payload["body"] == (
        "<untrusted source='https://example.test/data'>{\"ok\":true}</untrusted>"
    )
    assert base64.b64decode(payload["body_base64"]) == b'{"ok":true}'
    assert payload["body_truncated"] is False


@pytest.mark.asyncio
async def test_http_request_keeps_body_base64_for_misleading_text_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"\xff\xfe\x00PDF"
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "text/plain; charset=utf-8"},
            request=httpx.Request("GET", "https://example.test/mislabelled"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/mislabelled"))

    assert payload["body"] is not None
    assert "\ufffd" in payload["body"]
    assert base64.b64decode(payload["body_base64"]) == raw


@pytest.mark.asyncio
async def test_http_request_uses_body_base64_when_content_type_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"\x00\x01\x02"
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            request=httpx.Request("GET", "https://example.test/blob"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/blob"))

    assert payload["content_type"] == ""
    assert payload["body"] is None
    assert base64.b64decode(payload["body_base64"]) == raw


@pytest.mark.asyncio
async def test_http_request_honors_response_charset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "café".encode("iso-8859-1")
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "text/plain; charset=iso-8859-1"},
            request=httpx.Request("GET", "https://example.test/latin1"),
        ),
    )
    payload = json.loads(await _original_http_request()(url="https://example.test/latin1"))
    assert "café" in payload["body"]


@pytest.mark.asyncio
async def test_http_request_does_not_implicitly_save_large_binary_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b"x" * 1_000_001
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/octet-stream"},
            request=httpx.Request("GET", "https://example.test/large"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/large"))

    digest = hashlib.sha256(raw).hexdigest()
    saved_path = tmp_path / ".fetch" / f"{digest}.bin"
    assert payload["size"] == len(raw)
    assert payload["sha256"] == digest
    assert payload["body_saved"] is False
    assert payload["body"] is None
    assert payload["body_base64"] is not None
    assert payload["body_base64_truncated"] is True
    assert payload["path"] is None
    assert not saved_path.exists()


@pytest.mark.asyncio
async def test_http_request_does_not_implicitly_save_large_text_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b"<feed>" + (b"a" * 60_000) + b"</feed>"
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/xml"},
            request=httpx.Request("GET", "https://example.test/feed"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/feed"))

    digest = hashlib.sha256(raw).hexdigest()
    saved_path = tmp_path / ".fetch" / f"{digest}.bin"
    assert payload["size"] == len(raw)
    assert payload["sha256"] == digest
    assert payload["body_saved"] is False
    envelope_open = "<untrusted source='https://example.test/feed'>"
    assert payload["body"].startswith(envelope_open + "<feed>")
    assert payload["body"].endswith("</untrusted>")
    # The 10k text cap applies to the payload; the envelope rides on top.
    assert len(payload["body"]) == len(envelope_open) + 10_000 + len("</untrusted>")
    assert payload["body_base64"] is not None
    assert payload["body_truncated"] is True
    assert payload["body_base64_truncated"] is False
    assert payload["path"] is None
    assert not saved_path.exists()


@pytest.mark.asyncio
async def test_http_request_output_path_saves_inside_fetch_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b'{"ok":true}'
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    payload = json.loads(
        await _original_http_request()(
            url="https://example.test/data",
            output_path="raw.json",
        )
    )

    saved_path = tmp_path / ".fetch" / "raw.json"
    assert Path(payload["path"]) == saved_path
    assert saved_path.read_bytes() == raw
    assert payload["body_saved"] is True
    assert payload["body"] is None
    assert payload["body_base64"] is None


@pytest.mark.asyncio
async def test_http_request_output_path_rejects_existing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b'{"ok":true}'
    monkeypatch.chdir(tmp_path)
    existing_path = tmp_path / ".fetch" / "raw.json"
    existing_path.parent.mkdir()
    existing_path.write_bytes(b"keep")
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    with pytest.raises(ToolError, match="output_path already exists"):
        await _original_http_request()(
            url="https://example.test/data",
            output_path="raw.json",
        )

    assert existing_path.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_http_request_output_path_rejects_fetch_directory_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=b"escape",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    with pytest.raises(ToolError, match="output_path must stay inside"):
        await _original_http_request()(
            url="https://example.test/data",
            output_path="../escape.txt",
        )
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_http_request_output_path_rejects_foreign_posix_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agentos.tools.builtin import web

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web.os, "name", "nt")
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=b"foreign",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    with pytest.raises(ToolError, match="foreign_host_path"):
        await _original_http_request()(
            url="https://example.test/data",
            output_path="/Users/a1/Desktop/raw.txt",
        )

    assert not (tmp_path / "Users").exists()


@pytest.mark.asyncio
async def test_http_request_without_output_path_does_not_create_fetch_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b"x" * 60_000
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/octet-stream"},
            request=httpx.Request("GET", "https://example.test/blob"),
        ),
    )

    first = json.loads(await _original_http_request()(url="https://example.test/blob"))
    second = json.loads(await _original_http_request()(url="https://example.test/blob"))

    assert first["path"] is None
    assert second["path"] is None
    assert not (tmp_path / ".fetch").exists()


class _StreamingAsyncClient:
    """AsyncClient whose request() returns a streaming httpx.Response.

    The handler builds the response body lazily (chunked, no content-length) so
    the test can assert the tool stops reading near the download ceiling instead
    of buffering the entire body into memory.
    """

    def __init__(
        self, handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
    ) -> None:
        self._handler = handler

    async def __aenter__(self) -> _StreamingAsyncClient:
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
        return self._handler(request)


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, total: int, chunk: int = 256 * 1024) -> None:
        self._remaining = total
        self._chunk = chunk

    async def __aiter__(self):
        while self._remaining > 0:
            n = min(self._chunk, self._remaining)
            self._remaining -= n
            yield b"A" * n


@pytest.mark.asyncio
async def test_http_request_caps_download_at_hard_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Serve a 50MB chunked body (no content-length). The tool must stop reading
    # near the 1MiB download cap, not buffer the whole body into RAM.
    total = 50 * 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            stream=_AsyncBody(total),
            request=request,
        )

    monkeypatch.setattr(web.httpx, "AsyncClient", lambda *a, **k: _StreamingAsyncClient(handler))

    payload = json.loads(await _original_http_request()(url="https://example.test/huge"))

    assert payload["download_capped"] is True
    assert payload["body_base64_truncated"] is True
    assert payload["body_saved"] is False
    assert payload["size"] <= 1_000_000 + _STREAM_CHUNK, payload["size"]
    assert payload["size"] < total


@pytest.mark.asyncio
async def test_http_request_env_overrides_download_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_HTTP_DOWNLOAD_LIMIT", "200000")
    total = 5 * 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            stream=_AsyncBody(total),
            request=request,
        )

    monkeypatch.setattr(web.httpx, "AsyncClient", lambda *a, **k: _StreamingAsyncClient(handler))

    payload = json.loads(await _original_http_request()(url="https://example.test/huge"))

    assert payload["download_capped"] is True
    assert payload["size"] <= 200_000 + _STREAM_CHUNK, payload["size"]


_STREAM_CHUNK = 65_536


@pytest.mark.asyncio
async def test_http_request_output_path_records_workspace_file_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agentos.tools.types import ToolContext, current_tool_context

    raw = b"%PDF-1.4 sample report content"
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", "https://example.test/report.pdf"),
        ),
    )

    ctx = ToolContext(workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    try:
        payload = json.loads(
            await _original_http_request()(
                url="https://example.test/report.pdf",
                output_path="report.pdf",
            )
        )
        saved_path = tmp_path / ".fetch" / "report.pdf"
        assert Path(payload["path"]) == saved_path
        assert saved_path.read_bytes() == raw
        assert len(ctx.workspace_file_writes) == 1
        record = ctx.workspace_file_writes[0]
        assert record["name"] == "report.pdf"
        assert record["suffix"] == ".pdf"
        assert record["relative_path"] == ".fetch/report.pdf"
    finally:
        current_tool_context.reset(token)

