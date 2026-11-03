"""End to end: sessions_spawn(runtime="acp") -> TaskRuntime -> dispatch ->
AcpSessionManager -> projector -> events, transcript, usage, announce."""

from __future__ import annotations

import asyncio
import json

import pytest
from acp_runtime_helpers import wait_until
from acp_s_fakes import FakeRuntime, Script
from acp_s_harness import PARENT_KEY, acp_rig

from agentos.acp.types import AcpEvent, AcpTurnResult, AcpTurnTimeoutError, AcpUsage
from agentos.tools.builtin import sessions as sessions_tool


def _coding_script() -> Script:
    return Script(
        events=[
            AcpEvent(kind="thought_delta", text="planning"),
            AcpEvent(kind="tool_call", tool_call_id="t1", tool_kind="edit", title="Edit a.py"),
            AcpEvent(
                kind="tool_call_update", tool_call_id="t1", status="completed", raw_output="ok"
            ),
            AcpEvent(kind="usage", usage={"used": 10, "size": 100}, cost_usd=0.03),
            AcpEvent(kind="text_delta", text="Fixed the bug."),
        ],
        result=AcpTurnResult(
            stop_reason="end_turn",
            usage=AcpUsage(input_tokens=1200, output_tokens=300, cost_usd=0.03),
            tool_calls=1,
            duration_ms=10,
        ),
    )


async def _completions(rig, n: int = 1) -> list[dict]:
    """Parent-side completion rows; the announce runs just after the task settles."""
    for _ in range(200):
        rows = await rig.sessions.read_recent_transcript(PARENT_KEY, 20)
        found = [json.loads(r["content"]) for r in rows if r["role"] == "system"]
        if len(found) >= n:
            return found
        await asyncio.sleep(0.005)
    return found


async def _spawn(**kwargs) -> dict:
    return json.loads(await sessions_tool.sessions_spawn(runtime="acp", **kwargs))


async def test_spawned_acp_turn_streams_persists_and_announces(tmp_path) -> None:
    async with acp_rig(tmp_path, FakeRuntime([_coding_script()])) as rig:
        out = await _spawn(task="fix the bug", acp_agent="claude")
        child = out["session_key"]
        record = await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert str(record.status.value if hasattr(record.status, "value") else record.status) == (
            "succeeded"
        )

        names = rig.event_names(child)
        assert "session.event.tool_use_start" in names
        assert "session.event.tool_result" in names
        assert names.index("session.event.text_delta") < names.index("session.event.done")
        assert "session.event.error" not in names
        done = [p for k, n, p in rig.events if k == child and n == "session.event.done"][0]
        assert done["input_tokens"] == 1200 and abs(done["cost_usd"] - 0.03) < 1e-9
        assert rig.turn_runner.calls == 0

        transcript = await rig.sessions.read_recent_transcript(child, 10)
        roles = [row["role"] for row in transcript]
        assert roles == ["user", "assistant"]
        assistant = transcript[-1]
        assert assistant["content"] == "Fixed the bug."
        assert [s["type"] for s in assistant["tool_calls"]] == ["tool_use", "tool_result", "text"]

        row = await rig.sessions.get_session(child)
        assert row.input_tokens == 1200 and row.output_tokens == 300
        assert abs(row.total_cost_usd - 0.03) < 1e-9 and row.cost_source == "harness"
        assert row.origin["record_id"] == "rec-1"
        # oneshot by default: the process is gone but the session is idle, not closed
        assert row.origin["idle"] is True and not row.origin.get("closed")
        assert row.origin["last_turn"]["input_tokens"] == 1200

        completions = await _completions(rig)
        assert completions and completions[-1]["child_session_key"] == child
        stats = completions[-1]["stats"]
        assert stats.startswith("runtime ") and "tokens 1500 (in 1200 / out 300)" in stats
        assert "cost $0.0300" in stats
        # The parent transcript row is written before the live event goes out.
        assert await wait_until(
            lambda: any(n == "session.event.subagent_completion" for _, n, _ in rig.events)
        )
        live = [p for _, n, p in rig.events if n == "session.event.subagent_completion"]
        # Contract B: the persisted parent message and the live event carry the
        # harness identity and structured usage next to the Stats string.
        for payload in (completions[-1], live[-1]):
            assert payload["runtime"] == "acp"
            assert payload["acp_agent"] == "claude"
            assert payload["acp_agent_name"] == "Claude Code"
            assert payload["cwd"] == str(rig.workspace.resolve())
            assert payload["mode"] == "oneshot"
            assert payload["harness_mode"] is None
            usage = payload["usage"]
            assert set(usage) == {
                "duration_ms",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cost_usd",
                "tool_calls",
            }
            assert usage["input_tokens"] == 1200 and usage["output_tokens"] == 300
            assert usage["total_tokens"] == 1500 and usage["tool_calls"] == 1
            assert abs(usage["cost_usd"] - 0.03) < 1e-9
            assert isinstance(usage["duration_ms"], int)
            assert payload["stats"] == stats


