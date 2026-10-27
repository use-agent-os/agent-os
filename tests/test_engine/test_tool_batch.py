"""ToolBatchExecutor: declared concurrency, streamed results, concurrent approvals.

Concurrency is proven with events and in-flight counters rather than
wall-clock thresholds wherever possible, so these stay deterministic on a
loaded CI runner. No network, no provider.
"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import agentos.engine.runtime as runtime_module
from agentos.engine import Agent, AgentConfig, ToolResult
from agentos.engine.tool_batch import (
    ToolBatchExecutor,
    _Admission,
    _OrderedGate,
    registry_spec_lookup,
    resolve_tool_concurrency,
)
from agentos.engine.types import RunHeartbeatEvent, ToolCall, ToolResultEvent, WarningEvent
from agentos.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderTextDelta
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.tools.batch_slot import wait_for_batch_turn, wait_outside_tool_slot
from agentos.tools.registry import ToolRegistry, tool
from agentos.tools.types import ToolSpec

_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "src" / "agentos" / "tools" / "builtin"

# The names that used to live in runtime._SAFE_TOOL_NAMES.
_FORMER_ALLOWLIST = frozenset(
    {
        "agents_list",
        "git_diff",
        "git_log",
        "git_status",
        "glob_search",
        "grep_search",
        "image",
        "list_dir",
        "memory_get",
        "memory_search",
        "pdf",
        "projects_list",
        "read_file",
        "read_spreadsheet",
        "session_search",
        "session_status",
        "sessions_history",
        "sessions_list",
        "skill_list",
        "skill_search_community",
        "skill_view",
        "tts",
        "web_fetch",
        "web_search",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(name: str, **kwargs: Any) -> ToolSpec:
    return ToolSpec(name=name, description=name, parameters={}, **kwargs)


def _call(index: int, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(tool_use_id=f"t{index}", tool_name=name, arguments=dict(arguments))


def _ok(call: ToolCall, content: str = "ok") -> ToolResult:
    return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=content)


async def _identity_project(result: ToolResult, *, tool_call: ToolCall | None = None) -> ToolResult:
    return result


def _executor(
    execute: Callable[[ToolCall], Awaitable[ToolResult]],
    specs: dict[str, ToolSpec],
    *,
    batch_seconds: float = 10.0,
    tool_seconds: float = 5.0,
    **kwargs: Any,
) -> ToolBatchExecutor:
    loop = asyncio.get_running_loop()
    return ToolBatchExecutor(
        execute=execute,
        project=_identity_project,
        timeout_for=lambda _call: tool_seconds,
        spec_for=specs.get,
        deadline=loop.time() + batch_seconds,
        iteration_timeout=batch_seconds,
        **kwargs,
    )


async def _drain(executor: ToolBatchExecutor, calls: list[ToolCall]) -> list[Any]:
    return [event async for event in executor.run(calls)]


def _result_ids(events: list[Any]) -> list[str]:
    return [event.tool_use_id for event in events if isinstance(event, ToolResultEvent)]


class _Tracker:
    """Records which calls are in flight at the same time."""

    def __init__(self) -> None:
        self.in_flight: set[str] = set()
        self.max_in_flight = 0
        self.overlaps: list[frozenset[str]] = []

    def enter(self, call: ToolCall) -> None:
        self.in_flight.add(call.tool_use_id)
        self.max_in_flight = max(self.max_in_flight, len(self.in_flight))
        self.overlaps.append(frozenset(self.in_flight))

    def leave(self, call: ToolCall) -> None:
        self.in_flight.discard(call.tool_use_id)

    def ever_together(self, a: str, b: str) -> bool:
        return any(a in seen and b in seen for seen in self.overlaps)


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


def test_tool_without_declared_concurrency_is_exclusive() -> None:
    assert resolve_tool_concurrency(_spec("legacy"), {}).mode == "exclusive"
    assert resolve_tool_concurrency(None, {}).mode == "exclusive"


def test_tool_decorator_defaults_keep_unknown_tools_exclusive() -> None:
    registry = ToolRegistry()

    @tool(name="plain_tool", description="plain", registry=registry)
    async def plain_tool() -> str:
        return "ok"

    registered = registry.get("plain_tool")
    assert registered is not None
    assert registered.spec.concurrency == "exclusive"
    assert registered.spec.read_only is False
    assert registered.spec.requires_approval is False
    assert registered.spec.concurrency_key is None


def test_keyed_policy_uses_key_function_and_falls_back_to_exclusive() -> None:
    spec = _spec(
        "send",
        concurrency="keyed",
        concurrency_key=lambda args, _session: args.get("to"),
    )
    assert resolve_tool_concurrency(spec, {"to": "a"}).key == "a"
    assert resolve_tool_concurrency(spec, {"to": "a"}).mode == "keyed"
    assert resolve_tool_concurrency(spec, {}).mode == "exclusive"

    def _broken(_args: Any, _session: str | None) -> Any:
        raise RuntimeError("boom")

    broken = _spec("broken", concurrency="keyed", concurrency_key=_broken)
    assert resolve_tool_concurrency(broken, {}).mode == "exclusive"
    # keyed without a key function serialises on the tool name
    assert resolve_tool_concurrency(_spec("k", concurrency="keyed"), {}).key == ("tool", "k")


def test_registry_lookup_prefers_agent_registry_then_default() -> None:
    registry = ToolRegistry()
    registry.register(_spec("read_file"), lambda: None)  # type: ignore[arg-type]
    lookup = registry_spec_lookup(registry)
    own = lookup("read_file")
    assert own is not None and own.concurrency == "exclusive"
    builtin = registry_spec_lookup(None)("read_file")
    assert builtin is not None and builtin.concurrency == "parallel"
    assert registry_spec_lookup(registry)("no_such_tool") is None


def test_safe_tool_allowlist_is_gone() -> None:
    assert not hasattr(runtime_module, "_SAFE_TOOL_NAMES")
    assert not hasattr(runtime_module, "_get_tool_concurrency_policy")


def _declared_concurrency_by_tool() -> dict[str, str]:
    declared: dict[str, str] = {}
    for path in sorted(_BUILTIN_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tool"
            ):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            name = keywords.get("name")
            if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
                continue
            mode = keywords.get("concurrency")
            declared[name.value] = mode.value if isinstance(mode, ast.Constant) else "exclusive"
    return declared


def test_former_allowlist_members_are_registered_parallel() -> None:
    # Some of these register through factories (skills, memory, session
    # search), so read the registration sites rather than a live registry.
    declared = _declared_concurrency_by_tool()
    missing = sorted(_FORMER_ALLOWLIST - set(declared))
    assert missing == []
    # skill_view left the parallel set on purpose: it loads the deferred tools a
    # skill requires, so a call batched after it must wait for that load.
    still_parallel = _FORMER_ALLOWLIST - {"skill_view"}
    assert {name: declared[name] for name in still_parallel} == dict.fromkeys(
        still_parallel, "parallel"
    )
    assert declared["skill_view"] == "exclusive"
    assert declared["sessions_send"] == "keyed"
    assert declared["sessions_spawn"] == "keyed"
    for name in ("exec_command", "write_file", "edit_file", "apply_patch", "http_request"):
        assert declared[name] == "exclusive", name


def test_session_tools_key_by_target_and_parent() -> None:
    lookup = registry_spec_lookup(None)
    send = resolve_tool_concurrency(lookup("sessions_send"), {"session_key": " agent:x "})
    assert send.mode == "keyed" and send.key == ("sessions_send", "agent:x")
    assert resolve_tool_concurrency(lookup("sessions_send"), {}).mode == "exclusive"
    spawn = resolve_tool_concurrency(lookup("sessions_spawn"), {}, session_key="agent:main:p")
    assert spawn.mode == "keyed" and spawn.key == ("sessions_spawn", "agent:main:p")


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


async def test_results_stream_as_they_finish_and_pair_in_original_order() -> None:
    specs = {
        "slow": _spec("slow", concurrency="parallel"),
        "fast": _spec("fast", concurrency="parallel"),
    }
    release_slow = asyncio.Event()
    slow_finished = False

    async def execute(call: ToolCall) -> ToolResult:
        nonlocal slow_finished
        if call.tool_name == "slow":
            await release_slow.wait()
            slow_finished = True
        return _ok(call, call.tool_name)

    executor = _executor(execute, specs)
    calls = [_call(0, "slow"), _call(1, "fast")]
    seen: list[str] = []
    slow_done_when_fast_seen: bool | None = None
    async for event in executor.run(calls):
        if isinstance(event, ToolResultEvent):
            seen.append(event.tool_use_id)
            if event.tool_use_id == "t1":
                slow_done_when_fast_seen = slow_finished
                release_slow.set()

    # The fast result reached the consumer while the slow tool was still running.
    assert seen == ["t1", "t0"]
    assert slow_done_when_fast_seen is False
    # The transcript still pairs in the order the model asked.
    assert [block.tool_use_id for block in executor.result_blocks()] == ["t0", "t1"]
    assert [block.content for block in executor.result_blocks()] == ["slow", "fast"]


async def test_keyed_serialises_same_key_and_parallelises_different_keys() -> None:
    specs = {
        "send": _spec(
            "send",
            concurrency="keyed",
            concurrency_key=lambda args, _session: ("send", args["to"]),
        )
    }
    tracker = _Tracker()
    both_b_and_a = asyncio.Event()

    async def execute(call: ToolCall) -> ToolResult:
        tracker.enter(call)
        if {"t0", "t2"} <= tracker.in_flight:
            both_b_and_a.set()
        try:
            await asyncio.wait_for(both_b_and_a.wait(), timeout=0.3)
        except TimeoutError:
            pass
        await asyncio.sleep(0.01)
        tracker.leave(call)
        return _ok(call)

    calls = [_call(0, "send", to="a"), _call(1, "send", to="a"), _call(2, "send", to="b")]
    events = await _drain(_executor(execute, specs), calls)

    assert sorted(_result_ids(events)) == ["t0", "t1", "t2"]
    assert tracker.ever_together("t0", "t2"), "different keys did not overlap"
    assert not tracker.ever_together("t0", "t1"), "same key overlapped"


async def test_exclusive_runs_alone_and_keeps_its_place() -> None:
    specs = {
        "read": _spec("read", concurrency="parallel"),
        "write": _spec("write"),  # declares nothing -> exclusive
    }
    tracker = _Tracker()
    order: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        tracker.enter(call)
        order.append(call.tool_use_id)
        await asyncio.sleep(0.02)
        tracker.leave(call)
        return _ok(call)

    calls = [_call(0, "read"), _call(1, "read"), _call(2, "write"), _call(3, "read")]
    await _drain(_executor(execute, specs), calls)

    assert tracker.ever_together("t0", "t1")
    for other in ("t0", "t1", "t3"):
        assert not tracker.ever_together("t2", other), f"exclusive overlapped {other}"
    # earlier calls start before the exclusive one, later calls after it
    assert order.index("t2") > max(order.index("t0"), order.index("t1"))
    assert order.index("t3") > order.index("t2")


async def test_tools_without_new_fields_run_serially() -> None:
    specs = {"a": _spec("a"), "b": _spec("b")}
    tracker = _Tracker()

    async def execute(call: ToolCall) -> ToolResult:
        tracker.enter(call)
        await asyncio.sleep(0.02)
        tracker.leave(call)
        return _ok(call)

    events = await _drain(_executor(execute, specs), [_call(0, "a"), _call(1, "b")])
    assert tracker.max_in_flight == 1
    assert _result_ids(events) == ["t0", "t1"]


async def test_parallel_is_bounded_by_max_concurrency() -> None:
    specs = {"r": _spec("r", concurrency="parallel")}
    tracker = _Tracker()

    async def execute(call: ToolCall) -> ToolResult:
        tracker.enter(call)
        await asyncio.sleep(0.02)
        tracker.leave(call)
        return _ok(call)

    calls = [_call(i, "r") for i in range(7)]
    await _drain(_executor(execute, specs, max_concurrency=3), calls)
    assert tracker.max_in_flight == 3


# ---------------------------------------------------------------------------
# Pairing under failure
# ---------------------------------------------------------------------------


async def test_pairing_survives_exception_timeout_and_batch_deadline() -> None:
    specs = {name: _spec(name, concurrency="parallel") for name in ("boom", "hang", "ok")}

    async def execute(call: ToolCall) -> ToolResult:
        if call.tool_name == "boom":
            raise RuntimeError("kaput")
        if call.tool_name == "hang":
            await asyncio.sleep(30)
        return _ok(call)

    executor = _executor(execute, specs, tool_seconds=0.05)
    calls = [_call(0, "hang"), _call(1, "boom"), _call(2, "ok")]
    events = await _drain(executor, calls)

    assert sorted(_result_ids(events)) == ["t0", "t1", "t2"]
    outcomes = executor.outcomes
    assert [o.result.tool_use_id for o in outcomes] == ["t0", "t1", "t2"]
    assert outcomes[0].result.is_error and "timed out" in outcomes[0].result.content
    assert outcomes[1].result.is_error and "kaput" in outcomes[1].result.content
    assert not outcomes[2].result.is_error


class _FakeClock:
    """The batch clock, moved only by the test."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _clocked_executor(
    execute: Callable[[ToolCall], Awaitable[ToolResult]],
    specs: dict[str, ToolSpec],
    clock: _FakeClock,
    *,
    batch_seconds: float = 10.0,
    **kwargs: Any,
) -> ToolBatchExecutor:
    # Every budget is long in real time (the per-tool ``wait_for`` uses the
    # real loop clock), so only the fake clock can end the batch.
    kwargs.setdefault("heartbeat_interval", 0.005)
    return ToolBatchExecutor(
        execute=execute,
        project=_identity_project,
        timeout_for=lambda _call: 30.0,
        spec_for=specs.get,
        deadline=clock() + batch_seconds,
        iteration_timeout=batch_seconds,
        clock=clock,
        **kwargs,
    )


