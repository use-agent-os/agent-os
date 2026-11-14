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
    if dispatch_state == "retrying":
        # A failed automatic restart the manager is retrying with backoff.
        return "restarting"
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


def _telegram_pairing_rows(ctx: RpcContext) -> list[dict[str, Any]]:
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
                "pending": pairing["pending"],
                "paired": pairing["approved"],
                "locked_until": pairing["locked_until"],
                "groups_enabled": bool(entry.get("groups_enabled", False)),
                "group_chat_ids": list(entry.get("group_chat_ids") or []),
                "group_mention_required": bool(entry.get("group_mention_required", True)),
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
        raise ValueError("Pairing is only supported for Telegram channels")
    return config, entry


def _required_string(params: dict | None, key: str) -> str:
    value = str((params or {}).get(key) or "").strip()
    if not value:
        raise ValueError(f"params.{key} is required")
    return value


async def _runtime_status_map(ctx: RpcContext) -> dict[str, dict[str, Any]]:
    probe = getattr(ctx.channel_manager, "runtime_status", None) if ctx.channel_manager else None
    if not callable(probe):
        return {}
    try:
        result = await probe()
    except Exception:  # noqa: BLE001 - status must not fail on the runtime probe
        log.warning("channels.runtime_status_failed", exc_info=True)
        return {}
    return result if isinstance(result, dict) else {}


def _runtime_fields(runtime: dict[str, Any] | None, *, now: float) -> dict[str, Any]:
    """``state``, ``last_event_at``, ``terminal_reason``: the health sweep's view."""
    from agentos.gateway.maintenance import ChannelHealthPolicy, classify, get_maintenance

    if not runtime:
        return {"state": None, "last_event_at": None, "terminal_reason": None, "start_error": None}
    maintenance = get_maintenance()
    policy = maintenance.policy if maintenance is not None else ChannelHealthPolicy()
    last_event = runtime.get("last_event_at")
    return {
        "state": classify(runtime, now=now, policy=policy),
        "last_event_at": _iso_ts(last_event),
        "terminal_reason": runtime.get("terminal_reason"),
        "start_error": runtime.get("start_error"),
    }


def _iso_ts(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or not value:
        return None
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(float(value), UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _health_policy_payload() -> dict[str, Any]:
    """The auto-restart policy plus the last sweep/thaw, for ``channels.status``."""
    from agentos.gateway.boot_lifecycle import get_boot_tracker
    from agentos.gateway.maintenance import ChannelHealthPolicy, get_maintenance

    maintenance = get_maintenance()
    if maintenance is not None:
        payload = maintenance.snapshot()
        payload["active"] = True
    else:
        payload = {
            "policy": ChannelHealthPolicy().to_payload(),
            "last_sweep": None,
            "last_thaw": None,
            "active": False,
        }
    tracker = get_boot_tracker()
    payload["safe_mode"] = tracker.snapshot() if tracker is not None else {"active": False}
    return payload


@_d.method("channels.status")
async def _handle_channels_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    import time

    health_map = await ctx.channel_manager.health() if ctx.channel_manager else {}
    runtime_map = await _runtime_status_map(ctx)
    now = time.time()
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
                **_runtime_fields(runtime_map.get(name), now=now),
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
                **_runtime_fields(runtime_map.get(name), now=now),
            }
        )

    return {"channels": channels, "health_policy": _health_policy_payload()}


def _telegram_delivery_targets(row: dict[str, Any]) -> list[dict[str, str]]:
    """Recipients a Telegram channel can be pointed at, from its access snapshot."""
    targets: list[dict[str, str]] = []
    for user in row.get("paired") or []:
        chat_id = str(user.get("chat_id") or user.get("sender_id") or "").strip()
        if not chat_id:
            continue
        display = str(user.get("display_name") or "").strip()
        username = str(user.get("username") or "").strip().lstrip("@")
        if display and username:
            label = f"{display} (@{username})"
        else:
            label = display or (f"@{username}" if username else chat_id)
        targets.append({"id": chat_id, "label": label, "kind": "dm"})
    for group_id in row.get("group_chat_ids") or []:
        chat_id = str(group_id).strip()
        if chat_id:
            targets.append({"id": chat_id, "label": chat_id, "kind": "group"})
    return targets


