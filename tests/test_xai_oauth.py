"""xAI OAuth: endpoint pinning, device-code flow, refresh, and the token store.

Offline: every test swaps httpx for a fake. Nothing here reaches auth.x.ai.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentos import xai_oauth as oauth

TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
AUTHZ_ENDPOINT = "https://auth.x.ai/oauth2/authorize"


def _jwt(exp_offset_seconds: float) -> str:
    """A token whose only meaningful claim is ``exp``."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time() + exp_offset_seconds)}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def _response(payload: dict[str, Any], status: int = 200, url: str = TOKEN_ENDPOINT):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", url))


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "auth.json"
    monkeypatch.setenv("AGENTOS_AUTH_STORE", str(path))
    monkeypatch.delenv("AGENTOS_XAI_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("XAI_BASE_URL", raising=False)
    return path


# ── Endpoint pinning ────────────────────────────────────────────────────────


class TestEndpointPinning:
    @pytest.mark.parametrize(
        "url",
        [
            "http://auth.x.ai/oauth2/token",
            "https://auth.evil.test/oauth2/token",
            "https://x.ai.evil.test/oauth2/token",
            "https://",
        ],
    )
    def test_a_non_xai_discovery_endpoint_is_refused(self, url: str) -> None:
        """A cached endpoint would receive every future refresh token."""
        with pytest.raises(oauth.XaiOAuthError) as excinfo:
            oauth._validate_oauth_endpoint(url, field="token_endpoint")
        assert excinfo.value.code == "xai_discovery_invalid"

    @pytest.mark.parametrize(
        "url", ["https://auth.x.ai/oauth2/token", "https://x.ai/oauth2/token"]
    )
    def test_the_xai_origin_is_accepted(self, url: str) -> None:
        assert oauth._validate_oauth_endpoint(url, field="token_endpoint") == url

    @pytest.mark.parametrize(
        "value",
        ["http://api.x.ai/v1", "https://attacker.example/v1", "not a url"],
    )
    def test_an_off_origin_inference_base_url_falls_back(self, value: str) -> None:
        """Reject, but do not raise: a bad override must not lock the account out."""
        assert oauth.validate_inference_base_url(value) == oauth.DEFAULT_XAI_OAUTH_BASE_URL

    def test_an_xai_subdomain_base_url_is_kept(self) -> None:
        assert (
            oauth.validate_inference_base_url("https://staging.x.ai/v1/")
            == "https://staging.x.ai/v1"
        )


# ── Token store ─────────────────────────────────────────────────────────────


class TestTokenStore:
    def test_an_absent_store_reads_as_empty(self) -> None:
        assert oauth.read_oauth_state() == {}
        assert oauth.has_oauth_credentials() is False

    def test_a_corrupt_store_does_not_raise(self, _isolated_store: Path) -> None:
        _isolated_store.write_text("{not json", encoding="utf-8")
        assert oauth.read_oauth_state() == {}

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows has no POSIX mode bits; the store is protected by the profile ACL.",
    )
    def test_the_store_is_written_owner_only(self, _isolated_store: Path) -> None:
        oauth.write_oauth_state({"tokens": {"refresh_token": "r"}})
        assert _isolated_store.stat().st_mode & 0o077 == 0

    def test_the_store_is_written_atomically(self, _isolated_store: Path) -> None:
        """A crash mid-write must not leave a half-parsed token file behind."""
        oauth.write_oauth_state({"tokens": {"refresh_token": "r"}})
        assert json.loads(_isolated_store.read_text(encoding="utf-8"))["providers"]
        assert not list(_isolated_store.parent.glob("*.tmp"))

    def test_logout_removes_only_the_xai_entry(self, _isolated_store: Path) -> None:
        _isolated_store.write_text(
            json.dumps({"providers": {"xai-oauth": {"tokens": {}}, "other": {"keep": True}}}),
            encoding="utf-8",
        )
        assert oauth.clear_oauth_state() is True
        remaining = json.loads(_isolated_store.read_text(encoding="utf-8"))
        assert remaining["providers"] == {"other": {"keep": True}}

    def test_logout_reports_when_there_was_nothing_to_remove(self) -> None:
        assert oauth.clear_oauth_state() is False

    def test_status_never_exposes_a_token(self) -> None:
        oauth.write_oauth_state(
            {"tokens": {"access_token": _jwt(3600), "refresh_token": "super-secret-refresh"}}
        )
        rendered = json.dumps(oauth.oauth_status())
        assert "super-secret-refresh" not in rendered
        assert oauth.oauth_status()["logged_in"] is True


# ── Expiry ──────────────────────────────────────────────────────────────────


