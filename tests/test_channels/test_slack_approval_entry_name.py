"""Issue #1606: the Slack approval binding must compare the sessionKey's
channel segment against the entry's configured name.

Session keys embed the *entry name* (``ChannelManager._build_session_key`` ->
``build_group_key(channel=entry.name, ...)``). ``SlackChannel`` compared that
segment against ``channel_id``, a dataclass default of ``"slack"`` that the
registry never overwrote -- ``name`` sits in ``_COMMON_ENTRY_FIELDS``, so the
generic builder excluded it by construction. On any entry not literally
named ``slack`` every Approve/Deny click logged ``slack.interactive_mismatch``
and the approval never resolved. Discord, Telegram and MS Teams carry the
entry name on their config; the flat-dataclass path now gets it too.
"""

from __future__ import annotations

import pytest

from agentos.channels.registry import build_managed_channel
from agentos.channels.slack import SlackChannel
from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.gateway.config import SlackChannelEntry
from agentos.session.keys import DmScope, build_direct_key, build_group_key

CHANNEL_ID = "C12345"
DM_ID = "D12345"
USER_ID = "U12345"


@pytest.fixture(autouse=True)
def _clean_approval_queue():
    reset_approval_queue()
    yield
    reset_approval_queue()


def _channel(name: str) -> SlackChannel:
    # The clicker is admitted, so these tests exercise approval custody alone.
    entry = SlackChannelEntry(
        name=name, token="xoxb-test", slack_channel_id=CHANNEL_ID, allowed_user_ids=[USER_ID]
    )
    adapter = build_managed_channel(entry)
    assert isinstance(adapter, SlackChannel)
    return adapter


def _click(approval_id: str, *, action: str = "approve", channel_id: str = CHANNEL_ID) -> dict:
    # No ``response_url``: the handler only posts back when Slack gave it one,
    # so the test stays offline.
    return {
        "type": "block_actions",
        "user": {"id": USER_ID},
        "channel": {"id": channel_id},
        "team": {"id": "T1"},
        "message": {"text": "Approval requested", "blocks": []},
        "actions": [{"value": f"{action}:{approval_id}"}],
    }


def _pending(session_key: str) -> str:
    return get_approval_queue().request(
        "exec", {"argv": ["rm", "-rf"], "action_kind": "exec", "sessionKey": session_key}
    )


@pytest.mark.asyncio
async def test_approval_resolves_for_a_non_default_entry_name() -> None:
    queue = get_approval_queue()
    approval_id = _pending(
        build_group_key(agent_id="main", channel="slack-support", peer_id=CHANNEL_ID)
    )
    channel = _channel("slack-support")

    await channel._handle_slack_interactive(_click(approval_id))

    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is True


@pytest.mark.asyncio
async def test_direct_message_approval_resolves_for_a_non_default_entry_name() -> None:
    queue = get_approval_queue()
    # The manager keys DMs per channel+peer; the default ``DmScope.MAIN`` would
    # produce a 3-segment key the handler never checks at all.
    approval_id = _pending(
        build_direct_key(
            agent_id="main",
            channel="slack-support",
            peer_id=USER_ID,
            dm_scope=DmScope.PER_CHANNEL_PEER,
        )
    )
    channel = _channel("slack-support")

    await channel._handle_slack_interactive(_click(approval_id, action="deny", channel_id=DM_ID))

    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False


@pytest.mark.asyncio
async def test_default_entry_name_keeps_working() -> None:
    queue = get_approval_queue()
    approval_id = _pending(build_group_key(agent_id="main", channel="slack", peer_id=CHANNEL_ID))
    channel = _channel("slack")

    await channel._handle_slack_interactive(_click(approval_id))

    assert queue.get(approval_id).resolved is True


@pytest.mark.asyncio
async def test_click_from_another_entry_is_still_rejected() -> None:
    """The binding is tightened, not loosened: an approval raised on the
    ``slack-ops`` entry cannot be resolved by a click on ``slack-support``,
    even from the same channel id."""
    queue = get_approval_queue()
    approval_id = _pending(
        build_group_key(agent_id="main", channel="slack-ops", peer_id=CHANNEL_ID)
    )
    channel = _channel("slack-support")

    await channel._handle_slack_interactive(_click(approval_id))

    assert queue.get(approval_id).resolved is False


@pytest.mark.asyncio
async def test_click_from_another_channel_is_still_rejected() -> None:
    queue = get_approval_queue()
    approval_id = _pending(
        build_group_key(agent_id="main", channel="slack-support", peer_id=CHANNEL_ID)
    )
    channel = _channel("slack-support")

    await channel._handle_slack_interactive(_click(approval_id, channel_id="C_OTHER"))

    assert queue.get(approval_id).resolved is False


def test_registry_hands_the_entry_name_to_the_adapter() -> None:
    entry = SlackChannelEntry(name="slack-support", token="xoxb-test", slack_channel_id=CHANNEL_ID)

    adapter = build_managed_channel(entry)

    assert isinstance(adapter, SlackChannel)
    assert adapter.name == "slack-support"
    # The other entry fields still arrive the way they did before.
    assert adapter.token == "xoxb-test"
    assert adapter.slack_channel_id == CHANNEL_ID


def test_channel_id_stays_the_adapter_type_id() -> None:
    """``channel_id`` keys ``pending_overflow_policy_per_channel`` (documented
    as ``"slack"``); binding approvals to a separate ``name`` field leaves
    that contract alone."""
    assert _channel("slack-support").channel_id == "slack"
    assert SlackChannel(token="xoxb-test", slack_channel_id=CHANNEL_ID).name == "slack"
