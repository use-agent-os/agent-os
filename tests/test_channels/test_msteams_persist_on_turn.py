"""Regression test: a new conversation reference must survive an unclean restart.

``_save_conversation_cache()`` used to be called only from ``stop()``. Every
inbound message updated ``self._references`` in memory only (``_on_turn``),
so a process that never reaches a clean ``stop()`` -- a crash, an OOM kill, a
container redeploy -- silently lost every conversation reference learned
since the last graceful shutdown. Proactive/streamed replies to those
conversations would then fail after restart until the user messaged again.

``_on_turn`` must persist the cache as soon as a *new* conversation is first
learned, not only at shutdown.
"""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentos.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig


@pytest.fixture(autouse=True)
def _stub_botbuilder(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeTurnContext:
        @staticmethod
        def get_conversation_reference(activity: object) -> str:
            return f"REF:{activity.conversation.id}:{activity.id}"  # type: ignore[attr-defined]

    class _FakeActivity:
        def __init__(self, *, type: str, id: str, text: str) -> None:  # noqa: A002
            self.type = type
            self.id = id
            self.text = text

    core_module = types.ModuleType("botbuilder.core")
    core_module.TurnContext = _FakeTurnContext  # type: ignore[attr-defined]
    schema_module = types.ModuleType("botbuilder.schema")
    schema_module.Activity = _FakeActivity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botbuilder.core", core_module)
    monkeypatch.setitem(sys.modules, "botbuilder.schema", schema_module)


def _turn(conversation_id: str, activity_id: str) -> SimpleNamespace:
    activity = SimpleNamespace(
        type="message",
        id=activity_id,
        text="hi",
        conversation=SimpleNamespace(id=conversation_id, conversation_type="personal"),
        from_property=SimpleNamespace(id="user-1"),
        recipient=SimpleNamespace(id="bot-1"),
        entities=[],
        service_url="https://smba.trafficmanager.net/",
        channel_data={},
    )
    return SimpleNamespace(activity=activity)


async def test_on_turn_persists_a_newly_learned_conversation_immediately(
    tmp_path: object,
) -> None:
    """No stop() is ever called -- simulates a crash right after the message."""
    channel = MSTeamsChannel(
        config=MSTeamsChannelConfig(name="msteams", workspace_dir=str(tmp_path))
    )
    channel._adapter = MagicMock()

    await channel._on_turn(_turn("conversation-A", "a1"))

    cache_path = tmp_path / "state" / "msteams" / "conversations.json"
    assert cache_path.is_file()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "conversation-A" in data["conversations"]


async def test_on_turn_does_not_rewrite_the_cache_for_an_already_known_conversation(
    tmp_path: object,
) -> None:
    """Only a brand-new key needs an immediate flush; re-touching a known one
    does not need to hit disk on every single message."""
    channel = MSTeamsChannel(
        config=MSTeamsChannelConfig(name="msteams", workspace_dir=str(tmp_path))
    )
    channel._adapter = MagicMock()

    await channel._on_turn(_turn("conversation-A", "a1"))
    cache_path = tmp_path / "state" / "msteams" / "conversations.json"
    first_mtime = cache_path.stat().st_mtime_ns

    await channel._on_turn(_turn("conversation-A", "a2"))

    assert cache_path.stat().st_mtime_ns == first_mtime
