from __future__ import annotations

import asyncio
import json

import pytest

from agentos.channel_pairing import PAIRING_CODE_LENGTH, ChannelPairingStore
from agentos.channels._util import ChannelAccessPolicy, evaluate_policy
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage
from agentos.gateway.channel_dispatch import _should_skip_unmentioned, run_channel_dispatch
from agentos.gateway.routing import build_channel_route_envelope


def _incoming(channel: TelegramChannel, *, sender_id: int = 42, username: str = "alice"):
    return channel.parse_incoming(
        {
            "update_id": 10,
            "message": {
                "message_id": 7,
                "from": {
                    "id": sender_id,
                    "username": username,
                    "first_name": "Alice",
                    "last_name": "Nguyen",
                },
                "chat": {"id": sender_id, "type": "private"},
                "text": "hello",
            },
        }
    )


def test_unpaired_sender_creates_one_pending_request(tmp_path) -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(name="tg"),
        pairing_store=ChannelPairingStore(tmp_path / "pairing"),
    )
    message = _incoming(channel)

    assert (
        _should_skip_unmentioned(
            channel,
            message,
            "agent:main:telegram:direct:42",
        )
        is True
    )
    assert message.metadata["pairing_request_created"] is True
    pending = channel.access_snapshot()["pending"]
    assert len(pending) == 1
    assert pending[0]["sender_id"] == "42"
    assert pending[0]["username"] == "alice"
    assert pending[0]["display_name"] == "Alice Nguyen"
    assert len(pending[0]["code"]) == PAIRING_CODE_LENGTH

    repeated = _incoming(channel)
    assert (
        _should_skip_unmentioned(
            channel,
            repeated,
            "agent:main:telegram:direct:42",
        )
        is True
    )
    assert repeated.metadata["pairing_request_created"] is False
    assert repeated.metadata["pairing_code"] == pending[0]["code"]
    assert len(channel.access_snapshot()["pending"]) == 1


def test_pairing_grant_is_invalid_immediately_after_disconnect(tmp_path) -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(name="tg"),
        pairing_store=ChannelPairingStore(tmp_path / "pairing"),
    )
    message = _incoming(channel)
    _should_skip_unmentioned(channel, message, "agent:main:telegram:direct:42")

    request = channel.resolve_access_request("42", approved=True)

    assert request["username"] == "alice"
    paired = channel.evaluate_access(message, is_group=False, mentioned=True)
    assert paired.admit is True
    assert paired.admission is not None
    assert paired.admission_validator is not None
    assert paired.admission_validator(paired.admission) is True
    assert channel.revoke_sender("42") == "pairing"
    assert paired.admission_validator(paired.admission) is False
    assert (
        channel.evaluate_access(message, is_group=False, mentioned=True).reason
        == "not_paired"
    )


def test_pairing_runtime_proof_stays_out_of_persisted_route_metadata(tmp_path) -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(name="tg"),
        pairing_store=ChannelPairingStore(tmp_path / "pairing"),
    )
    message = _incoming(channel)
    _should_skip_unmentioned(channel, message, "agent:main:telegram:direct:42")
    channel.resolve_access_request("42", approved=True)
    envelope = build_channel_route_envelope(
        message,
        session_key="agent:main:telegram:direct:42",
        session_prefix="tg",
    )

    assert _should_skip_unmentioned(channel, message, envelope.session_key, envelope) is False
    assert json.loads(json.dumps(envelope.metadata)) == envelope.metadata

    tool_context = envelope.tool_context()
    assert tool_context.channel_admission is not None
    assert tool_context.channel_admission_validator is not None
    assert tool_context.channel_admission_validator(tool_context.channel_admission) is True

    channel.revoke_sender("42")
    assert tool_context.channel_admission_validator(tool_context.channel_admission) is False


def test_direct_messages_always_require_pairing(tmp_path) -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(),
        pairing_store=ChannelPairingStore(tmp_path / "pairing"),
    )
    message = _incoming(channel)

    decision = channel.evaluate_access(message, is_group=False, mentioned=True)

    assert decision.admit is False
    assert decision.reason == "not_paired"


def test_configured_group_requires_pairing_and_mention(tmp_path) -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(
            name="tg",
            groups_enabled=True,
            group_chat_ids=["group-1"],
            group_mention_required=True,
        ),
        pairing_store=ChannelPairingStore(tmp_path / "pairing"),
    )
    direct = _incoming(channel)
    _should_skip_unmentioned(channel, direct, "agent:main:telegram:direct:42")
    channel.resolve_access_request("42", approved=True)
    group = direct.model_copy(
        update={"channel_id": "group-1", "metadata": {"is_group": True}}
    )

    assert channel.evaluate_access(direct, is_group=False, mentioned=True).admit is True
    assert channel.evaluate_access(group, is_group=True, mentioned=False).reason == (
        "not_mentioned_in_group"
    )
    assert channel.evaluate_access(group, is_group=True, mentioned=True).admit is True


