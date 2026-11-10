"""Web Push for approval prompts: VAPID keys, subscriptions, encryption, delivery.

A browser that opted in (the console's "Push notifications for approvals"
toggle) hands the gateway a push subscription. When the approval queue
persists a request, every subscription gets an encrypted message the
browser's service worker turns into a notification -- so a phone with the
console tab closed still hears about it. When the request is resolved, a
replacement with the same notification tag says it is no longer pending.

Wire format, standards only, no third-party push library:

* Message encryption: RFC 8291 (``aes128gcm`` content coding, RFC 8188) with
  an ephemeral P-256 ECDH key per message.
* Sender identity: RFC 8292 VAPID, an ES256 JWT signed with a key pair
  generated once into the state directory (``web_push_vapid.json``, 0600).
* Crypto primitives come from ``cryptography``, which the MCP SDK already
  installs (it is deliberately not a direct dependency).
* Headers: ``TTL`` = seconds until the approval expires, ``Urgency: high``,
  ``Topic`` = the first 32 hex chars of sha256(approval id) so a newer message
  replaces one the push service still holds, and the payload carries the
  notification ``tag`` ``agentos-approval-<id>``.

The HTTP transport is injectable; tests never touch the network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
import struct
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import structlog

if TYPE_CHECKING:
    from agentos.application.approval_queue import PendingApproval

log = structlog.get_logger(__name__)

VAPID_FILENAME = "web_push_vapid.json"
SUBSCRIPTIONS_FILENAME = "web_push.sqlite"
#: RFC 8188 record size advertised in the header; a message is one record.
RECORD_SIZE = 4096
#: Push services must accept 4096 bytes of *encrypted* body (RFC 8291 §4):
#: the 86-byte header (salt, record size, key id length, 65-byte key), the
#: padding delimiter and the 16-byte AEAD tag come out of that.
MAX_PLAINTEXT_BYTES = 4096 - 86 - 1 - 16
#: A push service drops a message after its TTL; a resolved notice outlives
#: the request so a device that was offline still has the prompt cleared.
RESOLVED_TTL_SECONDS = 24 * 60 * 60
#: Consecutive delivery failures (other than 404/410) before a subscription
#: is dropped as dead.
MAX_FAILURES = 5
JWT_LIFETIME_SECONDS = 12 * 60 * 60
SUMMARY_MAX_CHARS = 180


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padded = value.strip() + "=" * (-len(value.strip()) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


# ---------------------------------------------------------------------------
# VAPID key pair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VapidKeys:
    private_key: Any  # cryptography EllipticCurvePrivateKey
    public_key_b64: str

    @classmethod
    def generate(cls) -> VapidKeys:
        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        return cls(private_key=private_key, public_key_b64=_public_b64(private_key))

    @classmethod
    def load_or_create(cls, state_dir: str | Path) -> VapidKeys:
        """Read the gateway's key pair, generating it on first use (mode 0600)."""
        from cryptography.hazmat.primitives import serialization

        path = Path(state_dir) / VAPID_FILENAME
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            private_key = serialization.load_pem_private_key(
                str(data["private_pem"]).encode("ascii"), password=None
            )
            if (path.stat().st_mode & 0o777) != 0o600:
                os.chmod(path, 0o600)
            return cls(private_key=private_key, public_key_b64=_public_b64(private_key))
        keys = cls.generate()
        pem = keys.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"private_pem": pem, "public_key": keys.public_key_b64}, handle)
        log.info("web_push.vapid_keys_generated", path=str(path))
        return keys


