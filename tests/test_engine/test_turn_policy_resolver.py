"""``TurnPolicyResolver``: one precedence chain for every turn budget (#3819)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.turn_policy import TurnPolicyResolver
from agentos.engine.types import AgentConfig


class _Log:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))


class _Sessions:
    def __init__(self, **values: Any) -> None:
        self._cfg = SimpleNamespace(**values)

    def get_session_config(self, session_key: str) -> Any:
        return self._cfg


def _resolver(
    *, session: dict[str, Any] | None = None, env: dict[str, str] | None = None, **config: Any
) -> tuple[TurnPolicyResolver, _Log]:
    log = _Log()
    resolver = TurnPolicyResolver(
        session_manager=_Sessions(**(session or {})) if session is not None else None,
        config=SimpleNamespace(**config),
        log=log,
        env=env or {},
    )
    return resolver, log


def test_explicit_beats_session_beats_env_beats_config() -> None:
    everything = {
        "session": {"agent_tool_timeout_seconds": 20},
        "env": {"AGENTOS_AGENT_TOOL_TIMEOUT": "30"},
        "agent_tool_timeout_seconds": 40,
    }
    resolver, _ = _resolver(**everything)
    assert resolver.tool_timeout("s", 10) == 10
    assert resolver.tool_timeout("s") == 20

    resolver, _ = _resolver(**{**everything, "session": {}})
    assert resolver.tool_timeout("s") == 30

    resolver, _ = _resolver(**{**everything, "session": {}, "env": {}})
    assert resolver.tool_timeout("s") == 40

    resolver, _ = _resolver()
    assert resolver.tool_timeout("s") == AgentConfig().tool_timeout


def test_unusable_values_are_logged_by_source_and_skipped() -> None:
    resolver, log = _resolver(
        session={"agent_max_provider_retries": -1},
        env={"AGENTOS_AGENT_MAX_PROVIDER_RETRIES": "many"},
        agent_max_provider_retries=True,
    )

    assert resolver.max_provider_retries("s") == AgentConfig().max_provider_retries
    assert log.warnings == [
        ("turn_runner.invalid_agent_max_provider_retries", {"source": "session", "value": -1}),
        ("turn_runner.invalid_agent_max_provider_retries", {"source": "env", "raw": "many"}),
        ("turn_runner.invalid_agent_max_provider_retries", {"source": "config", "value": True}),
    ]


def test_an_unusable_explicit_argument_raises() -> None:
    resolver, _ = _resolver()
    with pytest.raises(ValueError, match="request_timeout must be a positive number"):
        resolver.request_timeout("s", 0, default=lambda: 1.0)
    with pytest.raises(ValueError, match="iteration_timeout must be a non-negative number"):
        resolver.iteration_timeout("s", -1)


def test_request_timeout_rejects_zero_and_only_calls_the_default_when_unset() -> None:
    calls: list[str] = []

    def fallback() -> float:
        calls.append("llm")
        return 77.0

    resolver, log = _resolver(env={"AGENTOS_AGENT_REQUEST_TIMEOUT": "0"})
    assert resolver.request_timeout("s", None, default=fallback) == 77.0
    assert log.warnings == [
        ("turn_runner.invalid_agent_request_timeout", {"source": "env", "value": 0.0})
    ]

    resolver, _ = _resolver(agent_request_timeout_seconds=12)
    assert resolver.request_timeout("s", None, default=fallback) == 12.0
    assert calls == ["llm"]


def test_runtime_timeout_keeps_its_legacy_alias_and_warning_names() -> None:
    resolver, _ = _resolver(session={"llm_timeout_seconds": 0})
    assert resolver.runtime_timeout("s", default=9.0) == 0.0

    resolver, log = _resolver(env={"AGENTOS_TURN_TIMEOUT": "-5"}, llm_timeout_seconds=60)
    assert resolver.runtime_timeout("s", default=9.0) == 60.0
    assert log.warnings == [("turn_runner.negative_runtime_timeout", {"value": -5.0})]


def test_max_iterations_reports_its_source() -> None:
    resolver, _ = _resolver(env={"AGENTOS_AGENT_MAX_ITERATIONS": "7"})
    policy = resolver.max_iterations("s")
    assert (policy.max_iterations, policy.max_iterations_source) == (
        7,
        "env AGENTOS_AGENT_MAX_ITERATIONS",
    )
    assert resolver.max_iterations("s", 3).max_iterations_source == "explicit argument"
