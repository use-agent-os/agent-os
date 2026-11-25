"""Ticketed, connection-bound download URLs for artifacts and attachments.

A client on the WebSocket asks ``artifacts.download`` for a file and gets
``/api/v1/artifacts/download/<conn_id>/<ticket>`` back: 32 random bytes,
valid for five minutes, at most 128 live per connection, gone when that
connection closes. The URL needs no ``Authorization`` header -- the grant is
the credential -- so an ``<img>``, an ``<audio>`` or a plain link can load it,
and the desktop no longer has to fetch with a header and hand over a blob.

The route re-checks the grant and the file on every request, answers
``HEAD`` and a single byte range, serves ``?variant=thumbnail`` for still
images (PNG, at most 1200 px on the longest side), and sets headers that
keep a served file from acting as a page: ``Content-Security-Policy:
default-src 'none'; sandbox``, ``Content-Disposition: attachment``,
``Cache-Control: no-store`` and ``X-Content-Type-Options: nosniff``.
"""

from __future__ import annotations

import hmac
import io
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import anyio
import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import Send

from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.config import GatewayConfig
from agentos.gateway.protocol import ErrorCode, ErrorDetailCode
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher

log = structlog.get_logger(__name__)

DOWNLOAD_PATH_PREFIX = "/api/v1/artifacts/download"
DOWNLOAD_TICKET_TTL_SECONDS = 300.0
MAX_GRANTS_PER_CONNECTION = 128
THUMBNAIL_MAX_EDGE = 1200
#: Formats a thumbnail is made from. Animated and vector formats are not.
THUMBNAIL_SOURCE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
#: Connection ids that are not a WebSocket (HTTP RPC, channels): a grant
#: bound to one would never be revoked by a disconnect.
_NON_SOCKET_CONN_IDS = frozenset({"", "http", "_http", "channel", "system"})

GrantKind = Literal["artifact", "attachment", "workspace"]

