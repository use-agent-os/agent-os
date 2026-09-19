"""A silent-reply sentinel wrapped in Markdown is still silent.

The system prompt lists the sentinels as code spans (`NO_REPLY`, `HEARTBEAT_OK`),
and a model that copies that formatting answers with the backticks, or in bold, or
with a closing full stop. The engine compared the reply as written, so every one
of those went out to the channel as a literal message.

These run the real ``TurnRunner`` with a provider that answers with a fixed reply
and read the ``done`` text, which is what ``HeartbeatService`` delivers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.provider import DoneEvent, TextDeltaEvent
from agentos.tools import ToolContext
from agentos.tools.types import CallerKind


class _ReplyProvider:
    provider_name = "fake"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def chat(self, messages: Any, tools: Any = None, config: Any = None) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield TextDeltaEvent(text=self.reply)
        yield DoneEvent(stop_reason="end_turn", input_tokens=4, output_tokens=2)

    async def list_models(self) -> list[Any]:
        return []


class _Selector:
    def __init__(self, provider: _ReplyProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model="fake-model")

    def clone(self) -> _Selector:
        return self

    def resolve(self) -> _ReplyProvider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config.model = model


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    # The runner writes decision logs under the state dir.
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))


async def _delivered(reply: str, run_kind: str) -> str:
    runner = TurnRunner(provider_selector=_Selector(_ReplyProvider(reply)))
    done = [
        event
        async for event in runner.run(
            "Read HEARTBEAT.md if it exists.",
            "agent:main:main",
            ToolContext(caller_kind=CallerKind.AGENT),
            run_kind=run_kind,
        )
        if event.kind == "done"
    ]
    return done[-1].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "`HEARTBEAT_OK`",
        "**HEARTBEAT_OK**",
        "`HEARTBEAT_OK` - nothing needs attention.",
        "Checked the inbox and the calendar. **HEARTBEAT_OK**.",
    ],
)
async def test_a_wrapped_heartbeat_ack_is_not_delivered(reply):
    assert await _delivered(reply, "heartbeat") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["`NO_REPLY`", "**NO_REPLY**", "NO_REPLY.", "`NO_REPLY`."])
async def test_a_wrapped_no_reply_is_not_delivered(reply):
    assert await _delivered(reply, "default") == ""


# ── unchanged ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "run_kind"),
    [("HEARTBEAT_OK", "heartbeat"), ("HEARTBEAT_OK.", "heartbeat"), ("NO_REPLY", "default")],
)
async def test_a_bare_sentinel_is_still_silent(reply, run_kind):
    assert await _delivered(reply, run_kind) == ""


@pytest.mark.asyncio
async def test_a_reply_that_mentions_a_sentinel_is_still_delivered():
    """Positive control: only a reply that IS the token goes silent."""
    reply = "Reply `NO_REPLY` when a group message is not for you."

    assert await _delivered(reply, "default") == reply


@pytest.mark.asyncio
async def test_a_long_heartbeat_report_is_still_delivered():
    """The ack allowance still applies: a real report beside the token is sent."""
    report = "Disk on db-1 is at 97% and the nightly backup failed. " * 8
    reply = f"{report}**HEARTBEAT_OK**"

    assert await _delivered(reply, "heartbeat") == reply
