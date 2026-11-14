"""AOS-01: Discord, Slack and MS Teams admit only operator-admitted senders.

Before the fix these adapters declared an open ``ChannelAccessPolicy`` with no
way to narrow it, so any user who could DM or @mention the bot started an
agent turn (with the operator's shell), ran slash commands, and pressed
approval buttons. Admission is now ``allowed_user_ids`` and/or the Telegram
pairing flow, and with neither configured the channel fails closed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agentos.channel_pairing import ChannelPairingStore
from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from agentos.channels.slack import SlackChannel
from agentos.channels.types import IncomingMessage, OutgoingMessage
from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.gateway.channel_dispatch import _should_skip_unmentioned, run_channel_dispatch
from agentos.gateway.routing import build_channel_route_envelope

STRANGER = "999000111"
OPERATOR = "111000222"


@pytest.fixture(autouse=True)
def clean_approval_queue():
    reset_approval_queue()
    yield
    reset_approval_queue()


def _discord(store: ChannelPairingStore, **config: Any) -> DiscordChannel:
    return DiscordChannel(DiscordChannelConfig(token="dummy-token", **config), pairing_store=store)


def _slack(store: ChannelPairingStore, **kwargs: Any) -> SlackChannel:
    return SlackChannel(token="xoxb-test", slack_channel_id="", pairing_store=store, **kwargs)


def _teams(store: ChannelPairingStore, **config: Any) -> MSTeamsChannel:
    return MSTeamsChannel(MSTeamsChannelConfig(**config), pairing_store=store)


_BUILDERS = {
    "discord": lambda store, **kw: _discord(store, **kw),
    "slack": lambda store, **kw: _slack(store, **kw),
    "msteams": lambda store, **kw: _teams(store, **kw),
}


def _dm(sender_id: str = STRANGER, *, content: str = "hi", **metadata: Any) -> IncomingMessage:
    return IncomingMessage(
        sender_id=sender_id,
        channel_id="dm-555",
        content=content,
        metadata={"is_group": False, **metadata},
    )


@pytest.fixture
def store(tmp_path) -> ChannelPairingStore:
    return ChannelPairingStore(tmp_path / "pairing")


# -- the audit reproduction ---------------------------------------------------


def test_audit_repro_stranger_dm_is_skipped_with_stock_constructors() -> None:
    """Verbatim AOS-01 steps 1-3, using only constructors that predate the fix."""
    from agentos.channels._util import evaluate_policy

    channel = DiscordChannel(DiscordChannelConfig(token="dummy-token"))
    message = IncomingMessage(sender_id=STRANGER, channel_id="dm-555", content="hi")
    session_key = f"agent:main:discord:direct:{STRANGER}"
    envelope = build_channel_route_envelope(
        message, session_key=session_key, session_prefix="discord"
    )

    assert _should_skip_unmentioned(channel, message, session_key, envelope) is True
    assert (
        evaluate_policy(channel.policy, is_group=True, mentioned=True, sender_id=STRANGER).admit
        is False
    )
    slack = SlackChannel(token="xoxb-test", slack_channel_id="C1")
    assert (
        evaluate_policy(slack.policy, is_group=False, mentioned=False, sender_id=STRANGER).admit
        is False
    )


@pytest.mark.parametrize("kind", sorted(_BUILDERS))
def test_default_config_refuses_stranger_dm_and_group_mention(kind, store) -> None:
    """Port of the AOS-01 repro: a stranger's DM and @mention are refused."""
    channel = _BUILDERS[kind](store)
    message = _dm()
    session_key = f"agent:main:{kind}:direct:{STRANGER}"
    envelope = build_channel_route_envelope(message, session_key=session_key, session_prefix=kind)

    assert _should_skip_unmentioned(channel, message, session_key, envelope) is True
    assert message.metadata["access_denial_reason"] == "not_in_allowlist"
    group = channel.evaluate_access(message, is_group=True, mentioned=True)
    assert group.admit is False
    assert group.reason == "not_in_allowlist"
    # Code that still reads ``policy`` directly is strict too.
    assert channel.policy.allowlist_enabled is True
    assert channel.policy.allowlist == frozenset()


@pytest.mark.asyncio
async def test_stranger_dm_never_reaches_the_turn_runner(store) -> None:
    channel = _discord(store)
    channel.enqueue(_dm())
    sent: list[OutgoingMessage] = []
    channel.send = AsyncMock(side_effect=sent.append)  # type: ignore[method-assign]

    class TurnRunner:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"turn runner touched for a refused sender: {name}")

    async def run() -> None:
        await run_channel_dispatch(
            channel,
            turn_runner=TurnRunner(),
            session_manager=None,
            session_key_builder=lambda msg: f"agent:main:discord:direct:{msg.sender_id}",
            session_prefix="agent:main:discord",
        )

    task = asyncio.create_task(run())
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done(), task.exception() if task.done() else None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Closed mode stays silent: a stranger cannot make the bot talk.
    assert sent == []


