"""The memory review that runs once before a compaction summarises a session.

The periodic memory review gets a second trigger: right before a proactive or
manual compaction of a private conversation. Turns run through a real
``TurnRunner`` over SQLite sessions with the real proactive scheduler and the
real ``sessions.contextCompact`` handler; only the model is a stub, and it
records what it was sent and in which order.

The property every test here leans on: the compaction comes out the same
whether the review saved something, saved nothing, failed, timed out or was
skipped. Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from agentos import run_kinds
from agentos.engine import pre_compaction_review
from agentos.engine.compaction_coordinator import CompactionCoordinator
from agentos.engine.runtime import (
    _MEMORY_REVIEW_TOOL_NAMES,
    _PRE_COMPACTION_REVIEW_PROMPT,
    TurnRunner,
)
from agentos.gateway.config import (
    AgentOSRouterConfig,
    AttachmentsConfig,
    CompactionLlmConfig,
    GatewayConfig,
    MemoryConfig,
    MemoryNudgeConfig,
    MemoryRecallConfig,
)
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import ErrorEvent as ProviderError
from agentos.provider import Message
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.session.keys import allows_private_memory_prompt_injection
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, InteractionMode, ToolContext

KEY = "agent:main:webchat:direct:precompact"
SUMMARY = "SUMMARY: the user is moving the billing service to Postgres"
# Distinctive slices of the two review prompts (no line breaks inside).
PRE_COMPACTION_MARK = "are about to be summarised to free up context"
PERIODIC_MARK = "Review the conversation above and save what is worth remembering."
NOTHING = "Nothing to save."

INELIGIBLE_KEYS = [
    pytest.param("cron:nightly:run:abc", id="cron"),
    pytest.param("agent:main:subagent:run-9", id="subagent"),
    pytest.param("agent:main:discord:group:123", id="group"),
    pytest.param("agent:main:slack:channel:C42", id="channel"),
]


# -- the model -----------------------------------------------------------------


class _Provider:
    """Answers user turns, the review and the summarizer, and logs the order.

    ``review_mode`` decides the review's reply: ``ok`` (declines), ``hang``
    (blocks until cancelled), ``gate`` (blocks until ``review_gate`` is set),
    ``error`` (a non-retryable provider error) or ``raise`` (the stream
    raises). ``timeline`` records ``turn``, ``review``, ``review_done``,
    ``review_cancelled``, ``periodic`` and ``summary`` in call order.
    """

    provider_name = "test"

    def __init__(self, *, review_mode: str = "ok") -> None:
        self.model = "test/model"
        self.review_mode = review_mode
        self.review_gate = asyncio.Event()
        self.review_started = asyncio.Event()
        self.timeline: list[str] = []
        self.turn_requests: list[list[Message]] = []
        self.turn_systems: list[str] = []
        self.review_requests: list[list[Message]] = []
        self.review_systems: list[str] = []
        self.summarizer_requests: list[list[Message]] = []

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        system = str(getattr(config, "system", "") or "")
        if system.startswith("You are a conversation compactor"):
            self.summarizer_requests.append(list(messages))
            self.timeline.append("summary")
            return self._reply(SUMMARY)
        last = str(messages[-1].content) if messages else ""
        if PRE_COMPACTION_MARK in last:
            self.review_requests.append(list(messages))
            self.review_systems.append(system)
            self.timeline.append("review")
            return self._review()
        if PERIODIC_MARK in last:
            self.timeline.append("periodic")
            return self._review()
        self.turn_requests.append(list(messages))
        self.turn_systems.append(system)
        self.timeline.append("turn")
        return self._reply(f"answer {len(self.turn_requests)}")

    async def _reply(self, text: str) -> AsyncIterator[Any]:
        yield ProviderText(text=text)
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=1)

    async def _review(self) -> AsyncIterator[Any]:
        self.review_started.set()
        try:
            if self.review_mode == "hang":
                await asyncio.Event().wait()
            if self.review_mode == "gate":
                await self.review_gate.wait()
        except asyncio.CancelledError:
            self.timeline.append("review_cancelled")
            raise
        if self.review_mode == "error":
            yield ProviderError(code="401", message="invalid api key")
            return
        if self.review_mode == "raise":
            raise RuntimeError("review provider exploded")
        yield ProviderText(text=NOTHING)
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=1)
        self.timeline.append("review_done")

    async def list_models(self) -> list[Any]:
        return []


class _SelectorClone:
    current_config = SimpleNamespace(model="test/model")

    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)

    def resolve(self) -> _Provider:
        return self.provider


class _Selector:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


class _Catalog:
    def resolve_max_tokens(self, model_id, user_override=0, provider_name=""):  # noqa: ARG002
        return 1024

    def resolve_context_window(self, model_id, provider_name=""):  # noqa: ARG002
        return 40_000

    def get_capabilities(self, model_id, provider_name="", base_url=""):  # noqa: ARG002
        return None


_AUX_PROVIDER: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _auxiliary_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the summarizer's auxiliary client to the test provider."""
    from agentos.provider import auxiliary

    monkeypatch.setattr(
        auxiliary,
        "_client",
        auxiliary.AuxiliaryClient(
            config={"provider": "opencap", "model": "cheap-model"},
            provider_factory=lambda **_kwargs: _AUX_PROVIDER["provider"],
        ),
    )


# -- the runtime -----------------------------------------------------------------


@pytest.fixture
async def storages() -> AsyncIterator[list[Any]]:
    """Session storages (and memory indexes) a test opened; closed at teardown."""
    opened: list[Any] = []
    yield opened
    for storage in opened:
        await storage.close()


async def _manager(tmp_path: Path, storages: list[SessionStorage]) -> SessionManager:
    storage = SessionStorage(":memory:")
    await storage.connect()
    storages.append(storage)
    return SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)


class _Capture:
    """A turn-capture service that records what it was asked to capture."""

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    async def capture_turn(self, **kwargs: Any) -> None:
        self.captured.append(kwargs)


