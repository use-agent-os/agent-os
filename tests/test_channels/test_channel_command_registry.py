from __future__ import annotations

import pytest

from agentos.channels.command_registry import DEFAULT_COMMAND_REGISTRY
from agentos.channels.types import IncomingMessage
from agentos.engine.commands import DEFAULT_REGISTRY, Surface
from agentos.gateway.protocol import make_error_res, make_ok_res
from agentos.gateway.routing import build_channel_route_envelope


def _envelope():
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="test")
    return build_channel_route_envelope(
        msg,
        session_key="agent:main:telegram:u1",
        session_prefix="telegram",
        agent_id="main",
    )


async def _dispatch(command: str, payload):
    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            return make_ok_res(req_id, payload)

    return await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=_envelope(),
        message_content=command,
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )


def test_channel_command_names_include_usage_and_registry_words() -> None:
    expected = {
        word.lstrip("/").lower()
        for cmd in DEFAULT_REGISTRY.for_surface(Surface.CHANNEL)
        for word in cmd.words()
    }

    assert "usage" in DEFAULT_COMMAND_REGISTRY.command_names
    assert expected <= DEFAULT_COMMAND_REGISTRY.command_names


@pytest.mark.parametrize(
    ("command", "method", "params"),
    [
        ("/abort", "sessions.abort", {"key": "agent:main:telegram:u1"}),
        ("/auto", "router.hold.clear", {"key": "agent:main:telegram:u1"}),
        *[
            (
                f"/{tier}",
                "router.hold.set",
                {"key": "agent:main:telegram:u1", "tier": tier},
            )
            for tier in ("c0", "c1", "c2", "c3")
        ],
        ("/compact", "sessions.contextCompact", {"key": "agent:main:telegram:u1"}),
        ("/help", "commands.list_for_surface", {"surface": "channel"}),
        (
            "/history",
            "chat.history",
            {"sessionKey": "agent:main:telegram:u1"},
        ),
        ("/memory", "doctor.memory.status", {}),
        ("/model", "models.list", {}),
        ("/new", "sessions.reset", {"key": "agent:main:telegram:u1"}),
        ("/reset", "sessions.reset", {"key": "agent:main:telegram:u1"}),
        ("/skills", "skills.list", {}),
        ("/status", "status", {}),
        ("/usage", "usage.status", {}),
    ],
)
def test_all_native_channel_commands_map_to_real_rpc(command, method, params) -> None:
    match = DEFAULT_COMMAND_REGISTRY.match(_envelope(), command)

    assert match is not None
    _name, matched_method, params_factory = match
    assert matched_method == method
    assert params_factory(_envelope()) == params


@pytest.mark.asyncio
async def test_channel_help_renders_unified_channel_registry() -> None:
    commands = [
        {
            "name": cmd.name,
            "usage": cmd.usage,
            "description": cmd.description,
        }
        for cmd in DEFAULT_REGISTRY.for_surface(Surface.CHANNEL)
    ]

    reply = await _dispatch("/help", {"surface": "channel", "commands": commands})

    assert reply is not None
    assert reply.content.startswith("Available commands:\n")
    assert "/history — Show recent chat history." in reply.content
    assert "/usage — Show gateway aggregate usage." in reply.content
    assert len(reply.content) <= 1900


@pytest.mark.asyncio
async def test_channel_history_requests_ten_messages_and_renders_payload() -> None:
    calls = []

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            calls.append((method, params))
            return make_ok_res(
                req_id,
                {
                    "messages": [
                        {"role": "user", "text": "hello"},
                        {"role": "tool", "text": "internal tool output"},
                        {"role": "assistant", "text": "Hi there"},
                    ],
                    "loaded_count": 3,
                    "has_more": True,
                },
            )

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=_envelope(),
        message_content="/history",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert calls == [("chat.history", {"sessionKey": "agent:main:telegram:u1", "limit": 10})]
    assert reply is not None
    assert reply.content == (
        "Recent history:\nYou: hello\nAgent: Hi there\n… older messages are available."
    )


