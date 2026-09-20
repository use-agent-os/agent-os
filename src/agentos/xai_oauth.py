"""xAI OAuth (SuperGrok / X Premium+) device-code login and token storage.

Ported from NousResearch/hermes-agent ``hermes_cli/auth.py`` (MIT, Copyright
(c) 2025 Nous Research) — see ``THIRD_PARTY_NOTICES.md``.

Why this exists
---------------
xAI sells API credit and subscriptions separately. Someone paying for SuperGrok
or X Premium+ has no API key and no way to spend their subscription through an
API key. OAuth is the only path that bills ``x_search`` against a subscription
they already hold.

Client identity
---------------
The default client id is xAI's public Grok CLI client — the scope literally
reads ``grok-cli:access`` — which is what every third-party client uses because
xAI publishes no self-service registration. It is a *public* OAuth client
(device-code, no secret), so nothing here is confidential. Override it with
``AGENTOS_XAI_OAUTH_CLIENT_ID`` if you have your own registration.

Design notes that differ from upstream
--------------------------------------
* **Availability never touches the network.** Upstream's ``check_fn`` refreshes
  the token every time the model's tool list is rebuilt, i.e. once per turn.
  Here :func:`has_oauth_credentials` reads local state only, and the refresh
  happens on the async call path where a round trip is already expected.
* **Refresh is async** so it cannot block the gateway event loop, and is
  serialized by an in-process lock: xAI's refresh tokens are single-use, and
  two concurrent refreshes race one another into ``invalid_grant``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from agentos.env import trust_env as _trust_env
from agentos.paths import default_agentos_home

log = structlog.get_logger(__name__)

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEFAULT_XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"

PROVIDER_ID = "xai-oauth"

# A SuperGrok access token can run hours; a device-code token is often ~15
# minutes. Refreshing an hour early suits the former and would refresh the
# latter on literally every resolution, burning single-use refresh tokens — so
# the skew shrinks for short-lived tokens.
MAX_REFRESH_SKEW_SECONDS = 3600
SHORT_TOKEN_SKEW_SECONDS = 120
SHORT_TOKEN_THRESHOLD_SECONDS = 45 * 60

_DEFAULT_TIMEOUT_SECONDS = 20.0
_MAX_POLL_INTERVAL_SECONDS = 30

_refresh_lock = asyncio.Lock()


class XaiOAuthError(Exception):
    """An xAI OAuth failure, with enough shape for callers to react."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "xai_oauth_error",
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.relogin_required = relogin_required


def client_id() -> str:
    return os.environ.get("AGENTOS_XAI_OAUTH_CLIENT_ID", "").strip() or (
        DEFAULT_XAI_OAUTH_CLIENT_ID
    )


# ---------------------------------------------------------------------------
# Endpoint pinning
# ---------------------------------------------------------------------------


def _validate_oauth_endpoint(url: str, *, field: str) -> str:
    """Refuse a discovery endpoint that is not HTTPS on the xAI origin.

    Discovery output is cached on disk, so one MITM at login time would
    otherwise substitute a ``token_endpoint`` that receives the refresh token
    on every future refresh — a permanent leak from a single interception.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise XaiOAuthError(
            f"xAI OIDC discovery returned a non-HTTPS {field}: {url!r}.",
            code="xai_discovery_invalid",
        )
    host = (parsed.hostname or "").lower()
    if not host or (host != "x.ai" and not host.endswith(".x.ai")):
        raise XaiOAuthError(
            f"xAI OIDC discovery {field} host {host!r} is not on the xAI origin. "
            "Refusing a cached endpoint that may have been substituted; log in again.",
            code="xai_discovery_invalid",
        )
    return url


def validate_inference_base_url(value: str, *, fallback: str = DEFAULT_XAI_OAUTH_BASE_URL) -> str:
    """Pin the OAuth-authenticated inference origin to xAI.

    An API key is something the operator pasted knowingly. An OAuth bearer is
    tied to a live subscription and refreshes itself, so a stray
    ``base_url`` override pointing elsewhere would quietly ship it to a third
    party on every call. Reject rather than trust, but fall back instead of
    raising: a bad override should not lock someone out of their own account.
    """
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return fallback
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host and (host == "x.ai" or host.endswith(".x.ai")):
        return candidate
    log.warning("xai_oauth.base_url_rejected", host=host or "<none>", fallback=fallback)
    return fallback


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------


def auth_store_path() -> Path:
    override = os.environ.get("AGENTOS_AUTH_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    return default_agentos_home() / "auth.json"


def _read_store() -> dict[str, Any]:
    path = auth_store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - a corrupt store must not be fatal
        log.warning("xai_oauth.store_unreadable", error=str(exc))
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_store(store: dict[str, Any]) -> None:
    """Write the store atomically, owner-only where the platform supports it.

    Windows has no POSIX mode bits, so the file there is protected by the
    profile directory's ACL rather than by ``chmod``. Mirrors the tolerant
    handling in :mod:`agentos.env_store`.
    """
    path = auth_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
    _restrict(tmp)
    tmp.replace(path)
    _restrict(path)


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - no-op on Windows
        pass


def _dict_field(source: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``source[key]`` when it is a dict, else an empty one."""
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def read_oauth_state() -> dict[str, Any]:
    providers = _read_store().get("providers")
    if not isinstance(providers, dict):
        return {}
    state = providers.get(PROVIDER_ID)
    return state if isinstance(state, dict) else {}


