"""Tests for Telegram channel webhook secret token verification."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agentos.channel_pairing import ChannelPairingStore
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


@pytest.fixture
def pairing_store(tmp_path: Path) -> ChannelPairingStore:
    return ChannelPairingStore(tmp_path / "pairing")


def _make_app(channel: TelegramChannel) -> Starlette:
    app = Starlette()
    app.routes.append(channel.create_webhook_route())
    return app


def test_telegram_webhook_valid_secret(pairing_store: ChannelPairingStore) -> None:
    config = TelegramChannelConfig(
        name="tg",
        webhook_secret_token="super_secret_token_123",
        webhook_path="/telegram/webhook",
    )
    channel = TelegramChannel(config, pairing_store=pairing_store)
    app = _make_app(channel)

    client = TestClient(app)
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "super_secret_token_123"},
        json={
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 100, "username": "alice"},
                "chat": {"id": 100, "type": "private"},
                "text": "hi",
            },
        },
    )
    assert response.status_code == 200


def test_telegram_webhook_invalid_secret(pairing_store: ChannelPairingStore) -> None:
    config = TelegramChannelConfig(
        name="tg",
        webhook_secret_token="super_secret_token_123",
        webhook_path="/telegram/webhook",
    )
    channel = TelegramChannel(config, pairing_store=pairing_store)
    app = _make_app(channel)

    client = TestClient(app)
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong_secret"},
        json={"update_id": 1},
    )
    assert response.status_code == 401


def test_telegram_webhook_prefix_secret_rejected(pairing_store: ChannelPairingStore) -> None:
    config = TelegramChannelConfig(
        name="tg",
        webhook_secret_token="super_secret_token_123",
        webhook_path="/telegram/webhook",
    )
    channel = TelegramChannel(config, pairing_store=pairing_store)
    app = _make_app(channel)

    client = TestClient(app)
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "super_secret_token_123_extra"},
        json={"update_id": 1},
    )
    assert response.status_code == 401


def test_telegram_webhook_missing_header_rejected(pairing_store: ChannelPairingStore) -> None:
    config = TelegramChannelConfig(
        name="tg",
        webhook_secret_token="super_secret_token_123",
        webhook_path="/telegram/webhook",
    )
    channel = TelegramChannel(config, pairing_store=pairing_store)
    app = _make_app(channel)

    client = TestClient(app)
    response = client.post(
        "/telegram/webhook",
        json={"update_id": 1},
    )
    assert response.status_code == 401


def test_telegram_webhook_unconfigured_secret_returns_503(
    pairing_store: ChannelPairingStore,
) -> None:
    config = TelegramChannelConfig(
        name="tg",
        webhook_secret_token="temp_for_route_creation",
        webhook_path="/telegram/webhook",
    )
    channel = TelegramChannel(config, pairing_store=pairing_store)
    app = _make_app(channel)

    channel.config.webhook_secret_token = ""
    client = TestClient(app)
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "temp_for_route_creation"},
        json={"update_id": 1},
    )
    assert response.status_code == 503
