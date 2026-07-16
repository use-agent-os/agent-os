"""Tests for the offline EmbeddingGemma model downloader."""

from __future__ import annotations

import httpx
import pytest

from agentos.memory import model_download as md


def test_manifest_flattens_onnx_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    # downloaded_model_dir: None when absent, None when dir exists but empty
    assert md.downloaded_model_dir("google/embeddinggemma-300m") is None
    target = tmp_path / "embeddinggemma-300m-q8"
    target.mkdir(parents=True)
    assert md.downloaded_model_dir("google/embeddinggemma-300m") is None
    (target / "model_quantized.onnx").write_bytes(b"x")
    # Partial download (only the .onnx present, not the rest of the
    # manifest) must not resolve — this is the boot-race guard.
    assert md.downloaded_model_dir("google/embeddinggemma-300m") is None


def test_downloaded_model_dir_requires_all_manifest_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    target = tmp_path / "embeddinggemma-300m-q8"
    target.mkdir(parents=True)
    manifest = md.EMBEDDING_MODEL_MANIFESTS["google/embeddinggemma-300m"]
    names = [md._flattened_name(p) for p in manifest.files]

    # Write all files except the last one (still incomplete).
    for name in names[:-1]:
        (target / name).write_bytes(b"x")
    assert md.downloaded_model_dir("google/embeddinggemma-300m") is None

    # A zero-byte file (e.g. an interrupted os.replace target) also fails.
    (target / names[-1]).write_bytes(b"")
    assert md.downloaded_model_dir("google/embeddinggemma-300m") is None

    # Once every file has size > 0, the dir resolves.
    (target / names[-1]).write_bytes(b"x")
    assert md.downloaded_model_dir("google/embeddinggemma-300m") == target


def test_downloaded_model_dir_unknown_model_returns_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    assert md.downloaded_model_dir("nope/none") is None


_CDN_HOST = "https://cdn.example-xet.invalid/signed/"


def _mock_transport(fetched: list[str]) -> httpx.MockTransport:
    """Mirror Hugging Face: ``resolve/`` 302s to a signed CDN URL.

    ``fetched`` records only the origin (pre-redirect) requests, so callers can
    still count one entry per manifest file.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        name = url.rsplit("/", 1)[-1]
        if url.startswith(_CDN_HOST):
            return httpx.Response(200, content=f"data-{name}".encode())
        fetched.append(url)
        return httpx.Response(302, headers={"location": f"{_CDN_HOST}{name}"})

    return httpx.MockTransport(handler)


def _patch_transport(monkeypatch, transport: httpx.MockTransport) -> None:
    """Route the downloader's AsyncClient through ``transport``, keeping kwargs."""

    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(md.httpx, "AsyncClient", patched_async_client)


@pytest.mark.asyncio
async def test_download_streams_flattens_and_skips_existing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    fetched: list[str] = []
    _patch_transport(monkeypatch, _mock_transport(fetched))

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
async def test_download_follows_hf_cdn_redirect(tmp_path, monkeypatch) -> None:
    """Hugging Face 302s every ``resolve/`` URL to a signed CDN URL.

    httpx does not follow redirects by default, so raise_for_status() saw the
    302 as an error and the download aborted before writing a byte.
    """

    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url.startswith(_CDN_HOST):
            return httpx.Response(200, content=b"weights")
        return httpx.Response(302, headers={"location": f"{_CDN_HOST}model_quantized.onnx"})

    _patch_transport(monkeypatch, httpx.MockTransport(handler))

    path = await md.download_embedding_model("google/embeddinggemma-300m")

    assert any(url.startswith(_CDN_HOST) for url in seen), "redirect was not followed"
    assert (path / "model_quantized.onnx").read_bytes() == b"weights"


@pytest.mark.asyncio
async def test_download_unknown_model_raises() -> None:
    with pytest.raises(ValueError):
        await md.download_embedding_model("nope/none")


@pytest.mark.asyncio
async def test_download_reports_progress(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "user_models_dir", lambda: tmp_path)
    fetched: list[str] = []
    _patch_transport(monkeypatch, _mock_transport(fetched))

    progress_calls: list[tuple[str, int, int | None]] = []

    def _progress(name: str, done: int, total: int | None) -> None:
        progress_calls.append((name, done, total))

    await md.download_embedding_model("google/embeddinggemma-300m", progress=_progress)

    assert progress_calls
    names_reported = {name for name, _, _ in progress_calls}
    assert "tokenizer.json" in names_reported
