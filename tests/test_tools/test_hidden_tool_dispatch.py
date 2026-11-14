"""AOS-18: a tool hidden with ``exposed_by_default=False`` runs only when offered.

Visibility used to shape only the schema the model saw. Dispatch looked the
tool up by exact name and the policy chain denied only on a non-None
``allowed_tools``, so on the default surface a model that named
``install_skill_deps`` (or ``git_commit``, ``env_set``, ``acp`` ...) ran it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

import agentos.tools.builtin  # noqa: F401 - registers the builtin tools
from agentos.gateway.config import GatewayConfig
from agentos.tool_boundary import ToolCall
from agentos.tools import deferred
from agentos.tools.deferred import TOOL_SEARCH_NAME, DeferredToolSurface
from agentos.tools.dispatch import build_tool_handler, preflight_tool_call
from agentos.tools.policy import apply_tool_policy_from_config
from agentos.tools.registry import ToolRegistry, get_default_registry
from agentos.tools.types import ToolContext, ToolSpec
from agentos.tools.visibility import effective_tool_context

SESSION = "agent:main:hidden-dispatch"

_ran: list[dict[str, Any]] = []


async def _record(**kwargs: Any) -> str:
    _ran.append(kwargs)
    return "ran"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="read_file", description="Read.", parameters={}), _record)
    registry.register(
        ToolSpec(
            name="install_skill_deps",
            description="Install a skill's dependencies on the host.",
            parameters={"confirmed": {"type": "boolean"}},
            exposed_by_default=False,
        ),
        _record,
    )
    registry.register(
        ToolSpec(
            name="voice_maker",
            description="Synthesize speech.",
            parameters={},
            defer_loading=True,
        ),
        _record,
    )
    search = get_default_registry().get(TOOL_SEARCH_NAME)
    assert search is not None
    registry.register(search.spec, search.handler)
    return registry


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    _ran.clear()
    deferred.forget_session(SESSION)
    yield
    deferred.forget_session(SESSION)


def _default_ctx(registry: ToolRegistry) -> ToolContext:
    # The same steps a turn uses to build its context (runtime._build_tools).
    ctx = effective_tool_context(session_key=SESSION, caller_kind="agent")
    return apply_tool_policy_from_config(
        ctx, available_tools=registry.list_names(), config=GatewayConfig()
    )


def _error_class(content: str) -> str | None:
    return json.loads(content).get("error_class")


@pytest.mark.asyncio
async def test_hidden_tool_called_by_exact_name_is_refused() -> None:
    registry = _registry()
    ctx = _default_ctx(registry)
    assert ctx.allowed_tools is None

    result = await build_tool_handler(registry, ctx)(
        ToolCall(tool_use_id="t1", tool_name="install_skill_deps", arguments={"confirmed": True})
    )

    assert result.is_error is True
    assert _error_class(result.content) == "PolicyDenied"
    assert _ran == []


@pytest.mark.asyncio
async def test_hidden_tool_runs_when_the_context_allows_or_surfaces_it() -> None:
    registry = _registry()
    call = ToolCall(tool_use_id="t2", tool_name="install_skill_deps", arguments={})

    allowed = ToolContext(session_key=SESSION, allowed_tools={"install_skill_deps"})
    surfaced = ToolContext(session_key=SESSION, surfaced_tools={"install_skill_deps"})
    for ctx in (allowed, surfaced):
        result = await build_tool_handler(registry, ctx)(call)
        assert result.is_error is False, result.content

    assert len(_ran) == 2


@pytest.mark.asyncio
async def test_tool_search_is_admitted_only_with_a_deferred_surface() -> None:
    registry = _registry()
    ctx = ToolContext(session_key=SESSION)
    call = ToolCall(
        tool_use_id="t3", tool_name=TOOL_SEARCH_NAME, arguments={"names": ["voice_maker"]}
    )

    without = await build_tool_handler(registry, ctx)(call)
    assert _error_class(without.content) == "PolicyDenied"

    surface = DeferredToolSurface.build(registry, ctx)
    assert surface is not None
    with_surface = await surface.guard(build_tool_handler(registry, ctx))(call)
    assert with_surface.is_error is False, with_surface.content
    assert json.loads(with_surface.content)["loaded"] == ["voice_maker"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("git_commit", {"message": "x"}),
        ("env_set", {"name": "X", "value": "1"}),
        ("acp", {"action": "agents"}),
        ("gateway", {"action": "status"}),
    ],
)
async def test_builtin_hidden_tools_are_refused_on_the_default_surface(
    name: str, arguments: dict[str, Any]
) -> None:
    registry = get_default_registry()
    ctx = _default_ctx(registry)

    denial = await preflight_tool_call(
        registry=registry,
        ctx=ctx,
        tool_call=ToolCall(tool_use_id="t4", tool_name=name, arguments=arguments),
    )

    assert denial is not None
    assert _error_class(denial.content) == "PolicyDenied"