async def test_parent_wake_message_carries_stats_line(tmp_path) -> None:
    async with acp_rig(tmp_path, FakeRuntime([_coding_script()])) as rig:
        sent: list[str] = []
        original_send = rig.task_runtime.send

        async def _capture(session_key, message, provenance=None, stream_event_sink=None):
            if session_key == PARENT_KEY:
                sent.append(message)
                from types import SimpleNamespace

                return SimpleNamespace(task_id="wake")
            return await original_send(session_key, message, provenance=provenance)

        rig.task_runtime.send = _capture  # type: ignore[method-assign]
        out = await _spawn(task="fix", acp_agent="claude")
        # Close the spawn group like sessions_yield does, then let the child finish.
        await sessions_tool.sessions_yield()
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert await wait_until(lambda: bool(sent)), "parent was not woken"
        assert "Stats: runtime" in sent[-1]
        assert "Fixed the bug." in sent[-1]


async def test_timeout_fails_task_with_timeout_status(tmp_path) -> None:
    script = Script(raise_exc=AcpTurnTimeoutError("ACP turn timed out after 5s"))
    async with acp_rig(tmp_path, FakeRuntime([script])) as rig:
        out = await _spawn(task="slow", acp_agent="claude")
        record = await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "timeout"
        child = out["session_key"]
        errors = [p for k, n, p in rig.events if k == child and n == "session.event.error"]
        assert errors and errors[0]["code"] == "ACP_TURN_TIMEOUT"
        row = await rig.sessions.get_session(child)
        assert row.origin["last_turn"]["error_code"] == "ACP_TURN_TIMEOUT"


async def test_unavailable_manager_fails_turn_cleanly(tmp_path) -> None:
    from agentos.acp.manager import AcpSessionManager, set_acp_manager

    async with acp_rig(tmp_path) as rig:
        out = await _spawn(task="x", acp_agent="claude")
        # Runtime disappears between spawn and dispatch.
        set_acp_manager(AcpSessionManager(None, unavailable_reason="runtime missing"))
        record = await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "failed"
        errors = [p for k, n, p in rig.events if n == "session.event.error"]
        assert errors and errors[0]["code"] == "ACP_UNAVAILABLE"
        assert rig.turn_runner.calls == 0


async def test_task_runtime_cancel_cancels_harness_turn(tmp_path) -> None:
    runtime = FakeRuntime([Script(block=True)])
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="long", acp_agent="claude", mode="persistent")
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0)
        await rig.task_runtime.cancel(session_key=out["session_key"], source="test", reason="t")
        record = await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "cancelled"
        assert runtime.turn_cancels == 1


async def test_persistent_followup_and_steer(tmp_path) -> None:
    runtime = FakeRuntime(
        [
            Script(
                events=[AcpEvent(kind="text_delta", text="first")],
                result=AcpTurnResult(stop_reason="end_turn"),
            ),
            Script(block=True),
            Script(
                events=[AcpEvent(kind="text_delta", text="steered")],
                result=AcpTurnResult(stop_reason="end_turn"),
            ),
        ]
    )
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="start", acp_agent="claude", mode="persistent")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        await _completions(rig, 1)
        assert (await rig.sessions.get_session(child)).status == "done"

        follow = json.loads(await sessions_tool.sessions_send(child, "keep going"))
        assert follow["runtime"] == "acp" and follow["mode"] == "followup"
        row = await rig.sessions.get_session(child)
        assert row.status == "running"
        await asyncio.wait_for(_wait_for_calls(runtime, 2), timeout=5)

        steer = json.loads(await sessions_tool.sessions_send(child, "do X instead", mode="steer"))
        assert steer["mode"] == "steer"
        await rig.task_runtime.wait(steer["task_id"], timeout=5)
        await rig.task_runtime.wait(follow["task_id"], timeout=5)

        completions = await _completions(rig, 2)
        row = await rig.sessions.get_session(child)
        assert follow["task_id"] in row.origin["superseded_task_ids"]
        assert row.status == "done"
        # Only one harness process for the whole conversation.
        assert [c[0] for c in runtime.calls].count("ensure_session") == 1
        # spawn turn + steered turn announce; the superseded one stays silent.
        assert [c["status"] for c in completions] == ["succeeded", "succeeded"]
        assert completions[-1]["result"]["text"] == "steered"
        transcript = await rig.sessions.read_recent_transcript(child, 20)
        assert [r["content"] for r in transcript if r["role"] == "user"] == [
            "start",
            "keep going",
            "do X instead",
        ]


async def _wait_for_calls(runtime: FakeRuntime, n: int) -> None:
    while [c[0] for c in runtime.calls].count("start_turn") < n:
        await asyncio.sleep(0.005)


