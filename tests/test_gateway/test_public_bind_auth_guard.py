"""Startup guard: refuse to serve with auth.mode="none" on a non-loopback bind.

Covers the V3 hardening from issue #18 — "no auth by default" is safe only
while the gateway stays on loopback. Binding a wildcard or LAN address with
auth disabled must fail closed unless the operator explicitly opts in via
``auth.allow_unauthenticated_public``.
"""

from __future__ import annotations

import pytest

from agentos.gateway.config import (
    AuthConfig,
    GatewayConfig,
    enforce_public_bind_auth_guard,
)
from agentos.gateway.scopes import is_loopback_bind


def _config(
    host: str,
    auth_mode: str = "none",
    allow_public: bool = False,
    trusted_proxy: str | None = None,
) -> GatewayConfig:
    return GatewayConfig(
        host=host,
        auth=AuthConfig(
            mode=auth_mode,
            allow_unauthenticated_public=allow_public,
            trusted_proxy=trusted_proxy,
        ),
    )


class TestIsLoopbackBind:
    """The guard reuses the same predicate as the ownership check (auth.py),
    so a bind the guard allows is exactly a bind ownership treats as local."""

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "127.1.2.3", "::1", "localhost", "::ffff:127.0.0.1"],
    )
    def test_loopback_hosts(self, host: str) -> None:
        assert is_loopback_bind(host) is True

    @pytest.mark.parametrize(
        "host",
        ["0.0.0.0", "::", "192.168.1.5", "10.0.0.7", "example.com", ""],
    )
    def test_non_loopback_hosts(self, host: str) -> None:
        assert is_loopback_bind(host) is False


class TestEnforcePublicBindAuthGuard:
    def test_refuses_wildcard_bind_without_auth(self) -> None:
        with pytest.raises(ValueError, match="auth.mode"):
            enforce_public_bind_auth_guard(_config("0.0.0.0"))

    def test_refuses_lan_bind_without_auth(self) -> None:
        with pytest.raises(ValueError, match="auth.mode"):
            enforce_public_bind_auth_guard(_config("192.168.1.5"))

    def test_error_message_names_remediations(self) -> None:
        """The refusal must tell the operator every way out."""
        with pytest.raises(ValueError) as exc_info:
            enforce_public_bind_auth_guard(_config("0.0.0.0"))
        message = str(exc_info.value)
        assert "auth.mode" in message
        assert "allow_unauthenticated_public" in message
        assert "0.0.0.0" in message

    def test_allows_loopback_bind_without_auth(self) -> None:
        enforce_public_bind_auth_guard(_config("127.0.0.1"))

    def test_allows_wildcard_bind_with_token_auth(self) -> None:
        enforce_public_bind_auth_guard(_config("0.0.0.0", auth_mode="token"))

    @pytest.mark.parametrize("mode", ["password", "trusted-proxy", "tokenn", "on", ""])
    def test_refuses_public_bind_with_unenforced_auth_mode(self, mode: str) -> None:
        """[P1b] Only token is enforced end-to-end. password/trusted-proxy are
        not enforced on the HTTP surface, and a typo must never be read as
        "auth is on"."""
        with pytest.raises(ValueError, match="auth.mode"):
            enforce_public_bind_auth_guard(_config("0.0.0.0", auth_mode=mode))

    def test_refuses_trusted_proxy_even_with_proxy_configured(self) -> None:
        """[P1] AuthMiddleware only string-matches the client-controlled
        X-Forwarded-For header — trivially spoofable — and resolve_auth has no
        trusted-proxy resolver, so the mode is not enforced end-to-end. It must
        not pass the public-bind guard until real peer-IP validation lands."""
        with pytest.raises(ValueError, match="auth.mode"):
            enforce_public_bind_auth_guard(
                _config("0.0.0.0", auth_mode="trusted-proxy", trusted_proxy="10.0.0.1")
            )

    def test_error_message_only_recommends_enforced_modes(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            enforce_public_bind_auth_guard(_config("0.0.0.0"))
        message = str(exc_info.value)
        # Neither unimplemented/spoofable mode should be recommended.
        assert '"password"' not in message
        assert "trusted-proxy" not in message
        assert '"token"' in message

    def test_explicit_opt_in_allows_unauthenticated_public_bind(self) -> None:
        enforce_public_bind_auth_guard(_config("0.0.0.0", allow_public=True))


class TestAuthConfigOptIn:
    def test_opt_in_defaults_to_false(self) -> None:
        assert AuthConfig().allow_unauthenticated_public is False

    def test_opt_in_env_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_AUTH_ALLOW_UNAUTHENTICATED_PUBLIC", "true")
        assert AuthConfig().allow_unauthenticated_public is True


class TestStartGatewayServerWiring:
    @pytest.mark.asyncio
    async def test_start_gateway_server_refuses_unauthenticated_public_bind(self) -> None:
        """The guard must run inside start_gateway_server, before any services boot."""
        from agentos.gateway.boot import start_gateway_server

        with pytest.raises(ValueError, match="allow_unauthenticated_public"):
            await start_gateway_server(config=_config("0.0.0.0"))
