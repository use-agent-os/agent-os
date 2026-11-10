"""Boot turns the ``[task_runtime]`` / ``[subagents]`` keys into TaskRuntime arguments."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agentos.gateway.boot import (
    _make_children_limit_resolver,
    _task_runtime_cancel_grace_s,
    _task_runtime_max_concurrent_children,
    _task_runtime_max_nested_concurrency,
)
from agentos.gateway.config import GatewayConfig


def test_zero_children_limit_means_no_per_parent_limit() -> None:
    config = GatewayConfig()
    assert _task_runtime_max_concurrent_children(config) == 2
    config.subagents.max_concurrent_children = 0
    assert _task_runtime_max_concurrent_children(config) is None
    config.subagents.max_concurrent_children = 5
    assert _task_runtime_max_concurrent_children(config) == 5


def test_nested_ceiling_defaults_to_max_concurrency() -> None:
    config = GatewayConfig()
    config.task_runtime.max_concurrency = 6
    assert config.task_runtime.max_nested_concurrency is None
    assert _task_runtime_max_nested_concurrency(config) == 6
    config.task_runtime.max_nested_concurrency = 2
    assert _task_runtime_max_nested_concurrency(config) == 2


def test_nested_ceiling_is_validated_and_public() -> None:
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({"task_runtime": {"max_nested_concurrency": 0}})
    config = GatewayConfig.model_validate({"task_runtime": {"max_nested_concurrency": 3}})
    assert config.to_public_dict()["task_runtime"]["max_nested_concurrency"] == 3


def test_cancel_grace_reaches_the_runtime() -> None:
    config = GatewayConfig()
    assert _task_runtime_cancel_grace_s(config) == 5.0
    config.task_runtime.turn_cancel_grace_seconds = 0
    assert _task_runtime_cancel_grace_s(config) == 0.0


async def test_children_limit_resolver_reads_the_parent_agents_policy() -> None:
    class _Manager:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def get_agent_config(self, agent_id: str) -> dict[str, Any] | None:
            self.asked.append(agent_id)
            if agent_id == "strict":
                return {"subagents": {"max_concurrent_children": 1}}
            if agent_id == "odd":
                return {"subagents": {"max_concurrent_children": 0}}
            return None

    manager = _Manager()
    resolve = _make_children_limit_resolver(manager)

    assert await resolve("agent:strict:webchat:a") == 1
    assert await resolve("agent:main:webchat:b") is None
    assert await resolve("agent:odd:webchat:c") is None  # not a valid override
    assert manager.asked == ["strict", "main", "odd"]