class TestExpiry:
    def test_an_opaque_token_is_never_treated_as_expiring(self) -> None:
        assert oauth.access_token_is_expiring("opaque-token", 3600) is False

    def test_an_expired_jwt_is_expiring(self) -> None:
        assert oauth.access_token_is_expiring(_jwt(-10), 0) is True

    def test_a_long_lived_token_uses_the_full_skew(self) -> None:
        assert oauth.proactive_skew_seconds(_jwt(6 * 3600)) == oauth.MAX_REFRESH_SKEW_SECONDS

    def test_a_short_lived_token_uses_a_narrow_skew(self) -> None:
        """Device-code tokens run ~15 min; an hour of skew would refresh every call."""
        assert oauth.proactive_skew_seconds(_jwt(15 * 60)) == oauth.SHORT_TOKEN_SKEW_SECONDS


# ── Device-code login ───────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append({"url": url, "data": kwargs.get("data")})
        return self.responses.pop(0)


def _install_login(
    monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]
) -> _FakeClient:
    client = _FakeClient(responses)
    monkeypatch.setattr(
        oauth,
        "_discover_sync",
        lambda *a, **k: {
            "authorization_endpoint": AUTHZ_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
        },
    )
    monkeypatch.setattr(oauth.httpx, "Client", lambda **kwargs: client)
    return client


DEVICE_PAYLOAD = {
    "device_code": "dev-123",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://x.ai/device",
    "verification_uri_complete": "https://x.ai/device?code=ABCD-EFGH",
    "expires_in": 600,
    "interval": 5,
}