async def test_batch_deadline_times_out_running_and_queued_calls() -> None:
    """The deadline passes while t0 runs: t0 is cancelled, queued t1 never starts.

    Deterministic: the batch clock is fake and crosses the deadline only once
    t0 is known to be running, and no real timer is short enough to fire.
    """

    specs = {"hang": _spec("hang")}  # exclusive: t1 is still queued at the deadline
    clock = _FakeClock()
    started: list[str] = []
    cancelled: list[str] = []
    t0_running = asyncio.Event()

    async def execute(call: ToolCall) -> ToolResult:
        started.append(call.tool_use_id)
        t0_running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(call.tool_use_id)
            raise
        return _ok(call)

    async def _cross_deadline() -> None:
        await t0_running.wait()
        clock.now += 10.0

    executor = _clocked_executor(execute, specs, clock)
    crosser = asyncio.create_task(_cross_deadline())
    events = await asyncio.wait_for(_drain(executor, [_call(0, "hang"), _call(1, "hang")]), 5.0)
    await crosser

    assert _result_ids(events) == ["t0", "t1"]
    assert started == ["t0"]
    assert cancelled == ["t0"]
    for outcome in executor.outcomes:
        assert outcome.result.is_error
        assert outcome.result.content.endswith("timed out after 10.0s")


