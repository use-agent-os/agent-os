from __future__ import annotations

import pytest

from agentos.observability.trace import (
    JsonlTraceSink,
    MemoryTraceSink,
    PrivacyGuardSink,
    TraceContext,
    TraceEvent,
    load_trace_events,
    write_trace_event,
)


def test_trace_context_child_inherits_parent_identity() -> None:
    parent = TraceContext.new(
        trace_id="trace-1",
        session_key="agent:main:test",
        session_id="session-1",
        turn_id="turn-1",
        task_id="task-1",
        agent_id="main",
    )

    child = parent.child(run_id="child-run", agent_id="child")

    assert child.trace_id == "trace-1"
    assert child.session_key == "agent:main:test"
    assert child.session_id == "session-1"
    assert child.turn_id == "turn-1"
    assert child.task_id == "task-1"
    assert child.parent_run_id == "task-1"
    assert child.run_id == "child-run"
    assert child.agent_id == "child"


def test_trace_event_serializes_required_contract_fields() -> None:
    context = TraceContext.new(
        trace_id="trace-1",
        session_key="agent:main:test",
        turn_id="turn-1",
        agent_id="main",
    )
    event = TraceEvent(
        kind="turn_start",
        context=context,
        privacy="diagnostic",
        seq=7,
        attrs={"source": "test"},
        payload={"message_hash": "abc123"},
    )

    payload = event.to_dict()

    assert payload["schema_version"] == 1
    assert payload["kind"] == "turn_start"
    assert payload["privacy"] == "diagnostic"
    assert payload["trace_id"] == "trace-1"
    assert payload["session_key"] == "agent:main:test"
    assert payload["turn_id"] == "turn-1"
    assert payload["agent_id"] == "main"
    assert payload["seq"] == 7
    assert payload["attrs"] == {"source": "test"}
    assert payload["payload"] == {"message_hash": "abc123"}


def test_privacy_guard_blocks_raw_events_by_default() -> None:
    sink = MemoryTraceSink()
    guarded = PrivacyGuardSink(sink)
    context = TraceContext.new(trace_id="trace-1", session_key="agent:main:test")

    guarded.write(TraceEvent(kind="turn_start", context=context, privacy="diagnostic"))

    assert len(sink.events) == 1
    with pytest.raises(ValueError, match="raw trace event"):
        guarded.write(
            TraceEvent(
                kind="llm_request",
                context=context,
                privacy="raw",
                payload={"messages": [{"role": "user", "content": "secret"}]},
            )
        )


def test_memory_trace_sink_filters_by_trace_id() -> None:
    sink = MemoryTraceSink()
    sink.write(TraceEvent(kind="turn_start", context=TraceContext.new(trace_id="trace-a")))
    sink.write(TraceEvent(kind="turn_start", context=TraceContext.new(trace_id="trace-b")))

    assert [event.trace_id for event in sink.by_trace_id("trace-b")] == ["trace-b"]


def test_jsonl_trace_sink_persists_and_loads_by_trace_id(tmp_path) -> None:
    sink = JsonlTraceSink(log_dir=tmp_path)
    sink.write(
        TraceEvent(
            kind="turn_start",
            context=TraceContext.new(
                trace_id="trace-a",
                session_key="agent:main:test",
                turn_id="turn-1",
            ),
            seq=1,
        )
    )
    write_trace_event(
        TraceEvent(
            kind="turn_start",
            context=TraceContext.new(trace_id="trace-b", turn_id="turn-2"),
            seq=1,
        ),
        log_dir=tmp_path,
    )

    [path] = list(tmp_path.glob("traces-*.jsonl"))
    assert path.exists()
    events = load_trace_events("trace-a", log_dir=tmp_path)

    assert [event.kind for event in events] == ["turn_start"]
    assert events[0].trace_id == "trace-a"
    assert events[0].context.session_key == "agent:main:test"
    assert events[0].context.turn_id == "turn-1"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"trace_id": ""}, "trace_id must be non-empty"),
        ({"trace_id": "trace-1", "session_key": " "}, "session_key must be non-empty"),
    ],
)
def test_trace_context_rejects_invalid_identity(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TraceContext.new(**kwargs)


def test_trace_context_child_rejects_blank_parent_override() -> None:
    parent = TraceContext.new(trace_id="trace-1", turn_id="turn-1")

    with pytest.raises(ValueError, match="parent_run_id must be non-empty"):
        parent.child(parent_run_id="")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": ""}, "kind must be non-empty"),
        ({"schema_version": 99}, "unsupported trace schema_version"),
        ({"privacy": "unsafe"}, "invalid trace privacy"),
    ],
)
def test_trace_event_rejects_invalid_contract(kwargs: dict[str, object], message: str) -> None:
    context = TraceContext.new(trace_id="trace-1")
    base = {"kind": "turn_start", "context": context}
    base.update(kwargs)

    with pytest.raises(ValueError, match=message):
        TraceEvent(**base)  # type: ignore[arg-type]
