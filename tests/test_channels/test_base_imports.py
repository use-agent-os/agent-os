"""Smoke tests for the supported built-in channel surface."""

from __future__ import annotations

import importlib
import importlib.util

import pytest

from agentos.channels.contract import PUBLIC_VENDOR_ADAPTERS
from agentos.channels.registry import discover_channel_names

RETIRED_ADAPTERS = {"dingtalk", "matrix", "qq", "wecom"}


@pytest.mark.parametrize("adapter_name", PUBLIC_VENDOR_ADAPTERS)
def test_public_vendor_adapter_module_importable(adapter_name: str) -> None:
    importlib.import_module(f"agentos.channels.{adapter_name}")


def test_retired_builtin_adapter_modules_are_absent() -> None:
    assert RETIRED_ADAPTERS.isdisjoint(discover_channel_names())
    for adapter_name in RETIRED_ADAPTERS:
        assert importlib.util.find_spec(f"agentos.channels.{adapter_name}") is None
