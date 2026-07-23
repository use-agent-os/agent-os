"""Channel protocol types: IncomingMessage, OutgoingMessage, Channel Protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Message models (external API data → Pydantic)
# ---------------------------------------------------------------------------


class UnsupportedChannelOperation(RuntimeError):  # noqa: N818
    """Raised when a public channel API does not expose an operation."""

    def __init__(self, *, channel: str, operation: str, reason: str) -> None:
        self.channel = channel
        self.operation = operation
        self.reason = reason
        super().__init__(f"{channel}.{operation} is unsupported: {reason}")


class Attachment(BaseModel):
    """File or media attachment on a message."""

    name: str
    mime_type: str | None = None
    url: str | None = None
    data: bytes | None = None
    size: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncomingMessage(BaseModel):
    """Normalized inbound message from any channel.

    Metadata contract for channel adapters:
    - ``conversation_kind``: one of ``dm``, ``group``, ``group_dm``,
      ``thread``, ``topic``, or ``interaction``.
    - ``native_message_id``: platform-native message id.
    - ``native_chat_id``: platform-native chat, channel, or room id.
    - ``native_thread_id``: platform-native thread or topic id.
    - ``native_parent_id``: platform-native parent message id.
    - ``native_parent_channel_id``: platform-native parent channel id.
    - ``native_root_id``: platform-native root message id.
    - ``reply_target_id``: platform-native message id to reply to.
    - ``is_group``: bool consumed by ``ChannelManager`` for session keys.
    """

    sender_id: str
    channel_id: str
    content: str
    attachments: list[Attachment] = []
    metadata: dict[str, Any] = {}


class OutgoingMessage(BaseModel):
    """Normalized outbound message to any channel."""

    content: str
    attachments: list[Attachment] = []
    metadata: dict[str, Any] = {}
    reply_to: str | None = None


# ---------------------------------------------------------------------------
# Channel Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Channel(Protocol):
    """Abstract channel adapter protocol."""

    async def receive(self) -> IncomingMessage:
        """Block until an inbound message arrives and return it."""
        ...

    async def send(self, message: OutgoingMessage) -> None:
        """Deliver an outbound message to the channel."""
        ...

    async def edit(self, message_id: str, content: str) -> None:
        """Edit a previously sent message by ID."""
        ...

    async def delete(self, message_id: str) -> None:
        """Delete a previously sent message by ID."""
        ...


@runtime_checkable
class ManagedChannel(Channel, Protocol):
    """Channel with lifecycle management (start/stop/health).

    External channels (Slack, Discord, Telegram) that need connection
    management implement this. Simple channels (Terminal, WebSocket)
    only need the base Channel protocol.

    Async-lifecycle convention
    --------------------------
    Adapters whose underlying SDK exposes only an infinite-loop entry
    point MUST wrap that loop in ``asyncio.create_task(...)`` spawned
    from ``start()`` and return once the task is registered. ``stop()``
    cancels the task and awaits its completion. ``ChannelManager``
    relies on this contract to bound ``start_all()`` with a 30 s
    timeout by default.
    Adapters with known slow cold starts may expose ``startup_timeout_s``.

    metadata['is_group'] contract
    -----------------------------
    Every ``IncomingMessage`` yielded from ``receive()`` MUST set
    ``metadata['is_group']: bool`` — ``True`` for group / room
    messages, ``False`` for DMs. ``ChannelManager._build_session_key``
    reads this flag first; it falls back to legacy hardcoded
    Slack / Discord checks for backward compatibility with
    older adapters.
    """

    async def start(self) -> None:
        """Validate credentials, open connections, start background tasks."""
        ...

    async def stop(self) -> None:
        """Close connections, cancel background tasks, release resources."""
        ...

    async def health_check(self) -> ChannelHealth:
        """Return current health status of the adapter."""
        ...


# ---------------------------------------------------------------------------
# Internal channel state (dataclass for speed)
# ---------------------------------------------------------------------------


@dataclass
class ChannelMeta:
    """Internal runtime metadata for a channel instance."""

    channel_id: str
    label: str
    markdown_capable: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelHealth:
    """Health status of a channel adapter."""

    connected: bool
    bot_user_id: str | None = None
    last_message_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveryTargetResolution:
    """Resolved outbound delivery target for a managed channel."""

    ok: bool
    adapter: Any | None = None
    adapter_name: str = ""
    channel_type: str = ""
    to: str = ""
    account_id: str = ""
    thread_id: str = ""
    reason: str | None = None
