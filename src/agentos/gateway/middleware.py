"""Middleware pipeline: Auth, RateLimit, ErrorHandling, SecurityHeaders."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import urlsplit

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp

from agentos.gateway.access import is_loopback_address, peer_is_trusted_proxy, resolve_client_ip
from agentos.gateway.auth import token_matches
from agentos.gateway.auth_rate_limit import TOKEN_SCOPE, AuthGuard, retry_after_seconds
from agentos.gateway.config import GatewayConfig
from agentos.gateway.origin_policy import (
    OriginPolicy,
    build_origin_policy,
    request_is_forwarded,
)
from agentos.gateway.protocol import ErrorCode, ErrorDetailCode

log = structlog.get_logger(__name__)

# Endpoints that carry no credentials and expose no Control surface; exempt from
# both token auth and the cross-origin guard. Kept module-level so the origin
# guard (defined before AuthMiddleware) and AuthMiddleware share one source.
_PUBLIC_PATHS = frozenset({"/health", "/healthz", "/ready", "/readyz", "/startupz"})

# Routes that authenticate themselves with a credential other than the gateway
# token, so AuthMiddleware lets them through to their own check: a ticketed
# download carries a short-lived, connection-bound grant in its path, and the
# inbound webhooks take the separate ``[hooks] token`` (never the gateway token).
_SELF_AUTHENTICATED_PREFIXES: tuple[str, ...] = ("/api/v1/artifacts/download/", "/api/v1/hooks/")

# The RPC/API surface the cross-origin guard exists to protect. A UI base_path
# that overlaps this must NOT be trusted as an exemption prefix.
_API_PREFIX = "/api"

# The one JSON route under the Control UI prefix that must answer before the
# console holds a token: the SPA fetches it to learn its WS URL and auth mode.
# Everything else under ``{base_path}/api/`` is Control surface and is treated
# like the root ``/api/*`` routes.
_UI_BOOTSTRAP_SUFFIX = f"{_API_PREFIX}/bootstrap"

# The approval queue the Control UI polls. Rate-limited in its own per-IP
# bucket (see ``RateLimitMiddleware._bucket_for``) rather than shared with the
# rest of ``/api/*``.
_APPROVALS_PATH = f"{_API_PREFIX}/approvals"


def _is_under(prefix: str, path: str) -> bool:
    """True for the prefix itself or anything below it — never a bare-prefix match.

    ``/control`` and ``/control/chat`` match ``/control``; ``/controlpanel``
    does not, so a sibling route can never be swallowed by the exemption.
    """
    return path == prefix or path.startswith(prefix + "/")


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
    """Reject requests whose ``Host`` header names a host this gateway never declared.

    A drop-in replacement for Starlette's ``TrustedHostMiddleware`` that
    parses the ``Host`` header with ``urlsplit`` so **bracketed IPv6** hosts
    (``[::1]:18791``) compare correctly — Starlette's ``split(":")[0]``
    mangles those and would 400 a legitimate IPv6-loopback bind.

    This is the DNS-rebinding guard. A page that rebinds a hostname to the
    gateway's address still carries its foreign hostname in ``Host`` and is
    rejected. Admitted: every literal loopback ``Host`` (the shared
    ``access.is_loopback_address`` predicate — all of ``127.0.0.0/8``,
    ``::1``, ``localhost``, IPv4-mapped forms), the hostnames of the
    allowlisted origins (``allowed_hosts``), and — off loopback, where the
    operator chose to be reachable — any IP literal (rebinding needs a name)
    and private names (``.local``, ``.ts.net``); see
    ``OriginPolicy.host_allowed``. ``["*"]`` disables the check.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: list[str],
        policy: OriginPolicy | None = None,
    ) -> None:
        super().__init__(app)
        self._allow_any = "*" in allowed_hosts
        # Normalize allowlist to bare lowercased hosts (strip scheme/port/brackets).
        self._allowed = {self._normalize(h) for h in allowed_hosts if h != "*"}
        self._policy = policy

    @staticmethod
    def _normalize(host: str) -> str:
        value = host.strip().lower()
        # urlsplit needs a scheme-relative authority to parse host:port / [ipv6].
        parsed = urlsplit(f"//{value}") if "//" not in value else urlsplit(value)
        return (parsed.hostname or value.strip("[]")).rstrip(".")

    def _host_allowed(self, host: str) -> bool:
        if host in self._allowed:
            return True
        if self._policy is not None:
            return self._policy.host_allowed(host)
        return is_loopback_address(host)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if self._allow_any:
            return await call_next(request)  # type: ignore[no-any-return]
        raw_host = request.headers.get("host", "")
        try:
            host = self._normalize(raw_host)
        except ValueError:
            host = ""
        if self._host_allowed(host):
            return await call_next(request)  # type: ignore[no-any-return]
        # A foreign Host is usually DNS rebinding, but it can also be a
        # legitimate user reaching the gateway via a custom hostname (an
        # /etc/hosts alias, a LAN DNS name). Name the config keys that unblock
        # that case instead of a bare rejection.
        return PlainTextResponse(
            f"Rejected Host header {host!r}: this gateway only answers to loopback, its "
            "own addresses and declared origins (DNS-rebinding guard). If you reach it "
            "via this hostname, add its origin to control_ui.allowed_origins or set "
            "[gateway] public_origin.",
            status_code=400,
        )