def _runner(
    provider: _Provider,
    manager: SessionManager,
    tmp_path: Path,
    *,
    nudge: MemoryNudgeConfig | None = None,
    capture: _Capture | None = None,
    retriever: Any = None,
    **compaction: Any,
) -> TurnRunner:
    _AUX_PROVIDER["provider"] = provider
    compaction.setdefault("proactive_idle_seconds", 0)
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("The user prefers short answers.\n", encoding="utf-8")
    return TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=ToolRegistry(),
        session_manager=manager,
        model_catalog=_Catalog(),
        config=GatewayConfig(
            workspace_dir=str(workspace),
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
            preflight_compact_ratio=0.85,
            compaction=CompactionLlmConfig(**compaction),
            memory=MemoryConfig(
                nudge=nudge or MemoryNudgeConfig(),
                recall=MemoryRecallConfig(mode="off"),
            ),
        ),
        turn_capture_services={"main": capture} if capture is not None else None,
        memory_retrievers={"main": retriever} if retriever is not None else None,
    )


async def _seed(manager: SessionManager, key: str, *, rounds: int = 2, chars: int = 4500) -> None:
    if await manager.get_session(key) is None:
        await manager.create(key)
    for index in range(rounds):
        await manager.append_message(key, "user", f"old question {index} " + "q" * chars)
        await manager.append_message(key, "assistant", f"old answer {index} " + "a" * chars)


async def _turn(
    runner: TurnRunner,
    manager: SessionManager,
    key: str,
    text: str,
    tmp_path: Path,
    *,
    capture: bool = False,
) -> list[Any]:
    await manager.append_message(key, "user", text)
    return [
        event
        async for event in runner.run(
            text,
            key,
            tool_context=ToolContext(caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path)),
            history_has_persisted_user=True,
            no_memory_capture=not capture,
            run_kind=run_kinds.SESSION_TURN,
        )
    ]


def _spy_runs(runner: TurnRunner, timeline: list[str]) -> list[dict[str, Any]]:
    """Record every turn the runner starts, and mark when each one ends."""
    runs: list[dict[str, Any]] = []
    original = runner.run

    async def _spy(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        runs.append(kwargs)
        kind = kwargs.get("run_kind", "default")
        try:
            async for event in original(*args, **kwargs):
                yield event
        finally:
            timeline.append(f"run_end:{kind}")

    runner.run = _spy  # type: ignore[method-assign]
    return runs


def _review_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == pre_compaction_review.EVENT]


def _assert_quiet_skip(logs: list[dict[str, Any]], reason: str) -> None:
    """``disabled`` / ``ineligible`` are debug lines; a worker may filter them out."""
    events = _review_events(logs)
    assert len(events) <= 1
    assert all((e["outcome"], e["reason"]) == ("skipped", reason) for e in events)


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _random_key(key: str) -> bool:
    """Ids and clock readings, which differ between two otherwise equal runs."""
    return key in {"id", "ts", "timestamp"} or key.endswith(("_id", "_ids", "_at"))


async def _compaction_state(manager: SessionManager, key: str) -> dict[str, Any]:
    """What a compaction left behind, minus random ids and timestamps."""

    def _record(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _record(v) for k, v in value.items() if not _random_key(k)}
        if isinstance(value, list):
            return [_record(v) for v in value]
        return value

    rows = await manager.get_transcript(key)
    return {
        "summaries": [s.summary_text for s in await manager.get_summaries(key)],
        "rows": [(row.role, row.content, _record(row.provider_messages)) for row in rows],
    }


def _dump(messages: list[Message]) -> list[str]:
    return [json.dumps(m.model_dump(mode="json"), sort_keys=True) for m in messages]


async def _proactive_run(
    tmp_path: Path, storages: list[SessionStorage], *, review_mode: str, nudge: MemoryNudgeConfig
) -> tuple[_Provider, dict[str, Any], list[dict[str, Any]]]:
    """One user turn that crosses the proactive threshold, its compaction, a next turn."""
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode=review_mode)
    runner = _runner(provider, manager, tmp_path, nudge=nudge)
    await _seed(manager, KEY)
    with structlog.testing.capture_logs() as logs:
        await _turn(runner, manager, KEY, "deploy status?", tmp_path)
        await runner._proactive_compaction.wait_idle(KEY)
        state = await _compaction_state(manager, KEY)
        await _turn(runner, manager, KEY, "and the logs?", tmp_path)
        await runner._proactive_compaction.wait_idle(KEY)
    await _drain_reviews(runner)
    state["summarizer_input"] = [_dump(request) for request in provider.summarizer_requests]
    state["next_turn_request"] = _dump(provider.turn_requests[-1])
    return provider, state, logs


async def _drain_reviews(runner: TurnRunner) -> None:
    pending = list(runner._memory_nudge_pending)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


# -- 3.1: the review's prompt, tools and provenance -----------------------------


class _ReviewRunner:
    """Just enough of TurnRunner to drive one review turn and capture its call."""

    def __init__(self) -> None:
        memory = SimpleNamespace(nudge=MemoryNudgeConfig())
        self._config = SimpleNamespace(memory=memory)
        self.calls: list[dict[str, Any]] = []

    _memory_nudge_config = TurnRunner._memory_nudge_config
    _run_memory_nudge_review = TurnRunner._run_memory_nudge_review

    def _resolve_memory_source_dir(self, agent_id: str) -> str:
        return f"/memory/{agent_id}"

    async def run(self, message: str, session_key: str, **kwargs: Any) -> Any:
        self.calls.append({"message": message, "session_key": session_key, **kwargs})
        return
        yield  # pragma: no cover - makes this an async generator


async def test_pre_compaction_review_tools_prompt_and_provenance() -> None:
    """The allowlist and turn shape; what the handlers refuse inside the review
    is driven through the real tools in test_memory_pre_compaction_review_writes."""
    from agentos.engine.runtime import _PRE_COMPACTION_MEMORY_REVIEW

    runner = _ReviewRunner()
    outcome = await runner._run_memory_nudge_review(
        agent_id="main", session_key=KEY, variant=_PRE_COMPACTION_MEMORY_REVIEW
    )

    assert outcome == "ran"
    (call,) = runner.calls
    ctx = call["tool_context"]
    assert ctx.allowed_tools == {"memory", "memory_save", "memory_get", "memory_search"}
    assert "memory_delete" not in ctx.allowed_tools
    assert ctx.interaction_mode is InteractionMode.UNATTENDED
    assert ctx.source_kind == "memory_nudge"
    assert call["run_kind"] == run_kinds.MEMORY_NUDGE
    assert call["input_mode"] == "system_event"
    assert call["persist_input"] is False
    assert call["no_memory_capture"] is True
    assert call["input_provenance"] == {"kind": "memory_nudge", "trigger": "pre_compaction"}
    assert call["message"] == _PRE_COMPACTION_REVIEW_PROMPT
    assert PRE_COMPACTION_MARK in _PRE_COMPACTION_REVIEW_PROMPT
    assert "not already in memory" in _PRE_COMPACTION_REVIEW_PROMPT
    assert NOTHING in _PRE_COMPACTION_REVIEW_PROMPT


