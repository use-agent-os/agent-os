"""DiscordChannel: adapter for Discord Bot Gateway (WebSocket) and REST API."""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import httpx
import structlog
import websockets
import websockets.asyncio.client
from pydantic import BaseModel

from agentos.channels._attachment_io import (
    attachment_limit_for_mime,
    ensure_declared_size_within_limit,
    fetch_httpx_bytes_limited,
    preferred_attachment_mime,
)
from agentos.channels._util import (
    ChannelAccessPolicy,
    EventDedupeCache,
    RateLimiter,
    StreamThrottle,
    check_channel_file_size,
    retry_request,
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
from agentos.channels.types import (
    Attachment,
    ChannelHealth,
    IncomingMessage,
    OutgoingMessage,
)
from agentos.engine.native_commands import discord_application_commands
from agentos.env import trust_env as _trust_env

log = structlog.get_logger(__name__)

_DISCORD_MENTION_RE = re.compile(r"<@!?(\d+)>")
_DISCORD_DM_CHANNEL_TYPES = {1}
_DISCORD_GROUP_DM_CHANNEL_TYPES = {3}
_DISCORD_THREAD_CHANNEL_TYPES = {10, 11, 12}
_DISCORD_APPLICATION_COMMAND_INTERACTION_TYPE = 2
_DISCORD_DEFERRED_CHANNEL_MESSAGE_RESPONSE_TYPE = 5

# Gateway intents bitmask
GATEWAY_INTENTS = (
    (1 << 0)  # GUILDS
    | (1 << 9)  # GUILD_MESSAGES
    | (1 << 12)  # DIRECT_MESSAGES
    | (1 << 15)  # MESSAGE_CONTENT (privileged)
    | (1 << 10)  # GUILD_MESSAGE_REACTIONS
)

# Channel-contract constants pinned by the adapter audit.
CAPABILITY_TIER = "GREEN-shipping"

# Discord is a DM/group channel; runtime capability and sandbox policy still apply.
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


class DiscordChannelConfig(BaseModel):
    """Pydantic config for Discord channel adapter."""

    token: str
    application_id: str = ""
    default_channel_id: str = ""
    api_base: str = "https://discord.com/api/v10"
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = GATEWAY_INTENTS
    reconnect_max_retries: int = 5
    reconnect_base_delay_s: float = 1.0

    model_config = {}  # explicit params only; no env loading


@dataclass
class _GatewayState:
    session_id: str | None = None
    sequence: int | None = None
    resume_url: str | None = None
    heartbeat_interval_ms: int = 41250
    last_heartbeat_ack: bool = True


@dataclass
class DiscordChannel:
    """Channel adapter for Discord via Gateway WebSocket and REST API.

    Uses the ``websockets`` library for the gateway connection
    and ``httpx.AsyncClient`` for REST calls.
    """

    config: DiscordChannelConfig
    bot_user_id: str | None = None
    supports_slash_commands: bool = True
    # Discord mirrors slack: DMs admit, groups require mention.
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
    _connected: bool = field(default=False, init=False, repr=False)
    _last_message_at: datetime | None = field(default=None, init=False, repr=False)
    _ws: Any = field(default=None, init=False, repr=False)
    _state: _GatewayState = field(default_factory=_GatewayState, init=False, repr=False)
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _dispatch_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _reconnecting: bool = field(default=False, init=False, repr=False)
    # Bumped at the end of each successful _do_reconnect. Concurrent callers
    # snapshot this before taking the lock so a second waiter does not IDENTIFY
    # again on a socket the first waiter already replaced.
    _reconnect_generation: int = field(default=0, init=False, repr=False)
    # Consecutive failed reconnect attempts. Reset on a successful reconnect;
    # when it reaches config.reconnect_max_retries the channel goes dead
    # (_connected=False) instead of spinning forever or dying with an
    # unobserved task exception.
    _reconnect_failures: int = field(default=0, init=False, repr=False)
    _dedupe: EventDedupeCache = field(
        default_factory=lambda: EventDedupeCache(max_size=10_000),
        init=False,
        repr=False,
    )
    _rate_limiter: RateLimiter = field(default_factory=RateLimiter, init=False, repr=False)
    _sent_messages: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _channel_types: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _thread_parent_channels: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    @property
    def capability_profile(self) -> ChannelCapabilityProfile:
        return ChannelCapabilityProfile(
            channel_type="discord",
            group_chat=True,
            mentions=True,
            typing_indicator=True,
            streaming=True,
            native_file_upload=True,
            media=True,
            reactions=True,
            inbound_reactions=True,
            threads=True,
            group_dm=True,
            edit=True,
            delete=True,
            transports=("websocket",),
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
                tools=("multipart/form-data message attachments",),
                mutates=True,
                notes=("Discord file delivery attaches files to create-message requests.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.ATTACHMENTS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                tools=("attachment.url",),
                notes=("Inbound Discord attachments are downloaded from message attachment URLs.",),
            ),
            ChannelPlatformCapability(
                category=ChannelPlatformCategories.THREADS,
                status=ChannelPlatformCapabilityStatus.SUPPORTED,
                notes=("Discord thread channels are detected from channel type metadata.",),
            ),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.capability_profile.capability_tags()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_base,
                timeout=30.0,
                trust_env=_trust_env(),
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.config.token}"}

    # ------------------------------------------------------------------
    # Gateway WebSocket helpers
    # ------------------------------------------------------------------

    async def _connect_ws(self, url: str) -> Any:
        ws = await websockets.asyncio.client.connect(url)
        return ws

    async def _ws_send(self, payload: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))

    async def _ws_recv(self) -> dict[str, Any]:
        raw = await self._ws.recv()
        return cast(dict[str, Any], json.loads(raw))

    async def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _fetch_gateway_url(self) -> str:
        client = self._get_client()
        resp = await client.get("/gateway/bot", headers=self._auth_headers())
        resp.raise_for_status()
        return cast(str, resp.json()["url"]) + "?v=10&encoding=json"

    async def _identify(self) -> None:
        await self._ws_send(
            {
                "op": 2,
                "d": {
                    "token": self.config.token,
                    "intents": self.config.intents,
                    "properties": {
                        "os": "linux",
                        "browser": "agentos",
                        "device": "agentos",
                    },
                },
            }
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while self._connected:
            if not self._state.last_heartbeat_ack:
                log.warning("discord.heartbeat_timeout")
                try:
                    await self._reconnect()
                except Exception as exc:  # noqa: BLE001
                    # Reconnect budget exhausted; _connected is already
                    # False. The while-guard exits on the next iteration
                    # without sending another heartbeat into a dead
                    # socket.
                    log.warning(
                        "discord.heartbeat_reconnect_contained",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                return
            self._state.last_heartbeat_ack = False
            try:
                await self._ws_send({"op": 1, "d": self._state.sequence})
            except Exception as exc:  # noqa: BLE001
                # A send failure into a dead socket means the connection is
                # gone; treat it like a missed ACK so the reconnect path
                # runs instead of this loop dying with an unobserved
                # exception.
                log.warning(
                    "discord.heartbeat_send_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                self._state.last_heartbeat_ack = False
                try:
                    await self._reconnect()
                except Exception:  # noqa: BLE001
                    pass  # reconnect already logs; exit on the while-guard
                return
            await asyncio.sleep(self._state.heartbeat_interval_ms / 1000.0)

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _reconnect(self) -> None:
        """Re-establish the gateway connection.

        Concurrent callers wait for the in-flight reconnect rather than
        no-op returning: the dispatch loop keeps receiving after a
        reconnect and must not race past a still-closed socket. A second
        waiter that arrived after the socket was already replaced skips
        a duplicate IDENTIFY.

        Exceptions from ``_do_reconnect`` are contained so a single bad
        resume/fetch/connect cannot kill the dispatch loop with an
        unobserved task exception. Bounded backoff between attempts;
        after ``config.reconnect_max_retries`` consecutive failures the
        channel goes ``dead`` (``_connected=False``) so the
        ``ChannelManager`` retry budget can take over and operators see a
        stuck channel.
        """
        generation = self._reconnect_generation
        async with self._reconnect_lock:
            if not self._connected:
                return
            if generation != self._reconnect_generation:
                log.info("discord.reconnect_skipped_already_in_flight")
                return
            self._reconnecting = True
            try:
                try:
                    await self._do_reconnect()
                except Exception as exc:  # noqa: BLE001
                    # Bounded backoff between attempts; cap on the failure
                    # count transitions the channel to "dead" so the
                    # dispatch loop can exit cleanly and the operator sees
                    # a stuck channel rather than a silent kill.
                    self._reconnect_failures += 1
                    backoff = self.config.reconnect_base_delay_s * (
                        2 ** max(0, self._reconnect_failures - 1)
                    )
                    log.warning(
                        "discord.reconnect_attempt_connect_failed",
                        attempt=self._reconnect_failures,
                        max_retries=self.config.reconnect_max_retries,
                        backoff_s=round(backoff, 3),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    if self._reconnect_failures >= self.config.reconnect_max_retries:
                        log.error(
                            "discord.reconnect_exhausted",
                            attempts=self._reconnect_failures,
                            max_retries=self.config.reconnect_max_retries,
                        )
                        # Flip _connected so the dispatch loop exits on its
                        # next check. Re-raise to caller so op-7/op-9 paths
                        # that wrap the call can decide whether to keep
                        # trying; the dispatch loop catches here, so this
                        # primarily affects heartbeat-initiated reconnects.
                        self._connected = False
                        raise
                    # Sleep INSIDE the lock so concurrent reconnect callers
                    # serialize their backoffs and don't compound the storm.
                    await asyncio.sleep(backoff)
                    return
                self._reconnect_generation += 1
                # A clean reconnect resets the failure budget so a brief
                # blip doesn't permanently lower the bar for the next storm.
                if self._reconnect_failures:
                    log.info(
                        "discord.reconnect_recovered",
                        after_failures=self._reconnect_failures,
                    )
                    self._reconnect_failures = 0
            finally:
                self._reconnecting = False

    async def _do_reconnect(self) -> None:
        log.info("discord.reconnecting", session_id=self._state.session_id)
        current = asyncio.current_task()
        if self._heartbeat_task is not None and self._heartbeat_task is not current:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        await self._close_ws()

        url = self._state.resume_url or await self._fetch_gateway_url()
        self._ws = await self._connect_ws(url)
        hello = await self._ws_recv()
        self._state.heartbeat_interval_ms = hello["d"]["heartbeat_interval"]

        if self._state.session_id and self._state.sequence is not None:
            await self._ws_send(
                {
                    "op": 6,
                    "d": {
                        "token": self.config.token,
                        "session_id": self._state.session_id,
                        "seq": self._state.sequence,
                    },
                }
            )
        else:
            await self._identify()

        self._state.last_heartbeat_ack = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while self._connected:
            try:
                raw = await self._ws_recv()
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
            ):
                if self._connected:
                    try:
                        await self._reconnect()
                    except Exception as exc:  # noqa: BLE001
                        # _reconnect re-raises when the retry budget is
                        # exhausted (and flips _connected=False). Anything
                        # else is contained: the dispatch loop must not
                        # die silently.
                        log.warning(
                            "discord.connection_closed_reconnect_contained",
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                    if not self._connected:
                        return
                    continue
                return

            op = raw.get("op")
            if op == 0:  # Dispatch
                self._state.sequence = raw.get("s")
                event_type = raw.get("t")
                data = raw.get("d", {})
                await self._handle_dispatch(event_type, data)
            elif op == 1:  # Heartbeat request
                await self._ws_send({"op": 1, "d": self._state.sequence})
            elif op == 7:  # Reconnect
                try:
                    await self._reconnect()
                except Exception as exc:  # noqa: BLE001
                    # _reconnect re-raises when it has exhausted the retry
                    # budget (and flipped _connected=False). Anything else
                    # is contained: the dispatch loop must not die silently.
                    log.warning(
                        "discord.op7_reconnect_contained",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                if not self._connected:
                    return
                continue
            elif op == 9:  # Invalid Session
                resumable = raw.get("d", False)
                if not resumable:
                    self._state.session_id = None
                    self._state.sequence = None
                await asyncio.sleep(1 + random.random() * 4)
                try:
                    await self._reconnect()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "discord.op9_reconnect_contained",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                if not self._connected:
                    return
                continue
            elif op == 11:  # Heartbeat ACK
                self._state.last_heartbeat_ack = True

    async def _handle_dispatch(self, event_type: str | None, data: dict[str, Any]) -> None:
        if event_type == "READY":
            self._state.session_id = data["session_id"]
            self._state.resume_url = data.get("resume_gateway_url")
            self.bot_user_id = data["user"]["id"]
            log.info(
                "discord.ready",
                user=data["user"]["username"],
                guilds=len(data.get("guilds", [])),
            )
        elif event_type == "MESSAGE_CREATE":
            if data.get("author", {}).get("id") == self.bot_user_id:
                return
            msg_id = str(data.get("id") or "")
            if msg_id and not self._dedupe.check_and_add(f"message:{msg_id}"):
                return
            msg = self.parse_event(self._annotate_channel_context(data))
            self.enqueue(msg)
        elif event_type == "MESSAGE_REACTION_ADD":
            reaction_key = self._reaction_dedupe_key(data)
            if reaction_key and not self._dedupe.check_and_add(reaction_key):
                return
            self._enqueue_reaction(self._annotate_channel_context(data))
        elif event_type == "INTERACTION_CREATE":
            interaction_type = data.get("type")
            if interaction_type == 3:  # Message Component (button click)
                await self._handle_discord_component_interaction(data)
                return
            if interaction_type != _DISCORD_APPLICATION_COMMAND_INTERACTION_TYPE:
                return
            interaction_id = str(data.get("id") or "")
            if interaction_id and not self._dedupe.check_and_add(f"interaction:{interaction_id}"):
                return
            await self._handle_interaction(self._annotate_channel_context(data))
        elif event_type in {"CHANNEL_CREATE", "CHANNEL_UPDATE", "THREAD_CREATE", "THREAD_UPDATE"}:
            self._cache_channel_context(data)
        elif event_type == "GUILD_CREATE":
            for channel in data.get("channels", []):
                if isinstance(channel, dict):
                    self._cache_channel_context(channel)
            for thread in data.get("threads", []):
                if isinstance(thread, dict):
                    self._cache_channel_context(thread)
        elif event_type == "THREAD_LIST_SYNC":
            for thread in data.get("threads", []):
                if isinstance(thread, dict):
                    self._cache_channel_context(thread)
        elif event_type == "RESUMED":
            log.info("discord.resumed")

    def _cache_channel_context(self, data: dict[str, Any]) -> None:
        channel_id = data.get("id")
        channel_type = self._channel_type(data.get("type"))
        if isinstance(channel_id, str) and channel_id and channel_type is not None:
            self._channel_types[channel_id] = channel_type
        parent_id = data.get("parent_id")
        if isinstance(channel_id, str) and channel_id and isinstance(parent_id, str) and parent_id:
            self._thread_parent_channels[channel_id] = parent_id

    def _annotate_channel_context(self, data: dict[str, Any]) -> dict[str, Any]:
        channel_id = data.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id:
            return data
        enriched = dict(data)
        if "channel_type" not in enriched and channel_id in self._channel_types:
            enriched["channel_type"] = self._channel_types[channel_id]
        if (
            "thread_parent_channel_id" not in enriched
            and channel_id in self._thread_parent_channels
        ):
            enriched["thread_parent_channel_id"] = self._thread_parent_channels[channel_id]
        return enriched

    @staticmethod
    def _reaction_dedupe_key(data: dict[str, Any]) -> str:
        emoji = data.get("emoji", {})
        emoji_key = emoji.get("id") or emoji.get("name") or ""
        if not data.get("message_id") or not data.get("user_id") or not emoji_key:
            return ""
        return f"reaction:{data.get('message_id', '')}:{data.get('user_id', '')}:{emoji_key}"

    def _enqueue_reaction(self, data: dict[str, Any]) -> None:
        user_id = data.get("user_id", "unknown")
        channel_id = data.get("channel_id", "unknown")
        emoji = data.get("emoji", {})
        channel_type = self._channel_type(data.get("channel_type"))
        thread_id = self._native_thread_id(data, channel_type)
        conversation_kind = self._conversation_kind(data, channel_type, thread_id)
        parent_channel_id = data.get("thread_parent_channel_id")
        msg = IncomingMessage(
            sender_id=user_id,
            channel_id=channel_id,
            content="",
            metadata={
                "event_type": "MESSAGE_REACTION_ADD",
                "message_id": data.get("message_id"),
                "emoji_name": emoji.get("name", ""),
                "emoji_id": emoji.get("id"),
                "guild_id": data.get("guild_id"),
                "channel_type": channel_type,
                "is_group": conversation_kind in {"group", "group_dm", "thread", "topic"},
                "conversation_kind": conversation_kind,
                "native_message_id": data.get("message_id"),
                "native_chat_id": channel_id,
                "native_thread_id": thread_id,
                "native_parent_channel_id": parent_channel_id,
                "reply_target_id": data.get("message_id"),
            },
        )
        self.enqueue(msg)

    async def _defer_interaction_response(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        try:
            response = await self._get_client().post(
                f"/interactions/{interaction_id}/{interaction_token}/callback",
                json={"type": _DISCORD_DEFERRED_CHANNEL_MESSAGE_RESPONSE_TYPE},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            status_code = (
                exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            )
            log.warning(
                "discord.interaction_ack_failed",
                interaction_id=interaction_id,
                error_type=type(exc).__name__,
                status_code=status_code,
            )
            return False
        log.debug("discord.interaction_deferred", interaction_id=interaction_id)
        return True

    async def _handle_discord_component_interaction(self, data: dict[str, Any]) -> None:
        interaction_id = str(data.get("id") or "")
        if interaction_id and not self._dedupe.check_and_add(f"interaction:{interaction_id}"):
            log.debug(
                "discord.component_interaction_duplicate_dropped",
                interaction_id=interaction_id,
            )
            return

        interaction_data = data.get("data", {})
        custom_id = interaction_data.get("custom_id", "")
        if not custom_id.startswith("approve:") and not custom_id.startswith("deny:"):
            return

        act, approval_id = custom_id.split(":", 1)
        approved = act == "approve"

        member = data.get("member")
        member_user = member.get("user") if isinstance(member, dict) else None
        direct_user = data.get("user")
        user = member_user if isinstance(member_user, dict) else direct_user
        user_id = str(user.get("id") if isinstance(user, dict) else "unknown")
        channel_id = str(data.get("channel_id") or "unknown")
        guild_id = data.get("guild_id")
        is_group = guild_id is not None

        # 1. Admission Check
        from agentos.channels._util import evaluate_policy

        decision = evaluate_policy(
            self.policy,
            is_group=is_group,
            mentioned=True,
            sender_id=user_id,
        )
        interaction_token = data.get("token")
        if not decision.admit:
            client = self._get_client()
            try:
                resp = await client.post(
                    f"/interactions/{interaction_id}/{interaction_token}/callback",
                    json={
                        "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
                        "data": {
                            "content": "Unauthorized: Only paired users can approve/deny tools.",
                            "flags": 64,  # EPHEMERAL
                        },
                    },
                )
                resp.raise_for_status()
            except Exception as exc:
                log.warning("discord.component_unauthorized_response_failed", error=str(exc))
            return

        from agentos.gateway.approval_queue import get_approval_queue

        queue = get_approval_queue()

        # 2. Retrieve PendingApproval and verify sessionKey match
        try:
            entry = queue.get(approval_id)
        except KeyError:
            client = self._get_client()
            try:
                resp = await client.post(
                    f"/interactions/{interaction_id}/{interaction_token}/callback",
                    json={
                        "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
                        "data": {
                            "content": "Error: Approval request not found or expired.",
                            "flags": 64,  # EPHEMERAL
                        },
                    },
                )
                resp.raise_for_status()
            except Exception as exc:
                log.warning("discord.component_keyerror_response_failed", error=str(exc))
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
                expected_peer = channel_id if session_mode in ("group", "channel") else user_id
                if session_channel != "discord" or session_peer != expected_peer:
                    log.warning(
                        "discord.component_mismatch",
                        session_key=session_key,
                        expected_peer=expected_peer,
                        session_peer=session_peer,
                    )
                    return

        # 3. Resolve Approval Queue
        try:
            queue.resolve(approval_id, approved)
        except ValueError:
            client = self._get_client()
            try:
                resp = await client.post(
                    f"/interactions/{interaction_id}/{interaction_token}/callback",
                    json={
                        "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
                        "data": {
                            "content": "Error: Approval request was already resolved.",
                            "flags": 64,  # EPHEMERAL
                        },
                    },
                )
                resp.raise_for_status()
            except Exception as exc:
                log.warning("discord.component_valueerror_response_failed", error=str(exc))
            return

        # 4. Respond with UPDATE_MESSAGE
        orig_message = data.get("message", {})
        orig_content = orig_message.get("content", "")
        decision_text = "Approved ✅" if approved else "Denied ❌"
        new_content = f"{orig_content}\n\n**{decision_text}**"

        client = self._get_client()
        try:
            resp = await client.post(
                f"/interactions/{interaction_id}/{interaction_token}/callback",
                json={
                    "type": 7,  # UPDATE_MESSAGE
                    "data": {
                        "content": new_content,
                        "components": [],
                    },
                },
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning("discord.component_response_failed", error=str(exc))

        # 5. Enqueue the virtual message
        msg = IncomingMessage(
            sender_id=user_id,
            channel_id=channel_id,
            content="Approve" if approved else "Deny",
            metadata={
                "interaction_type": "component_click",
                "guild_id": data.get("guild_id"),
                "is_group": is_group,
            },
        )
        self.enqueue(msg)

    async def _handle_interaction(self, data: dict[str, Any]) -> None:
        """Parse a slash command interaction into IncomingMessage."""
        interaction_data = data.get("data")
        interaction_id = str(data.get("id") or "")
        interaction_token = str(data.get("token") or "")
        if not isinstance(interaction_data, dict) or not interaction_id or not interaction_token:
            return

        command_name = interaction_data.get("name")
        if not isinstance(command_name, str) or not command_name:
            return
        if not await self._defer_interaction_response(interaction_id, interaction_token):
            return

        member = data.get("member")
        member_user = member.get("user") if isinstance(member, dict) else None
        direct_user = data.get("user")
        user = member_user if isinstance(member_user, dict) else direct_user
        if not isinstance(user, dict):
            user = {}
        channel_id = str(data.get("channel_id") or "unknown")

        # Build content from command name and options
        raw_options = interaction_data.get("options")
        options = raw_options if isinstance(raw_options, list) else []
        option_parts = [
            opt.get("value", "") for opt in options if isinstance(opt, dict) and opt.get("value")
        ]
        content = f"/{command_name} {' '.join(str(v) for v in option_parts)}".strip()
        channel_type = self._channel_type(data.get("channel_type"))
        thread_id = self._native_thread_id(data, channel_type)
        conversation_kind = self._conversation_kind(data, channel_type, thread_id)
        parent_channel_id = data.get("thread_parent_channel_id")
        application_id = str(data.get("application_id") or self.config.application_id)

        msg = IncomingMessage(
            sender_id=str(user.get("id") or "unknown"),
            channel_id=channel_id,
            content=content,
            metadata={
                "interaction_type": "slash_command",
                "command_name": command_name,
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "interaction_application_id": application_id,
                "guild_id": data.get("guild_id"),
                "channel_type": channel_type,
                "is_group": conversation_kind in {"group", "group_dm", "thread", "topic"},
                "conversation_kind": conversation_kind,
                "native_message_id": data.get("id"),
                "native_chat_id": channel_id,
                "native_thread_id": thread_id,
                "native_parent_channel_id": parent_channel_id,
                "reply_target_id": data.get("id"),
            },
        )
        self.enqueue(msg)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to the Discord gateway and begin dispatch/heartbeat loops."""
        url = self.config.gateway_url
        self._ws = await self._connect_ws(url)

        # Receive Hello
        hello = await self._ws_recv()
        self._state.heartbeat_interval_ms = hello["d"]["heartbeat_interval"]

        # Identify
        await self._identify()

        # Receive Ready
        ready = await self._ws_recv()
        if ready.get("t") == "READY":
            await self._handle_dispatch("READY", ready.get("d", {}))

        try:
            await self.register_native_slash_commands()
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning(
                "discord.commands_not_registered",
                reason="sync_failed",
                error=str(exc),
            )

        self._connected = True
        self._state.last_heartbeat_ack = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        log.info("discord.started", bot_user_id=self.bot_user_id)

    async def stop(self) -> None:
        """Disconnect from gateway and clean up."""
        self._connected = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            self._dispatch_task = None
        await self._close_ws()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        log.info("discord.stopped")

    def is_connected(self) -> bool:
        return self._connected

    async def health_check(self) -> ChannelHealth:
        return ChannelHealth(
            connected=self._connected,
            bot_user_id=self.bot_user_id,
            last_message_at=self._last_message_at,
            extra={
                "session_id": self._state.session_id,
                "sequence": self._state.sequence,
            },
        )

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def enqueue(self, message: IncomingMessage) -> None:
        self._queue.put_nowait(message)
        self._last_message_at = datetime.now(UTC)

    async def receive(self) -> IncomingMessage:
        msg = await self._queue.get()
        log.debug("discord.receive", content=msg.content[:80])
        return msg

    async def resolve_inbound_attachment(self, attachment: Attachment) -> Attachment:
        """Fetch Discord attachment bytes; shared ingest owns validation."""

        if attachment.data is not None or not attachment.url:
            return attachment
        limit = attachment_limit_for_mime(attachment.mime_type)
        ensure_declared_size_within_limit(attachment.size, name=attachment.name, limit=limit)
        payload, content_type = await fetch_httpx_bytes_limited(
            self._get_client(),
            attachment.url,
            name=attachment.name,
            limit=limit,
        )
        return Attachment(
            name=attachment.name,
            mime_type=preferred_attachment_mime(content_type, attachment.mime_type),
            data=payload,
            size=len(payload),
            metadata={**attachment.metadata, "source_url": attachment.url},
        )

    def parse_event(self, data: dict[str, Any]) -> IncomingMessage:
        author = data.get("author", {})
        content = data.get("content", "")
        channel_id = data.get("channel_id", "unknown")
        message_id = data.get("id")
        channel_type = self._channel_type(data.get("channel_type"))
        thread_id = self._native_thread_id(data, channel_type)
        referenced_message_id = data.get("message_reference", {}).get("message_id")
        parent_channel_id = data.get("thread_parent_channel_id")
        if not isinstance(parent_channel_id, str):
            parent_channel_id = data.get("message_reference", {}).get("channel_id")
        conversation_kind = self._conversation_kind(data, channel_type, thread_id)
        is_group = conversation_kind in {"group", "group_dm", "thread", "topic"}

        attachments: list[Attachment] = []
        for att in data.get("attachments", []):
            attachments.append(
                Attachment(
                    name=att.get("filename", "unknown"),
                    mime_type=att.get("content_type"),
                    url=att.get("url"),
                    size=att.get("size"),
                )
            )

        metadata: dict[str, Any] = {
            "message_id": message_id,
            "channel_id": channel_id,
            "guild_id": data.get("guild_id"),
            "channel_type": channel_type,
            "is_group": is_group,
            "conversation_kind": conversation_kind,
            "thread_id": thread_id,
            "referenced_message_id": referenced_message_id,
            "native_message_id": message_id,
            "native_chat_id": channel_id,
            "native_thread_id": thread_id,
            "native_parent_id": referenced_message_id,
            "native_parent_channel_id": parent_channel_id,
            "native_root_id": referenced_message_id,
            "reply_target_id": message_id,
        }

        return IncomingMessage(
            sender_id=author.get("id", "unknown"),
            channel_id=channel_id,
            content=content,
            attachments=attachments,
            metadata=metadata,
        )

    @staticmethod
    def _channel_type(raw: Any) -> int | None:
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdecimal():
            return int(raw)
        return None

    @staticmethod
    def _native_thread_id(data: dict[str, Any], channel_type: int | None) -> str | None:
        explicit = data.get("thread_id")
        if isinstance(explicit, str) and explicit:
            return explicit
        thread = data.get("thread")
        if isinstance(thread, dict):
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
        if channel_type in _DISCORD_THREAD_CHANNEL_TYPES:
            channel_id = data.get("channel_id")
            if isinstance(channel_id, str) and channel_id:
                return channel_id
        return None

    @staticmethod
    def _conversation_kind(
        data: dict[str, Any],
        channel_type: int | None,
        thread_id: str | None,
    ) -> str:
        if thread_id or channel_type in _DISCORD_THREAD_CHANNEL_TYPES:
            return "thread"
        if channel_type in _DISCORD_GROUP_DM_CHANNEL_TYPES:
            return "group_dm"
        if channel_type in _DISCORD_DM_CHANNEL_TYPES:
            return "dm"
        if data.get("guild_id") is not None:
            return "group"
        return "dm"

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def build_reply_message(self, content: str, inbound: IncomingMessage) -> OutgoingMessage:
        """Route replies to the triggering interaction or channel."""
        metadata: dict[str, Any] = {}
        if inbound.metadata.get("interaction_type") == "slash_command":
            for key in (
                "interaction_id",
                "interaction_token",
                "interaction_application_id",
            ):
                value = inbound.metadata.get(key)
                if isinstance(value, str) and value:
                    metadata[key] = value
        return OutgoingMessage(
            content=content,
            reply_to=inbound.channel_id,
            metadata=metadata,
        )

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> dict[str, Any]:
        """Route streaming replies to the triggering interaction or channel."""
        kwargs: dict[str, Any] = {"channel_id": inbound.channel_id}
        if inbound.metadata.get("interaction_type") == "slash_command":
            for key in (
                "interaction_id",
                "interaction_token",
                "interaction_application_id",
            ):
                value = inbound.metadata.get(key)
                if isinstance(value, str) and value:
                    kwargs[key] = value
        return kwargs

    async def send(self, message: OutgoingMessage) -> ChannelSendResult:
        await self._rate_limiter.acquire()
        client = self._get_client()
        channel_id = message.reply_to or self.config.default_channel_id

        payload: dict[str, Any] = {"content": message.content}

        if message.metadata.get("embeds"):
            payload["embeds"] = message.metadata["embeds"]

        if message.metadata.get("components"):
            payload["components"] = message.metadata["components"]

        if message.metadata.get("reply_to_message_id"):
            payload["message_reference"] = {
                "message_id": message.metadata["reply_to_message_id"],
            }

        interaction_token = message.metadata.get("interaction_token")
        if isinstance(interaction_token, str) and interaction_token:
            application_id = message.metadata.get("interaction_application_id")
            if not isinstance(application_id, str) or not application_id:
                application_id = self.config.application_id
            interaction_id = str(message.metadata.get("interaction_id") or "")
            if not application_id:
                return ChannelSendResult.failed(
                    capability=ChannelCapabilities.GROUP_CHAT,
                    target_id=interaction_id or channel_id,
                    reason="missing Discord application id for interaction response",
                )
            resp = await retry_request(
                client.patch,
                f"/webhooks/{application_id}/{interaction_token}/messages/@original",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            message_id = str(data.get("id") or "")
            if message_id:
                self._sent_messages[message_id] = channel_id
            log.debug(
                "discord.interaction_response_completed",
                interaction_id=interaction_id,
                message_id=message_id,
            )
            return ChannelSendResult.sent(
                capability=ChannelCapabilities.GROUP_CHAT,
                target_id=interaction_id or channel_id,
                provider_message_id=message_id,
            )

        resp = await retry_request(
            client.post,
            f"/channels/{channel_id}/messages",
            json=payload,
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        self._sent_messages[data["id"]] = channel_id
        log.debug("discord.send", channel_id=channel_id, message_id=data.get("id"))
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.GROUP_CHAT,
            target_id=channel_id,
            provider_message_id=str(data.get("id", "")),
        )

    MAX_FILE_BYTES: ClassVar[int] = 10 * 1024 * 1024

    async def send_file(
        self,
        channel_id: str,
        file_path: str,
        content: str = "",
    ) -> ChannelSendResult:
        check_channel_file_size(file_path, self.MAX_FILE_BYTES, "Discord")
        await self._rate_limiter.acquire()
        client = self._get_client()
        with open(file_path, "rb") as f:
            resp = await retry_request(
                client.post,
                f"/channels/{channel_id}/messages",
                data={"content": content} if content else {},
                files={"file": (Path(file_path).name, f)},
                headers=self._auth_headers(),
            )
        resp.raise_for_status()
        data = resp.json()
        message_id = str(data.get("id", ""))
        if message_id:
            self._sent_messages[message_id] = channel_id
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
            target_id=channel_id,
            provider_message_id=message_id,
        )

    async def edit(self, message_id: str, content: str) -> ChannelSendResult:
        await self._rate_limiter.acquire()
        client = self._get_client()
        channel_id = self._sent_messages.get(message_id, self.config.default_channel_id)
        resp = await retry_request(
            client.patch,
            f"/channels/{channel_id}/messages/{message_id}",
            json={"content": content},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        log.debug("discord.edit", message_id=message_id)
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.EDIT,
            target_id=channel_id,
            provider_message_id=message_id,
        )

    async def delete(self, message_id: str) -> ChannelSendResult:
        await self._rate_limiter.acquire()
        client = self._get_client()
        channel_id = self._sent_messages.get(message_id, self.config.default_channel_id)
        resp = await retry_request(
            client.delete,
            f"/channels/{channel_id}/messages/{message_id}",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        self._sent_messages.pop(message_id, None)
        log.debug("discord.delete", message_id=message_id)
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.DELETE,
            target_id=channel_id,
            provider_message_id=message_id,
        )

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    async def register_slash_commands(self, commands: list[dict[str, Any]]) -> None:
        """Register global slash commands for the bot application."""
        await self._rate_limiter.acquire()
        client = self._get_client()
        resp = await retry_request(
            client.put,
            f"/applications/{self.config.application_id}/commands",
            json=commands,
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        log.info("discord.commands_registered", count=len(commands))

    async def register_native_slash_commands(self) -> None:
        """Synchronize Discord's application-command menu with the registry."""
        if not self.config.application_id:
            log.warning(
                "discord.commands_not_registered",
                reason="missing_application_id",
                config_key="application_id",
                setup_hint=(
                    "Set channels.channels[].application_id to the Discord Application ID."
                ),
            )
            return
        await self.register_slash_commands(discord_application_commands())

    # ------------------------------------------------------------------
    # Mentions
    # ------------------------------------------------------------------

    @staticmethod
    def extract_mentions(text: str) -> list[str]:
        return _DISCORD_MENTION_RE.findall(text)

    @staticmethod
    def format_mention(user_id: str) -> str:
        return f"<@{user_id}>"

    def is_mentioned(self, text: str) -> bool:
        if self.bot_user_id is None:
            return False
        return self.bot_user_id in self.extract_mentions(text)

    def is_group_mentioned(self, msg: IncomingMessage) -> bool:
        """Uniform mention check for group gating. Delegates to is_mentioned."""
        if msg.metadata.get("interaction_type") == "slash_command":
            return True
        return self.is_mentioned(msg.content)

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def send_typing(self, channel_id: str | None = None) -> ChannelSendResult:
        """Send typing indicator via Discord REST API (lasts ~10s)."""
        target = channel_id or self.config.default_channel_id
        if not target:
            return ChannelSendResult.unsupported(
                capability=ChannelCapabilities.TYPING_INDICATOR,
                reason="no channel target",
            )
        client = self._get_client()
        await client.post(
            f"/channels/{target}/typing",
            headers=self._auth_headers(),
        )
        return ChannelSendResult.sent(
            capability=ChannelCapabilities.TYPING_INDICATOR,
            target_id=target,
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def send_streaming(
        self,
        chunks: AsyncIterator[str],
        *,
        channel_id: str | None = None,
        interaction_id: str | None = None,
        interaction_token: str | None = None,
        interaction_application_id: str | None = None,
        update_interval_ms: int = 500,
    ) -> str | None:
        """Stream a message: post first chunk, PATCH edits for subsequent.

        Returns the message ID or None if iterator was empty.

        Uses ``StreamThrottle`` so two PATCH calls cannot race and a
        single transient failure does not lose accumulated text.
        """
        target = channel_id or self.config.default_channel_id
        client = self._get_client()
        throttle = StreamThrottle(interval_s=update_interval_ms / 1000.0)
        message_id: str | None = None
        interaction_path: str | None = None
        if interaction_token:
            application_id = interaction_application_id or self.config.application_id
            if not application_id:
                raise ValueError("missing Discord application id for interaction response")
            interaction_path = f"/webhooks/{application_id}/{interaction_token}/messages/@original"

        async def _post(text: str) -> None:
            nonlocal message_id
            await self._rate_limiter.acquire()
            if interaction_path is not None:
                resp = await retry_request(
                    client.patch,
                    interaction_path,
                    json={"content": text},
                )
            else:
                resp = await retry_request(
                    client.post,
                    f"/channels/{target}/messages",
                    json={"content": text},
                    headers=self._auth_headers(),
                )
            resp.raise_for_status()
            message_id = resp.json().get("id")

        async def _edit(text: str) -> None:
            await self._rate_limiter.acquire()
            if interaction_path is not None:
                await retry_request(
                    client.patch,
                    interaction_path,
                    json={"content": text},
                )
            else:
                await retry_request(
                    client.patch,
                    f"/channels/{target}/messages/{message_id}",
                    json={"content": text},
                    headers=self._auth_headers(),
                )

        async for chunk in chunks:
            throttle.add(chunk)
            await throttle.maybe_flush(post=_post, edit=_edit)

        await throttle.force_flush(post=_post, edit=_edit)
        return message_id

    # ------------------------------------------------------------------
    # Session key
    # ------------------------------------------------------------------

    def session_key(self, user_id: str, channel_id: str) -> str:
        return f"discord:{user_id}:{channel_id}"

    def session_key_from_event(self, data: dict[str, Any]) -> str:
        user_id = data.get("author", {}).get("id", "unknown")
        channel_id = data.get("channel_id", "unknown")
        return self.session_key(user_id, channel_id)
