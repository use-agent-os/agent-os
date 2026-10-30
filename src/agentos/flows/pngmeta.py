"""The ``agentos-flow`` PNG text chunk: a saved image carries the graph that made it.

Written by the Save node as an ``iTXt`` chunk holding JSON
``{"flow_id", "rev", "run_id", "graph"}``; read back by ``flows.import_png`` so
dropping an output onto the canvas restores the flow. Only the flow document
goes in — it never holds keys or tokens.
"""

from __future__ import annotations

import io
import json
from typing import Any

CHUNK_KEY = "agentos-flow"


def embed_flow_metadata(png: bytes, metadata: dict[str, Any]) -> bytes:
    """Return *png* re-saved with the ``agentos-flow`` chunk set to *metadata*."""
    from PIL import Image, PngImagePlugin

    image = Image.open(io.BytesIO(png))
    image.load()
    info = PngImagePlugin.PngInfo()
    # Keep any other text chunks the image already had, minus a stale copy of ours.
    for key, value in (getattr(image, "text", None) or {}).items():
        if key != CHUNK_KEY and isinstance(value, str):
            info.add_text(key, value)
    payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    info.add_itxt(CHUNK_KEY, payload, zip=True)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def read_flow_metadata(data: bytes) -> dict[str, Any] | None:
    """The ``agentos-flow`` JSON of an image, or ``None`` (not a PNG, no chunk, bad JSON)."""
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:  # noqa: BLE001 - any undecodable input simply has no metadata
        return None
    if str(image.format or "").upper() != "PNG":
        return None
    text = getattr(image, "text", None) or {}
    raw = text.get(CHUNK_KEY) if isinstance(text, dict) else None
    if raw is None:
        raw = image.info.get(CHUNK_KEY)
    if not isinstance(raw, str | bytes):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
