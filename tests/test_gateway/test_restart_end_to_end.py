"""``/restart`` from a Telegram topic, end to end through the real pieces.

The channel command registry, the RPC dispatcher, ``gateway.restart`` and a
real ``GatewayRestarter`` run; only the server, the pid lock, the exec and
the channel adapter are fakes. Then a "new boot" reads the request file and
confirms back into the same chat and topic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.channels.command_registry import DEFAULT_COMMAND_REGISTRY, build_channel_rpc_context
from agentos.channels.types import OutgoingMessage
from agentos.gateway.config import GatewayConfig
from agentos.gateway.restart import (
    GatewayRestarter,
    announce_restart_completion,
    restart_request_path,
    set_restarter,
)
from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.rpc import get_dispatcher


class _FakeServer:
    def __init__(self) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(gateway_ready=True))
        self._services = SimpleNamespace(task_runtime=None)
        self.close_calls: list[dict[str, Any]] = []

    async def close(self, reason: str = "shutdown", **kwargs: Any) -> None:
        self.close_calls.append({"reason": reason, **kwargs})


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


class _FakeChannelManager:
    def __init__(self, adapter: _FakeAdapter) -> None:
        self.adapter = adapter

    def resolve_delivery_target(
        self, *, target: str, to: str = "", account_id: str = "", thread_id: str = ""
    ) -> Any:
        assert target == "telegram-main"
        return SimpleNamespace(
            ok=True, adapter=self.adapter, channel_type="telegram", to=to, thread_id=thread_id
        )


def _envelope() -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.CHANNEL,
        source_name="telegram-main",
        agent_id="main",
        session_key="agent:main:telegram:group:-100:topic:7",
        channel_name="telegram-main",
        channel_type="telegram",
        channel_id="-100",
        thread_id="7",
        sender_id="u1",
    )


async def _send(command: str, *, chat_restart: bool) -> OutgoingMessage | None:
    config = GatewayConfig(gateway_control={"chat_restart": chat_restart})
    return await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=_envelope(),
        message_content=command,
        rpc_dispatcher=get_dispatcher(),
        context_factory=lambda envelope: build_channel_rpc_context(envelope, gateway_config=config),
    )


@pytest.fixture
def wired(tmp_path: Path) -> Any:
    server = _FakeServer()
    execs: list[list[str]] = []
    released: list[str] = []
    config = SimpleNamespace(state_dir=str(tmp_path / "state"))

    async def _no_sleep(_seconds: float) -> None:
        return None

    restarter = GatewayRestarter(
        server,
        config,
        pid_lock=SimpleNamespace(release=lambda: released.append("released")),
        exec_fn=lambda _path, argv, _env: execs.append(argv),
        task_factory=asyncio.create_task,
        agent_surface=SimpleNamespace(export_operator_secret_file=lambda: None),
        sleep=_no_sleep,
        supported=True,
    )
    set_restarter(restarter)
    try:
        yield SimpleNamespace(
            server=server, restarter=restarter, execs=execs, released=released, config=config
        )
    finally:
        set_restarter(None)


@pytest.mark.asyncio
async def test_restart_from_a_telegram_topic_round_trip(wired: Any) -> None:
    reply = await _send("/restart", chat_restart=True)

    # The ack goes back into the same topic, before anything is torn down.
    assert reply is not None
    assert reply.content.startswith("Restarting gateway… draining active turns (up to 30s)")
    assert reply.reply_to == "7"
    assert reply.metadata["denied"] is False
    assert wired.server.close_calls == []

    await wired.restarter.task
    assert wired.server.close_calls == [
        {
            "reason": "restart:safe",
            "drain_timeout": 30.0,
            "cancel_in_flight": False,
            "restart_expected_ms": 3000,
        }
    ]
    assert wired.server.app.state.gateway_ready is False

    wired.restarter.exec_if_pending()
    assert wired.released == ["released"]
    (argv,) = wired.execs
    assert argv[1:4] == ["-P", "-m", "agentos.cli.main"]

    stored = json.loads(restart_request_path(wired.config).read_text(encoding="utf-8"))
    assert stored["origin"] == {
        "kind": "channel",
        "sessionKey": "agent:main:telegram:group:-100:topic:7",
        "channelName": "telegram-main",
        "channelType": "telegram",
        "channelId": "-100",
        "threadId": "7",
        "accountId": None,
        "senderId": "u1",
        "connId": None,
    }

    # The next boot: the confirmation lands in the same chat and topic.
    adapter = _FakeAdapter()
    ok = await announce_restart_completion(
        wired.config, channel_manager=_FakeChannelManager(adapter), session_manager=None
    )
    assert ok is True
    (message,) = adapter.sent
    assert message.content.startswith("Gateway restarted (v")
    assert message.reply_to == "7"
    assert message.metadata == {"channel": "-100"}
    assert not restart_request_path(wired.config).exists()


@pytest.mark.asyncio
async def test_restart_force_from_chat(wired: Any) -> None:
    reply = await _send("/restart force", chat_restart=True)

    assert reply is not None
    assert reply.content == "Restarting gateway now (force). I'll confirm here when it's back."
    await wired.restarter.task
    assert wired.server.close_calls[0]["cancel_in_flight"] is True
    assert wired.server.close_calls[0]["drain_timeout"] == 2.0


@pytest.mark.asyncio
async def test_restart_from_chat_is_denied_while_the_toggle_is_off(wired: Any) -> None:
    reply = await _send("/restart", chat_restart=False)

    assert reply is not None
    assert reply.metadata["denied"] is True
    assert reply.content.startswith("/restart denied: Gateway restart from chat is disabled")
    assert "[gateway_control] chat_restart = true" in reply.content
    assert wired.restarter.pending is None
    assert wired.server.close_calls == []


@pytest.mark.asyncio
async def test_second_restart_while_one_is_pending(wired: Any) -> None:
    first = await _send("/restart", chat_restart=True)
    second = await _send("/restart", chat_restart=True)

    assert first is not None and second is not None
    assert second.content == "A gateway restart is already in progress."
    await wired.restarter.task
    assert len(wired.server.close_calls) == 1