SANDBOX_HEADERS: dict[str, str] = {
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@dataclass(frozen=True, slots=True)
class DownloadGrant:
    ticket: str
    conn_id: str
    kind: GrantKind
    target_id: str
    session_key: str
    expires_at: float
    name: str = ""
    mime: str = ""

    @property
    def path(self) -> str:
        return f"{DOWNLOAD_PATH_PREFIX}/{quote(self.conn_id, safe='')}/{self.ticket}"


class DownloadGrantRegistry:
    """In-memory grants: ``ticket -> grant``, indexed by connection."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DOWNLOAD_TICKET_TTL_SECONDS,
        max_per_connection: int = MAX_GRANTS_PER_CONNECTION,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max = int(max_per_connection)
        self._clock = clock
        self._grants: dict[str, DownloadGrant] = {}
        # Insertion order = mint order, so the oldest grant evicts first.
        self._by_conn: dict[str, OrderedDict[str, None]] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def mint(
        self,
        conn_id: str,
        kind: GrantKind,
        target_id: str,
        session_key: str,
        *,
        name: str = "",
        mime: str = "",
    ) -> DownloadGrant:
        now = self._clock()
        grant = DownloadGrant(
            ticket=secrets.token_urlsafe(32),
            conn_id=conn_id,
            kind=kind,
            target_id=target_id,
            session_key=session_key,
            expires_at=now + self._ttl,
            name=name,
            mime=mime,
        )
        with self._lock:
            self._sweep_locked(now)
            tickets = self._by_conn.setdefault(conn_id, OrderedDict())
            while len(tickets) >= self._max:
                oldest, _ = tickets.popitem(last=False)
                self._grants.pop(oldest, None)
            tickets[grant.ticket] = None
            self._grants[grant.ticket] = grant
        return grant

    def lookup(self, conn_id: str, ticket: str) -> DownloadGrant | None:
        """The live grant for ``ticket`` if it was minted for ``conn_id``."""
        now = self._clock()
        with self._lock:
            grant = self._grants.get(ticket)
            if grant is None:
                return None
            if grant.expires_at <= now:
                self._drop_locked(grant)
                return None
        if not hmac.compare_digest(grant.conn_id.encode(), conn_id.encode()):
            return None
        return grant

    def revoke_connection(self, conn_id: str) -> int:
        with self._lock:
            tickets = self._by_conn.pop(conn_id, OrderedDict())
            for ticket in tickets:
                self._grants.pop(ticket, None)
            return len(tickets)

    def live_count(self, conn_id: str | None = None) -> int:
        with self._lock:
            self._sweep_locked(self._clock())
            if conn_id is None:
                return len(self._grants)
            return len(self._by_conn.get(conn_id) or {})

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()
            self._by_conn.clear()

    def _drop_locked(self, grant: DownloadGrant) -> None:
        self._grants.pop(grant.ticket, None)
        tickets = self._by_conn.get(grant.conn_id)
        if tickets is not None:
            tickets.pop(grant.ticket, None)
            if not tickets:
                self._by_conn.pop(grant.conn_id, None)

    def _sweep_locked(self, now: float) -> None:
        for grant in [g for g in self._grants.values() if g.expires_at <= now]:
            self._drop_locked(grant)


_registry = DownloadGrantRegistry()


def get_download_grants() -> DownloadGrantRegistry:
    return _registry


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

_d = get_dispatcher()


def _param(params: dict[str, Any], *names: str) -> str:
    for name in names:
        value = params.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@_d.method("artifacts.download", CONTROL_ONLY)
async def _handle_artifacts_download(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Mint a ticketed download URL for an artifact or an attachment.

    Params: ``session_key``, then exactly one of ``artifact_id`` /
    ``attachment_sha256``; ``transport`` (only ``"http"``); for an
    attachment, optional display ``name`` and ``mime``. camelCase spellings
    are accepted. Returns ``url`` (gateway-relative), ``expiresAt`` (epoch
    ms), ``ttlSeconds``, ``kind``, ``name``, ``mime`` and ``size``.
    """
    from agentos.artifacts import ArtifactIntegrityError, ArtifactNotFoundError
    from agentos.gateway.artifacts import resolve_artifact_download
    from agentos.gateway.attachments import (
        AttachmentIntegrityError,
        AttachmentNotFoundError,
        resolve_attachment_download,
        safe_download_name,
        safe_media_type,
    )

    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    transport = _param(params, "transport") or "http"
    if transport != "http":
        raise ValueError("params.transport must be 'http'")
    conn_id = str(getattr(ctx, "conn_id", "") or "")
    if conn_id in _NON_SOCKET_CONN_IDS:
        raise ValueError("artifacts.download needs a WebSocket connection: the URL is bound to it")
    session_key = _param(params, "session_key", "sessionKey")
    if not session_key:
        raise ValueError("params.session_key is required")
    artifact_id = _param(params, "artifact_id", "artifactId")
    sha = _param(params, "attachment_sha256", "attachmentSha256").lower()
    if bool(artifact_id) == bool(sha):
        raise ValueError("exactly one of params.artifact_id / params.attachment_sha256")

    config = ctx.config
    session_manager = getattr(ctx, "session_manager", None)
    grants = get_download_grants()
    if artifact_id:
        try:
            ref, path = await resolve_artifact_download(
                config, session_manager, session_key, artifact_id
            )
        except ArtifactIntegrityError as exc:
            raise _integrity_error(exc) from exc
        except ArtifactNotFoundError as exc:
            raise RpcHandlerError(ErrorCode.NOT_FOUND, "artifact not found") from exc
        grant = grants.mint(conn_id, "artifact", ref.id, session_key, name=ref.name, mime=ref.mime)
        size = ref.size
    else:
        try:
            path = await resolve_attachment_download(config, session_manager, session_key, sha)
        except AttachmentIntegrityError as exc:
            raise _integrity_error(exc) from exc
        except AttachmentNotFoundError as exc:
            raise RpcHandlerError(ErrorCode.NOT_FOUND, "attachment not found") from exc
        grant = grants.mint(
            conn_id,
            "attachment",
            sha,
            session_key,
            name=safe_download_name(params.get("name")),
            mime=safe_media_type(params.get("mime")),
        )
        size = path.stat().st_size
    return {
        "url": grant.path,
        "kind": grant.kind,
        "name": grant.name,
        "mime": grant.mime,
        "size": size,
        "expiresAt": int(grant.expires_at * 1000),
        "ttlSeconds": int(grants.ttl_seconds),
    }


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


class _SingleRangeFileResponse(FileResponse):
    """A ``FileResponse`` that answers one byte range; several get the whole file."""

    async def _handle_multiple_ranges(  # type: ignore[override]
        self,
        send: Send,
        ranges: list[tuple[int, int]],
        file_size: int,
        send_header_only: bool,
    ) -> None:
        await self._handle_simple(send, send_header_only, False)


def content_disposition(name: str) -> str:
    """``attachment`` with an ASCII fallback and the RFC 5987 UTF-8 name."""
    cleaned = name.replace("\r", " ").replace("\n", " ") or "download"
    ascii_name = cleaned.encode("ascii", "ignore").decode().replace('"', "'") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(cleaned, safe='')}"