async def test_periodic_review_keeps_its_allowlist_prompt_and_provenance() -> None:
    runner = _ReviewRunner()
    await runner._run_memory_nudge_review(agent_id="main", session_key=KEY)

    (call,) = runner.calls
    assert _MEMORY_REVIEW_TOOL_NAMES == frozenset(
        {"memory", "memory_save", "memory_get", "memory_search", "memory_delete"}
    )
    assert call["tool_context"].allowed_tools == set(_MEMORY_REVIEW_TOOL_NAMES)
    assert call["input_provenance"] == {"kind": "memory_nudge"}
    assert PERIODIC_MARK in call["message"]


def test_before_compaction_is_on_by_default_and_validated() -> None:
    assert MemoryNudgeConfig().before_compaction is True
    assert MemoryConfig.model_validate({"nudge": {"before_compaction": False}}).nudge == (
        MemoryNudgeConfig(before_compaction=False)
    )
    with pytest.raises(ValueError):
        MemoryNudgeConfig.model_validate({"before_compaction_typo": True})


# -- 3.2: proactive compaction ----------------------------------------------------


async def test_proactive_compaction_reviews_once_and_finishes_before_the_summary(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    runs = _spy_runs(runner, provider.timeline)
    await _seed(manager, KEY)

    with structlog.testing.capture_logs() as logs:
        await _turn(runner, manager, KEY, "deploy status?", tmp_path)
        await runner._proactive_compaction.wait_idle(KEY)

    assert provider.timeline == [
        "turn",
        f"run_end:{run_kinds.SESSION_TURN}",
        "review",
        "review_done",
        f"run_end:{run_kinds.MEMORY_NUDGE}",
        "summary",
    ]
    (review,) = [run for run in runs if run["run_kind"] == run_kinds.MEMORY_NUDGE]
    assert review["input_provenance"] == {"kind": "memory_nudge", "trigger": "pre_compaction"}
    assert "memory_delete" not in review["tool_context"].allowed_tools
    # The review read the conversation that was about to be summarised.
    reviewed = "\n".join(str(m.content) for m in provider.review_requests[0])
    assert "old question 0" in reviewed and "deploy status?" in reviewed
    (event,) = _review_events(logs)
    assert event["outcome"] == "ran"
    assert event["compaction"] == "proactive_background"
    assert event["agent_id"] == "main" and event["session_key"] == KEY
    assert len(await manager.get_summaries(KEY)) == 1


async def test_turned_off_compacts_with_no_review(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    provider, state, logs = await _proactive_run(
        tmp_path, storages, review_mode="ok", nudge=MemoryNudgeConfig(before_compaction=False)
    )

    assert provider.review_requests == []
    assert provider.timeline.count("summary") == 1
    assert len(state["summaries"]) == 1
    _assert_quiet_skip(logs, "disabled")


async def test_nudge_switched_off_disables_the_pre_compaction_review_too(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    provider, _state, logs = await _proactive_run(
        tmp_path, storages, review_mode="ok", nudge=MemoryNudgeConfig(enabled=False)
    )

    assert provider.review_requests == []
    _assert_quiet_skip(logs, "disabled")


@pytest.mark.parametrize(
    ("nudge", "reason"),
    [
        pytest.param(MemoryNudgeConfig(), None, id="on"),
        pytest.param(MemoryNudgeConfig(before_compaction=False), "disabled", id="switch-off"),
        pytest.param(MemoryNudgeConfig(enabled=False), "disabled", id="nudge-off"),
        pytest.param(MemoryNudgeConfig(interval=0), "disabled", id="interval-zero"),
    ],
)
async def test_eligibility_follows_the_periodic_review(
    tmp_path: Path, storages: list[SessionStorage], nudge: MemoryNudgeConfig, reason: str | None
) -> None:
    manager = await _manager(tmp_path, storages)
    runner = _runner(_Provider(), manager, tmp_path, nudge=nudge)

    assert runner._pre_compaction_review_skip_reason(KEY) == reason


async def test_the_switch_is_read_live(tmp_path: Path, storages: list[SessionStorage]) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    await manager.create(KEY)

    runner._config.memory.nudge.before_compaction = False
    assert (
        await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )
        == "skipped"
    )
    runner._config.memory.nudge.before_compaction = True
    assert (
        await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )
        == "ran"
    )


@pytest.mark.parametrize(
    ("review_mode", "outcome"),
    [
        pytest.param("ok", "ran", id="nothing-to-save"),
        pytest.param("hang", "timeout", id="timeout"),
        pytest.param("error", "error", id="provider-error"),
        pytest.param("raise", "error", id="stream-raises"),
    ],
)
async def test_proactive_compaction_is_identical_whatever_the_review_did(
    tmp_path: Path, storages: list[SessionStorage], review_mode: str, outcome: str
) -> None:
    """Same summarizer input, same persisted result, same next turn."""
    _, baseline, _ = await _proactive_run(
        tmp_path / "off",
        storages,
        review_mode="ok",
        nudge=MemoryNudgeConfig(before_compaction=False),
    )
    provider, reviewed, logs = await _proactive_run(
        tmp_path / "on",
        storages,
        review_mode=review_mode,
        nudge=MemoryNudgeConfig(timeout_seconds=0.3),
    )

    assert len(provider.review_requests) == 1
    assert reviewed == baseline
    (event,) = [e for e in _review_events(logs) if e["outcome"] != "skipped"]
    assert event["outcome"] == outcome
    if review_mode == "hang":
        assert "review_cancelled" in provider.timeline
        assert event["timeout_seconds"] == 0.3
    # Never in the transcript the next turn replays.
    sent = "\n".join(reviewed["next_turn_request"])
    assert PRE_COMPACTION_MARK not in sent
    assert NOTHING not in sent and "invalid api key" not in sent


async def test_nothing_to_save_changes_no_memory_file(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode="ok")
    runner = _runner(provider, manager, tmp_path)
    await _seed(manager, KEY)
    before = _files(tmp_path / "workspace")

    await _turn(runner, manager, KEY, "deploy status?", tmp_path)
    await runner._proactive_compaction.wait_idle(KEY)

    assert provider.timeline.count("review_done") == 1
    assert len(await manager.get_summaries(KEY)) == 1
    assert _files(tmp_path / "workspace") == before


async def test_a_turn_during_the_review_cancels_it_with_the_job(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    """The user's next message wins: the review stops, nothing is rewritten under the turn."""
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode="gate")
    runner = _runner(provider, manager, tmp_path)
    scheduler = runner._proactive_compaction
    await _seed(manager, KEY)

    await _turn(runner, manager, KEY, "deploy status?", tmp_path)
    await asyncio.wait_for(provider.review_started.wait(), timeout=2)
    job = scheduler._sessions[KEY].task
    assert job is not None and not job.done()
    # The review holds no session lock: the user's message is not stalled.
    assert not runner.get_session_lock(KEY).locked()

    second = await _turn(runner, manager, KEY, "and the logs?", tmp_path)
    await asyncio.wait_for(asyncio.gather(job, return_exceptions=True), timeout=2)

    assert job.done()
    assert "review_cancelled" in provider.timeline
    assert provider.summarizer_requests == []
    assert any(getattr(event, "kind", "") == "done" for event in second)
    # The second turn ran on the untouched history.
    assert str(provider.turn_requests[-1][0].content).startswith("old question 0")
    # A cancelled review was billed, like a cancelled summary.
    assert scheduler._backoff[KEY] == 1

    # The next job reviews again and compacts.
    provider.review_gate.set()
    await scheduler.wait_idle(KEY)
    assert len(await manager.get_summaries(KEY)) == 1
    await _drain_reviews(runner)


async def test_no_headroom_when_the_finished_turn_is_already_at_the_trigger(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    await manager.create(KEY)

    with structlog.testing.capture_logs() as logs:
        await runner._proactive_compaction._review_memory(
            KEY,
            SimpleNamespace(
                agent_id="main", estimated_tokens=7_300, limit_tokens=7_200, generation=0
            ),
        )

    assert provider.review_requests == []
    (event,) = _review_events(logs)
    assert (event["outcome"], event["reason"], event["compaction"]) == (
        "skipped",
        "no_headroom",
        "proactive_background",
    )


@pytest.mark.parametrize("key", INELIGIBLE_KEYS)
async def test_ineligible_sessions_are_never_reviewed(
    tmp_path: Path, storages: list[SessionStorage], key: str
) -> None:
    assert not allows_private_memory_prompt_injection(key)
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    await _seed(manager, key)

    with structlog.testing.capture_logs() as logs:
        outcome = await runner.review_memory_before_compaction(
            agent_id="main", session_key=key, compaction="manual"
        )

    assert outcome == "skipped"
    assert provider.review_requests == []
    assert runner._pre_compaction_review_skip_reason(key) == "ineligible"
    _assert_quiet_skip(logs, "ineligible")


async def test_a_group_chat_crossing_the_proactive_threshold_is_compacted_unreviewed(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    key = "agent:main:discord:group:123"
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    await _seed(manager, key)

    await _turn(runner, manager, key, "deploy status?", tmp_path)
    await runner._proactive_compaction.wait_idle(key)

    assert provider.timeline == ["turn", "summary"]
    assert len(await manager.get_summaries(key)) == 1


# -- 3.2: skip reasons --------------------------------------------------------------


def _note(runner: TurnRunner, key: str = KEY) -> bool:
    return runner._note_turn_for_memory_nudge(
        agent_id="main",
        session_key=key,
        input_mode="user",
        run_kind=run_kinds.SESSION_TURN,
        no_memory_capture=False,
        turn_segments=[],
    )


async def test_a_periodic_review_one_turn_ago_skips_it_as_recent(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path, nudge=MemoryNudgeConfig(interval=2))
    await manager.create(KEY)
    assert [_note(runner), _note(runner)] == [False, True]  # the periodic review fired
    _note(runner)  # one user turn later

    with structlog.testing.capture_logs() as logs:
        outcome = await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="proactive_background"
        )

    assert outcome == "skipped"
    assert provider.review_requests == []
    (event,) = _review_events(logs)
    assert event["reason"] == "recent_review"


async def test_recent_means_the_last_two_user_turns(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path, nudge=MemoryNudgeConfig(interval=100))
    await manager.create(KEY)

    async def review() -> str:
        return await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )

    # No review has run for this session in this process: nothing is recent.
    _note(runner)
    assert await review() == "ran"
    # The review that just ran is recent for the next two user turns.
    assert await review() == "skipped"
    _note(runner)
    assert await review() == "skipped"
    _note(runner)
    assert await review() == "skipped"
    _note(runner)
    assert await review() == "ran"
    assert len(provider.review_requests) == 2


async def test_a_second_review_for_the_same_session_is_skipped_while_one_runs(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode="gate")
    runner = _runner(provider, manager, tmp_path)
    await manager.create(KEY)

    first = asyncio.create_task(
        runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="proactive_background"
        )
    )
    await asyncio.wait_for(provider.review_started.wait(), timeout=2)
    with structlog.testing.capture_logs() as logs:
        second = await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )
    provider.review_gate.set()

    assert second == "skipped"
    assert _review_events(logs)[0]["reason"] == "in_progress"
    assert await asyncio.wait_for(first, timeout=2) == "ran"
    assert len(provider.review_requests) == 1


