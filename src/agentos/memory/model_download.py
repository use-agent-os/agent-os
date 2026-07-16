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
            "onnx/model_quantized.onnx",
            "onnx/model_quantized.onnx_data",
            "tokenizer.json",
            "tokenizer_config.json",
            "config.json",
            "special_tokens_map.json",
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
    """Return the target dir for ``model_id`` if it exists and has an ONNX file.

    Returns ``None`` when the model is unknown, the directory does not exist,
    or the directory exists but contains no ``*.onnx`` file (e.g. a partial or
    stale download).
    """

    manifest = EMBEDDING_MODEL_MANIFESTS.get(model_id)
    if manifest is None:
        return None
    target = user_models_dir() / manifest.target_dirname
    if not target.is_dir():
        return None
    if next(target.glob("*.onnx"), None) is None:
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

    async with httpx.AsyncClient(trust_env=_trust_env(), timeout=_DOWNLOAD_TIMEOUT_S) as client:
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