def _public_b64(private_key: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64url_encode(raw)


def _jwt_audience(endpoint: str) -> str:
    """The push service origin: scheme and host, plus the port only when it is not the default."""
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme)
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def vapid_authorization(
    keys: VapidKeys, endpoint: str, subject: str, *, now: float | None = None
) -> str:
    """The ``Authorization: vapid t=..., k=...`` value for ``endpoint`` (RFC 8292)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    audience = _jwt_audience(endpoint)
    issued = int(now if now is not None else time.time())
    header = b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64url_encode(
        json.dumps(
            {"aud": audience, "exp": issued + JWT_LIFETIME_SECONDS, "sub": subject},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    der = keys.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{claims}.{signature}, k={keys.public_key_b64}"


# ---------------------------------------------------------------------------
# RFC 8291 message encryption
# ---------------------------------------------------------------------------


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt_payload(
    plaintext: bytes,
    ua_public_b64: str,
    auth_secret_b64: str,
    *,
    salt: bytes | None = None,
    sender_private_key: Any = None,
) -> bytes:
    """Encrypt ``plaintext`` for one subscription; the result is the request body."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise ValueError(f"push payload exceeds {MAX_PLAINTEXT_BYTES} bytes")
    ua_public = b64url_decode(ua_public_b64)
    auth_secret = b64url_decode(auth_secret_b64)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_private = sender_private_key or ec.generate_private_key(ec.SECP256R1())
    as_public = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = as_private.exchange(ec.ECDH(), ua_key)
    # Combine the ECDH secret with the subscription's auth secret.
    prk_key = _hmac_sha256(auth_secret, shared)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hmac_sha256(prk_key, key_info + b"\x01")
    salt = salt if salt is not None else os.urandom(16)
    prk = _hmac_sha256(salt, ikm)
    cek = _hmac_sha256(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac_sha256(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    # One record, so it is also the last: padding delimiter 0x02.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!L", RECORD_SIZE) + bytes([len(as_public)]) + as_public
    return header + ciphertext


def decrypt_payload(body: bytes, ua_private_key: Any, auth_secret_b64: str) -> bytes:
    """Inverse of :func:`encrypt_payload` from the browser's side (tests, diagnostics)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = body[:16]
    idlen = body[20]
    as_public = body[21 : 21 + idlen]
    ciphertext = body[21 + idlen :]
    ua_public = ua_private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    shared = ua_private_key.exchange(ec.ECDH(), as_key)
    prk_key = _hmac_sha256(b64url_decode(auth_secret_b64), shared)
    ikm = _hmac_sha256(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac_sha256(salt, ikm)
    cek = _hmac_sha256(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac_sha256(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    padded = AESGCM(cek).decrypt(nonce, ciphertext, None)
    return padded.rstrip(b"\x00")[:-1]


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PushSubscription:
    endpoint: str
    p256dh: str
    auth: str


def _is_public_host(host: str) -> bool:
    value = host.strip("[]").lower()
    if not value or value == "localhost" or value.endswith((".local", ".localhost")):
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return address.is_global


def validate_subscription(endpoint: Any, keys: Any) -> PushSubscription:
    """Check a browser ``PushSubscription.toJSON()`` before storing it.

    The endpoint is where the gateway will POST, so it must be an ``https``
    URL on a public host -- never a loopback or private address.
    """
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("params.endpoint is required")
    endpoint = endpoint.strip()
    if len(endpoint) > 2048:
        raise ValueError("params.endpoint is too long")
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
    except ValueError as exc:
        raise ValueError("params.endpoint is not a URL") from exc
    if parts.scheme != "https" or not _is_public_host(host):
        raise ValueError("params.endpoint must be an https URL on a public host")
    if not isinstance(keys, dict):
        raise ValueError("params.keys must be an object with p256dh and auth")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        raise ValueError("params.keys.p256dh and params.keys.auth are required")
    try:
        public = b64url_decode(p256dh)
        secret = b64url_decode(auth)
    except (ValueError, TypeError) as exc:
        raise ValueError("params.keys must be base64url") from exc
    if len(public) != 65 or public[0] != 0x04:
        raise ValueError("params.keys.p256dh must be an uncompressed P-256 point")
    if len(secret) != 16:
        raise ValueError("params.keys.auth must be 16 bytes")
    return PushSubscription(endpoint=endpoint, p256dh=p256dh.strip(), auth=auth.strip())


class SubscriptionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS web_push_subscriptions ("
            " endpoint TEXT PRIMARY KEY,"
            " p256dh TEXT NOT NULL,"
            " auth TEXT NOT NULL,"
            " created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL,"
            " last_status INTEGER,"
            " failures INTEGER NOT NULL DEFAULT 0)"
        )
        self._conn.commit()
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def upsert(self, subscription: PushSubscription) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO web_push_subscriptions "
                "(endpoint, p256dh, auth, created_at, updated_at, failures) "
                "VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
                "auth = excluded.auth, updated_at = excluded.updated_at, failures = 0",
                (subscription.endpoint, subscription.p256dh, subscription.auth, now, now),
            )
            self._conn.commit()

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def all(self) -> list[PushSubscription]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT endpoint, p256dh, auth FROM web_push_subscriptions"
            ).fetchall()
        return [PushSubscription(row["endpoint"], row["p256dh"], row["auth"]) for row in rows]

    def record(self, endpoint: str, status: int | None) -> None:
        """Note one delivery: 404/410 or repeated failures drop the subscription."""
        with self._lock:
            if status in (404, 410):
                self._conn.execute(
                    "DELETE FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,)
                )
            elif status is not None and 200 <= status < 300:
                self._conn.execute(
                    "UPDATE web_push_subscriptions SET last_status = ?, failures = 0 "
                    "WHERE endpoint = ?",
                    (status, endpoint),
                )
            else:
                self._conn.execute(
                    "UPDATE web_push_subscriptions SET last_status = ?, failures = failures + 1 "
                    "WHERE endpoint = ?",
                    (status, endpoint),
                )
                self._conn.execute(
                    "DELETE FROM web_push_subscriptions WHERE endpoint = ? AND failures >= ?",
                    (endpoint, MAX_FAILURES),
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PushRequest:
    endpoint: str
    body: bytes
    headers: dict[str, str]


#: ``transport(request) -> HTTP status`` (``None`` when the request failed).
PushTransport = Callable[[PushRequest], Awaitable[int | None]]


async def httpx_transport(request: PushRequest) -> int | None:
    """POST one message; the connection is validated at connect time.

    The client refuses to dial a private, loopback, link-local or reserved
    address whatever the hostname resolves to *now*, so a subscription whose
    name was public at subscribe time cannot be rebound onto the gateway's
    own network.
    """
    import httpx

    from agentos.tools.ssrf_client import ssrf_guarded_client, validate_fetch_address
    from agentos.tools.types import SSRFBlockedError

    try:
        async with ssrf_guarded_client(
            validator=validate_fetch_address, timeout=10.0, follow_redirects=False
        ) as client:
            response = await client.post(
                request.endpoint, content=request.body, headers=request.headers
            )
    except SSRFBlockedError:
        log.warning("web_push.endpoint_blocked", reason="resolves to a non-public address")
        return None
    except httpx.HTTPError as exc:
        log.info("web_push.delivery_failed", error=type(exc).__name__)
        return None
    return response.status_code


async def assert_endpoint_resolves_publicly(endpoint: str) -> None:
    """Resolve the endpoint's host now and refuse any non-public address.

    The literal check in :func:`validate_subscription` cannot see that a name
    like ``localtest.me`` points at ``127.0.0.1``.
    """
    import anyio

    from agentos.tools.ssrf_client import _getaddrinfo_addresses, validate_fetch_address
    from agentos.tools.types import SSRFBlockedError

    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    port = parts.port or 443
    try:
        addresses = await anyio.to_thread.run_sync(_getaddrinfo_addresses, host, port)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"params.endpoint host does not resolve: {host}") from exc
    if not addresses:
        raise ValueError(f"params.endpoint host does not resolve: {host}")
    for address in addresses:
        try:
            validate_fetch_address(host, address)
        except SSRFBlockedError as exc:
            raise ValueError("params.endpoint must resolve to a public address") from exc


def approval_topic(approval_id: str) -> str:
    return hashlib.sha256(approval_id.encode("utf-8")).hexdigest()[:32]


def approval_tag(approval_id: str) -> str:
    return f"agentos-approval-{approval_id}"


def _summary(entry: PendingApproval) -> str:
    from agentos.application.approval_events import approval_display_title

    text = " ".join(approval_display_title(entry.namespace, entry.params).split())
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text


#: Title of the note that replaces a pushed prompt, by how it was resolved.
RESOLVED_TITLES: dict[str, str] = {
    "approved": "Approval granted",
    "denied": "Approval denied",
    "expired": "Approval expired",
    "cancelled": "Approval cancelled: its run was stopped",
}


def approval_payload(
    entry: PendingApproval, *, base_path: str, resolved: bool = False
) -> dict[str, Any]:
    """The JSON a subscribed service worker receives for ``entry``.

    A resolution replaces the prompt under the same ``tag``, titled by its
    ``decision`` (an expired or cancelled prompt too, not only a decided one).
    """
    base = base_path.rstrip("/")
    decision = entry.decision if resolved else None
    title = "Approval needed"
    if resolved:
        title = RESOLVED_TITLES.get(decision or "", "Approval no longer pending")
    payload: dict[str, Any] = {
        "type": "approval.resolved" if resolved else "approval.requested",
        "id": entry.approval_id,
        "namespace": entry.namespace,
        "title": title,
        "summary": _summary(entry),
        # The Inbox lists every pending approval and scrolls to this one.
        "url": f"{base}/inbox?approval={quote(entry.approval_id, safe='')}",
        "tag": approval_tag(entry.approval_id),
        "state": "resolved" if resolved else "pending",
    }
    if decision:
        payload["decision"] = decision
    if entry.audience_session_keys:
        payload["sessionKey"] = entry.audience_session_keys[-1]
    return payload


class WebPushService:
    """Approval listener that fans every request and resolution out to subscribers."""

    def __init__(
        self,
        *,
        keys: VapidKeys,
        store: SubscriptionStore,
        config: Any,
        transport: PushTransport | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        lifespan_end: Callable[[PendingApproval], float] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.keys = keys
        self.store = store
        self._config = config
        self._transport = transport or httpx_transport
        self._loop = loop
        self._lifespan_end = lifespan_end
        self._clock = clock
        self._tasks: set[asyncio.Future[Any]] = set()

    @classmethod
    def from_state_dir(cls, state_dir: str | Path, config: Any, **kwargs: Any) -> WebPushService:
        return cls(
            keys=VapidKeys.load_or_create(state_dir),
            store=SubscriptionStore(Path(state_dir) / SUBSCRIPTIONS_FILENAME),
            config=config,
            **kwargs,
        )

    @property
    def enabled(self) -> bool:
        push = getattr(self._config, "push", None)
        return bool(getattr(push, "web_enabled", True))

    def _subject(self) -> str:
        push = getattr(self._config, "push", None)
        return str(getattr(push, "vapid_subject", "") or "https://useagentos.dev")

    def _base_path(self) -> str:
        control_ui = getattr(self._config, "control_ui", None)
        return str(getattr(control_ui, "base_path", "/control") or "/control")

    def _ttl_for(self, entry: PendingApproval) -> int:
        if self._lifespan_end is not None:
            end = self._lifespan_end(entry)
        elif entry.expires_at is not None:
            end = entry.expires_at
        else:
            end = entry.created_at + 300.0
        return max(0, int(end - self._clock()))

    # -- ApprovalListener ---------------------------------------------------

    def on_requested(self, entry: PendingApproval) -> None:
        if not self.enabled:
            return
        ttl = self._ttl_for(entry)
        if ttl <= 0:
            return
        payload = approval_payload(entry, base_path=self._base_path())
        self._schedule(
            self.send_all(payload, ttl=ttl, urgency="high", topic=approval_topic(entry.approval_id))
        )

    def on_resolved(self, entry: PendingApproval) -> None:
        if not self.enabled:
            return
        payload = approval_payload(entry, base_path=self._base_path(), resolved=True)
        self._schedule(
            self.send_all(
                payload,
                ttl=RESOLVED_TTL_SECONDS,
                urgency="normal",
                topic=approval_topic(entry.approval_id),
            )
        )

    # -- sending ------------------------------------------------------------

    def _schedule(self, coro: Awaitable[Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task: asyncio.Future[Any] = asyncio.ensure_future(coro)
        elif self._loop is not None and self._loop.is_running():
            task = asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(coro, self._loop),  # type: ignore[arg-type]
                loop=self._loop,
            )
        else:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def build_request(
        self,
        subscription: PushSubscription,
        payload: dict[str, Any],
        *,
        ttl: int,
        urgency: str,
        topic: str,
    ) -> PushRequest:
        body = encrypt_payload(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            subscription.p256dh,
            subscription.auth,
        )
        headers = {
            "TTL": str(ttl),
            "Urgency": urgency,
            "Topic": topic,
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Authorization": vapid_authorization(
                self.keys, subscription.endpoint, self._subject(), now=self._clock()
            ),
        }
        return PushRequest(endpoint=subscription.endpoint, body=body, headers=headers)

    async def send_all(
        self, payload: dict[str, Any], *, ttl: int, urgency: str, topic: str
    ) -> list[int | None]:
        statuses: list[int | None] = []
        for subscription in self.store.all():
            try:
                request = self.build_request(
                    subscription, payload, ttl=ttl, urgency=urgency, topic=topic
                )
            except (ValueError, TypeError):
                log.warning("web_push.subscription_unusable", exc_info=True)
                self.store.remove(subscription.endpoint)
                statuses.append(None)
                continue
            status = await self._transport(request)
            self.store.record(subscription.endpoint, status)
            statuses.append(status)
        if statuses:
            log.info(
                "web_push.sent",
                type=payload.get("type"),
                approval_id=payload.get("id"),
                subscriptions=len(statuses),
                delivered=sum(1 for s in statuses if s is not None and 200 <= s < 300),
            )
        return statuses

    async def drain(self) -> None:
        """Wait for in-flight deliveries (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def close(self) -> None:
        self.store.close()


_service: WebPushService | None = None


def set_web_push_service(service: WebPushService | None) -> None:
    global _service
    _service = service


def get_web_push_service(config: Any = None) -> WebPushService | None:
    """The gateway's service; built on first use from ``config`` when unset."""
    global _service
    if _service is None and config is not None:
        state_dir = getattr(config, "state_dir", None)
        if state_dir:
            try:
                _service = WebPushService.from_state_dir(state_dir, config)
            except ImportError:
                # ``cryptography`` arrives with the MCP SDK; a stripped install
                # without it simply has no Web Push.
                log.warning("web_push.unavailable", reason="cryptography is not installed")
                return None
    return _service


__all__ = [
    "PushRequest",
    "PushSubscription",
    "PushTransport",
    "SubscriptionStore",
    "VapidKeys",
    "WebPushService",
    "approval_payload",
    "approval_tag",
    "approval_topic",
    "assert_endpoint_resolves_publicly",
    "b64url_decode",
    "b64url_encode",
    "decrypt_payload",
    "encrypt_payload",
    "get_web_push_service",
    "httpx_transport",
    "set_web_push_service",
    "validate_subscription",
    "vapid_authorization",
]
