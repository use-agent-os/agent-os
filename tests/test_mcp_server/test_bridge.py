from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agentos.mcp_server import bridge as bridge_module
from agentos.mcp_server.bridge import AgentOSMCPBridge


class FakeGatewayClient:
    def __init__(self) -> None:
        self.connected_url: str | None = None
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self, url: str) -> None:
        self.connected_url = url

    async def close(self) -> None:
        self.closed = True

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        self.calls.append(("sessions.list", {"limit": limit}))
        return {"sessions": [{"key": "agent:main:main", "entry_count": 2}], "count": 1}

    async def resolve_session(self, key: str) -> dict[str, Any]:
        self.calls.append(("sessions.resolve", {"key": key}))
        return {"key": key, "session_id": "sid-1", "agent_id": "main"}

    async def session_history(self, session_key: str, limit: int = 1000) -> dict[str, Any]:
        self.calls.append(("chat.history", {"sessionKey": session_key, "limit": limit}))
        return {
            "messages": [
                {
                    "id": "m1",
                    "role": "user",
                    "text": "hello",
                    "timestamp": 1,
                },
                {
                    "id": "m2",
                    "role": "assistant",
                    "text": "looked it up",
                    "timestamp": 2,
                    "tool_calls": [
                        {
                            "type": "tool_use",
                            "tool_use_id": "tool-1",
                            "name": "lookup",
                            "input": {"q": "hello"},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "name": "lookup",
                            "result": "world",
                            "is_error": False,
                            "execution_status": {
                                "version": 1,
                                "status": "success",
                                "exit_code": 0,
                                "timed_out": False,
                                "truncated": False,
                                "reason": None,
                                "source": "adapter",
                                "preservation_class": "normal",
                            },
                        },
                    ],
                },
            ]
        }

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params))
        if method == "sessions.messages.subscribe":
            return {
                "subscribed": True,
                "key": params["key"],
                "current_stream_seq": 7,
                "replay_complete": True,
            }
        if method == "sessions.send":
            return {"status": "accepted", "key": params["key"], "task_id": "task-1"}
        raise AssertionError(f"unexpected method: {method}")

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self.events.get()
        return await asyncio.wait_for(self.events.get(), timeout=timeout)


@pytest.mark.asyncio
async def test_bridge_reuses_gateway_read_rpcs() -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(
        gateway_url="ws://127.0.0.1:18791/ws",
        gateway_client_factory=lambda: client,
    )

    sessions = await bridge.conversations_list(limit=10)
    resolved = await bridge.session_resolve("agent:main:main")
    messages = await bridge.messages_read("agent:main:main", limit=5)

    assert client.connected_url == "ws://127.0.0.1:18791/ws"
    assert sessions["sessions"][0]["key"] == "agent:main:main"
    assert resolved["session_id"] == "sid-1"
    assert messages["messages"][0]["text"] == "hello"
    assert client.calls[:3] == [
        ("sessions.list", {"limit": 10}),
        ("sessions.resolve", {"key": "agent:main:main"}),
        ("chat.history", {"sessionKey": "agent:main:main", "limit": 5}),
    ]


@pytest.mark.asyncio
async def test_transcript_jsonl_preserves_tool_result_execution_status() -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    transcript = await bridge.transcript_jsonl("agent:main:main", limit=5)

    rows = [json.loads(line) for line in transcript.splitlines()]
    tool_result_message = rows[2]["message"]
    assert tool_result_message["isError"] is False
    assert tool_result_message["executionStatus"]["status"] == "success"


