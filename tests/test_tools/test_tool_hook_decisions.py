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
from agentos.tool_boundary import ToolCall, ToolResult
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, ToolSpec


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
        {"_invalid_provider_context_arguments": True, "tool": "read"},
    ],
    ids=["projection", "historical", "raw", "compacted-placeholder"],
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
    if "_raw" not in rewritten:
        assert direct.execution_status is not None
        assert direct.execution_status["reason"] == "provider_context_projection_reused"


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


# ---------------------------------------------------------------------------
# after_tool sees the settled result
# ---------------------------------------------------------------------------


class _DocsWorkspaceGuard:
    """The ``after_tool`` of the documented ``WorkspaceGuard`` example, verbatim."""

    name = "workspace-guard"
    fail_closed = True

    def __init__(self) -> None:
        self.outcomes: list[ToolHookResult] = []

    def before_tool(self, call: ToolHookCall) -> None:
        return None

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> Any:
        self.outcomes.append(outcome)
        text = str(getattr(outcome.result, "content", "") or "")
        if "sk-" in text:
            return ReplaceResult(text.replace("sk-", "sk-[redacted]-"))
        return None


def _single_tool_registry(name: str, handler: Any, **properties: Any) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name=name,
            description="d",
            parameters=properties or {"path": {"type": "string"}},
        ),
        handler,
    )
    return registry


async def test_documented_redaction_example_redacts_the_secret() -> None:
    guard = _DocsWorkspaceGuard()
    result, _ = await _dispatch([guard], {"path": "a"})

    assert isinstance(guard.outcomes[0].result, ToolResult)
    assert guard.outcomes[0].result.content == "read a with token sk-secret-123"
    assert guard.outcomes[0].raw_result == "read a with token sk-secret-123"
    assert "sk-secret" not in result.content
    assert "sk-[redacted]-secret-123" in result.content


async def test_after_tool_sees_a_dict_result_as_its_settled_json_string() -> None:
    async def handler(**_: Any) -> dict[str, Any]:
        return {"token": "sk-live-1", "rows": 2}

    guard = _DocsWorkspaceGuard()
    handler_fn = build_tool_handler(
        _single_tool_registry("query", handler, q={"type": "string"}), None, tool_hooks=[guard]
    )
    result = await handler_fn(ToolCall(tool_use_id="c1", tool_name="query", arguments={}))

    seen = guard.outcomes[0]
    assert seen.raw_result == {"token": "sk-live-1", "rows": 2}
    assert seen.result is not None and isinstance(seen.result.content, str)
    assert "sk-live" not in result.content
    assert "sk-[redacted]-live-1" in result.content


async def test_redacted_pending_approval_result_still_reads_as_pending() -> None:
    from agentos.engine.tool_projection import _pending_approval_payload

    async def handler(**_: Any) -> str:
        return json.dumps(
            {
                "status": "approval_required",
                "approval_id": "ap-1",
                "command": "deploy --key sk-abc",
            }
        )

    guard = _DocsWorkspaceGuard()
    handler_fn = build_tool_handler(
        _single_tool_registry("deploy", handler, command={"type": "string"}),
        ToolContext(),
        tool_hooks=[guard],
    )
    result = await handler_fn(ToolCall(tool_use_id="c1", tool_name="deploy", arguments={}))

    assert "sk-abc" not in result.content
    pending = _pending_approval_payload(result.content)
    assert pending is not None and pending["approval_id"] == "ap-1"
    assert result.execution_status is not None
    assert result.execution_status["reason"] == "approval_pending"
    # What a hook had to do with the old raw dict: it no longer parses.
    assert _pending_approval_payload(str({"status": "approval_required"})) is None


