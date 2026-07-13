"""Channels domain RPC handlers."""

from __future__ import annotations

from typing import Any

import structlog

from agentos.channel_pairing import ChannelPairingStore
from agentos.channels.contract import (
    channel_capability_profile,
    channel_platform_manifest,
)
from agentos.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()
log = structlog.get_logger(__name__)


def _channel_status(connected: bool) -> str:
    return "connected" if connected else "stopped"


def _configured_channel_entries(ctx: RpcContext) -> list[dict[str, Any]]:
    config = getattr(ctx, "config", None)
    channels_cfg = getattr(config, "channels", None)
    entries = getattr(channels_cfg, "channels", None) or []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if hasattr(entry, "model_dump"):
            out.append(entry.model_dump(mode="python"))
        elif isinstance(entry, dict):
            out.append(dict(entry))
    return out


def _health_extra(health: Any) -> dict[str, Any]:
    extra = getattr(health, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _status_for(*, connected: bool, enabled: bool, dispatch_state: str | None) -> str:
    if not enabled:
        return "disabled"
    if dispatch_state in {"dead", "exhausted", "restarting"}:
        return dispatch_state
    return _channel_status(connected)


def _capability_payload(adapter: Any | None) -> tuple[list[str], dict[str, Any] | None]:
    profile = channel_capability_profile(adapter)
    if profile is None:
        return [], None
    return sorted(profile.capability_tags()), {
        "channel_type": profile.channel_type,
        "transports": list(profile.transports),
    }


def _platform_manifest_payload(adapter: Any | None) -> dict[str, Any] | None:
    manifest = channel_platform_manifest(adapter)
    return manifest.to_dict() if manifest is not None else None


def _diagnostics_payload() -> dict[str, Any]:
    return {"network_probe": "not_run"}


def _telegram_access_rows(ctx: RpcContext) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in _configured_channel_entries(ctx):
        if entry.get("type") != "telegram":
            continue
        name = str(entry.get("name") or "")
        adapter = ctx.channel_manager.get(name) if ctx.channel_manager else None
        snapshot_fn = getattr(adapter, "access_snapshot", None)
        if callable(snapshot_fn):
            snapshot = snapshot_fn()
        else:
            pairing = ChannelPairingStore().snapshot(name)
            snapshot = {
                "mode": entry.get("access_mode") or "pairing",
                "group_mode": entry.get("group_access_mode") or "allowlist",
                "pending": pairing["pending"],
                "approved": [dict(item, source="pairing") for item in pairing["approved"]] + [
                    {
                        "sender_id": str(sender_id),
                        "username": "",
                        "display_name": "",
                        "chat_id": "",
                        "source": "config",
                    }
                    for sender_id in entry.get("approved_sender_ids") or []
                ],
                "locked_until": pairing["locked_until"],
            }
        rows.append({"name": name, "type": "telegram", **snapshot})
    return rows


def _telegram_entry(ctx: RpcContext, channel_name: str) -> tuple[Any, Any]:
    from agentos.gateway.rpc_onboarding import _active_config

    config = _active_config(ctx)
    entry = next(
        (item for item in config.channels.channels if item.name == channel_name),
        None,
    )
    if entry is None:
        raise KeyError(f"Channel not found: {channel_name}")
    if entry.type != "telegram":
        raise ValueError("Chat account approvals are only supported for Telegram channels")
    return config, entry


def _persist_telegram_access(
    ctx: RpcContext,
    channel_name: str,
    *,
    access_mode: str | None = None,
    approved_sender_ids: list[str] | None = None,
) -> tuple[Any, str]:
    from agentos.gateway.rpc_onboarding import _apply_inplace, _persist
    from agentos.onboarding.mutations import upsert_channel

    config, entry = _telegram_entry(ctx, channel_name)
    payload = entry.model_dump(mode="python")
    if access_mode is not None:
        payload["access_mode"] = access_mode
    if approved_sender_ids is not None:
        payload["approved_sender_ids"] = approved_sender_ids
    result = upsert_channel(config, entry_payload=payload)
    config_path = _persist(ctx, result.config, restart_required=False)
    _apply_inplace(ctx, result.config)
    updated = next(item for item in result.config.channels.channels if item.name == channel_name)
    return updated, config_path


def _required_string(params: dict | None, key: str) -> str:
    value = str((params or {}).get(key) or "").strip()
    if not value:
        raise ValueError(f"params.{key} is required")
    return value


@_d.method("channels.status", scope="operator.read")
async def _handle_channels_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    health_map = await ctx.channel_manager.health() if ctx.channel_manager else {}
    manager_types = (
        getattr(ctx.channel_manager, "_channel_types", {}) if ctx.channel_manager else {}
    )
    channels: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in _configured_channel_entries(ctx):
        name = str(entry.get("name") or "")
        if not name:
            continue
        enabled = bool(entry.get("enabled", True))
        health = health_map.get(name)
        extra = _health_extra(health)
        adapter = ctx.channel_manager.get(name) if ctx.channel_manager else None
        capabilities, capability_profile = _capability_payload(adapter)
        platform_manifest = _platform_manifest_payload(adapter)
        connected = bool(getattr(health, "connected", False)) if health else False
        channels.append(
            {
                "name": name,
                "connected": connected,
                "status": _status_for(
                    connected=connected,
                    enabled=enabled,
                    dispatch_state=extra.get("dispatch_state"),
                ),
                "bot_user_id": getattr(health, "bot_user_id", None) if health else None,
                "connected_since": extra.get("connected_since"),
                "restart_attempts": extra.get("restart_attempts", 0),
                "type": entry.get("type"),
                "enabled": enabled,
                "configured": True,
                "capabilities": capabilities,
                "capability_profile": capability_profile,
                "platform_manifest": platform_manifest,
                "diagnostics": _diagnostics_payload(),
            }
        )
        seen.add(name)

    for name, health in health_map.items():
        if name in seen:
            continue
        extra = _health_extra(health)
        adapter = ctx.channel_manager.get(name) if ctx.channel_manager else None
        capabilities, capability_profile = _capability_payload(adapter)
        platform_manifest = _platform_manifest_payload(adapter)
        connected = bool(getattr(health, "connected", False))
        channels.append(
            {
                "name": name,
                "connected": connected,
                "status": _status_for(
                    connected=connected,
                    enabled=True,
                    dispatch_state=extra.get("dispatch_state"),
                ),
                "bot_user_id": getattr(health, "bot_user_id", None),
                "connected_since": extra.get("connected_since"),
                "restart_attempts": extra.get("restart_attempts", 0),
                "type": manager_types.get(name) or type(adapter).__name__,
                "enabled": True,
                "configured": False,
                "capabilities": capabilities,
                "capability_profile": capability_profile,
                "platform_manifest": platform_manifest,
                "diagnostics": _diagnostics_payload(),
            }
        )

    return {"channels": channels}


@_d.method("channels.access.list", scope="operator.pairing")
async def _handle_channels_access_list(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return {"channels": _telegram_access_rows(ctx)}


@_d.method("channels.access.setMode", scope="operator.pairing")
async def _handle_channels_access_set_mode(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    channel_name = _required_string(params, "channel")
    mode = _required_string(params, "mode")
    if mode == "approval":
        mode = "pairing"
    if mode not in {"pairing", "allowlist", "open", "disabled"}:
        raise ValueError("params.mode must be pairing, allowlist, open, or disabled")
    _updated, config_path = _persist_telegram_access(
        ctx,
        channel_name,
        access_mode=mode,
    )
    adapter = ctx.channel_manager.get(channel_name) if ctx.channel_manager else None
    set_mode = getattr(adapter, "set_access_mode", None)
    if callable(set_mode):
        set_mode(mode)
    return {
        "channel": channel_name,
        "mode": mode,
        "configPath": config_path,
        "restartRequired": False,
    }


@_d.method("channels.access.resolve", scope="operator.pairing")
async def _handle_channels_access_resolve(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    channel_name = _required_string(params, "channel")
    sender_id = _required_string(params, "senderId")
    if not isinstance(params, dict) or "approved" not in params:
        raise ValueError("params.approved is required")
    if not isinstance(params["approved"], bool):
        raise ValueError("params.approved must be a boolean")
    approved = params["approved"]
    _telegram_entry(ctx, channel_name)
    adapter = ctx.channel_manager.get(channel_name) if ctx.channel_manager else None
    store = getattr(adapter, "pairing_store", None) or ChannelPairingStore()
    snapshot_fn = getattr(adapter, "access_snapshot", None)
    snapshot = snapshot_fn() if callable(snapshot_fn) else store.snapshot(channel_name)
    pending = {
        str(item.get("sender_id") or "") for item in snapshot.get("pending", [])
    }
    if sender_id not in pending:
        raise KeyError(f"Telegram access request not found: {sender_id}")

    resolve = getattr(adapter, "resolve_access_request", None)
    if callable(resolve):
        request = resolve(sender_id, approved=approved)
    else:
        pending_request = next(
            item
            for item in snapshot["pending"]
            if str(item.get("sender_id") or "") == sender_id
        )
        request = (
            store.approve(channel_name, str(pending_request.get("code") or ""))
            if approved
            else store.deny(channel_name, sender_id)
        )
    notify = getattr(adapter, "notify_access_resolution", None)
    if callable(notify):
        try:
            await notify(request, approved=approved)
        except Exception as exc:  # noqa: BLE001 - decision has already been committed.
            log.warning(
                "channel.access_resolution_notification_failed",
                channel=channel_name,
                sender_id=sender_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
    return {"channel": channel_name, "senderId": sender_id, "approved": approved}


@_d.method("channels.access.revoke", scope="operator.pairing")
async def _handle_channels_access_revoke(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    channel_name = _required_string(params, "channel")
    sender_id = _required_string(params, "senderId")
    _config, entry = _telegram_entry(ctx, channel_name)
    adapter = ctx.channel_manager.get(channel_name) if ctx.channel_manager else None
    if sender_id in entry.approved_sender_ids:
        approved_ids = [item for item in entry.approved_sender_ids if item != sender_id]
        _persist_telegram_access(
            ctx,
            channel_name,
            approved_sender_ids=approved_ids,
        )
        if adapter is not None:
            adapter.config.approved_sender_ids = approved_ids
        source = "config"
    else:
        store = getattr(adapter, "pairing_store", None) or ChannelPairingStore()
        store.revoke(channel_name, sender_id)
        source = "pairing"
    return {
        "channel": channel_name,
        "senderId": sender_id,
        "revoked": True,
        "source": source,
    }


@_d.method("channels.logout", scope="operator.admin")
async def _handle_channels_logout(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    channel_name = None
    if isinstance(params, dict):
        channel_name = params.get("channel") or params.get("name")
    if not channel_name:
        raise ValueError("channel name required")
    if ctx.channel_manager is None:
        raise KeyError(f"Channel not found: {channel_name}")
    if ctx.channel_manager.get(channel_name) is None:
        raise KeyError(f"Channel not found: {channel_name}")
    await ctx.channel_manager.stop_channel(channel_name)
    return {"status": "disconnected", "channel": channel_name}


@_d.method("channels.restart", scope="operator.admin")
async def _handle_channels_restart(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    channel_name = None
    if isinstance(params, dict):
        channel_name = params.get("channel") or params.get("name")
    if not channel_name:
        raise ValueError("channel name required")
    if ctx.channel_manager is None:
        raise KeyError(f"Channel not found: {channel_name}")
    if ctx.channel_manager.get(channel_name) is None:
        raise KeyError(f"Channel not found: {channel_name}")
    await ctx.channel_manager.restart_channel(channel_name)
    return {"status": "restarted", "channel": channel_name}
