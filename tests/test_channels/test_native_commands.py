from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.slack import SlackChannel
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.engine import native_commands
from agentos.engine.native_commands import (
    DISCORD_COMMAND_NAME_LIMIT,
    DISCORD_DESCRIPTION_LIMIT,
    SLACK_COMMAND_NAME_LIMIT,
    SLACK_DESCRIPTION_LIMIT,
    TELEGRAM_COMMAND_LIMIT,
    TELEGRAM_COMMAND_NAME_LIMIT,
    TELEGRAM_DESCRIPTION_LIMIT,
    discord_application_commands,
    slack_command_manifest,
    telegram_bot_commands,
)


def test_native_command_payloads_are_derived_from_channel_registry() -> None:
    telegram = telegram_bot_commands()
    discord = discord_application_commands()

    assert {item["command"] for item in telegram} == {item["name"] for item in discord}
    assert {item["command"] for item in telegram} >= {"help", "new", "status"}
    assert all("/" not in item["command"] for item in telegram)
    assert all(item["command"].islower() for item in telegram)
    assert all(len(item["command"]) <= TELEGRAM_COMMAND_NAME_LIMIT for item in telegram)
    assert all(len(item["name"]) <= DISCORD_COMMAND_NAME_LIMIT for item in discord)
    assert all(item["type"] == 1 for item in discord)
    assert len(telegram) <= TELEGRAM_COMMAND_LIMIT
    assert all(len(item["description"]) <= TELEGRAM_DESCRIPTION_LIMIT for item in telegram)
    assert all(len(item["description"]) <= DISCORD_DESCRIPTION_LIMIT for item in discord)


def test_native_channel_commands_normalize_name_and_bound_length(monkeypatch) -> None:
    from agentos.engine.commands import (
        CommandDef,
        CommandExecution,
        ExecutionKind,
        SlashCommandRegistry,
        Surface,
    )

    custom_registry = SlashCommandRegistry(
        (
            CommandDef(
                name="/Custom_LONG_Command_Name_Exceeding_Thirty_Two_Characters",
                usage="/custom",
                description="A command with uppercase and >32 chars",
                execution={
                    Surface.CHANNEL: CommandExecution(
                        kind=ExecutionKind.LOCAL, action="custom"
                    )
                },
            ),
        )
    )
    monkeypatch.setattr(native_commands, "DEFAULT_REGISTRY", custom_registry)

    telegram = telegram_bot_commands()
    discord = discord_application_commands()
    assert len(telegram) == 1
    assert telegram[0]["command"] == "custom_long_command_name_exceedi"
    assert len(telegram[0]["command"]) == TELEGRAM_COMMAND_NAME_LIMIT
    assert telegram[0]["command"].islower()
    assert discord[0]["name"] == "custom_long_command_name_exceedi"
    assert len(discord[0]["name"]) == DISCORD_COMMAND_NAME_LIMIT


def test_telegram_command_payload_honors_platform_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        native_commands,
        "_channel_commands",
        lambda: tuple((f"command{index}", "description") for index in range(101)),
    )

    assert len(telegram_bot_commands()) == TELEGRAM_COMMAND_LIMIT


def test_slack_manifest_uses_same_commands_and_request_url() -> None:
    manifest = slack_command_manifest("https://example.test/slack/events")
    commands = manifest["features"]["slash_commands"]

    assert {item["command"].removeprefix("/") for item in commands} == {
        item["command"] for item in telegram_bot_commands()
    }
    assert all(item["url"] == "https://example.test/slack/events" for item in commands)
    assert all(item["should_escape"] is False for item in commands)
    assert all(
        len(item["command"].removeprefix("/")) <= SLACK_COMMAND_NAME_LIMIT
        for item in commands
    )
    assert all(len(item["description"]) <= SLACK_DESCRIPTION_LIMIT for item in commands)


def test_slack_manifest_requires_a_request_url() -> None:
    with pytest.raises(ValueError, match="request URL"):
        slack_command_manifest("")


@pytest.mark.asyncio
async def test_telegram_registration_pushes_the_unified_command_menu() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> None:
        calls.append((method, payload))

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001
    await channel.register_slash_commands()

    assert calls == [
        (
            "setMyCommands",
            {"commands": telegram_bot_commands(), "scope": {"type": "default"}},
        )
    ]


