"""The Teams conversation cache survives an unclean restart (#2658).

``_save_conversation_cache`` used to run only from ``stop()``, which a crash,
OOM kill or redeploy never reaches. Every conversation learned since the last
clean stop was gone after such a restart, and so was the last-activity order
that ``send(reply_to=None)`` -- the heartbeat/cron delivery path -- reads to
pick "whoever last spoke".

Each test "crashes" by simply never calling ``stop()`` and restarts with a
fresh channel pointed at the same workspace. The Bot Framework SDK is not an
installable dependency, so it is stubbed; references are small objects that
round-trip through ``serialize``/``deserialize`` like the real ones.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from agentos.channels import msteams
from agentos.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from agentos.channels.types import OutgoingMessage


class _FakeReference:
    def __init__(self, tag: str = "") -> None:
        self.tag = tag

    def serialize(self) -> dict[str, str]:
        return {"tag": self.tag}

    def deserialize(self, data: dict[str, str]) -> _FakeReference:
        return _FakeReference(data["tag"])


@pytest.fixture(autouse=True)
def _stub_botbuilder(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeTurnContext:
        @staticmethod
        def get_conversation_reference(activity: Any) -> _FakeReference:
            return _FakeReference(f"{activity.conversation.id}:{activity.id}")

    core_module = types.ModuleType("botbuilder.core")
    core_module.TurnContext = _FakeTurnContext  # type: ignore[attr-defined]
    schema_module = types.ModuleType("botbuilder.schema")
    schema_module.ConversationReference = _FakeReference  # type: ignore[attr-defined]
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


def _channel(workspace: Path) -> MSTeamsChannel:
    channel = MSTeamsChannel(
        config=MSTeamsChannelConfig(name="msteams", workspace_dir=str(workspace))
    )
    channel._adapter = MagicMock()
    channel._adapter.continue_conversation = AsyncMock()
    return channel


async def _restarted(workspace: Path) -> MSTeamsChannel:
    channel = _channel(workspace)
    await channel.start()
    return channel


def _cache_file(workspace: Path) -> Path:
    return workspace / "state" / "msteams" / "conversations.json"


def _cached_tags(workspace: Path) -> dict[str, str]:
    data = json.loads(_cache_file(workspace).read_text(encoding="utf-8"))
    return {key: ref["tag"] for key, ref in data["conversations"].items()}


def _sent_tag(channel: MSTeamsChannel) -> str:
    call = channel._adapter.continue_conversation.call_args
    reference = call.args[0] if call.args else call.kwargs["reference"]
    return str(reference.tag)


async def test_a_new_conversation_is_on_disk_without_a_clean_stop(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    await channel._on_turn(_turn("conversation-A", "a1"))

    assert _cached_tags(tmp_path) == {"conversation-A": "conversation-A:a1"}

    restarted = await _restarted(tmp_path)
    await restarted.send(OutgoingMessage(content="hello", reply_to="conversation-A"))
    assert _sent_tag(restarted) == "conversation-A:a1"


async def test_heartbeat_after_a_crash_goes_to_whoever_spoke_last(tmp_path: Path) -> None:
    # A is learned first, B second, then A speaks again. Persisting only when
    # a key is new would leave B last on disk, and the heartbeat would land
    # in B's chat after the restart.
    channel = _channel(tmp_path)
    await channel._on_turn(_turn("conversation-A", "a1"))
    await channel._on_turn(_turn("conversation-B", "b1"))
    await channel._on_turn(_turn("conversation-A", "a2"))

    assert list(_cached_tags(tmp_path)) == ["conversation-B", "conversation-A"]

    restarted = await _restarted(tmp_path)
    await restarted.send(OutgoingMessage(content="heartbeat", reply_to=None))
    assert _sent_tag(restarted) == "conversation-A:a2"


async def test_a_known_conversation_keeps_its_latest_reference(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    await channel._on_turn(_turn("conversation-A", "a1"))
    await channel._on_turn(_turn("conversation-A", "a2"))

    assert _cached_tags(tmp_path) == {"conversation-A": "conversation-A:a2"}


async def test_a_failed_save_does_not_drop_the_inbound_message(tmp_path: Path) -> None:
    # A regular file where the state directory should be makes every save
    # fail with an OSError.
    (tmp_path / "state").write_text("not a directory", encoding="utf-8")
    channel = _channel(tmp_path)

    with structlog.testing.capture_logs() as captured:
        await channel._on_turn(_turn("conversation-A", "a1"))

    incoming = await channel.receive()
    assert incoming.content == "hi"
    assert list(channel._references) == ["conversation-A"]
    assert [e["event"] for e in captured if e["log_level"] == "warning"] == [
        "msteams.cache_save_failed"
    ]


async def test_an_interrupted_save_leaves_the_previous_cache_loadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = _channel(tmp_path)
    await channel._on_turn(_turn("conversation-A", "a1"))
    before = _cache_file(tmp_path).read_bytes()

    def _killed_before_rename(src: str, dst: str) -> None:
        raise OSError("process killed mid-save")

    with monkeypatch.context() as patch:
        patch.setattr(msteams.os, "replace", _killed_before_rename)
        await channel._on_turn(_turn("conversation-B", "b1"))

    assert _cache_file(tmp_path).read_bytes() == before
    assert sorted(p.name for p in _cache_file(tmp_path).parent.iterdir()) == ["conversations.json"]
    restarted = await _restarted(tmp_path)
    assert list(restarted._references) == ["conversation-A"]


async def test_clean_stop_still_saves_the_cache(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    channel._references["conversation-A"] = _FakeReference("conversation-A:seeded")

    await channel.stop()

    assert _cached_tags(tmp_path) == {"conversation-A": "conversation-A:seeded"}
