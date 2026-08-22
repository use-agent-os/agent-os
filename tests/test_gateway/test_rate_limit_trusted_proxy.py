from __future__ import annotations

import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from agentos.gateway.config import AuthConfig, GatewayConfig, RateLimitConfig
from agentos.gateway.middleware import RateLimitMiddleware


def _create_app(
    *,
    max_requests: int = 2,
    window_seconds: int = 60,
    trusted_proxy: str | None = None,
    max_tracked_clients: int = 10_000,
) -> tuple[Starlette, list[RateLimitMiddleware]]:
    app = Starlette()

    async def api_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app.add_route("/api/test", api_endpoint, methods=["GET"])

    config = GatewayConfig(
        auth=AuthConfig(trusted_proxy=trusted_proxy),
        rate_limit=RateLimitConfig(
            enabled=True,
            max_requests=max_requests,
            window_seconds=window_seconds,
        ),
    )

    middleware_instance: list[RateLimitMiddleware] = []

    def middleware_factory(inner_app):
        mw = RateLimitMiddleware(
            inner_app,
            config=config,
            max_tracked_clients=max_tracked_clients,
        )
        middleware_instance.append(mw)
        return mw

    app.add_middleware(middleware_factory)
    return app, middleware_instance


def test_untrusted_client_cannot_bypass_rate_limit_via_x_forwarded_for() -> None:
    """An untrusted peer rotating X-Forwarded-For is rate limited by peer IP and does not bypass."""
    app, mw_holder = _create_app(max_requests=2, window_seconds=60, trusted_proxy=None)

    with TestClient(app, client=("198.51.100.1", 50000)) as client:
        # Request 1: sends spoofed IP 1.1.1.1
        resp1 = client.get("/api/test", headers={"x-forwarded-for": "1.1.1.1"})
        assert resp1.status_code == 200

        # Request 2: sends spoofed IP 2.2.2.2
        resp2 = client.get("/api/test", headers={"x-forwarded-for": "2.2.2.2"})
        assert resp2.status_code == 200

        # Request 3: sends spoofed IP 3.3.3.3 -> must be 429 because peer IP
        # (198.51.100.1) exceeded max_requests (2)
        resp3 = client.get("/api/test", headers={"x-forwarded-for": "3.3.3.3"})
        assert resp3.status_code == 429
        assert resp3.json() == {"error": "Too Many Requests", "code": "RATE_LIMITED"}

    # Memory check: only the peer IP was tracked, not the spoofed IPs
    mw = mw_holder[0]
    assert "198.51.100.1" in mw._windows
    assert "1.1.1.1" not in mw._windows
    assert "2.2.2.2" not in mw._windows
    assert "3.3.3.3" not in mw._windows
    assert len(mw._windows) == 1


def test_trusted_proxy_honors_x_forwarded_for() -> None:
    """When peer IP matches configured trusted_proxy, X-Forwarded-For is used as client identity."""
    app, mw_holder = _create_app(
        max_requests=2,
        window_seconds=60,
        trusted_proxy="10.0.0.1, 10.0.0.2",
    )

    with TestClient(app, client=("10.0.0.1", 50000)) as client:
        # Client A makes 2 requests
        assert (
            client.get("/api/test", headers={"x-forwarded-for": "203.0.113.50"}).status_code == 200
        )
        assert (
            client.get("/api/test", headers={"x-forwarded-for": "203.0.113.50"}).status_code == 200
        )
        # Client A is rate limited
        assert (
            client.get("/api/test", headers={"x-forwarded-for": "203.0.113.50"}).status_code == 429
        )

        # Client B sends through same trusted proxy -> not blocked
        assert (
            client.get("/api/test", headers={"x-forwarded-for": "203.0.113.51"}).status_code == 200
        )

    mw = mw_holder[0]
    assert "203.0.113.50" in mw._windows
    assert "203.0.113.51" in mw._windows
    assert "10.0.0.1" not in mw._windows


def test_trusted_proxy_x_forwarded_for_multiple_ips() -> None:
    """X-Forwarded-For with multiple IPs takes the first (client) IP."""
    app, mw_holder = _create_app(
        max_requests=1,
        window_seconds=60,
        trusted_proxy="10.0.0.1",
    )

    with TestClient(app, client=("10.0.0.1", 50000)) as client:
        resp = client.get(
            "/api/test",
            headers={"x-forwarded-for": "203.0.113.99, 10.0.0.5, 10.0.0.1"},
        )
        assert resp.status_code == 200

    mw = mw_holder[0]
    assert "203.0.113.99" in mw._windows
    assert len(mw._windows) == 1


def test_trusted_proxy_missing_or_blank_forwarded_for_falls_back_to_peer() -> None:
    """If trusted proxy sends empty or missing X-Forwarded-For, fall back to peer IP."""
    app, mw_holder = _create_app(
        max_requests=1,
        window_seconds=60,
        trusted_proxy="10.0.0.1",
    )

    with TestClient(app, client=("10.0.0.1", 50000)) as client:
        assert client.get("/api/test", headers={"x-forwarded-for": "  "}).status_code == 200
        assert client.get("/api/test").status_code == 429

    mw = mw_holder[0]
    assert "10.0.0.1" in mw._windows


def test_max_tracked_clients_evicts_lru() -> None:
    """When number of distinct clients exceeds max_tracked_clients, oldest LRU entry is evicted."""
    app, mw_holder = _create_app(
        max_requests=5,
        window_seconds=60,
        trusted_proxy="10.0.0.1",
        max_tracked_clients=3,
    )

    with TestClient(app, client=("10.0.0.1", 50000)) as client:
        client.get("/api/test", headers={"x-forwarded-for": "1.1.1.1"})
        client.get("/api/test", headers={"x-forwarded-for": "2.2.2.2"})
        client.get("/api/test", headers={"x-forwarded-for": "3.3.3.3"})

        mw = mw_holder[0]
        assert len(mw._windows) == 3
        assert list(mw._windows.keys()) == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]

        # Accessing 1.1.1.1 again makes 2.2.2.2 the least recently used
        client.get("/api/test", headers={"x-forwarded-for": "1.1.1.1"})
        assert list(mw._windows.keys()) == ["2.2.2.2", "3.3.3.3", "1.1.1.1"]

        # Adding 4.4.4.4 exceeds capacity (3), so 2.2.2.2 should be evicted
        client.get("/api/test", headers={"x-forwarded-for": "4.4.4.4"})
        assert len(mw._windows) == 3
        assert list(mw._windows.keys()) == ["3.3.3.3", "1.1.1.1", "4.4.4.4"]
        assert "2.2.2.2" not in mw._windows


def test_sweep_expired_entries() -> None:
    """Expired window timestamps are purged during periodic sweeps."""
    app, mw_holder = _create_app(
        max_requests=5,
        window_seconds=1,
        trusted_proxy="10.0.0.1",
        max_tracked_clients=10,
    )

    with TestClient(app, client=("10.0.0.1", 50000)) as client:
        client.get("/api/test", headers={"x-forwarded-for": "1.1.1.1"})
        client.get("/api/test", headers={"x-forwarded-for": "2.2.2.2"})

        mw = mw_holder[0]
        assert len(mw._windows) == 2

        # Wait for window to expire
        time.sleep(1.1)

        # Trigger next request after window has expired
        client.get("/api/test", headers={"x-forwarded-for": "3.3.3.3"})

        # Expired entries 1.1.1.1 and 2.2.2.2 should have been swept
        assert "1.1.1.1" not in mw._windows
        assert "2.2.2.2" not in mw._windows
        assert "3.3.3.3" in mw._windows
