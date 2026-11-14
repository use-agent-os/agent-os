"""Regression test: Discord approval sessionKey binding hardcodes "discord".

``_handle_discord_component_interaction`` checks
``session_channel != "discord"`` -- a hardcoded literal -- instead of the
configured channel entry's actual name, unlike the Telegram and MSTeams
handlers which correctly compare against ``self.config.name``.

Session keys embed the *configured entry name* (``ChannelManager.
_build_session_key`` -> ``session.keys.build_group_key(channel=entry.name,
...)``), which is a required field with no default
(``ConfiguredChannelEntry.name: str``) -- every real deployment sets it
explicitly, and any multi-account Discord setup necessarily has at least
one entry not literally named "discord" (dict keys must be unique). For
any such entry, every approve/deny click is wrongly rejected as a
"mismatch" even though it is the correct, intended approver -- the pending
tool-call approval is never resolved.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.session.keys import build_group_key


@pytest.fixture(autouse=True)
def _clean_approval_queue():
    reset_approval_queue()
    yield
    reset_approval_queue()


async def test_discord_approval_resolves_for_a_non_default_named_entry() -> None:
    """A Discord entry configured as e.g. "discord-support" (not literally
    "discord") must still be able to resolve its own pending approvals.
    """
    queue = get_approval_queue()
    session_key = build_group_key(agent_id="main", channel="discord-support", peer_id="chan123")
    approval_id = queue.request(
        "exec",
        {"argv": ["rm", "-rf"], "action_kind": "exec", "sessionKey": session_key},
    )

    channel = DiscordChannel(
        DiscordChannelConfig(
            token="test-token", name="discord-support", allowed_user_ids=["usr123"]
        )
    )
    channel.policy = replace(channel.policy, allowlist=frozenset(), allowlist_enabled=False)

    post_calls: list[tuple[str, dict]] = []

    async def fake_post(path, json=None, headers=None, **kwargs):
        post_calls.append((path, json))

        class FakeResp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {"id": "999"}

        return FakeResp()

    channel._get_client = lambda: AsyncMock(post=fake_post)  # type: ignore[method-assign]

    data = {
        "type": 3,
        "id": "int123",
        "token": "token123",
        "channel_id": "chan123",
        "user": {"id": "usr123"},
        "message": {"content": "Approval requested"},
        "data": {"custom_id": f"approve:{approval_id}"},
    }

    await channel._handle_discord_component_interaction(data)

    entry = queue.get(approval_id)
    assert entry.resolved is True, (
        "approval was never resolved -- the sessionKey check wrongly treated "
        "a correctly-matching entry name as a mismatch"
    )
    assert entry.approved is True