async def test_handler_exception_reaches_after_tool_with_its_envelope() -> None:
    async def handler(**_: Any) -> str:
        raise RuntimeError("down")

    guard = _DocsWorkspaceGuard()
    handler_fn = build_tool_handler(
        _single_tool_registry("read", handler), None, tool_hooks=[guard]
    )
    result = await handler_fn(_call({"path": "a"}))

    seen = guard.outcomes[0]
    assert isinstance(seen.exception, RuntimeError)
    assert seen.result is not None and seen.result.is_error
    assert seen.result.content == result.content


async def test_later_hook_sees_the_earlier_replacement() -> None:
    later = _DocsWorkspaceGuard()
    await _dispatch([_Hook("first", after=ReplaceResult("replaced sk-x")), later], {"path": "a"})

    assert later.outcomes[0].result is not None
    assert later.outcomes[0].result.content == "replaced sk-x"


# ---------------------------------------------------------------------------
# fail_closed on after_tool
# ---------------------------------------------------------------------------


class _CrashingAfter:
    def __init__(self, *, fail_closed: bool) -> None:
        self.name = "redactor"
        self.fail_closed = fail_closed

    def before_tool(self, call: ToolHookCall) -> None:
        return None

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> None:
        raise RuntimeError("redactor crashed")


async def test_fail_closed_after_tool_crash_withholds_the_result() -> None:
    result, recorder = await _dispatch([_CrashingAfter(fail_closed=True)], {"path": "a"})

    assert recorder.calls == [{"path": "a"}]
    assert result.is_error
    assert "sk-secret" not in result.content
    envelope = json.loads(result.content)
    assert envelope["error_class"] == "ToolHookFailed"
    assert "withheld" in envelope["user_message"]
    assert result.execution_status is not None
    assert result.execution_status["reason"] == "hook_failed"
    assert result.tool_use_id == "c1"


async def test_fail_open_after_tool_crash_keeps_the_result() -> None:
    result, _ = await _dispatch([_CrashingAfter(fail_closed=False)], {"path": "a"})

    assert not result.is_error
    assert result.content == "read a with token sk-secret-123"


async def test_fail_closed_after_tool_crash_on_a_denial_still_withholds() -> None:
    handler_fn = build_tool_handler(
        _registry(_Recorder()),
        ToolContext(denied_tools={"read"}),
        tool_hooks=[_CrashingAfter(fail_closed=True)],
    )
    result = await handler_fn(_call({"path": "a"}))

    assert result.is_error
    assert json.loads(result.content)["error_class"] == "ToolHookFailed"


# ---------------------------------------------------------------------------
# after_tool pairing on every early return
# ---------------------------------------------------------------------------


class _Observer:
    def __init__(self, name: str = "observer") -> None:
        self.name = name
        self.before = 0
        self.outcomes: list[ToolHookResult] = []

    def before_tool(self, call: ToolHookCall) -> None:
        self.before += 1

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> None:
        self.outcomes.append(outcome)


async def test_block_pairs_after_tool_for_hooks_that_ran_before_it() -> None:
    observer, blocker, after_blocker = _Observer(), _Hook("guard", before=Block("no")), _Observer()
    result, recorder = await _dispatch([observer, blocker, after_blocker], {"path": "a"})

    assert recorder.calls == []
    assert observer.before == 1 and len(observer.outcomes) == 1
    seen = observer.outcomes[0].result
    assert seen is not None and seen.content == result.content
    assert json.loads(seen.content)["error_class"] == "ToolHookBlocked"
    # A hook after the block never saw before_tool, so it gets no after_tool.
    assert after_blocker.before == 0 and after_blocker.outcomes == []


async def test_fail_closed_before_tool_crash_pairs_after_tool() -> None:
    observer = _Observer()
    await _dispatch([observer, _Hook("guard", raises=True, fail_closed=True)], {"path": "a"})

    assert observer.before == 1 and len(observer.outcomes) == 1


