"""``Agent`` waits for approvals through an injected port (#3819)."""

from __future__ import annotations

import ast
import inspect

import pytest

import agentos.engine.agent as agent_module
from agentos.engine.agent import Agent
from agentos.engine.approval_port import default_approval_waiter, wait_for_pending_approval


class _Waiter:
    def __init__(self, *, known: bool = True) -> None:
        self.calls: list[tuple[str, float | None]] = []
        self._known = known

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        self.calls.append((approval_id, timeout))
        if not self._known:
            raise KeyError(approval_id)
        return True


class _Provider:
    provider_name = "stub"


@pytest.mark.asyncio
async def test_a_pending_result_waits_on_the_injected_waiter() -> None:
    waiter = _Waiter()
    await wait_for_pending_approval(waiter, {"approval_id": "a1"}, timeout=5.0)
    assert waiter.calls == [("a1", 5.0)]


@pytest.mark.asyncio
async def test_missing_or_unknown_approvals_are_skipped() -> None:
    waiter = _Waiter(known=False)
    await wait_for_pending_approval(waiter, {}, timeout=1.0)
    await wait_for_pending_approval(waiter, {"approval_id": "gone"}, timeout=1.0)
    assert waiter.calls == [("gone", 1.0)]


def test_agent_keeps_the_injected_waiter_or_the_default() -> None:
    waiter = _Waiter()
    assert Agent(_Provider(), approval_waiter=waiter)._approval_waiter is waiter  # type: ignore[arg-type]
    assert Agent(_Provider())._approval_waiter is default_approval_waiter()  # type: ignore[arg-type]


def test_agent_module_does_not_import_the_gateway() -> None:
    tree = ast.parse(inspect.getsource(agent_module))
    imported = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert [name for name in imported if name.startswith("agentos.gateway")] == []