async def test_queued_call_is_not_started_once_the_batch_deadline_has_passed() -> None:
    """Regression for the windows-latest flake.

    t0 ends on its own exactly at the batch deadline (its timeout is capped to
    the batch's remaining time, so both expire together). That frees the gate
    and admits t1 before the batch loop sees the deadline; t1 must time out
    unstarted rather than run a tool the batch has already given up on.
    """

    specs = {"hang": _spec("hang")}
    clock = _FakeClock()
    started: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        started.append(call.tool_use_id)
        if call.tool_use_id == "t0":
            clock.now += 10.0  # the deadline passes while t0 is running
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="Tool 'hang' timed out after 10.0s",
                is_error=True,
            )
        return _ok(call)

    executor = _clocked_executor(execute, specs, clock, heartbeat_interval=15.0)
    events = await asyncio.wait_for(_drain(executor, [_call(0, "hang"), _call(1, "hang")]), 5.0)

    assert started == ["t0"]
    assert _result_ids(events) == ["t0", "t1"]
    assert [o.result.tool_use_id for o in executor.outcomes] == ["t0", "t1"]
    assert executor.outcomes[1].result.is_error
    assert executor.outcomes[1].result.content.endswith("timed out after 10.0s")


async def test_heartbeat_while_tools_run() -> None:
    specs = {"slow": _spec("slow")}

    async def execute(call: ToolCall) -> ToolResult:
        await asyncio.sleep(0.08)
        return _ok(call)

    events = await _drain(_executor(execute, specs, heartbeat_interval=0.02), [_call(0, "slow")])
    kinds = [type(event) for event in events]
    assert RunHeartbeatEvent in kinds
    assert kinds.index(RunHeartbeatEvent) < kinds.index(ToolResultEvent)


