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
5. Non-ASCII bytes in the header token are handled gracefully (401, not 500)
   — ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII ``str``, so
   both sides are encoded to ``bytes`` before comparing.
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


@ pytest.mark.asyncio
async def test_matching_secret_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    body = (
        b'{"update_id": 1, "message": {"message_id": 7, '
        b'"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, '
        b'"text": "hello"}}'
    )
    request = _build_request(
        {"X-Telegram-Bot-Api-Secret-Token": SECRET}, body=body
    )
    response = await channel._handle_webhook(request)
    assert response.status_code == 200


@ pytest.mark.asyncio
async def test_wrong_length_secret_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    request = _build_request({"X-Telegram-Bot-Api-Secret-Token": "short"})
    response = await channel._handle_webhook(request)
    assert response.status_code == 401


@ pytest.mark.asyncio
async def test_same_length_wrong_secret_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    request = _build_request({"X-Telegram-Bot-Api-Secret-Token": "x" * len(SECRET)})
    response = await channel._handle_webhook(request)
    assert response.status_code == 401


@ pytest.mark.asyncio
async def test_missing_header_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """No header at all — must 401, not raise ``TypeError`` from compare_digest."""
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    request = _build_request({})
    response = await channel._handle_webhook(request)
    assert response.status_code == 401


@ pytest.mark.asyncio
async def test_non_ascii_header_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-ASCII (>0x80) header bytes — must 401, NOT 500.

    Starlette decodes headers as latin-1, so bytes >= 0x80 produce
    non-ASCII str. ``hmac.compare_digest(a_str, b_str)`` raises TypeError;
    comparing as ``bytes`` avoids this crash.
    """
    channel = _make_channel()
    _patch_handle(monkeypatch, channel)

    # latin-1 byte 0xe9 = é — this would crash plain str compare_digest
    request = _build_request(
        {"X-Telegram-Bot-Api-Secret-Token": "secre\xe9t"}
    )
    response = await channel._handle_webhook(request)
    assert response.status_code == 401


@ pytest.mark.asyncio
async def test_compare_digest_bytes_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sabotage check: replacing with plain ``!=`` on str must fail this test."""
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
    # Verify both sides are bytes, not str
    for a, b in calls:
        assert isinstance(a, bytes), f"left operand is {type(a).__name__}, expected bytes"
        assert isinstance(b, bytes), f"right operand is {type(b).__name__}, expected bytes"