class _Zombie:
    """A review that ignores cancellation, then tries to write memory.

    Stands in for ``_run_memory_nudge_review`` (so it runs inside the review
    scope the runner set up) and calls the real ``memory`` tool handler once
    released. Later calls return at once.
    """

    def __init__(self, tmp_path: Path, closing: list[Any]) -> None:
        self.tmp_path = tmp_path
        self.closing = closing
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.write_results: list[dict[str, Any]] = []

    async def tools(self) -> None:
        from agentos.memory.retrieval import MemoryRetriever
        from agentos.memory.store import LongTermMemoryStore
        from agentos.tools.builtin.memory_tools import create_memory_tools

        store = LongTermMemoryStore(self.tmp_path / "zombie.db")
        self.closing.append(store)
        await store.initialize()
        registry = ToolRegistry()
        create_memory_tools(
            stores=store,
            retrievers=MemoryRetriever(store),
            memory_dir=str(self.tmp_path / "workspace"),
            registry=registry,
        )
        self.memory = registry.get("memory").handler

    async def __call__(self, **_kwargs: Any) -> str:
        from agentos.tools.types import current_tool_context

        self.calls += 1
        if self.calls > 1:
            return "ran"
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self.release.wait()  # swallows the cancellation and keeps going
        token = current_tool_context.set(
            ToolContext(
                caller_kind=CallerKind.AGENT,
                agent_id="main",
                session_key=KEY,
                source_kind="memory_nudge",
            )
        )
        try:
            result = await self.memory(action="add", content="Written after the deadline.")
        finally:
            current_tool_context.reset(token)
        self.write_results.append(json.loads(result))
        return "ran"