def _integrity_error(exc: Exception) -> RpcHandlerError:
    return RpcHandlerError(
        ErrorCode.INTERNAL_ERROR,
        str(exc),
        details={"code": ErrorDetailCode.INTEGRITY_ERROR.value},
    )


def _json_error(
    status: int, code: ErrorCode, message: str, detail: ErrorDetailCode | None = None
) -> JSONResponse:
    body: dict[str, Any] = {"error": message, "code": code.value}
    if detail is not None:
        body["details"] = {"code": detail.value}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _not_found() -> JSONResponse:
    # One answer for an unknown, expired, revoked or foreign ticket: the
    # route never tells a caller which of those it hit.
    return _json_error(404, ErrorCode.NOT_FOUND, "Download not found")


def make_thumbnail(path: Path, max_edge: int = THUMBNAIL_MAX_EDGE) -> bytes | None:
    """A PNG no larger than ``max_edge`` on its longest side, or None.

    ``None`` when the file is not a still PNG/JPEG/WebP image. The format is
    read from the bytes, never from the claimed mime.
    """
    from PIL import Image

    try:
        with Image.open(path) as source:
            if source.format not in THUMBNAIL_SOURCE_FORMATS:
                return None
            if getattr(source, "is_animated", False):
                return None
            source.thumbnail((max_edge, max_edge))
            image: Image.Image = source
            if image.mode not in {"RGB", "RGBA", "L", "LA"}:
                image = image.convert("RGBA")
            out = io.BytesIO()
            image.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def register_download_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
    registry: DownloadGrantRegistry | None = None,
    connection_alive: Callable[[str], bool] | None = None,
) -> None:
    """Register ``GET|HEAD /api/v1/artifacts/download/{conn_id}/{ticket}``."""
    from agentos.artifacts import ArtifactIntegrityError, ArtifactNotFoundError
    from agentos.gateway.artifacts import resolve_artifact_download
    from agentos.gateway.attachments import (
        AttachmentIntegrityError,
        AttachmentNotFoundError,
        resolve_attachment_download,
    )

    grants = registry or get_download_grants()

    def _alive(conn_id: str) -> bool:
        if connection_alive is not None:
            return connection_alive(conn_id)
        from agentos.gateway.websocket import get_registry

        return get_registry().get(conn_id) is not None

    async def download_handler(request: Request) -> Response:
        conn_id = str(request.path_params.get("conn_id", ""))
        ticket = str(request.path_params.get("ticket", ""))
        grant = grants.lookup(conn_id, ticket)
        if grant is None or not _alive(conn_id):
            return _not_found()
        if grant.kind == "workspace":
            return await _workspace_download(request, grant)
        try:
            if grant.kind == "artifact":
                ref, path = await resolve_artifact_download(
                    config, session_manager, grant.session_key, grant.target_id
                )
                mime, name = ref.mime, ref.name
            else:
                path = await resolve_attachment_download(
                    config, session_manager, grant.session_key, grant.target_id
                )
                mime, name = grant.mime or "application/octet-stream", grant.name
        except (ArtifactNotFoundError, AttachmentNotFoundError):
            return _not_found()
        except (ArtifactIntegrityError, AttachmentIntegrityError):
            # 409 as on the legacy ?sessionKey= routes; the reason is typed.
            return _json_error(
                409,
                ErrorCode.INTERNAL_ERROR,
                "Download integrity check failed",
                ErrorDetailCode.INTEGRITY_ERROR,
            )

        headers = dict(SANDBOX_HEADERS)
        variant = request.query_params.get("variant", "")
        if variant == "thumbnail":
            thumbnail = await anyio.to_thread.run_sync(make_thumbnail, path)
            if thumbnail is None:
                return _json_error(
                    415,
                    ErrorCode.INVALID_REQUEST,
                    "No thumbnail for this file",
                    ErrorDetailCode.UNSUPPORTED_MEDIA_TYPE,
                )
            stem = name.rsplit(".", 1)[0] if "." in name else name
            headers["Content-Disposition"] = content_disposition(f"{stem or 'image'}-thumb.png")
            body = b"" if request.method == "HEAD" else thumbnail
            headers["Content-Length"] = str(len(thumbnail))
            return Response(body, media_type="image/png", headers=headers)
        if variant:
            return _json_error(400, ErrorCode.INVALID_REQUEST, f"Unknown variant {variant!r}")
        headers["Content-Disposition"] = content_disposition(name)
        return _SingleRangeFileResponse(path, media_type=mime, headers=headers)

    async def _workspace_download(request: Request, grant: DownloadGrant) -> Response:
        """A workspace image or PDF: re-resolved under the browser's rules.

        The bytes are read through the descriptor the checks ran on and
        answered from memory (the kind's size cap bounds them), so the file
        cannot be swapped for a link between the check and the send.
        """
        from agentos.gateway.rpc_workspace import workspace_root
        from agentos.gateway.workspace_files import (
            WorkspaceFileError,
            parse_relative_path,
            read_download,
        )

        if request.query_params.get("variant"):
            return _json_error(400, ErrorCode.INVALID_REQUEST, "Unknown variant")
        try:
            parts = parse_relative_path(grant.target_id)
            root = workspace_root(config, grant.session_key or "main")
            found = await anyio.to_thread.run_sync(read_download, root, parts)
        except WorkspaceFileError:
            return _not_found()
        headers = dict(SANDBOX_HEADERS)
        headers["Content-Disposition"] = content_disposition(found.name)
        headers["Content-Length"] = str(found.size)
        body = b"" if request.method == "HEAD" else found.data
        return Response(body, media_type=found.mime, headers=headers)

    async def contained(request: Request) -> Response:
        # Reachable without a token: an unexpected failure is logged by type
        # and redacted message, never as a traceback whose frames print the
        # config (and its secrets).
        try:
            return await download_handler(request)
        except Exception as exc:  # noqa: BLE001 - see comment
            from agentos.redact import redact_sensitive_text

            log.warning(
                "gateway.download_failed",
                error_type=type(exc).__name__,
                error=(redact_sensitive_text(str(exc)) or "")[:300],
            )
            return _json_error(500, ErrorCode.INTERNAL_ERROR, "Download failed")

    app.router.routes.append(
        Route(
            f"{DOWNLOAD_PATH_PREFIX}/{{conn_id}}/{{ticket}}",
            contained,
            methods=["GET", "HEAD"],
        )
    )


__all__ = [
    "DOWNLOAD_PATH_PREFIX",
    "DOWNLOAD_TICKET_TTL_SECONDS",
    "MAX_GRANTS_PER_CONNECTION",
    "SANDBOX_HEADERS",
    "DownloadGrant",
    "DownloadGrantRegistry",
    "content_disposition",
    "get_download_grants",
    "make_thumbnail",
    "register_download_routes",
]