async def test_pending_warnings_follow_the_result_that_raised_them() -> None:
    warnings: list[WarningEvent] = []
    specs = {"w": _spec("w")}

    async def execute(call: ToolCall) -> ToolResult:
        warnings.append(WarningEvent(code="c", message="m"))
        return _ok(call)

    events = await _drain(_executor(execute, specs, pending_warnings=warnings), [_call(0, "w")])
    assert [type(event) for event in events] == [ToolResultEvent, WarningEvent]
    assert warnings == []


async def test_gate_releases_slot_of_task_cancelled_before_start() -> None:
    gate = _OrderedGate()
    first = gate.reserve(exclusive=True)
    second = gate.reserve(exclusive=True)
    assert first.future.done() and not second.future.done()
    gate.release(first)
    assert second.future.done()
    third = gate.reserve(exclusive=False)
    gate.release(second)  # never awaited: still frees the gate
    assert third.future.done()
    gate.release(third)
    gate.release(third)  # idempotent


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


def _pending(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(content)
    except ValueError:
        return None
    if isinstance(payload, dict) and payload.get("status") == "approval_required":
        return payload
    return None


async def test_approvals_are_awaited_concurrently_and_retried_as_granted() -> None:
    specs = {"exec": _spec("exec", requires_approval=True)}
    decisions: dict[str, asyncio.Future[bool]] = {}
    waiting: set[str] = set()
    max_waiting = 0
    both_waiting = asyncio.Event()

    async def wait_for_approval(payload: dict[str, Any], *, timeout: float) -> None:
        nonlocal max_waiting
        approval_id = payload["approval_id"]
        waiting.add(approval_id)
        max_waiting = max(max_waiting, len(waiting))
        if len(waiting) == 2:
            both_waiting.set()
        await decisions[approval_id]
        waiting.discard(approval_id)

    async def execute(call: ToolCall) -> ToolResult:
        approval_id = call.arguments.get("approval_id")
        if approval_id is None:
            approval_id = f"ap-{call.tool_use_id}"
            decisions[approval_id] = asyncio.get_running_loop().create_future()
            return _ok(
                call, json.dumps({"status": "approval_required", "approval_id": approval_id})
            )
        if decisions[approval_id].result():
            return _ok(call, f"ran {call.tool_use_id}")
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=json.dumps({"status": "approval_denied", "approval_id": approval_id}),
            is_error=True,
        )

    executor = _executor(
        execute,
        specs,
        pending_approval=_pending,
        wait_for_approval=wait_for_approval,
    )
    calls = [_call(0, "exec"), _call(1, "exec")]
    results: list[ToolResultEvent] = []

    async def _consume() -> None:
        async for event in executor.run(calls):
            if isinstance(event, ToolResultEvent):
                results.append(event)

    consumer = asyncio.create_task(_consume())
    await asyncio.wait_for(both_waiting.wait(), timeout=2.0)
    assert max_waiting == 2  # both approvals outstanding at once

    # The second approval is granted first; its retry must not wait for the first.
    decisions["ap-t1"].set_result(True)
    for _ in range(50):
        if any(r.result == "ran t1" for r in results):
            break
        await asyncio.sleep(0.01)
    assert any(r.result == "ran t1" for r in results)
    assert not consumer.done()

    decisions["ap-t0"].set_result(False)  # denied: yields a denied result, blocks nothing
    await asyncio.wait_for(consumer, timeout=2.0)

    assert [r.tool_use_id for r in results] == ["t0", "t1", "t1", "t0"]
    outcomes = executor.outcomes
    assert [o.result.tool_use_id for o in outcomes] == ["t0", "t1"]
    assert "approval_denied" in outcomes[0].result.content
    assert outcomes[1].result.content == "ran t1"
    assert outcomes[1].call.arguments == {"approval_id": "ap-t1"}


