"""Plain-mode tool disabling for local and constrained providers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import ToolsConfig


def test_tools_config_can_disable_all_model_tools() -> None:
    tools = ToolsConfig(enabled=False)

    assert tools.enabled is False


def test_turn_runner_skips_tool_registry_when_tools_are_disabled() -> None:
    runner = object.__new__(TurnRunner)
    runner._config = SimpleNamespace(tools=ToolsConfig(enabled=False))
    runner._tool_registry = MagicMock()
    metadata: dict[str, object] = {}

    definitions, handler = runner._build_tools(None, metadata=metadata)

    assert definitions == []
    assert handler is None
    assert metadata == {"tool_profile": "disabled"}
    runner._tool_registry.list_names.assert_not_called()
