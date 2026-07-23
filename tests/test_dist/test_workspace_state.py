from __future__ import annotations

import importlib.util

from agentos.channels.contract import PUBLIC_VENDOR_ADAPTERS
from agentos.dist.workspace_state import BUNDLED_CHANNELS, build_workspace_state


def test_bundled_channels_are_importable_adapter_modules() -> None:
    missing = [
        name
        for name in BUNDLED_CHANNELS
        if importlib.util.find_spec(f"agentos.channels.{name}") is None
    ]

    assert missing == []


def test_workspace_state_does_not_advertise_unshipped_or_retired_channels() -> None:
    state = build_workspace_state()

    assert {
        "dingtalk",
        "matrix",
        "qq",
        "qqbot",
        "wecom",
        "whatsapp",
    }.isdisjoint(state["bundled_channels"])


def test_workspace_state_channel_inventory_matches_public_and_internal_adapters() -> None:
    expected = tuple(sorted((*PUBLIC_VENDOR_ADAPTERS, "terminal", "websocket")))

    assert tuple(sorted(BUNDLED_CHANNELS)) == expected