@_d.method("channels.deliveryTargets")
async def _handle_channels_delivery_targets(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Recipients that can be offered as a choice, keyed by channel name.

    Only channels whose recipients are actually known appear — today that means
    Telegram, which has a pairing store. A caller finding its channel absent
    should ask the operator to type the id rather than pretend the list is
    exhaustive.
    """
    targets: dict[str, list[dict[str, str]]] = {}
    for row in _telegram_pairing_rows(ctx):
        rows = _telegram_delivery_targets(row)
        if rows:
            targets[str(row.get("name") or "")] = rows
    return {"targets": targets}


@_d.method("channels.pairing.list")
async def _handle_channels_pairing_list(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return {"channels": _telegram_pairing_rows(ctx)}


async def _resolve_pairing_request(
    params: dict | None,
    ctx: RpcContext,
    *,
    approved: bool,
) -> dict[str, Any]:
    channel_name = _required_string(params, "channel")
    sender_id = _required_string(params, "senderId")
    _telegram_entry(ctx, channel_name)
    adapter = ctx.channel_manager.get(channel_name) if ctx.channel_manager else None
    store = getattr(adapter, "pairing_store", None) or ChannelPairingStore()
    snapshot_fn = getattr(adapter, "access_snapshot", None)
    snapshot = snapshot_fn() if callable(snapshot_fn) else store.snapshot(channel_name)
    pending = {str(item.get("sender_id") or "") for item in snapshot.get("pending", [])}
    if sender_id not in pending:
        raise KeyError(f"Telegram access request not found: {sender_id}")

    resolve = getattr(adapter, "resolve_access_request", None)
    if callable(resolve):
        request = resolve(sender_id, approved=approved)
    else:
        request_entry = next(
            item for item in snapshot["pending"] if str(item.get("sender_id") or "") == sender_id
        )
        request = (
            store.approve(channel_name, str(request_entry.get("code") or ""))
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
    return {
        "channel": channel_name,
        "senderId": sender_id,
        "status": "paired" if approved else "denied",
    }


@_d.method("channels.pairing.approve")
async def _handle_channels_pairing_approve(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    # Letting a new sender in is the user's decision, like any approval; an
    # agent's connection may deny, revoke or clear, never admit.
    from agentos.gateway.rpc_approvals import require_human

    require_human(ctx, "Pairing a channel sender")
    return await _resolve_pairing_request(params, ctx, approved=True)


@_d.method("channels.pairing.deny")
async def _handle_channels_pairing_deny(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return await _resolve_pairing_request(params, ctx, approved=False)


@_d.method("channels.pairing.revoke")
async def _handle_channels_pairing_revoke(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    channel_name = _required_string(params, "channel")
    sender_id = _required_string(params, "senderId")
    _telegram_entry(ctx, channel_name)
    adapter = ctx.channel_manager.get(channel_name) if ctx.channel_manager else None
    store = getattr(adapter, "pairing_store", None) or ChannelPairingStore()
    store.revoke(channel_name, sender_id)
    return {
        "channel": channel_name,
        "senderId": sender_id,
        "revoked": True,
        "status": "disconnected",
    }


@_d.method("channels.pairing.clearPending")
async def _handle_channels_pairing_clear_pending(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    channel_name = _required_string(params, "channel")
    _telegram_entry(ctx, channel_name)
    adapter = ctx.channel_manager.get(channel_name) if ctx.channel_manager else None
    store = getattr(adapter, "pairing_store", None) or ChannelPairingStore()
    cleared = store.clear_pending(channel_name)
    return {"channel": channel_name, "cleared": cleared}


@_d.method("channels.logout")
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


@_d.method("channels.start")
async def _handle_channels_start(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Start one channel by name, or every channel; the safe-mode override.

    Without a name: leaves the gateway's safe mode (acknowledging the crashes
    that tripped it, so the next boot does not re-enter it) and starts every
    channel, the scheduler and the heartbeat. With a name: starts just that
    channel and leaves safe mode as it is -- the way to find which channel
    crashed the gateway.
    """
    from agentos.gateway.boot_lifecycle import get_boot_tracker

    channel_name = None
    if isinstance(params, dict):
        channel_name = params.get("channel") or params.get("name")
    manager = ctx.channel_manager
    tracker = get_boot_tracker()
    safe_mode_was_active = bool(tracker is not None and tracker.safe_mode_active)

    if channel_name:
        if manager is None or manager.get(channel_name) is None:
            raise KeyError(f"Channel not found: {channel_name}")
        start = getattr(manager, "start_channel", None) or manager.restart_channel
        await start(channel_name)
        return {
            "status": "started",
            "channel": channel_name,
            "safe_mode": tracker.snapshot() if tracker is not None else {"active": False},
        }

    results: dict[str, bool] = {}
    if safe_mode_was_active and tracker is not None:
        # The resume hook starts the channels (and cron + heartbeat).
        await tracker.exit_safe_mode(reason="channels.start")
        if manager is not None:
            errors_fn = getattr(manager, "start_errors", None)
            errors = errors_fn() if callable(errors_fn) else {}
            results = {name: name not in errors for name, _adapter in manager.items()}
    elif manager is not None:
        start_stopped = getattr(manager, "start_stopped", None)
        results = await start_stopped() if callable(start_stopped) else {}
    return {
        "status": "started",
        "channels": results,
        "safe_mode_cleared": safe_mode_was_active,
        "safe_mode": tracker.snapshot() if tracker is not None else {"active": False},
    }


@_d.method("channels.restart")
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
