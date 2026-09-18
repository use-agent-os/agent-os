from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentos.channels.contract import ChannelCapabilities, ChannelSendStatus
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None, status_code: int = 200) -> None:
        self._payload = payload or {"ok": True}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_telegram_send_file_short_caption_no_overflow(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
            requests.append((path, kwargs))
            if path == "/bottoken/sendDocument":
                return _FakeResponse(
                    {"ok": True, "result": {"message_id": 101, "document": {"file_id": "doc-101"}}}
                )
            if path == "/bottoken/sendMessage":
                return _FakeResponse({"ok": True, "result": {"message_id": 102}})
            raise AssertionError(f"Unexpected path: {path}")

    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    channel._client = FakeClient()  # type: ignore[assignment]

    result = await channel.send_file(
        "12345", str(file_path), content="A short caption under 1024 chars."
    )

    assert result.status == ChannelSendStatus.SENT
    assert result.capability == ChannelCapabilities.NATIVE_FILE_UPLOAD
    assert result.target_id == "12345"
    assert result.provider_message_id == "101"
    assert result.provider_file_id == "doc-101"

    # Exactly one request to sendDocument, none to sendMessage
    assert len(requests) == 1
    assert requests[0][0] == "/bottoken/sendDocument"
    assert requests[0][1]["data"]["caption"] == "A short caption under 1024 chars."
    assert requests[0][1]["data"]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_telegram_send_file_caption_overflow_sends_follow_up(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
            requests.append((path, kwargs))
            if path == "/bottoken/sendDocument":
                return _FakeResponse(
                    {"ok": True, "result": {"message_id": 201, "document": {"file_id": "doc-201"}}}
                )
            if path == "/bottoken/sendMessage":
                return _FakeResponse({"ok": True, "result": {"message_id": 202}})
            raise AssertionError(f"Unexpected path: {path}")

    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    channel._client = FakeClient()  # type: ignore[assignment]

    # Create content that exceeds 1024 characters
    paragraph1 = "Paragraph 1: " + ("alpha " * 120)  # ~730 chars
    paragraph2 = "Paragraph 2: " + ("beta " * 120)  # ~720 chars
    content = f"{paragraph1}\n\n{paragraph2}"  # ~1450 chars

    result = await channel.send_file("12345", str(file_path), content=content)

    assert result.status == ChannelSendStatus.SENT
    assert result.provider_message_id == "201"
    assert result.provider_file_id == "doc-201"

    # Should have two requests: sendDocument, then sendMessage for the overflow
    assert len(requests) == 2
    assert requests[0][0] == "/bottoken/sendDocument"
    doc_caption = requests[0][1]["data"]["caption"]
    assert len(doc_caption) <= 1024
    assert "Paragraph 1" in doc_caption

    assert requests[1][0] == "/bottoken/sendMessage"
    overflow_text = requests[1][1]["json"]["text"]
    assert "Paragraph 2" in overflow_text
    assert requests[1][1]["json"]["chat_id"] == "12345"


@pytest.mark.asyncio
async def test_telegram_send_file_empty_content(tmp_path: Path) -> None:
    file_path = tmp_path / "empty.txt"
    file_path.write_text("empty", encoding="utf-8")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
            requests.append((path, kwargs))
            return _FakeResponse(
                {"ok": True, "result": {"message_id": 301, "document": {"file_id": "doc-301"}}}
            )

    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    channel._client = FakeClient()  # type: ignore[assignment]

    result = await channel.send_file("12345", str(file_path), content="")

    assert result.status == ChannelSendStatus.SENT
    assert len(requests) == 1
    assert requests[0][0] == "/bottoken/sendDocument"
    assert "caption" not in requests[0][1]["data"]


@pytest.mark.asyncio
async def test_telegram_send_file_huge_caption_overflow_chunks_follow_up(tmp_path: Path) -> None:
    file_path = tmp_path / "large.txt"
    file_path.write_text("large", encoding="utf-8")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
            requests.append((path, kwargs))
            msg_id = len(requests) + 400
            if path == "/bottoken/sendDocument":
                return _FakeResponse(
                    {
                        "ok": True,
                        "result": {"message_id": msg_id, "document": {"file_id": "doc-401"}},
                    }
                )
            if path == "/bottoken/sendMessage":
                return _FakeResponse({"ok": True, "result": {"message_id": msg_id}})
            raise AssertionError(f"Unexpected path: {path}")

    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    channel._client = FakeClient()  # type: ignore[assignment]

    # Create content that exceeds 6000 chars (> 1024 caption + > 4096 single message)
    paragraphs = [f"Section {i}: " + ("word " * 150) for i in range(10)]
    content = "\n\n".join(paragraphs)  # ~7500 chars

    result = await channel.send_file("12345", str(file_path), content=content)

    assert result.status == ChannelSendStatus.SENT
    # 1 sendDocument + 2 sendMessage calls (since remaining ~6700 chars splits across 4096 limit)
    assert len(requests) >= 3
    assert requests[0][0] == "/bottoken/sendDocument"
    assert len(requests[0][1]["data"]["caption"]) <= 1024
    for req in requests[1:]:
        assert req[0] == "/bottoken/sendMessage"
        assert len(req[1]["json"]["text"]) <= 4096
