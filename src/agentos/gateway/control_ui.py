"""Control UI route factory — serves the built React console with SPA fallback."""

from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agentos import __version__
from agentos.gateway.config import GatewayConfig

# Vite fingerprints production assets, so successful asset responses can be
# cached permanently. The SPA shell remains no-store below so every navigation
# discovers the current asset names.
_STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
_INDEX_CACHE_CONTROL = "no-store"
_CONTROL_BASE_TAG = re.compile(
    r"<base\b(?=[^>]*\bdata-agentos-control-base\b)[^>]*>",
    flags=re.IGNORECASE,
)


def _is_fingerprinted_asset_path(path: str) -> bool:
    """Classify mounted asset paths independently of the host path separator."""
    normalized = path.replace("\\", "/").lstrip("/")
    return normalized.startswith("assets/")


class _CachedStaticFiles(StaticFiles):
    """StaticFiles subclass that attaches Cache-Control to 200 responses.

    Subclassing rather than middleware-wrapping keeps the path scoped to the
    /static mount only. Range (206) and conditional-GET (304) flows pass
    through unchanged so browsers' Last-Modified / ETag logic continues
    working.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return PlainTextResponse(
                    "Not Found",
                    status_code=404,
                    headers={"Cache-Control": _INDEX_CACHE_CONTROL},
                )
            raise
        if response.status_code == 200:
            if not _is_fingerprinted_asset_path(path):
                # Top-level files have stable URLs (index.html, pre-paint theme
                # bootstrap, and the license ledger), so they must not survive
                # an upgrade in cache.
                response.headers["Cache-Control"] = _INDEX_CACHE_CONTROL
            elif not os.environ.get("AGENTOS_STATIC_NO_CACHE"):
                response.headers.setdefault("Cache-Control", _STATIC_CACHE_CONTROL)
        return response


_DIST_DIR = Path(__file__).parent / "static" / "dist"

# Keep the existing bootstrap version contract for RPC consumers.
_TEMPLATE_VERSION_SUFFIX = str(int(time.time()))


def _request_ws_url(request: Request, config: GatewayConfig) -> str:
    """Build the browser-facing websocket URL from the current request."""
    host = request.headers.get("host") or f"{config.host}:{config.port}"
    if config.host in {"0.0.0.0", "::"} and host == "testserver":
        host = f"127.0.0.1:{config.port}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    ws_scheme = "wss" if scheme == "https" else "ws"
    return f"{ws_scheme}://{host}/ws"


def _build_bootstrap_context(config: GatewayConfig, request: Request) -> dict:
    """Build the public bootstrap payload consumed by the React application."""
    return {
        "version": f"{__version__}+{_TEMPLATE_VERSION_SUFFIX}",
        "ws_url": _request_ws_url(request, config),
        "auth_mode": config.auth.mode,
        "base_path": config.control_ui.base_path,
        "config_path": config.config_path or "",
        "features": {
            "diagnostics": config.diagnostics_enabled,
        },
    }


def _render_spa_shell(base: str) -> str:
    """Load the built SPA shell and inject its configured runtime asset base."""
    index_path = _DIST_DIR / "index.html"
    try:
        source = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Control UI production bundle is missing") from exc

    if len(_CONTROL_BASE_TAG.findall(source)) != 1:
        raise RuntimeError("Control UI production bundle has an invalid base marker")

    control_base = base or "/"
    asset_base = f"{base}/static/dist/" if base else "/static/dist/"
    base_tag = (
        '<base data-agentos-control-base="'
        f'{html.escape(control_base, quote=True)}" '
        f'href="{html.escape(asset_base, quote=True)}">'
    )
    return _CONTROL_BASE_TAG.sub(base_tag, source, count=1)


def _unavailable_response() -> HTMLResponse:
    """Return an actionable response when a source checkout has not built the UI."""
    body = """\
<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>AgentOS Control UI unavailable</title></head>
  <body>
    <main>
      <h1>Control UI assets are unavailable</h1>
      <p>Build the production frontend, then restart the gateway:</p>
      <pre>python scripts/build_control_ui.py build</pre>
    </main>
  </body>
</html>
"""
    return HTMLResponse(
        body,
        status_code=503,
        headers={"Cache-Control": _INDEX_CACHE_CONTROL},
    )


def create_control_ui_routes(config: GatewayConfig) -> list[Route | Mount]:
    """Create routes for the Control UI. Returns empty list if disabled."""
    if not config.control_ui.enabled:
        return []

    base = config.control_ui.base_path

    async def serve_index(_request: Request) -> HTMLResponse:
        try:
            shell = _render_spa_shell(base)
        except RuntimeError:
            return _unavailable_response()
        return HTMLResponse(
            shell,
            headers={"Cache-Control": _INDEX_CACHE_CONTROL},
        )

    async def serve_bootstrap(request: Request) -> JSONResponse:
        ctx = _build_bootstrap_context(config, request)
        return JSONResponse(ctx, headers={"Cache-Control": _INDEX_CACHE_CONTROL})

    routes: list[Route | Mount] = []
    if _DIST_DIR.is_dir():
        routes.append(
            Mount(
                f"{base}/static/dist",
                app=_CachedStaticFiles(directory=str(_DIST_DIR)),
                name="control_ui_dist",
            )
        )
    routes.extend(
        [
            Route(f"{base}/api/bootstrap", serve_bootstrap, methods=["GET"]),
            Route(f"{base}/{{path:path}}", serve_index, methods=["GET"]),
            Route(f"{base}/", serve_index, methods=["GET"]),
        ]
    )
    return routes
