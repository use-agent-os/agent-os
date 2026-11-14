"""Starlette ASGI application factory with routes and middleware."""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import structlog
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from agentos import __version__
from agentos.application.approval_events import approval_display_item
from agentos.gateway.approval_queue import get_approval_queue
from agentos.gateway.auth_rate_limit import AuthGuard
from agentos.gateway.config import GatewayConfig
from agentos.gateway.control_ui import create_control_ui_routes
from agentos.gateway.middleware import (
    AuthMiddleware,
    ErrorHandlingMiddleware,
    LoopbackHostMiddleware,
    LoopbackOriginMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from agentos.gateway.protocol import ErrorCode, error_category
from agentos.gateway.readiness import readiness_payload, startup_payload
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.websocket import handle_ws_connection

_start_time = time.time()

log = structlog.get_logger(__name__)

#: HTTP status for each canonical error category (``protocol.error_category``).
#: ``UNAUTHORIZED`` stays 403: an RPC refused on this surface is a
#: permission answer, the 401 for a missing/wrong token comes from
#: ``AuthMiddleware`` before any handler runs.
RPC_CATEGORY_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNAUTHORIZED: 403,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


def rpc_status_code(result: Any, default: int = 500) -> int:
    """HTTP status for a dispatcher result, from its code's category."""
    if result.error is None:
        return default
    category = error_category(result.error.code)
    if category is None:
        return default
    return RPC_CATEGORY_STATUS.get(category, default)


def resolve_trusted_hosts(config: GatewayConfig) -> list[str]:
    """Return the extra hostnames ``LoopbackHostMiddleware`` admits.

    Every literal loopback ``Host`` is admitted by the middleware itself via
    the shared ``access.is_loopback_address`` predicate — the same one the
    startup guard and the origin policy use — and off loopback the policy
    adds IP literals and private names (``OriginPolicy.host_allowed``). This
    list carries the hostnames of the declared origins on top:
    ``control_ui.allowed_origins``, explicit ``cors.allowed_origins`` entries
    and ``[gateway] public_origin`` (a reverse-proxied UI on another name). A
    DNS-rebinding page can use none of them: its request carries the
    attacker's foreign hostname in ``Host`` (CVE-2026-53869 class).

    Before the origin policy, a non-loopback bind answered ``["*"]`` and took
    any ``Host``; it now gets the same declared list.
    """
    from agentos.gateway.origin_policy import configured_origins, warn_unparseable_origins

    warn_unparseable_origins(config)
    hosts: list[str] = []
    for origin in configured_origins(config):
        try:
            hostname = urlsplit(origin).hostname
        except ValueError:
            hostname = None
        if hostname and hostname not in hosts:
            hosts.append(hostname)
    return hosts


def create_gateway_app(
    config: GatewayConfig,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    skill_loader: Any = None,
    cron_scheduler: Any = None,
    turn_runner: Any = None,
    task_runtime: Any = None,
    heartbeat_service: Any = None,
    heartbeat_loop: Any = None,
    agent_registry: Any = None,
    diagnostics_state: Any = None,
    memory_managers: dict[str, Any] | None = None,
    memory_stores: dict[str, Any] | None = None,
    memory_retrievers: dict[str, Any] | None = None,
    extra_routes: list[Route] | None = None,
) -> Starlette:
    """Build and return the Starlette ASGI application."""
    if diagnostics_state is None:
        from agentos.gateway.diagnostics import DiagnosticsState

        diagnostics_state = DiagnosticsState.from_config(config)

    dispatcher = get_dispatcher()
    # Failed-auth limiter and pre-auth socket budget, shared by the HTTP
    # middleware and the WebSocket handshake of this app.
    auth_guard = AuthGuard.from_config(config)
    _rpc_status_code = rpc_status_code

    # ── HTTP endpoint handlers ───────────────────────────────────────────────

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "status": "live"})

    async def metrics_endpoint(request: Request) -> Response:
        from agentos.observability.metrics import format_prometheus_metrics

        content = format_prometheus_metrics()
        return Response(
            content,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def root(request: Request) -> Response:
        if not config.control_ui.enabled:
            return JSONResponse(
                {
                    "ok": True,
                    "service": "agentos-gateway",
                    "version": __version__,
                    "control_ui": False,
                }
            )
        return RedirectResponse(url=f"{config.control_ui.base_path}/")

    async def ready(request: Request) -> JSONResponse:
        # ``{ready, state, failing}``: the supervisor contract (gateway/readiness.py).
        payload, status_code = readiness_payload(request.app.state)
        payload["uptime_ms"] = int((time.time() - _start_time) * 1000)
        return JSONResponse(payload, status_code=status_code)

    async def startupz(request: Request) -> JSONResponse:
        payload, status_code = startup_payload(request.app.state)
        return JSONResponse(payload, status_code=status_code)

    async def api_config(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "config.get", None, ctx)
        if result.ok:
            return JSONResponse(result.payload or {})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result))

    async def api_sessions(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "sessions.list", None, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"sessions": []})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result))

    async def api_chat(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "chat.send", body, ctx)
        if result.ok:
            return JSONResponse({"ok": True, **(result.payload or {})})
        return JSONResponse(
            {"error": result.error.message if result.error else "error"},
            status_code=_rpc_status_code(result, default=400),
        )

    async def api_system_status(request: Request) -> JSONResponse:
        uptime = int((time.time() - _start_time) * 1000)
        provider_name = None
        if provider_selector is not None:
            # Report the *configured* provider id (e.g. "openrouter"), not the
            # wire-protocol backend class. OpenAI-compatible providers
            # (openrouter / deepseek / gemini) are all served by OpenAIProvider,
            # so introspecting the instance would mislabel them as "openai".
            provider_name = getattr(provider_selector, "active_provider_id", None)
            if not provider_name:
                try:
                    p = provider_selector.resolve()
                    provider_name = getattr(p, "name", None) or type(p).__name__
                except Exception:
                    pass
        # Operator-visible failover health: an open breaker explains why turns
        # are running on a fallback provider rather than the configured one.
        breakers: list[dict[str, Any]] = []
        active_breaker: dict[str, Any] | None = None
        if provider_selector is not None:
            try:
                from agentos.provider.circuit_breaker import snapshot_payload

                breakers = snapshot_payload(getattr(provider_selector, "circuit_breaker", None))
                if provider_name:
                    active_breaker = next(
                        (row for row in breakers if row.get("provider") == provider_name),
                        None,
                    )
            except Exception:  # noqa: BLE001 - diagnostic surface only
                breakers = []
        return JSONResponse(
            {
                "version": __version__,
                "uptime_ms": uptime,
                "status": "running",
                "provider": provider_name,
                "auth_mode": config.auth.mode,
                "circuitBreaker": active_breaker,
                "circuitBreakers": breakers,
            }
        )

    async def api_usage(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "usage.status", None, ctx)
        if result.ok:
            # Merge breakdown from usage.cost into the status response
            cost_result = await dispatcher.dispatch("_http", "usage.cost", None, ctx)
            payload = result.payload or {}
            if cost_result.ok and cost_result.payload:
                payload["breakdown"] = cost_result.payload.get("breakdown", [])
                payload["totalSessions"] = payload.get("totalSessions", 0)
            return JSONResponse(payload)
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result))

    def _make_ctx(request: Request | None = None) -> RpcContext:
        from agentos.gateway.auth import denied_access, resolve_auth, resolve_http_auth

        # The same admission uploads and audio use (``auth.resolve_http_auth``),
        # against the transport peer: ``gateway run`` keeps uvicorn from
        # rewriting it (``boot.UVICORN_PEER_KWARGS``).
        access = (
            resolve_http_auth(config, request)
            if request is not None
            else resolve_auth(config, auth_params={}, client_kind="control", peer_ip=None)
        )
        if access is None:
            access = denied_access()
        else:
            from agentos.gateway.agent_surface import get_agent_surface

            agent_auth: dict[str, str] = {}
            if request is not None:
                agent_token = request.headers.get("x-agentos-agent-token")
                if agent_token:
                    agent_auth["agentToken"] = agent_token
                operator = request.headers.get("x-agentos-operator-secret")
                if operator:
                    agent_auth["operatorSecret"] = operator
            # The CLI's on-disk operator file proves the operator only on the
            # WebSocket handshake: nothing first-party sends it over HTTP, and
            # an agent's in-process HTTP tool could (gateway.agent_surface).
            access = get_agent_surface().attach(
                access,
                agent_auth,
                agent_process=_http_peer_is_agent_process(request),
                file_secret=False,
            )
        return _rpc_ctx(access)

    def _http_peer_is_agent_process(request: Request | None) -> bool:
        """Whether a process the gateway spawned sent *request* (Linux; else False).

        ``request.client`` is the transport peer (``gateway run`` turns
        uvicorn's ``X-Forwarded-For`` rewrite off), so a forwarding header the
        caller added does not hide an agent's process. Where a host embeds the
        app with that rewrite on, the claimed address matches no local socket
        and the other rules decide, as before.
        """
        from agentos.gateway.peer_process import peer_is_descendant

        if request is None or request.client is None:
            return False
        server = request.scope.get("server")
        return peer_is_descendant(
            (request.client.host, request.client.port),
            tuple(server) if isinstance(server, (tuple, list)) else None,
        )

    def _rpc_ctx(access: Any, conn_id: str = "http") -> RpcContext:
        return RpcContext(
            conn_id=conn_id,
            access=access,
            session_manager=session_manager,
            config=config,
            provider_selector=provider_selector,
            model_catalog=getattr(turn_runner, "_model_catalog", None),
            tool_registry=tool_registry,
            subscription_manager=subscription_manager,
            channel_manager=channel_manager,
            usage_tracker=usage_tracker,
            skill_loader=skill_loader,
            cron_scheduler=cron_scheduler,
            turn_runner=turn_runner,
            task_runtime=task_runtime,
            heartbeat_service=heartbeat_service,
            heartbeat_loop=heartbeat_loop,
            agent_registry=agent_registry,
            diagnostics_state=diagnostics_state,
            memory_managers=memory_managers or {},
            memory_stores=memory_stores or {},
            memory_retrievers=memory_retrievers or {},
        )

    async def api_rpc(request: Request) -> JSONResponse:
        """POST /api/v1/rpc — allowlisted RPC over HTTP (``gateway.http_rpc``)."""
        from agentos.gateway.http_rpc import handle_http_rpc

        return await handle_http_rpc(
            request,
            dispatcher=dispatcher,
            make_ctx=_make_ctx,
            status_for=_rpc_status_code,
        )

    async def api_channels_status(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "channels.status", None, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"channels": []})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result))

    async def api_channels_logout(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "channels.logout", body, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"ok": True})
        msg = result.error.message if result.error else "error"
        return JSONResponse({"error": msg}, status_code=_rpc_status_code(result, default=400))

    async def api_approvals(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "exec.approvals.get", None, ctx)
        if not result.ok:
            return JSONResponse(
                {"error": result.error.message if result.error else "error"},
                status_code=_rpc_status_code(result),
            )
        settings = result.payload or {}
        mode = settings.get("mode", "prompt")
        queue = get_approval_queue()
        # Enriched for UI display; ``approval.requested`` carries the same item.
        items = [approval_display_item(p, mode=mode) for p in queue.list_pending()]
        return JSONResponse(
            {
                "pending": items,
                "mode": mode,
                "allowPatterns": settings.get("allowPatterns", []),
                "denyPatterns": settings.get("denyPatterns", []),
            }
        )

    async def api_approvals_settings(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        mode = body.get("mode")
        if mode not in {"prompt", "auto-approve", "auto-deny"}:
            return JSONResponse(
                {"error": "mode must be prompt, auto-approve, or auto-deny"},
                status_code=400,
            )
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch(
            "_http",
            "exec.approvals.set",
            {
                "mode": mode,
                "allowPatterns": body.get("allowPatterns"),
                "denyPatterns": body.get("denyPatterns"),
            },
            ctx,
        )
        if not result.ok:
            return JSONResponse(
                {"error": result.error.message if result.error else "error"},
                status_code=_rpc_status_code(result),
            )
        queue = get_approval_queue()
        settings = queue.get_settings()
        return JSONResponse(
            {
                "mode": settings.mode,
                "allowPatterns": settings.allow_patterns,
                "denyPatterns": settings.deny_patterns,
            }
        )

    async def api_approvals_resolve(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        approval_id = body.get("id")
        approved = body.get("approved", False)
        namespace = body.get("namespace", "exec")
        if not approval_id:
            return JSONResponse({"error": "id is required"}, status_code=400)
        ctx = _make_ctx(request)
        method = "plugin.approval.resolve" if namespace == "plugin" else "exec.approval.resolve"
        resolve_params = {
            "id": approval_id,
            "approved": approved,
            "allowAlways": bool(body.get("allowAlways", False)),
            "rememberIntent": bool(body.get("rememberIntent", False)),
        }
        if namespace != "plugin" and body.get("elevatedMode") in ("on", "bypass", "full"):
            resolve_params["elevatedMode"] = body.get("elevatedMode")
        if namespace != "plugin" and isinstance(body.get("payload"), dict):
            resolve_params["payload"] = body["payload"]
        result = await dispatcher.dispatch(
            "_http",
            method,
            resolve_params,
            ctx,
        )
        if result.ok:
            return JSONResponse(result.payload or {"ok": True})
        return JSONResponse(
            {"error": result.error.message if result.error else "error"},
            status_code=_rpc_status_code(result),
        )

    async def api_elevated_mode(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        ctx = _make_ctx(request)
        if not ctx.access.admitted:
            return JSONResponse({"error": "control connection required"}, status_code=403)
        from agentos.gateway.agent_surface import recognized_agent

        if recognized_agent(ctx) is not None:
            # Elevating a session (and so auto-approving its pending rows) is
            # the user's decision, never the agent's (rpc_approvals.require_human).
            return JSONResponse({"error": "Not available to an agent"}, status_code=403)
        session_key = str(body.get("sessionKey") or body.get("session_key") or "").strip()
        if not session_key:
            return JSONResponse({"error": "sessionKey is required"}, status_code=400)
        raw_mode = body.get("mode")
        mode = None if raw_mode in (None, "", "off") else str(raw_mode)
        if mode not in (None, "on", "bypass", "full"):
            return JSONResponse(
                {"error": "mode must be off, on, bypass, or full"},
                status_code=400,
            )
        queue = get_approval_queue()
        try:
            queue.set_elevated_mode(session_key, mode)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        resolved_pending = 0
        if mode in ("bypass", "full"):
            resolved_pending = queue.resolve_pending_for_session(
                session_key,
                approved=True,
                elevated_mode=mode,
            )
        return JSONResponse(
            {
                "sessionKey": session_key,
                "mode": mode or "off",
                "resolvedPending": resolved_pending,
            }
        )

    # ── Agents / Cron HTTP endpoints ────────────────────────────────────────

    async def api_agents(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "agents.list", None, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"agents": []})
        return JSONResponse(
            {"error": result.error.message if result.error else "error"},
            status_code=_rpc_status_code(result),
        )

    async def api_cron(request: Request) -> JSONResponse:
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "cron.list", None, ctx)
        if result.ok:
            return JSONResponse(
                result.payload
                if isinstance(result.payload, list)
                else {"jobs": result.payload or []}
            )
        return JSONResponse({"jobs": []})

    # ── WebSocket handler ────────────────────────────────────────────────────

    async def api_chat_history(request: Request) -> JSONResponse:
        """GET /api/chat/history?sessionKey=xxx — return chat transcript."""
        session_key = request.query_params.get("sessionKey", "agent:main:webchat:default")
        params: dict[str, Any] = {"sessionKey": session_key}
        for key in ("limit", "before", "after", "includeCanonical", "includeSummaries"):
            if key in request.query_params:
                params[key] = request.query_params[key]
        ctx = _make_ctx(request)
        result = await dispatcher.dispatch("_http", "chat.history", params, ctx)
        if result.ok:
            return JSONResponse(result.payload or {"messages": []})
        return JSONResponse(
            {"error": result.error.message if result.error else "error"},
            status_code=_rpc_status_code(result),
        )

    # Capture the bind posture and the origin policy once, at app-build time.
    # config.apply mutates config.host in place without rebinding the live
    # socket, so the guards must not re-read it per request (P2).
    from agentos.gateway.access import is_loopback_bind
    from agentos.gateway.origin_policy import OriginPolicyCORSMiddleware, build_origin_policy

    bind_is_loopback = is_loopback_bind(config.host)
    origin_policy = build_origin_policy(config, bind_is_loopback=bind_is_loopback)

    async def ws_endpoint(ws: WebSocket) -> None:
        await handle_ws_connection(
            ws,
            config,
            dispatcher,
            session_manager,
            provider_selector=provider_selector,
            tool_registry=tool_registry,
            subscription_manager=subscription_manager,
            channel_manager=channel_manager,
            usage_tracker=usage_tracker,
            skill_loader=skill_loader,
            cron_scheduler=cron_scheduler,
            turn_runner=turn_runner,
            task_runtime=task_runtime,
            heartbeat_service=heartbeat_service,
            heartbeat_loop=heartbeat_loop,
            agent_registry=agent_registry,
            diagnostics_state=diagnostics_state,
            memory_managers=memory_managers,
            memory_stores=memory_stores,
            memory_retrievers=memory_retrievers,
            bind_is_loopback=bind_is_loopback,
            auth_guard=auth_guard,
            origin_policy=origin_policy,
        )

    # ── Routes ───────────────────────────────────────────────────────────────

    routes = [
        Route("/", root, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/healthz", health, methods=["GET"]),
        Route("/ready", ready, methods=["GET"]),
        Route("/readyz", ready, methods=["GET"]),
        Route("/startupz", startupz, methods=["GET"]),
        Route("/api/config", api_config, methods=["GET"]),
        Route("/api/sessions", api_sessions, methods=["GET"]),
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/chat/history", api_chat_history, methods=["GET"]),
        Route("/api/agents", api_agents, methods=["GET"]),
        Route("/api/cron", api_cron, methods=["GET"]),
        Route("/api/system/status", api_system_status, methods=["GET"]),
        Route("/api/usage", api_usage, methods=["GET"]),
        Route("/api/channels/status", api_channels_status, methods=["GET"]),
        Route("/api/channels/logout", api_channels_logout, methods=["POST"]),
        Route("/api/approvals", api_approvals, methods=["GET"]),
        Route("/api/approvals/settings", api_approvals_settings, methods=["POST"]),
        Route("/api/approvals/resolve", api_approvals_resolve, methods=["POST"]),
        Route("/api/elevated-mode", api_elevated_mode, methods=["POST"]),
        Route("/api/v1/rpc", api_rpc, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
    ]

    # ── Prometheus metrics endpoint ──────────────────────────────────────────
    if config.observability.metrics_enabled:
        metrics_path = config.observability.metrics_path or "/metrics"
        routes.append(Route(metrics_path, metrics_endpoint, methods=["GET"]))

    # ── Channel webhook routes (Slack, etc.) ──────────────────────────────
    if extra_routes:
        routes.extend(extra_routes)

    # ── Control UI routes ────────────────────────────────────────────────
    routes.extend(create_control_ui_routes(config))

    # ── Middleware ───────────────────────────────────────────────────────────

    middleware = [
        Middleware(ErrorHandlingMiddleware, debug=config.debug),
        # DNS-rebinding guard: reject Host headers naming a host this gateway
        # never declared (loopback, declared origins, and off loopback its own
        # addresses and private names).
        Middleware(
            LoopbackHostMiddleware,
            allowed_hosts=resolve_trusted_hosts(config),
            policy=origin_policy,
        ),
        # Cross-site browser guard for the HTTP surface: reject foreign page
        # Origins before CORS can reflect them (the same OriginPolicy as the WS
        # handshake guard, on every bind). No-op without an Origin header.
        Middleware(
            LoopbackOriginMiddleware,
            config=config,
            bind_is_loopback=bind_is_loopback,
            policy=origin_policy,
        ),
        # CORS reflects only origins the policy allows; never ``*``.
        Middleware(
            OriginPolicyCORSMiddleware,
            policy=origin_policy,
            allow_credentials=config.cors.allow_credentials,
            allow_methods=config.cors.allowed_methods,
            allow_headers=config.cors.allowed_headers,
        ),
        Middleware(
            RateLimitMiddleware,
            config=config,
            control_ui_base_path=config.control_ui.base_path,
        ),
        Middleware(SecurityHeadersMiddleware, path_prefix=config.control_ui.base_path),
        Middleware(
            AuthMiddleware,
            config=config,
            control_ui_base_path=config.control_ui.base_path,
            auth_guard=auth_guard,
        ),
    ]

    def _close_browser_sessions() -> None:
        """Release managed browsers when the server shuts down.

        ``agentos gateway stop`` sends SIGTERM, which uvicorn turns into an ASGI
        lifespan shutdown — so this is the hook that actually fires. A managed
        session owns a Chromium that otherwise outlives the gateway (an earlier
        build leaked one browser per run); attach sessions only disconnect, so
        the operator's own browser is never killed.
        """
        try:
            from agentos.tools.agent_browser import close_all_sessions

            close_all_sessions()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass

    def _record_clean_shutdown(_app: Starlette) -> None:
        """A SIGTERM ends here (uvicorn's graceful shutdown), not in ``close()``.

        Recording the boot as clean at this point keeps ``agentos gateway
        stop`` and a supervisor's stop out of the crash-loop breaker's count;
        only a process that dies without any shutdown is unclean.
        """
        tracker = getattr(_app.state, "boot_tracker", None)
        mark_clean = getattr(tracker, "mark_clean", None)
        if callable(mark_clean):
            try:
                mark_clean()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        # SIGTERM ends uvicorn without GatewayServer.close(); shutdown hooks
        # still get their turn (once per process, whichever path is first).
        from agentos.gateway.lifecycle_hooks import run_shutdown_hooks_once

        await run_shutdown_hooks_once(config, "lifespan")
        _close_browser_sessions()
        _record_clean_shutdown(_app)

    app = Starlette(
        routes=routes,
        middleware=middleware,
        debug=config.debug,
        lifespan=_lifespan,
    )
    app.state.diagnostics_state = diagnostics_state
    app.state.auth_guard = auth_guard
    app.state.origin_policy = origin_policy

    # Bridge upload endpoint: self-hosted multipart sink that
    # returns an opaque file_uuid the chat.send validator can resolve.
    from agentos.gateway.uploads import (  # noqa: PLC0415 — local import keeps app.py boot light
        get_upload_store,
        register_upload_routes,
    )

    register_upload_routes(app, config=config, store=get_upload_store())
    from agentos.gateway.artifacts import register_artifact_routes  # noqa: PLC0415
    from agentos.gateway.attachments import register_attachment_routes  # noqa: PLC0415
    from agentos.gateway.audio_transcription import (  # noqa: PLC0415
        register_audio_transcription_routes,
    )

    register_attachment_routes(
        app,
        config=config,
        session_manager=session_manager,
    )
    register_artifact_routes(
        app,
        config=config,
        session_manager=session_manager,
    )
    from agentos.gateway.download_grants import register_download_routes  # noqa: PLC0415

    register_download_routes(app, config=config, session_manager=session_manager)
    from agentos.gateway.inbound_hooks import register_inbound_hook_routes  # noqa: PLC0415

    async def _hook_dispatch(
        session_key: str, message: str, provenance: dict[str, Any]
    ) -> dict[str, Any]:
        # The hooks token is not a Control credential: the turn runs under an
        # admitted but unbound context, which every operator-only check (the
        # trading guardrails) treats as an agent's.
        from agentos.gateway.access import ConnectionSurface
        from agentos.gateway.agent_surface import AgentBinding
        from agentos.gateway.auth import AccessContext

        access = AccessContext(
            surface=ConnectionSurface.CONTROL,
            admitted=True,
            credential_verified=True,
            agent=AgentBinding(via="unbound"),
        )
        result = await dispatcher.dispatch(
            "_hook",
            "chat.send",
            {"sessionKey": session_key, "message": message, "inputProvenance": provenance},
            _rpc_ctx(access, conn_id="hook"),
        )
        if result.ok:
            return dict(result.payload or {})
        error = result.error
        details = error.details if error is not None else None
        return {
            "ok": False,
            "error": error.message if error else "error",
            "code": error.code if error else ErrorCode.INTERNAL_ERROR.value,
            "detail": details.get("code") if isinstance(details, dict) else None,
        }

    def _hook_wake(reason: str) -> bool:
        if heartbeat_loop is None:
            return False
        heartbeat_loop.request_now(reason=reason)
        return True

    register_inbound_hook_routes(
        app,
        config=config,
        dispatch_message=_hook_dispatch,
        wake=_hook_wake,
        auth_guard=auth_guard,
    )
    from agentos.gateway.tools_invoke import register_tools_invoke_route  # noqa: PLC0415

    register_tools_invoke_route(
        app,
        config=config,
        make_ctx=_make_ctx,
        tool_registry=tool_registry,
        runtime={
            "session_manager": session_manager,
            "task_runtime": task_runtime,
            "cron_scheduler": cron_scheduler,
            "channel_manager": channel_manager,
            "tool_hooks": getattr(turn_runner, "tool_hooks", None),
        },
    )
    register_audio_transcription_routes(app, config=config)

    return app