@pytest.mark.parametrize(
    ("command", "entity_type", "expected"),
    [
        ("/status", "bot_command", True),
        ("/status@AgentBot", "bot_command", True),
        ("/status@agentbot", "bot_command", True),
        ("/status@OtherBot", "bot_command", False),
        ("/status@AgentBotExtra", "bot_command", False),
        ("/status@AgentBot_2", "bot_command", False),
    ],
)
def test_telegram_group_bot_command_entity_is_mention_aware(
    command: str,
    entity_type: str,
    expected: bool,
) -> None:
    channel = TelegramChannel(TelegramChannelConfig())
    channel.bot_username = "AgentBot"
    message = channel.parse_incoming(
        {
            "message": {
                "message_id": 8,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": -100, "type": "supergroup"},
                "text": command,
                "entities": [{"type": entity_type, "offset": 0, "length": len(command)}],
            }
        }
    )

    assert message.metadata["bot_username"] == "AgentBot"
    assert channel.is_group_mentioned(message) is expected


def test_telegram_group_bot_command_in_media_caption_is_mention_aware() -> None:
    channel = TelegramChannel(TelegramChannelConfig())
    channel.bot_username = "AgentBot"
    command = "/status"
    message = channel.parse_incoming(
        {
            "message": {
                "message_id": 8,
                "from": {"id": 42, "username": "alice"},
                "chat": {"id": -100, "type": "supergroup"},
                "caption": command,
                "caption_entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
                "photo": [
                    {
                        "file_id": "photo-1",
                        "file_unique_id": "unique-1",
                        "width": 100,
                        "height": 100,
                    }
                ],
            }
        }
    )

    assert message.content == command
    assert message.metadata["content_entities"] == message.metadata["caption_entities"]
    assert channel.is_group_mentioned(message) is True


def test_populated_legacy_allowlist_remains_strict_without_explicit_flag() -> None:
    policy = ChannelAccessPolicy(allowlist=frozenset({"allowed"}))

    assert (
        evaluate_policy(
            policy,
            is_group=False,
            mentioned=False,
            sender_id="blocked",
        ).reason
        == "not_in_allowlist"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/status", "/reset"])
async def test_unapproved_sender_is_gated_before_slash_command_dispatch(command: str) -> None:
    message = IncomingMessage(
        sender_id="42",
        channel_id="42",
        content=command,
        metadata={"is_group": False},
    )

    class FakeChannel:
        supports_slash_commands = True
        policy = ChannelAccessPolicy(allowlist_enabled=True)

        def __init__(self) -> None:
            self.received = False
            self.denials: list[str] = []
            self.notified = False

        async def receive(self):
            if not self.received:
                self.received = True
                return message
            raise asyncio.CancelledError

        def record_access_denial(self, _message, reason: str) -> None:
            self.denials.append(reason)

        async def notify_access_denied(self, _message) -> None:
            self.notified = True

    class FakeDispatcher:
        def __init__(self) -> None:
            self.calls = 0

        async def dispatch(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("slash command dispatch must remain gated")

    channel = FakeChannel()
    dispatcher = FakeDispatcher()

    with pytest.raises(asyncio.CancelledError):
        await run_channel_dispatch(
            channel,
            turn_runner=None,
            session_manager=None,
            session_key_builder=lambda _msg: "agent:main:telegram:direct:42",
            session_prefix="agent:main:telegram",
            rpc_dispatcher=dispatcher,
            channel_rpc_context_factory=lambda _envelope: object(),
        )

    assert channel.denials == ["not_in_allowlist"]
    assert channel.notified is True
    assert dispatcher.calls == 0


async def test_handle_telegram_callback_handles_null_data_and_null_message() -> None:
    channel = TelegramChannel(TelegramChannelConfig(name="tg"))

    # Null data (e.g. game or webapp buttons) must exit cleanly without raising AttributeError
    await channel._handle_telegram_callback({"id": "cb-1", "data": None})

    # Non-approval callback with None message (e.g. inline query buttons) must also exit cleanly
    await channel._handle_telegram_callback(
        {"id": "cb-2", "data": "random_payload", "message": None}
    )

