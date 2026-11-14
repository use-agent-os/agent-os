"""RPC tests for channel status payloads."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import agentos.gateway.rpc_channels  # noqa: F401  ensures registration
from agentos.channel_pairing import ChannelPairingStore
from agentos.channels.contract import (
    ChannelCapabilities,
    ChannelCapabilityProfile,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
)
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.onboarding.mutations import upsert_channel


def _read_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t",
    )


def _pairing_ctx(tmp_path) -> RpcContext:
    return RpcContext(
        conn_id="t",
        config=GatewayConfig(config_path=str(tmp_path / "agentos.toml")),
    )


@pytest.mark.asyncio
async def test_channels_status_includes_configured_channels_without_manager():
    ctx = _read_ctx()
    res = upsert_channel(
        GatewayConfig(),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "xoxb-secret",
            "signing_secret": "ss",
        },
    )
    ctx.config = res.config

    rpc_res = await get_dispatcher().dispatch("r1", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["channels"] == [
        {
            "name": "work",
            "connected": False,
            "status": "stopped",
            "bot_user_id": None,
            "connected_since": None,
            "restart_attempts": 0,
            "type": "slack",
            "enabled": True,
            "configured": True,
            "capabilities": [],
            "capability_profile": None,
            "platform_manifest": None,
            "diagnostics": {"network_probe": "not_run"},
            # No allowed_user_ids and no pairing: the channel admits nobody.
            "admission": {"mode": "closed", "allowed_user_count": 0, "pairing_enabled": False},
            # No channel manager: no runtime view for the health sweep.
            "state": None,
            "last_event_at": None,
            "terminal_reason": None,
            "start_error": None,
        }
    ]
    policy = rpc_res.payload["health_policy"]
    assert policy["active"] is False
    assert policy["policy"]["sweep_interval_s"] == 300.0
    assert policy["safe_mode"] == {"active": False}


@pytest.mark.asyncio
async def test_channels_status_reports_adapter_capabilities_without_network_probe():
    class FakeHealth:
        connected = True
        bot_user_id = "bot-1"
        extra = {"connected_since": "now", "restart_attempts": 2}

    class FakeAdapter:
        capability_profile = ChannelCapabilityProfile(
            channel_type="discord",
            group_chat=True,
            native_file_upload=True,
            inbound_reactions=True,
            thread_messages=True,
            group_dm=True,
            transports=("websocket",),
        )

    class FakeManager:
        _channel_types = {"discord": "discord"}

        async def health(self):
            return {"discord": FakeHealth()}

        def get(self, name: str):
            assert name == "discord"
            return FakeAdapter()

    ctx = _read_ctx()
    ctx.channel_manager = FakeManager()

    rpc_res = await get_dispatcher().dispatch("r1", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload is not None
    row = rpc_res.payload["channels"][0]
    assert row["name"] == "discord"
    assert row["status"] == "connected"
    assert set(row["capabilities"]) >= {
        ChannelCapabilities.GROUP_CHAT,
        ChannelCapabilities.GROUP_DM,
        ChannelCapabilities.INBOUND_REACTIONS,
        ChannelCapabilities.NATIVE_FILE_UPLOAD,
        ChannelCapabilities.THREAD_MESSAGES,
        ChannelCapabilities.WEBSOCKET,
    }
    assert row["capability_profile"] == {
        "channel_type": "discord",
        "transports": ["websocket"],
    }
    assert row["platform_manifest"]["channel_type"] == "discord"
    assert (
        row["platform_manifest"]["capabilities"][ChannelPlatformCategories.CHAT]["status"]
        == ChannelPlatformCapabilityStatus.SUPPORTED
    )
    assert (
        row["platform_manifest"]["capabilities"][ChannelPlatformCategories.FILES]["status"]
        == ChannelPlatformCapabilityStatus.CONFIG_REQUIRED
    )
    assert (
        row["platform_manifest"]["capabilities"][ChannelPlatformCategories.DOCS]["status"]
        == ChannelPlatformCapabilityStatus.UNSUPPORTED
    )
    assert row["diagnostics"] == {"network_probe": "not_run"}


@pytest.mark.asyncio
async def test_telegram_pairing_persists_and_updates_live_adapter(tmp_path):
    ctx = _pairing_ctx(tmp_path)
    configured = upsert_channel(
        ctx.config,
        entry_payload={
            "type": "telegram",
            "name": "tg",
            "token": "secret",
        },
    )
    ctx.config = configured.config
    ctx.config.config_path = str(tmp_path / "agentos.toml")
    adapter = TelegramChannel(
        TelegramChannelConfig(name="tg", token="secret"),
        pairing_store=ChannelPairingStore(tmp_path / "pairing"),
    )
    message = IncomingMessage(
        sender_id="42",
        channel_id="42",
        content="hello",
        metadata={"sender_username": "alice", "sender_display_name": "Alice"},
    )
    adapter.record_access_denial(message, "not_paired")
    adapter.notify_access_resolution = AsyncMock()  # type: ignore[method-assign]

    class FakeManager:
        def get(self, name: str):
            return adapter if name == "tg" else None

    ctx.channel_manager = FakeManager()

    listed = await get_dispatcher().dispatch("r1", "channels.pairing.list", {}, ctx)
    resolved = await get_dispatcher().dispatch(
        "r2",
        "channels.pairing.approve",
        {"channel": "tg", "senderId": "42"},
        ctx,
    )

    assert listed.error is None, listed.error
    assert listed.payload["channels"][0]["pending"][0]["username"] == "alice"
    assert resolved.error is None, resolved.error
    assert resolved.payload == {"channel": "tg", "senderId": "42", "status": "paired"}
    assert adapter.pairing_store.is_approved("tg", "42") is True

    revoked = await get_dispatcher().dispatch(
        "r3",
        "channels.pairing.revoke",
        {"channel": "tg", "senderId": "42"},
        ctx,
    )

    assert revoked.error is None, revoked.error
    assert revoked.payload["status"] == "disconnected"
    assert adapter.pairing_store.is_approved("tg", "42") is False


@pytest.mark.asyncio
async def test_telegram_pairing_list_exposes_fixed_group_posture_without_adapter(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    ctx = _pairing_ctx(tmp_path)
    configured = upsert_channel(
        ctx.config,
        entry_payload={
            "type": "telegram",
            "name": "tg",
            "token": "secret",
            "groups_enabled": True,
            "group_chat_ids": ["-100"],
            "group_mention_required": True,
        },
    )
    ctx.config = configured.config
    ctx.config.config_path = str(tmp_path / "agentos.toml")

    result = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.list",
        {},
        ctx,
    )

    assert result.error is None, result.error
    row = result.payload["channels"][0]
    assert row["pending"] == []
    assert row["paired"] == []
    assert row["groups_enabled"] is True
    assert row["group_chat_ids"] == ["-100"]
    assert row["group_mention_required"] is True


@pytest.mark.asyncio
async def test_telegram_pairing_can_be_approved_without_active_adapter(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    ctx = _pairing_ctx(tmp_path)
    configured = upsert_channel(
        ctx.config,
        entry_payload={"type": "telegram", "name": "tg", "token": "secret"},
    )
    ctx.config = configured.config
    ctx.config.config_path = str(tmp_path / "agentos.toml")
    store = ChannelPairingStore()
    store.request("tg", "42", profile={"username": "alice"})

    result = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.approve",
        {"channel": "tg", "senderId": "42"},
        ctx,
    )

    assert result.error is None, result.error
    assert store.is_approved("tg", "42") is True


@pytest.mark.asyncio
async def test_discord_pairing_rpc_lists_and_approves_when_enabled(tmp_path, monkeypatch):
    """AOS-01: channels.pairing.* serves Discord/Slack entries with pairing on."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    ctx = _pairing_ctx(tmp_path)
    configured = upsert_channel(
        ctx.config,
        entry_payload={
            "type": "discord",
            "name": "dc",
            "token": "secret",
            "pairing_enabled": True,
        },
    )
    configured = upsert_channel(
        configured.config,
        entry_payload={"type": "slack", "name": "sl", "token": "xoxb", "signing_secret": "s"},
    )
    ctx.config = configured.config
    ctx.config.config_path = str(tmp_path / "agentos.toml")
    store = ChannelPairingStore()
    store.request("dc", "4242", profile={"chat_id": "dm-1"})

    listed = await get_dispatcher().dispatch("r1", "channels.pairing.list", {}, ctx)
    approved = await get_dispatcher().dispatch(
        "r2", "channels.pairing.approve", {"channel": "dc", "senderId": "4242"}, ctx
    )
    refused = await get_dispatcher().dispatch(
        "r3", "channels.pairing.approve", {"channel": "sl", "senderId": "4242"}, ctx
    )

    assert listed.error is None, listed.error
    rows = listed.payload["channels"]
    assert [(row["name"], row["type"]) for row in rows] == [("dc", "discord")]
    assert rows[0]["pending"][0]["sender_id"] == "4242"
    assert approved.error is None, approved.error
    assert store.is_approved("dc", "4242") is True
    # Slack entry without pairing_enabled is not a pairing channel.
    assert refused.error is not None