async def test_oneshot_followup_resumes_the_harness_session(tmp_path) -> None:
    """2026-10-19 repro: a follow-up into a finished oneshot child got
    ACP_SESSION_CLOSED. It now resumes the recorded harness session in a new
    process, which is closed again after the turn."""
    runtime = FakeRuntime(
        [
            Script(
                events=[AcpEvent(kind="text_delta", text="first answer")],
                result=AcpTurnResult(stop_reason="end_turn"),
            ),
            Script(
                events=[AcpEvent(kind="text_delta", text="second answer")],
                result=AcpTurnResult(stop_reason="end_turn"),
            ),
        ]
    )
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="once", acp_agent="claude")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        await _completions(rig, 1)
        row = await rig.sessions.get_session(child)
        assert row.origin["idle"] is True and row.origin["acp_session_id"] == "sess-1"
        assert runtime.closed == [("rec-1", False)]

        follow = json.loads(await sessions_tool.sessions_send(child, "more"))
        record = await rig.task_runtime.wait(follow["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "succeeded"

        ensures = [c[1] for c in runtime.calls if c[0] == "ensure_session"]
        assert len(ensures) == 2
        assert ensures[1].resume_session_id == "sess-1"
        assert ensures[1].extra["record_id"] == "rec-1"
        assert ensures[1].mode == "oneshot"
        assert len(runtime.closed) == 2  # closed again after the follow-up
        row = await rig.sessions.get_session(child)
        assert row.origin["idle"] is True and not row.origin.get("closed")
        transcript = await rig.sessions.read_recent_transcript(child, 10)
        assert [(r["role"], r["content"]) for r in transcript] == [
            ("user", "once"),
            ("assistant", "first answer"),
            ("user", "more"),
            ("assistant", "second answer"),
        ]
        # The parent sent this follow-up, so it is announced like the spawn.
        completions = await _completions(rig, 2)
        assert completions[-1]["result"]["text"] == "second answer"


async def test_resume_failure_starts_fresh_with_a_notice(tmp_path) -> None:
    runtime = FakeRuntime()
    runtime.resume_supported = False
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="once", acp_agent="claude")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        await _completions(rig, 1)

        follow = json.loads(await sessions_tool.sessions_send(child, "again"))
        record = await rig.task_runtime.wait(follow["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "succeeded"

        notice = "Started a new Claude Code session; the previous context could not be resumed."
        deltas = [
            p["text"] for k, n, p in rig.events if k == child and n == "session.event.text_delta"
        ]
        assert any(notice in d for d in deltas)
        transcript = await rig.sessions.read_recent_transcript(child, 10)
        assert transcript[-1]["role"] == "assistant"
        assert transcript[-1]["content"].startswith(notice + "\n\n")
        assert transcript[-1]["content"].endswith("echo: again")
        # The new harness session is the one a later follow-up resumes.
        row = await rig.sessions.get_session(child)
        assert row.origin["acp_session_id"] == "sess-2"


async def test_turn_into_closed_session_is_logged_persisted_and_emitted(tmp_path) -> None:
    async with acp_rig(tmp_path) as rig:
        out = await _spawn(task="once", acp_agent="claude", mode="persistent")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        await rig.manager.close(child, reason="operator_close")
        row = await rig.sessions.get_session(child)
        assert row.origin["closed"] is True and row.origin["closed_reason"] == "operator_close"

        # The user types into the closed child (desktop sessions.send path).
        entry = await rig.sessions.append_message(child, role="user", content="hello?")
        from agentos.gateway.routing import build_web_route_envelope

        envelope = build_web_route_envelope(
            session_key=child, agent_id="main", conn_id="c1", channel_id="web:c1"
        )
        handle = await rig.task_runtime.enqueue(
            envelope,
            "hello?",
            run_kind="session_turn",
            persisted_user_message_id=entry.message_id,
        )
        record = await rig.task_runtime.wait(handle.task_id, timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "failed"

        errors = [p for k, n, p in rig.events if k == child and n == "session.event.error"]
        assert errors and errors[-1]["code"] == "ACP_SESSION_CLOSED"
        assert "was closed" in errors[-1]["error_message"]
        assert errors[-1]["terminal_message"]
        transcript = await rig.sessions.read_recent_transcript(child, 10)
        assert transcript[-2]["content"] == "hello?"
        assert transcript[-1]["role"] == "system"
        # The row is for the person reading the chat; the event keeps the
        # agent-facing detail.
        assert transcript[-1]["content"] == (
            "Error: This Claude Code session has ended. Start a new chat to continue."
        )
        assert "resume_session_id=sess-1" in errors[-1]["error_message"]
        # Shown by chat.history (the UI resyncs from it right after the error).
        from agentos.chat.history import transcript_entries_to_chat_messages

        history = transcript_entries_to_chat_messages(await rig.sessions.get_transcript(child))
        assert history[-1]["role"] == "system" and history[-1]["text"].startswith("Error:")
        row = await rig.sessions.get_session(child)
        assert row.origin["last_turn"]["error_code"] == "ACP_SESSION_CLOSED"


async def test_harness_start_failure_leaves_an_error_row(tmp_path) -> None:
    from agentos.acp.types import AcpSessionInitError

    runtime = FakeRuntime()

    async def _broken(spec, *, session_key):
        raise AcpSessionInitError(
            "ACP agent 'claude' needs claude on PATH", code="ACP_HARNESS_MISSING"
        )

    runtime.ensure_session = _broken  # type: ignore[method-assign]
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="x", acp_agent="claude")
        child = out["session_key"]
        record = await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "failed"
        transcript = await rig.sessions.read_recent_transcript(child, 10)
        assert [r["role"] for r in transcript] == ["user", "system"]
        assert transcript[-1]["content"] == "Error: ACP agent 'claude' needs claude on PATH"


async def test_user_typed_child_turn_does_not_wake_the_parent(tmp_path) -> None:
    """A turn the user types into the child (desktop sessions.send on the
    child key) runs the harness but never announces to / wakes the parent."""
    async with acp_rig(tmp_path) as rig:
        sent: list[str] = []
        original_send = rig.task_runtime.send

        async def _capture(session_key, message, provenance=None, stream_event_sink=None):
            if session_key == PARENT_KEY:
                sent.append(message)
                from types import SimpleNamespace

                return SimpleNamespace(task_id="wake")
            return await original_send(session_key, message, provenance=provenance)

        rig.task_runtime.send = _capture  # type: ignore[method-assign]
        out = await _spawn(task="once", acp_agent="claude")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert len(await _completions(rig, 1)) == 1
        woken = len(sent)

        from agentos.gateway.rpc import get_dispatcher
        from agentos.gateway.rpc.registry import RpcContext

        ctx = RpcContext(
            conn_id="desk",
            config=rig.config,
            session_manager=rig.sessions,
            task_runtime=rig.task_runtime,
        )
        res = await get_dispatcher().dispatch(
            "1", "sessions.send", {"key": child, "message": "and one more thing"}, ctx
        )
        assert res.ok, res.error
        record = await rig.task_runtime.wait(res.payload["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "succeeded"
        assert record.run_kind == "session_turn"
        await asyncio.sleep(0.05)

        assert len(await _completions(rig, 1)) == 1  # no second completion row
        assert len(sent) == woken
        completions = [p for k, n, p in rig.events if n == "session.event.subagent_completion"]
        assert len(completions) == 1
        transcript = await rig.sessions.read_recent_transcript(child, 10)
        assert transcript[-1]["content"] == "echo: and one more thing"


async def test_operator_cancel_ends_task_as_cancelled_not_failed(tmp_path) -> None:
    """``acp cancel`` (manager.cancel, not task_runtime.cancel) must not read as a
    failure to the parent: live testing showed the parent retrying the task."""
    runtime = FakeRuntime([Script(block=True)])
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="long", acp_agent="claude", mode="persistent")
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0)
        assert await rig.manager.cancel(out["session_key"], reason="operator_cancel")
        record = await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "cancelled"
        assert record.terminal_reason == "cancelled"
        errors = [p for k, n, p in rig.events if n == "session.event.error"]
        assert errors and errors[0]["code"] == "ACP_TURN_CANCELLED"
        assert errors[0]["terminal_reason"] == "cancelled"


def _cost_script(cost: float, text: str = "ok") -> Script:
    return Script(
        events=[
            AcpEvent(kind="usage", usage={"used": 1, "size": 2}, cost_usd=cost),
            AcpEvent(kind="text_delta", text=text),
        ],
        result=AcpTurnResult(stop_reason="end_turn", usage=AcpUsage(cost_usd=cost)),
    )


async def test_resumed_turn_is_charged_only_its_cost_increase(tmp_path) -> None:
    """agent:main:acp:108fc5c7: the resumed turn read the session-cumulative
    1.1732 after 1.0338 and the whole amount was booked to it."""
    runtime = FakeRuntime([_cost_script(1.0338, "first"), _cost_script(1.1732, "second")])
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="once", acp_agent="claude")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        await _completions(rig, 1)
        follow = json.loads(await sessions_tool.sessions_send(child, "more"))
        await rig.task_runtime.wait(follow["task_id"], timeout=5)

        assert len([c for c in runtime.calls if c[0] == "ensure_session"]) == 2  # resumed
        row = await rig.sessions.get_session(child)
        assert abs(row.origin["last_turn"]["cost_usd"] - 0.1394) < 1e-6
        assert abs(row.total_cost_usd - 1.1732) < 1e-6
        assert row.origin["acp_cost_reading"] == {"acp_session_id": "sess-1", "amount": 1.1732}
        transcript = await rig.sessions.read_recent_transcript(child, 10)
        assert abs(transcript[-1]["turn_usage"]["cost_usd"] - 0.1394) < 1e-6


async def test_cancelled_turn_records_its_cost_reading(tmp_path) -> None:
    runtime = FakeRuntime(
        [
            _cost_script(0.5),
            Script(
                events=[AcpEvent(kind="usage", usage={"used": 1, "size": 2}, cost_usd=0.7)],
                block=True,
            ),
            _cost_script(0.9),
        ]
    )
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="one", acp_agent="claude", mode="persistent")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        follow = json.loads(await sessions_tool.sessions_send(child, "two"))
        await asyncio.wait_for(_wait_for_calls(runtime, 2), timeout=5)
        await asyncio.sleep(0.01)
        await rig.task_runtime.cancel(session_key=child, source="test", reason="t")
        await rig.task_runtime.wait(follow["task_id"], timeout=5)
        row = await rig.sessions.get_session(child)
        assert abs(row.origin["last_turn"]["cost_usd"] - 0.2) < 1e-6
        assert row.origin["last_turn"]["stop_reason"] == "cancelled"

        third = json.loads(await sessions_tool.sessions_send(child, "three"))
        await rig.task_runtime.wait(third["task_id"], timeout=5)
        row = await rig.sessions.get_session(child)
        assert abs(row.origin["last_turn"]["cost_usd"] - 0.2) < 1e-6
        assert abs(row.total_cost_usd - 0.9) < 1e-6


async def test_cancelled_turn_keeps_its_work_and_a_note(tmp_path) -> None:
    """agent:main:acp:22cdc031 was stopped after 21 tool calls and its history
    held only the task."""
    runtime = FakeRuntime(
        [
            Script(
                events=[
                    AcpEvent(kind="text_delta", text="Looking around."),
                    AcpEvent(
                        kind="tool_call",
                        tool_call_id="t1",
                        tool_kind="read",
                        title="Read a.py",
                        raw_input={"file_path": "a.py"},
                    ),
                    AcpEvent(kind="tool_call_update", tool_call_id="t1", status="completed"),
                    AcpEvent(
                        kind="tool_call",
                        tool_call_id="t2",
                        tool_kind="execute",
                        title="pytest -q",
                        raw_input={"command": "pytest -q"},
                    ),
                ],
                block=True,
            )
        ]
    )
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="long job", acp_agent="claude")
        child = out["session_key"]
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0.02)
        await rig.task_runtime.cancel(session_key=child, source="test", reason="stop")
        record = await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert str(getattr(record.status, "value", record.status)) == "cancelled"

        transcript = await rig.sessions.read_recent_transcript(child, 10)
        assert [r["role"] for r in transcript] == ["user", "assistant", "system"]
        assistant = transcript[1]
        assert assistant["content"] == "Looking around."
        names = [s.get("name") for s in assistant["tool_calls"] if s["type"] == "tool_use"]
        assert names == ["Read a.py", "Run pytest -q"]
        assert transcript[2]["content"] == "Cancelled after 2 tool calls (0:00)."
        assert not transcript[2]["content"].startswith("Error:")

        completions = await _completions(rig, 1)
        usage = completions[-1]["usage"]
        assert usage["tool_calls"] == 2 and isinstance(usage["duration_ms"], int)
        assert usage["input_tokens"] is None  # nobody reported tokens for this turn
        assert completions[-1]["status"] == "cancelled"


