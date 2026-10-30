"""Deferred tool loading: tool_search lists the long tail and loads it on demand."""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agentos.tools.builtin  # noqa: F401 - registers the builtin tools
from agentos.engine.context_breakdown import tool_payload_chars
from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import ToolsConfig
from agentos.identity.prompt import assemble_system_prompt
from agentos.identity.types import AgentIdentity, AgentProfile
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tool_boundary import ToolCall, ToolResult
from agentos.tools import deferred
from agentos.tools.builtin import tool_search as tool_search_module
from agentos.tools.deferred import (
    DEFERRED_BUILTIN_TOOLS,
    TOOL_SEARCH_NAME,
    DeferredToolSurface,
    bind_agent_tools,
    deferred_tool_names,
    hydrate_loaded_tools,
    persist_loaded_with,
    prompt_tool_names,
    surface_for_turn,
)
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.registry import ToolRegistry, get_default_registry
from agentos.tools.types import ToolContext, ToolSpec, current_tool_context

SESSION = "agent:main:deferred-test"

#: The default request must carry at least this many fewer tool schemas than
#: the full surface. Without the per-agent memory and skill tools, the bare
#: builtin registry defers 20 (tool_search adds one back).
MIN_SCHEMAS_SAVED = 19

#: Pinned so the CHANGELOG, docs/configuration.md and the tool_search docstring
#: cannot drift from the code: 27 builtins are marked deferred, all of them
#: visible on a stock gateway (builtins + skill + memory tools), whose request
#: goes from 66 to 40 tool schemas.
FLAGGED_BUILTINS = 27
STOCK_DEFERRED = 27
STOCK_FULL_SCHEMAS = 66
STOCK_REQUEST_SCHEMAS = 40


async def _ok(**_kwargs: Any) -> str:
    return "ran"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in ("read_file", "exec_command", "web_search"):
        registry.register(ToolSpec(name=name, description=f"{name} tool.", parameters={}), _ok)
    registry.register(
        ToolSpec(
            name="voice_maker",
            description="Synthesize speech from text. Returns an audio file.",
            parameters={"text": {"type": "string"}},
            required=["text"],
            defer_loading=True,
            execution_timeout_seconds=300,
        ),
        _ok,
    )
    registry.register(
        ToolSpec(
            name="project_admin",
            description="Rename or move projects.",
            parameters={},
            defer_loading=True,
        ),
        _ok,
    )
    search = get_default_registry().get(TOOL_SEARCH_NAME)
    assert search is not None
    registry.register(search.spec, search.handler)
    return registry


def _ctx(**overrides: Any) -> ToolContext:
    return ToolContext(session_key=SESSION, **overrides)


@pytest.fixture(autouse=True)
def _clean_session_state() -> Iterator[None]:
    deferred.forget_session(SESSION)
    yield
    deferred.forget_session(SESSION)


def _names(definitions: list[Any]) -> list[str]:
    return [d.name for d in definitions]


# ---------------------------------------------------------------------------
# What the request carries
# ---------------------------------------------------------------------------


def test_deferred_tools_are_left_out_and_tool_search_lists_them() -> None:
    registry = _registry()

    full = _names(registry.to_tool_definitions(_ctx()))
    request = registry.request_tool_definitions(_ctx())
    names = _names(request)

    assert "voice_maker" in full and "project_admin" in full
    assert TOOL_SEARCH_NAME not in full  # only offered when something is deferred
    assert names == ["exec_command", "read_file", TOOL_SEARCH_NAME, "web_search"]
    search = next(d for d in request if d.name == TOOL_SEARCH_NAME)
    assert "- voice_maker: Synthesize speech from text." in search.description
    assert "- project_admin: Rename or move projects." in search.description


def test_disabling_deferral_sends_every_tool() -> None:
    registry = _registry()

    names = _names(registry.request_tool_definitions(_ctx(), defer_loading=False))

    assert names == _names(registry.to_tool_definitions(_ctx()))
    assert TOOL_SEARCH_NAME not in names
    assert (
        surface_for_turn(
            registry, _ctx(), config=SimpleNamespace(tools=ToolsConfig(defer_loading=False))
        )
        is None
    )


