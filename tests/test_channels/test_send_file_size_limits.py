"""Regression tests for per-adapter file size ceilings in channel send_file (#683).

Verifies that:
- Check helper enforces limits and formats actionable error messages.
- DiscordChannel enforces its 10 MB ceiling.
- TelegramChannel enforces its 50 MB ceiling (and does not reject 30 MB files).
- EmailChannel enforces its 25 MB ceiling and returns a failed ChannelSendResult.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentos.channels._util import check_channel_file_size
from agentos.channels.contract import ChannelSendStatus
from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.email import EmailChannel, EmailChannelConfig
from agentos.channels.slack import SlackChannel
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


class TestCheckChannelFileSize:
    def test_accepts_file_under_limit(self, tmp_path: Path) -> None:
        f = tmp_path / "small.txt"
        f.write_bytes(b"x" * 100)
        assert check_channel_file_size(f, 200, "TestAdapter") == 100

    def test_accepts_file_at_exact_limit(self, tmp_path: Path) -> None:
        f = tmp_path / "exact.txt"
        f.write_bytes(b"x" * 200)
        assert check_channel_file_size(f, 200, "TestAdapter") == 200

    def test_rejects_file_exceeding_limit(self, tmp_path: Path) -> None:
        f = tmp_path / "oversized.txt"
        f.write_bytes(b"x" * 300)
        with pytest.raises(ValueError) as exc_info:
            check_channel_file_size(f, 200, "CustomService")
        msg = str(exc_info.value)
        assert "300 bytes" in msg
        assert "CustomService" in msg
        assert "200 bytes" in msg

    def test_raises_on_nonexistent_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.bin"
        with pytest.raises(ValueError, match="Cannot read file size"):
            check_channel_file_size(missing, 1024, "TestAdapter")


class TestDiscordFileSizeCeiling:
    def test_discord_ceiling_is_10_mb(self) -> None:
        assert DiscordChannel.MAX_FILE_BYTES == 10 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_send_file_rejects_oversized_before_rate_limiting(self, tmp_path: Path) -> None:
        f = tmp_path / "large_audio.wav"
        f.write_bytes(b"x" * 10)

        channel = DiscordChannel(config=DiscordChannelConfig(token="test-token"))

        with patch("os.path.getsize", return_value=11 * 1024 * 1024):
            with pytest.raises(ValueError, match="exceeds Discord 10 MB upload ceiling"):
                await channel.send_file(channel_id="123", file_path=str(f))


class TestTelegramFileSizeCeiling:
    def test_telegram_ceiling_is_50_mb(self) -> None:
        assert TelegramChannel.MAX_FILE_BYTES == 50 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_send_file_rejects_oversized_file(self, tmp_path: Path) -> None:
        f = tmp_path / "huge_doc.pdf"
        f.write_bytes(b"x" * 10)

        channel = TelegramChannel(config=TelegramChannelConfig(token="fake-token"))

        with patch("os.path.getsize", return_value=51 * 1024 * 1024):
            with pytest.raises(ValueError, match="exceeds Telegram 50 MB upload ceiling"):
                await channel.send_file(chat_id="123", file_path=str(f))

    @pytest.mark.asyncio
    async def test_send_file_permits_30_mb_file(self, tmp_path: Path) -> None:
        """30 MB document is accepted by Telegram (50 MB limit), unlike blanket 25 MB limits."""
        f = tmp_path / "medium_doc.pdf"
        f.write_bytes(b"x" * 10)

        channel = TelegramChannel(config=TelegramChannelConfig(token="fake-token"))

        with patch("os.path.getsize", return_value=30 * 1024 * 1024):
            mock_client = AsyncMock()
            mock_client.post.return_value.json.return_value = {"ok": True, "result": {}}
            with patch.object(channel, "_get_client", return_value=mock_client):
                with patch.object(channel, "_parse_api_response", return_value={"message_id": "1"}):
                    res = await channel.send_file(chat_id="123", file_path=str(f))
                    assert res.status == ChannelSendStatus.SENT


class TestEmailFileSizeCeiling:
    def test_email_ceiling_is_25_mb(self) -> None:
        assert EmailChannel.MAX_ATTACHMENT_BYTES == 25 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_send_file_fails_gracefully_on_oversized_attachment(self, tmp_path: Path) -> None:
        f = tmp_path / "oversized_attachment.zip"
        f.write_bytes(b"x" * 10)

        channel = EmailChannel(
            config=EmailChannelConfig(
                imap_host="localhost",
                smtp_host="localhost",
                username="test@example.com",
            )
        )

        with patch("os.path.getsize", return_value=26 * 1024 * 1024):
            result = await channel.send_file(thread_id="thread-123", file_path=str(f))

        assert result.status == ChannelSendStatus.FAILED
        assert "exceeds Email 25 MB upload ceiling" in result.reason


class TestSlackFileSizeCeiling:
    def test_slack_ceiling_is_1_gb(self) -> None:
        assert SlackChannel.MAX_FILE_BYTES == 1024 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_send_file_rejects_oversized_file(self, tmp_path: Path) -> None:
        f = tmp_path / "huge_file.dat"
        f.write_bytes(b"x" * 10)

        channel = SlackChannel(token="xoxb-test", slack_channel_id="C00000001")

        with patch("os.path.getsize", return_value=1025 * 1024 * 1024):
            with pytest.raises(ValueError, match="exceeds Slack 1024 MB upload ceiling"):
                await channel.send_file(channel_id="C00000001", file_path=str(f))
