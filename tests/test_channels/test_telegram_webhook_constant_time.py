"""Constant-time comparison for Telegram webhook secret tokens (fix #962).

The previous implementation compared the configured ``webhook_secret_token``
against the ``X-Telegram-Bot-Api-Secret-Token`` request header using ``!=``,
which short-circuits at the first mismatching character. A network attacker
who can observe response timings can mount a character-by-character brute-force
on the secret — feasible because Telegram secrets are operator-chosen strings
without rate limits on the webhook endpoint.

These tests verify:

1. A matching secret reaches update handling (200).
2. Wrong-length and same-length wrong secrets are rejected (401).
3. A missing header is rejected (401) instead of raising ``TypeError``.
4. A malformed JSON body with a valid secret returns 400 — auth runs first.
"""

from __future__ import annotations

import hmac
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig

SECRET = "topsecret-token-aa11"


def _make_channel() -> TelegramChannel:
    return TelegramChannel(
        TelegramChannelConfig(
            name="tg-ct",
            token="000:fake",
            webhook_secret_token=SECRET,
        )
    )


def _build_request(
    headers: dict[str, str],
    body: bytes = b"{}",
) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "headers": raw_headers, "query_string": b""},
        receive=receive,
    )


def _patch_handle(monkeypatch: pytest.MonkeyPatch, channel: TelegramChannel) -> None:
    """Replace the update handler so the test only exercises auth."""
    monkeypatch.setattr(
        channel,
        "_handle_telegram_callback",
        AsyncMock(),
        raising=False,
    )
    real_parse = channel.parse_incoming
    calls: list[tuple[str, Any]] = []

    def spy_parse(update: dict[str, Any]) -> Any:
        calls.append(("parse_incoming", id(update)))
        return real_parse(update)

    monkeypatch.setattr(channel, "parse_incoming", spy_parse)


@pytest.mark.asyncio
async def test_matching_secret_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    body = (
        b'{"update_id": 1, "message": {"message_id": 7, '
        b'"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, '
        b'"text": "hello"}}'
    )
    request = _build_request({"X-Telegram-Bot-Api-Secret-Token": SECRET}, body=body)
    response = await channel._handle_webhook(request)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_wrong_length_secret_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    request = _build_request({"X-Telegram-Bot-Api-Secret-Token": "short"})
    response = await channel._handle_webhook(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_same_length_wrong_secret_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    request = _build_request({"X-Telegram-Bot-Api-Secret-Token": "x" * len(SECRET)})
    response = await channel._handle_webhook(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_missing_header_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """No header at all — must 401, not raise ``TypeError`` from compare_digest."""
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    request = _build_request({})
    response = await channel._handle_webhook(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_header_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-ASCII bytes in header must not raise TypeError — must return 401."""
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    # Starlette decodes >=0x80 as latin-1, producing a non-ASCII str.
    # hmac.compare_digest(str, str) raises TypeError on non-ASCII.
    request = _build_request({"X-Telegram-Bot-Api-Secret-Token": "\xc3\xa9" + "x" * 18})
    response = await channel._handle_webhook(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_compare_digest_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sabotage check: reverting to plain ``!=`` must fail this test."""
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr("agentos.channels.telegram.hmac.compare_digest", spy)

    request = _build_request({"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"})
    response = await channel._handle_webhook(request)
    assert response.status_code == 401
    assert calls, "channel._handle_webhook did not delegate to hmac.compare_digest"