async def _until(predicate: Callable[[], bool], *, limit: int = 500) -> None:
    """Yield to the loop until *predicate* holds (bounded, no wall clock)."""

    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def test_inline_approval_waits_free_the_exclusive_slot() -> None:
    """Calls that wait for approval inside the tool (exec on the Web UI) ask at once.

    Both exclusive calls must have requested their approval before either is
    answered, and a parallel call queued behind them runs meanwhile. Once
    approved, the tool bodies still run one at a time, and the transcript
    order is the order the model emitted the calls.
    """

    specs = {
        "exec": _spec("exec", early_approval=True),
        "read": _spec("read", concurrency="parallel"),
    }
    loop = asyncio.get_running_loop()
    decisions: dict[str, asyncio.Future[bool]] = {}
    tracker = _Tracker()
    bodies: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        if call.tool_name == "read":
            tracker.enter(call)
            tracker.leave(call)
            return _ok(call, "read")
        decisions[call.tool_use_id] = loop.create_future()
        approved = await wait_outside_tool_slot(decisions[call.tool_use_id])
        tracker.enter(call)
        bodies.append(call.tool_use_id)
        for _ in range(5):  # give an overlapping body every chance to show up
            await asyncio.sleep(0)
        tracker.leave(call)
        return _ok(call, f"ran {call.tool_use_id}" if approved else "denied")

    executor = _executor(execute, specs)
    results: list[ToolResultEvent] = []

    async def _consume() -> None:
        async for event in executor.run([_call(0, "exec"), _call(1, "exec"), _call(2, "read")]):
            if isinstance(event, ToolResultEvent):
                results.append(event)

    consumer = asyncio.create_task(_consume())
    await _until(lambda: len(decisions) == 2 and any(r.tool_use_id == "t2" for r in results))
    assert not any(future.done() for future in decisions.values())

    # Answer the second first, then the first; then both bodies race for the slot.
    decisions["t1"].set_result(True)
    decisions["t0"].set_result(True)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert not tracker.ever_together("t0", "t1")
    # Approved out of order, the commands still run in the order emitted.
    assert bodies == ["t0", "t1"]
    assert [o.result.tool_use_id for o in executor.outcomes] == ["t0", "t1", "t2"]
    assert [o.result.content for o in executor.outcomes] == ["ran t0", "ran t1", "read"]
    assert sorted(r.tool_use_id for r in results) == ["t0", "t1", "t2"]