@pytest.mark.parametrize(
    "rewritten",
    [{"path": "[tool_use_argument_projection]\nomitted"}, {"path": 42}],
    ids=["refused", "readmission-rejected"],
)
async def test_refused_rewrite_pairs_after_tool(rewritten: dict[str, Any]) -> None:
    observer = _Observer()
    result, recorder = await _dispatch(
        [observer, _Hook("refill", before=Rewrite(rewritten))], {"path": "a"}
    )

    assert recorder.calls == [] and result.is_error
    assert len(observer.outcomes) == 1
    assert observer.outcomes[0].result is not None
    assert observer.outcomes[0].result.content == result.content


async def test_block_envelope_can_be_replaced() -> None:
    result, _ = await _dispatch(
        [_Hook("guard", before=Block("no"), after=ReplaceResult("blocked, quietly"))],
        {"path": "a"},
    )

    assert result.content == "blocked, quietly"
    assert result.is_error


# ---------------------------------------------------------------------------
# ReplaceResult on the denial and run-budget paths
# ---------------------------------------------------------------------------


async def test_replace_result_applies_to_a_policy_denial() -> None:
    observer = _Observer()
    handler_fn = build_tool_handler(
        _registry(_Recorder()),
        ToolContext(denied_tools={"read"}),
        tool_hooks=[observer, _Hook("redact", after=ReplaceResult("denied [redacted]"))],
    )
    result = await handler_fn(_call({"path": "a"}))

    assert observer.outcomes[0].result is not None
    assert observer.outcomes[0].result.is_error
    assert result.content == "denied [redacted]"
    assert result.is_error
    assert result.tool_use_id == "c1"


async def test_replace_result_applies_to_a_run_budget_skip() -> None:
    from agentos.result_budget import ToolRunBudgetPolicy

    calls: list[dict[str, Any]] = []

    async def web_fetch(**arguments: Any) -> str:
        calls.append(arguments)
        return "page"

    registry = _single_tool_registry(
        "web_fetch", web_fetch, url={"type": "string"}, max_chars={"type": "integer"}
    )
    ctx = ToolContext(
        tool_run_budget_policy=ToolRunBudgetPolicy(max_web_fetch_calls_per_turn=0),
    )
    handler_fn = build_tool_handler(
        registry, ctx, tool_hooks=[_Hook("redact", after=ReplaceResult("skipped [redacted]"))]
    )
    result = await handler_fn(
        ToolCall(tool_use_id="c1", tool_name="web_fetch", arguments={"url": "https://x.test"})
    )

    assert calls == []
    assert result.content == "skipped [redacted]"


# ---------------------------------------------------------------------------
# ReplaceResult status and size
# ---------------------------------------------------------------------------


async def test_replacement_that_flips_is_error_resets_the_status() -> None:
    handler_fn = build_tool_handler(
        _registry(_Recorder()),
        ToolContext(),
        tool_hooks=[_Hook("err", after=ReplaceResult("not allowed", is_error=True))],
    )
    result = await handler_fn(_call({"path": "a"}))

    assert result.is_error
    assert result.execution_status is not None
    assert result.execution_status["status"] == "error"
    assert result.execution_status["reason"] == "hook_replaced"


async def test_replacement_that_clears_is_error_resets_the_status() -> None:
    async def handler(**_: Any) -> str:
        raise RuntimeError("down")

    handler_fn = build_tool_handler(
        _single_tool_registry("read", handler),
        ToolContext(),
        tool_hooks=[_Hook("fix", after=ReplaceResult("cached copy", is_error=False))],
    )
    result = await handler_fn(_call({"path": "a"}))

    assert not result.is_error
    assert result.execution_status is not None
    assert result.execution_status["status"] == "success"


async def test_replacement_keeping_the_flag_keeps_the_tool_status() -> None:
    async def handler(**_: Any) -> str:
        raise RuntimeError("down")

    handler_fn = build_tool_handler(
        _single_tool_registry("read", handler),
        ToolContext(),
        tool_hooks=[_Hook("redact", after=ReplaceResult("down [redacted]"))],
    )
    result = await handler_fn(_call({"path": "a"}))

    assert result.is_error
    assert result.execution_status is not None
    assert result.execution_status["reason"] == "runtime_error"


