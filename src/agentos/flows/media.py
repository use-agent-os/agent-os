"""Content-addressed image blobs for flows: ``<root>/media/<sha256>.<ext>``.

Clients never read these files directly; ``flows.media`` returns the bytes
(or a thumbnail) over RPC. Pillow decodes dimensions, makes thumbnails and
downscales inputs before they are sent to a provider.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
from pathlib import Path
from typing import Any

from agentos.flows.types import FlowError, ImageRef, is_sha256

#: Largest blob accepted from an upload or a provider.
MAX_IMAGE_BYTES = 40 * 1024 * 1024
#: Inputs are fitted into this box before they are sent to a provider.
MAX_INPUT_SIDE = 2048

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def _open_image(data: bytes) -> Any:
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def sniff_image(data: bytes) -> tuple[str, int, int]:
    """``(mime, width, height)`` of encoded image *data*; raises ``ValueError``."""
    try:
        image = _open_image(data)
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise ValueError(f"not a readable image: {exc}") from exc
    mime = _MIME_BY_FORMAT.get(str(image.format or "").upper())
    if mime is None:
        raise ValueError(f"unsupported image format {image.format!r}")
    return mime, int(image.width), int(image.height)


def downscale(data: bytes, mime: str, max_side: int = MAX_INPUT_SIDE) -> tuple[bytes, str]:
    """Fit an encoded image into ``max_side`` x ``max_side`` (aspect kept)."""
    try:
        image = _open_image(data)
    except Exception:  # noqa: BLE001 - leave what we cannot decode untouched
        return data, mime
    if image.width <= max_side and image.height <= max_side:
        return data, mime
    image.thumbnail((max_side, max_side))
    return _encode(image, prefer=mime)


def _encode(image: Any, *, prefer: str) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    if prefer == "image/jpeg" and not has_alpha:
        image.convert("RGB").save(buffer, format="JPEG", quality=90)
        return buffer.getvalue(), "image/jpeg"
    if image.mode not in ("RGB", "RGBA", "L", "LA"):
        image = image.convert("RGBA" if has_alpha else "RGB")
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue(), "image/png"


class MediaStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    def _path_for(self, sha: str, mime: str) -> Path:
        return self.root / f"{sha}.{_EXT_BY_MIME.get(mime, 'bin')}"

    def find(self, sha: str) -> Path | None:
        if not is_sha256(sha):
            return None
        for ext in _EXT_BY_MIME.values():
            candidate = self.root / f"{sha.lower()}.{ext}"
            if candidate.is_file():
                return candidate
        return None

    def exists(self, sha: str) -> bool:
        return self.find(sha) is not None

    def put(self, data: bytes, mime: str | None = None) -> ImageRef:
        """Store *data* (idempotent) and describe it. Raises ``ValueError``."""
        if not data:
            raise ValueError("empty image")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        sniffed, width, height = sniff_image(data)
        del mime  # the bytes decide, not the caller's label
        sha = hashlib.sha256(data).hexdigest()
        path = self._path_for(sha, sniffed)
        with self._lock:
            if not path.is_file():
                self.root.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
                tmp.write_bytes(data)
                os.replace(tmp, path)
        return ImageRef(sha256=sha, mime=sniffed, width=width, height=height, bytes=len(data))

    def read(self, sha: str) -> tuple[bytes, str]:
        path = self.find(sha)
        if path is None:
            raise FlowError("flows.not_found", f"image {sha} is not in the media store")
        ext = path.suffix.lstrip(".")
        mime = next((m for m, e in _EXT_BY_MIME.items() if e == ext), "application/octet-stream")
        return path.read_bytes(), mime

    def ref(self, sha: str) -> ImageRef:
        data, _mime = self.read(sha)
        mime, width, height = sniff_image(data)
        return ImageRef(sha256=sha.lower(), mime=mime, width=width, height=height, bytes=len(data))

    def thumbnail(self, sha: str, max_side: int) -> tuple[bytes, str, int, int]:
        """``(data, mime, width, height)`` fitted into ``max_side``; cached on disk."""
        max_side = max(16, min(int(max_side), 4096))
        data, mime = self.read(sha)
        _, width, height = sniff_image(data)
        if width <= max_side and height <= max_side:
            return data, mime, width, height
        for ext, thumb_mime in (("png", "image/png"), ("jpg", "image/jpeg")):
            cached = self.thumbs_dir / f"{sha}_{max_side}.{ext}"
            if cached.is_file():
                thumb = cached.read_bytes()
                _, tw, th = sniff_image(thumb)
                return thumb, thumb_mime, tw, th
        image = _open_image(data)
        image.thumbnail((max_side, max_side))
        thumb, thumb_mime = _encode(image, prefer="image/jpeg")
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)
        ext = "png" if thumb_mime == "image/png" else "jpg"
        (self.thumbs_dir / f"{sha}_{max_side}.{ext}").write_bytes(thumb)
        return thumb, thumb_mime, int(image.width), int(image.height)

    def delete(self, sha: str) -> None:
        path = self.find(sha)
        if path is not None:
            path.unlink(missing_ok=True)
        if self.thumbs_dir.is_dir():
            for thumb in self.thumbs_dir.glob(f"{sha}_*"):
                thumb.unlink(missing_ok=True)


def to_png(data: bytes) -> bytes:
    """Re-encode any supported image as PNG (keeps alpha)."""
    image = _open_image(data)
    if str(image.format or "").upper() == "PNG":
        return data
    encoded, _ = _encode(image, prefer="image/png")
    return encoded