async def test_inline_approval_wait_keeps_key_lock_order_after_readmission() -> None:
    """A keyed call that waits outside its slot re-takes its key before running."""

    specs = {"k": _spec("k", concurrency="keyed")}
    loop = asyncio.get_running_loop()
    gate_open: asyncio.Future[bool] = loop.create_future()
    tracker = _Tracker()

    async def execute(call: ToolCall) -> ToolResult:
        if call.tool_use_id == "t0":
            await wait_outside_tool_slot(gate_open)
        tracker.enter(call)
        for _ in range(5):
            await asyncio.sleep(0)
        tracker.leave(call)
        return _ok(call, call.tool_use_id)

    executor = _executor(execute, specs)
    consumer = asyncio.create_task(_drain(executor, [_call(0, "k"), _call(1, "k")]))
    await _until(lambda: "t1" in {c for seen in tracker.overlaps for c in seen})
    gate_open.set_result(True)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert not tracker.ever_together("t0", "t1")
    assert [o.result.content for o in executor.outcomes] == ["t0", "t1"]


async def test_batch_deadline_while_waiting_outside_the_slot_pairs_every_call() -> None:
    specs = {"exec": _spec("exec", early_approval=True)}
    clock = _FakeClock()
    loop = asyncio.get_running_loop()
    waiting: list[str] = []
    never: asyncio.Future[bool] = loop.create_future()

    async def execute(call: ToolCall) -> ToolResult:
        waiting.append(call.tool_use_id)
        await wait_outside_tool_slot(asyncio.shield(never))
        return _ok(call)

    async def _cross_deadline() -> None:
        await _until(lambda: len(waiting) == 2)
        clock.now += 10.0

    executor = _clocked_executor(execute, specs, clock)
    crosser = asyncio.create_task(_cross_deadline())
    events = await asyncio.wait_for(_drain(executor, [_call(0, "exec"), _call(1, "exec")]), 5.0)
    await crosser

    assert sorted(_result_ids(events)) == ["t0", "t1"]
    assert all("timed out" in o.result.content for o in executor.outcomes)
    never.cancel()


async def test_time_queued_for_readmission_does_not_count_against_the_call_timeout() -> None:
    """Like a CLI retry, whose timer starts once admitted: waiting for another
    call's command to finish before running is not the call's own run time."""

    gate = _OrderedGate()
    admission = _Admission(gate=gate, slot=gate.reserve(exclusive=True), exclusive=True)
    await admission.acquire()
    held_for = 0.02

    async def _other_call() -> None:
        other = gate.reserve(exclusive=True)
        await other.future  # admitted once ``admission`` steps out
        await _until(lambda: bool(gate._queue))  # ``admission`` queued again
        await asyncio.sleep(held_for)  # the other call's command runs
        gate.release(other)

    other_task = asyncio.create_task(_other_call())
    async with asyncio.timeout(30.0) as scope:
        admission.timeout_scope = scope
        original = scope.when()
        assert original is not None
        await admission.wait_outside(asyncio.sleep(0))
        extended = scope.when()
    await other_task

    # The loop clock ticks coarsely on Windows (~15.6ms), so a 20ms sleep can
    # read as one tick; allow one clock step of slack.
    tick = time.get_clock_info("monotonic").resolution
    assert extended is not None and extended - original >= held_for - tick
    assert not admission.yielded
    admission.release()


async def test_wait_outside_tool_slot_is_a_plain_await_outside_a_batch() -> None:
    async def _value() -> int:
        return 7

    assert await wait_outside_tool_slot(_value()) == 7
    await wait_for_batch_turn()  # a no-op outside a batch


def _exec_like(
    decisions: dict[str, asyncio.Future[bool]], ran: list[str], entered: list[str]
) -> Callable[[ToolCall], Awaitable[ToolResult]]:
    """Mirror exec_command: an optional inline approval, then the turn, then the command."""

    async def execute(call: ToolCall) -> ToolResult:
        entered.append(call.tool_use_id)
        decision = decisions.get(call.tool_use_id)
        if decision is not None and not await wait_outside_tool_slot(decision):
            return _ok(call, "denied")
        await wait_for_batch_turn()
        ran.append(call.tool_use_id)
        for _ in range(5):
            await asyncio.sleep(0)
        return _ok(call, f"ran {call.tool_use_id}")

    return execute


async def test_later_exec_does_not_run_before_an_earlier_exec_awaiting_approval() -> None:
    """[exec "rm -rf dist" (needs approval), exec "cp -r build dist"]: cp must not
    run first and then have rm delete its result once the operator clicks."""

    specs = {"exec": _spec("exec", early_approval=True)}
    loop = asyncio.get_running_loop()
    decisions: dict[str, asyncio.Future[bool]] = {"t0": loop.create_future()}
    ran: list[str] = []
    entered: list[str] = []

    executor = _executor(_exec_like(decisions, ran, entered), specs)
    consumer = asyncio.create_task(_drain(executor, [_call(0, "exec"), _call(1, "exec")]))
    # t1 gets as far as its (absent) approval prompt, but no further.
    await _until(lambda: entered == ["t0", "t1"])
    for _ in range(20):
        await asyncio.sleep(0)
    assert ran == []

    decisions["t0"].set_result(True)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert ran == ["t0", "t1"]
    assert [o.result.content for o in executor.outcomes] == ["ran t0", "ran t1"]


