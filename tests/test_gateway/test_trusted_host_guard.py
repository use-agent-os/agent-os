"""TrustedHostMiddleware wiring — Host-header allowlist against DNS rebinding.

A loopback bind is not a boundary against a browser: a malicious page can
rebind ``attacker.com`` to ``127.0.0.1`` and reach the local gateway, with
the attacker's hostname in the ``Host`` header (CVE-2026-53869 class). On a
loopback bind we therefore pin ``Host`` to a loopback allowlist. On a
non-loopback bind (which only starts past ``enforce_public_bind_auth_guard``
— so auth is on, or the operator explicitly opted in) we do not constrain
Host, since the operator deliberately serves other names.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentos.gateway.app import resolve_trusted_hosts
from agentos.gateway.config import AuthConfig, ControlUiConfig, CorsConfig, GatewayConfig
from agentos.gateway.middleware import LoopbackHostMiddleware


def _config(
    host: str = "127.0.0.1",
    auth_mode: str = "none",
    allow_public: bool = False,
    ui_origins: list[str] | None = None,
    cors_origins: list[str] | None = None,
    base_path: str = "/control",
) -> GatewayConfig:
    return GatewayConfig(
        host=host,
        auth=AuthConfig(mode=auth_mode, allow_unauthenticated_public=allow_public),
        control_ui=ControlUiConfig(allowed_origins=ui_origins or [], base_path=base_path),
        cors=CorsConfig(allowed_origins=cors_origins)
        if cors_origins is not None
        else CorsConfig(),
    )


class TestResolveTrustedHosts:
    def test_loopback_bind_constrains_host(self) -> None:
        """No wildcard on a loopback bind — loopback Host values themselves are
        admitted by LoopbackHostMiddleware via scopes.is_loopback_address."""
        hosts = resolve_trusted_hosts(_config(host="127.0.0.1"))
        assert "*" not in hosts

    def test_non_loopback_bind_does_not_constrain(self) -> None:
        """Past the startup guard a public bind has auth (or explicit opt-in)."""
        hosts = resolve_trusted_hosts(_config(host="0.0.0.0", auth_mode="token"))
        assert hosts == ["*"]

    def test_loopback_bind_includes_configured_ui_origin_host(self) -> None:
        hosts = resolve_trusted_hosts(
            _config(host="127.0.0.1", ui_origins=["https://ui.example.com"])
        )
        assert "ui.example.com" in hosts

    def test_unparseable_ui_origin_warns_instead_of_dying_silently(self) -> None:
        """A scheme-less entry can never match anything (here or in the WS
        guard) — the operator must hear about it at startup, not debug a
        dead allowlist entry."""
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            hosts = resolve_trusted_hosts(
                _config(host="127.0.0.1", ui_origins=["ui.example.com"])
            )
        assert "ui.example.com" not in hosts
        assert any("origin" in entry["event"] for entry in logs)


class TestLoopbackHostMiddlewareIntegration:
    def _app(self, config: GatewayConfig) -> Starlette:
        from starlette.middleware import Middleware

        async def ok(_request: object) -> PlainTextResponse:
            return PlainTextResponse("ok")

        return Starlette(
            routes=[Route("/probe", ok)],
            middleware=[
                Middleware(
                    LoopbackHostMiddleware,
                    allowed_hosts=resolve_trusted_hosts(config),
                )
            ],
        )

    def test_rebound_host_header_rejected_on_loopback_bind(self) -> None:
        client = TestClient(self._app(_config(host="127.0.0.1")))
        resp = client.get("/probe", headers={"host": "attacker.com"})
        assert resp.status_code == 400

    def test_rejected_host_message_points_to_the_config_fix(self) -> None:
        """A user reaching the gateway via a custom loopback hostname (e.g.
        /etc/hosts) is a legitimate false-positive; the 400 body must name the
        config key that unblocks them instead of a bare 'Invalid host'."""
        client = TestClient(self._app(_config(host="127.0.0.1")))
        resp = client.get("/probe", headers={"host": "myagent.local"})
        assert resp.status_code == 400
        assert "control_ui.allowed_origins" in resp.text
        assert "myagent.local" in resp.text

    def test_loopback_host_header_allowed(self) -> None:
        client = TestClient(self._app(_config(host="127.0.0.1")))
        resp = client.get("/probe", headers={"host": "127.0.0.1"})
        assert resp.status_code == 200
        resp2 = client.get("/probe", headers={"host": "localhost:18791"})
        assert resp2.status_code == 200

    def test_ipv6_loopback_host_header_allowed(self) -> None:
        """Regression: Starlette's TrustedHostMiddleware split-on-':' mangles
        bracketed IPv6 and 400s a legit ``[::1]`` bind. This must admit it."""
        client = TestClient(self._app(_config(host="::1")))
        resp = client.get("/probe", headers={"host": "[::1]:18791"})
        assert resp.status_code == 200
        resp2 = client.get("/probe", headers={"host": "[::1]"})
        assert resp2.status_code == 200

    def test_ipv6_rebound_host_rejected(self) -> None:
        client = TestClient(self._app(_config(host="::1")))
        resp = client.get("/probe", headers={"host": "attacker.com"})
        assert resp.status_code == 400

    def test_public_bind_allows_any_host(self) -> None:
        client = TestClient(self._app(_config(host="0.0.0.0", auth_mode="token")))
        resp = client.get("/probe", headers={"host": "anything.example.com"})
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        ("bind", "host_header"),
        [
            ("127.0.0.2", "127.0.0.2:18791"),
            ("127.1.2.3", "127.1.2.3:18791"),
            ("::ffff:127.0.0.1", "[::ffff:127.0.0.1]:18791"),
        ],
    )
    def test_alternate_loopback_bind_reachable_at_its_own_address(
        self, bind: str, host_header: str
    ) -> None:
        """Regression: every bind is_loopback_bind blesses (all of 127/8,
        IPv4-mapped loopback) must be reachable at its own address — the Host
        guard shares the loopback predicate with the startup and WS guards."""
        client = TestClient(self._app(_config(host=bind)))
        resp = client.get("/probe", headers={"host": host_header})
        assert resp.status_code == 200

    def test_trailing_dot_loopback_host_admitted(self) -> None:
        client = TestClient(self._app(_config(host="127.0.0.1")))
        resp = client.get("/probe", headers={"host": "localhost.:18791"})
        assert resp.status_code == 200


class TestRealAppWiring:
    """Mutation guard: LoopbackHostMiddleware must be wired into the real
    ``create_gateway_app`` middleware stack. The throwaway-app tests above
    cannot detect the wiring line being dropped."""

    def _real_app(self):  # noqa: ANN202
        from agentos.gateway.app import create_gateway_app

        return create_gateway_app(GatewayConfig())

    def test_foreign_host_rejected_by_real_app(self) -> None:
        client = TestClient(self._real_app())  # default Host: testserver
        assert client.get("/ready").status_code == 400
        assert (
            client.get("/ready", headers={"host": "attacker.com"}).status_code == 400
        )

    def test_loopback_host_admitted_by_real_app(self) -> None:
        client = TestClient(self._real_app(), base_url="http://localhost")
        assert client.get("/ready").status_code in (200, 503)


class TestLoopbackOriginGuardHttp:
    """Cross-site browser requests to the HTTP ``/api/*`` surface must be
    rejected on a loopback bind — the same drive-by threat the WS handshake
    guard covers, over the sibling transport. Without this, default CORS
    (``["*"]`` + credentials) reflects the attacker Origin and a page on
    evil.com can read config/sessions and drive chat.send."""

    def _client(self, **cfg_kwargs: object) -> TestClient:
        from agentos.gateway.app import create_gateway_app

        return TestClient(
            create_gateway_app(_config(**cfg_kwargs)),  # type: ignore[arg-type]
            base_url="http://localhost",
        )

    def test_cross_site_origin_rejected(self) -> None:
        client = self._client()
        resp = client.get("/api/sessions", headers={"origin": "http://evil.com"})
        assert resp.status_code == 403
        assert "access-control-allow-origin" not in resp.headers

    def test_loopback_origin_allowed(self) -> None:
        client = self._client()
        resp = client.get(
            "/api/sessions", headers={"origin": "http://localhost:18791"}
        )
        assert resp.status_code == 200

    def test_no_origin_allowed(self) -> None:
        """CLI/curl requests carry no Origin and must be untouched."""
        client = self._client()
        assert client.get("/api/sessions").status_code == 200

    def test_allowlisted_ui_origin_allowed(self) -> None:
        client = self._client(ui_origins=["https://agent.example.com"])
        resp = client.get(
            "/api/sessions", headers={"origin": "https://agent.example.com"}
        )
        assert resp.status_code == 200

    def test_explicit_cors_origin_allowed(self) -> None:
        """A deliberate non-wildcard cors.allowed_origins entry keeps working."""
        client = self._client(cors_origins=["https://myapp.example"])
        resp = client.get(
            "/api/sessions", headers={"origin": "https://myapp.example"}
        )
        assert resp.status_code == 200

    def test_wildcard_cors_default_does_not_admit_foreign_origin(self) -> None:
        client = self._client(cors_origins=["*"])
        resp = client.get("/api/sessions", headers={"origin": "http://evil.com"})
        assert resp.status_code == 403

    @pytest.mark.parametrize("path", ["/health", "/healthz", "/ready", "/readyz"])
    def test_public_health_paths_never_origin_gated(self, path: str) -> None:
        """Health probes (monitoring, load balancers) may carry an Origin and
        must never be blocked — AuthMiddleware exempts them too."""
        client = self._client()
        resp = client.get(path, headers={"origin": "http://monitoring.example.com"})
        assert resp.status_code == 200

    def test_control_ui_shell_not_origin_gated(self) -> None:
        """The Control UI surface is served, not an RPC sink; the origin guard
        must not 403 it (CSP/SecurityHeaders cover that surface)."""
        client = self._client()
        resp = client.get("/control/", headers={"origin": "http://evil.com"})
        assert resp.status_code != 403

    @pytest.mark.parametrize("base_path", ["/", "/api", "/api/v1"])
    def test_root_or_api_base_path_does_not_disable_guard(self, base_path: str) -> None:
        """[P1a] A UI base_path of "/" (normalized to "") or one overlapping the
        API surface must NOT wholesale-exempt /api/* from the Origin guard."""
        from agentos.gateway.app import create_gateway_app

        client = TestClient(
            create_gateway_app(_config(base_path=base_path)),
            base_url="http://localhost",
        )
        resp = client.get("/api/config", headers={"origin": "http://evil.com"})
        assert resp.status_code == 403
        assert "access-control-allow-origin" not in resp.headers
