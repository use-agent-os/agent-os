"""Deep recall inherits the session's chat model.

With ``[memory.recall] provider/model`` empty, the deep lane calls the model
the session is chatting with this turn (its pinned model, else the agent's
model, else a router hold, else the primary ``[llm]`` model), through the
turn's own provider credentials. ``AGENTOS_MEMORY_RECALL_*``,
``[auxiliary.tasks.memory_recall]`` and ``[memory.recall]`` still win, and the
recall call never thinks. Fixtures are synthetic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import MemoryConfig, MemoryRecallConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.memory.active_recall import (
    ActiveRecall,
    ActiveRecallState,
    DeepRecallTarget,
    RecallRecord,
    get_active_recall_state,
)
from agentos.memory.types import MemorySearchResult, MemorySource
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import auxiliary
from agentos.provider.auxiliary import AuxiliaryClient
from agentos.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from agentos.provider.types import ChatConfig, ModelCapabilities
from agentos.router_control import HOLD_SOURCE_USER, RouterControlTarget

KEY = "agent:coder:main"
PRIMARY = ProviderConfig(
    provider="anthropic",
    model="claude-big",
    api_key="sk-session",
    base_url="https://llm.example/v1",
)


class _RecordingFactory:
    """``provider_factory`` double: records what each recall call was sent."""

    def __init__(self) -> None:
        self.builds: list[dict[str, Any]] = []
        self.configs: list[ChatConfig | None] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.builds.append(kwargs)
        factory = self

        class _Provider:
            def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
                factory.configs.append(config)
                return self._stream()

            async def _stream(self) -> AsyncIterator[Any]:
                yield ProviderText(text="NONE")
                yield ProviderDone(stop_reason="end_turn", input_tokens=5, output_tokens=1)

        return _Provider()


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingFactory]:
    for name in ("AGENTOS_MEMORY_RECALL_PROVIDER", "AGENTOS_MEMORY_RECALL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    recording = _RecordingFactory()
    monkeypatch.setattr(
        auxiliary,
        "_client",
        AuxiliaryClient(
            config=SimpleNamespace(provider="openrouter", model="aux-cheap", tasks={}),
            llm_config=SimpleNamespace(provider="anthropic", model="claude-big"),
            provider_factory=recording,
        ),
    )
    yield recording


def _runner(
    *,
    recall: dict[str, Any] | None = None,
    router: bool = False,
    selector: bool = True,
    llm_thinking: str | None = None,
) -> TurnRunner:
    return TurnRunner(
        provider_selector=ModelSelector(SelectorConfig(primary=PRIMARY)) if selector else None,
        config=SimpleNamespace(
            memory=SimpleNamespace(recall=MemoryRecallConfig(**(recall or {}))),
            agents={"coder": {"model": "agent-model"}},
            agentos_router=SimpleNamespace(enabled=router),
            llm=SimpleNamespace(provider="anthropic", base_url="", thinking=llm_thinking),
        ),
    )


def _hold(runner: TurnRunner, model: str) -> None:
    runner.router_control_hold_store.set_hold(
        KEY,
        RouterControlTarget(target_id=model, target_type="model", tier="fast", model=model),
        evidence="test",
        source=HOLD_SOURCE_USER,
    )


async def _call(
    runner: TurnRunner, turn_model: str | None, recall: dict[str, Any] | None = None
) -> DeepRecallTarget:
    complete, target = runner._deep_recall_completer(
        agent_id="coder",
        session_key=KEY,
        config=MemoryRecallConfig(**(recall or {})),
        turn_model=turn_model,
    )
    assert await complete("system", "user") == "NONE"
    assert isinstance(target, DeepRecallTarget)
    return target


# -- resolution order -----------------------------------------------------------


async def test_session_pinned_model_is_used_with_the_session_credentials(
    factory: _RecordingFactory,
) -> None:
    target = await _call(_runner(), "session-model")

    assert target == DeepRecallTarget("anthropic", "session-model", "session_model")
    assert factory.builds[-1] == {
        "provider": "anthropic",
        "model": "session-model",
        "api_key": "sk-session",
        "base_url": "https://llm.example/v1",
    }


async def test_agent_model_is_labelled_as_such(factory: _RecordingFactory) -> None:
    target = await _call(_runner(), "agent-model")

    assert target == DeepRecallTarget("anthropic", "agent-model", "agent_model")


async def test_explicit_turn_model_beats_a_router_hold(factory: _RecordingFactory) -> None:
    runner = _runner(router=True)
    _hold(runner, "held-model")

    assert (await _call(runner, "session-model")).rule == "session_model"


async def test_router_hold_is_used_when_the_turn_has_no_explicit_model(
    factory: _RecordingFactory,
) -> None:
    runner = _runner(router=True)
    _hold(runner, "held-model")

    target = await _call(runner, None)

    assert target == DeepRecallTarget("anthropic", "held-model", "router_hold")
    assert factory.builds[-1]["api_key"] == "sk-session"


async def test_router_hold_is_ignored_while_the_router_is_off(factory: _RecordingFactory) -> None:
    runner = _runner(router=False)
    _hold(runner, "held-model")

    assert await _call(runner, None) == DeepRecallTarget("anthropic", "claude-big", "llm")


async def test_primary_llm_model_is_the_session_default(factory: _RecordingFactory) -> None:
    target = await _call(_runner(), None)

    assert target == DeepRecallTarget("anthropic", "claude-big", "llm")
    assert factory.builds[-1]["base_url"] == "https://llm.example/v1"


async def test_memory_recall_config_beats_the_session_model(factory: _RecordingFactory) -> None:
    target = await _call(
        _runner(), "session-model", recall={"provider": "openrouter", "model": "recall-fast"}
    )

    assert target == DeepRecallTarget("openrouter", "recall-fast", "memory_recall")
    assert factory.builds[-1]["model"] == "recall-fast"
    # Not the session's key: the recall provider differs from the chat provider.
    assert factory.builds[-1]["api_key"] != "sk-session"


async def test_auxiliary_task_config_beats_memory_recall_config(
    factory: _RecordingFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    auxiliary._client.config.tasks = {
        "memory_recall": SimpleNamespace(provider="openrouter", model="task-model")
    }

    target = await _call(_runner(), "session-model", recall={"model": "recall-fast"})

    assert (target.model, target.rule) == ("task-model", "auxiliary_task")


async def test_task_env_beats_everything(
    factory: _RecordingFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOS_MEMORY_RECALL_PROVIDER", "openrouter")
    monkeypatch.setenv("AGENTOS_MEMORY_RECALL_MODEL", "env-model")
    auxiliary._client.config.tasks = {"memory_recall": SimpleNamespace(model="task-model")}

    target = await _call(_runner(), "session-model", recall={"model": "recall-fast"})

    assert (target.model, target.rule) == ("env-model", "env")


async def test_env_model_alone_is_not_mixed_with_the_session_target(
    factory: _RecordingFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator model without a provider keeps the auxiliary provider chain."""
    monkeypatch.setenv("AGENTOS_MEMORY_RECALL_MODEL", "env-model")

    target = await _call(_runner(), "session-model")

    assert (target.provider, target.model, target.rule) == ("openrouter", "env-model", "env")


