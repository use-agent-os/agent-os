from __future__ import annotations

import ast
from pathlib import Path

from agentos.tools.policy import ToolSurfaceCapabilities
from agentos.tools.types import (
    CallerKind,
    InteractionMode,
    RegisteredTool,
    ToolContext,
    ToolSpec,
)
from agentos.tools.visibility import (
    ToolProfile,
    effective_tool_context,
    resolve_profile,
    visible_registered_tools,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "src/agentos/tools/registry.py"
VISIBILITY = ROOT / "src/agentos/tools/visibility.py"


async def _handler() -> str:
    return "ok"


def _registered_tool(
    name: str,
    *,
    exposed_by_default: bool = True,
) -> RegisteredTool:
    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=f"{name} tool",
            parameters={},
            exposed_by_default=exposed_by_default,
        ),
        handler=_handler,
    )


def _imports_from(path: Path) -> set[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports.add((node.module, alias.name))
    return imports


def _top_level_classes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _top_level_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _top_level_assignments(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_registry_delegates_visibility_policy_to_tools_visibility_boundary() -> None:
    imports = _imports_from(REGISTRY)

    assert ("agentos.tools", "visibility") in imports

    registry_classes = _top_level_classes(REGISTRY)
    registry_functions = _top_level_functions(REGISTRY)
    registry_assignments = _top_level_assignments(REGISTRY)
    assert "ToolProfile" not in registry_classes
    assert "resolve_profile" not in registry_functions
    assert {"ToolProfile", "resolve_profile"} <= registry_assignments

    visibility_classes = _top_level_classes(VISIBILITY)
    visibility_functions = _top_level_functions(VISIBILITY)
    assert "ToolProfile" in visibility_classes
    assert "resolve_profile" in visibility_functions
    assert "effective_tool_context" in visibility_functions
    assert "visible_registered_tools" in visibility_functions


def test_visibility_boundary_uses_single_configured_profile() -> None:
    channel_ctx = ToolContext(caller_kind=CallerKind.CHANNEL)

    assert resolve_profile(channel_ctx) is ToolProfile.CONFIGURED
    assert list(ToolProfile) == [ToolProfile.CONFIGURED]


def test_visibility_boundary_preserves_context_visibility_rules() -> None:
    tools = [
        _registered_tool("visible"),
        _registered_tool("configured"),
        _registered_tool("hidden", exposed_by_default=False),
    ]
    ctx = ToolContext(
                caller_kind=CallerKind.CHANNEL,
        allowed_tools={"visible", "hidden", "configured"},
        surfaced_tools={"hidden"},
    )

    visible = visible_registered_tools(tools, ctx, sort=True)

    assert [tool.spec.name for tool in visible] == ["configured", "hidden", "visible"]


def test_visibility_boundary_preserves_effective_runtime_contexts() -> None:
    subagent_ctx = effective_tool_context(
        session_key="subagent:worker",
        caller_kind=None,
        interaction_mode=None,
        tool_surface_capabilities=ToolSurfaceCapabilities(session_manager=True),
    )
    canonical_subagent_ctx = effective_tool_context(
        session_key="agent:ops:subagent:run-123",
        caller_kind=None,
        interaction_mode=None,
        tool_surface_capabilities=ToolSurfaceCapabilities(session_manager=True),
    )
    cron_ctx = effective_tool_context(
        session_key="cron:nightly",
        caller_kind=None,
        interaction_mode=None,
        tool_surface_capabilities=ToolSurfaceCapabilities(scheduler=True),
    )
    channel_ctx = effective_tool_context(
        caller_kind=CallerKind.CHANNEL,
        tool_surface_capabilities=ToolSurfaceCapabilities(
            channel_backing=True,
            scheduler=True,
        ),
    )

    assert subagent_ctx.caller_kind is CallerKind.SUBAGENT
    assert subagent_ctx.interaction_mode is InteractionMode.UNATTENDED
    assert "publish_artifact" in subagent_ctx.denied_tools
    assert canonical_subagent_ctx.caller_kind is CallerKind.SUBAGENT
    assert canonical_subagent_ctx.interaction_mode is InteractionMode.UNATTENDED
    assert canonical_subagent_ctx.agent_id == "ops"
    assert "publish_artifact" in canonical_subagent_ctx.denied_tools
    assert cron_ctx.caller_kind is CallerKind.CRON
    assert cron_ctx.allowed_tools is not None
    assert "read_file" in cron_ctx.allowed_tools
    assert "exec_command" in cron_ctx.denied_tools
    assert channel_ctx.caller_kind is CallerKind.CHANNEL
    assert channel_ctx.allowed_tools is None
