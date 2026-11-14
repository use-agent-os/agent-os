"""Delegated work never exceeds the authority of the turn that started it.

``sessions_spawn`` children and ``sessions_send`` turns are resolved later for
their *own* agent and session. They must still be clipped to the caller's
tool surface and elevation, and a policy-restricted caller may not reach
another agent unless ``subagents.allow_agents`` names it. The operator's own
console keeps its full reach.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import agentos.tools.builtin  # noqa: F401 - registers the built-in tools
from agentos.gateway.config import GatewayConfig
from agentos.gateway.routing import (
    RouteEnvelope,
    SourceKind,
    build_cron_route_envelope,
    build_web_route_envelope,
    tool_context_from_envelope,
)
from agentos.gateway.task_runtime import TaskRuntime
from agentos.tools.authority import AUTHORITY_METADATA_KEY
from agentos.tools.builtin import sessions as sessions_tool
from agentos.tools.builtin import shell
from agentos.tools.policy import apply_tool_policy_from_config
from agentos.tools.policy_runtime import ToolSurfaceCapabilities, resolve_runtime_tool_surface
from agentos.tools.registry import get_default_registry
from agentos.tools.types import (
    CRON_AGENT_DENY,
    InteractionMode,
    ToolContext,
    ToolError,
    current_tool_context,
)

_HOST_TOOLS = ("exec_command", "background_process", "write_file", "edit_file", "apply_patch")
_RESTRICTED_SUPPORT = {
    "id": "support",
    "tools": {"deny": ["group:runtime", "execute_code", "write_file", "edit_file", "apply_patch"]},
}


def _caps(*, channel_backing: bool) -> ToolSurfaceCapabilities:
    return ToolSurfaceCapabilities(
        session_manager=True,
        task_runtime=True,
        scheduler=True,
        gateway_config=True,
        channel_backing=channel_backing,
    )


def _resolve(
    envelope: RouteEnvelope,
    config: GatewayConfig,
    *,
    channel_backing: bool = False,
    default_elevated: str | None = None,
    cron_default_elevated: str | None = None,
) -> ToolContext:
    """The same layers Runtime._build_tools applies."""
    ctx = tool_context_from_envelope(
        envelope,
        default_elevated=default_elevated,
        cron_default_elevated=cron_default_elevated,
    )
    ctx = apply_tool_policy_from_config(
        ctx, available_tools=get_default_registry().list_names(), config=config
    )
    return resolve_runtime_tool_surface(ctx, capabilities=_caps(channel_backing=channel_backing))


def _offered(ctx: ToolContext, name: str) -> bool:
    return name not in ctx.denied_tools and (ctx.allowed_tools is None or name in ctx.allowed_tools)


def _channel_envelope(agent_id: str, session_key: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.CHANNEL,
        source_name="discord",
        agent_id=agent_id,
        session_key=session_key,
        sender_id="dummy-sender",
        channel_type="discord",
        channel_name="discord",
        channel_id="dm-dummy",
        interaction_mode=InteractionMode.UNATTENDED,
    )


class _SpawnManager:
    has_agent_registry = True

    async def get_agent_config(self, agent_id: str) -> dict | None:
        return {"id": agent_id} if agent_id in ("main", "support", "ops") else None

    async def get_current_session(self) -> None:
        return None

    async def list_sessions(self, **_kwargs: Any) -> list:
        return []

    async def create(self, **_kwargs: Any) -> None:
        return None

    async def append_message(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _CapturingRuntime:
    def __init__(self) -> None:
        self.envelopes: list[RouteEnvelope] = []

    async def enqueue(self, envelope: RouteEnvelope, _task: str, **_kwargs: Any) -> Any:
        self.envelopes.append(envelope)
        return SimpleNamespace(task_id="t-1")


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    def _wire(config: GatewayConfig, manager: Any, runtime: Any) -> None:
        monkeypatch.setattr(sessions_tool, "_session_manager", manager)
        monkeypatch.setattr(sessions_tool, "_task_runtime", runtime)
        monkeypatch.setattr(sessions_tool, "_gateway_config", config)

    return _wire


async def _spawn(ctx: ToolContext, agent_id: str | None) -> None:
    token = current_tool_context.set(ctx)
    try:
        await sessions_tool.sessions_spawn(agent_id=agent_id, task="dummy task")
    finally:
        current_tool_context.reset(token)


# ---------------------------------------------------------------------------
# AOS-14: sessions_spawn
# ---------------------------------------------------------------------------


async def test_restricted_parent_cannot_spawn_another_agent_by_default(wired) -> None:
    config = GatewayConfig.model_validate({"agents": [_RESTRICTED_SUPPORT]})
    runtime = _CapturingRuntime()
    wired(config, _SpawnManager(), runtime)
    parent = _resolve(
        _channel_envelope("support", "agent:support:discord:dm:dummy"),
        config,
        channel_backing=True,
    )
    assert not _offered(parent, "exec_command")
    assert _offered(parent, "sessions_spawn")

    with pytest.raises(ToolError, match="Cross-agent spawn not allowed"):
        await _spawn(parent, "main")
    assert runtime.envelopes == []


async def test_child_of_restricted_parent_is_clipped_to_the_parent_surface(wired) -> None:
    config = GatewayConfig.model_validate(
        {
            "agents": [_RESTRICTED_SUPPORT],
            "agents_defaults": {"subagents": {"allow_agents": ["main"]}},
        }
    )
    runtime = _CapturingRuntime()
    wired(config, _SpawnManager(), runtime)
    parent = _resolve(
        _channel_envelope("support", "agent:support:discord:dm:dummy"),
        config,
        channel_backing=True,
    )

    await _spawn(parent, "main")  # permitted by allow_agents ...

    child_env = runtime.envelopes[0]
    assert AUTHORITY_METADATA_KEY in child_env.metadata
    child = _resolve(child_env, config)
    assert child.agent_id == "main"
    # ... but 'main' (no tool policy) no longer hands the child a shell.
    for name in _HOST_TOOLS:
        assert not _offered(child, name), name


async def test_same_agent_child_of_restricted_parent_stays_restricted(wired) -> None:
    config = GatewayConfig.model_validate({"agents": [_RESTRICTED_SUPPORT]})
    runtime = _CapturingRuntime()
    wired(config, _SpawnManager(), runtime)
    parent = _resolve(
        _channel_envelope("support", "agent:support:discord:dm:dummy"),
        config,
        channel_backing=True,
    )
    await _spawn(parent, None)
    child = _resolve(runtime.envelopes[0], config)
    assert not _offered(child, "exec_command")
    assert not _offered(child, "write_file")


async def test_operator_parent_still_spawns_a_full_child_on_any_agent(wired) -> None:
    """The single-operator path: a Web console spawning 'main' or 'ops'."""
    config = GatewayConfig.model_validate({"agents": [{"id": "main"}, {"id": "ops"}]})
    runtime = _CapturingRuntime()
    wired(config, _SpawnManager(), runtime)
    parent = _resolve(
        build_web_route_envelope(session_key="agent:main:main", conn_id="c1"),
        config,
        default_elevated="bypass",
    )
    assert parent.elevated == "bypass"

    await _spawn(parent, None)
    await _spawn(parent, "ops")

    for envelope in runtime.envelopes:
        child = _resolve(envelope, config)
        for name in _HOST_TOOLS:
            assert _offered(child, name), name


def test_cron_turn_cannot_reach_delegation_tools_even_under_a_global_profile() -> None:
    assert {"sessions_spawn", "sessions_send"} <= CRON_AGENT_DENY
    config = GatewayConfig.model_validate({"tools": {"profile": "coding"}})
    job = SimpleNamespace(
        id="job-dummy", name="dummy", handler_key="agent_run", tool_policy=None, delivery=None
    )
    envelope = build_cron_route_envelope(job, session_key="cron:job-dummy:run:1")
    for elevated in (None, "bypass"):
        ctx = _resolve(envelope, config, cron_default_elevated=elevated)
        assert not _offered(ctx, "sessions_spawn")
        assert not _offered(ctx, "sessions_send")


# ---------------------------------------------------------------------------
# AOS-15: sessions_send
# ---------------------------------------------------------------------------


class _SendManager:
    def __init__(self, agents_config: dict[str, dict] | None = None) -> None:
        self._agents = agents_config or {}

    async def get_session(self, key: str) -> Any:
        return SimpleNamespace(status="idle", session_key=key, spawned_by=None)

    async def get_agent_config(self, agent_id: str) -> dict | None:
        return self._agents.get(agent_id)


def _runtime_capturing_send() -> tuple[TaskRuntime, list[RouteEnvelope]]:
    captured: list[RouteEnvelope] = []
    runtime = TaskRuntime.__new__(TaskRuntime)
    runtime._last_envelope_by_session = {}

    async def _enqueue(envelope: RouteEnvelope, _message: str, **_kwargs: Any) -> Any:
        captured.append(envelope)
        return SimpleNamespace(task_id="t-1")

    runtime.enqueue = _enqueue  # type: ignore[method-assign]
    return runtime, captured


_MESSAGING_SUPPORT = {
    "agents": [{"id": "support", "tools": {"profile": "messaging"}}, {"id": "main"}],
}


def _support_group_caller(config: GatewayConfig) -> ToolContext:
    # DM-shaped key: the group form is additionally scope-confined (AOS-21).
    return _resolve(
        _channel_envelope("support", "agent:support:telegram:direct:dummy"),
        config,
        channel_backing=True,
    )


async def _send(ctx: ToolContext, target: str) -> None:
    token = current_tool_context.set(ctx)
    try:
        await sessions_tool.sessions_send(session_key=target, message="(dummy) list files")
    finally:
        current_tool_context.reset(token)


async def test_restricted_channel_cannot_send_to_another_agent(wired) -> None:
    config = GatewayConfig.model_validate(_MESSAGING_SUPPORT)
    runtime, captured = _runtime_capturing_send()
    wired(config, _SendManager(), runtime)
    caller = _support_group_caller(config)
    assert not _offered(caller, "exec_command")
    assert _offered(caller, "sessions_send")

    with pytest.raises(ToolError, match="Cross-agent send not allowed"):
        await _send(caller, "agent:main:main")
    assert captured == []


async def test_permitted_cross_agent_send_runs_with_the_senders_authority(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = GatewayConfig.model_validate(
        {**_MESSAGING_SUPPORT, "agents_defaults": {"subagents": {"allow_agents": ["main"]}}}
    )
    runtime, captured = _runtime_capturing_send()
    # The target's last route is the operator's elevated web console.
    cached = build_web_route_envelope(session_key="agent:main:main", conn_id="c1")
    cached.metadata["elevated"] = "full"
    runtime._last_envelope_by_session["agent:main:main"] = cached
    wired(config, _SendManager(), runtime)

    await _send(_support_group_caller(config), "agent:main:main")

    queued = captured[0]
    assert "elevated" not in queued.metadata
    assert queued.input_provenance["kind"] == "inter_session"
    target = _resolve(queued, config, default_elevated="bypass")
    assert target.agent_id == "main"
    assert not _offered(target, "exec_command")
    assert not _offered(target, "write_file")
    assert target.elevated is None

    # The shell's fallback to the session's own /elevated mode is capped too.
    class _Queue:
        def get_elevated_mode(self, _key: str | None) -> str:
            return "full"

    monkeypatch.setattr(shell, "get_approval_queue", lambda: _Queue())
    token = current_tool_context.set(target)
    try:
        assert shell._context_elevated_mode() is None
    finally:
        current_tool_context.reset(token)


async def test_operator_send_keeps_target_tools_and_elevation(wired) -> None:
    config = GatewayConfig.model_validate({"agents": [{"id": "main"}, {"id": "ops"}]})
    runtime, captured = _runtime_capturing_send()
    cached = build_web_route_envelope(session_key="agent:ops:main", conn_id="c2")
    runtime._last_envelope_by_session["agent:ops:main"] = cached
    wired(config, _SendManager(), runtime)
    caller = _resolve(
        build_web_route_envelope(session_key="agent:main:main", conn_id="c1"),
        config,
        default_elevated="bypass",
    )

    await _send(caller, "agent:ops:main")

    target = _resolve(captured[0], config, default_elevated="bypass")
    assert target.agent_id == "ops"
    assert target.elevated == "bypass"
    for name in _HOST_TOOLS:
        assert _offered(target, name), name


async def test_malformed_authority_metadata_fails_closed() -> None:
    envelope = build_web_route_envelope(session_key="agent:main:main", conn_id="c1")
    envelope.metadata[AUTHORITY_METADATA_KEY] = {"allowed_tools": "exec_command"}
    ctx = _resolve(envelope, GatewayConfig(), default_elevated="full")
    assert ctx.allowed_tools == set()
    assert ctx.elevated is None


async def test_restricted_caller_cannot_drive_an_acp_harness_session(wired) -> None:
    config = GatewayConfig.model_validate(_MESSAGING_SUPPORT)
    runtime, captured = _runtime_capturing_send()
    wired(config, _SendManager(), runtime)
    with pytest.raises(ToolError, match="cannot drive an ACP harness"):
        await _send(_support_group_caller(config), "agent:support:acp:run1")
    assert captured == []