async def test_without_a_selector_the_auxiliary_chain_applies(
    factory: _RecordingFactory,
) -> None:
    target = await _call(_runner(selector=False), "session-model")

    assert target == DeepRecallTarget("openrouter", "aux-cheap", "auxiliary")


# -- thinking -------------------------------------------------------------------


async def test_recall_call_never_thinks_even_when_the_session_does(
    factory: _RecordingFactory,
) -> None:
    await _call(_runner(llm_thinking="high"), "session-model")

    config = factory.configs[-1]
    assert config is not None
    assert config.thinking is False
    assert config.thinking_budget_tokens == 0
    assert config.thinking_level is None
    assert config.max_tokens == 512


async def test_recall_call_carries_model_capabilities(factory: _RecordingFactory) -> None:
    """Capabilities let adapters send an explicit "no reasoning" to reasoning models."""
    caps = ModelCapabilities(supports_reasoning=True, reasoning_format="openai")
    seen: list[tuple[str, str]] = []

    class _Catalog:
        def get_capabilities(self, model: str, *, provider_name: str, base_url: str) -> Any:
            seen.append((model, provider_name))
            return caps

    runner = _runner()
    runner._model_catalog = _Catalog()
    complete, _ = runner._deep_recall_completer(
        agent_id="coder", session_key=KEY, config=MemoryRecallConfig(), turn_model="o-reasoner"
    )
    await complete("system", "user")

    assert seen == [("o-reasoner", "anthropic")]
    config = factory.configs[-1]
    assert config is not None
    assert config.model_capabilities == caps
    assert config.thinking is False


# -- status ---------------------------------------------------------------------


class _Retriever:
    async def search(self, query: str, opts: Any, **_: Any) -> list[MemorySearchResult]:
        if getattr(opts, "source", None) is MemorySource.memory:
            return []
        return [
            MemorySearchResult(
                chunk_id="c1",
                path="memory/2026-10-02.md",
                start_line=1,
                end_line=2,
                score=0.9,
                snippet="Decided on Postgres for billing.",
                text="Decided on Postgres for billing.",
                source=MemorySource.sessions,
            )
        ]


async def test_record_and_rpc_status_show_the_deep_model() -> None:
    state = ActiveRecallState()

    async def _complete(system: str, user: str) -> str:
        return "Billing uses Postgres."

    block = await ActiveRecall(state).recall(
        session_key=KEY,
        message="What did we decide about the billing database last time?",
        retriever=_Retriever(),
        config=MemoryRecallConfig(),
        trigger_timeout=5.0,
        complete=_complete,
        deep_target=DeepRecallTarget("anthropic", "session-model", "session_model"),
    )
    assert "Postgres" in block
    record = state.last(KEY)
    assert record is not None
    assert (record.deep_provider, record.deep_model, record.deep_model_rule) == (
        "anthropic",
        "session-model",
        "session_model",
    )

    shared = get_active_recall_state()
    shared.record(record)
    try:
        ctx = RpcContext(conn_id="test")
        ctx.config = SimpleNamespace(memory=MemoryConfig())
        res = await get_dispatcher().dispatch(
            "r1", "memory.recall.status", {"sessionKey": KEY}, ctx
        )
    finally:
        shared.clear()
    assert res.ok
    last = res.payload["session"]["last"]
    assert last["deepProvider"] == "anthropic"
    assert last["deepModel"] == "session-model"
    assert last["deepModelRule"] == "session_model"


async def test_deep_model_is_not_recorded_when_the_deep_lane_does_not_run() -> None:
    state = ActiveRecallState()
    await ActiveRecall(state).recall(
        session_key=KEY,
        message="Explain DNS over HTTPS.",
        retriever=_Retriever(),
        config=MemoryRecallConfig(),
        trigger_timeout=5.0,
        complete=None,
        deep_target=DeepRecallTarget("anthropic", "session-model", "session_model"),
    )
    record = state.last(KEY)
    assert isinstance(record, RecallRecord)
    assert record.deep_model == ""
