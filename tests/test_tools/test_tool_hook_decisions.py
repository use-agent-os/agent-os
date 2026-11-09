"""Tool hooks can allow, block or rewrite a call and replace its result.

Scenarios of the ``tool-call-hooks`` spec (OpenSpec change
``refactor-agent-harness-core``), through the real ``build_tool_handler``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import agentos.tools.dispatch as dispatch_module
from agentos.engine.hooks import (
    Allow,
    Block,
    ReplaceResult,
    Rewrite,
    ToolHookCall,
    ToolHookResult,
)
from agentos.tool_boundary import ToolCall
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolSpec


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def handler(self, **arguments: Any) -> str:
        self.calls.append(arguments)
        return f"read {arguments.get('path')} with token sk-secret-123"


def _registry(recorder: _Recorder) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="read",
            description="read a file",
            parameters={"path": {"type": "string"}},
            required=["path"],
        ),
        recorder.handler,
    )
    return registry


class _Hook:
    def __init__(
        self,
        name: str,
        before: Any = None,
        after: Any = None,
        *,
        raises: bool = False,
        fail_closed: bool = False,
    ) -> None:
        self.name = name
        self._before = before
        self._after = after
        self._raises = raises
        self.fail_closed = fail_closed
        self.seen: list[dict[str, Any]] = []

    def before_tool(self, call: ToolHookCall) -> Any:
        self.seen.append(dict(call.tool_call.arguments))
        if self._raises:
            raise RuntimeError("guard crashed")
        return self._before

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> Any:
        return self._after


def _call(arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(tool_use_id="c1", tool_name="read", arguments=arguments)


async def _dispatch(hooks: list[Any], arguments: dict[str, Any]) -> tuple[Any, _Recorder]:
    recorder = _Recorder()
    handler = build_tool_handler(_registry(recorder), None, tool_hooks=hooks)
    return await handler(_call(arguments)), recorder


def _message(result: Any) -> str:
    return str(json.loads(result.content).get("user_message"))


async def test_block_skips_the_handler_and_shows_the_reason() -> None:
    result, recorder = await _dispatch(
        [_Hook("guard", before=Block("writes outside workspace"))], {"path": "a"}
    )

    assert recorder.calls == []
    assert result.is_error
    assert "writes outside workspace" in _message(result)


async def test_first_block_wins_and_later_hooks_are_not_consulted() -> None:
    later = _Hook("later")
    result, recorder = await _dispatch(
        [_Hook("first", before=Allow()), _Hook("second", before=Block("no")), later],
        {"path": "a"},
    )

    assert recorder.calls == [] and result.is_error
    assert later.seen == []


async def test_rewrite_runs_with_the_new_arguments_and_later_hooks_see_them() -> None:
    later = _Hook("later")
    result, recorder = await _dispatch(
        [_Hook("redirect", before=Rewrite({"path": "safe.txt"})), later], {"path": "a"}
    )

    assert recorder.calls == [{"path": "safe.txt"}]
    assert later.seen == [{"path": "safe.txt"}]
    assert not result.is_error


async def test_rewrite_to_invalid_arguments_is_rejected_by_admission() -> None:
    result, recorder = await _dispatch([_Hook("bad", before=Rewrite({"path": 42}))], {"path": "a"})

    assert recorder.calls == []
    assert result.is_error
    assert json.loads(result.content)["error_class"] == "ToolArgumentsRejected"


@pytest.mark.parametrize(
    "rewritten",
    [
        {"path": "[tool_use_argument_projection]\nomitted"},
        {"path": "[historical_tool_argument_omitted]\nomitted"},
        {"_raw": "{not json"},
    ],
    ids=["projection", "historical", "raw"],
)
async def test_rewrite_is_refused_where_the_models_own_arguments_would_be(
    rewritten: dict[str, Any],
) -> None:
    # A hook filling arguments from history can pick up a placeholder or an
    # unparsed payload; it is refused exactly like the model's own call.
    direct, direct_recorder = await _dispatch([], dict(rewritten))
    result, recorder = await _dispatch([_Hook("refill", before=Rewrite(rewritten))], {"path": "a"})

    assert direct.is_error and direct_recorder.calls == []
    assert recorder.calls == []
    assert result.is_error
    assert json.loads(result.content)["error_class"] == json.loads(direct.content)["error_class"]


async def test_rewrite_goes_through_the_policy_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_by_policy: list[dict[str, Any]] = []
    real_chain = dispatch_module.run_chain_with_emit

    def spy(dispatch_input: Any, *, emit: Any) -> Any:
        seen_by_policy.append(dict(dispatch_input.tool_call.arguments))
        return real_chain(dispatch_input, emit=emit)

    monkeypatch.setattr(dispatch_module, "run_chain_with_emit", spy)

    await _dispatch([_Hook("redirect", before=Rewrite({"path": "other"}))], {"path": "a"})

    assert seen_by_policy == [{"path": "other"}]


async def test_after_tool_can_redact_the_result() -> None:
    result, _ = await _dispatch(
        [_Hook("redact", after=ReplaceResult("read a with token [redacted]"))], {"path": "a"}
    )

    assert result.content == "read a with token [redacted]"
    assert result.tool_use_id == "c1"
    assert "sk-secret" not in result.content


async def test_observe_only_hook_that_raises_does_not_block() -> None:
    result, recorder = await _dispatch([_Hook("observer", raises=True)], {"path": "a"})

    assert recorder.calls == [{"path": "a"}]
    assert not result.is_error


async def test_fail_closed_guard_that_raises_blocks_the_call() -> None:
    result, recorder = await _dispatch(
        [_Hook("guard", raises=True, fail_closed=True)], {"path": "a"}
    )

    assert recorder.calls == []
    assert result.is_error
    assert "failed" in _message(result)


async def test_legacy_hook_returning_none_still_runs_the_call() -> None:
    class Legacy:
        name = "legacy"

        def __init__(self) -> None:
            self.before = 0

        def before_tool(self, call: ToolHookCall) -> None:
            self.before += 1

        def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> None:
            return None

    legacy = Legacy()
    result, recorder = await _dispatch([legacy], {"path": "a"})

    assert legacy.before == 1
    assert recorder.calls == [{"path": "a"}]
    assert not result.is_error
