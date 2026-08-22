from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.middleware import AuthMiddleware


def test_invalid_auth_mode_raises_validation_error() -> None:
    # Reject unknown/unimplemented modes at config validation time
    with pytest.raises(ValidationError) as excinfo:
        AuthConfig(mode="password")
    assert "Unsupported or unimplemented auth.mode" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        AuthConfig(mode="tokn")  # typo
    assert "Unsupported or unimplemented auth.mode" in str(excinfo.value)


@pytest.mark.asyncio
async def test_auth_middleware_fails_closed_for_unimplemented_mode() -> None:
    # Build a config that has an bypassed or unimplemented mode
    # Since Pydantic prevents direct validation bypass at init, we can test middleware
    # dispatch directly using a mock request.
    from starlette.types import Scope

    async def dummy_app(scope: Scope, receive: Any, send: Any) -> None:
        pass

    config = GatewayConfig()
    # Bypass validation by mutating the private model fields or using model_construct
    config.auth = AuthConfig.model_construct(mode="password")

    middleware = AuthMiddleware(dummy_app, config=config)

    # Mock request targeting a non-public route
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/sessions",
        "headers": [],
    }
    request = Request(scope)

    async def mock_call_next(req: Request) -> Response:
        return Response("OK")

    response = await middleware.dispatch(request, mock_call_next)
    assert response.status_code == 401
