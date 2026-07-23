"""Server-side auth resolution: Principal + ScopeResolver.

Two resolver strategies live here:

* :class:`TokenScopeResolver` validates a shared token and issues a
  Principal whose scopes come from ``config.auth.token_scopes`` — normalized
  via :func:`agentos.gateway.scopes.normalize_operator_scopes` so that a
  token declared with ``["operator.write"]`` behaves identically to one
  declared with ``["operator.write", "operator.read"]``.
* :class:`OpenScopeResolver` serves no-auth mode. An operator who connects
  from a loopback peer to a loopback-bound gateway is treated as the local
  owner and gets :data:`CLI_DEFAULT_OPERATOR_SCOPES`. Any other no-auth
  operator — including a loopback peer on a gateway bound to ``0.0.0.0``
  — gets the narrower :data:`REMOTE_OPERATOR_SCOPES` set (no ``admin``,
  no ``pairing``).

The loopback-upgrade path is the mechanism by which the Control UI gets
admin privileges in the default single-machine deployment. Remote browser
access remains ungranted until a deliberate token is configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from agentos.gateway.scopes import (
    CLI_DEFAULT_OPERATOR_SCOPES,
    NODE_DEFAULT_SCOPES,
    REMOTE_OPERATOR_SCOPES,
    is_loopback_address,
    is_loopback_bind,
    normalize_operator_scopes,
)

if TYPE_CHECKING:
    from agentos.gateway.config import GatewayConfig

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Principal:
    """Server-computed identity credential. Immutable. Lifetime = connection.

    ``is_owner`` flags the caller as a locally-proven gateway owner. It is
    **advisory only** — authorization decisions must go through
    :mod:`agentos.gateway.scopes` so that the scope set (not the flag)
    governs what a caller may do. Non-gateway consumers (tool dispatch,
    scheduler handlers) still read the flag for owner-only tool gating;
    the field remains for their benefit.
    """

    role: str  # "operator" | "node"
    scopes: frozenset[str]  # server-computed, not client-declared
    is_owner: bool  # operator on a loopback-proven channel → True
    authenticated: bool


@runtime_checkable
class ScopeResolver(Protocol):
    """Strategy interface for auth-mode-specific scope computation."""

    def resolve(
        self,
        auth_params: dict,
        role_claim: str,
        config: GatewayConfig,
        *,
        peer_ip: str | None = None,
    ) -> Principal: ...


class TokenScopeResolver:
    """Token mode: validate token, compute scopes from config, ignore client claims."""

    def resolve(
        self,
        auth_params: dict,
        role_claim: str,
        config: GatewayConfig,
        *,
        peer_ip: str | None = None,
    ) -> Principal:
        provided = (auth_params or {}).get("token")
        configured_token = config.auth.token
        if not configured_token or provided != configured_token:
            raise ValueError("Invalid token")

        allowed_roles = config.auth.allowed_roles
        if role_claim not in allowed_roles:
            raise ValueError(f"Invalid role: {role_claim!r}")

        if role_claim == "node":
            scopes = NODE_DEFAULT_SCOPES
            is_owner = False
        else:
            scopes = normalize_operator_scopes(config.auth.token_scopes)
            # Owner flag follows proximity, not the token — a shared token
            # used from a LAN peer should not claim ownership.
            is_owner = is_loopback_bind(config.host) and is_loopback_address(peer_ip)

        return Principal(
            role=role_claim,
            scopes=scopes,
            is_owner=is_owner,
            authenticated=True,
        )


class OpenScopeResolver:
    """No-auth mode with loopback-scoped admin upgrade.

    Scope issuance depends on transport provenance:

    * Operator on a loopback-bound gateway connecting from a loopback peer
      → :data:`CLI_DEFAULT_OPERATOR_SCOPES` (includes ``admin`` and
      ``pairing``). This is the Control UI / local CLI case.
    * Operator elsewhere → :data:`REMOTE_OPERATOR_SCOPES` (``read`` /
      ``write`` / ``approvals``; no ``admin``). A gateway bound to
      ``0.0.0.0`` accepts remote peers and must not auto-upgrade even a
      loopback client, because that client could be a reverse-tunnel
      relay.
    * Node → :data:`NODE_DEFAULT_SCOPES` regardless of peer address.

    ``config.debug`` retains the historical "dev mode grants whatever
    ``token_scopes`` says" behavior so an operator who truly wants the
    full surface on a public bind can opt in.
    """

    def resolve(
        self,
        auth_params: dict,
        role_claim: str,
        config: GatewayConfig,
        *,
        peer_ip: str | None = None,
    ) -> Principal:
        allowed_roles = config.auth.allowed_roles
        if role_claim not in allowed_roles:
            raise ValueError(f"Invalid role: {role_claim!r}")

        if role_claim == "node":
            return Principal(
                role="node",
                scopes=NODE_DEFAULT_SCOPES,
                is_owner=False,
                authenticated=False,
            )

        local_owner = is_loopback_bind(config.host) and is_loopback_address(peer_ip)

        if config.debug:
            scopes = normalize_operator_scopes(config.auth.token_scopes)
        elif local_owner:
            scopes = CLI_DEFAULT_OPERATOR_SCOPES
        else:
            scopes = REMOTE_OPERATOR_SCOPES

        return Principal(
            role=role_claim,
            scopes=scopes,
            is_owner=local_owner,
            authenticated=False,
        )


_RESOLVERS: dict[str, ScopeResolver] = {
    "token": TokenScopeResolver(),
    "none": OpenScopeResolver(),
}


def resolve_auth(
    config: GatewayConfig,
    auth_params: dict,
    role_claim: str,
    *,
    peer_ip: str | None = None,
) -> Principal | None:
    """Pick resolver by auth mode, return Principal or None on failure.

    ``peer_ip`` is the caller's IP as observed at the transport layer
    (WebSocket upgrade, HTTP request). It is consulted for loopback
    proximity checks in :class:`OpenScopeResolver` and to set the
    ``is_owner`` flag in :class:`TokenScopeResolver`. ``None`` is treated
    as "unknown" — non-loopback for the purposes of any upgrade.
    """
    resolver = _RESOLVERS.get(config.auth.mode)
    if resolver is None:
        log.warning("auth.unsupported_mode", mode=config.auth.mode)
        return None
    try:
        return resolver.resolve(auth_params, role_claim, config, peer_ip=peer_ip)
    except ValueError as exc:
        log.warning("auth.failed", mode=config.auth.mode, error=str(exc))
        return None
