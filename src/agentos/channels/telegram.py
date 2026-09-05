"""Telegram channel adapter backed by the public Bot API over HTTP."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx
import structlog
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agentos.channel_pairing import ChannelPairingStore, PairingStoreError
from agentos.channels._attachment_io import (
    attachment_limit_for_mime,
    ensure_declared_size_within_limit,
    fetch_httpx_bytes_limited,
    preferred_attachment_mime,
)
from agentos.channels._telegram_formatting import render_telegram_html
from agentos.channels._util import (
    AccessDecision,
    ChannelAccessPolicy,
    EventDedupeCache,
    FloodStrikeBackoff,
    StreamThrottle,
    check_channel_file_size,
)
from agentos.channels.contract import (
    ChannelCapabilities,
    ChannelCapabilityProfile,
    ChannelPlatformCapability,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
    ChannelPlatformManifest,
    ChannelSendResult,
)
from agentos.channels.types import Attachment, ChannelHealth, IncomingMessage, OutgoingMessage
from agentos.engine.native_commands import telegram_bot_commands
from agentos.env import trust_env as _trust_env
from agentos.gateway.audio_transcription import MAX_TRANSCRIPTION_BYTES

log = structlog.get_logger(__name__)

CAPABILITY_TIER = "GREEN-shipping"

DM_SAFETY_TIERS: tuple[str, ...] = ("safe", "confirm")

RETRYABLE_ERROR_CLASSES: tuple[str, ...] = (
    "transport_transient",
    "rate_limited",
    "channel_degraded",
)
FATAL_ERROR_CLASSES: tuple[str, ...] = (
    "auth_invalid",
    "payload_rejected",
    "target_missing",
    "contract_violation",
)

_DEFAULT_TIMEOUT_S = 30.0
_POLL_TIMEOUT_HEADROOM_S = 5.0
_CONNECT_RETRY_DELAYS_S = (0.25, 0.5)
#: Transport failures that happen before any request bytes are written, and so
#: can be retried without risking a duplicate Bot API call.
_PRE_SEND_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

#: ``TelegramApiError`` covers both "Telegram answered no" and "we never reached
#: Telegram". These substrings mark the second kind, which is not a verdict on
#: whatever was asked about — see :meth:`TelegramChannel.probe_target`.
_TRANSPORT_FAILURE_MARKERS = (
    "connection failed",
    "request failed",
    "returned invalid JSON",
    "returned an invalid response",
)
_DEDUPE_SIZE = 4096
_ALLOWED_UPDATES = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
)
#: Hard ceiling Telegram enforces on ``sendMessage``/``editMessageText`` text.
#: Measured on the *rendered* HTML, which is longer than the markdown it came from.
_MESSAGE_TEXT_LIMIT = 4096
#: Telegram tolerates roughly one edit per second per chat — well below Slack's
#: 500ms default, so streaming updates get their own slower cadence.
_STREAM_UPDATE_INTERVAL_MS = 1200
_STREAM_FLOOD_STRIKE_CAP = 3
_STREAM_FLOOD_DECAY_S = 30.0


class TelegramApiError(RuntimeError):
    """Raised when the Telegram Bot API returns ``ok: false``."""


class TelegramFloodError(TelegramApiError):
    """Raised when Telegram rate-limits a call (HTTP 429 / ``retry_after``).

    A subclass so every existing ``except TelegramApiError`` site keeps working;
    callers that care about flood control catch this one and read
    :attr:`retry_after`.
    """

    def __init__(self, message: str, retry_after: float = 1.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TelegramChannelConfig(BaseModel):
    """Adapter-level config for Telegram Bot API."""

    name: str = "telegram"
    token: str = ""
    default_chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    transport_name: Literal["polling", "webhook"] = "polling"
    webhook_path: str = "/telegram/events"
    webhook_url: str = ""
    webhook_secret_token: str = ""
    drop_pending_updates: bool = False
    poll_timeout_s: int = 30
    poll_limit: int = 100
    poll_idle_sleep_s: float = 0.1
    event_dedupe_size: int = _DEDUPE_SIZE
    allowed_updates: tuple[str, ...] = _ALLOWED_UPDATES
    groups_enabled: bool = False
    group_chat_ids: list[str] = Field(default_factory=list)
    group_mention_required: bool = True
    transcribe_voice: bool = False
    max_voice_duration_s: int = Field(default=120, gt=0)

    model_config = {}

    @field_validator("group_chat_ids", mode="before")
    @classmethod
    def _normalize_group_chat_ids(cls, value: Any) -> list[str]:
        values = value.split(",") if isinstance(value, str) else (value or [])
        normalized = (str(item).strip() for item in values)
        return list(dict.fromkeys(item for item in normalized if item))

    @model_validator(mode="after")
    def _validate_group_configuration(self) -> TelegramChannelConfig:
        if self.groups_enabled and not self.group_chat_ids:
            raise ValueError("telegram groups_enabled requires at least one group_chat_id")
        return self


def _coerce_telegram_int(value: Any) -> int | str:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return str(value)


def _slice_utf16(text: str, offset: int, length: int) -> str:
    """Slice ``text`` by a Telegram message-entity offset/length.

    Telegram entity ``offset``/``length`` are counted in UTF-16 code units
    (per the Bot API), while Python string indexing is by Unicode code point.
    Any non-BMP character (emoji, some CJK-ext, math letters) earlier in the
    message is one Python index but two UTF-16 units, so slicing the ``str``
    directly drifts and misaligns the extracted mention/command. Encode to
    UTF-16 and slice on the code-unit grid so the entity lines up exactly.
    """
    if offset < 0 or length < 0:
        return ""
    u16 = text.encode("utf-16-le")
    return u16[offset * 2 : (offset + length) * 2].decode("utf-16-le", errors="replace")


@dataclass
class TelegramChannel:
    """Managed adapter for Telegram Bot API polling or webhooks."""

    config: TelegramChannelConfig
    pairing_store: ChannelPairingStore = field(default_factory=ChannelPairingStore)

    supports_slash_commands: bool = True
    typing_keepalive_interval_s: ClassVar[float] = 4.0
    MAX_FILE_BYTES: ClassVar[int] = 50 * 1024 * 1024
    policy: ChannelAccessPolicy = field(
        default_factory=lambda: ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=True,
            mention_required_in_group=True,
            allowlist=frozenset(),
        )
    )
    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _owns_client: bool = field(default=False, init=False, repr=False)
    _poll_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _update_offset: int | None = field(default=None, init=False, repr=False)
    _dedupe: EventDedupeCache = field(init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _known_sender_profiles: dict[str, dict[str, str]] = field(
        default_factory=dict, init=False, repr=False
    )
    bot_user_id: str | None = None
    bot_username: str | None = None

    def __post_init__(self) -> None:
        self._dedupe = EventDedupeCache(max_size=self.config.event_dedupe_size)
        self._refresh_access_policy()

    def _refresh_access_policy(self) -> None:
        # Telegram uses ``evaluate_access`` for separate DM/group policies.
        # Keep this declaration open so generic dispatch behavior is unchanged
        # for adapters without a custom evaluator.
        self.policy = ChannelAccessPolicy(
            dm_allowed=True,
            group_allowed=self.config.groups_enabled,
            mention_required_in_group=self.config.group_mention_required,
        )

    @staticmethod
    def _sender_profile(message: IncomingMessage) -> dict[str, str]:
        sender_id = str(message.sender_id or "").strip()
        return {
            "sender_id": sender_id,
            "username": str(message.metadata.get("sender_username") or ""),
            "display_name": str(message.metadata.get("sender_display_name") or ""),
            "chat_id": str(message.channel_id or message.metadata.get("chat_id") or ""),
        }

    def _remember_sender(self, message: IncomingMessage) -> None:
        profile = self._sender_profile(message)
        if profile["sender_id"]:
            self._known_sender_profiles[profile["sender_id"]] = profile

    def record_access_denial(self, message: IncomingMessage, reason: str) -> None:
        """Create a durable pairing request for an unauthorized Telegram DM."""
        if reason != "not_paired" or bool(message.metadata.get("is_group")):
            return
        profile = self._sender_profile(message)
        sender_id = profile["sender_id"]
        if not sender_id:
            return
        self._known_sender_profiles[sender_id] = profile
        try:
            result = self.pairing_store.request(
                self.config.name,
                sender_id,
                profile=profile,
            )
        except PairingStoreError as exc:
            log.error(
                "telegram.pairing_store_error",
                channel=self.config.name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            message.metadata["pairing_request_status"] = "store_error"
            return
        message.metadata["pairing_request_status"] = result.status
        message.metadata["pairing_request_created"] = result.created
        if result.code:
            message.metadata["pairing_code"] = result.code
        if result.retry_after_s:
            message.metadata["pairing_retry_after_s"] = result.retry_after_s
        message.metadata["access_denial_reason"] = reason

    async def notify_access_denied(self, message: IncomingMessage) -> None:
        """Return a Hermes-style one-time pairing code to a new DM sender."""
        if not message.metadata.get("pairing_request_created"):
            return
        code = str(message.metadata.get("pairing_code") or "")
        if not code:
            return
        await self.send(
            self.build_reply_message(
                "Pairing code: "
                f"{code}\n\nIt expires in 1 hour. Approve this connection in "
                "AgentOS Control UI or run: "
                f"agentos channels pairing approve {self.config.name} {code}",
                message,
            )
        )

    def access_snapshot(self) -> dict[str, Any]:
        snapshot = self.pairing_store.snapshot(self.config.name)
        return {
            "pending": snapshot["pending"],
            "paired": snapshot["approved"],
            "locked_until": snapshot["locked_until"],
            "groups_enabled": self.config.groups_enabled,
            "group_chat_ids": list(self.config.group_chat_ids),
            "group_mention_required": self.config.group_mention_required,
        }

    def evaluate_access(
        self,
        message: IncomingMessage,
        *,
        is_group: bool,
        mentioned: bool,
    ) -> AccessDecision:
        sender_id = str(message.sender_id or "").strip()
        try:
            admission = self.pairing_store.admission(self.config.name, sender_id)
        except PairingStoreError as exc:
            log.error(
                "telegram.pairing_store_error",
                channel=self.config.name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            admission = None

        if is_group:
            if not self.config.groups_enabled:
                return AccessDecision(admit=False, reason="group_denied")
            if str(message.channel_id or "") not in self.config.group_chat_ids:
                return AccessDecision(admit=False, reason="group_not_configured")
            if self.config.group_mention_required and not mentioned:
                return AccessDecision(admit=False, reason="not_mentioned_in_group")
            if admission is None:
                return AccessDecision(admit=False, reason="not_paired")
            return AccessDecision(
                admit=True,
                reason="group_admitted",
                admission=admission,
                admission_validator=self.pairing_store.validate_admission,
            )

        if admission is not None:
            return AccessDecision(
                admit=True,
                reason="dm_admitted",
                admission=admission,
                admission_validator=self.pairing_store.validate_admission,
            )
        return AccessDecision(admit=False, reason="not_paired")

    def resolve_access_request(self, sender_id: str, *, approved: bool) -> dict[str, Any]:
        sender_id = str(sender_id).strip()
        pending = next(
            (
                item
                for item in self.pairing_store.snapshot(self.config.name)["pending"]
                if str(item.get("sender_id") or "") == sender_id
            ),
            None,
        )
        if pending is None:
            raise KeyError(f"Telegram pairing request not found: {sender_id}")
        if approved:
            return self.pairing_store.approve(self.config.name, str(pending["code"]))
        return self.pairing_store.deny(self.config.name, sender_id)

    def revoke_sender(self, sender_id: str) -> str:
        sender_id = str(sender_id).strip()
        self.pairing_store.revoke(self.config.name, sender_id)
        return "pairing"

    async def notify_access_resolution(
        self,
        request: dict[str, Any],
        *,
        approved: bool,
    ) -> None:
        chat_id = str(request.get("chat_id") or "")
        if not chat_id:
            return
        content = (
            "This Telegram connection is paired. Send your message again to continue."
            if approved
            else "This Telegram pairing request was denied."
        )
        await self.send(
            OutgoingMessage(content=content, reply_to=chat_id, metadata={"chat_id": chat_id})
        )

    @property
    def transport_name(self) -> str:
        return self.config.transport_name

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="telegram",
            group_chat=True,
            mentions=True,
            typing_indicator=True,
            streaming=True,
            native_file_upload=True,
            media=True,
            reply=True,
            thread_reply=True,
            edit=True,
            delete=True,
            transports=(self.config.transport_name,),
        )

    @property
    def platform_capability_manifest(self) -> ChannelPlatformManifest:
        return ChannelPlatformManifest.from_channel_profile(
            self.capability_profile,
            has_send_file=True,
            has_inbound_attachment_resolver=True,
        ).with_capabilities(
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.FILES,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("sendDocument", "getFile"),
                mutates=True,
                notes=("Telegram sends generated files with sendDocument.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("getFile",),
                notes=("Inbound Telegram files are resolved through getFile.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.THREADS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                notes=("Forum topic thread IDs are preserved when Telegram provides them.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_base,
                timeout=_DEFAULT_TIMEOUT_S,
                trust_env=_trust_env(),
            )
            self._owns_client = True
        return self._client

    def _safe_api_error_detail(self, value: Any, fallback: str) -> str:
        detail = str(value or fallback)
        if self.config.token:
            detail = detail.replace(self.config.token, "[REDACTED]")
        return detail[:1000]

    @staticmethod
    def _retry_after_seconds(payload: Any) -> float | None:
        """Pull ``parameters.retry_after`` out of a Bot API error body."""
        if not isinstance(payload, dict):
            return None
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            return None
        try:
            return float(parameters["retry_after"])
        except (KeyError, TypeError, ValueError):
            return None

    def _parse_api_response(self, response: Any, method: str) -> Any:
        """Validate a Bot API response without exposing the token-bearing URL."""
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            status_code = getattr(response, "status_code", "unknown")
            message = f"Telegram {method} failed with HTTP {status_code}"
            if status_code == 429:
                try:
                    body = response.json()
                except (TypeError, ValueError):
                    body = None
                raise TelegramFloodError(
                    message,
                    retry_after=self._retry_after_seconds(body) or 1.0,
                ) from None
            raise TelegramApiError(message) from None
        try:
            data = response.json()
        except (TypeError, ValueError):
            raise TelegramApiError(f"Telegram {method} returned invalid JSON") from None
        if not isinstance(data, dict):
            raise TelegramApiError(f"Telegram {method} returned an invalid response")
        if data.get("ok") is not True:
            fallback = f"Telegram {method} failed"
            detail = self._safe_api_error_detail(data.get("description"), fallback)
            # Telegram also answers ``ok: false`` with a 200 for some floods.
            if (retry_after := self._retry_after_seconds(data)) is not None:
                raise TelegramFloodError(detail, retry_after=retry_after)
            raise TelegramApiError(detail)
        return data.get("result")

    async def _api(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.config.token:
            raise ValueError("telegram API call requires token")
        client = self._get_client()
        request_payload = payload or {}
        request_timeout: float | None = None
        if method == "getUpdates":
            try:
                poll_timeout = float(request_payload.get("timeout") or 0)
            except (TypeError, ValueError):
                poll_timeout = 0.0
            request_timeout = max(
                _DEFAULT_TIMEOUT_S,
                poll_timeout + _POLL_TIMEOUT_HEADROOM_S,
            )
        for retry_delay in (*_CONNECT_RETRY_DELAYS_S, None):
            try:
                request_kwargs: dict[str, Any] = {"json": request_payload}
                if request_timeout is not None:
                    request_kwargs["timeout"] = request_timeout
                response = await client.post(f"/bot{self.config.token}/{method}", **request_kwargs)
                break
            except _PRE_SEND_CONNECT_ERRORS:
                # Nothing reached Telegram yet — DNS/TLS never completed, or we
                # never got a pooled connection to write to — so resending is
                # safe. ConnectTimeout and PoolTimeout are TimeoutException
                # siblings of ConnectError, not subclasses, so they have to be
                # named here or they fall through to the generic branch below
                # and fail on the first attempt. ReadTimeout deliberately stays
                # out: by then the request is in flight, and re-sending a
                # getUpdates long poll would double-poll it.
                if retry_delay is None:
                    raise TelegramApiError(f"Telegram {method} connection failed") from None
                log.warning(
                    "telegram.api_connect_retry",
                    method=method,
                    retry_in_s=retry_delay,
                )
                await asyncio.sleep(retry_delay)
            except httpx.RequestError:
                raise TelegramApiError(f"Telegram {method} request failed") from None
        return self._parse_api_response(response, method)

    async def start(self) -> None:
        if not self.config.token:
            raise ValueError("telegram.start: token is required")
        if self.config.transport_name == "webhook":
            if not self.config.webhook_url:
                raise ValueError("telegram.start: webhook_url is required for webhook mode")
            if not self.config.webhook_secret_token:
                raise ValueError(
                    "telegram.start: webhook_secret_token is required for webhook mode"
                )

        me = await self._api("getMe")
        if isinstance(me, dict):
            self.bot_user_id = str(me.get("id", "")) or None
            username = me.get("username")
            self.bot_username = str(username) if username else None

        try:
            await self.register_slash_commands()
        except TelegramApiError as exc:
            log.warning("telegram.commands_not_registered", error=str(exc))

        if self.config.transport_name == "webhook":
            if self.config.webhook_url:
                payload: dict[str, Any] = {
                    "url": self.config.webhook_url,
                    "drop_pending_updates": self.config.drop_pending_updates,
                    "allowed_updates": list(self.config.allowed_updates),
                }
                payload["secret_token"] = self.config.webhook_secret_token
                await self._api("setWebhook", payload)
        else:
            await self._api(
                "deleteWebhook",
                {"drop_pending_updates": self.config.drop_pending_updates},
            )
            self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram:poll")

        self._connected = True
        log.info(
            "telegram.started",
            name=self.config.name,
            transport=self.config.transport_name,
            bot_user_id=self.bot_user_id,
        )

    async def register_slash_commands(self) -> None:
        """Synchronize Telegram's native command menu with the channel registry."""
        commands = telegram_bot_commands()
        scope = {"type": "default"}
        await self._api("setMyCommands", {"commands": commands, "scope": scope})
        log.info("telegram.commands_registered", count=len(commands))

    async def stop(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None
        self._owns_client = False
        self._connected = False
        log.info("telegram.stopped", name=self.config.name)

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(
            connected=self._connected,
            bot_user_id=self.bot_user_id,
            last_message_at=self._last_message_at,
            extra={"transport": self.config.transport_name},
        )

    async def _poll_loop(self) -> None:
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    self._get_updates_payload(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - polling must survive transient faults.
                log.warning(
                    "telegram.poll_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                await asyncio.sleep(self.config.poll_idle_sleep_s)
                continue
            if not isinstance(updates, list):
                updates = []
            for update in updates:
                if not isinstance(update, dict):
                    continue
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._update_offset = update_id + 1
                if "callback_query" in update:
                    try:
                        await self._handle_telegram_callback(update["callback_query"])
                    except Exception as exc:
                        log.warning("telegram.callback_query_handle_failed", error=str(exc))
                    continue
                try:
                    msg = self.parse_incoming(update)
                except ValueError:
                    log.debug("telegram.unsupported_update_ignored", update_id=update_id)
                    continue
                self.enqueue(msg)
            if not updates:
                await asyncio.sleep(self.config.poll_idle_sleep_s)

    def _get_updates_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timeout": self.config.poll_timeout_s,
            "limit": self.config.poll_limit,
            "allowed_updates": list(self.config.allowed_updates),
        }
        if self._update_offset is not None:
            payload["offset"] = self._update_offset
        return payload

    def enqueue(self, message: IncomingMessage) -> None:
        self._remember_sender(message)
        msg_id = str(message.metadata.get("message_id", ""))
        update_id = message.metadata.get("update_id")
        dedupe_key = f"{update_id}:{msg_id}" if update_id is not None else msg_id
        if dedupe_key and not self._dedupe.check_and_add(dedupe_key):
            log.debug("telegram.duplicate_dropped", key=dedupe_key)
            return
        self._queue.put_nowait(message)
        self._last_message_at = datetime.now(UTC)

    async def receive(self) -> IncomingMessage:
        msg = await self._queue.get()
        self._last_message_at = datetime.now(UTC)
        return msg

    def create_webhook_route(self, path: str | None = None) -> Route:
        if not self.config.webhook_secret_token:
            raise ValueError("telegram webhook route requires webhook_secret_token")
        route_path = path or self.config.webhook_path
        return Route(route_path, endpoint=self._handle_webhook, methods=["POST"])

    async def _handle_webhook(self, request: Request) -> Response:
        secret = self.config.webhook_secret_token
        if not secret:
            return Response(status_code=503)
        header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(secret.encode("utf-8"), header_token.encode("utf-8")):
            return Response(status_code=401)
        try:
            update = await request.json()
        except Exception:
            return Response(status_code=400)
        if not isinstance(update, dict):
            return Response(status_code=400)
        if "callback_query" in update:
            try:
                await self._handle_telegram_callback(update["callback_query"])
            except Exception as exc:
                log.warning("telegram.callback_query_handle_failed", error=str(exc))
            return Response(status_code=200)
        try:
            msg = self.parse_incoming(update)
        except ValueError:
            log.debug("telegram.unsupported_update_ignored", update_id=update.get("update_id"))
            return Response(status_code=200)
        self.enqueue(msg)
        return Response(status_code=200)

    @staticmethod
    def _telegram_file_attachment(
        media: dict[str, Any],
        *,
        media_kind: str,
        default_name: str,
        default_mime: str | None = None,
    ) -> Attachment | None:
        file_id = media.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            return None
        name = media.get("file_name")
        if not isinstance(name, str) or not name.strip():
            unique = media.get("file_unique_id")
            suffix = str(unique) if unique else file_id
            name = f"{default_name}-{suffix}"
        mime = media.get("mime_type") if isinstance(media.get("mime_type"), str) else default_mime
        size = media.get("file_size") if isinstance(media.get("file_size"), int) else None
        duration = (
            media.get("duration") if isinstance(media.get("duration"), (int, float)) else None
        )
        metadata = {"telegram_file_id": file_id, "telegram_media_kind": media_kind}
        if duration is not None:
            metadata["duration"] = duration
        return Attachment(
            name=name,
            mime_type=mime,
            size=size,
            metadata=metadata,
        )

    def _telegram_media_attachments(self, msg: dict[str, Any]) -> list[Attachment]:
        attachments: list[Attachment] = []

        document = msg.get("document")
        if isinstance(document, dict):
            att = self._telegram_file_attachment(
                document,
                media_kind="document",
                default_name="telegram-document",
            )
            if att is not None:
                attachments.append(att)

        photo = msg.get("photo")
        if isinstance(photo, list) and photo:
            candidates = [p for p in photo if isinstance(p, dict)]
            if candidates:
                best = max(
                    candidates,
                    key=lambda p: (
                        int(p.get("file_size") or 0),
                        int(p.get("width") or 0) * int(p.get("height") or 0),
                    ),
                )
                att = self._telegram_file_attachment(
                    best,
                    media_kind="photo",
                    default_name="telegram-photo",
                    default_mime="image/jpeg",
                )
                if att is not None:
                    attachments.append(att)

        for key, default_name in (
            ("video", "telegram-video"),
            ("audio", "telegram-audio"),
            ("voice", "telegram-voice"),
            ("sticker", "telegram-sticker"),
            ("video_note", "telegram-video-note"),
        ):
            media = msg.get(key)
            if isinstance(media, dict):
                default_mime = "image/webp" if key == "sticker" else None
                att = self._telegram_file_attachment(
                    media,
                    media_kind=key,
                    default_name=default_name,
                    default_mime=default_mime,
                )
                if att is not None:
                    attachments.append(att)

        return attachments

    async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
        """Resolve Telegram file references into bytes; shared ingest validates."""

        if attachment.data is not None:
            return attachment
        file_id = attachment.metadata.get("telegram_file_id")
        if not isinstance(file_id, str) or not file_id:
            return attachment

        media_kind = attachment.metadata.get("telegram_media_kind")
        is_transcription_kind = media_kind in ("voice", "audio", "video_note")
        transcribe_enabled = getattr(self.config, "transcribe_voice", False)

        if transcribe_enabled and is_transcription_kind:
            # Check duration limit pre-download
            duration = attachment.metadata.get("duration")
            max_duration = getattr(self.config, "max_voice_duration_s", 120)
            if duration is not None and duration > max_duration:
                raise ValueError(
                    f"Audio clip exceeds the maximum duration of {max_duration} "
                    "seconds and could not be transcribed."
                )
            # Check size limit pre-download
            if attachment.size is not None and attachment.size > MAX_TRANSCRIPTION_BYTES:
                raise ValueError(
                    "Audio clip exceeds the maximum size of 30 MB and could not be transcribed."
                )
            limit = MAX_TRANSCRIPTION_BYTES
        else:
            limit = attachment_limit_for_mime(attachment.mime_type)

        ensure_declared_size_within_limit(attachment.size, name=attachment.name, limit=limit)
        file_info = await self._api("getFile", {"file_id": file_id})
        if not isinstance(file_info, dict):
            raise TelegramApiError("Telegram getFile returned invalid result")
        ensure_declared_size_within_limit(
            file_info.get("file_size"),
            name=attachment.name,
            limit=limit,
        )
        file_path = file_info.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise TelegramApiError("Telegram getFile returned no file_path")
        try:
            payload, content_type = await fetch_httpx_bytes_limited(
                self._get_client(),
                f"/file/bot{self.config.token}/{file_path}",
                name=attachment.name,
                limit=limit,
            )
        except httpx.HTTPError:
            raise TelegramApiError("Telegram file download failed") from None
        name = attachment.name
        if not name or name.startswith("telegram-"):
            path_name = file_path.rsplit("/", 1)[-1]
            if path_name:
                name = path_name
        return Attachment(
            name=name,
            mime_type=preferred_attachment_mime(content_type, attachment.mime_type),
            data=payload,
            size=len(payload),
            metadata={**attachment.metadata, "telegram_file_path": file_path},
        )

    async def _handle_telegram_callback(self, cb: dict[str, Any]) -> None:
        cb_id = cb.get("id")
        data = cb.get("data", "")
        if not data.startswith("approve:") and not data.startswith("deny:"):
            return

        act, approval_id = data.split(":", 1)
        approved = act == "approve"

        sender = cb.get("from", {})
        sender_id = str(sender.get("id") or "")
        msg = cb.get("message", {})
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id") or "")
        chat_type = chat.get("type", "")
        is_group = chat_type in {"group", "supergroup", "channel"}

        # 1. Admission Check
        temp_msg = IncomingMessage(
            sender_id=sender_id,
            channel_id=chat_id,
            content="",
        )
        decision = self.evaluate_access(temp_msg, is_group=is_group, mentioned=True)
        if not decision.admit:
            try:
                await self._api(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": cb_id,
                        "text": "Unauthorized: Only paired users can approve/deny tools.",
                        "show_alert": True,
                    },
                )
            except Exception as exc:
                log.warning("telegram.callback_unauthorized_answer_failed", error=str(exc))
            return

        from agentos.gateway.approval_queue import get_approval_queue

        queue = get_approval_queue()

        # 2. Retrieve PendingApproval and verify sessionKey match
        try:
            entry = queue.get(approval_id)
        except KeyError:
            try:
                await self._api(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": cb_id,
                        "text": "Error: Approval request not found or expired.",
                        "show_alert": True,
                    },
                )
            except Exception as exc:
                log.warning("telegram.callback_keyerror_answer_failed", error=str(exc))
            return

        session_key = entry.params.get("sessionKey")
        if isinstance(session_key, str) and session_key:
            parts = session_key.split(":")
            if parts and parts[0] == "subagent":
                parts = parts[1:]
            if len(parts) >= 5:
                session_channel = parts[2]
                session_mode = parts[3]
                session_peer = parts[4]
                expected_peer = chat_id if session_mode in ("group", "channel") else sender_id
                if session_channel != self.config.name or session_peer != expected_peer:
                    try:
                        await self._api(
                            "answerCallbackQuery",
                            {
                                "callback_query_id": cb_id,
                                "text": "Unauthorized: Approval does not belong to this chat.",
                                "show_alert": True,
                            },
                        )
                    except Exception as exc:
                        log.warning("telegram.callback_mismatch_answer_failed", error=str(exc))
                    return

        # 3. Resolve Approval Queue
        try:
            queue.resolve(approval_id, approved)
        except ValueError:
            try:
                await self._api(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": cb_id,
                        "text": "Error: Approval request was already resolved.",
                        "show_alert": True,
                    },
                )
            except Exception as exc:
                log.warning("telegram.callback_valueerror_answer_failed", error=str(exc))
            return

        # 4. Answer Callback Query and Edit message text
        try:
            await self._api("answerCallbackQuery", {"callback_query_id": cb_id})
        except Exception as exc:
            log.warning("telegram.callback_query_answer_failed", error=str(exc))

        message_id = msg.get("message_id")
        orig_text = msg.get("text", "")
        decision_text = "Approved ✅" if approved else "Denied ❌"
        new_text = f"{orig_text}\n\n<b>{decision_text}</b>"

        if chat_id and message_id:
            try:
                await self._api(
                    "editMessageText",
                    {
                        "chat_id": str(chat_id),
                        "message_id": message_id,
                        "text": new_text,
                        "parse_mode": "HTML",
                        "reply_markup": None,
                    },
                )
            except Exception as exc:
                log.warning("telegram.callback_message_edit_failed", error=str(exc))

        # 5. Enqueue the virtual message
        metadata = {
            "is_group": is_group,
            "chat_type": chat_type,
            "chat_id": chat_id,
            "message_id": str(msg.get("message_id", "")),
        }
        username = sender.get("username")
        if username:
            metadata["sender_username"] = str(username)

        virtual_msg = IncomingMessage(
            sender_id=sender_id,
            channel_id=chat_id,
            content="Approve" if approved else "Deny",
            metadata=metadata,
        )
        self.enqueue(virtual_msg)

    def parse_incoming(self, update: dict[str, Any]) -> IncomingMessage:
        msg = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
        )
        if not isinstance(msg, dict):
            raise ValueError("Telegram update did not contain a supported message payload")
        chat = msg.get("chat", {}) or {}
        sender = msg.get("from", {}) or {}
        chat_type = chat.get("type", "")
        is_group = chat_type in {"group", "supergroup", "channel"}
        message_id = msg.get("message_id", "")

        metadata = {
            "is_group": is_group,
            "chat_type": chat_type,
            "chat_id": str(chat.get("id", self.config.default_chat_id)),
            "message_id": str(message_id),
        }
        username = sender.get("username")
        if username:
            metadata["sender_username"] = str(username)
        if self.bot_username:
            metadata["bot_username"] = self.bot_username
        display_name = " ".join(
            str(sender.get(key) or "").strip() for key in ("first_name", "last_name")
        ).strip()
        if display_name:
            metadata["sender_display_name"] = display_name
        if (update_id := update.get("update_id")) is not None:
            metadata["update_id"] = update_id
        if (thread_id := msg.get("message_thread_id")) is not None:
            metadata["thread_id"] = str(thread_id)
        for key in ("entities", "caption_entities"):
            if key in msg:
                metadata[key] = msg[key]

        reply_to_message = msg.get("reply_to_message")
        if isinstance(reply_to_message, dict):
            reply_to_from = reply_to_message.get("from") or {}
            metadata["reply_to_message_id"] = str(reply_to_message.get("message_id", ""))
            metadata["reply_to_message_from_id"] = str(reply_to_from.get("id", ""))
            metadata["reply_to_message_from_username"] = str(reply_to_from.get("username", ""))

        content = msg.get("text") or msg.get("caption") or ""
        content_entity_key = "entities" if msg.get("text") else "caption_entities"
        content_entities = msg.get(content_entity_key)
        if isinstance(content_entities, list):
            metadata["content_entities"] = content_entities
        attachments = self._telegram_media_attachments(msg)
        if not content:
            for media_key in (
                "document",
                "photo",
                "video",
                "audio",
                "voice",
                "sticker",
                "video_note",
            ):
                if media_key in msg:
                    content = f"[{media_key}]"
                    break

        return IncomingMessage(
            sender_id=str(sender.get("id", "")),
            channel_id=str(chat.get("id", self.config.default_chat_id)),
            content=str(content),
            attachments=attachments,
            metadata=metadata,
        )

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        if not msg.metadata.get("is_group"):
            return True
        username = self.bot_username
        if not username:
            return False

        # If this message is a reply to the bot, count it as a mention.
        reply_to_from_id = msg.metadata.get("reply_to_message_from_id")
        reply_to_from_username = msg.metadata.get("reply_to_message_from_username")
        if (reply_to_from_id and str(reply_to_from_id) == str(self.bot_user_id or "")) or (
            reply_to_from_username
            and reply_to_from_username.casefold() == username.lstrip("@").casefold()
        ):
            return True

        mention = f"@{username}".lower()
        text = msg.content or ""
        entities = msg.metadata.get("content_entities")
        if not isinstance(entities, list):
            entities = msg.metadata.get("entities") or msg.metadata.get("caption_entities") or []
        has_mismatched_bot_command = False
        if isinstance(entities, list):
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                entity_type = entity.get("type")
                if entity_type == "mention":
                    offset = int(entity.get("offset", 0))
                    length = int(entity.get("length", 0))
                    if _slice_utf16(text, offset, length).lower() == mention:
                        return True
                if entity_type == "text_mention":
                    user = entity.get("user") or {}
                    if str(user.get("id", "")) == str(self.bot_user_id or ""):
                        return True
                if entity_type == "bot_command":
                    offset = int(entity.get("offset", 0))
                    length = int(entity.get("length", 0))
                    command = _slice_utf16(text, offset, length)
                    _, separator, target = command.partition("@")
                    if not separator:
                        return True
                    if target.casefold() == username.lstrip("@").casefold():
                        return True
                    has_mismatched_bot_command = True
        if has_mismatched_bot_command:
            return False
        return mention in text.lower()

    def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
        metadata: dict[str, Any] = {"chat_id": inbound.channel_id}
        if (thread_id := inbound.metadata.get("thread_id")) is not None:
            metadata["thread_id"] = thread_id
        return OutgoingMessage(content=content, reply_to=inbound.channel_id, metadata=metadata)

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, Any]:
        """Stream the reply into the chat (and forum topic) that triggered it.

        Without this hook dispatch calls ``send_streaming`` with no kwargs, so a
        bot with no ``default_chat_id`` — the normal deployment — would have
        nowhere to stream to.
        """
        kwargs: dict[str, Any] = {"chat_id": inbound.channel_id}
        if (thread_id := inbound.metadata.get("thread_id")) is not None:
            kwargs["thread_id"] = thread_id
        return kwargs

    async def probe_target(self, target: str) -> tuple[bool, str]:
        """Whether ``getChat`` can see *target*, and why not when it cannot.

        Callers that are about to *store* a chat id use this to fail early —
        cron does, at save time, because otherwise a mistyped recipient is only
        discovered by a scheduled run hours later.

        A transport failure is re-raised rather than answered ``False``: the bot
        being unable to reach Telegram is not evidence about the chat id, and
        the caller is the one that knows whether to treat "don't know" as fatal.
        """
        chat_id = (target or "").strip()
        if not chat_id:
            return True, ""
        try:
            await self._api("getChat", {"chat_id": chat_id})
        except TelegramApiError as exc:
            message = str(exc)
            if any(marker in message for marker in _TRANSPORT_FAILURE_MARKERS):
                raise
            # "Telegram getChat failed: Bad Request: chat not found" reads better
            # as just the part Telegram said.
            _, _, detail = message.partition(": ")
            return False, detail or message
        return True, ""

    async def send_typing(
        self,
        channel_id: str | None = None,
        *,
        thread_id: str | None = None,
    ) -> ChannelSendResult:
        """Show Telegram's native typing status in a chat or forum topic."""
        target = channel_id or self.config.default_chat_id
        if not target:
            return ChannelSendResult.unsupported(
                capability=ChannelCapabilities.TYPING_INDICATOR,
                reason="no chat target",
            )
        payload: dict[str, Any] = {"chat_id": str(target), "action": "typing"}
        if thread_id:
            payload["message_thread_id"] = _coerce_telegram_int(thread_id)
        await self._api("sendChatAction", payload)
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.TYPING_INDICATOR,
            target_id=str(target),
        )

    async def send(self, message: OutgoingMessage) -> dict[str, Any]:
        payload = self._build_send_payload(message)
        try:
            result = await self._api("sendMessage", payload)
        except TelegramApiError as exc:
            auto_rendered = "parse_mode" not in message.metadata
            if not auto_rendered or "parse entities" not in str(exc).lower():
                raise
            log.warning("telegram.markdown_fallback", error=str(exc))
            payload["text"] = message.content
            payload.pop("parse_mode", None)
            result = await self._api("sendMessage", payload)
        return result if isinstance(result, dict) else {"result": result}

    @staticmethod
    def _split_for_limit(segment: str) -> tuple[str, str]:
        """Split *segment* into the largest prefix that fits one message, plus the rest.

        The 4096 budget applies to the *rendered* HTML, which is longer than the
        markdown it came from, so the cut point is found by binary search over
        the raw text and then nudged back to the nearest line/word boundary.
        """
        if len(render_telegram_html(segment)) <= _MESSAGE_TEXT_LIMIT:
            return segment, ""
        low, high, best = 1, len(segment) - 1, 1
        while low <= high:
            mid = (low + high) // 2
            if len(render_telegram_html(segment[:mid])) <= _MESSAGE_TEXT_LIMIT:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        cut = best
        for boundary in ("\n", " "):
            found = segment.rfind(boundary, 0, best)
            if found >= best // 2:
                cut = found + 1
                break
        return segment[:cut], segment[cut:]

    async def _stream_send(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None,
    ) -> int | str:
        """Post one streaming message and return its Telegram ``message_id``."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": render_telegram_html(text),
            "parse_mode": "HTML",
        }
        if thread_id:
            payload["message_thread_id"] = _coerce_telegram_int(thread_id)
        try:
            result = await self._api("sendMessage", payload)
        except TelegramApiError as exc:
            if "parse entities" not in str(exc).lower():
                raise
            log.warning("telegram.markdown_fallback", error=str(exc))
            payload["text"] = text
            payload.pop("parse_mode", None)
            result = await self._api("sendMessage", payload)
        if not isinstance(result, dict) or result.get("message_id") is None:
            raise TelegramApiError("Telegram sendMessage returned no message_id")
        message_id = result["message_id"]
        return message_id if isinstance(message_id, int) else str(message_id)

    async def _stream_edit(self, chat_id: str, message_id: int | str, text: str) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": _coerce_telegram_int(message_id),
            "text": render_telegram_html(text),
            "parse_mode": "HTML",
        }
        try:
            await self._api("editMessageText", payload)
        except TelegramApiError as exc:
            if "parse entities" not in str(exc).lower():
                raise
            log.warning("telegram.markdown_fallback", error=str(exc))
            payload["text"] = text
            payload.pop("parse_mode", None)
            await self._api("editMessageText", payload)

    async def send_streaming(
        self,
        chunks: AsyncIterator[str],
        *,
        chat_id: str | None = None,
        thread_id: str | None = None,
        update_interval_ms: int = _STREAM_UPDATE_INTERVAL_MS,
    ) -> str | None:
        """Stream a reply by posting one message and editing it as text arrives.

        Returns the ``"<chat_id>|<message_id>"`` reference of the last message
        written — the form :meth:`edit` and :meth:`delete` accept — or ``None``
        when the stream produced no text.

        ``StreamThrottle`` keeps a fast producer from firing two concurrent
        ``editMessageText`` calls and preserves accumulated text when a flush
        fails. ``FloodStrikeBackoff`` watches for 429s: once Telegram has said
        "too many requests" three times in 30s the edit loop stops and the
        remaining text is delivered as one final message rather than fighting
        the rate limiter.
        """
        target = str(chat_id or self.config.default_chat_id or "").strip()
        if not target:
            # Raising (rather than returning a soft "unsupported") is what makes
            # dispatch replay the answer through ``channel.send``; a quiet return
            # would drop the user's reply entirely.
            raise RuntimeError("Telegram stream has no target chat")

        throttle = StreamThrottle(interval_s=update_interval_ms / 1000.0)
        backoff = FloodStrikeBackoff(
            cap=_STREAM_FLOOD_STRIKE_CAP,
            decay_s=_STREAM_FLOOD_DECAY_S,
            adapter="telegram",
        )
        message_id: int | str | None = None
        segment_start = 0
        delivered = 0

        async def _post_segments(remaining: str) -> None:
            """Post *remaining* as one or more new messages, splitting at the cap.

            ``delivered`` advances after each successful send, so a failure part
            way through never causes already-visible text to be resent.
            """
            nonlocal message_id, segment_start, delivered
            while True:
                head, tail = self._split_for_limit(remaining)
                message_id = await self._stream_send(target, head, thread_id)
                delivered = segment_start + len(head)
                if not tail:
                    return
                segment_start = delivered
                remaining = tail

        async def _post(text: str) -> int | str | None:
            await _post_segments(text[segment_start:])
            log.debug("telegram.stream_start", chat_id=target, message_id=message_id)
            return message_id

        async def _edit(text: str) -> int | str | None:
            nonlocal delivered, segment_start
            current = message_id
            if current is None:  # pragma: no cover - throttle opens before it edits
                raise TelegramApiError("Telegram stream edit before the message was opened")
            head, tail = self._split_for_limit(text[segment_start:])
            await self._stream_edit(target, current, head)
            delivered = segment_start + len(head)
            if tail:
                # This message is full: freeze it and roll over into a new one.
                segment_start = delivered
                await _post_segments(tail)
            return message_id

        async for chunk in chunks:
            throttle.add(chunk)
            if backoff.should_fallback():
                continue
            try:
                if await throttle.maybe_flush(post=_post, edit=_edit) is not None:
                    backoff.record_success()
            except TelegramFloodError as exc:
                backoff.record_429()
                log.warning(
                    "telegram.stream_rate_limited",
                    chat_id=target,
                    retry_after=exc.retry_after,
                )

        text = throttle.text
        if not text:
            return None

        # ``delivered < len(text)`` skips a final flush that would repeat the
        # last one verbatim — Telegram rejects an edit that changes nothing.
        if delivered < len(text) and not backoff.should_fallback():
            try:
                await throttle.force_flush(post=_post, edit=_edit)
            except TelegramFloodError as exc:
                backoff.record_429()
                log.warning(
                    "telegram.stream_rate_limited",
                    chat_id=target,
                    retry_after=exc.retry_after,
                )

        if delivered < len(text):
            # Either the circuit opened or the last flush was rate-limited.
            # Everything past the watermark still has to reach the user, as a
            # plain message rather than another edit.
            segment_start = delivered
            await self._deliver_stream_remainder(_post_segments, text[delivered:])

        log.debug("telegram.stream_end", chat_id=target, length=len(text))
        return f"{target}|{message_id}" if message_id is not None else None

    @staticmethod
    async def _deliver_stream_remainder(
        post_segments: Any,
        remainder: str,
    ) -> None:
        """Final-only delivery of text the edit loop could not place.

        One bounded retry: a flood that just tripped the circuit usually clears
        within ``retry_after``, and there is no consumer left to fall back to.
        """
        try:
            await post_segments(remainder)
        except TelegramFloodError as exc:
            await asyncio.sleep(min(exc.retry_after, 5.0))
            await post_segments(remainder)

    async def send_file(
        self,
        chat_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        if not self.config.token:
            raise ValueError("telegram.send_file requires token")
        path = Path(file_path)
        check_channel_file_size(path, self.MAX_FILE_BYTES, "Telegram")
        payload = {"chat_id": str(chat_id)}
        if content:
            payload["caption"] = render_telegram_html(content)
            payload["parse_mode"] = "HTML"
        client = self._get_client()
        try:
            with path.open("rb") as f:
                response = await client.post(
                    f"/bot{self.config.token}/sendDocument",
                    data=payload,
                    files={"document": (path.name, f)},
                )
        except httpx.RequestError:
            raise TelegramApiError("Telegram sendDocument request failed") from None
        raw_result = self._parse_api_response(response, "sendDocument")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        raw_document = result.get("document")
        document: dict[str, Any] = raw_document if isinstance(raw_document, dict) else {}
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=str(chat_id),
            provider_message_id=str(result.get("message_id", "")),
            provider_file_id=str(document.get("file_id", "")),
        )

    def _build_send_payload(self, message: OutgoingMessage) -> dict[str, Any]:
        metadata = message.metadata
        route_chat_id = metadata.get("channel")
        chat_id = (
            metadata.get("chat_id")
            or metadata.get("channel_id")
            or route_chat_id
            or message.reply_to
            or self.config.default_chat_id
        )
        if not chat_id:
            raise ValueError("telegram.send requires chat_id via metadata, reply_to, or config")
        payload: dict[str, Any] = {"chat_id": str(chat_id), "text": message.content}
        thread_id = metadata.get("thread_id") or metadata.get("message_thread_id")
        if thread_id is None and route_chat_id and message.reply_to:
            thread_id = message.reply_to
        if thread_id:
            payload["message_thread_id"] = _coerce_telegram_int(thread_id)
        if (reply_message_id := metadata.get("reply_to_message_id")) is not None:
            payload["reply_parameters"] = {
                "message_id": _coerce_telegram_int(reply_message_id),
            }
        if "parse_mode" in metadata:
            if parse_mode := metadata.get("parse_mode"):
                payload["parse_mode"] = str(parse_mode)
        else:
            payload["text"] = render_telegram_html(message.content)
            payload["parse_mode"] = "HTML"
        if "reply_markup" in metadata:
            payload["reply_markup"] = metadata["reply_markup"]
        return payload

    async def edit(self, message_id: str, content: str) -> None:
        chat_id, raw_message_id = self._split_message_ref(message_id)
        await self._api(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": _coerce_telegram_int(raw_message_id),
                "text": render_telegram_html(content),
                "parse_mode": "HTML",
            },
        )

    async def delete(self, message_id: str) -> None:
        chat_id, raw_message_id = self._split_message_ref(message_id)
        await self._api(
            "deleteMessage",
            {
                "chat_id": chat_id,
                "message_id": _coerce_telegram_int(raw_message_id),
            },
        )

    def _split_message_ref(self, message_id: str) -> tuple[str, str]:
        chat_id, sep, raw_message_id = message_id.partition("|")
        if sep:
            return chat_id, raw_message_id
        if not self.config.default_chat_id:
            raise ValueError("telegram edit/delete requires '<chat_id>|<message_id>'")
        return self.config.default_chat_id, message_id


__all__ = [
    "CAPABILITY_TIER",
    "DM_SAFETY_TIERS",
    "FATAL_ERROR_CLASSES",
    "RETRYABLE_ERROR_CLASSES",
    "TelegramApiError",
    "TelegramChannel",
    "TelegramChannelConfig",
    "TelegramFloodError",
]