async def test_operator_cancel_also_leaves_the_note(tmp_path) -> None:
    runtime = FakeRuntime(
        [
            Script(
                events=[AcpEvent(kind="tool_call", tool_call_id="t", tool_kind="read", title="R")],
                block=True,
            )
        ]
    )
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="long", acp_agent="claude", mode="persistent")
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0.02)
        assert await rig.manager.cancel(out["session_key"], reason="operator_cancel")
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        transcript = await rig.sessions.read_recent_transcript(out["session_key"], 10)
        assert transcript[-1]["role"] == "system"
        assert transcript[-1]["content"] == "Cancelled after 1 tool call (0:00)."


def _narrated_script(final: str) -> Script:
    return Script(
        events=[
            AcpEvent(kind="text_delta", text="Now the DCA mandate dumps."),
            AcpEvent(kind="tool_call", tool_call_id="a", tool_kind="read", title="Read d.json"),
            AcpEvent(kind="tool_call_update", tool_call_id="a", status="completed"),
            AcpEvent(kind="text_delta", text="The two list dumps were truncated."),
            AcpEvent(kind="tool_call", tool_call_id="b", tool_kind="read", title="Read e.json"),
            AcpEvent(kind="tool_call_update", tool_call_id="b", status="completed"),
            AcpEvent(kind="text_delta", text=final),
        ],
        result=AcpTurnResult(stop_reason="end_turn", tool_calls=2),
    )