class TestDeviceCodeLogin:
    def test_a_successful_login_is_persisted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install_login(
            monkeypatch,
            [
                _response(DEVICE_PAYLOAD, url=oauth.XAI_OAUTH_DEVICE_CODE_URL),
                _response({"access_token": _jwt(3600), "refresh_token": "r-1"}),
            ],
        )
        prompts: list[tuple[str, str, int]] = []
        oauth.device_code_login(
            on_prompt=lambda url, code, interval: prompts.append((url, code, interval)),
            sleep=lambda _s: None,
        )

        assert prompts == [("https://x.ai/device?code=ABCD-EFGH", "ABCD-EFGH", 5)]
        assert oauth.has_oauth_credentials() is True
        assert oauth.read_oauth_state()["source"] == "oauth-device-code"
        assert client.requests[0]["data"]["client_id"] == oauth.DEFAULT_XAI_OAUTH_CLIENT_ID
        assert client.requests[0]["data"]["scope"] == oauth.XAI_OAUTH_SCOPE

    def test_authorization_pending_is_polled_until_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install_login(
            monkeypatch,
            [
                _response(DEVICE_PAYLOAD, url=oauth.XAI_OAUTH_DEVICE_CODE_URL),
                _response({"error": "authorization_pending"}, status=400),
                _response({"error": "authorization_pending"}, status=400),
                _response({"access_token": _jwt(3600), "refresh_token": "r-1"}),
            ],
        )
        slept: list[float] = []
        oauth.device_code_login(on_prompt=lambda *a: None, sleep=slept.append)
        assert slept == [5, 5]
        assert len(client.requests) == 4

    def test_slow_down_widens_the_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_login(
            monkeypatch,
            [
                _response(DEVICE_PAYLOAD, url=oauth.XAI_OAUTH_DEVICE_CODE_URL),
                _response({"error": "slow_down"}, status=400),
                _response({"access_token": _jwt(3600), "refresh_token": "r-1"}),
            ],
        )
        slept: list[float] = []
        oauth.device_code_login(on_prompt=lambda *a: None, sleep=slept.append)
        assert slept == [6]

    def test_a_token_response_without_a_refresh_token_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without one, the login is unusable the moment the access token expires."""
        _install_login(
            monkeypatch,
            [
                _response(DEVICE_PAYLOAD, url=oauth.XAI_OAUTH_DEVICE_CODE_URL),
                _response({"access_token": _jwt(3600)}),
            ],
        )
        with pytest.raises(oauth.XaiOAuthError) as excinfo:
            oauth.device_code_login(on_prompt=lambda *a: None, sleep=lambda _s: None)
        assert excinfo.value.code == "xai_device_token_invalid"
        assert oauth.has_oauth_credentials() is False

    def test_a_denied_authorization_fails_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_login(
            monkeypatch,
            [
                _response(DEVICE_PAYLOAD, url=oauth.XAI_OAUTH_DEVICE_CODE_URL),
                _response({"error": "access_denied"}, status=400),
            ],
        )
        with pytest.raises(oauth.XaiOAuthError) as excinfo:
            oauth.device_code_login(on_prompt=lambda *a: None, sleep=lambda _s: None)
        assert excinfo.value.code == "xai_device_token_failed"

    def test_a_pending_login_survives_the_process_that_started_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--no-wait` and `--resume` are different processes, as is a restarted gateway."""
        _install_login(
            monkeypatch,
            [_response(DEVICE_PAYLOAD, url=oauth.XAI_OAUTH_DEVICE_CODE_URL)],
        )
        pending = oauth.start_device_login()

        # Nothing in memory: a fresh reader sees it only because it is on disk.
        recovered = oauth.get_pending_login(pending.login_id)
        assert recovered is not None
        assert recovered.device_code == pending.device_code
        assert oauth.latest_pending_login() is not None

    def test_an_expired_pending_login_is_swept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_login(
            monkeypatch,
            [
                _response(
                    {**DEVICE_PAYLOAD, "expires_in": 1},
                    url=oauth.XAI_OAUTH_DEVICE_CODE_URL,
                )
            ],
        )
        pending = oauth.start_device_login()
        monkeypatch.setattr(oauth.time, "time", lambda: pending.expires_at + 1)
        assert oauth.get_pending_login(pending.login_id) is None
        assert oauth.latest_pending_login() is None

    def test_completing_a_login_clears_its_pending_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A completed grant left on disk would be replayed by the next resume."""
        _install_login(
            monkeypatch,
            [_response(DEVICE_PAYLOAD, url=oauth.XAI_OAUTH_DEVICE_CODE_URL)],
        )
        pending = oauth.start_device_login()
        _install_refresh(monkeypatch, [])
        monkeypatch.setattr(
            oauth.httpx,
            "Client",
            lambda **kwargs: _FakeClient(
                [_response({"access_token": _jwt(3600), "refresh_token": "r-1"})]
            ),
        )

        complete, _interval = oauth.poll_device_login(pending)

        assert complete is True
        assert oauth.get_pending_login(pending.login_id) is None
        assert oauth.has_oauth_credentials() is True

    def test_the_client_id_is_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_XAI_OAUTH_CLIENT_ID", "my-own-registration")
        client = _install_login(
            monkeypatch,
            [
                _response(DEVICE_PAYLOAD, url=oauth.XAI_OAUTH_DEVICE_CODE_URL),
                _response({"access_token": _jwt(3600), "refresh_token": "r-1"}),
            ],
        )
        oauth.device_code_login(on_prompt=lambda *a: None, sleep=lambda _s: None)
        assert client.requests[0]["data"]["client_id"] == "my-own-registration"


# ── Refresh ─────────────────────────────────────────────────────────────────


class _FakeAsyncClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append({"url": url, "data": kwargs.get("data")})
        return self.responses.pop(0)


def _install_refresh(
    monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]
) -> _FakeAsyncClient:
    client = _FakeAsyncClient(responses)
    monkeypatch.setattr(oauth.httpx, "AsyncClient", lambda **kwargs: client)
    return client


def _stored(access_token: str, refresh_token: str = "r-1") -> None:
    oauth.write_oauth_state(
        {
            "tokens": {"access_token": access_token, "refresh_token": refresh_token},
            "discovery": {"token_endpoint": TOKEN_ENDPOINT},
            "base_url": oauth.DEFAULT_XAI_OAUTH_BASE_URL,
        }
    )


class TestResolveBearer:
    @pytest.mark.asyncio
    async def test_no_login_resolves_to_none(self) -> None:
        assert await oauth.resolve_oauth_bearer() is None

    @pytest.mark.asyncio
    async def test_a_live_token_is_returned_without_a_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = _jwt(6 * 3600)
        _stored(token)
        client = _install_refresh(monkeypatch, [])
        resolved = await oauth.resolve_oauth_bearer()
        assert resolved == (token, oauth.DEFAULT_XAI_OAUTH_BASE_URL)
        assert client.requests == []

    @pytest.mark.asyncio
    async def test_an_expiring_token_is_refreshed_and_stored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stored(_jwt(30))
        fresh = _jwt(6 * 3600)
        client = _install_refresh(
            monkeypatch, [_response({"access_token": fresh, "refresh_token": "r-2"})]
        )
        resolved = await oauth.resolve_oauth_bearer()
        assert resolved is not None and resolved[0] == fresh
        assert client.requests[0]["data"]["grant_type"] == "refresh_token"
        assert oauth.read_oauth_state()["tokens"]["refresh_token"] == "r-2"

    @pytest.mark.asyncio
    async def test_a_rotated_refresh_token_replaces_the_old_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """xAI refresh tokens are single-use; keeping the old one bricks the login."""
        _stored(_jwt(30), refresh_token="r-old")
        _install_refresh(
            monkeypatch, [_response({"access_token": _jwt(3600), "refresh_token": "r-new"})]
        )
        await oauth.resolve_oauth_bearer()
        assert oauth.read_oauth_state()["tokens"]["refresh_token"] == "r-new"

    @pytest.mark.asyncio
    async def test_skew_is_recomputed_for_the_token_found_under_the_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """proactive_skew_seconds depends on the specific token's own remaining
        lifetime -- a long-lived token uses the full hour of skew, a short
        device-code token a narrow 2 minutes, or it would be refreshed on
        every resolution. If a concurrent caller refreshes first, the
        re-read under the lock can find a token in a different lifetime
        class than the one the pre-lock skew was computed from; reusing that
        stale skew applies the wrong threshold to it."""
        # Pre-lock: 50 minutes left is past the 45-minute short-token cutoff,
        # so this token's own skew is the full hour -- and 50 min < 1h makes
        # it look due for refresh from the outer, pre-lock check.
        old_state = {
            "tokens": {"access_token": _jwt(50 * 60), "refresh_token": "r-old"},
            "discovery": {"token_endpoint": TOKEN_ENDPOINT},
            "base_url": oauth.DEFAULT_XAI_OAUTH_BASE_URL,
        }
        # Under the lock: a concurrent caller already refreshed to a token
        # with 10 minutes left -- a short-lived token whose own 2-minute
        # skew does not call it expiring, so it must be accepted as-is.
        new_state = {
            "tokens": {"access_token": _jwt(10 * 60), "refresh_token": "r-new"},
            "discovery": {"token_endpoint": TOKEN_ENDPOINT},
            "base_url": oauth.DEFAULT_XAI_OAUTH_BASE_URL,
        }
        calls = {"n": 0}

        def fake_read() -> dict[str, Any]:
            calls["n"] += 1
            return old_state if calls["n"] == 1 else new_state

        monkeypatch.setattr(oauth, "read_oauth_state", fake_read)
        client = _install_refresh(monkeypatch, [])  # a refresh call here is the bug

        resolved = await oauth.resolve_oauth_bearer()

        assert resolved == (
            new_state["tokens"]["access_token"],
            oauth.DEFAULT_XAI_OAUTH_BASE_URL,
        )
        assert client.requests == []

    @pytest.mark.asyncio
    async def test_a_403_is_reported_as_a_tier_gate_not_a_relogin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-logging in cannot fix an entitlement problem, so do not suggest it."""
        _stored(_jwt(30))
        _install_refresh(monkeypatch, [_response({"error": "forbidden"}, status=403)])
        with pytest.raises(oauth.XaiOAuthError) as excinfo:
            await oauth.resolve_oauth_bearer()
        assert excinfo.value.code == "xai_oauth_tier_denied"
        assert excinfo.value.relogin_required is False
        # Tokens survive: the grant is still valid, the account just is not entitled.
        assert oauth.read_oauth_state()["tokens"]["refresh_token"] == "r-1"

    @pytest.mark.asyncio
    async def test_an_invalid_grant_quarantines_the_dead_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Next call should fail locally instead of making another doomed request."""
        _stored(_jwt(30))
        _install_refresh(monkeypatch, [_response({"error": "invalid_grant"}, status=400)])
        with pytest.raises(oauth.XaiOAuthError) as excinfo:
            await oauth.resolve_oauth_bearer()
        assert excinfo.value.relogin_required is True
        assert oauth.has_oauth_credentials() is False
        assert oauth.read_oauth_state()["last_auth_error"]["code"] == "xai_refresh_failed"

    @pytest.mark.asyncio
    async def test_a_5xx_does_not_quarantine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A transient upstream failure must not throw away a working login."""
        _stored(_jwt(30))
        _install_refresh(monkeypatch, [_response({"error": "oops"}, status=503)])
        with pytest.raises(oauth.XaiOAuthError) as excinfo:
            await oauth.resolve_oauth_bearer()
        assert excinfo.value.relogin_required is False
        assert oauth.has_oauth_credentials() is True

    @pytest.mark.asyncio
    async def test_a_login_without_a_refresh_token_asks_for_relogin(self) -> None:
        oauth.write_oauth_state({"tokens": {"access_token": _jwt(-10)}})
        with pytest.raises(oauth.XaiOAuthError) as excinfo:
            await oauth.resolve_oauth_bearer()
        assert excinfo.value.code == "xai_auth_missing_refresh_token"

    @pytest.mark.asyncio
    async def test_a_poisoned_token_endpoint_is_refused_on_the_refresh_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-validate the cached endpoint: the store may predate the pinning rule."""
        oauth.write_oauth_state(
            {
                "tokens": {"access_token": _jwt(30), "refresh_token": "r-1"},
                "discovery": {"token_endpoint": "https://attacker.example/token"},
            }
        )
        _install_refresh(monkeypatch, [_response({"access_token": _jwt(3600)})])
        with pytest.raises(oauth.XaiOAuthError) as excinfo:
            await oauth.resolve_oauth_bearer()
        assert excinfo.value.code == "xai_discovery_invalid"