@pytest.mark.asyncio
async def test_messages_send_subscribes_before_accepting_turn() -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.messages_send("agent:main:main", "continue", intent="continue")

    assert result == {
        "status": "accepted",
        "key": "agent:main:main",
        "task_id": "task-1",
        "current_stream_seq": 7,
        "replay_complete": True,
        "replay_gap_reason": None,
    }
    assert client.closed is True
    assert client.calls == [
        ("sessions.messages.subscribe", {"key": "agent:main:main", "since_stream_seq": None}),
        (
            "sessions.send",
            {
                "key": "agent:main:main",
                "message": "continue",
                "attachments": [],
                "intent": "continue",
                "_source": {
                    "caller_kind": "cli",
                    "channel_kind": "cli",
                    "channel_id": "mcp:bridge",
                    "source_kind": "mcp",
                    "source_name": "mcp_server",
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_events_wait_returns_session_events_until_terminal() -> None:
    client = FakeGatewayClient()
    await client.events.put(
        {
            "event": "session.event.text_delta",
            "payload": {"session_key": "agent:main:main", "stream_seq": 8, "text": "hi"},
        }
    )
    await client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 9, "reason": "stop"},
        }
    )
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.events_wait("agent:main:main", since_stream_seq=7, timeout_ms=1000)

    assert result["current_stream_seq"] == 9
    assert [event["event"] for event in result["events"]] == [
        "session.event.text_delta",
        "session.event.done",
    ]


@pytest.mark.asyncio
async def test_transcript_jsonl_exports_standard_tool_evidence() -> None:
    bridge = AgentOSMCPBridge(gateway_client_factory=FakeGatewayClient)

    text = await bridge.transcript_jsonl("agent:main:main")
    rows = [json.loads(line) for line in text.splitlines()]

    assert rows[0]["message"]["role"] == "user"
    assert rows[1]["message"]["role"] == "assistant"
    assert rows[1]["message"]["content"][0]["type"] == "toolCall"
    assert rows[1]["message"]["content"][0]["name"] == "lookup"
    assert rows[2]["message"]["role"] == "toolResult"
    assert rows[2]["message"]["toolCallId"] == "tool-1"


def test_mcp_server_package_does_not_import_cli_layer() -> None:
    package_root = Path("src/agentos/mcp_server")
    imported_modules: set[str] = set()
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    assert not any(module.startswith("agentos.cli") for module in imported_modules)
    assert not any(
        module == "agentos.gateway" or module.startswith("agentos.gateway.")
        for module in imported_modules
    )


@pytest.mark.asyncio
async def test_events_wait_uses_dedicated_connection_and_closes_it() -> None:
    clients: list[FakeGatewayClient] = []
    event_client = FakeGatewayClient()

    def factory() -> FakeGatewayClient:
        if len(clients) == 1:
            clients.append(event_client)
            return event_client
        client = FakeGatewayClient()
        clients.append(client)
        return client

    bridge = AgentOSMCPBridge(gateway_client_factory=factory)
    await bridge.conversations_list()
    await clients[0].events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 3},
        }
    )
    await event_client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 8},
        }
    )

    result = await bridge.events_wait("agent:main:main", timeout_ms=1000)

    assert len(clients) == 2
    assert result["current_stream_seq"] == 8
    assert clients[0].closed is False
    assert event_client.closed is True


class RecordingEventClient(FakeGatewayClient):
    """Fake client that records the timeout handed to each ``recv_event`` call."""

    def __init__(self, *, event: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.recv_timeouts: list[float | None] = []
        self._event = event or {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 1},
        }

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        self.recv_timeouts.append(timeout)
        return dict(self._event)


class EndlessEventClient(FakeGatewayClient):
    """Fake client that yields non-terminal events forever."""

    def __init__(self) -> None:
        super().__init__()
        self.recv_count = 0

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        self.recv_count += 1
        return {
            "event": "session.event.text_delta",
            "payload": {
                "session_key": "agent:main:main",
                "stream_seq": self.recv_count,
                "text": "x",
            },
        }


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(10**9, bridge_module._MAX_SESSION_LIST_LIMIT), (0, 1), (-5, 1), (10, 10)],
)
@pytest.mark.asyncio
async def test_conversations_list_clamps_limit(requested: int, expected: int) -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.conversations_list(limit=requested)

    assert client.calls == [("sessions.list", {"limit": expected})]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(10**9, bridge_module._MAX_HISTORY_LIMIT), (0, 1), (-5, 1), (25, 25)],
)
@pytest.mark.asyncio
async def test_messages_read_clamps_limit(requested: int, expected: int) -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.messages_read("agent:main:main", limit=requested)

    assert client.calls == [("chat.history", {"sessionKey": "agent:main:main", "limit": expected})]