async def test_completion_result_is_the_final_answer(tmp_path) -> None:
    final = "**Summary:** three DCA mandates are active and the totals match the ledger."
    async with acp_rig(tmp_path, FakeRuntime([_narrated_script(final)])) as rig:
        out = await _spawn(task="summarize", acp_agent="claude")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        completions = await _completions(rig, 1)
        assert completions[-1]["result"]["text"] == final
        # The child transcript keeps everything, as separate paragraphs.
        transcript = await rig.sessions.read_recent_transcript(child, 10)
        assert transcript[-1]["content"] == (
            "Now the DCA mandate dumps.\n\nThe two list dumps were truncated.\n\n" + final
        )


async def test_short_final_answer_falls_back_to_the_full_text(tmp_path) -> None:
    async with acp_rig(tmp_path, FakeRuntime([_narrated_script("Done.")])) as rig:
        out = await _spawn(task="summarize", acp_agent="claude")
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        completions = await _completions(rig, 1)
        text = completions[-1]["result"]["text"]
        assert text.startswith("Now the DCA mandate dumps.") and text.endswith("Done.")


def _stoppable_script() -> Script:
    return Script(
        events=[
            AcpEvent(kind="tool_call", tool_call_id="t1", tool_kind="read", title="Read a"),
            AcpEvent(kind="tool_call_update", tool_call_id="t1", status="completed"),
            AcpEvent(kind="tool_call", tool_call_id="t2", tool_kind="read", title="Read b"),
        ],
        block=True,
    )


def _capture_parent_wakes(rig) -> list[str]:
    sent: list[str] = []
    original_send = rig.task_runtime.send

    async def _capture(session_key, message, provenance=None, stream_event_sink=None):
        if session_key == PARENT_KEY:
            sent.append(message)
            from types import SimpleNamespace

            return SimpleNamespace(task_id="wake")
        return await original_send(session_key, message, provenance=provenance)

    rig.task_runtime.send = _capture  # type: ignore[method-assign]
    return sent


