"""Binary connection admission for gateway clients."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from agentos.gateway.access import (
    ConnectionSurface,
    is_loopback_address,
    is_loopback_bind,
    peer_is_trusted_proxy,
)

if TYPE_CHECKING:
    from starlette.requests import Request

    from agentos.gateway.agent_surface import AgentBinding
    from agentos.gateway.config import GatewayConfig

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Server-computed connection admission.

    The context intentionally carries no role, scope, admin, or owner flag.
    ``admitted`` is the entire human authorization state. ``surface`` limits
    which protocol contract the connection may use.
    """

    surface: ConnectionSurface
    admitted: bool
    credential_verified: bool
    # Set at admission when the connection is an agent's (see
    # ``gateway.agent_surface``). Read by handlers whose answer depends on
    # who is asking — the trading guardrails — never by admission itself.
    agent: AgentBinding | None = None


def denied_access(surface: ConnectionSurface = ConnectionSurface.CONTROL) -> AccessContext:
    """Build a fail-closed context for a rejected connection."""

    return AccessContext(surface=surface, admitted=False, credential_verified=False)


def token_matches(provided: str | None, configured: str | None) -> bool:
    """Return True iff ``provided`` equals the configured gateway token.

    Uses ``hmac.compare_digest`` so a remote caller cannot walk the secret a
    byte at a time from response latency. Missing or empty configured values
    fail closed; the attacker-controlled side is never short-circuited with
    ``==`` / ``!=``.
    """

    if not isinstance(configured, str) or not configured:
        return False
    candidate = provided if isinstance(provided, str) else ""
    return hmac.compare_digest(candidate.encode("utf-8"), configured.encode("utf-8"))


def _surface(value: str | ConnectionSurface | None) -> ConnectionSurface:
    try:
        return ConnectionSurface(value or ConnectionSurface.CONTROL)
    except ValueError as exc:
        raise ValueError(f"Invalid client kind: {value!r}") from exc


def resolve_auth(
    config: GatewayConfig,
    auth_params: dict,
    client_kind: str | ConnectionSurface = ConnectionSurface.CONTROL,
    *,
    peer_ip: str | None = None,
) -> AccessContext | None:
    """Authenticate one connection and return its fixed protocol surface.

    ``mode=none`` is accepted only when both the listener and peer are
    loopback. Token mode is all-or-nothing: a valid token admits the complete
    selected surface. Fine-grained human scopes are deliberately unsupported.
    """

    try:
        surface = _surface(client_kind)
    except ValueError as exc:
        log.warning("auth.failed", mode=config.auth.mode, error=str(exc))
        return None
    if surface not in {ConnectionSurface.CONTROL, ConnectionSurface.NODE}:
        log.warning("auth.failed", mode=config.auth.mode, error="unsupported client surface")
        return None

    if config.auth.mode == "token":
        provided = (auth_params or {}).get("token")
        if not token_matches(provided, config.auth.token):
            log.warning("auth.failed", mode=config.auth.mode, error="invalid token")
            return None
        return AccessContext(
            surface=surface,
            admitted=True,
            credential_verified=True,
        )

    if config.auth.mode == "none":
        local = is_loopback_bind(config.host) and is_loopback_address(peer_ip)
        if not local:
            log.warning(
                "auth.failed",
                mode=config.auth.mode,
                error="no-auth connections require a loopback listener and peer",
            )
            return None
        return AccessContext(
            surface=surface,
            admitted=True,
            credential_verified=False,
        )

    if config.auth.mode == "trusted-proxy":
        # Secondary gate at the RPC layer: the transport peer must be a trusted
        # proxy. This is the same check as AuthMiddleware._is_trusted_proxy --
        # the middleware admits the request on that basis, and the RPC layer
        # confirms it. X-Forwarded-For consumption is gated by the middleware
        # (RateLimitMiddleware._get_client_ip) which uses the same trusted-set
        # check, so a spoofed XFF from a non-trusted peer is never honored.
        if not peer_is_trusted_proxy(config.auth.trusted_proxy, peer_ip):
            log.warning(
                "auth.failed",
                mode=config.auth.mode,
                error="peer is not a trusted proxy",
            )
            return None
        return AccessContext(
            surface=surface,
            admitted=True,
            credential_verified=False,
        )

    log.warning("auth.unsupported_mode", mode=config.auth.mode)
    return None


def extract_http_token(request: Request | None) -> str | None:
    """Header-only gateway token (``Authorization: Bearer`` or ``x-agentos-token``)."""

    if request is None:
        return None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.headers.get("x-agentos-token")


def resolve_http_auth(config: GatewayConfig, request: Request) -> AccessContext | None:
    """``resolve_auth`` for a plain HTTP request, against its transport peer.

    Routes that do their own admission call this instead of re-implementing a
    mode-specific check, so every auth mode (token, trusted-proxy, none) is
    enforced the same way it is for RPC and the WebSocket handshake.
    """

    token = extract_http_token(request)
    return resolve_auth(
        config,
        auth_params={"token": token} if token else {},
        client_kind=ConnectionSurface.CONTROL,
        peer_ip=request.client.host if request.client else None,
    )


__all__ = [
    "AccessContext",
    "denied_access",
    "extract_http_token",
    "resolve_auth",
    "resolve_http_auth",
    "token_matches",
]
