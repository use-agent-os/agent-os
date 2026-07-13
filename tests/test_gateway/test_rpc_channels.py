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
from agentos.gateway.auth import Principal
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.onboarding.mutations import upsert_channel


def _read_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
        ),
    )


def _pairing_ctx(tmp_path) -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.pairing"}),
            is_owner=True,
            authenticated=True,
        ),
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
        }
    ]


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
    assert row["platform_manifest"]["capabilities"][ChannelPlatformCategories.CHAT][
        "status"
    ] == ChannelPlatformCapabilityStatus.SUPPORTED
    assert row["platform_manifest"]["capabilities"][ChannelPlatformCategories.FILES][
        "status"
    ] == ChannelPlatformCapabilityStatus.CONFIG_REQUIRED
    assert row["platform_manifest"]["capabilities"][ChannelPlatformCategories.DOCS][
        "status"
    ] == ChannelPlatformCapabilityStatus.UNSUPPORTED
    assert row["diagnostics"] == {"network_probe": "not_run"}


@pytest.mark.asyncio
async def test_telegram_access_approval_persists_and_updates_live_adapter(tmp_path):
    ctx = _pairing_ctx(tmp_path)
    configured = upsert_channel(
        ctx.config,
        entry_payload={
            "type": "telegram",
            "name": "tg",
            "token": "secret",
            "access_mode": "pairing",
        },
    )
    ctx.config = configured.config
    ctx.config.config_path = str(tmp_path / "agentos.toml")
    adapter = TelegramChannel(
        TelegramChannelConfig(name="tg", token="secret", access_mode="pairing"),
        pairing_store=ChannelPairingStore(tmp_path / "pairing"),
    )
    message = IncomingMessage(
        sender_id="42",
        channel_id="42",
        content="hello",
        metadata={"sender_username": "alice", "sender_display_name": "Alice"},
    )
    adapter.record_access_denial(message, "not_in_allowlist")
    adapter.notify_access_resolution = AsyncMock()  # type: ignore[method-assign]

    class FakeManager:
        def get(self, name: str):
            return adapter if name == "tg" else None

    ctx.channel_manager = FakeManager()

    listed = await get_dispatcher().dispatch("r1", "channels.access.list", {}, ctx)
    resolved = await get_dispatcher().dispatch(
        "r2",
        "channels.access.resolve",
        {"channel": "tg", "senderId": "42", "approved": True},
        ctx,
    )

    assert listed.error is None, listed.error
    assert listed.payload["channels"][0]["pending"][0]["username"] == "alice"
    assert resolved.error is None, resolved.error
    assert resolved.payload == {"channel": "tg", "senderId": "42", "approved": True}
    assert ctx.config.channels.channels[0].approved_sender_ids == []
    assert adapter.pairing_store.is_approved("tg", "42") is True

    revoked = await get_dispatcher().dispatch(
        "r3",
        "channels.access.revoke",
        {"channel": "tg", "senderId": "42"},
        ctx,
    )

    assert revoked.error is None, revoked.error
    assert revoked.payload["source"] == "pairing"
    assert adapter.pairing_store.is_approved("tg", "42") is False


@pytest.mark.asyncio
async def test_telegram_access_mode_updates_without_active_adapter(tmp_path):
    ctx = _pairing_ctx(tmp_path)
    configured = upsert_channel(
        ctx.config,
        entry_payload={"type": "telegram", "name": "tg", "token": "secret"},
    )
    ctx.config = configured.config
    ctx.config.config_path = str(tmp_path / "agentos.toml")

    result = await get_dispatcher().dispatch(
        "r1",
        "channels.access.setMode",
        {"channel": "tg", "mode": "pairing"},
        ctx,
    )

    assert result.error is None, result.error
    assert result.payload["restartRequired"] is False
    assert ctx.config.channels.channels[0].access_mode == "pairing"


@pytest.mark.asyncio
async def test_telegram_access_can_be_approved_without_active_adapter(
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
        "channels.access.resolve",
        {"channel": "tg", "senderId": "42", "approved": True},
        ctx,
    )

    assert result.error is None, result.error
    assert store.is_approved("tg", "42") is True
