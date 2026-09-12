from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels.slack import SlackChannel
from agentos.tools.builtin.messaging import _delete_message_id


def test_delete_message_id_encodes_target_for_slack() -> None:
    # Verify _delete_message_id prefixes target for Slack
    res = _delete_message_id("slack", "C99999999", "1712345678.123456")
    assert res == "C99999999|1712345678.123456"

    # Preserves existing channel|ts
    res_existing = _delete_message_id("slack", "C99999999", "C11111111|1712345678.123456")
    assert res_existing == "C11111111|1712345678.123456"


@pytest.mark.asyncio
async def test_slack_channel_delete_honors_target_channel() -> None:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C_DEFAULT")

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    channel._client = mock_client

    # Case 1: Default channel when no target prefix is present
    await channel.delete("1712345678.123456")
    mock_client.post.assert_awaited_with(
        "/chat.delete",
        json={"channel": "C_DEFAULT", "ts": "1712345678.123456"},
    )

    # Case 2: Explicit target channel passed via channel_id|ts
    await channel.delete("C99999999|1712345678.123456")
    mock_client.post.assert_awaited_with(
        "/chat.delete",
        json={"channel": "C99999999", "ts": "1712345678.123456"},
    )