async def test_the_deadline_holds_even_if_the_review_ignores_cancellation(
    tmp_path: Path, storages: list[SessionStorage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No receipt, no gate: compaction never waits past ``timeout_seconds``.

    The review left running is still the session's review: another one is not
    started beside it, and it cannot begin a memory write any more.
    """
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path, nudge=MemoryNudgeConfig(timeout_seconds=0.1))
    await manager.create(KEY)
    zombie = _Zombie(tmp_path, storages)
    await zombie.tools()
    monkeypatch.setattr(runner, "_run_memory_nudge_review", zombie)
    memory_before = (tmp_path / "workspace" / "MEMORY.md").read_text(encoding="utf-8")
    started = time.monotonic()

    with structlog.testing.capture_logs() as logs:
        outcome = await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )
        again = await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )

    assert outcome == "timeout"
    assert time.monotonic() - started < 1.0
    events = _review_events(logs)
    assert events[0]["outcome"] == "timeout"
    assert again == "skipped" and events[1]["reason"] == "in_progress"
    zombie.release.set()
    await asyncio.gather(*runner._memory_nudge_pending, return_exceptions=True)
    (write,) = zombie.write_results
    assert write["success"] is False and "has ended" in write["error"]
    assert (tmp_path / "workspace" / "MEMORY.md").read_text(encoding="utf-8") == memory_before
    assert ("main", KEY) not in runner._pre_compaction_reviews


async def test_a_review_cancelled_with_its_compaction_is_not_overlapped(
    tmp_path: Path, storages: list[SessionStorage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user turn cancels the proactive job; a manual compaction comes next."""
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    await manager.create(KEY)
    zombie = _Zombie(tmp_path, storages)
    await zombie.tools()
    monkeypatch.setattr(runner, "_run_memory_nudge_review", zombie)
    memory_before = (tmp_path / "workspace" / "MEMORY.md").read_text(encoding="utf-8")

    job = asyncio.create_task(
        runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="proactive_background"
        )
    )
    await asyncio.wait_for(zombie.started.wait(), timeout=2)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job

    with structlog.testing.capture_logs() as logs:
        manual = await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )

    assert manual == "skipped"
    assert _review_events(logs)[0]["reason"] == "in_progress"
    assert zombie.calls == 1
    zombie.release.set()
    await asyncio.gather(*runner._memory_nudge_pending, return_exceptions=True)
    (write,) = zombie.write_results
    assert write["success"] is False and "has ended" in write["error"]
    assert (tmp_path / "workspace" / "MEMORY.md").read_text(encoding="utf-8") == memory_before
    # Once it has finished, the next compaction gets its review again.
    assert (
        await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )
        == "ran"
    )
    assert zombie.calls == 2


# -- 3.2: the counter -------------------------------------------------------------


async def test_after_a_pre_compaction_review_the_periodic_one_is_a_full_interval_away(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path, nudge=MemoryNudgeConfig(interval=10))
    await manager.create(KEY)
    assert not any(_note(runner) for _ in range(8))  # two turns short of a review

    assert (
        await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )
        == "ran"
    )

    assert [_note(runner) for _ in range(10)] == [False] * 9 + [True]