def write_oauth_state(state: dict[str, Any]) -> None:
    store = _read_store()
    providers = store.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers[PROVIDER_ID] = state
    store["providers"] = providers
    _write_store(store)


def clear_oauth_state() -> bool:
    """Forget the stored login. Returns whether anything was removed."""
    store = _read_store()
    providers = store.get("providers")
    if not isinstance(providers, dict) or PROVIDER_ID not in providers:
        return False
    providers.pop(PROVIDER_ID, None)
    store["providers"] = providers
    _write_store(store)
    return True


def _quarantine(reason: str, message: str) -> None:
    """Drop dead tokens so the next call fails locally instead of over the wire."""
    state = read_oauth_state()
    tokens = dict(state.get("tokens") or {})
    tokens.pop("access_token", None)
    tokens.pop("refresh_token", None)
    state["tokens"] = tokens
    state["last_auth_error"] = {
        "code": reason,
        "message": message,
        "relogin_required": True,
        "at": datetime.now(UTC).isoformat(),
    }
    write_oauth_state(state)


# ---------------------------------------------------------------------------
# Token inspection
# ---------------------------------------------------------------------------


def _jwt_expiry(token: str) -> float | None:
    if not isinstance(token, str) or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8"))
    except Exception:  # noqa: BLE001 - an opaque token is not an error
        return None
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, int | float) else None


def proactive_skew_seconds(access_token: str) -> int:
    expiry = _jwt_expiry(access_token)
    if expiry is None:
        return MAX_REFRESH_SKEW_SECONDS
    remaining = expiry - time.time()
    if 0 < remaining <= SHORT_TOKEN_THRESHOLD_SECONDS:
        return min(SHORT_TOKEN_SKEW_SECONDS, MAX_REFRESH_SKEW_SECONDS)
    return MAX_REFRESH_SKEW_SECONDS


def access_token_is_expiring(access_token: str, skew_seconds: int = 0) -> bool:
    expiry = _jwt_expiry(access_token)
    if expiry is None:
        return False
    return expiry <= time.time() + max(0, int(skew_seconds))


def has_oauth_credentials() -> bool:
    """Whether a login is on disk. Local only — never makes a request.

    Capability detection runs whenever the tool surface is rebuilt, so this
    must stay cheap. A token that turns out to be revoked surfaces as a call
    error, not as a tool that silently vanishes mid-session.
    """
    tokens = _dict_field(read_oauth_state(), "tokens")
    return bool(str(tokens.get("refresh_token") or "").strip()) or bool(
        str(tokens.get("access_token") or "").strip()
    )


