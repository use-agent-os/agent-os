"""The reply enqueued after a Slack approval click must thread under the
prompt it answered when that prompt is a top-level (non-threaded) message.

``_handle_slack_interactive`` synthesizes a fake inbound event for
``parse_event`` so the "Approve"/"Deny" acknowledgement can flow back through
the normal reply pipeline. It copied ``orig_message.get("thread_ts")`` but
never ``orig_message.get("ts")``. ``parse_event`` sets ``metadata["ts"]`` from
whatever ``ts`` key is present -- absent here, so it landed as ``None`` -- and
``_reply_thread_ts`` (used by ``build_reply_message`` /
``streaming_reply_kwargs``) falls back to ``metadata["ts"]`` only when
``thread_ts`` is missing. When the approval prompt itself was never posted in
a thread (``orig_message["thread_ts"]`` is absent), both fields ended up
``None`` and the agent's reply to the click posted as an unthreaded top-level
message instead of threading under the approval prompt -- silently losing the
thread anchor, most confusingly when several approvals are pending at once in
the same channel.
"""

from __future__ import annotations

import pytest

from agentos.channels.slack import SlackChannel
from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue

CHANNEL_ID = "C12345"
USER_ID = "U12345"
PROMPT_TS = "1700000000.000100"


@pytest.fixture(autouse=True)
def _clean_approval_queue():
    reset_approval_queue()
    yield
    reset_approval_queue()


def _channel() -> SlackChannel:
    channel = SlackChannel(token="xoxb-test", slack_channel_id=CHANNEL_ID, reply_in_thread=True)
    return channel


def _click(approval_id: str, *, message: dict) -> dict:
    return {
        "type": "block_actions",
        "user": {"id": USER_ID},
        "channel": {"id": CHANNEL_ID},
        "team": {"id": "T1"},
        "message": message,
        "actions": [{"value": f"approve:{approval_id}"}],
    }


def _pending() -> str:
    return get_approval_queue().request("exec", {"argv": ["ls"], "action_kind": "exec"})


@pytest.mark.asyncio
async def test_reply_threads_under_a_top_level_approval_prompt() -> None:
    """The approval prompt itself was posted top-level (no thread_ts): the
    enqueued reply must thread under the prompt's own ts, not post bare."""
    channel = _channel()
    approval_id = _pending()
    # No "thread_ts" on the original message -- it was a top-level post.
    await channel._handle_slack_interactive(
        _click(approval_id, message={"text": "Approve?", "blocks": [], "ts": PROMPT_TS})
    )

    msg = await channel.receive()

    assert channel._reply_thread_ts(msg) == PROMPT_TS


@pytest.mark.asyncio
async def test_reply_still_threads_under_an_existing_thread() -> None:
    """When the prompt was itself already inside a thread, that thread_ts
    keeps winning (unchanged behavior)."""
    channel = _channel()
    approval_id = _pending()
    await channel._handle_slack_interactive(
        _click(
            approval_id,
            message={
                "text": "Approve?",
                "blocks": [],
                "ts": "1700000000.000200",
                "thread_ts": PROMPT_TS,
            },
        )
    )

    msg = await channel.receive()

    assert channel._reply_thread_ts(msg) == PROMPT_TS
