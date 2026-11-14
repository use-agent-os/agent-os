"""Contexts denied private-memory reads stay denied across every tool.

Group/channel chats, cron turns and subagents may not read private memory
(``memory_search``, ``memory_get``, ``session_search``). They also may not
*write* the curated store a private session injects as the user's profile,
and ``sessions_history`` / ``sessions_list`` / ``sessions_send`` reach only
the caller's own session and the sessions it spawned. The operator's main
and DM sessions keep every one of these tools.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import MemoryNudgeConfig
from agentos.gateway.routing import build_cron_route_envelope, tool_context_from_envelope
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tool_boundary import ToolCall
from agentos.tools.builtin import sessions as sessions_tool
from agentos.tools.policy.checks import PrivateMemoryScopePolicy
from agentos.tools.policy.types import DispatchInput
from agentos.tools.policy_runtime import ToolSurfaceCapabilities, resolve_runtime_tool_surface
from agentos.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    ToolError,
    current_tool_context,
)

_MEMORY_WRITE_TOOLS = ("memory", "memory_save", "memory_delete")
_PRIVATE_KEY = "agent:main:webchat:default"
_GROUP_KEY = "agent:main:discord:group:room1"
_MARKER = "DUMMY-MARKER-V323 operator private note"


def _group_ctx() -> ToolContext:
    return ToolContext(
        caller_kind=CallerKind.CHANNEL,
        interaction_mode=InteractionMode.UNATTENDED,
        session_key=_GROUP_KEY,
        channel_kind="discord",
        channel_id="room1",
    )


def _cron_ctx() -> ToolContext:
    job = SimpleNamespace(id="job1", name="job1", handler_key="agent_run", tool_policy=None)
    envelope = build_cron_route_envelope(job, session_key="cron:job1")
    return tool_context_from_envelope(envelope, cron_default_elevated=None)


def _subagent_ctx(session_key: str = "agent:main:subagent:abc") -> ToolContext:
    return ToolContext(
        caller_kind=CallerKind.SUBAGENT,
        interaction_mode=InteractionMode.UNATTENDED,
        session_key=session_key,
    )


def _private_ctxs() -> list[ToolContext]:
    return [
        ToolContext(caller_kind=CallerKind.WEB, session_key="agent:main:main"),
        ToolContext(
            caller_kind=CallerKind.CHANNEL,
            interaction_mode=InteractionMode.UNATTENDED,
            session_key="agent:main:discord:direct:owner",
            channel_kind="discord",
            channel_id="owner",
        ),
    ]


def _blocked_ctxs() -> list[ToolContext]:
    return [_group_ctx(), _cron_ctx(), _subagent_ctx()]


def _scope_denied(ctx: ToolContext, tool_name: str) -> bool:
    decision = PrivateMemoryScopePolicy().evaluate(
        DispatchInput(
            tool_call=ToolCall(tool_use_id="t1", tool_name=tool_name, arguments={}),
            ctx=ctx,
            registered=None,  # type: ignore[arg-type]
            known_skill_names=frozenset(),
            registry=None,  # type: ignore[arg-type]
        )
    )
    return not decision.allowed


# ---------------------------------------------------------------------------
# AOS-20: curated memory writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ctx", _blocked_ctxs(), ids=["group", "cron", "subagent"])
def test_blocked_contexts_cannot_write_curated_memory(ctx: ToolContext) -> None:
    surface = resolve_runtime_tool_surface(ctx, capabilities=ToolSurfaceCapabilities())
    for name in _MEMORY_WRITE_TOOLS:
        assert name in surface.denied_tools, name
        # Dispatch-time defense in depth, for a context built without the surface.
        assert _scope_denied(ctx, name), name


@pytest.mark.parametrize("ctx", _private_ctxs(), ids=["web-main", "dm"])
def test_operator_private_sessions_keep_curated_memory(ctx: ToolContext) -> None:
    surface = resolve_runtime_tool_surface(ctx, capabilities=ToolSurfaceCapabilities())
    for name in (*_MEMORY_WRITE_TOOLS, "memory_search"):
        assert name not in surface.denied_tools, name
        assert not _scope_denied(ctx, name), name


class _NudgeRunner:
    def __init__(self) -> None:
        nudge = MemoryNudgeConfig(interval=1)
        self._config = type("C", (), {"memory": type("M", (), {"nudge": nudge})()})()
        self._memory_nudge_counters: dict[tuple[str, str], int] = {}

    _memory_nudge_config = TurnRunner._memory_nudge_config
    _memory_nudge_allowed = TurnRunner._memory_nudge_allowed
    _turn_used_memory_tool = staticmethod(TurnRunner._turn_used_memory_tool)
    _capture_filter_matches = staticmethod(TurnRunner._capture_filter_matches)
    _hydrate_nudge_counter = staticmethod(TurnRunner._hydrate_nudge_counter)
    _evict_nudge_counters_if_needed = TurnRunner._evict_nudge_counters_if_needed
    _note_turn_for_memory_nudge = TurnRunner._note_turn_for_memory_nudge


def _nudge(runner: _NudgeRunner, session_key: str) -> bool:
    return runner._note_turn_for_memory_nudge(
        agent_id="main",
        session_key=session_key,
        input_mode="user",
        run_kind="channel_turn",
        no_memory_capture=False,
        turn_segments=[],
    )


def test_group_chat_turns_never_trigger_a_memory_review() -> None:
    runner = _NudgeRunner()
    assert _nudge(runner, _GROUP_KEY) is False
    assert runner._memory_nudge_counters == {}
    # The operator's own DM still reviews.
    assert _nudge(runner, "agent:main:discord:direct:owner") is True


# ---------------------------------------------------------------------------
# AOS-21: transcript reads and sends
# ---------------------------------------------------------------------------


@pytest.fixture
async def manager(monkeypatch: pytest.MonkeyPatch):
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    await mgr.create(session_key=_PRIVATE_KEY, agent_id="main")
    await mgr.append_message(_PRIVATE_KEY, role="user", content=_MARKER)
    await mgr.create(session_key=_GROUP_KEY, agent_id="main")
    await mgr.append_message(_GROUP_KEY, role="user", content="group chatter")
    await mgr.create(
        session_key="agent:main:subagent:child1",
        agent_id="main",
        spawned_by=_GROUP_KEY,
        parent_session_key=_GROUP_KEY,
        spawn_depth=1,
    )
    await mgr.append_message("agent:main:subagent:child1", role="assistant", content="child done")
    monkeypatch.setattr(sessions_tool, "_session_manager", mgr)
    monkeypatch.setattr(sessions_tool, "_task_runtime", None)
    try:
        yield mgr
    finally:
        await storage.close()


async def _as(ctx: ToolContext, fn: Any, **kwargs: Any) -> Any:
    token = current_tool_context.set(ctx)
    try:
        return json.loads(await fn(**kwargs))
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("ctx", _blocked_ctxs(), ids=["group", "cron", "subagent"])
async def test_blocked_contexts_cannot_read_or_list_operator_transcripts(
    manager, ctx: ToolContext
) -> None:
    with pytest.raises(ToolError, match="Session not found"):
        await _as(ctx, sessions_tool.sessions_history, session_key=_PRIVATE_KEY)
    listed = await _as(ctx, sessions_tool.sessions_list)
    assert _PRIVATE_KEY not in {row["session_key"] for row in listed}
    assert _MARKER not in json.dumps(listed)


async def test_blocked_contexts_cannot_send_into_operator_sessions(manager) -> None:
    for ctx in _blocked_ctxs():
        with pytest.raises(ToolError, match="only reach this session"):
            await _as(ctx, sessions_tool.sessions_send, session_key=_PRIVATE_KEY, message="hi")


async def test_group_context_reads_and_lists_its_own_session_and_children(manager) -> None:
    group = _group_ctx()
    own = await _as(group, sessions_tool.sessions_history, session_key=_GROUP_KEY)
    child = await _as(
        group, sessions_tool.sessions_history, session_key="agent:main:subagent:child1"
    )
    listed = {row["session_key"] for row in await _as(group, sessions_tool.sessions_list)}

    assert own["messages"][0]["content"] == "group chatter"
    assert child["messages"][0]["content"] == "child done"
    assert listed == {_GROUP_KEY, "agent:main:subagent:child1"}


async def test_operator_session_still_reads_every_transcript(manager) -> None:
    web = ToolContext(caller_kind=CallerKind.WEB, session_key="agent:main:main")
    history = await _as(web, sessions_tool.sessions_history, session_key=_PRIVATE_KEY)
    listed = {row["session_key"] for row in await _as(web, sessions_tool.sessions_list)}

    assert history["messages"][0]["content"] == _MARKER
    assert {_PRIVATE_KEY, _GROUP_KEY, "agent:main:subagent:child1"} <= listed