STOP_TEXT = (
    "The user stopped this Claude Code run after 2 tool calls (0:00). Do not restart it; "
    "tell the user what was done so far and ask how to continue."
)


async def test_user_stop_tells_the_parent_not_to_restart(tmp_path) -> None:
    """o01ewhl3: after the user pressed Stop the parent read "The task was
    cancelled before it finished." and spawned the same job again."""
    from agentos.gateway import rpc_acp
    from agentos.gateway.rpc import get_dispatcher
    from agentos.gateway.rpc.registry import RpcContext

    runtime = FakeRuntime([_stoppable_script()])
    async with acp_rig(tmp_path, runtime) as rig:
        sent = _capture_parent_wakes(rig)
        out = await _spawn(task="long job", acp_agent="claude")
        child = out["session_key"]
        await sessions_tool.sessions_yield()
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0.02)
        # The card / bar Stop: acp.cancel over RPC.
        rpc_acp.set_acp_manager(rig.manager)
        try:
            ctx = RpcContext(
                conn_id="desk",
                config=rig.config,
                session_manager=rig.sessions,
                task_runtime=rig.task_runtime,
            )
            res = await get_dispatcher().dispatch("1", "acp.cancel", {"session_key": child}, ctx)
        finally:
            rpc_acp.set_acp_manager(None)
        assert res.ok, res.error
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert await wait_until(lambda: bool(sent)), "parent was not woken"

        # What the parent model reads on wake.
        wake = sent[-1]
        assert "stopped_by=user" in wake
        assert STOP_TEXT in wake
        assert "do not start it again" in wake
        # The persisted completion and the live event say the same.
        completions = await _completions(rig, 1)
        assert completions[-1]["stopped_by"] == "user"
        assert completions[-1]["terminal_message"].startswith(STOP_TEXT)
        # ... and how to continue that run instead of spawning a new one.
        assert f'sessions_send(session_key="{child}"' in completions[-1]["terminal_message"]
        assert completions[-1]["cancel_source"] == "acp_rpc_cancel"
        live = [p for _, n, p in rig.events if n == "session.event.subagent_completion"]
        assert live[-1]["terminal_message"] == completions[-1]["terminal_message"]
        assert f'continue_with=sessions_send(session_key="{child}"' in wake


async def test_stop_before_the_parent_yields_still_reaches_the_wake(tmp_path) -> None:
    """The group wake built from the session rows (parent yields after the
    child already stopped) carries the stop too."""
    runtime = FakeRuntime([_stoppable_script()])
    async with acp_rig(tmp_path, runtime) as rig:
        sent = _capture_parent_wakes(rig)
        out = await _spawn(task="long job", acp_agent="claude")
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0.02)
        rig.manager.note_stop(out["session_key"], "user")
        await rig.task_runtime.cancel(
            session_key=out["session_key"], source="sessions_abort", reason="stop"
        )
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        await _completions(rig, 1)
        await sessions_tool.sessions_yield()
        assert await wait_until(lambda: bool(sent)), "parent was not woken"
        assert STOP_TEXT in sent[-1]


def test_a_parent_steer_is_not_a_user_stop() -> None:
    from agentos.gateway.subagent_announce import _stop_fields

    row = {"origin": {"kind": "acp", "agent_id": "claude", "last_turn": {}}}
    assert _stop_fields(row, status="cancelled", cancel_source="acp_steer") == {}
    assert _stop_fields(row, status="succeeded", cancel_source=None) == {}
    fields = _stop_fields(row, status="cancelled", cancel_source="sessions_abort")
    assert fields["stopped_by"] == "user"
    assert fields["terminal_message"].startswith("The user stopped this Claude Code run.")
    assert fields["user_message"] == "You stopped this Claude Code run."
    subagent = {"origin": {"kind": "subagent"}}
    assert _stop_fields(subagent, status="cancelled", cancel_source=None) == {}


async def test_report_followed_by_a_tool_and_a_remark_keeps_the_report(tmp_path) -> None:
    """agent:main:acp:578b561b: the full report, then ToolSearch, then a short
    meta remark; result.text was only the remark."""
    report = "## Report\n\n" + "Finding: the ledger and the mandates agree. " * 30
    remark = (
        "I will not call ExitPlanMode; the full report is in the previous message, "
        "so there is nothing left to approve."
    )
    script = Script(
        events=[
            AcpEvent(kind="tool_call", tool_call_id="r", tool_kind="read", title="Read x"),
            AcpEvent(kind="tool_call_update", tool_call_id="r", status="completed"),
            AcpEvent(kind="text_delta", text=report),
            AcpEvent(kind="tool_call", tool_call_id="s", tool_kind="other", title="ToolSearch"),
            AcpEvent(kind="tool_call_update", tool_call_id="s", status="completed"),
            AcpEvent(kind="text_delta", text=remark),
        ],
        result=AcpTurnResult(stop_reason="end_turn"),
    )
    async with acp_rig(tmp_path, FakeRuntime([script])) as rig:
        out = await _spawn(task="plan", acp_agent="claude", harness_mode="plan")
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        completions = await _completions(rig, 1)
        assert completions[-1]["result"]["text"] == report.strip() + "\n\n" + remark