@pytest.mark.asyncio
async def test_telegram_start_and_stop_preserves_registered_commands() -> None:
    channel = TelegramChannel(
        TelegramChannelConfig(
            token="token",
            transport_name="webhook",
            webhook_url="https://example.test/telegram",
            webhook_secret_token="secret",
        )
    )
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        calls.append((method, payload))
        return {"id": 1, "username": "agentos"} if method == "getMe" else True

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001

    await channel.start()
    await channel.stop()

    methods = [method for method, _ in calls]
    assert methods == ["getMe", "setMyCommands", "setWebhook"]
    assert "deleteMyCommands" not in methods


@pytest.mark.asyncio
async def test_telegram_start_survives_command_registration_failure() -> None:
    from agentos.channels.telegram import TelegramApiError

    channel = TelegramChannel(
        TelegramChannelConfig(
            token="token",
            transport_name="webhook",
            webhook_url="https://example.test/telegram",
            webhook_secret_token="secret",
        )
    )
    calls: list[str] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        calls.append(method)
        if method == "getMe":
            return {"id": 1}
        if method == "setMyCommands":
            raise TelegramApiError("rate limited")
        return True

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001

    await channel.start()

    assert (await channel.health_check()).connected is True
    assert calls == ["getMe", "setMyCommands", "setWebhook"]
    await channel.stop()


@pytest.mark.asyncio
async def test_discord_registration_pushes_the_unified_command_menu() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app-id"))
    calls: list[list[dict[str, Any]]] = []

    async def capture(commands: list[dict[str, Any]]) -> None:
        calls.append(commands)

    channel.register_slash_commands = capture  # type: ignore[method-assign]
    await channel.register_native_slash_commands()

    assert calls == [discord_application_commands()]


@pytest.mark.asyncio
async def test_discord_missing_application_id_logs_setup_hint() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    with capture_logs() as logs:
        await channel.register_native_slash_commands()

    event = next(log for log in logs if log["event"] == "discord.commands_not_registered")
    assert event["config_key"] == "application_id"
    assert "Discord Application ID" in event["setup_hint"]


def _stub_discord_gateway_start(channel: DiscordChannel) -> None:
    frames = iter(
        [
            {"op": 10, "d": {"heartbeat_interval": 45_000}},
            {"t": "READY", "d": {"user": {"id": "bot"}}},
        ]
    )

    async def connect(_url: str) -> object:
        return object()

    async def receive() -> dict[str, Any]:
        return next(frames)

    async def no_op(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def idle() -> None:
        await asyncio.Event().wait()

    channel._connect_ws = connect  # type: ignore[method-assign]  # noqa: SLF001
    channel._ws_recv = receive  # type: ignore[method-assign]  # noqa: SLF001
    channel._identify = no_op  # type: ignore[method-assign]  # noqa: SLF001
    channel._handle_dispatch = no_op  # type: ignore[method-assign]  # noqa: SLF001
    channel._close_ws = no_op  # type: ignore[method-assign]  # noqa: SLF001
    channel._heartbeat_loop = idle  # type: ignore[method-assign]  # noqa: SLF001
    channel._dispatch_loop = idle  # type: ignore[method-assign]  # noqa: SLF001


@pytest.mark.asyncio
async def test_discord_start_invokes_native_command_registration() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app-id"))
    _stub_discord_gateway_start(channel)
    registered = False

    async def register() -> None:
        nonlocal registered
        registered = True

    channel.register_native_slash_commands = register  # type: ignore[method-assign]

    await channel.start()
    await channel.stop()

    assert registered is True


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(httpx.HTTPError("REST request failed"), id="http-error"),
        pytest.param(RuntimeError("retry_request exhausted"), id="rate-limit-exhausted"),
    ],
)
@pytest.mark.asyncio
async def test_discord_start_survives_native_command_registration_failure(
    error: Exception,
) -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app-id"))
    _stub_discord_gateway_start(channel)

    async def register() -> None:
        raise error

    channel.register_native_slash_commands = register  # type: ignore[method-assign]

    with capture_logs() as logs:
        await channel.start()

    try:
        assert (await channel.health_check()).connected is True
        event = next(log for log in logs if log["event"] == "discord.commands_not_registered")
        assert event["reason"] == "sync_failed"
        assert event["error"] == str(error)
    finally:
        await channel.stop()


