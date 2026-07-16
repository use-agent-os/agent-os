"""On-demand downloader for local embedding models (EmbeddingGemma ONNX export).

Downloads are opt-in and run only when the CLI (or a future onboarding step)
explicitly invokes :func:`download_embedding_model`. Files land under
``agentos.paths.default_agentos_home() / "models" / "embeddings"`` so a single
download is shared across agents and survives config/version changes.

Remote Hugging Face paths use an ``onnx/`` subdirectory (e.g.
``onnx/model_quantized.onnx``); those are flattened into the target directory
root so the resulting layout matches what :mod:`agentos.memory.embedding`'s
bundled-ONNX loader expects (``*.onnx`` glob at the top level, with the
external-weights ``.onnx_data`` file sitting next to it).

Fetches follow redirects: Hugging Face serves ``resolve/`` URLs as a 302 to a
signed, short-lived CDN URL.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from agentos.env import trust_env as _trust_env
from agentos.paths import default_agentos_home

logger = structlog.get_logger(__name__)

_STREAM_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class ModelManifest:
    """Describes a downloadable local embedding model."""

    model_id: str
    target_dirname: str
    base_url: str
    files: tuple[str, ...]  # remote relative paths (may include subdirs)
    approx_total_mb: int


EMBEDDING_MODEL_MANIFESTS: dict[str, ModelManifest] = {
    "google/embeddinggemma-300m": ModelManifest(
        model_id="google/embeddinggemma-300m",
        target_dirname="embeddinggemma-300m-q8",
        base_url="https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX/resolve/main/",
        files=(
            "onnx/model_quantized.onnx_data",
            "tokenizer.json",
            "tokenizer_config.json",
            "config.json",
            "special_tokens_map.json",
            "onnx/model_quantized.onnx",
        ),
        approx_total_mb=340,
    ),
}


def user_models_dir() -> Path:
    """Return the root directory where downloaded embedding models live."""

    return default_agentos_home() / "models" / "embeddings"


def _manifest_for(model_id: str) -> ModelManifest:
    manifest = EMBEDDING_MODEL_MANIFESTS.get(model_id)
    if manifest is None:
        raise ValueError(f"Unknown embedding model id: {model_id!r}")
    return manifest


def downloaded_model_dir(model_id: str) -> Path | None:
    """Return the target dir for ``model_id`` if a complete download exists.

    For a known model (registered in ``EMBEDDING_MODEL_MANIFESTS``), every
    file in its manifest must be present with size > 0 — a gateway booting
    mid-download (or after a crash) must never resolve a model with missing
    weights. Returns ``None`` when the model is unknown, the directory does
    not exist, or any manifest file is missing/empty (a partial or stale
    download).
    """

    manifest = EMBEDDING_MODEL_MANIFESTS.get(model_id)
    if manifest is None:
        return None
    target = user_models_dir() / manifest.target_dirname
    if not target.is_dir():
        return None
    for remote_path in manifest.files:
        file_path = target / _flattened_name(remote_path)
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return None
    return target


def _flattened_name(remote_path: str) -> str:
    """Flatten a remote relative path (e.g. ``onnx/x.onnx``) to its basename."""

    return remote_path.rsplit("/", 1)[-1]


async def download_embedding_model(
    model_id: str,
    *,
    progress: Callable[[str, int, int | None], None] | None = None,
) -> Path:
    """Download every file in ``model_id``'s manifest into its target dir.

    Files already present with size > 0 are skipped (no network call). Each
    file streams to a ``<name>.part`` sibling and is atomically moved into
    place with :func:`os.replace` once fully written, so a crash mid-download
    never leaves a truncated file at the final name.

    Raises:
        ValueError: if ``model_id`` has no registered manifest.
    """

    manifest = _manifest_for(model_id)
    target_dir = user_models_dir() / manifest.target_dirname
    target_dir.mkdir(parents=True, exist_ok=True)

    # follow_redirects: Hugging Face answers every ``resolve/`` URL with a 302 to
    # a signed CDN URL, so redirects must be followed. Safe here because the URL
    # comes from a hardcoded manifest, never from user input (unlike the
    # deliberately non-following clients in agentos.tools.builtin).
    async with httpx.AsyncClient(
        trust_env=_trust_env(),
        timeout=_DOWNLOAD_TIMEOUT_S,
        follow_redirects=True,
    ) as client:
        for remote_path in manifest.files:
            name = _flattened_name(remote_path)
            final_path = target_dir / name
            if final_path.exists() and final_path.stat().st_size > 0:
                logger.info("model_download.skip_existing", model_id=model_id, file=name)
                continue

            url = manifest.base_url + remote_path
            part_path = target_dir / f"{name}.part"
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total = response.headers.get("content-length")
                total_bytes = int(total) if total is not None else None
                done_bytes = 0
                with part_path.open("wb") as fh:
                    async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
                        fh.write(chunk)
                        done_bytes += len(chunk)
                        if progress is not None:
                            progress(name, done_bytes, total_bytes)
            os.replace(part_path, final_path)
            logger.info("model_download.fetched", model_id=model_id, file=name)

    return target_dir
