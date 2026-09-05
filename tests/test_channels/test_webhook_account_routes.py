"""Webhook routes must not silently shadow another configured account."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Iterator
from urllib.parse import urlencode

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from agentos.channels.manager import ChannelManager
from agentos.channels.slack import SlackChannel
from agentos.gateway.config import SlackChannelEntry, TelegramChannelEntry

_NOW = 1_700_000_000


def _headers(body: bytes, secret: str, timestamp: int = _NOW) -> dict[str, str]:
    base = b"v0:" + str(timestamp).encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": signature,
    }


@pytest.fixture
def manager() -> Iterator[ChannelManager]:
    entries = [
        SlackChannelEntry.model_validate(
            {
                "name": f"route-test-{account}",
                "token": f"test-token-{account}",
                "signing_secret": f"test-secret-{account}",
                "webhook_path": f"/slack/{account}/events",
            }
        )
        for account in ("first", "second")
    ]
    result = ChannelManager.from_config(entries, turn_runner=None, session_manager=None)
    yield result
    for entry in entries:
        result._unregister_tool_channel(entry.name, result.get(entry.name))


@pytest.mark.parametrize("payload_kind", ["event", "command", "challenge"])
async def test_configured_slack_paths_dispatch_to_their_own_account(
    manager: ChannelManager, monkeypatch: pytest.MonkeyPatch, payload_kind: str
) -> None:
    monkeypatch.setattr("agentos.channels.slack.time.time", lambda: _NOW)
    routes = manager.collect_webhook_routes()
    assert [route.path for route in routes] == ["/slack/first/events", "/slack/second/events"]
    app = Starlette(routes=routes)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        for account in ("first", "second"):
            if payload_kind == "command":
                body = urlencode(
                    {
                        "command": "/help",
                        "text": account,
                        "user_id": "U_TEST",
                        "channel_id": "D_TEST",
                    }
                ).encode()
                content_type = "application/x-www-form-urlencoded"
            else:
                payload = (
                    {"type": "url_verification", "challenge": account}
                    if payload_kind == "challenge"
                    else {
                        "type": "event_callback",
                        "event": {
                            "type": "message",
                            "channel": "D_TEST",
                            "user": "U_TEST",
                            "text": account,
                            "ts": "1.1",
                        },
                    }
                )
                body = json.dumps(payload).encode()
                content_type = "application/json"
            headers = _headers(body, f"test-secret-{account}")
            headers["content-type"] = content_type
            response = await client.post(f"/slack/{account}/events", content=body, headers=headers)
            assert response.status_code == 200
            channel = manager.get(f"route-test-{account}")
            assert isinstance(channel, SlackChannel)
            if payload_kind == "challenge":
                assert response.json() == {"challenge": account}
            else:
                message = channel._queue.get_nowait()
                assert message.content == (
                    f"/help {account}" if payload_kind == "command" else account
                )
            for other in manager._channels.values():
                assert isinstance(other, SlackChannel)
                assert other._queue.empty()


@pytest.mark.parametrize(
    "wrong_secret, timestamp, expected", [(True, _NOW, 401), (False, _NOW - 301, 403)]
)
async def test_custom_slack_route_preserves_signature_and_replay_checks(
    manager: ChannelManager,
    monkeypatch: pytest.MonkeyPatch,
    wrong_secret: bool,
    timestamp: int,
    expected: int,
) -> None:
    monkeypatch.setattr("agentos.channels.slack.time.time", lambda: _NOW)
    body = json.dumps({"type": "url_verification", "challenge": "test"}).encode()
    secret = "test-secret-first" if wrong_secret else "test-secret-second"
    app = Starlette(routes=manager.collect_webhook_routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        response = await client.post(
            "/slack/second/events", content=body, headers=_headers(body, secret, timestamp)
        )
    assert response.status_code == expected
    for channel in manager._channels.values():
        assert isinstance(channel, SlackChannel)
        assert channel._queue.empty()


def test_default_slack_path_and_explicit_override_remain_supported() -> None:
    channel = SlackChannel(token="test-token", slack_channel_id="")
    assert channel.create_webhook_route().path == "/slack/events"
    channel.webhook_path = "/configured/events"
    assert channel.create_webhook_route().path == "/configured/events"
    assert channel.create_webhook_route("/explicit/events").path == "/explicit/events"


async def test_interactive_payload_reaches_only_its_configured_account(
    manager: ChannelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentos.channels.slack.time.time", lambda: _NOW)
    handled: list[tuple[str, dict]] = []

    async def handle_interactive(channel: SlackChannel, payload: dict) -> None:
        handled.append((channel.webhook_path, payload))

    monkeypatch.setattr(SlackChannel, "_handle_slack_interactive", handle_interactive)
    payload = {"type": "block_actions", "actions": []}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    headers = _headers(body, "test-secret-second")
    headers["content-type"] = "application/x-www-form-urlencoded"
    app = Starlette(routes=manager.collect_webhook_routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        response = await client.post("/slack/second/events", content=body, headers=headers)
    await asyncio.sleep(0)
    assert response.status_code == 200
    assert handled == [("/slack/second/events", payload)]


def test_socket_and_polling_entries_do_not_conflict_with_webhooks() -> None:
    entries = [
        SlackChannelEntry(name="webhook", token="test-token"),
        SlackChannelEntry(
            name="socket", token="test-token", connection_mode="socket", app_token="test-app-token"
        ),
        TelegramChannelEntry.model_validate(
            {"name": "polling", "token": "test-token", "webhook_path": "/slack/events"}
        ),
        SlackChannelEntry(name="disabled", token="test-token", enabled=False),
    ]
    manager = ChannelManager.from_config(entries, turn_runner=None, session_manager=None)
    try:
        assert [route.path for route in manager.collect_webhook_routes()] == ["/slack/events"]
    finally:
        for entry in entries:
            if entry.enabled:
                manager._unregister_tool_channel(entry.name, manager.get(entry.name))


@pytest.mark.parametrize("channel_type", ["slack", "telegram"])
def test_duplicate_webhook_paths_are_rejected(channel_type: str) -> None:
    if channel_type == "slack":
        entries = [
            SlackChannelEntry(name=name, token="test-token", webhook_path="/slack/events")
            for name in ("one", "two")
        ]
    else:
        entries = [
            TelegramChannelEntry(
                name=name,
                token="test-token",
                transport_name="webhook",
                webhook_url="https://example.test/telegram/events",
                webhook_secret_token="test-secret",
            )
            for name in ("one", "two")
        ]
    manager = ChannelManager.from_config(entries, turn_runner=None, session_manager=None)
    try:
        with pytest.raises(ValueError, match=r"one.*two.*webhook_path"):
            manager.collect_webhook_routes()
    finally:
        for entry in entries:
            manager._unregister_tool_channel(entry.name, manager.get(entry.name))


def test_same_path_with_disjoint_methods_is_not_a_collision() -> None:
    class WebhookChannel:
        def __init__(self, method: str) -> None:
            self.method = method

        def create_webhook_route(self) -> Route:
            return Route("/events", lambda request: Response(), methods=[self.method])

    manager = ChannelManager(
        {"get": WebhookChannel("GET"), "post": WebhookChannel("POST")}, None, None
    )
    assert len(manager.collect_webhook_routes()) == 2


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("roundtrip", [False, True])
async def test_automatic_slack_routes_are_account_named_and_survive_config_reload(
    monkeypatch: pytest.MonkeyPatch, reverse: bool, roundtrip: bool
) -> None:
    monkeypatch.setattr("agentos.channels.slack.time.time", lambda: _NOW)
    entries = [
        SlackChannelEntry(name=name, token="test-token", signing_secret=f"secret-{name}")
        for name in ("team-a", "team-b")
    ]
    if reverse:
        entries.reverse()
    if roundtrip:
        entries = [
            SlackChannelEntry.model_validate_json(entry.model_dump_json()) for entry in entries
        ]
    manager = ChannelManager.from_config(entries, turn_runner=None, session_manager=None)
    try:
        routes = manager.collect_webhook_routes()
        assert {route.path for route in routes} == {"/slack/events/team-a", "/slack/events/team-b"}
        assert [route.path for route in manager.collect_webhook_routes()] == [
            route.path for route in routes
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=Starlette(routes=routes)),
            base_url="https://example.test",
        ) as client:
            for name in ("team-a", "team-b"):
                body = json.dumps(
                    {
                        "type": "event_callback",
                        "event": {
                            "type": "message",
                            "channel": "D_TEST",
                            "user": "U_TEST",
                            "text": name,
                        },
                    }
                ).encode()
                headers = {**_headers(body, f"secret-{name}"), "content-type": "application/json"}
                response = await client.post(f"/slack/events/{name}", content=body, headers=headers)
                assert response.status_code == 200
                channel = manager.get(name)
                assert isinstance(channel, SlackChannel)
                assert channel._queue.get_nowait().content == name
                assert all(adapter._queue.empty() for adapter in manager._channels.values())
        # Derivation must not persist a computed default into the user's config.
        assert all(not entry.webhook_path for entry in entries)
    finally:
        for entry in entries:
            manager._unregister_tool_channel(entry.name, manager.get(entry.name))


def test_explicit_slack_path_is_preserved_alongside_an_automatic_account() -> None:
    entries = [
        SlackChannelEntry(name="legacy", token="test-token", webhook_path="/slack/events"),
        SlackChannelEntry(name="second", token="test-token"),
    ]
    manager = ChannelManager.from_config(entries, turn_runner=None, session_manager=None)
    try:
        assert {route.path for route in manager.collect_webhook_routes()} == {
            "/slack/events",
            "/slack/events/second",
        }
    finally:
        for entry in entries:
            manager._unregister_tool_channel(entry.name, manager.get(entry.name))


def test_explicit_slack_path_cannot_shadow_an_automatic_account() -> None:
    entries = [
        SlackChannelEntry(name="first", token="test-token", webhook_path="/slack/events/second"),
        SlackChannelEntry(name="second", token="test-token"),
    ]
    manager = ChannelManager.from_config(entries, turn_runner=None, session_manager=None)
    try:
        with pytest.raises(ValueError, match="first.*second.*webhook_path"):
            manager.collect_webhook_routes()
    finally:
        for entry in entries:
            manager._unregister_tool_channel(entry.name, manager.get(entry.name))


@pytest.mark.parametrize("name", ["team/a", "{account}", "..", ""])
def test_automatic_slack_paths_reject_names_that_change_url_routing(name: str) -> None:
    entries = [
        SlackChannelEntry(name=name, token="test-token"),
        SlackChannelEntry(name="second", token="test-token"),
    ]
    with pytest.raises(ValueError, match="explicit webhook_path"):
        ChannelManager.from_config(entries, turn_runner=None, session_manager=None)
