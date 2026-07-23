from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentos.channels.contract import ChannelCapabilities, ChannelSendStatus
from agentos.channels.slack import SlackChannel
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = payload or {"ok": True}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_slack_send_file_uses_external_upload_flow(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
            requests.append((path, kwargs))
            if path == "/files.getUploadURLExternal":
                return _FakeResponse(
                    {"ok": True, "upload_url": "https://upload.test", "file_id": "F1"}
                )
            if path == "https://upload.test":
                return _FakeResponse({"ok": True})
            if path == "/files.completeUploadExternal":
                return _FakeResponse({"ok": True, "files": [{"id": "F1"}]})
            raise AssertionError(path)

    channel = SlackChannel(token="xoxb-token", slack_channel_id="C-default")
    channel._client = FakeClient()  # type: ignore[assignment]

    result = await channel.send_file("C-target", str(file_path), content="done")

    assert result.status == ChannelSendStatus.SENT
    assert result.capability == ChannelCapabilities.NATIVE_FILE_UPLOAD
    assert result.target_id == "C-target"
    assert result.provider_file_id == "F1"
    assert requests[0][0] == "/files.getUploadURLExternal"
    assert requests[1][0] == "https://upload.test"
    assert requests[2] == (
        "/files.completeUploadExternal",
        {
            "json": {
                "files": [{"id": "F1", "title": "report.txt"}],
                "channel_id": "C-target",
                "initial_comment": "done",
            }
        },
    )


@pytest.mark.asyncio
async def test_telegram_send_file_posts_document_upload(tmp_path: Path) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF")
    requests: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
            requests.append((path, kwargs))
            return _FakeResponse(
                {"ok": True, "result": {"message_id": 42, "document": {"file_id": "doc-1"}}}
            )

    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    channel._client = FakeClient()  # type: ignore[assignment]

    result = await channel.send_file("12345", str(file_path), content="done")

    assert result.status == ChannelSendStatus.SENT
    assert result.target_id == "12345"
    assert result.provider_message_id == "42"
    assert result.provider_file_id == "doc-1"
    assert requests[0][0] == "/bottoken/sendDocument"
    assert requests[0][1]["data"] == {"chat_id": "12345", "caption": "done"}