def test_turn_runner_build_tools_honours_the_switch() -> None:
    registry = _registry()

    def build(config: object) -> list[str]:
        runner = TurnRunner(
            provider_selector=None,
            tool_registry=registry,
            session_manager=object(),
            config=config,
        )
        defs, handler = runner._build_tools(_ctx())
        assert handler is not None
        return _names(defs)

    on = build(SimpleNamespace(tools=ToolsConfig()))
    off = build(SimpleNamespace(tools=ToolsConfig(defer_loading=False)))

    assert ToolsConfig().defer_loading is True
    assert "voice_maker" not in on and TOOL_SEARCH_NAME in on
    assert "voice_maker" in off and TOOL_SEARCH_NAME not in off


def test_an_allowlisted_surface_defers_nothing() -> None:
    registry = _registry()
    ctx = _ctx(allowed_tools={"read_file", "voice_maker"})

    assert registry.deferred_tools(ctx) == []
    assert _names(registry.request_tool_definitions(ctx)) == ["read_file", "voice_maker"]


def test_a_surfaced_tool_is_loaded_up_front() -> None:
    registry = _registry()
    ctx = _ctx(surfaced_tools={"voice_maker"})

    assert "voice_maker" in _names(registry.request_tool_definitions(ctx))
    assert [rt.spec.name for rt in registry.deferred_tools(ctx)] == ["project_admin"]


def test_denying_tool_search_turns_deferral_off() -> None:
    registry = _registry()
    ctx = _ctx(denied_tools={TOOL_SEARCH_NAME})

    assert registry.deferred_tools(ctx) == []
    assert "voice_maker" in _names(registry.request_tool_definitions(ctx))


def test_default_request_is_much_smaller_than_the_full_surface() -> None:
    """Measurement: what deferral saves on the stock builtin set."""

    registry = get_default_registry()
    ctx = _ctx()

    full = registry.to_tool_definitions(ctx)
    request = registry.request_tool_definitions(ctx)
    full_chars = sum(tool_payload_chars(d) for d in full)
    request_chars = sum(tool_payload_chars(d) for d in request)

    assert len(full) - len(request) >= MIN_SCHEMAS_SAVED
    # The tool_search catalog is far cheaper than the schemas it stands for.
    assert full_chars - request_chars >= 8_000


def test_builtin_registration_sites_match_the_documented_set() -> None:
    registry = get_default_registry()
    flagged = {rt.spec.name for rt in registry.all_tools() if rt.spec.defer_loading}

    assert flagged == DEFERRED_BUILTIN_TOOLS & set(registry.list_names())
    # Never defer what a typical turn, a prompt section, or a bundled skill
    # needs without a search first.
    for name in (
        "read_file",
        "write_file",
        "edit_file",
        "apply_patch",
        "exec_command",
        "web_search",
        "web_fetch",
        "ask_user",
        "sessions_spawn",
        "skill_view",
        "image_generate",
        "publish_artifact",
        "cron",
        # Moved back after live QA: deferred, the model took the shell instead.
        "create_csv",
        "create_xlsx",
        "create_pdf_report",
        "create_pptx",
        "git_status",
        "git_diff",
        "git_log",
        TOOL_SEARCH_NAME,
    ):
        rt = registry.get(name)
        if rt is not None:
            assert not rt.spec.defer_loading, name


# ---------------------------------------------------------------------------
# Searching and loading
# ---------------------------------------------------------------------------


async def test_search_loads_a_tool_into_the_next_request_and_the_running_agent() -> None:
    registry = _registry()
    surface = DeferredToolSurface.build(registry, _ctx())
    assert surface is not None
    live = surface.tool_definitions()
    by_name = {d.name: d for d in live}
    agent = SimpleNamespace(tool_definitions=live, _tool_definition_by_name=by_name)
    bind_agent_tools(surface.guard(_handler_returning("x")), agent)

    matches = surface.search("speech audio")
    newly = await surface.load(rt.spec.name for rt in matches)

    assert newly == ["voice_maker"]
    # Appended in place: the agent's next provider call carries it.
    assert live[-1].name == "voice_maker"
    assert by_name["voice_maker"].execution_timeout_seconds == 300
    # The next turn rebuilds the same order: always-loaded first, then loads.
    rebuilt = DeferredToolSurface.build(registry, _ctx())
    assert rebuilt is not None
    assert _names(rebuilt.tool_definitions()) == _names(live)