@pytest.mark.asyncio
async def test_transcript_jsonl_clamps_limit_through_messages_read() -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.transcript_jsonl("agent:main:main", limit=10**9)

    assert client.calls == [
        (
            "chat.history",
            {"sessionKey": "agent:main:main", "limit": bridge_module._MAX_HISTORY_LIMIT},
        )
    ]


@pytest.mark.asyncio
async def test_events_wait_clamps_effective_deadline() -> None:
    client = RecordingEventClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.events_wait("agent:main:main", timeout_ms=3_600_000)

    assert client.recv_timeouts
    first_timeout = client.recv_timeouts[0]
    assert first_timeout is not None
    # The deadline must come from the clamped timeout, not the requested hour.
    assert first_timeout <= bridge_module._MAX_EVENTS_WAIT_TIMEOUT_MS / 1000


@pytest.mark.asyncio
async def test_events_wait_clamps_deadline_on_a_coarse_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows' monotonic clock has ~15ms granularity, so two reads inside the
    # same tick return the same float and ``deadline - now`` reduces to
    # ``(t + cap) - t``, which rounds above ``cap`` for many values of ``t``.
    frozen = 32554.749911971794
    assert (frozen + 300.0) - frozen > 300.0
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: frozen)

    client = RecordingEventClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.events_wait("agent:main:main", timeout_ms=3_600_000)

    first_timeout = client.recv_timeouts[0]
    assert first_timeout is not None
    assert first_timeout <= bridge_module._MAX_EVENTS_WAIT_TIMEOUT_MS / 1000


@pytest.mark.asyncio
async def test_events_wait_preserves_timeout_below_the_cap() -> None:
    client = RecordingEventClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.events_wait("agent:main:main", timeout_ms=2_000)

    first_timeout = client.recv_timeouts[0]
    assert first_timeout is not None
    assert 1.0 < first_timeout <= 2.0


@pytest.mark.asyncio
async def test_events_wait_clamps_max_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "_MAX_EVENTS_WAIT_EVENTS", 3)
    client = EndlessEventClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    # Keep the deadline short: if the clamp regresses, the loop honours 10**9
    # against an endless client, and the test should fail fast rather than
    # allocate events until the runner dies.
    result = await bridge.events_wait("agent:main:main", timeout_ms=200, max_events=10**9)

    assert len(result["events"]) == 3


@pytest.mark.asyncio
async def test_events_wait_preserves_max_events_below_the_cap() -> None:
    client = EndlessEventClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.events_wait("agent:main:main", timeout_ms=200, max_events=2)

    assert len(result["events"]) == 2
    # Regression for #1798: hitting the max_events cap is a successful call,
    # not a timeout -- the collected events arrived well within the budget.
    assert result["timed_out"] is False


class TimeoutRaisingEventClient(FakeGatewayClient):
    """Fake client whose recv_event always raises TimeoutError immediately."""

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        raise TimeoutError


@pytest.mark.asyncio
async def test_events_wait_reports_timed_out_on_genuine_deadline_expiry() -> None:
    """A real timeout -- recv_event exhausting its deadline with no terminal
    event seen -- must still report timed_out: True."""
    client = TimeoutRaisingEventClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.events_wait("agent:main:main", timeout_ms=50, max_events=5)

    assert result["events"] == []
    assert result["timed_out"] is True


@pytest.mark.asyncio
async def test_events_wait_terminal_event_is_not_a_timeout() -> None:
    client = RecordingEventClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.events_wait("agent:main:main", timeout_ms=2_000)

    assert result["timed_out"] is False
