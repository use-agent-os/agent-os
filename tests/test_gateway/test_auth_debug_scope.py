from __future__ import annotations

from agentos.gateway.auth import OpenScopeResolver
from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.scopes import (
    CLI_DEFAULT_OPERATOR_SCOPES,
    REMOTE_OPERATOR_SCOPES,
    normalize_operator_scopes,
)


def test_open_auth_loopback_operator_gets_local_owner_scopes_when_debug_false() -> None:
    principal = OpenScopeResolver().resolve(
        {},
        "operator",
        GatewayConfig(debug=False, host="127.0.0.1"),
        peer_ip="127.0.0.1",
    )

    assert principal.scopes == CLI_DEFAULT_OPERATOR_SCOPES
    assert principal.is_owner is True
    assert principal.authenticated is False


def test_open_auth_exposed_operator_gets_remote_scopes_when_debug_false() -> None:
    principal = OpenScopeResolver().resolve(
        {},
        "operator",
        GatewayConfig(debug=False, host="0.0.0.0"),
        peer_ip="127.0.0.1",
    )

    assert principal.scopes == REMOTE_OPERATOR_SCOPES
    assert principal.is_owner is False
    assert principal.authenticated is False


def test_open_auth_debug_operator_uses_configured_token_scopes() -> None:
    configured_scopes = ["operator.write"]
    principal = OpenScopeResolver().resolve(
        {},
        "operator",
        GatewayConfig(
            debug=True,
            host="0.0.0.0",
            auth=AuthConfig(token_scopes=configured_scopes),
        ),
        peer_ip="203.0.113.7",
    )

    assert principal.scopes == normalize_operator_scopes(configured_scopes)
    assert principal.is_owner is False
    assert principal.authenticated is False