class LoopbackOriginMiddleware(BaseHTTPMiddleware):
    """Reject cross-site browser requests to the HTTP surface, on every bind.

    The sibling of the WS handshake Origin guard (``is_allowed_ws_origin``),
    covering the same browser threat over plain HTTP: with
    ``auth.mode="none"`` a loopback peer is admitted to Control, so a
    malicious page in the victim's browser could otherwise ``fetch()`` the
    Control RPC surface the WS guard protects (``/api/config``,
    ``/api/sessions``, ``/api/chat``) and read the responses. Both guards
    apply one ``OriginPolicy``: the allowlist (``control_ui.allowed_origins``,
    explicit ``cors.allowed_origins`` entries, ``[gateway] public_origin``,
    the seeded loopback origins), private same-origin, and a loopback origin
    on a local socket. Before it, a non-loopback bind skipped this check.

    Requests without an ``Origin`` header (CLI, curl, browser navigations)
    pass through untouched.

    Public paths are exempt, mirroring ``AuthMiddleware.PUBLIC_PATHS`` and the
    Control UI prefix: health probes carry no credentials and no Control surface,
    and the Control UI shell is a served page (guarded by CSP /
    ``SecurityHeadersMiddleware``), not an RPC sink. Only the ``/api/*`` and
    RPC surface — the drive-by target — is gated.

    The UI prefix exemption stops at ``{base_path}/api/`` (``_is_ui_path``
    below): the shell and its fingerprinted assets are top-level navigations
    and subresource loads, but the JSON routes mounted under the UI prefix
    (``/control/api/bootstrap``) are fetch-only and therefore exactly the
    drive-by target this guard exists for. Un-exempting the whole ``/api/``
    subtree rather than that one path keeps future JSON routes gated by
    default (#351).
    """

    def __init__(
        self,
        app: ASGIApp,
        config: GatewayConfig,
        bind_is_loopback: bool,
        policy: OriginPolicy | None = None,
    ) -> None:
        super().__init__(app)
        self._config = config
        self._ui_prefix = _safe_ui_exempt_prefix(config.control_ui.base_path)
        # Passed in from create_gateway_app, computed eagerly at build time.
        # Starlette instantiates BaseHTTPMiddleware lazily (first request), so
        # capturing here would read an already-mutated config.host; the caller
        # captures the posture before config.apply can change it (P2).
        self._bind_is_loopback = bind_is_loopback
        self._policy = policy or build_origin_policy(config, bind_is_loopback=bind_is_loopback)

    def _is_ui_path(self, path: str) -> bool:
        if self._ui_prefix is None:
            return False
        if not _is_under(self._ui_prefix, path):
            return False
        # ...except the JSON surface mounted under the prefix: a cross-origin
        # page cannot read the shell it navigates to, but it *can* read
        # fetch("/control/api/bootstrap"). Those stay gated.
        return not _is_under(self._ui_prefix + _API_PREFIX, path)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in _PUBLIC_PATHS or self._is_ui_path(path):
            return await call_next(request)  # type: ignore[no-any-return]
        origin = request.headers.get("origin")
        if origin:
            decision = self._policy.evaluate(
                origin,
                host_header=request.headers.get("host"),
                peer_ip=request.client.host if request.client else None,
                forwarded=request_is_forwarded(request.headers),
            )
            if not decision.allowed:
                log.info("gateway.origin_rejected", origin=origin, path=path)
                return PlainTextResponse("Origin not allowed", status_code=403)
        return await call_next(request)  # type: ignore[no-any-return]