# -- allowlist ------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(_BUILDERS))
def test_allowlisted_sender_is_admitted_and_others_are_not(kind, store) -> None:
    if kind == "slack":
        channel = _slack(store, allowed_user_ids=[OPERATOR])
    else:
        channel = _BUILDERS[kind](store, allowed_user_ids=f" {OPERATOR} ,")

    assert channel.evaluate_access(_dm(OPERATOR), is_group=False, mentioned=False).admit
    assert channel.evaluate_access(_dm(OPERATOR), is_group=True, mentioned=True).admit
    unmentioned = channel.evaluate_access(_dm(OPERATOR), is_group=True, mentioned=False)
    assert unmentioned.reason == "not_mentioned_in_group"
    assert not channel.evaluate_access(_dm(STRANGER), is_group=False, mentioned=False).admit
    assert not channel.evaluate_access(_dm(STRANGER), is_group=True, mentioned=True).admit
    # Adapters substitute "unknown" for a missing sender; it never matches.
    assert not channel.evaluate_access(_dm("unknown"), is_group=False, mentioned=False).admit


# -- pairing --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_pairing_flow_issues_code_and_admits_after_approval(store) -> None:
    channel = _discord(store, name="dc", pairing_enabled=True)
    sent: list[OutgoingMessage] = []
    channel.send = AsyncMock(side_effect=sent.append)  # type: ignore[method-assign]
    message = _dm()

    assert _should_skip_unmentioned(channel, message, f"agent:main:dc:direct:{STRANGER}")
    assert message.metadata["access_denial_reason"] == "not_paired"
    assert message.metadata["pairing_request_created"] is True
    code = message.metadata["pairing_code"]
    await channel.notify_access_denied(message)
    assert len(sent) == 1
    assert code in sent[0].content
    assert f"agentos channels pairing approve dc {code}" in sent[0].content

    # A group message from the unpaired user is refused without a code.
    group_msg = _dm(is_group=True)
    assert not channel.evaluate_access(group_msg, is_group=True, mentioned=True).admit

    channel.resolve_access_request(STRANGER, approved=True)
    envelope = build_channel_route_envelope(
        message, session_key=f"agent:main:dc:direct:{STRANGER}", session_prefix="dc"
    )
    assert _should_skip_unmentioned(channel, message, envelope.session_key, envelope) is False
    context = envelope.tool_context()
    assert context.channel_admission is not None
    assert context.channel_admission_validator is not None
    assert context.channel_admission_validator(context.channel_admission) is True

    channel.revoke_sender(STRANGER)
    assert context.channel_admission_validator(context.channel_admission) is False
    assert channel.evaluate_access(message, is_group=False, mentioned=False).reason == "not_paired"


@pytest.mark.parametrize("kind", ["slack", "msteams"])
def test_slack_and_teams_pairing_opens_a_request(kind, store) -> None:
    channel = _BUILDERS[kind](store, pairing_enabled=True)
    message = _dm()

    assert _should_skip_unmentioned(channel, message, f"agent:main:{kind}:direct:{STRANGER}")
    pending = channel.access_snapshot()["pending"]
    assert [item["sender_id"] for item in pending] == [STRANGER]
    assert channel.access_snapshot()["mode"] == "pairing"


def test_closed_mode_does_not_open_pairing_requests(store) -> None:
    channel = _discord(store)
    _should_skip_unmentioned(channel, _dm(), f"agent:main:discord:direct:{STRANGER}")
    assert channel.access_snapshot()["pending"] == []
    assert channel.access_snapshot()["mode"] == "closed"


# -- slash commands -------------------------------------------------------------