async def test_a_skipped_review_leaves_the_counter_alone(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(
        provider,
        manager,
        tmp_path,
        nudge=MemoryNudgeConfig(interval=10, before_compaction=False),
    )
    await manager.create(KEY)
    assert not any(_note(runner) for _ in range(8))

    await runner.review_memory_before_compaction(
        agent_id="main", session_key=KEY, compaction="manual"
    )

    assert [_note(runner), _note(runner)] == [False, True]


def test_recency_lives_in_the_bounded_counter_dict() -> None:
    """No second per-session structure: eviction drops recency with the count."""
    from agentos.engine.runtime import _MAX_NUDGE_COUNTERS

    runner = TurnRunner.__new__(TurnRunner)
    runner._config = SimpleNamespace(memory=SimpleNamespace(nudge=MemoryNudgeConfig(interval=1000)))
    runner._memory_nudge_counters = {}
    for i in range(_MAX_NUDGE_COUNTERS + 50):
        runner._note_memory_review_ran("main", f"s{i}")
    assert len(runner._memory_nudge_counters) <= _MAX_NUDGE_COUNTERS + 1


# -- 3.2: manual compaction -----------------------------------------------------------


def _rpc_context(runner: TurnRunner | None, manager: SessionManager) -> RpcContext:
    ctx = RpcContext(conn_id="conn-1", config=GatewayConfig())
    ctx.session_manager = manager
    ctx.turn_runner = runner
    return ctx


async def _manual_compact(
    tmp_path: Path,
    storages: list[SessionStorage],
    *,
    review_mode: str,
    nudge: MemoryNudgeConfig,
    key: str = KEY,
    window: int = 8000,
    prepare: Callable[[TurnRunner], None] | None = None,
) -> tuple[_Provider, TurnRunner, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode=review_mode)
    runner = _runner(provider, manager, tmp_path, nudge=nudge)
    if prepare is not None:
        prepare(runner)
    await _seed(manager, key, rounds=6, chars=3000)
    with structlog.testing.capture_logs() as logs:
        res = await get_dispatcher().dispatch(
            "r1",
            "sessions.contextCompact",
            {"key": key, "contextWindowTokens": window},
            _rpc_context(runner, manager),
        )
    assert res.ok, res.error
    state = await _compaction_state(manager, key)
    await _drain_reviews(runner)
    return provider, runner, res.payload, state, logs


async def test_manual_compaction_reviews_before_it_compacts(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    provider, _runner_, payload, state, logs = await _manual_compact(
        tmp_path, storages, review_mode="ok", nudge=MemoryNudgeConfig()
    )

    assert payload["compacted"] is True
    assert provider.timeline == ["review", "review_done"]
    reviewed = "\n".join(str(m.content) for m in provider.review_requests[0])
    assert "old question 0" in reviewed
    (event,) = _review_events(logs)
    assert (event["outcome"], event["compaction"]) == ("ran", "manual")
    assert len(state["summaries"]) == 1


@pytest.mark.parametrize(
    ("review_mode", "outcome"),
    [
        pytest.param("ok", "ran", id="nothing-to-save"),
        pytest.param("hang", "timeout", id="timeout"),
        pytest.param("error", "error", id="provider-error"),
        pytest.param("raise", "error", id="stream-raises"),
    ],
)
async def test_manual_compaction_response_is_identical_whatever_the_review_did(
    tmp_path: Path, storages: list[SessionStorage], review_mode: str, outcome: str
) -> None:
    _, _, baseline_payload, baseline_state, _ = await _manual_compact(
        tmp_path / "off",
        storages,
        review_mode="ok",
        nudge=MemoryNudgeConfig(before_compaction=False),
    )
    provider, _, payload, state, logs = await _manual_compact(
        tmp_path / "on",
        storages,
        review_mode=review_mode,
        nudge=MemoryNudgeConfig(timeout_seconds=0.3),
    )

    assert len(provider.review_requests) == 1
    assert payload == baseline_payload
    assert state == baseline_state
    (event,) = _review_events(logs)
    assert event["outcome"] == outcome


class _OverBudget:
    """A usage tracker whose spend ceiling is already reached."""

    def check_budget_limits(self, session_key: str, config: Any) -> tuple[bool, str]:
        return True, "Daily spend limit reached."


class _NoProvider:
    """A provider selector that cannot resolve any provider."""

    def clone(self) -> Any:
        return SimpleNamespace(
            current_config=SimpleNamespace(model="test/model"),
            override_model=lambda _model: None,
            resolve=lambda: None,
        )


def _over_budget(runner: TurnRunner) -> None:
    runner._usage_tracker = _OverBudget()


def _without_provider(runner: TurnRunner) -> None:
    runner._provider_selector = _NoProvider()


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(_over_budget, id="spend-budget-refusal"),
        pytest.param(_without_provider, id="no-provider"),
    ],
)
async def test_a_review_refused_before_any_model_call_changes_nothing(
    tmp_path: Path, storages: list[SessionStorage], prepare: Callable[[TurnRunner], None]
) -> None:
    """The turn's two early exits: no error row for the compaction to summarise."""
    _, _, baseline_payload, baseline_state, _ = await _manual_compact(
        tmp_path / "off",
        storages,
        review_mode="ok",
        nudge=MemoryNudgeConfig(before_compaction=False),
        prepare=prepare,
    )
    provider, _, payload, state, logs = await _manual_compact(
        tmp_path / "on", storages, review_mode="ok", nudge=MemoryNudgeConfig(), prepare=prepare
    )

    assert provider.review_requests == []  # refused before the model was called
    (event,) = _review_events(logs)
    assert event["outcome"] == "error"
    assert payload == baseline_payload
    assert state == baseline_state
    assert not any(role == "system" for role, _, _ in state["rows"])


@pytest.mark.parametrize("key", INELIGIBLE_KEYS)
async def test_manual_compaction_of_an_ineligible_session_runs_no_review(
    tmp_path: Path, storages: list[SessionStorage], key: str
) -> None:
    provider, runner, payload, _, logs = await _manual_compact(
        tmp_path, storages, review_mode="ok", nudge=MemoryNudgeConfig(), key=key
    )

    assert payload["compacted"] is True
    assert provider.review_requests == []
    assert runner._pre_compaction_review_skip_reason(key) == "ineligible"
    _assert_quiet_skip(logs, "ineligible")


async def test_manual_compaction_reviews_without_holding_the_session_lock(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode="gate")
    runner = _runner(provider, manager, tmp_path)
    await _seed(manager, KEY, rounds=6, chars=3000)

    compaction = asyncio.create_task(
        get_dispatcher().dispatch(
            "r1",
            "sessions.contextCompact",
            {"key": KEY, "contextWindowTokens": 8000},
            _rpc_context(runner, manager),
        )
    )
    await asyncio.wait_for(provider.review_started.wait(), timeout=2)
    assert not runner.get_session_lock(KEY).locked()
    # A message can still be written while the review runs.
    await manager.append_message(KEY, "user", "still there?")
    provider.review_gate.set()
    res = await asyncio.wait_for(compaction, timeout=5)

    assert res.ok, res.error
    assert res.payload["compacted"] is True


# -- 3.2: forced compactions have no headroom ------------------------------------------


OVERFLOW = "This model's maximum context length is 6000 tokens"


class _OverflowingProvider(_Provider):
    """Rejects the first user-turn request as too long for the context."""

    def __init__(self) -> None:
        super().__init__()
        self.rejected = False

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        system = str(getattr(config, "system", "") or "")
        if not self.rejected and not system.startswith("You are a conversation compactor"):
            self.rejected = True
            self.timeline.append("overflow")
            return self._overflow()
        return super().chat(messages, tools=tools, config=config)

    async def _overflow(self) -> AsyncIterator[Any]:
        yield ProviderError(code="400", message=OVERFLOW)