async def test_replacement_is_held_to_the_single_result_cap() -> None:
    from agentos.result_budget import ToolResultBudgetPolicy

    ctx = ToolContext(
        tool_result_budget_policy=ToolResultBudgetPolicy(
            max_single_tool_result_chars=2_000,
            max_tool_result_chars_per_turn=2_500,
        )
    )
    handler_fn = build_tool_handler(
        _registry(_Recorder()), ctx, tool_hooks=[_Hook("huge", after=ReplaceResult("x" * 50_000))]
    )
    result = await handler_fn(_call({"path": "a"}))

    assert len(result.content) < 5_000
    assert json.loads(result.content)["result_truncated"] is True


# ---------------------------------------------------------------------------
# Arguments: in-place edits, effective arguments, approval retries
# ---------------------------------------------------------------------------


async def test_in_place_argument_edit_does_not_reach_the_handler() -> None:
    class Mutator:
        name = "mutator"

        def before_tool(self, call: ToolHookCall) -> None:
            call.tool_call.arguments["path"] = 12345
            call.tool_call.arguments["bogus"] = {"x": 1}

        def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> None:
            call.tool_call.arguments["path"] = "after"

    later = _Hook("later")
    result, recorder = await _dispatch([Mutator(), later], {"path": "a"})

    assert recorder.calls == [{"path": "a"}]
    assert later.seen == [{"path": "a"}]
    assert not result.is_error
    assert result.effective_arguments is None


async def test_rewrite_reports_the_effective_arguments() -> None:
    result, recorder = await _dispatch(
        [_Hook("redirect", before=Rewrite({"path": "safe.txt"}))], {"path": "a"}
    )
    plain, _ = await _dispatch([_Hook("observer")], {"path": "a"})

    assert recorder.calls == [{"path": "safe.txt"}]
    assert result.effective_arguments == {"path": "safe.txt"}
    assert plain.effective_arguments is None


async def test_rewrite_denied_by_policy_still_reports_the_effective_arguments() -> None:
    handler_fn = build_tool_handler(
        _registry(_Recorder()),
        ToolContext(denied_tools={"read"}),
        tool_hooks=[_Hook("redirect", before=Rewrite({"path": "safe.txt"}))],
    )
    result = await handler_fn(_call({"path": "a"}))

    assert result.is_error
    assert result.effective_arguments == {"path": "safe.txt"}


async def test_rewrite_keeps_a_granted_approval_id() -> None:
    calls: list[dict[str, Any]] = []

    async def deploy(**arguments: Any) -> str:
        calls.append(arguments)
        return "ok"

    later = _Hook("later")
    handler_fn = build_tool_handler(
        _single_tool_registry(
            "deploy", deploy, command={"type": "string"}, approval_id={"type": "string"}
        ),
        None,
        tool_hooks=[_Hook("rewrite", before=Rewrite({"command": "deploy --safe"})), later],
    )
    await handler_fn(
        ToolCall(
            tool_use_id="c1",
            tool_name="deploy",
            arguments={"command": "deploy", "approval_id": "ap-1"},
        )
    )

    assert calls == [{"command": "deploy --safe", "approval_id": "ap-1"}]
    assert later.seen == [{"command": "deploy --safe", "approval_id": "ap-1"}]


async def test_cancelled_call_still_pairs_after_tool_without_a_result() -> None:
    import asyncio

    started = asyncio.Event()

    async def slow(**_: Any) -> str:
        started.set()
        await asyncio.sleep(10)
        return "never"

    observer = _Observer()
    handler_fn = build_tool_handler(
        _single_tool_registry("read", slow), None, tool_hooks=[observer]
    )
    task = asyncio.create_task(handler_fn(_call({"path": "a"})))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(observer.outcomes) == 1
    assert observer.outcomes[0].result is None
    assert isinstance(observer.outcomes[0].exception, asyncio.CancelledError)
