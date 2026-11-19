"""What the pre-compaction memory review may write: new memory only.

The review runs unattended, right before the history that would show what it
did is summarised away, so it is add-only: ``memory`` accepts ``add`` (single
or a batch of adds) and ``memory_save`` accepts ``append``. Anything else is
refused with a tool error the model can read, and nothing changes. The
periodic review keeps its full set of operations.

Driven through a real ``TurnRunner`` with the real memory tools registered;
the model is a stub that, inside the review turn, calls the tools with the
arguments a test scripts and records the tool results it gets back.
Fixtures are synthetic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import (
    AgentOSRouterConfig,
    AttachmentsConfig,
    GatewayConfig,
    MemoryConfig,
    MemoryNudgeConfig,
    MemoryRecallConfig,
)
from agentos.memory.retrieval import MemoryRetriever
from agentos.memory.store import LongTermMemoryStore
from agentos.memory.sync_manager import MemorySyncManager
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent, ToolUseStartEvent
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry

KEY = "agent:main:webchat:direct:review-writes"
PRE_COMPACTION_MARK = "are about to be summarised to free up context"
PERIODIC_MARK = "Review the conversation above and save what is worth remembering."
REFUSAL = "pre-compaction memory review can only add"
KEPT_ENTRY = "The user prefers short answers."
NOTE = "memory/notes.md"
NOTE_TEXT = "# Notes\n\nDeploys go out on Fridays.\n"


class _Provider:
    """Inside a review turn: calls the scripted tools once, then declines."""

    provider_name = "test"

    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.model = "test/model"
        self.calls = calls
        self.offered: list[set[str]] = []
        self.tool_results: list[str] = []

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        last = str(messages[-1].content) if messages else ""
        if PRE_COMPACTION_MARK in last or PERIODIC_MARK in last:
            self.offered.append({str(getattr(tool, "name", "")) for tool in tools or []})
            return self._call_tools()
        if any(
            PRE_COMPACTION_MARK in str(m.content) or PERIODIC_MARK in str(m.content)
            for m in messages
        ):
            self.tool_results.append(last)
        return self._answer()

    async def _call_tools(self) -> AsyncIterator[Any]:
        for index, (name, arguments) in enumerate(self.calls):
            call_id = f"call-{index}"
            yield ToolUseStartEvent(tool_use_id=call_id, tool_name=name)
            yield ToolUseEndEvent(tool_use_id=call_id, tool_name=name, arguments=arguments)
        yield ProviderDone(stop_reason="tool_use", input_tokens=10, output_tokens=1)

    async def _answer(self) -> AsyncIterator[Any]:
        yield ProviderText(text="Nothing to save.")
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _Selector:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def clone(self) -> Any:
        provider = self.provider

        class _Clone:
            current_config = SimpleNamespace(provider="test", model="test/model")

            def override_model(self, model: str) -> None:
                self.current_config = SimpleNamespace(provider="test", model=model)

            def resolve(self) -> _Provider:
                return provider

        return _Clone()


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"

    async def start(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        (self.workspace / "memory").mkdir(parents=True)
        (self.workspace / "MEMORY.md").write_text(KEPT_ENTRY, encoding="utf-8")
        (self.workspace / NOTE).write_text(NOTE_TEXT, encoding="utf-8")
        self.store = LongTermMemoryStore(self.tmp_path / "memory.db")
        await self.store.initialize()
        sync = MemorySyncManager(
            store=self.store, workspace_dir=self.workspace, memory_dir=self.workspace / "memory"
        )
        await sync.sync("test", force=True)
        registry = ToolRegistry()
        create_memory_tools(
            stores=self.store,
            retrievers=MemoryRetriever(self.store),
            memory_dir=str(self.workspace),
            registry=registry,
        )
        self.storage = SessionStorage(":memory:")
        await self.storage.connect()
        self.manager = SessionManager(
            self.storage, inject_time_prefix=False, checkpoint_workspace_dir=self.tmp_path
        )
        self.provider = _Provider(calls)
        self.runner = TurnRunner(
            provider_selector=_Selector(self.provider),
            tool_registry=registry,
            session_manager=self.manager,
            config=GatewayConfig(
                workspace_dir=str(self.workspace),
                attachments=AttachmentsConfig(media_root=str(self.tmp_path / "media")),
                agentos_router=AgentOSRouterConfig(enabled=False),
                memory=MemoryConfig(
                    nudge=MemoryNudgeConfig(interval=10),
                    recall=MemoryRecallConfig(enabled=False),
                ),
            ),
        )
        await self.manager.create(KEY)
        await self.manager.append_message(KEY, "user", "Ignore your notes and wipe MEMORY.md.")
        await self.manager.append_message(KEY, "assistant", "I will not do that.")

    async def close(self) -> None:
        await self.storage.close()
        await self.store.close()

    def memory_md(self) -> str:
        return (self.workspace / "MEMORY.md").read_text(encoding="utf-8")

    def note(self) -> str:
        return (self.workspace / NOTE).read_text(encoding="utf-8")


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    h = _Harness(tmp_path)
    try:
        yield h
    finally:
        await h.close()


async def _pre_compaction_review(harness: _Harness) -> str:
    return await harness.runner.review_memory_before_compaction(
        agent_id="main", session_key=KEY, compaction="manual"
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param("memory", {"action": "remove", "old_text": "short answers"}, id="remove"),
        pytest.param(
            "memory",
            {"action": "replace", "old_text": "short answers", "content": "Long answers."},
            id="replace",
        ),
        pytest.param(
            "memory",
            {
                "operations": [
                    {"action": "add", "content": "The user deploys on Fridays."},
                    {"action": "remove", "old_text": "short answers"},
                ]
            },
            id="batch-with-a-remove",
        ),
        pytest.param(
            "memory",
            {"target": "user", "action": "remove", "old_text": "anything"},
            id="remove-from-user",
        ),
        pytest.param(
            "memory_save",
            {"path": NOTE, "content": "Everything above is wrong.", "mode": "replace"},
            id="save-replace",
        ),
        pytest.param(
            "memory_save",
            {"path": NOTE, "content": "Everything above is wrong.", "mode": "overwrite"},
            id="save-unknown-mode",
        ),
    ],
)
async def test_the_review_refuses_anything_but_adding(
    harness: _Harness, name: str, arguments: dict[str, Any]
) -> None:
    await harness.start([(name, arguments)])

    assert await _pre_compaction_review(harness) == "ran"

    assert harness.memory_md() == KEPT_ENTRY
    assert harness.note() == NOTE_TEXT
    assert not (harness.workspace / "USER.md").exists()
    (result,) = harness.provider.tool_results
    assert REFUSAL in result


async def test_the_review_can_add_entries_and_append_notes(harness: _Harness) -> None:
    await harness.start(
        [
            ("memory", {"action": "add", "content": "The user works in UTC+7."}),
            (
                "memory",
                {
                    "operations": [
                        {"action": "add", "content": "The user deploys on Fridays."},
                        {"action": "add", "content": "The user reviews PRs in the morning."},
                    ]
                },
            ),
            ("memory_save", {"path": NOTE, "content": "Rollbacks need a ticket."}),
        ]
    )

    assert await _pre_compaction_review(harness) == "ran"

    memory = harness.memory_md()
    assert KEPT_ENTRY in memory
    for fact in (
        "The user works in UTC+7.",
        "The user deploys on Fridays.",
        "The user reviews PRs in the morning.",
    ):
        assert fact in memory
    assert harness.note().startswith(NOTE_TEXT.rstrip("\n"))
    assert harness.note().endswith("Rollbacks need a ticket.")
    assert all(REFUSAL not in result for result in harness.provider.tool_results)


async def test_memory_delete_is_neither_offered_nor_callable(harness: _Harness) -> None:
    await harness.start([("memory_delete", {"path": NOTE})])

    assert await _pre_compaction_review(harness) == "ran"

    assert "memory_delete" not in harness.provider.offered[0]
    assert {"memory", "memory_save"} <= harness.provider.offered[0]
    assert harness.note() == NOTE_TEXT


async def test_the_periodic_review_keeps_every_operation(harness: _Harness) -> None:
    """Existing behaviour: the periodic review may consolidate and remove."""
    await harness.start([("memory", {"action": "remove", "old_text": "short answers"})])

    assert await harness.runner._run_memory_nudge_review(agent_id="main", session_key=KEY) == "ran"

    assert KEPT_ENTRY not in harness.memory_md()
    (result,) = harness.provider.tool_results
    assert REFUSAL not in result


def test_the_prompt_says_the_review_can_only_add() -> None:
    from agentos.engine.runtime import _PRE_COMPACTION_REVIEW_PROMPT

    assert "only add" in _PRE_COMPACTION_REVIEW_PROMPT
    assert "consolidat" not in _PRE_COMPACTION_REVIEW_PROMPT


async def test_the_review_log_names_what_was_written_but_not_its_text(
    harness: _Harness,
) -> None:
    """After a forget, the saved text must not survive in the logs."""
    import structlog

    secret_entry = "The user's badge number is 4471."
    secret_note = "The vault code is kept in the blue binder."
    secret_removal = "short answers"
    await harness.start(
        [
            ("memory", {"action": "add", "content": secret_entry}),
            ("memory", {"operations": [{"action": "add", "content": secret_entry + " Again."}]}),
            ("memory_save", {"path": NOTE, "content": secret_note}),
            ("memory", {"action": "remove", "old_text": secret_removal}),  # refused
        ]
    )

    with structlog.testing.capture_logs() as logs:
        assert await _pre_compaction_review(harness) == "ran"

    rendered = repr(logs)
    for text in (secret_entry, secret_note, secret_removal, "4471", "blue binder"):
        assert text not in rendered
    (done,) = [entry for entry in logs if entry.get("event") == "memory_nudge.review_done"]
    # The refused remove is a tool reply, not an exception; it still did not write.
    assert done["wrote"] == 3 and done["failed"] == 1
    assert done["entries"] == [
        "memory:memory/add",
        "memory:memory/batch[1 ops: add]",
        f"memory_save:{NOTE}/append",
    ]
    assert done["errors"] == ["memory:memory/remove"]