async def test_a_provider_overflow_compacts_in_turn_with_no_review(
    tmp_path: Path, storages: list[SessionStorage], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_sleep(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("agentos.engine.agent.asyncio.sleep", _no_sleep)
    manager = await _manager(tmp_path, storages)
    provider = _OverflowingProvider()
    runner = _runner(provider, manager, tmp_path, proactive=False)
    # Under the preflight trigger: only the provider's rejection forces it.
    await _seed(manager, KEY, rounds=3, chars=1500)

    with structlog.testing.capture_logs() as logs:
        events = await _turn(runner, manager, KEY, "deploy status?", tmp_path)

    assert any(getattr(event, "kind", "") == "done" for event in events)
    assert provider.timeline[:2] == ["overflow", "summary"]
    assert provider.review_requests == []
    (event,) = _review_events(logs)
    assert (event["outcome"], event["reason"], event["compaction"]) == (
        "skipped",
        "no_headroom",
        "agent_inline_overflow",
    )


async def test_a_gateway_overflow_compaction_logs_no_headroom(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    from agentos.gateway.config import ContextOverflowPolicy
    from agentos.gateway.context_overflow import apply_context_overflow_policy

    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    await _seed(manager, KEY, rounds=6, chars=3000)

    with structlog.testing.capture_logs() as logs:
        outcome = await apply_context_overflow_policy(
            config=runner._config,
            message="next question",
            transcript=await manager.get_transcript(KEY),
            session_key=KEY,
            session_manager=manager,
            compaction_marker=runner,
            policy_override=ContextOverflowPolicy.AUTO_SUMMARIZE,
            budget_override=8000,
        )

    assert outcome.over_budget and outcome.removed_count > 0
    assert provider.review_requests == []
    (event,) = _review_events(logs)
    assert (event["reason"], event["compaction"]) == ("no_headroom", "gateway_auto_summarize")


async def test_the_review_turn_itself_never_compacts() -> None:
    """Inside the review the coordinator declines, so the transcript stays as it was."""
    calls: list[Any] = []

    def _factory(**_kwargs: Any) -> Any:
        calls.append(_kwargs)
        raise AssertionError("no compaction service may be built inside the review")

    coordinator = CompactionCoordinator(
        SimpleNamespace(context_overflow_threshold=0.8), None, KEY, service_factory=_factory
    )
    messages = [Message(role="user", content="x" * 100)]

    with pre_compaction_review.pre_compaction_review_scope(KEY):
        result = await coordinator.compact(
            messages, trigger="preflight", turn_input_index=1, force=False
        )
        forced = await coordinator.compact(messages, trigger="overflow", force=True)

    assert (result.status, result.reason, result.compacted) == (
        "skipped",
        "pre_compaction_review",
        False,
    )
    assert result.messages is messages and result.turn_input_index == 1
    assert not forced.compacted
    assert calls == []
    # Another session, or no review at all, compacts as usual.
    with pre_compaction_review.pre_compaction_review_scope("agent:main:other"):
        with pytest.raises(AssertionError):
            await coordinator.compact(messages, trigger="preflight", turn_input_index=1)


# -- 3.3: invisible to the conversation ------------------------------------------------


QUESTION = "Which billing database are we using, Postgres or MySQL?"


async def _notes_retriever(tmp_path: Path, closing: list[Any]) -> Any:
    """A real FTS memory index over one note, so proactive recall has a hit.

    The index is added to *closing*: an unclosed one keeps aiosqlite's
    non-daemon worker thread alive, and a failing test (whose traceback keeps
    the store reachable) then never lets the process exit.
    """
    from agentos.memory.retrieval import MemoryRetriever
    from agentos.memory.store import LongTermMemoryStore
    from agentos.memory.sync_manager import MemorySyncManager

    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "decisions.md").write_text(
        "# Decisions\n\n## Billing database\n"
        "On 2026-10-02 we decided to use Postgres instead of MySQL for the billing "
        "service, mainly for JSONB support and row-level security.\n",
        encoding="utf-8",
    )
    store = LongTermMemoryStore(tmp_path / "memory.db")
    closing.append(store)
    await store.initialize()
    sync = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory_dir)
    await sync.sync("test", force=True)
    return MemoryRetriever(store, sync_manager=sync)


async def test_the_review_leaves_no_trace_in_transcript_capture_recall_or_count(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    capture = _Capture()
    retriever = await _notes_retriever(tmp_path, storages)
    # Proactive compaction off: the review below is the only one that runs.
    runner = _runner(
        provider,
        manager,
        tmp_path,
        capture=capture,
        retriever=retriever,
        nudge=MemoryNudgeConfig(interval=10),
        proactive=False,
    )
    decisions: list[tuple[str, str | None]] = []
    decide = runner._active_recall_skip_reason

    def _record(**kwargs: Any) -> str | None:
        reason = decide(**kwargs)
        decisions.append((kwargs["run_kind"], reason))
        return reason

    runner._active_recall_skip_reason = _record  # type: ignore[method-assign]
    await _seed(manager, KEY)

    await _turn(runner, manager, KEY, QUESTION, tmp_path, capture=True)
    await asyncio.gather(*runner._memory_nudge_pending)  # the turn's counter update
    rows_after_turn = await _compaction_state(manager, KEY)
    counter_after_turn = runner._memory_nudge_counters[("main", KEY)].count
    outcome = await runner.review_memory_before_compaction(
        agent_id="main", session_key=KEY, compaction="manual"
    )
    rows_after_review = await _compaction_state(manager, KEY)

    assert outcome == "ran"
    assert len(provider.review_requests) == 1
    # Transcript: not one row, not one byte.
    assert rows_after_review == rows_after_turn
    # Capture: the user's turn only.
    assert [c["user_text"] for c in capture.captured] == [QUESTION]
    # Recall: live for the user's turn, turned away for the review.
    assert decisions[0] == (run_kinds.SESSION_TURN, None)
    user_turn = provider.turn_systems[0] + str(provider.turn_requests[0][-1].content)
    assert "## Recalled Memory" in user_turn and "Postgres" in user_turn
    assert decisions[1][0] == run_kinds.MEMORY_NUDGE and decisions[1][1] is not None
    review_turn = provider.review_systems[0] + str(provider.review_requests[0][-1].content)
    assert "## Recalled Memory" not in review_turn
    # Not a user turn: the counter was restarted, not advanced. (Seeded from
    # the two stored user rows, plus the turn above.)
    assert counter_after_turn == 3
    assert runner._memory_nudge_counters[("main", KEY)].count == 0
    # And the next turn's history carries nothing from it.
    await _turn(runner, manager, KEY, "and the logs?", tmp_path)
    sent = "\n".join(_dump(provider.turn_requests[-1]))
    assert PRE_COMPACTION_MARK not in sent and NOTHING not in sent


async def test_a_failed_review_writes_no_error_row(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    """An internal turn's provider error is the harness's, not the conversation's."""
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode="error")
    runner = _runner(provider, manager, tmp_path, proactive=False)
    await _seed(manager, KEY)
    await _turn(runner, manager, KEY, "deploy status?", tmp_path)
    before = await _compaction_state(manager, KEY)

    outcome = await runner.review_memory_before_compaction(
        agent_id="main", session_key=KEY, compaction="manual"
    )

    assert outcome == "error"
    assert await _compaction_state(manager, KEY) == before
    rows = await manager.get_transcript(KEY)
    assert not any("invalid api key" in str(row.content) for row in rows)


async def test_the_review_does_not_disturb_a_running_turns_compaction_anchor(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    await _seed(manager, KEY)
    anchor = object()
    runner._compaction_snapshots[KEY] = anchor  # type: ignore[assignment]
    runner._turn_compacted_sessions.add(KEY)

    assert (
        await runner.review_memory_before_compaction(
            agent_id="main", session_key=KEY, compaction="manual"
        )
        == "ran"
    )

    assert runner._compaction_snapshots[KEY] is anchor
    assert KEY in runner._turn_compacted_sessions


@pytest.mark.parametrize("review_mode", ["error", "raise", "hang"])
async def test_a_failed_or_stopped_periodic_review_writes_nothing_either(
    tmp_path: Path, storages: list[SessionStorage], review_mode: str
) -> None:
    """The same internal-turn rule holds for the periodic trigger."""
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode=review_mode)
    runner = _runner(
        provider, manager, tmp_path, nudge=MemoryNudgeConfig(timeout_seconds=0.2), proactive=False
    )
    await _seed(manager, KEY)
    await _turn(runner, manager, KEY, "deploy status?", tmp_path)
    before = await _compaction_state(manager, KEY)

    outcome = await runner._run_memory_nudge_review(agent_id="main", session_key=KEY)

    assert provider.timeline.count("periodic") == 1
    assert outcome == ("timeout" if review_mode == "hang" else "error")
    assert await _compaction_state(manager, KEY) == before


# -- a review only before a compaction that will summarise ------------------------


async def test_a_manual_compaction_with_nothing_to_summarise_runs_no_review(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    provider, _, payload, state, logs = await _manual_compact(
        tmp_path, storages, review_mode="ok", nudge=MemoryNudgeConfig(), window=1_000_000
    )

    assert (payload["compacted"], payload["skip_reason"]) == (False, "within_compaction_budget")
    assert provider.review_requests == []
    assert state["summaries"] == []
    (event,) = _review_events(logs)
    assert (event["outcome"], event["reason"], event["compaction"]) == (
        "skipped",
        "nothing_to_compact",
        "manual",
    )


async def test_a_proactive_job_that_would_summarise_nothing_runs_no_review(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    scheduler = runner._proactive_compaction
    checked: list[str] = []

    async def _not_worth(session_key: str, pending: Any) -> str:
        checked.append(session_key)
        return "not_worth_compacting"

    scheduler._precheck = _not_worth  # type: ignore[method-assign]
    await _seed(manager, KEY)

    with structlog.testing.capture_logs() as logs:
        await _turn(runner, manager, KEY, "deploy status?", tmp_path)
        await scheduler.wait_idle(KEY)

    assert checked == [KEY]
    assert provider.review_requests == []
    (event,) = _review_events(logs)
    assert (event["outcome"], event["reason"], event["compaction"]) == (
        "skipped",
        "nothing_to_compact",
        "proactive_background",
    )


async def test_the_proactive_precheck_is_the_compaction_decision(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    """``_precheck`` answers ``None`` for a job that then really summarises."""
    manager = await _manager(tmp_path, storages)
    provider = _Provider()
    runner = _runner(provider, manager, tmp_path)
    scheduler = runner._proactive_compaction
    answers: list[str | None] = []
    precheck = scheduler._precheck

    async def _spy(session_key: str, pending: Any) -> str | None:
        answers.append(await precheck(session_key, pending))
        return answers[-1]

    scheduler._precheck = _spy  # type: ignore[method-assign]
    await _seed(manager, KEY)

    await _turn(runner, manager, KEY, "deploy status?", tmp_path)
    await scheduler.wait_idle(KEY)

    assert answers == [None]
    assert len(provider.review_requests) == 1
    assert len(await manager.get_summaries(KEY)) == 1


async def test_a_review_cancelled_with_its_job_is_logged(
    tmp_path: Path, storages: list[SessionStorage]
) -> None:
    manager = await _manager(tmp_path, storages)
    provider = _Provider(review_mode="gate")
    runner = _runner(provider, manager, tmp_path)
    scheduler = runner._proactive_compaction
    await _seed(manager, KEY)

    with structlog.testing.capture_logs() as logs:
        await _turn(runner, manager, KEY, "deploy status?", tmp_path)
        await asyncio.wait_for(provider.review_started.wait(), timeout=2)
        job = scheduler._sessions[KEY].task
        assert job is not None
        await _turn(runner, manager, KEY, "and the logs?", tmp_path)
        await asyncio.wait_for(asyncio.gather(job, return_exceptions=True), timeout=2)

    events = _review_events(logs)
    assert [(e["outcome"], e["compaction"]) for e in events][:1] == [
        ("cancelled", "proactive_background")
    ]
    assert events[0]["duration_ms"] >= 0
    provider.review_gate.set()
    await scheduler.wait_idle(KEY)
    await _drain_reviews(runner)