async def test_exclusive_tool_without_early_approval_waits_its_turn() -> None:
    """A plain exclusive call is not admitted while an earlier one awaits approval;
    a later exec still raises its own prompt only after it, in order."""

    specs = {"exec": _spec("exec", early_approval=True), "write": _spec("write")}
    loop = asyncio.get_running_loop()
    decisions: dict[str, asyncio.Future[bool]] = {
        "t0": loop.create_future(),
        "t2": loop.create_future(),
    }
    ran: list[str] = []
    entered: list[str] = []
    exec_like = _exec_like(decisions, ran, entered)

    async def execute(call: ToolCall) -> ToolResult:
        if call.tool_name == "write":
            entered.append(call.tool_use_id)
            ran.append(call.tool_use_id)
            return _ok(call, "wrote")
        return await exec_like(call)

    executor = _executor(execute, specs)
    calls = [_call(0, "exec"), _call(1, "write"), _call(2, "exec")]
    consumer = asyncio.create_task(_drain(executor, calls))
    await _until(lambda: entered == ["t0"])
    for _ in range(20):
        await asyncio.sleep(0)
    assert entered == ["t0"] and ran == []

    decisions["t0"].set_result(True)
    await _until(lambda: "t2" in entered)
    assert ran == ["t0", "t1"]
    decisions["t2"].set_result(True)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert ran == ["t0", "t1", "t2"]
    assert [o.result.content for o in executor.outcomes] == ["ran t0", "wrote", "ran t2"]


async def test_denied_earlier_exec_lets_the_later_one_run() -> None:
    specs = {"exec": _spec("exec", early_approval=True)}
    loop = asyncio.get_running_loop()
    decisions: dict[str, asyncio.Future[bool]] = {
        "t0": loop.create_future(),
        "t1": loop.create_future(),
    }
    ran: list[str] = []
    entered: list[str] = []

    executor = _executor(_exec_like(decisions, ran, entered), specs)
    consumer = asyncio.create_task(_drain(executor, [_call(0, "exec"), _call(1, "exec")]))
    await _until(lambda: entered == ["t0", "t1"])  # both prompts pending at once
    decisions["t1"].set_result(True)
    for _ in range(20):
        await asyncio.sleep(0)
    assert ran == []  # approved, but t0 has not finished yet
    decisions["t0"].set_result(False)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert ran == ["t1"]
    assert [o.result.content for o in executor.outcomes] == ["denied", "ran t1"]


async def test_call_timeout_does_not_run_while_queued_for_readmission() -> None:
    """The tool timeout must not expire while the call waits to be admitted again:
    an approved command would otherwise time out without ever running."""

    gate = _OrderedGate()
    admission = _Admission(gate=gate, slot=gate.reserve(exclusive=True), exclusive=True)
    await admission.acquire()
    loop = asyncio.get_running_loop()

    async def _other_call() -> None:
        other = gate.reserve(exclusive=True)
        await other.future  # admitted once ``admission`` steps out
        await _until(lambda: bool(gate._queue))  # ``admission`` queued again
        await asyncio.sleep(0.045)  # longer than the call's whole timeout
        gate.release(other)

    other_task = asyncio.create_task(_other_call())
    async with asyncio.timeout(0.03) as scope:
        admission.timeout_scope = scope
        await admission.wait_outside(asyncio.sleep(0))
        resumed = scope.when()
        now = loop.time()
    await other_task

    assert resumed is not None and 0 < resumed - now <= 0.03
    admission.release()


async def test_readmission_timeout_is_capped_by_the_batch_clock() -> None:
    gate = _OrderedGate()
    admission = _Admission(
        gate=gate,
        slot=gate.reserve(exclusive=True),
        exclusive=True,
        batch_remaining=lambda: 5.0,
    )
    await admission.acquire()
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(30.0) as scope:
        admission.timeout_scope = scope
        await admission.wait_outside(asyncio.sleep(0))
        resumed = scope.when()
        now = loop.time()
    assert resumed is not None and resumed - now <= 5.0
    admission.release()


