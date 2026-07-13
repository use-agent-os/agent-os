"""agentos.channels — Channel adapter layer.

Adapters: Terminal, WebSocket, Slack, Discord, Telegram.
"""

from agentos.channels.discord import DiscordChannel
from agentos.channels.manager import ChannelManager
from agentos.channels.slack import SlackChannel
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.terminal import TerminalChannel
from agentos.channels.types import (
    Attachment,
    Channel,
    ChannelHealth,
    ChannelMeta,
    IncomingMessage,
    ManagedChannel,
    OutgoingMessage,
)
from agentos.channels.websocket import WebSocketChannel

__all__ = [
    # Protocol + types
    "Channel",
    "ManagedChannel",
    "ChannelHealth",
    "ChannelMeta",
    "IncomingMessage",
    "OutgoingMessage",
    "Attachment",
    # Manager
    "ChannelManager",
    # Adapters
    "TerminalChannel",
    "WebSocketChannel",
    "SlackChannel",
    "DiscordChannel",
    "TelegramChannel",
    "TelegramChannelConfig",
]