async def test_tool_search_tool_loads_by_name_and_says_so() -> None:
    registry = _registry()
    ctx = _ctx()
    surface = DeferredToolSurface.build(registry, ctx)
    assert surface is not None
    handler = surface.guard(build_tool_handler(registry, ctx))

    result = await handler(
        ToolCall(
            tool_use_id="t1",
            tool_name=TOOL_SEARCH_NAME,
            arguments={"names": ["voice_maker", "read_file"]},
        )
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["loaded"] == ["voice_maker"]
    assert payload["not_deferred"] == ["read_file"]
    assert "now available" in payload["note"]
    assert "voice_maker" in _names(surface.tool_definitions())


async def test_calling_an_unloaded_deferred_tool_says_to_load_it_first() -> None:
    registry = _registry()
    ctx = _ctx()
    surface = DeferredToolSurface.build(registry, ctx)
    assert surface is not None
    handler = surface.guard(build_tool_handler(registry, ctx))
    call = ToolCall(tool_use_id="t2", tool_name="voice_maker", arguments={"text": "hi"})

    blocked = await handler(call)

    envelope = json.loads(blocked.content)
    assert blocked.is_error
    assert envelope["error_class"] == "ToolNotLoaded"
    assert 'tool_search(names=["voice_maker"])' in envelope["user_message"]
    assert "not found" not in envelope["user_message"].lower()

    await surface.load(["voice_maker"])
    ran = await handler(call)
    assert not ran.is_error
    assert "ran" in ran.content


async def test_a_policy_forbidden_tool_cannot_be_listed_or_loaded() -> None:
    registry = _registry()
    ctx = _ctx(denied_tools={"voice_maker"})
    surface = DeferredToolSurface.build(registry, ctx)
    assert surface is not None

    assert "voice_maker" not in surface.deferred
    search = next(d for d in surface.tool_definitions() if d.name == TOOL_SEARCH_NAME)
    assert "voice_maker" not in search.description
    assert surface.search("speech", ["voice_maker"]) == []
    assert await surface.load(["voice_maker"]) == []
    assert "voice_maker" not in _names(surface.tool_definitions())

    handler = surface.guard(build_tool_handler(registry, ctx))
    result = await handler(
        ToolCall(tool_use_id="t3", tool_name=TOOL_SEARCH_NAME, arguments={"names": ["voice_maker"]})
    )
    assert json.loads(result.content)["loaded"] == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_loaded_tools_survive_a_reload_and_a_reset_clears_them() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage)
        registry = _registry()
        ctx = _ctx(artifact_session_id="sid-1")
        surface = surface_for_turn(registry, ctx, session_manager=manager)
        assert surface is not None
        await surface.load(["project_admin"])
        await surface.load(["voice_maker"])

        # A gateway restart: nothing in memory, only the session store.
        deferred.forget_session(SESSION)
        assert await hydrate_loaded_tools(manager, SESSION) == ["project_admin", "voice_maker"]
        reloaded = surface_for_turn(registry, ctx, session_manager=manager)
        assert reloaded is not None
        assert _names(reloaded.tool_definitions())[-2:] == ["project_admin", "voice_maker"]

        # "New chat" invalidates every context state of the session.
        await manager.invalidate_context_states(SESSION, reason="session_reset")
        assert await hydrate_loaded_tools(manager, SESSION) == []
        fresh = surface_for_turn(registry, ctx, session_manager=manager)
        assert fresh is not None
        assert "voice_maker" not in _names(fresh.tool_definitions())
    finally:
        await storage.close()


async def test_persist_callback_needs_a_store() -> None:
    assert persist_loaded_with(object(), SESSION, None) is None


# ---------------------------------------------------------------------------
# Prompt and skills see deferred tools as available
# ---------------------------------------------------------------------------


