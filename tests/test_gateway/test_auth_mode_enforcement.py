"""Every ``auth.mode`` value either enforces credentials or is refused (#352).

``auth.mode="password"`` was an advertised, env-bound mode with no branch in
``AuthMiddleware.dispatch``: it fell through to ``call_next`` and admitted the
whole non-RPC surface unauthenticated. So did any typo'd mode. The fix is two
layers — validation refuses an unimplemented mode at load time, and the
middleware fails closed on anything that reaches it anyway (the config object
is read live, so a runtime mutation must not reopen the hole).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import SUPPORTED_AUTH_MODES, AuthConfig, GatewayConfig


class TestAuthModeValidation:
    @pytest.mark.parametrize("mode", list(SUPPORTED_AUTH_MODES))
    def test_supported_modes_are_accepted(self, mode: str) -> None:
        assert AuthConfig(mode=mode).mode == mode

    def test_password_mode_is_refused(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AuthConfig(mode="password")
        message = str(exc_info.value)
        assert "password" in message
        assert "token" in message

    @pytest.mark.parametrize("mode", ["tokenn", "on", "", "basic", "Password"])
    def test_unknown_modes_are_refused(self, mode: str) -> None:
        with pytest.raises(ValidationError):
            AuthConfig(mode=mode)

    def test_mode_is_normalized(self) -> None:
        assert AuthConfig(mode=" TOKEN ").mode == "token"

    def test_assignment_is_validated(self) -> None:
        auth = AuthConfig(mode="token", token="secret")
        with pytest.raises(ValidationError):
            auth.mode = "password"
        assert auth.mode == "token"

    def test_password_field_still_loads(self) -> None:
        """An existing config carrying auth.password must not fail to parse."""
        assert AuthConfig(password="hunter2").password == "hunter2"

    @pytest.mark.parametrize(
        "env_name",
        ["AGENTOS_AUTH_MODE", "AGENTOS_GATEWAY_AUTH__MODE"],
    )
    def test_env_bound_password_mode_is_refused(
        self, env_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``AGENTOS_AUTH_PASSWORD`` made this an env-bound mode; both env
        spellings of ``mode`` must be refused, not just the TOML one."""
        monkeypatch.setenv(env_name, "password")
        monkeypatch.setenv("AGENTOS_AUTH_PASSWORD", "hunter2")
        with pytest.raises(ValidationError):
            GatewayConfig()

    @pytest.mark.parametrize(
        "env_name",
        ["AGENTOS_AUTH_MODE", "AGENTOS_GATEWAY_AUTH__MODE"],
    )
    def test_env_bound_token_mode_still_loads(
        self, env_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(env_name, "token")
        monkeypatch.setenv("AGENTOS_AUTH_TOKEN", "secret-123")
        assert GatewayConfig().auth.mode == "token"

    def test_toml_with_password_mode_is_refused(self, tmp_path) -> None:
        path = tmp_path / "agentos.toml"
        path.write_text('[auth]\nmode = "password"\npassword = "hunter2"\n')
        with pytest.raises(ValidationError):
            GatewayConfig.load_from_toml(path)


def _unenforced_config(mode: str = "password") -> GatewayConfig:
    """A gateway whose live config carries a mode with no enforcement branch.

    ``model_construct`` bypasses validation on purpose: this is the runtime
    posture the middleware must fail closed on, not the load-time one.
    """

    return GatewayConfig(
        host="127.0.0.1",
        auth=AuthConfig.model_construct(mode=mode, password="hunter2"),
    )


class TestUnenforcedModeFailsClosed:
    @pytest.mark.parametrize(
        "path",
        ["/api/sessions", "/api/config", "/api/system/status"],
    )
    def test_non_rpc_surface_is_denied(self, path: str) -> None:
        app = create_gateway_app(config=_unenforced_config())
        with TestClient(app, base_url="http://localhost") as client:
            response = client.get(path)
            assert response.status_code == 401
            assert response.json().get("code") == "UNAUTHORIZED"

    @pytest.mark.parametrize("mode", ["password", "tokenn", "", "basic"])
    def test_every_unenforced_mode_is_denied(self, mode: str) -> None:
        """A typo must fail closed exactly like the advertised mode did."""
        app = create_gateway_app(config=_unenforced_config(mode))
        with TestClient(app, base_url="http://localhost") as client:
            assert client.get("/api/sessions").status_code == 401

    def test_post_route_is_denied(self) -> None:
        """The fall-through admitted writes, not just reads — /api/chat runs a
        turn with the operator's provider credentials."""
        app = create_gateway_app(config=_unenforced_config())
        with TestClient(app, base_url="http://localhost") as client:
            response = client.post("/api/chat", json={"message": "hi"})
            assert response.status_code == 401
            assert response.json().get("code") == "UNAUTHORIZED"

    def test_upload_route_is_denied(self) -> None:
        app = create_gateway_app(config=_unenforced_config())
        with TestClient(app, base_url="http://localhost") as client:
            response = client.post(
                "/api/v1/files/upload",
                files={"file": ("a.txt", b"hello", "text/plain")},
            )
            assert response.status_code == 401

    def test_transcribe_route_is_denied(self) -> None:
        app = create_gateway_app(config=_unenforced_config())
        with TestClient(app, base_url="http://localhost") as client:
            response = client.post(
                "/api/audio/transcribe",
                files={"file": ("a.webm", b"audio", "audio/webm")},
            )
            assert response.status_code == 401

    def test_health_probe_stays_public(self) -> None:
        """Fail-closed must not take the credential-free health surface down."""
        app = create_gateway_app(config=_unenforced_config())
        with TestClient(app, base_url="http://localhost") as client:
            assert client.get("/health").status_code == 200


class TestSupportedModesStillWork:
    def test_none_mode_admits_loopback(self) -> None:
        app = create_gateway_app(config=GatewayConfig(auth=AuthConfig(mode="none")))
        with TestClient(app, base_url="http://localhost") as client:
            assert client.get("/api/system/status").status_code == 200

    def test_token_mode_enforces(self) -> None:
        config = GatewayConfig(auth=AuthConfig(mode="token", token="secret-123"))
        app = create_gateway_app(config=config)
        with TestClient(app, base_url="http://localhost") as client:
            assert client.get("/api/system/status").status_code == 401
            assert (
                client.get(
                    "/api/system/status",
                    headers={"Authorization": "Bearer secret-123"},
                ).status_code
                == 200
            )
