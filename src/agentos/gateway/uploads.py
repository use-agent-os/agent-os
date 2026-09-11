"""Bridge upload endpoint + in-memory store for the file_uuid path.

The store maps an opaque ``file_uuid`` to the bytes of an uploaded file.
A ``.meta`` marker file is written to disk at insert time so that, after
a gateway restart, ``get(file_uuid)`` for an entry whose bytes are gone
returns the specific :class:`AttachmentLostInRestartError` instead of the
generic :class:`AttachmentNotFoundError`. That lets clients show
"uploaded file lost in restart, please re-upload" instead of
"unknown uuid".

Per-uuid ``asyncio.Lock`` protects the resolver/sweeper race: both the
resolver and the sweeper acquire the lock; the sweeper skips locked uuids
and retries on the next sweep tick. Refcounting was rejected as more state
under cancellation. The explicit eviction hook is wired into
``rpc_sessions._handle_sessions_send`` and fires only on the success
path after ``start_turn_via_runtime`` returns (locked semantic).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Message, Receive

from agentos.contracts.attachments import (
    ALLOWED_MEDIA_TYPES,
    attachment_size_limit_for_mime,
    normalize_attachment_mime,
)
from agentos.gateway.auth import token_matches
from agentos.gateway.config import GatewayConfig

log = logging.getLogger(__name__)


_ALLOWED_MIMES: frozenset[str] = ALLOWED_MEDIA_TYPES

_DEFAULT_MAX_FILE_BYTES = 30 * 1024 * 1024
_DEFAULT_TTL_SECONDS = 10 * 60

# Multipart framing (boundaries, part headers, sibling text fields) inflates
# the request body past the file bytes themselves. Allow for it so a body that
# is exactly at the cap is not refused on its Content-Length alone, while an
# obviously abusive body still never reaches the parser.
_MULTIPART_FRAMING_ALLOWANCE = 64 * 1024


class UploadStoreError(Exception):
    """Base class for upload-store-specific errors."""


class UploadOversizeError(UploadStoreError):
    pass


class UploadUnsupportedMimeError(UploadStoreError):
    pass


class AttachmentNotFoundError(UploadStoreError):
    """The uuid is unknown to this store (never inserted, or already swept)."""


class AttachmentLostInRestartError(UploadStoreError):
    """The marker exists on disk but the in-memory bytes are gone.

    Concrete UX hook for "uploaded file lost in restart".
    """


class RequestBodyTooLargeError(Exception):
    """The request body exceeded the byte budget granted to its endpoint."""


# ---------------------------------------------------------------------------
# Request-size guards, shared with the audio transcription route.
# ---------------------------------------------------------------------------


def declared_content_length(request: Request) -> int | None:
    """Return the declared body size, or ``None`` when absent or unparseable.

    A missing or malformed header means "unknown", never zero: chunked and
    lying clients must fall through to the streaming guard below.
    """

    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def bounded_request(request: Request, limit: int) -> Request:
    """Return a view of ``request`` whose body stream stops after ``limit``.

    Starlette's ``max_part_size`` only guards non-file parts -- a file part
    streams unbounded into a ``SpooledTemporaryFile`` before the handler runs
    (``MultiPartParser.on_part_data`` skips the check when the part has a
    file). Raising out of ``receive`` aborts the parse while the body is still
    arriving, so an abusive upload never lands on disk in full either.
    """

    remaining = limit

    async def receive() -> Message:
        nonlocal remaining
        message = await request.receive()
        if message.get("type") == "http.request":
            remaining -= len(message.get("body", b""))
            if remaining < 0:
                raise RequestBodyTooLargeError(f"request body exceeds {limit} bytes")
        return message

    return Request(request.scope, cast(Receive, receive))


@dataclass
class _Entry:
    name: str
    mime: str
    sha256: str
    size: int
    bytes: bytes
    expires_at: float


class UploadStore:
    """Bridge upload store: in-memory bytes + on-disk ``.meta`` markers.

    The store enforces the configured size and MIME caps so a malicious
    or buggy client cannot smuggle disallowed bytes past it. The route
    handler MAY duplicate those checks but MUST NOT skip them.
    """

    def __init__(
        self,
        marker_dir: Path | None,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.marker_dir: Path | None = Path(marker_dir) if marker_dir is not None else None
        self.ttl_seconds = ttl_seconds
        self.max_file_bytes = max_file_bytes
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_for_locks = asyncio.Lock()
        if self.marker_dir is not None:
            self.marker_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers

    async def _get_uuid_lock(self, file_uuid: str) -> asyncio.Lock:
        async with self._lock_for_locks:
            lock = self._locks.get(file_uuid)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[file_uuid] = lock
            return lock

    def _marker_path(self, file_uuid: str) -> Path | None:
        if self.marker_dir is None:
            return None
        root = self.marker_dir.resolve()
        path = (self.marker_dir / f"{file_uuid}.meta").resolve()
        if path.parent != root:
            # file_uuid crosses RPC boundaries unvalidated; a traversal shape
            # ("../x", an absolute path — pathlib replaces the base) must never
            # resolve outside marker_dir. Markers are flat direct children.
            raise ValueError(f"file_uuid {file_uuid!r} escapes marker_dir")
        return path

    def _read_marker(self, file_uuid: str) -> dict[str, Any] | None:
        path = self._marker_path(file_uuid)
        if path is None or not path.exists():
            return None
        try:
            return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    def _marker_expired(self, marker: dict[str, Any]) -> bool:
        expires_at = marker.get("expires_at")
        if not isinstance(expires_at, (int, float, str)):
            return False
        try:
            return float(expires_at) < self._now()
        except ValueError:
            return False

    def _write_marker(self, file_uuid: str, meta: dict[str, Any]) -> None:
        path = self._marker_path(file_uuid)
        if path is None:
            return
        try:
            path.write_text(json.dumps(meta), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - filesystem failure path
            log.warning("uploads.marker_write_failed uuid=%s err=%s", file_uuid, exc)

    def _delete_marker(self, file_uuid: str) -> None:
        path = self._marker_path(file_uuid)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass

    def _now(self) -> float:
        return time.time()

    # ------------------------------------------------------------------ public

    async def put(self, name: str, mime: str, payload: bytes) -> str:
        """Insert a new attachment; return its opaque file_uuid."""

        normalized_mime = normalize_attachment_mime(mime)
        if normalized_mime not in _ALLOWED_MIMES:
            raise UploadUnsupportedMimeError(f"mime {mime!r} is not allowed")
        mime_limit = attachment_size_limit_for_mime(normalized_mime, staged=True)
        max_bytes = min(self.max_file_bytes, mime_limit)
        if len(payload) > max_bytes:
            raise UploadOversizeError(
                f"upload exceeds {max_bytes} byte cap for {normalized_mime} "
                f"(got {len(payload)})"
            )

        file_uuid = f"u-{_uuid.uuid4().hex}"
        sha = hashlib.sha256(payload).hexdigest()
        expires_at = self._now() + self.ttl_seconds
        entry = _Entry(
            name=name,
            mime=normalized_mime,
            sha256=sha,
            size=len(payload),
            bytes=payload,
            expires_at=expires_at,
        )

        # Sweep before insert so the eviction loop runs at least once per
        # successful put (no background thread needed).
        await self._sweep_expired_locked()

        lock = await self._get_uuid_lock(file_uuid)
        async with lock:
            self._entries[file_uuid] = entry
            self._write_marker(
                file_uuid,
                {
                    "sha256": sha,
                    "mime": normalized_mime,
                    "name": name,
                    "size": len(payload),
                    "expires_at": expires_at,
                },
            )
        return file_uuid

    async def get(self, file_uuid: str) -> tuple[bytes, dict[str, Any]]:
        """Return ``(bytes, metadata)`` for an active uuid; raise otherwise."""

        await self._sweep_expired_locked()
        lock = await self._get_uuid_lock(file_uuid)
        async with lock:
            entry = self._entries.get(file_uuid)
            if entry is None:
                marker = self._read_marker(file_uuid)
                if marker is not None and self._marker_expired(marker):
                    self._delete_marker(file_uuid)
                    raise AttachmentNotFoundError(file_uuid)
                if marker is not None:
                    raise AttachmentLostInRestartError(file_uuid)
                raise AttachmentNotFoundError(file_uuid)
            if entry.expires_at < self._now():
                self._entries.pop(file_uuid, None)
                self._delete_marker(file_uuid)
                raise AttachmentNotFoundError(file_uuid)
            return entry.bytes, {
                "name": entry.name,
                "mime": entry.mime,
                "sha256": entry.sha256,
                "size": entry.size,
            }

    async def evict(self, file_uuid: str) -> bool:
        """Explicit eviction; returns True if the entry existed."""

        lock = await self._get_uuid_lock(file_uuid)
        async with lock:
            existed = file_uuid in self._entries
            self._entries.pop(file_uuid, None)
            self._delete_marker(file_uuid)
        async with self._lock_for_locks:
            self._locks.pop(file_uuid, None)
        return existed

    async def _sweep_expired_locked(self) -> int:
        now = self._now()
        expired = [u for u, e in list(self._entries.items()) if e.expires_at < now]
        if not expired:
            return 0
        count = 0
        removed: list[str] = []
        for u in expired:
            lock = self._locks.get(u)
            # Skip-without-blocking: if the lock is held a resolver/upload
            # is in flight; this pass leaves it for the next sweep tick.
            if lock is not None and lock.locked():
                continue
            self._entries.pop(u, None)
            self._delete_marker(u)
            removed.append(u)
            count += 1
        if removed:
            async with self._lock_for_locks:
                for u in removed:
                    lock = self._locks.get(u)
                    if lock is not None and not lock.locked():
                        self._locks.pop(u, None)
        return count


# ---------------------------------------------------------------------------
# HTTP route registration.
# ---------------------------------------------------------------------------


def _extract_authorization_token(request: Request) -> str | None:
    """Header-only token extraction.

    Like all gateway endpoints, the multipart upload endpoint requires
    header-based token auth (``Authorization: Bearer <token>`` or
    ``x-agentos-token: <token>``) and rejects query-string tokens.
    """

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.headers.get("x-agentos-token")


def _too_large(max_bytes: int, *, mime: str | None = None) -> JSONResponse:
    """Build the shared 413 body used by every upload size guard."""

    suffix = f" for {mime}" if mime else ""
    return JSONResponse(
        {"error": f"upload exceeds {max_bytes} byte cap{suffix}", "code": "TOO_LARGE"},
        status_code=413,
    )


def register_upload_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    store: UploadStore,
) -> None:
    """Register POST /api/v1/files/upload on the given Starlette app."""

    async def upload_handler(request: Request) -> JSONResponse:
        if config.auth.mode == "token":
            if config.auth.token and not token_matches(
                _extract_authorization_token(request), config.auth.token
            ):
                return JSONResponse(
                    {
                        "error": (
                            "Authorization header (Bearer …) required for "
                            "/api/v1/files/upload"
                        ),
                        "code": "UNAUTHORIZED",
                    },
                    status_code=401,
                )

        # The per-mime cap is only known once the part headers are parsed, so
        # bound the body by the store's absolute ceiling first and tighten it
        # below. Without this the whole body spools to disk before any check.
        stream_budget = store.max_file_bytes + _MULTIPART_FRAMING_ALLOWANCE
        declared = declared_content_length(request)
        if declared is not None and declared > stream_budget:
            return _too_large(store.max_file_bytes)

        try:
            form = await bounded_request(request, stream_budget).form()
        except RequestBodyTooLargeError:
            return _too_large(store.max_file_bytes)
        except Exception as exc:
            return JSONResponse(
                {"error": f"multipart/form-data required: {exc}"}, status_code=400
            )

        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                {"error": "missing 'file' multipart field"}, status_code=400
            )

        filename = getattr(upload, "filename", None) or "attachment"
        content_type = getattr(upload, "content_type", None) or form.get("mime") or ""
        if not isinstance(content_type, str) or not content_type:
            return JSONResponse(
                {"error": "missing or invalid 'mime' / content-type"}, status_code=400
            )
        normalized_mime = normalize_attachment_mime(content_type)
        if normalized_mime is None:
            return JSONResponse(
                {"error": "missing or invalid 'mime' / content-type"}, status_code=400
            )

        # Read one byte past the cap: enough to detect an oversize part, never
        # enough to materialise it. ``store.put`` re-checks as defence in depth.
        cap = min(
            store.max_file_bytes,
            attachment_size_limit_for_mime(normalized_mime, staged=True),
        )
        payload = await upload.read(cap + 1)
        if not isinstance(payload, bytes) or len(payload) == 0:
            return JSONResponse(
                {"error": "empty upload"}, status_code=400
            )
        if len(payload) > cap:
            return _too_large(cap, mime=normalized_mime)

        try:
            file_uuid = await store.put(filename, normalized_mime, payload)
        except UploadOversizeError as exc:
            return JSONResponse({"error": str(exc), "code": "TOO_LARGE"}, status_code=413)
        except UploadUnsupportedMimeError as exc:
            return JSONResponse(
                {"error": str(exc), "code": "UNSUPPORTED_MEDIA_TYPE"}, status_code=415
            )

        return JSONResponse(
            {
                "file_uuid": file_uuid,
                "filename": filename,
                "mime": normalized_mime,
                "size": len(payload),
            }
        )

    app.router.routes.append(
        Route("/api/v1/files/upload", upload_handler, methods=["POST"])
    )


# ---------------------------------------------------------------------------
# Singleton accessor.
# ---------------------------------------------------------------------------


_default_store: UploadStore | None = None


def get_upload_store() -> UploadStore:
    """Return the process-global upload store, lazily constructed.

    Tests that need a clean store should pass a fresh ``UploadStore`` to the
    function under test; production code that just wants the default reaches
    in via this accessor.
    """

    global _default_store
    if _default_store is None:
        _default_store = UploadStore(marker_dir=None)
    return _default_store


def set_upload_store(store: UploadStore | None) -> None:
    """Override the singleton (production wiring + test reset)."""

    global _default_store
    _default_store = store