class _SlackResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class _SlackManifestClient:
    def __init__(self, *, export_ok: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, str] | None]] = []
        self.export_ok = export_ok

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _SlackResponse:
        self.calls.append((path, json, headers))
        if path == "/auth.test":
            return _SlackResponse({"ok": True, "user_id": "UBOT"})
        if path == "/apps.manifest.export":
            if not self.export_ok:
                return _SlackResponse({"ok": False, "error": "token_expired"})
            return _SlackResponse(
                {
                    "ok": True,
                    "manifest": {
                        "display_information": {"name": "AgentOS"},
                        "features": {"bot_user": {"display_name": "AgentOS"}},
                    },
                }
            )
        return _SlackResponse({"ok": True, "app_id": "A1"})


@pytest.mark.asyncio
async def test_slack_start_merges_and_updates_native_commands() -> None:
    channel = SlackChannel(
        token="xoxb-test",
        slack_channel_id="C1",
        app_id="A1",
        manifest_token="xoxe.xoxp-test",
        command_request_url="https://example.test/slack/events",
    )
    client = _SlackManifestClient()
    channel._get_client = lambda: client  # type: ignore[method-assign]  # noqa: SLF001

    await channel.start()

    assert [call[0] for call in client.calls] == [
        "/auth.test",
        "/apps.manifest.export",
        "/apps.manifest.update",
    ]
    update_payload = client.calls[-1][1]
    assert update_payload is not None
    manifest = json.loads(update_payload["manifest"])
    assert manifest["display_information"] == {"name": "AgentOS"}
    assert (
        manifest["features"]["slash_commands"]
        == slack_command_manifest("https://example.test/slack/events")["features"]["slash_commands"]
    )
    assert client.calls[-1][2] == {"Authorization": "Bearer xoxe.xoxp-test"}
    await channel.stop()


@pytest.mark.asyncio
async def test_slack_start_survives_manifest_sync_failure() -> None:
    channel = SlackChannel(
        token="xoxb-test",
        slack_channel_id="C1",
        app_id="A1",
        manifest_token="expired-token",
        command_request_url="https://example.test/slack/events",
    )
    client = _SlackManifestClient(export_ok=False)
    channel._get_client = lambda: client  # type: ignore[method-assign]  # noqa: SLF001

    await channel.start()

    assert channel.is_connected() is True
    assert [call[0] for call in client.calls] == ["/auth.test", "/apps.manifest.export"]
    await channel.stop()


_SLASH_SIGNING_SECRET = "s3cr3t"


class _SlashRequest:
    def __init__(
        self, channel_id: str = "C1", *, signing_secret: str = _SLASH_SIGNING_SECRET
    ) -> None:
        self.channel_id = channel_id
        timestamp = str(int(time.time()))
        digest = hmac.HMAC(
            signing_secret.encode(),
            f"v0:{timestamp}:command=%2Fstatus".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.headers = {
            "content-type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": f"v0={digest}",
        }

    async def body(self) -> bytes:
        return b"command=%2Fstatus"

    async def form(self) -> dict[str, str]:
        return {
            "command": "/status",
            "text": "",
            "user_id": "U1",
            "channel_id": self.channel_id,
            "channel_name": "general",
        }


@pytest.mark.parametrize(
    ("channel_id", "channel_type", "is_group"),
    [
        ("C1", "channel", True),
        ("G1", "group", True),
        ("D1", "im", False),
    ],
)
@pytest.mark.asyncio
async def test_slack_slash_command_form_uses_channel_id_conversation_type(
    channel_id: str,
    channel_type: str,
    is_group: bool,
) -> None:
    channel = SlackChannel(
        token="token", slack_channel_id="C1", signing_secret=_SLASH_SIGNING_SECRET
    )

    response = await channel._handle_webhook(_SlashRequest(channel_id))  # noqa: SLF001

    assert response.status_code == 200
    message = await channel.receive()
    assert message.content == "/status"
    assert message.sender_id == "U1"
    assert message.channel_id == channel_id
    assert message.metadata["channel_type"] == channel_type
    assert message.metadata["is_group"] is is_group
    assert message.metadata["interaction_type"] == "slash_command"
