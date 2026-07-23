from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import agentos.tool_boundary as tool_boundary
from agentos.engine.types import ToolCall
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.registry import get_default_registry
from agentos.tools.types import CallerKind, ToolContext

REMOVED_TOOL_NAMES = {"generate_image", "spawn_subagent", "send_message"}
CANONICAL_TOOL_NAMES = {"image_generate", "sessions_spawn", "sessions_send"}
OWNER_ONLY_TOOL_NAMES = {"http_request", "git_commit"}


def test_tool_call_boundary_has_canonical_and_stable_exports() -> None:
    from agentos.engine import ToolHandler as EngineToolHandler
    from agentos.engine.types import ToolResult as EngineToolResult
    from agentos.tools.boundary import ToolCall as ToolsToolCall

    assert tool_boundary.ToolCall is ToolCall
    assert tool_boundary.ToolResult is EngineToolResult
    assert ToolsToolCall is ToolCall
    assert EngineToolHandler is tool_boundary.AgentToolHandler


def test_engine_types_import_does_not_register_builtin_tools() -> None:
    script = (
        "import sys; "
        "import agentos.engine.types; "
        "assert 'agentos.tools.builtin' not in sys.modules, "
        "sorted(k for k in sys.modules if k.startswith('agentos.tools'))"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0, result.stdout


def test_default_registry_public_surface_uses_canonical_tool_names() -> None:
    import agentos.tools.builtin  # noqa: F401

    registry = get_default_registry()
    owner_names = {
        tool.name
        for tool in registry.to_tool_definitions(
            ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
        )
    }
    channel_names = {
        tool.name
        for tool in registry.to_tool_definitions(
            ToolContext(is_owner=False, caller_kind=CallerKind.CHANNEL)
        )
    }

    assert REMOVED_TOOL_NAMES.isdisjoint(owner_names)
    assert REMOVED_TOOL_NAMES.isdisjoint(channel_names)
    assert CANONICAL_TOOL_NAMES <= owner_names
    assert OWNER_ONLY_TOOL_NAMES <= owner_names
    assert OWNER_ONLY_TOOL_NAMES.isdisjoint(channel_names)


async def test_removed_tools_are_not_dispatchable_by_name() -> None:
    import agentos.tools.builtin  # noqa: F401

    handler = build_tool_handler(
        get_default_registry(),
        ToolContext(is_owner=True, caller_kind=CallerKind.AGENT),
    )

    for name in REMOVED_TOOL_NAMES:
        result = await handler(ToolCall(tool_use_id=f"tc-{name}", tool_name=name, arguments={}))
        assert result.is_error is True
        assert '"error_class": "ToolNotFound"' in result.content


def test_web_ui_tool_icon_map_avoids_removed_wrapper_tools() -> None:
    source = Path("frontend/src/views/chat/transcript/tools.ts").read_text(encoding="utf-8")
    start = source.index("const TOOL_ICONS:")
    end = source.index("export function toolIconName", start)
    tool_display_map = source[start:end]

    for name in REMOVED_TOOL_NAMES:
        assert name not in tool_display_map
    assert "http_request" in tool_display_map