def request_client_ip(request: Request, config: GatewayConfig) -> str:
    """The client a request is attributed to: the transport peer, or the
    ``X-Forwarded-For`` client when the peer is a trusted proxy (walked right
    to left; see ``access.resolve_client_ip``)."""
    peer_ip = request.client.host if request.client else None
    return resolve_client_ip(
        peer_ip, request.headers.get("x-forwarded-for"), config.auth.trusted_proxy
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Token-based auth middleware. Skips public paths.

    In token mode a presented wrong token counts against the failed-auth
    budget shared with the WebSocket handshake (``auth_guard``); a client
    over it is answered ``429`` with ``Retry-After`` until the lockout ends.
    A request with no token is a plain ``401`` and never consumes budget.
    """

    PUBLIC_PATHS = _PUBLIC_PATHS

    def __init__(
        self,
        app: ASGIApp,
        config: GatewayConfig,
        control_ui_base_path: str | None = None,
        auth_guard: AuthGuard | None = None,
    ) -> None:
        super().__init__(app)
        self._config = config
        self._auth_guard = auth_guard
        base_path = (
            config.control_ui.base_path if control_ui_base_path is None else control_ui_base_path
        )
        self._ui_prefix = _safe_ui_exempt_prefix(base_path)
        # Modes already reported by the fail-closed branch. A gateway stuck in
        # that posture would otherwise log once per request.
        self._reported_unsupported_modes: set[str] = set()

    def _is_ui_path(self, path: str) -> bool:
        """True for the served Control UI surface that carries no credentials.

        The shell and its assets are exempt. The JSON surface under
        ``{base_path}/api/`` is not — it is Control surface and gets the same
        token gate as the root ``/api/*`` routes — with one carve-out for
        ``/api/bootstrap``, which the console must read before it has a token.
        """
        if self._ui_prefix is None:
            return False
        if not _is_under(self._ui_prefix, path):
            return False
        if path == self._ui_prefix + _UI_BOOTSTRAP_SUFFIX:
            return True
        return not _is_under(self._ui_prefix + _API_PREFIX, path)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip auth for public endpoints. WebSocket handshakes never reach a
        # BaseHTTPMiddleware (websocket scopes bypass it and authenticate in
        # websocket.py), so an ``Upgrade`` header on an http scope is just a
        # client-controlled header and must never exempt a request.
        if request.url.path in self.PUBLIC_PATHS or self._is_ui_path(request.url.path):
            return await call_next(request)  # type: ignore[no-any-return]

        if request.url.path.startswith(_SELF_AUTHENTICATED_PREFIXES):
            return await call_next(request)  # type: ignore[no-any-return]

        auth_mode = self._config.auth.mode
        if auth_mode == "none":
            return await call_next(request)  # type: ignore[no-any-return]

        if auth_mode == "token":
            token = self._extract_token(request)
            limiter = self._auth_guard.limiter if self._auth_guard is not None else None
            if limiter is None or not token:
                if not token_matches(token, self._config.auth.token):
                    return JSONResponse(
                        {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                    )
            else:
                client_ip = request_client_ip(request, self._config)
                # A browser request (it carries Origin) is also charged to the
                # address's browser budget, which is never loopback-exempt, as
                # on the WebSocket: a page in the operator's browser reaches
                # the gateway from loopback too.
                origin = request.headers.get("origin") or None
                decision = limiter.check(client_ip, TOKEN_SCOPE, origin=origin)
                if not decision.allowed:
                    return _auth_rate_limited(decision.retry_after_ms)
                if not token_matches(token, self._config.auth.token):
                    decision = limiter.record_failure(client_ip, TOKEN_SCOPE, origin=origin)
                    if decision.delay_s > 0:
                        await asyncio.sleep(decision.delay_s)
                    return JSONResponse(
                        {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                    )
                limiter.record_success(client_ip, TOKEN_SCOPE, origin=origin)

        elif auth_mode == "trusted-proxy":
            # A trusted-proxy deployment means a reverse proxy in front of the
            # gateway terminates TLS and sets X-Forwarded-For. Admission must
            # require that the *real transport peer* is one of the trusted
            # proxies — NOT that some client-supplied header merely *contains*
            # the proxy name. The old check (``proxy not in forwarded_for``) was
            # a substring match: any client could send ``X-Forwarded-For: <proxy>``
            # and pass.
            peer_ip = request.client.host if request.client else None
            if not self._is_trusted_proxy(peer_ip):
                return JSONResponse(
                    {"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401
                )
            # Peer IS a trusted proxy — X-Forwarded-For is exactly what
            # trusted-proxy mode exists to consume (nginx / Caddy / ALB all
            # set it on every forwarded request). The same ``_is_trusted_proxy``
            # check gates downstream consumption of the header in
            # ``RateLimitMiddleware._get_client_ip``, so a spoofed XFF from a
            # non-trusted peer can never be honored.

        else:
            # Fail closed on any mode without an enforcement branch above
            # (#352). ``AuthConfig`` rejects those at validation time, but this
            # middleware reads the config object live — a runtime mutation must
            # never fall through to an unauthenticated pass, which is exactly
            # how ``auth.mode="password"`` silently admitted every request.
            if auth_mode not in self._reported_unsupported_modes:
                self._reported_unsupported_modes.add(auth_mode)
                log.warning("gateway.auth.unsupported_mode", mode=auth_mode)
            return JSONResponse({"error": "Unauthorized", "code": "UNAUTHORIZED"}, status_code=401)

        return await call_next(request)  # type: ignore[no-any-return]

    def _is_trusted_proxy(self, peer_ip: str | None) -> bool:
        return peer_is_trusted_proxy(self._config.auth.trusted_proxy, peer_ip)

    def _extract_token(self, request: Request) -> str | None:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        return request.headers.get("x-agentos-token")


def _auth_rate_limited(retry_after_ms: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": "Too many failed authentication attempts",
            "code": ErrorCode.RATE_LIMITED.value,
            "details": {
                "code": ErrorDetailCode.AUTH_RATE_LIMITED.value,
                "retry_after_ms": retry_after_ms,
            },
        },
        status_code=429,
        headers={"Retry-After": str(retry_after_seconds(retry_after_ms))},
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple sliding-window rate limiter per client IP.

    Requests are counted in named buckets. Everything shares the ``"api"``
    bucket except ``GET /api/approvals``, which gets its own ``"approvals"``
    bucket with a higher cap (``rate_limit.approvals_max_requests``) so the
    Control UI's 1.5s poll cannot exhaust the budget the rest of the API
    depends on — and so that poll cannot be used to enumerate pending tool
    calls without limit either.
    """

    def __init__(
        self,
        app: ASGIApp,
        config: GatewayConfig,
        control_ui_base_path: str | None = None,
        max_tracked_clients: int = 10_000,
    ) -> None:
        super().__init__(app)
        self._config = config
        base_path = (
            config.control_ui.base_path if control_ui_base_path is None else control_ui_base_path
        )
        self._ui_prefix = _safe_ui_exempt_prefix(base_path)
        # {(bucket, ip): [timestamp, ...]} with LRU eviction ordering. An IP
        # that both polls approvals and calls the rest of the API therefore
        # holds two entries; ``max_tracked_clients`` bounds entries, not IPs.
        self._windows: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._max_tracked_clients = max_tracked_clients
        self._last_sweep: float = 0.0

    def _is_ui_path(self, path: str) -> bool:
        if self._ui_prefix is None:
            return False
        if not _is_under(self._ui_prefix, path):
            return False
        if path == self._ui_prefix + _UI_BOOTSTRAP_SUFFIX:
            return True
        return not _is_under(self._ui_prefix + _API_PREFIX, path)

    def _is_trusted_proxy(self, peer_ip: str | None) -> bool:
        return peer_is_trusted_proxy(self._config.auth.trusted_proxy, peer_ip)

    def _get_client_ip(self, request: Request) -> str:
        return request_client_ip(request, self._config)

    def _sweep_expired(self, now: float, window: float) -> None:
        self._last_sweep = now
        expired = [
            key
            for key, timestamps in self._windows.items()
            if not timestamps or (now - timestamps[-1] >= window)
        ]
        for key in expired:
            self._windows.pop(key, None)

    def _evict_excess(self, now: float, window: float) -> None:
        self._sweep_expired(now, window)
        while len(self._windows) > self._max_tracked_clients:
            self._windows.popitem(last=False)

    def _bucket_for(self, request: Request, path: str) -> tuple[str, int]:
        """Return the ``(bucket name, max requests)`` this request counts against.

        Only the polled approvals read gets the dedicated bucket. ``HEAD`` is
        included because Starlette serves it from the same ``methods=["GET"]``
        route, so a HEAD runs the handler — and therefore the SQLite read — in
        full; charging it elsewhere would leave the handler reachable
        ``max_requests`` extra times per window on top of the advertised cap.
        The mutating approval routes (``/api/approvals/resolve``,
        ``/api/approvals/settings``) are not polled and stay on the shared cap.
        """
        if request.method in ("GET", "HEAD") and path == _APPROVALS_PATH:
            return "approvals", self._config.rate_limit.approvals_max_requests
        return "api", self._config.rate_limit.max_requests

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self._config.rate_limit.enabled:
            return await call_next(request)  # type: ignore[no-any-return]

        # Exempt the Control UI shell + static assets from per-IP rate limiting.
        # The SPA pulls ~30 small files on every page load (CSS, JS, fonts);
        # without this exemption a couple of refreshes from a single LAN device
        # blows past the API bucket and the operator sees a hard 429 on the
        # bare HTML. Mutating endpoints under /api/* are still limited.
        path = request.url.path
        if self._is_ui_path(path):
            return await call_next(request)  # type: ignore[no-any-return]

        bucket, max_req = self._bucket_for(request, path)
        client_ip = self._get_client_ip(request)
        key = (bucket, client_ip)
        now = time.time()
        window = float(self._config.rate_limit.window_seconds)
        sweep_interval = min(window, 60.0)

        # Periodic sweep of expired windows
        if now - self._last_sweep >= sweep_interval:
            self._sweep_expired(now, window)

        # Prune old timestamps
        timestamps = [t for t in self._windows.get(key, []) if now - t < window]

        if len(timestamps) >= max_req:
            self._windows[key] = timestamps
            self._windows.move_to_end(key)
            return JSONResponse(
                {"error": "Too Many Requests", "code": "RATE_LIMITED"}, status_code=429
            )

        timestamps.append(now)
        self._windows[key] = timestamps
        self._windows.move_to_end(key)

        if len(self._windows) > self._max_tracked_clients:
            self._evict_excess(now, window)

        return await call_next(request)  # type: ignore[no-any-return]


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions and return structured JSON errors."""

    def __init__(self, app: ASGIApp, debug: bool = False) -> None:
        super().__init__(app)
        self._debug = debug

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)  # type: ignore[no-any-return]
        except Exception as exc:
            error_id = secrets.token_hex(6)
            log.error(
                "gateway.unhandled_exception",
                error_id=error_id,
                path=request.url.path,
                method=request.method,
                error=str(exc),
                exc_info=True,
            )
            from agentos.redact import redact_sensitive_text

            if self._debug:
                return JSONResponse(
                    {
                        "error": redact_sensitive_text(str(exc)),
                        "code": "INTERNAL_ERROR",
                        "error_id": error_id,
                    },
                    status_code=500,
                )
            return JSONResponse(
                {
                    "error": "Internal server error",
                    "code": "INTERNAL_ERROR",
                    "error_id": error_id,
                },
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
            # Remote gateway profiles are a supported operator feature: the
            # browser may connect to a bootstrap-provided endpoint or the
            # explicit ``agentos.wsUrl`` override. CSP cannot enumerate that
            # user-selected authority ahead of time, so permit only the two
            # WebSocket schemes in addition to same-origin HTTP requests.
            response.headers["content-security-policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https://raw.githubusercontent.com; "
                "connect-src 'self' ws: wss:; "
                "font-src 'self';"
            )
            response.headers["x-frame-options"] = "DENY"
            response.headers["x-content-type-options"] = "nosniff"
            response.headers["referrer-policy"] = "strict-origin-when-cross-origin"
        return response
