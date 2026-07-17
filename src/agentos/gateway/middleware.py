"""Middleware pipeline: Auth, RateLimit, ErrorHandling, SecurityHeaders."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp

from agentos.gateway.config import GatewayConfig
from agentos.gateway.scopes import is_loopback_address

# Endpoints that carry no credentials and expose no admin surface; exempt from
# both token auth and the cross-origin guard. Kept module-level so the origin
# guard (defined before AuthMiddleware) and AuthMiddleware share one source.
_PUBLIC_PATHS = frozenset({"/health", "/healthz", "/ready", "/readyz"})

# The RPC/API surface the cross-origin guard exists to protect. A UI base_path
# that overlaps this must NOT be trusted as an exemption prefix.
_API_PREFIX = "/api"


def _safe_ui_exempt_prefix(base_path: str) -> str | None:
    """Return the Control UI prefix safe to exempt from the Origin guard, or None.

    The UI shell/static routes are served content, not RPC sinks, so exempting
    them is fine — but only when the prefix is a real, non-empty path that does
    not overlap ``/api``. ``base_path="/"`` (normalized to "") or ``"/api"``
    would otherwise wholesale-exempt every request, disabling the guard; those
    fail closed to None (the shell is a top-level navigation and sends no
    Origin, so gating it costs nothing).
    """
    prefix = base_path.rstrip("/")
    if not prefix:
        return None
    if prefix == _API_PREFIX or prefix.startswith(_API_PREFIX + "/"):
        return None
    return prefix


class LoopbackHostMiddleware(BaseHTTPMiddleware):
    """Reject requests whose ``Host`` header is neither loopback nor allowlisted.

    A drop-in replacement for Starlette's ``TrustedHostMiddleware`` that
    parses the ``Host`` header with ``urlsplit`` so **bracketed IPv6** hosts
    (``[::1]:18791``) compare correctly — Starlette's ``split(":")[0]``
    mangles those and would 400 a legitimate IPv6-loopback bind.

    This is the DNS-rebinding guard: when the check is active (loopback
    binds), any literal loopback ``Host`` — the shared
    ``scopes.is_loopback_address`` predicate, i.e. all of ``127.0.0.0/8``,
    ``::1``, ``localhost``, IPv4-mapped forms; exactly the binds the startup
    guard blesses — is admitted, plus the extra hostnames in
    ``allowed_hosts``. A page that rebinds a hostname to ``127.0.0.1`` still
    carries its foreign hostname in ``Host`` and is rejected. ``["*"]``
    disables the check (public binds, already auth-gated).
    """

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        super().__init__(app)
        self._allow_any = "*" in allowed_hosts
        # Normalize allowlist to bare lowercased hosts (strip scheme/port/brackets).
        self._allowed = {self._normalize(h) for h in allowed_hosts if h != "*"}

    @staticmethod
    def _normalize(host: str) -> str:
        value = host.strip().lower()
        # urlsplit needs a scheme-relative authority to parse host:port / [ipv6].
        parsed = urlsplit(f"//{value}") if "//" not in value else urlsplit(value)
        return (parsed.hostname or value.strip("[]")).rstrip(".")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if self._allow_any:
            return await call_next(request)  # type: ignore[no-any-return]
        raw_host = request.headers.get("host", "")
        try:
            host = self._normalize(raw_host)
        except ValueError:
            host = ""
        if host in self._allowed or is_loopback_address(host):
            return await call_next(request)  # type: ignore[no-any-return]
        # A non-loopback Host on a loopback bind is usually DNS rebinding, but
        # it can also be a legitimate user reaching the gateway via a custom
        # hostname (e.g. an /etc/hosts alias to 127.0.0.1). Name the config key
        # that unblocks that case instead of a bare rejection.
        return PlainTextResponse(
            f"Rejected Host header {host!r}: only loopback hosts are accepted on a "
            "loopback bind (DNS-rebinding guard). If you reach this gateway via a "
            "custom hostname, add its origin to control_ui.allowed_origins.",
            status_code=400,
        )


class LoopbackOriginMiddleware(BaseHTTPMiddleware):
    """Reject cross-site browser requests to the HTTP surface on a loopback bind.

    The sibling of the WS handshake Origin guard (``is_allowed_ws_origin``),
    covering the same browser threat over plain HTTP: with
    ``auth.mode="none"`` a loopback peer is granted operator scopes and the
    default CORS posture (``["*"]`` + credentials) reflects any page Origin,
    so a malicious page in the victim's browser could otherwise ``fetch()``
    the same admin RPC surface the WS guard protects (``/api/config``,
    ``/api/sessions``, ``/api/chat``) and read the responses.

    Requests without an ``Origin`` header (CLI, curl, browser navigations)
    pass through untouched. A browser request carries the page origin, which
    must be loopback or explicitly allowed — ``control_ui.allowed_origins``
    or a non-wildcard ``cors.allowed_origins`` entry (deliberate operator
    intent; the wildcard default is exactly the posture this guard fences).
    On a non-loopback bind the check is a no-op, matching the WS guard: the
    gateway only starts there with auth on or an explicit opt-in.

    Public paths are exempt, mirroring ``AuthMiddleware.PUBLIC_PATHS`` and the
    Control UI prefix: health probes carry no credentials and no admin surface,
    and the Control UI shell is a served page (guarded by CSP /
    ``SecurityHeadersMiddleware``), not an RPC sink. Only the ``/api/*`` and
    RPC surface — the drive-by target — is gated.
    """

    def __init__(
        self, app: ASGIApp, config: GatewayConfig, bind_is_loopback: bool
    ) -> None:
        super().__init__(app)
        self._config = config
        self._cors_origins = [o for o in config.cors.allowed_origins if o != "*"]
        self._ui_prefix = _safe_ui_exempt_prefix(config.control_ui.base_path)
        # Passed in from create_gateway_app, computed eagerly at build time.
        # Starlette instantiates BaseHTTPMiddleware lazily (first request), so
        # capturing here would read an already-mutated config.host; the caller
        # captures the posture before config.apply can change it (P2).
        self._bind_is_loopback = bind_is_loopback

    def _is_ui_path(self, path: str) -> bool:
        if self._ui_prefix is None:
            return False
        # Exact shell ("/control") or anything under it ("/control/..."),
        # never a bare-prefix match that would swallow sibling routes.
        return path == self._ui_prefix or path.startswith(self._ui_prefix + "/")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in _PUBLIC_PATHS or self._is_ui_path(path):
            return await call_next(request)  # type: ignore[no-any-return]
        origin = request.headers.get("origin")
        if origin:
            # Shared predicate with the WS handshake guard (one threat model,
            # one allowlist). Local import: websocket.py pulls the RPC stack.
            from agentos.gateway.websocket import (
                is_allowed_ws_origin,
                origin_in_allowlist,
            )

            if not (
                is_allowed_ws_origin(
                    origin, self._config, bind_is_loopback=self._bind_is_loopback
                )
                or origin_in_allowlist(origin, self._cors_origins)
            ):
                return PlainTextResponse("Origin not allowed", status_code=403)
        return await call_next(request)  # type: ignore[no-any-return]


class AuthMiddleware(BaseHTTPMiddleware):
    """Token-based auth middleware. Skips public paths."""

    PUBLIC_PATHS = _PUBLIC_PATHS

    def __init__(self, app: ASGIApp, config: GatewayConfig) -> None:
        super().__init__(app)
        self._config = config

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip auth for public endpoints and WebSocket upgrades (WS handles own auth)
        if request.url.path in self.PUBLIC_PATHS or request.url.path.startswith(
            self._config.control_ui.base_path
        ):
            return await call_next(request)  # type: ignore[no-any-return]

        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)  # type: ignore[no-any-return]

        auth_mode = self._config.auth.mode
        if auth_mode == "none":
            return await call_next(request)  # type: ignore[no-any-return]

        if auth_mode == "token":
            token = self._extract_token(request)
            if token != self._config.auth.token:
                return JSONResponse(
                    {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                )

        elif auth_mode == "trusted-proxy":
            proxy = self._config.auth.trusted_proxy
            forwarded_for = request.headers.get("x-forwarded-for", "")
            if proxy and proxy not in forwarded_for:
                return JSONResponse(
                    {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                )

        return await call_next(request)  # type: ignore[no-any-return]

    def _extract_token(self, request: Request) -> str | None:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        token_header = request.headers.get("x-agentos-token")
        if token_header:
            return token_header
        return request.query_params.get("token")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple sliding-window rate limiter per client IP."""

    def __init__(self, app: ASGIApp, config: GatewayConfig) -> None:
        super().__init__(app)
        self._config = config
        # {ip: [(timestamp, count), ...]}
        self._windows: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self._config.rate_limit.enabled:
            return await call_next(request)  # type: ignore[no-any-return]

        # Exempt the Control UI shell + static assets from per-IP rate limiting.
        # The SPA pulls ~30 small files on every page load (CSS, JS, fonts);
        # without this exemption a couple of refreshes from a single LAN device
        # blows past the API bucket and the operator sees a hard 429 on the
        # bare HTML. Mutating endpoints under /api/* are still limited.
        base = self._config.control_ui.base_path
        path = request.url.path
        if base and (path == base or path.startswith(f"{base}/")):
            return await call_next(request)  # type: ignore[no-any-return]
        if request.method == "GET" and path == "/api/approvals":
            return await call_next(request)  # type: ignore[no-any-return]

        client_ip = self._get_client_ip(request)
        now = time.time()
        window = self._config.rate_limit.window_seconds
        max_req = self._config.rate_limit.max_requests

        # Prune old timestamps
        self._windows[client_ip] = [t for t in self._windows[client_ip] if now - t < window]

        if len(self._windows[client_ip]) >= max_req:
            return JSONResponse(
                {"error": "Too Many Requests", "code": "RATE_LIMITED"}, status_code=429
            )

        self._windows[client_ip].append(now)
        return await call_next(request)  # type: ignore[no-any-return]

    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions and return structured JSON errors."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)  # type: ignore[no-any-return]
        except Exception as exc:
            return JSONResponse(
                {"error": str(exc), "code": "INTERNAL_ERROR"},
                status_code=500,
            )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject security headers (CSP, X-Frame-Options, etc.) on Control UI routes."""

    def __init__(self, app: ASGIApp, path_prefix: str = "/control") -> None:
        super().__init__(app)
        self._path_prefix = path_prefix

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)  # type: ignore[assignment]
        if request.url.path.startswith(self._path_prefix):
            response.headers["content-security-policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self' ws: wss:; "
                "font-src 'self';"
            )
            response.headers["x-frame-options"] = "DENY"
            response.headers["x-content-type-options"] = "nosniff"
            response.headers["referrer-policy"] = "strict-origin-when-cross-origin"
        return response