def oauth_status() -> dict[str, Any]:
    """A description of the stored login. Never includes a token value."""
    state = read_oauth_state()
    tokens = _dict_field(state, "tokens")
    access = str(tokens.get("access_token") or "").strip()
    expiry = _jwt_expiry(access)
    return {
        "provider": PROVIDER_ID,
        "logged_in": has_oauth_credentials(),
        "has_refresh_token": bool(str(tokens.get("refresh_token") or "").strip()),
        "expires_at": (
            datetime.fromtimestamp(expiry, tz=UTC).isoformat() if expiry is not None else None
        ),
        "expiring_soon": access_token_is_expiring(access, proactive_skew_seconds(access)),
        "base_url": str(state.get("base_url") or DEFAULT_XAI_OAUTH_BASE_URL),
        "source": str(state.get("source") or ""),
        "last_refresh": state.get("last_refresh"),
        "last_auth_error": state.get("last_auth_error"),
        "store_path": str(auth_store_path()),
    }


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def _discover_sync(timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> dict[str, str]:
    try:
        response = httpx.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=timeout_seconds,
            trust_env=_trust_env(),
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - one error shape for every failure mode
        raise XaiOAuthError(
            f"xAI OIDC discovery failed: {exc}", code="xai_discovery_failed"
        ) from exc
    if not isinstance(payload, dict):
        raise XaiOAuthError(
            "xAI OIDC discovery response was not a JSON object.", code="xai_discovery_invalid"
        )
    authorization_endpoint = str(payload.get("authorization_endpoint") or "").strip()
    token_endpoint = str(payload.get("token_endpoint") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise XaiOAuthError(
            "xAI OIDC discovery response was missing required endpoints.",
            code="xai_discovery_incomplete",
        )
    _validate_oauth_endpoint(authorization_endpoint, field="authorization_endpoint")
    _validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    return {
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
    }


def _request_device_code(client: httpx.Client) -> dict[str, Any]:
    response = client.post(
        XAI_OAUTH_DEVICE_CODE_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={"client_id": client_id(), "scope": XAI_OAUTH_SCOPE},
    )
    if response.status_code != 200:
        raise XaiOAuthError(
            f"xAI device-code request failed (HTTP {response.status_code}).",
            code="xai_device_code_failed",
        )
    payload = response.json()
    required = (
        "device_code",
        "user_code",
        "verification_uri",
        "expires_in",
        "interval",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise XaiOAuthError(
            f"xAI device-code response missing fields: {', '.join(missing)}",
            code="xai_device_code_invalid",
        )
    return dict(payload)


@dataclass(frozen=True)
class PendingDeviceLogin:
    """A device authorization awaiting the user's approval.

    Carries no secret the user must handle: ``user_code`` is meaningless
    without their own xAI session, and ``device_code`` is the client's half of
    a grant that only becomes a token once they approve it.
    """

    login_id: str
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: float
    token_endpoint: str
    discovery: dict[str, str]

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def as_json(self) -> dict[str, Any]:
        return {
            "login_id": self.login_id,
            "device_code": self.device_code,
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "interval": self.interval,
            "expires_at": self.expires_at,
            "token_endpoint": self.token_endpoint,
            "discovery": dict(self.discovery),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PendingDeviceLogin | None:
        try:
            return cls(
                login_id=str(raw["login_id"]),
                device_code=str(raw["device_code"]),
                user_code=str(raw["user_code"]),
                verification_uri=str(raw["verification_uri"]),
                interval=max(1, int(raw["interval"])),
                expires_at=float(raw["expires_at"]),
                token_endpoint=str(raw["token_endpoint"]),
                discovery=_dict_field(raw, "discovery"),
            )
        except (KeyError, TypeError, ValueError):
            return None


# Pending logins live in the token store rather than in memory, because the
# half that starts a login and the half that finishes it are often different
# processes: `agentos auth login --no-wait` in one shell, `--resume` in
# another, or an agent driving both through exec_command. Keeping them in RAM
# also meant a gateway restart silently voided a login the operator was in the
# middle of approving. Expiry is wall-clock for the same reason — a monotonic
# deadline means nothing to the next process.
_PENDING_KEY = "pending_logins"


def _read_pending() -> dict[str, Any]:
    return _dict_field(_read_store(), _PENDING_KEY)


def _write_pending(entries: dict[str, Any]) -> None:
    store = _read_store()
    store[_PENDING_KEY] = entries
    _write_store(store)


def _sweep_pending() -> dict[str, Any]:
    entries = _read_pending()
    live = {
        login_id: raw
        for login_id, raw in entries.items()
        if isinstance(raw, dict) and float(raw.get("expires_at") or 0) > time.time()
    }
    if len(live) != len(entries):
        _write_pending(live)
    return live


def _forget_pending(login_id: str) -> None:
    entries = _read_pending()
    if entries.pop(login_id, None) is not None:
        _write_pending(entries)


def _persist_login(payload: dict[str, Any], discovery: dict[str, str]) -> dict[str, Any]:
    state = {
        "tokens": {
            "access_token": str(payload.get("access_token") or "").strip(),
            "refresh_token": str(payload.get("refresh_token") or "").strip(),
            "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        },
        "discovery": discovery,
        "base_url": validate_inference_base_url(
            os.environ.get("XAI_BASE_URL", ""), fallback=DEFAULT_XAI_OAUTH_BASE_URL
        ),
        "source": "oauth-device-code",
        "last_refresh": datetime.now(UTC).isoformat(),
        "last_auth_error": None,
    }
    write_oauth_state(state)
    return state


def _new_client(timeout_seconds: float) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(max(20.0, float(timeout_seconds))),
        headers={"Accept": "application/json"},
        trust_env=_trust_env(),
    )


def start_device_login(
    *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
) -> PendingDeviceLogin:
    """Ask xAI for a device code and return what the user needs to see.

    Split from the polling half so a caller that cannot block — a Web UI
    request, or an agent turn — can show the code immediately and poll on its
    own schedule. :func:`device_code_login` is the blocking composition of the
    two, for the CLI.
    """
    _sweep_pending()
    discovery = _discover_sync(timeout_seconds)
    with _new_client(timeout_seconds) as client:
        device = _request_device_code(client)

    pending = PendingDeviceLogin(
        login_id=uuid.uuid4().hex,
        device_code=str(device["device_code"]),
        user_code=str(device["user_code"]),
        verification_uri=str(
            device.get("verification_uri_complete") or device["verification_uri"]
        ),
        interval=max(1, int(device["interval"])),
        expires_at=time.time() + max(1, int(device["expires_in"])),
        token_endpoint=discovery["token_endpoint"],
        discovery=discovery,
    )
    entries = _sweep_pending()
    entries[pending.login_id] = pending.as_json()
    _write_pending(entries)
    return pending


def get_pending_login(login_id: str) -> PendingDeviceLogin | None:
    raw = _sweep_pending().get(login_id)
    return PendingDeviceLogin.from_json(raw) if isinstance(raw, dict) else None


def latest_pending_login() -> PendingDeviceLogin | None:
    """The most recently started login still awaiting approval, if any.

    Lets a caller resume without having to carry the id around — the common
    case, since only one login is ever in flight for one operator.
    """
    candidates = [
        parsed
        for raw in _sweep_pending().values()
        if isinstance(raw, dict) and (parsed := PendingDeviceLogin.from_json(raw)) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.expires_at)


def poll_device_login(
    pending: PendingDeviceLogin, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
) -> tuple[bool, int]:
    """Poll once. Returns ``(complete, next_interval)``.

    On completion the tokens are persisted and the pending entry is dropped.
    A denial or an expiry raises; ``authorization_pending`` simply returns
    ``False`` so the caller can wait and ask again.
    """
    if pending.expired:
        _forget_pending(pending.login_id)
        raise XaiOAuthError(
            "The xAI device authorization expired before it was approved.",
            code="xai_device_code_timeout",
        )

    with _new_client(timeout_seconds) as client:
        response = client.post(
            pending.token_endpoint,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id(),
                "device_code": pending.device_code,
            },
        )

    if response.status_code == 200:
        payload = response.json()
        if not payload.get("access_token") or not payload.get("refresh_token"):
            _forget_pending(pending.login_id)
            raise XaiOAuthError(
                "xAI device-code token response was missing required tokens.",
                code="xai_device_token_invalid",
            )
        _forget_pending(pending.login_id)
        _persist_login(dict(payload), pending.discovery)
        return True, pending.interval

    try:
        error_payload = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON error is still terminal
        _forget_pending(pending.login_id)
        raise XaiOAuthError(
            f"xAI device-code polling returned HTTP {response.status_code}.",
            code="xai_device_token_failed",
        ) from None

    error_code = str(error_payload.get("error") or "")
    if error_code == "authorization_pending":
        return False, pending.interval
    if error_code == "slow_down":
        widened = min(pending.interval + 1, _MAX_POLL_INTERVAL_SECONDS)
        entries = _read_pending()
        entries[pending.login_id] = replace(pending, interval=widened).as_json()
        _write_pending(entries)
        return False, widened

    _forget_pending(pending.login_id)
    detail = error_payload.get("error_description") or error_code or (
        f"HTTP {response.status_code}"
    )
    raise XaiOAuthError(
        f"xAI device-code polling failed: {detail}", code="xai_device_token_failed"
    )


def device_code_login(
    *,
    on_prompt: Any,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Run the whole device-code flow, blocking until approval. Used by the CLI.

    ``on_prompt(verification_url, user_code, interval)`` fires once the codes
    are known, so the caller decides how to present them — nothing here opens a
    browser or writes to stdout on its own.
    """
    pending = start_device_login(timeout_seconds=timeout_seconds)
    on_prompt(pending.verification_uri, pending.user_code, pending.interval)

    interval = pending.interval
    while True:
        complete, interval = poll_device_login(pending, timeout_seconds=timeout_seconds)
        if complete:
            return read_oauth_state()
        sleep(interval)
        refreshed = get_pending_login(pending.login_id)
        if refreshed is None:
            raise XaiOAuthError(
                "The xAI device authorization expired before it was approved.",
                code="xai_device_code_timeout",
            )
        pending = refreshed


async def _refresh(
    refresh_token: str, token_endpoint: str, timeout_seconds: float
) -> dict[str, Any]:
    _validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(max(5.0, timeout_seconds)),
        headers={"Accept": "application/json"},
        trust_env=_trust_env(),
    ) as client:
        response = await client.post(
            token_endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": client_id(),
                "refresh_token": refresh_token,
            },
        )

    if response.status_code == 403:
        # The grant exists but the account is not entitled to API access.
        # Logging in again cannot fix that, so do not suggest it.
        raise XaiOAuthError(
            "xAI refused this OAuth account for API access (HTTP 403). xAI restricts "
            "API/OAuth use to certain SuperGrok tiers even when the in-app subscription "
            "is active. Logging in again will not change it — set XAI_API_KEY instead, "
            "or upgrade at https://x.ai/grok.",
            code="xai_oauth_tier_denied",
            relogin_required=False,
        )
    if response.status_code != 200:
        raise XaiOAuthError(
            f"xAI token refresh failed (HTTP {response.status_code}).",
            code="xai_refresh_failed",
            relogin_required=response.status_code in {400, 401},
        )

    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise XaiOAuthError(
            "xAI token refresh returned invalid JSON.",
            code="xai_refresh_invalid_json",
            relogin_required=True,
        ) from exc
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise XaiOAuthError(
            "xAI token refresh response was missing access_token.",
            code="xai_refresh_invalid_response",
            relogin_required=True,
        )
    return {
        "access_token": access_token,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
    }


async def resolve_oauth_bearer(
    *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
) -> tuple[str, str] | None:
    """Return ``(access_token, base_url)``, refreshing first if it is due.

    ``None`` means no login is stored. A stored-but-broken login raises, so the
    caller can report why instead of silently falling back to an API key that
    the user may not have.
    """
    state = read_oauth_state()
    tokens = _dict_field(state, "tokens")
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token and not refresh_token:
        return None

    base_url = validate_inference_base_url(
        str(state.get("base_url") or ""), fallback=DEFAULT_XAI_OAUTH_BASE_URL
    )
    skew = proactive_skew_seconds(access_token)
    if access_token and not access_token_is_expiring(access_token, skew):
        return access_token, base_url

    if not refresh_token:
        raise XaiOAuthError(
            "The stored xAI login has no refresh token. Run `agentos auth login xai`.",
            code="xai_auth_missing_refresh_token",
            relogin_required=True,
        )

    async with _refresh_lock:
        # Re-read under the lock: a concurrent caller may already have
        # refreshed, and xAI's refresh tokens are single-use. The skew is
        # recomputed too -- it depends on the specific token's own remaining
        # lifetime (short device-code tokens use a narrow skew so they are
        # not refreshed on every resolution), so reusing the pre-lock value
        # against whatever token turns up here would apply the wrong
        # threshold whenever a concurrent refresh changed the token's
        # lifetime class while this caller was waiting on the lock.
        state = read_oauth_state()
        tokens = _dict_field(state, "tokens")
        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        skew = proactive_skew_seconds(access_token)
        if access_token and not access_token_is_expiring(access_token, skew):
            return access_token, base_url
        if not refresh_token:
            raise XaiOAuthError(
                "The stored xAI login has no refresh token. Run `agentos auth login xai`.",
                code="xai_auth_missing_refresh_token",
                relogin_required=True,
            )

        discovery = _dict_field(state, "discovery")
        token_endpoint = str(discovery.get("token_endpoint") or "").strip()
        if not token_endpoint:
            token_endpoint = _discover_sync(timeout_seconds)["token_endpoint"]

        try:
            refreshed = await _refresh(refresh_token, token_endpoint, timeout_seconds)
        except XaiOAuthError as exc:
            if exc.relogin_required:
                _quarantine(exc.code, str(exc))
            raise

        state["tokens"] = refreshed
        state["last_refresh"] = datetime.now(UTC).isoformat()
        state["last_auth_error"] = None
        write_oauth_state(state)
        return refreshed["access_token"], base_url