@pytest.mark.asyncio
async def test_stranger_slash_command_is_gated_before_rpc_dispatch(store) -> None:
    channel = _discord(store)
    channel.enqueue(
        _dm(
            content="/reset",
            interaction_type="slash_command",
            interaction_id="int-1",
            interaction_token="tok-1",
            interaction_application_id="app-1",
        )
    )
    sent: list[OutgoingMessage] = []
    channel.send = AsyncMock(side_effect=sent.append)  # type: ignore[method-assign]

    class Dispatcher:
        calls = 0

        async def dispatch(self, *_args: Any, **_kwargs: Any) -> Any:
            Dispatcher.calls += 1
            raise AssertionError("slash command reached RPC dispatch")

    task = asyncio.create_task(
        run_channel_dispatch(
            channel,
            turn_runner=None,
            session_manager=None,
            session_key_builder=lambda msg: f"agent:main:discord:direct:{msg.sender_id}",
            session_prefix="agent:main:discord",
            rpc_dispatcher=Dispatcher(),
            channel_rpc_context_factory=lambda _envelope: object(),
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done(), task.exception() if task.done() else None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert Dispatcher.calls == 0
    # The deferred interaction is answered so the user is not left waiting.
    assert len(sent) == 1
    assert sent[0].metadata["interaction_token"] == "tok-1"
    assert "not authorized" in sent[0].content


# -- approval buttons -----------------------------------------------------------


def _discord_component(approval_id: str, user_id: str) -> dict[str, Any]:
    return {
        "type": 3,
        "id": f"int-{user_id}",
        "token": "token123",
        "channel_id": "dm-555",
        "user": {"id": user_id},
        "message": {"content": "Approval requested"},
        "data": {"custom_id": f"approve:{approval_id}"},
    }


@pytest.mark.asyncio
async def test_discord_approval_button_requires_admission(store) -> None:
    queue = get_approval_queue()
    approval_id = queue.request(
        "exec",
        {
            "argv": ["python3", "-c", "1"],
            "action_kind": "exec",
            "sessionKey": f"agent:main:discord:direct:{OPERATOR}",
        },
    )
    channel = _discord(store, allowed_user_ids=[OPERATOR])
    posts: list[tuple[str, Any]] = []

    async def fake_post(path: str, json: Any = None, **_kwargs: Any) -> Any:
        posts.append((path, json))
        return AsyncMock(raise_for_status=lambda: None)

    channel._get_client = lambda: AsyncMock(post=fake_post)  # type: ignore[method-assign]

    await channel._handle_discord_component_interaction(_discord_component(approval_id, STRANGER))
    assert queue.get(approval_id).resolved is False
    assert "Unauthorized" in posts[-1][1]["data"]["content"]

    await channel._handle_discord_component_interaction(_discord_component(approval_id, OPERATOR))
    assert queue.get(approval_id).resolved is True


@pytest.mark.asyncio
async def test_slack_approval_button_requires_admission(store) -> None:
    queue = get_approval_queue()
    approval_id = queue.request(
        "exec",
        {
            "argv": ["python3", "-c", "1"],
            "action_kind": "exec",
            "sessionKey": "agent:main:slack:channel:C12345",
        },
    )
    channel = _slack(store, allowed_user_ids=[OPERATOR])

    def payload(user_id: str) -> dict[str, Any]:
        return {
            "type": "block_actions",
            "user": {"id": user_id},
            "channel": {"id": "C12345"},
            "message": {"text": "Approve?", "blocks": []},
            "actions": [{"value": f"approve:{approval_id}"}],
        }

    with patch("httpx.AsyncClient.post", new=AsyncMock()):
        await channel._handle_slack_interactive(payload(STRANGER))
        assert queue.get(approval_id).resolved is False
        await channel._handle_slack_interactive(payload(OPERATOR))
    assert queue.get(approval_id).resolved is True


# -- config wiring --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "adapter_ids"),
    [
        (
            {"type": "discord", "name": "dc", "token": "t"},
            lambda ch: ch.config.allowed_user_ids,
        ),
        (
            {"type": "slack", "name": "sl", "token": "xoxb", "signing_secret": "s"},
            lambda ch: ch.allowed_user_ids,
        ),
    ],
)
def test_config_entry_wires_admission_into_the_adapter(raw, adapter_ids) -> None:
    from agentos.channels.registry import build_managed_channel, parse_channel_entry

    closed = build_managed_channel(parse_channel_entry(raw))
    assert closed is not None
    assert closed.access_snapshot()["mode"] == "closed"  # type: ignore[attr-defined]

    entry = parse_channel_entry({**raw, "allowed_user_ids": "U1, U2,U1", "pairing_enabled": True})
    assert entry.allowed_user_ids == ["U1", "U2"]  # type: ignore[attr-defined]
    channel = build_managed_channel(entry)
    assert channel is not None
    assert adapter_ids(channel) == ["U1", "U2"]
    assert channel.access_snapshot()["mode"] == "allowlist+pairing"  # type: ignore[attr-defined]
    assert channel.evaluate_access(  # type: ignore[attr-defined]
        _dm("U2"), is_group=False, mentioned=False
    ).admit


@pytest.mark.asyncio
async def test_start_warns_when_admission_is_closed(store) -> None:
    channel = _slack(store)
    channel._get_client = lambda: AsyncMock(  # type: ignore[method-assign]
        post=AsyncMock(
            return_value=AsyncMock(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True, "user_id": "UBOT"},
            )
        )
    )
    channel.register_native_slash_commands = AsyncMock()  # type: ignore[method-assign]
    with patch("agentos.channels._admission.log") as log:
        await channel.start()
    assert log.warning.call_args.args[0] == "channel.admission_closed"


def test_slack_fallback_sender_is_never_admitted(store) -> None:
    """parse_event uses ``sender_id`` when an event has no user; never admit it."""
    channel = _slack(store, allowed_user_ids=["slack-user"], pairing_enabled=True)
    message = channel.parse_event({"channel": "D1", "channel_type": "im", "text": "hi"})

    assert message.sender_id == "slack-user"
    assert not channel.evaluate_access(message, is_group=False, mentioned=False).admit
    channel.record_access_denial(message, "not_in_allowlist")
    assert channel.access_snapshot()["pending"] == []