def test_prompt_lists_only_loaded_tools_but_keeps_tool_dependent_sections() -> None:
    registry = get_default_registry()
    surface = DeferredToolSurface.build(registry, _ctx())
    assert surface is not None
    request = surface.tool_definitions()

    tools, deferred_names = prompt_tool_names(request, SESSION)
    profile = AgentProfile(agent_id="main", identity=AgentIdentity(name="Test"))
    prompt = assemble_system_prompt(profile, tools=tools, deferred_tools=deferred_names)
    listing, load_first = (
        prompt.split("## Available Tools", 1)[1]
        .split("\n## ", 1)[0]
        .split("### Tools that must be loaded first", 1)
    )

    assert "- read_file" in listing
    assert "- tts" not in listing
    assert "- tts: " in load_first
    assert "`tool_search(" in load_first
    assert set(deferred_names) <= set(tools)
    assert deferred_tool_names(SESSION) == list(deferred_names)

    # Without deferral the prompt is exactly what it was.
    plain = assemble_system_prompt(profile, tools=["read_file", "tts"])
    assert plain == assemble_system_prompt(profile, tools=["read_file", "tts"], deferred_tools=[])
    assert "tool_search" not in plain


async def test_skill_view_loads_the_deferred_tools_a_skill_requires() -> None:
    from agentos.tools.builtin.skill_tools import _load_required_tools_note

    registry = _registry()
    ctx = _ctx()
    surface = DeferredToolSurface.build(registry, ctx)
    assert surface is not None
    skill = SimpleNamespace(requires_tools=["voice_maker", "read_file"])

    token = current_tool_context.set(ctx)
    try:
        note = await _load_required_tools_note(skill)
        again = await _load_required_tools_note(skill)
    finally:
        current_tool_context.reset(token)

    assert note.startswith("[Just loaded the tools this skill uses: voice_maker.")
    assert "prefer calling them over running this skill's scripts with exec_command" in note
    assert again == ""
    assert surface.loaded == ["voice_maker"]


def _handler_returning(content: str) -> Any:
    async def _handler(call: ToolCall) -> ToolResult:
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=content)

    return _handler


# ---------------------------------------------------------------------------
# Pinned numbers, concurrency, disabled tools, `agentos context`
# ---------------------------------------------------------------------------

_STOCK_MEASURE = """
import json
from unittest.mock import MagicMock
import agentos.tools.builtin  # noqa: F401
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.builtin.skill_tools import create_skill_tools
from agentos.tools.registry import get_default_registry
from agentos.tools.types import ToolContext
create_skill_tools(MagicMock())
create_memory_tools(MagicMock(), MagicMock())
reg = get_default_registry()
ctx = ToolContext(session_key="agent:main:main")
print("MEASURE " + json.dumps({
    "deferred": len(reg.deferred_tools(ctx)),
    "full": len(reg.to_tool_definitions(ctx)),
    "request": len(reg.request_tool_definitions(ctx)),
    "concurrency": {
        name: [reg.get(name).spec.concurrency, reg.get(name).spec.read_only]
        for name in ("tool_search", "skill_view")
    },
}))
"""


