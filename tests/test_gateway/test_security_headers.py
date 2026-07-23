"""Security-policy regression tests for the browser Control UI."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentos.gateway.middleware import SecurityHeadersMiddleware


def _client() -> TestClient:
    app = Starlette(
        routes=[Route("/control/", lambda _request: PlainTextResponse("ok"))],
        middleware=[
            Middleware(SecurityHeadersMiddleware, path_prefix="/control"),
        ],
    )
    return TestClient(app)


def test_control_ui_csp_allows_only_the_skill_registry_image_host() -> None:
    response = _client().get("/control/")

    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "img-src 'self' data: https://raw.githubusercontent.com;" in policy
    assert "img-src https:" not in policy
    assert "img-src *" not in policy
    assert "script-src 'self';" in policy
    assert "script-src 'self' 'unsafe-inline'" not in policy
    # Remote gateway profiles are an explicit console feature, so WebSocket
    # schemes remain available while HTTP fetches stay same-origin.
    assert "connect-src 'self' ws: wss:;" in policy
    assert "connect-src 'self' https:" not in policy


def test_security_headers_do_not_leak_to_non_ui_routes() -> None:
    app = Starlette(
        routes=[Route("/ready", lambda _request: PlainTextResponse("ready"))],
        middleware=[
            Middleware(SecurityHeadersMiddleware, path_prefix="/control"),
        ],
    )

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