@pytest.mark.asyncio
async def test_channel_history_handles_empty_and_bounds_long_content() -> None:
    empty = await _dispatch("/history", {"messages": [], "loaded_count": 0})
    long_history = await _dispatch(
        "/history",
        {
            "messages": [
                {"role": "user", "text": f"message {index} " + "x" * 800}
                for index in range(10)
            ],
            "loaded_count": 10,
        },
    )

    assert empty is not None
    assert empty.content == "No chat history yet."
    assert long_history is not None
    assert len(long_history.content) <= 1900


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "payload", "expected"),
    [
        ("/abort", {"aborted": True}, "Turn aborted."),
        ("/abort", {"aborted": False}, "No turn is currently running."),
        ("/auto", {"cleared": True}, "Automatic model routing restored."),
        ("/auto", {"cleared": False}, "Automatic model routing is already active."),
        (
            "/c0",
            {
                "tier": "c0",
                "provider": "ollama",
                "model": "qwen2.5:7b",
                "ttlSeconds": 300,
            },
            "Router pinned to c0: ollama/qwen2.5:7b (expires in 300s).",
        ),
        ("/new", {"reset": True}, "Started a new chat session."),
        ("/reset", {"reset": True}, "Conversation context reset."),
        ("/clear", {"reset": True}, "Conversation context reset."),
    ],
)
async def test_channel_action_commands_render_outcomes(command, payload, expected) -> None:
    reply = await _dispatch(command, payload)

    assert reply is not None
    assert reply.content == expected


@pytest.mark.asyncio
async def test_channel_information_commands_render_payloads() -> None:
    status = await _dispatch(
        "/status",
        {
            "status": "running",
            "version": "2026.7.20",
            "provider": "ollama",
            "uptime_ms": 65_000,
            "active_sessions": 2,
        },
    )
    usage = await _dispatch(
        "/usage",
        {
            "totalSessions": 3,
            "activeSessions": 1,
            "totalInputTokens": 1200,
            "totalOutputTokens": 300,
            "totalTokens": 1500,
            "totalCostUsd": 0.012345,
            "totalCacheReadTokens": 50,
            "totalCacheWriteTokens": 10,
        },
    )
    memory = await _dispatch(
        "/memory",
        {
            "backend": "sqlite",
            "status": "ok",
            "entryCount": 42,
            "sizeBytes": 2048,
            "vecAvailable": True,
            "ftsAvailable": True,
        },
    )

    assert status is not None
    assert "AgentOS 2026.7.20 · running" in status.content
    assert "Provider: ollama" in status.content
    assert usage is not None
    assert "Tokens: 1,500 (1,200 in / 300 out)" in usage.content
    assert "Cost: $0.012345" in usage.content
    assert memory is not None
    assert "Memory: ok · sqlite" in memory.content
    assert "Entries: 42 · Size: 2.0 KiB" in memory.content


@pytest.mark.asyncio
async def test_channel_models_and_skills_are_bounded() -> None:
    models = await _dispatch(
        "/model",
        [
            {"provider": "ollama", "id": f"model-{index}-" + "x" * 200}
            for index in range(30)
        ],
    )
    skills = await _dispatch(
        "/skills",
        {
            "skills": [
                {"name": f"skill-{index}-" + "x" * 200, "status": "ready"}
                for index in range(30)
            ]
        },
    )

    assert models is not None
    assert models.content.startswith("Available models (30):")
    assert "more model" in models.content
    assert len(models.content) <= 1900
    assert skills is not None
    assert skills.content.startswith("Loaded skills (30):")
    assert "more skill" in skills.content
    assert len(skills.content) <= 1900


@pytest.mark.asyncio
async def test_channel_models_and_skills_render_empty_payloads() -> None:
    models = await _dispatch("/model", [])
    skills = await _dispatch("/skills", {"skills": []})

    assert models is not None
    assert models.content == "No models reported by the active provider."
    assert skills is not None
    assert skills.content == "No user-invocable skills are loaded."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "payload"),
    [
        ("/history", None),
        ("/status", []),
        ("/memory", "bad"),
        ("/skills", []),
        ("/model", {}),
        ("/usage", None),
    ],
)
async def test_channel_malformed_success_payload_falls_back_safely(command, payload) -> None:
    reply = await _dispatch(command, payload)

    assert reply is not None
    assert reply.content == f"{command} completed"


@pytest.mark.asyncio
async def test_channel_compact_command_uses_short_context_budget_wording() -> None:
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="/compact")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            return make_ok_res(
                req_id,
                {
                    "key": "agent:main:feishu:u1",
                    "compacted": False,
                    "status": "skipped",
                },
            )

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content="/compact",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert reply is not None
    assert reply.content == "Already within context budget; no compact was applied."
    assert reply.metadata["command"] == "compact"


@pytest.mark.asyncio
async def test_channel_compact_command_reports_failure_shortly() -> None:
    msg = IncomingMessage(sender_id="u1", channel_id="c1", content="/compact")
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:u1",
        session_prefix="feishu",
        agent_id="main",
    )

    class FakeDispatcher:
        async def dispatch(self, req_id, method, params, ctx):
            return make_error_res(req_id, "INTERNAL_ERROR", "provider down")

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=envelope,
        message_content="/compact",
        rpc_dispatcher=FakeDispatcher(),
        context_factory=lambda _envelope: object(),
    )

    assert reply is not None
    assert reply.content == "Compact failed: provider down"
    assert reply.metadata["command"] == "compact"