async def test_call_admitted_again_after_the_batch_deadline_does_not_run() -> None:
    specs = {"exec": _spec("exec", early_approval=True)}
    clock = _FakeClock()
    loop = asyncio.get_running_loop()
    decision: asyncio.Future[bool] = loop.create_future()
    entered: list[str] = []
    ran: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        entered.append(call.tool_use_id)
        try:
            await wait_outside_tool_slot(decision)
        except TimeoutError:  # what exec does; must not let the command run
            pass
        ran.append(call.tool_use_id)
        return _ok(call, "ran")

    executor = _clocked_executor(execute, specs, clock, heartbeat_interval=15.0)
    consumer = asyncio.create_task(_drain(executor, [_call(0, "exec")]))
    await _until(lambda: entered == ["t0"])
    clock.now += 10.0  # the batch deadline passes while the operator decides
    decision.set_result(True)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert ran == []
    assert executor.outcomes[0].result.is_error
    assert executor.outcomes[0].result.content.endswith("timed out after 10.0s")


# ---------------------------------------------------------------------------
# Through the agent loop
# ---------------------------------------------------------------------------


class _FixedToolCallProvider:
    provider_name = "fake"

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(list(messages))
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > 1:
            yield ProviderTextDelta(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        for i, name in enumerate(self._tool_names):
            yield ProviderToolUseStart(tool_use_id=f"tool-{i}", tool_name=name)
            yield ProviderToolUseEnd(tool_use_id=f"tool-{i}", tool_name=name, arguments={})
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


async def test_agent_streams_first_result_before_batch_finishes() -> None:
    """Measurement: the fast tool's result reaches the consumer mid-batch."""

    registry = ToolRegistry()
    for name in ("slow_read", "fast_read"):
        registry.register(_spec(name, concurrency="parallel", read_only=True), lambda: None)  # type: ignore[arg-type]

    release_slow = asyncio.Event()
    slow_running_when_fast_seen: bool | None = None
    slow_running = False

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal slow_running
        if call.tool_name == "slow_read":
            slow_running = True
            await release_slow.wait()
            slow_running = False
        return _ok(call, call.tool_name)

    provider = _FixedToolCallProvider(["slow_read", "fast_read"])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[
            ToolDefinition(name=n, description=n, input_schema=ToolInputSchema())
            for n in ("slow_read", "fast_read")
        ],
        tool_handler=handler,
        tool_registry=registry,
    )

    order: list[str] = []
    async for event in agent.run_turn("go"):
        if isinstance(event, ToolResultEvent):
            order.append(event.tool_name)
            if event.tool_name == "fast_read":
                slow_running_when_fast_seen = slow_running
                release_slow.set()

    assert order == ["fast_read", "slow_read"]
    assert slow_running_when_fast_seen is True
    # The provider sees one tool_result per tool_use, in the order requested.
    second_request = provider.calls[1]
    result_ids = [
        block.tool_use_id
        for msg in second_request
        if isinstance(msg.content, list)
        for block in msg.content
        if getattr(block, "type", None) == "tool_result"
    ]
    assert result_ids == ["tool-0", "tool-1"]


def test_default_max_tool_concurrency_is_eight() -> None:
    assert AgentConfig().max_safe_tool_concurrency == 8


async def test_inline_approval_wait_goes_through_outside_wait_hook() -> None:
    """An inline approval wait is handed to ``outside_wait`` so the turn can time it."""

    specs = {"exec": _spec("exec")}
    loop = asyncio.get_running_loop()
    decision: asyncio.Future[bool] = loop.create_future()
    seen: list[str] = []

    async def outside_wait(awaitable):  # type: ignore[no-untyped-def]
        seen.append("enter")
        try:
            return await awaitable
        finally:
            seen.append("exit")

    async def execute(call: ToolCall) -> ToolResult:
        approved = await wait_outside_tool_slot(decision)
        seen.append("body")
        return _ok(call, "ran" if approved else "denied")

    executor = _executor(execute, specs)
    executor.outside_wait = outside_wait

    async def _consume() -> None:
        async for _event in executor.run([_call(0, "exec")]):
            pass

    consumer = asyncio.create_task(_consume())
    await _until(lambda: seen == ["enter"])
    decision.set_result(True)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert seen == ["enter", "exit", "body"]
    assert [o.result.content for o in executor.outcomes] == ["ran"]


async def test_exclusive_call_does_not_overtake_shared_calls_emitted_before_it() -> None:
    """[write, read, read, write] runs in the model's order.

    Shared calls used to queue behind every exclusive call (no rank), so the
    second write ran before the two reads the model emitted ahead of it.
    """

    specs = {"write": _spec("write"), "read": _spec("read", concurrency="parallel")}
    started: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        started.append(call.tool_use_id)
        await asyncio.sleep(0)
        return _ok(call)

    executor = _executor(execute, specs)
    calls = [_call(0, "write"), _call(1, "read"), _call(2, "read"), _call(3, "write")]
    async for _event in executor.run(calls):
        pass

    assert started == ["t0", "t1", "t2", "t3"]
    assert [o.result.tool_use_id for o in executor.outcomes] == ["t0", "t1", "t2", "t3"]