async def test_sessions_history_clips_tool_bodies_of_acp_children(tmp_path) -> None:
    big = "x" * 4000
    script = Script(
        events=[
            AcpEvent(
                kind="tool_call",
                tool_call_id="w",
                tool_kind="edit",
                title="Write a.py",
                raw_input={"file_path": "a.py", "content": big},
            ),
            AcpEvent(kind="tool_call_update", tool_call_id="w", status="completed", raw_output=big),
            AcpEvent(kind="text_delta", text="The report the parent needs."),
        ],
        result=AcpTurnResult(stop_reason="end_turn"),
    )
    async with acp_rig(tmp_path, FakeRuntime([script])) as rig:
        out = await _spawn(task="write", acp_agent="claude")
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        history = json.loads(await sessions_tool.sessions_history(out["session_key"]))
        assistant = [m for m in history["messages"] if m["role"] == "assistant"][-1]
        assert assistant["content"] == "The report the parent needs."
        use = next(s for s in assistant["tool_calls"] if s["type"] == "tool_use")
        result = next(s for s in assistant["tool_calls"] if s["type"] == "tool_result")
        assert use["input"]["content"] == "x" * 500 + "[…3500 chars]"
        assert result["result"].startswith("x" * 500 + "[…")
        assert len(json.dumps(history)) < 3000


async def test_user_stop_has_a_user_facing_message_and_last_turn_summary(tmp_path) -> None:
    runtime = FakeRuntime([_stoppable_script()])
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="long job", acp_agent="claude")
        child = out["session_key"]
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0.02)
        rig.manager.note_stop(child, "user")  # what acp.cancel records first
        await rig.task_runtime.cancel(session_key=child, source="acp_rpc_cancel", reason="x")
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        completions = await _completions(rig, 1)
        assert completions[-1]["user_message"] == (
            "You stopped this Claude Code run after 2 tool calls (0:00)."
        )
        assert completions[-1]["terminal_message"].startswith(STOP_TEXT)  # for the parent
        last_turn = (await rig.sessions.get_session(child)).origin["last_turn"]
        assert last_turn["stopped_by"] == "user"
        assert last_turn["summary"] == "Stopped by you after 2 tool calls (0:00)."
        assert "error" not in last_turn  # no "cancelled by an operator..." text
        assert last_turn["error_code"] == "ACP_TURN_CANCELLED"
        status = await rig.manager.status(child)
        assert status["last_turn"]["summary"] == "Stopped by you after 2 tool calls (0:00)."


async def test_parent_kill_is_recorded_but_does_not_wake_the_parent(tmp_path) -> None:
    """The parent killed its child, answered in that turn, then answered again
    when the cancelled completion woke it."""
    from agentos.tools.builtin import agents as agents_tool

    runtime = FakeRuntime([_stoppable_script()])
    async with acp_rig(tmp_path, runtime) as rig:
        sent = _capture_parent_wakes(rig)
        out = await _spawn(task="long job", acp_agent="claude")
        child = out["session_key"]
        await sessions_tool.sessions_yield()
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0.02)
        killed = json.loads(await agents_tool.subagents(action="kill", session_key=child))
        assert killed["status"] == "killed"
        await rig.task_runtime.wait(out["task_id"], timeout=5)

        completions = await _completions(rig, 1)
        assert completions[-1]["stopped_by"] == "agent"
        assert completions[-1]["user_message"] == (
            "Stopped by the agent after 2 tool calls (0:00)."
        )
        assert completions[-1]["terminal_message"].startswith("You stopped this Claude Code run")
        # The parent transcript row is written before the live event goes out.
        live: list[dict] = []
        for _ in range(100):
            live = [p for _, n, p in rig.events if n == "session.event.subagent_completion"]
            if live:
                break
            await asyncio.sleep(0.02)
        assert live and live[-1]["stopped_by"] == "agent"
        await asyncio.sleep(0.05)
        assert sent == []  # recorded, not woken
        last_turn = (await rig.sessions.get_session(child)).origin["last_turn"]
        assert last_turn["stopped_by"] == "agent"
        assert last_turn["summary"] == "Stopped by the agent after 2 tool calls (0:00)."


async def test_a_cut_answer_tells_the_parent_where_the_rest_is(tmp_path) -> None:
    """agent:main:acp:ef3f879b: a 17,116-character report was cut at 12,000
    and the parent re-read every file itself instead of reading the rest."""
    head = "".join(f"Section {i}: " + "finding " * 30 + "\n" for i in range(40))
    report = (head * 2)[:17_116]
    script = Script(
        events=[
            AcpEvent(kind="tool_call", tool_call_id="r", tool_kind="read", title="Read x"),
            AcpEvent(kind="tool_call_update", tool_call_id="r", status="completed"),
            AcpEvent(kind="text_delta", text=report),
        ],
        result=AcpTurnResult(stop_reason="end_turn"),
    )
    async with acp_rig(tmp_path, FakeRuntime([script])) as rig:
        sent = _capture_parent_wakes(rig)
        out = await _spawn(task="audit", acp_agent="claude")
        child = out["session_key"]
        await sessions_tool.sessions_yield()
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        assert await wait_until(lambda: bool(sent)), "parent was not woken"

        completions = await _completions(rig, 1)
        result = completions[-1]["result"]
        assert result["truncated"] is True and len(result["text"]) == 12_000
        assert result["full_chars"] == len(report.strip())
        wake = sent[-1]
        assert "result_truncated=true" in wake
        assert "never copy files out of a harness's own directories (~/.claude" in wake
        assert (
            f"The answer was cut at 12,000 of {len(report.strip()):,} characters; read the "
            f'rest with sessions_history(session_key="{child}", answer_offset=12000) - do '
            "not redo the work."
        ) in wake

        # ... and that call starts exactly where the completion stopped.
        page = json.loads(await sessions_tool.sessions_history(child, answer_offset=12_000))
        assert page["answer_total_chars"] == len(report.strip())
        assert result["text"] + page["answer"] == report.strip()[: 12_000 + len(page["answer"])]


