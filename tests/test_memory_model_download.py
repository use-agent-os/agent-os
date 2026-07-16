"""Tests for the offline EmbeddingGemma model downloader."""

from __future__ import annotations

import httpx
import pytest

from agentos.memory import model_download as md


def test_manifest_flattens_onnx_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    # downloaded_model_dir: None when absent, None when dir exists but no onnx
    assert md.downloaded_model_dir("google/embeddinggemma-300m") is None
    target = tmp_path / "embeddinggemma-300m-q8"
    target.mkdir(parents=True)
    assert md.downloaded_model_dir("google/embeddinggemma-300m") is None
    (target / "model_quantized.onnx").write_bytes(b"x")
    assert md.downloaded_model_dir("google/embeddinggemma-300m") == target


def _mock_transport(fetched: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        name = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(200, content=f"data-{name}".encode())

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_download_streams_flattens_and_skips_existing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    fetched: list[str] = []
    real_async_client = httpx.AsyncClient
    transport = _mock_transport(fetched)

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(md.httpx, "AsyncClient", patched_async_client)

    path = await md.download_embedding_model("google/embeddinggemma-300m")

    assert (path / "model_quantized.onnx").read_bytes() == b"data-model_quantized.onnx"
    assert (path / "model_quantized.onnx_data").exists()  # flattened next to .onnx
    assert (path / "tokenizer.json").exists()
    assert (path / "tokenizer_config.json").exists()
    assert (path / "config.json").exists()
    assert (path / "special_tokens_map.json").exists()
    assert not list(path.glob("*.part"))
    assert len(fetched) == 6

    # second run skips everything (no new fetches)
    before = list(fetched)
    await md.download_embedding_model("google/embeddinggemma-300m")
    assert fetched == before


@pytest.mark.asyncio
async def test_download_unknown_model_raises() -> None:
    with pytest.raises(ValueError):
        await md.download_embedding_model("nope/none")


@pytest.mark.asyncio
async def test_download_reports_progress(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    fetched: list[str] = []
    real_async_client = httpx.AsyncClient
    transport = _mock_transport(fetched)

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(md.httpx, "AsyncClient", patched_async_client)

    progress_calls: list[tuple[str, int, int | None]] = []

    def _progress(name: str, done: int, total: int | None) -> None:
        progress_calls.append((name, done, total))

    await md.download_embedding_model("google/embeddinggemma-300m", progress=_progress)

    assert progress_calls
    names_reported = {name for name, _, _ in progress_calls}
    assert "tokenizer.json" in names_reported