@functools.cache
def _stock_measure() -> dict[str, Any]:
    """A stock gateway surface, built in a subprocess so the per-agent memory
    and skill tools it registers do not leak into this test session."""

    out = subprocess.run(
        [sys.executable, "-c", _STOCK_MEASURE],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    line = next(row for row in out.splitlines() if row.startswith("MEASURE "))
    return dict(json.loads(line[len("MEASURE ") :]))


def test_the_documented_numbers_match_a_stock_gateway_surface() -> None:
    measured = _stock_measure()

    assert len(DEFERRED_BUILTIN_TOOLS) == FLAGGED_BUILTINS
    assert (measured["deferred"], measured["full"], measured["request"]) == (
        STOCK_DEFERRED,
        STOCK_FULL_SCHEMAS,
        STOCK_REQUEST_SCHEMAS,
    ), "update CHANGELOG.md, docs/configuration.md and the tool_search docstring"

    root = Path(__file__).resolve().parents[2]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = changelog[changelog.index("- Deferred tool loading.") :].split("\n- ", 1)[0]
    flat = " ".join(entry.split())
    assert f"{FLAGGED_BUILTINS} rarely used builtins" in flat
    assert f"from {STOCK_FULL_SCHEMAS} to {STOCK_REQUEST_SCHEMAS} tool schemas" in flat
    docs = " ".join((root / "docs" / "configuration.md").read_text(encoding="utf-8").split())
    assert f"{FLAGGED_BUILTINS} builtins" in docs
    assert f"{STOCK_REQUEST_SCHEMAS} tool schemas instead of {STOCK_FULL_SCHEMAS}" in docs
    search_doc = " ".join((tool_search_module.__doc__ or "").split())
    assert f"stands in for {STOCK_DEFERRED} tools" in search_doc
    assert f"from {STOCK_FULL_SCHEMAS} to {STOCK_REQUEST_SCHEMAS} tool schemas" in search_doc


def test_tools_that_change_the_loaded_set_run_alone() -> None:
    """A deferred tool batched after tool_search / skill_view must see the load."""

    from agentos.engine.tool_batch import resolve_tool_concurrency

    assert _stock_measure()["concurrency"] == {
        TOOL_SEARCH_NAME: ["exclusive", False],
        "skill_view": ["exclusive", False],
    }
    search = get_default_registry().get(TOOL_SEARCH_NAME)
    assert search is not None
    assert resolve_tool_concurrency(search.spec, {}).mode == "exclusive"


def test_disabled_tools_clear_a_stale_surface() -> None:
    registry = _registry()
    assert DeferredToolSurface.build(registry, _ctx()) is not None
    assert deferred_tool_names(SESSION)

    runner = TurnRunner(
        provider_selector=None,
        tool_registry=registry,
        session_manager=object(),
        config=SimpleNamespace(tools=ToolsConfig(enabled=False)),
    )
    defs, handler = runner._build_tools(_ctx())

    assert defs == [] and handler is None
    assert deferred_tool_names(SESSION) == []
    tools, deferred_names = prompt_tool_names(defs, SESSION)
    assert (tools, deferred_names) == ([], {})


def test_agentos_context_honours_defer_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from agentos.cli import context_cmd

    def tool_count(value: str) -> int:
        monkeypatch.setenv("AGENTOS_GATEWAY_TOOLS__DEFER_LOADING", value)
        result = CliRunner().invoke(context_cmd.app, ["--json"])
        assert result.exit_code == 0, result.output
        # Debug logs share stdout under the test runner; the JSON is the last line.
        json_line = [row for row in result.output.splitlines() if row.startswith("{")][-1]
        payload = json.loads(json_line)
        counts[value] = (payload["defer_loading"], payload["deferred_tools"])
        return next(s["items"] for s in payload["sections"] if s["name"] == "tools")

    counts: dict[str, tuple[bool, int]] = {}
    ctx = ToolContext()
    registry = get_default_registry()
    assert tool_count("false") == len(registry.request_tool_definitions(ctx, defer_loading=False))
    assert tool_count("true") == len(registry.request_tool_definitions(ctx))
    assert tool_count("false") > tool_count("true")
    # --json carries the deferred count the text output prints.
    assert counts["false"] == (False, 0)
    assert counts["true"] == (True, len(registry.deferred_tools(ctx)))
    assert counts["true"][1] > 0


def test_rendered_prompt_lists_every_deferred_tool_with_the_load_instruction() -> None:
    """Live QA: a name only inside tool_search's description was not enough."""

    registry = get_default_registry()
    surface = DeferredToolSurface.build(registry, _ctx())
    assert surface is not None
    tools, deferred_map = prompt_tool_names(surface.tool_definitions(), SESSION)
    profile = AgentProfile(agent_id="main", identity=AgentIdentity(name="Test"))
    prompt = assemble_system_prompt(profile, tools=tools, deferred_tools=deferred_map)

    section = prompt.split("### Tools that must be loaded first", 1)[1].split("\n## ", 1)[0]
    assert 'call `tool_search(names=["<name>"])` once, then call the tool' in section
    assert "Prefer a matching tool here over the shell or a skill script." in section
    assert set(deferred_map) == set(surface.deferred)
    for name, rt in surface.deferred.items():
        line = f"- {name}: {deferred.summarize_description(rt.spec.description)}"
        assert line in section, name
    # One line per tool, no schemas.
    assert '"properties"' not in section
    assert len(section) < 4_000

    search = next(d for d in surface.tool_definitions() if d.name == TOOL_SEARCH_NAME)
    assert search.description.startswith("Before using the shell or a skill script")
    for name in surface.deferred:
        assert f"- {name}" in search.description