async def test_every_character_of_a_long_answer_reaches_the_model_once(tmp_path) -> None:
    """agent:main:acp:fd260ca2 (full_chars 24,064): the 8,389- and
    4,277-character pages were stored cut to 2,000 characters with "…", and
    the answer itself had been cut at the old 24,000-character turn cap.

    Pages are checked the way the model receives them: in the turn (the
    provider cap, smallest at a 4k-token window) and in later turns (the
    persisted transcript segment)."""
    from agentos.context_budget import ContextBudgetGovernor
    from agentos.engine.runtime import _persisted_tool_result_segment
    from agentos.engine.types import ToolResultEvent
    from agentos.result_budget import register_persisted_result_budget

    register_persisted_result_budget(
        "sessions_history", sessions_tool._sessions_history_persist_budget
    )
    # Markdown with quotes, backslashes, newlines and non-ASCII: JSON escaping
    # makes the encoded page longer than its text.
    line = '| "item" | ok — "quoted" \\ path\\to\\file |\n'
    report = (line * 2_000)[:24_064]
    script = Script(
        events=[
            AcpEvent(kind="tool_call", tool_call_id="r", tool_kind="read", title="Read x"),
            AcpEvent(kind="tool_call_update", tool_call_id="r", status="completed"),
            AcpEvent(kind="text_delta", text=report),
        ],
        result=AcpTurnResult(stop_reason="end_turn"),
    )
    smallest_turn_cap = ContextBudgetGovernor.from_values(
        context_window_tokens=4_000,
        max_output_tokens=1_000,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).tool_result_provider_chars_for(None)
    async with acp_rig(tmp_path, FakeRuntime([script])) as rig:
        out = await _spawn(task="report", acp_agent="claude")
        child = out["session_key"]
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        completions = await _completions(rig, 1)
        answer = report.strip()
        assert completions[-1]["result"]["full_chars"] == len(answer)  # not cut at 24,000
        received = completions[-1]["result"]["text"]
        offset: int | None = len(received)
        calls = 0
        while offset is not None:
            raw = await sessions_tool.sessions_history(child, answer_offset=offset)
            calls += 1
            assert len(raw) <= smallest_turn_cap  # arrives whole in the turn
            segment = _persisted_tool_result_segment(
                ToolResultEvent(tool_use_id=f"p{calls}", tool_name="sessions_history", result=raw)
            )
            assert "result_truncated" not in segment  # ... and in later turns
            page = json.loads(segment["result"])
            assert page["answer_offset"] == len(received)
            received += page["answer"]
            offset = page["next_offset"]
        assert received == answer  # every character exactly once
        assert calls <= 5


async def test_parent_can_continue_a_stopped_child_instead_of_respawning(tmp_path) -> None:
    """After the user stopped Claude Code and said "go on", the parent tried a
    spawn with a resume id, got resume_forbidden and started over."""
    from agentos.tools.types import ToolError

    runtime = FakeRuntime([_stoppable_script()])
    async with acp_rig(tmp_path, runtime) as rig:
        out = await _spawn(task="long job", acp_agent="claude")
        child = out["session_key"]
        await asyncio.wait_for(runtime.turn_started.wait(), timeout=5)
        await asyncio.sleep(0.02)
        rig.manager.note_stop(child, "user")
        await rig.task_runtime.cancel(session_key=child, source="acp_rpc_cancel", reason="x")
        await rig.task_runtime.wait(out["task_id"], timeout=5)
        await _completions(rig, 1)

        # A session key (or its hex) is not a harness session id: the error
        # says how to continue that child.
        for wrong in (child, child.rsplit(":", 1)[-1]):
            with pytest.raises(ToolError) as info:
                await _spawn(task="go on", acp_agent="claude", resume_session_id=wrong)
            message = str(info.value)
            assert message.startswith("resume_forbidden:")
            assert f'sessions_send(session_key="{child}", message=...)' in message
        with pytest.raises(ToolError, match=r"sessions_send\(session_key=<the child key>"):
            await _spawn(task="go on", acp_agent="claude", resume_session_id="nope")

        # The child's real harness session id (or its record id) is accepted.
        row = await rig.sessions.get_session(child)
        for real in (row.origin["acp_session_id"], row.origin["record_id"]):
            again = await _spawn(task="go on", acp_agent="claude", resume_session_id=real)
            new_row = await rig.sessions.get_session(again["session_key"])
            assert new_row.origin["resume_session_id"] == row.origin["acp_session_id"]

        # The right path: sessions_send resumes the stopped child itself.
        follow = json.loads(await sessions_tool.sessions_send(child, "go on"))
        assert follow["session_key"] == child
