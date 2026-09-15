"""The auto-publish backstop must match a filename, not a substring of one.

``auto_publish_omitted_workspace_artifacts`` publishes a deliverable the model
wrote but forgot to publish, gated on the file being "named in the assistant's
final text". Plain containment made one filename match inside a longer one —
``data.json`` is a substring of ``metadata.json`` — so a reply that named a
different file shipped an intermediate the model never mentioned.

The turn-level pair at the bottom runs both directions through ``TurnRunner``,
so the negative cannot pass vacuously on a misconfigured context.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.artifact_delivery import _text_mentions_written_file
from agentos.engine.runtime import TurnRunner
from agentos.engine.types import ArtifactEvent
from agentos.gateway.config import AgentOSRouterConfig, AttachmentsConfig, GatewayConfig
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import Message, ModelInfo
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.builtin import filesystem
from agentos.tools.registry import ToolRegistry, ToolSpec
from agentos.tools.types import CallerKind, ToolContext

_RECORD = {
    "relative_path": "reports/out.csv",
    "path": r"C:\ws\reports\out.csv",
    "name": "out.csv",
}


@pytest.mark.parametrize(
    "text",
    [
        "Created out.csv for you.",
        "Saved to `out.csv`.",
        'Wrote "out.csv" to disk.',
        "[out.csv](/dl) is ready",
        "**out.csv** updated",
        "See reports/out.csv",
        "See reports\\out.csv",
        "Written (out.csv).",
        "out.csv",
    ],
)
def test_a_named_file_is_still_recognised(text: str) -> None:
    assert _text_mentions_written_file(text, _RECORD) is True


@pytest.mark.parametrize(
    "text",
    [
        "I refreshed checkout.csv instead.",
        "See handout.csv for the summary.",
        "Look at my-out.csv please.",
        "Backed up to out.csv.bak already.",
        "Wrote out.csvx by mistake.",
        "Nothing relevant here.",
    ],
)
def test_a_longer_filename_does_not_count_as_a_mention(text: str) -> None:
    assert _text_mentions_written_file(text, _RECORD) is False


def test_the_reported_pair(tmp_path) -> None:
    """``data.json`` sits inside ``metadata.json`` — the shape that started this."""
    record = {"relative_path": "data.json", "path": "/ws/data.json", "name": "data.json"}

    assert _text_mentions_written_file("I refreshed metadata.json.", record) is False
    assert _text_mentions_written_file("I refreshed data.json.", record) is True


class _Provider:
    """Writes ``data.json``, then says whatever the test asked it to say."""

    provider_name = "test"

    def __init__(self, final_text: str) -> None:
        self.calls = 0
        self.model = "test/model"
        self._final_text = final_text

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="w1", tool_name="write_file")
            yield ProviderToolUseEnd(
                tool_use_id="w1",
                tool_name="write_file",
                arguments={"path": "data.json", "content": '{"rows": 1}'},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=self._final_text)
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[ModelInfo]:
        return []


class _SelectorClone:
    current_config = SimpleNamespace(model="test/model")

    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)
        self.provider.model = model

    def resolve(self) -> _Provider:
        return self.provider


class _ProviderSelector:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    write_file = filesystem.write_file.__wrapped__.__wrapped__  # type: ignore[attr-defined]
    registry.register(
        ToolSpec(
            name="write_file",
            description="Write a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
        write_file,
    )
    return registry


async def _artifacts_for_final_text(tmp_path, final_text: str, key: str) -> list[ArtifactEvent]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = f"agent:main:webchat:{key}"
    await manager.create(session_key)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(_Provider(final_text)),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path / "workspace"),
        allowed_tools={"write_file"},
        elevated="full",
    )
    try:
        events = [
            event
            async for event in runner.run(
                "crunch the numbers",
                session_key,
                tool_context=tool_context,
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]
        return [event for event in events if isinstance(event, ArtifactEvent)]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_a_turn_publishes_the_file_its_reply_names(tmp_path) -> None:
    """Positive control: the backstop still fires when the reply names the file."""
    artifacts = await _artifacts_for_final_text(tmp_path, "Wrote data.json for you.", "named")

    assert [artifact.name for artifact in artifacts] == ["data.json"]


@pytest.mark.asyncio
async def test_a_turn_does_not_publish_a_file_only_named_inside_another(tmp_path) -> None:
    artifacts = await _artifacts_for_final_text(
        tmp_path, "I refreshed metadata.json with the new schema.", "substring"
    )

    assert artifacts == []
