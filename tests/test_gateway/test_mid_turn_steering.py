"""Server-side pending inputs and mid-turn steering through the gateway RPCs.

Real ``TaskRuntime`` + dispatch + ``SessionManager`` (``gateway_turn_rig``);
the model is replaced by a runner that checks the steering queue the way the
agent loop does at a checkpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from gateway_turn_rig import ScriptedRunner, turn_rig, wait_for

from agentos.engine.steering import steering_for, take_steering
from agentos.engine.types import DoneEvent, TextDeltaEvent
from agentos.gateway.protocol import CAPABILITIES
from agentos.gateway.rpc import get_dispatcher
from agentos.provider import Message
from agentos.session.models import AgentTaskStatus


class SteerableRunner:
    """A turn that attaches to its steering queue, then takes one message on release."""

    def __init__(self, *, attach: bool = True, take: bool = True) -> None:
        self.attach = attach
        self.take = take
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.steered: list[str] = []
        self.steered_ids: list[str] = []

    async def run(self, message: str, session_key: str, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls.append((message, kwargs))
        source = steering_for(session_key) if self.attach else None
        yield TextDeltaEvent(text="working")
        self.started.set()
        await self.release.wait()
        if self.take:
            messages: list[Message] = []
            event = await take_steering(source, messages, self.steered_ids)
            if event is not None:
                self.steered.append(event.text)
                yield event
        yield DoneEvent(text="done")


async def _send(rig: Any, message: str, **extra: Any) -> dict[str, Any]:
    params = {"sessionKey": rig.key, "message": message, **extra}
    res = await get_dispatcher().dispatch("s", "chat.send", params, rig.ctx(rig.live.conn_id))
    assert res.ok, res.error
    return dict(res.payload)


async def _call(rig: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    res = await get_dispatcher().dispatch("c", method, params, rig.ctx(rig.live.conn_id))
    assert res.ok, res.error
    return dict(res.payload)


async def _user_rows(rig: Any) -> list[str]:
    return [e.content for e in await rig.sessions.get_transcript(rig.key) if e.role == "user"]


def test_capability_is_advertised() -> None:
    assert "mid-turn-steering" in CAPABILITIES


async def test_steer_send_joins_the_running_turn_and_lands_where_it_is_taken(
    tmp_path: Path,
) -> None:
    runner = SteerableRunner()
    async with turn_rig(tmp_path, runner) as rig:
        first = await _send(rig, "first")
        await asyncio.wait_for(runner.started.wait(), timeout=3)

        second = await _send(rig, "change of plan", queueMode="steer")
        history = await _call(rig, "chat.history", {"sessionKey": rig.key})
        rows_before_take = await _user_rows(rig)

        runner.release.set()
        await rig.runtime.wait(first["task_id"], timeout=3)
        steered = await rig.runtime.status(second["task_id"])
        rows_after = await _user_rows(rig)
        user_ids = [
            e.message_id for e in await rig.sessions.get_transcript(rig.key) if e.role == "user"
        ]
        history_after = await _call(rig, "chat.history", {"sessionKey": rig.key})

    assert second["queue_mode"] == "steer"
    assert second["task_id"] != first["task_id"]
    # Not in the transcript until the turn takes it; listed as pending meanwhile.
    assert rows_before_take == ["first"]
    [pending] = history["pending_inputs"]
    assert pending["task_id"] == second["task_id"]
    assert pending["text"] == "change of plan"
    assert pending["steer"] is True
    assert "message_id" not in pending
    # Taken by the running turn: one turn ran, the message is in it.
    assert runner.steered == ["change of plan"]
    assert len(runner.calls) == 1
    assert rows_after == ["first", "change of plan"]
    assert steered.status == AgentTaskStatus.SUCCEEDED
    assert steered.terminal_reason == "steered"
    assert history_after["pending_inputs"] == []
    # The row id the turn records (steered_entry_ids) is the transcript row.
    assert runner.steered_ids == [user_ids[1]]


async def test_steer_message_no_checkpoint_took_runs_as_the_next_turn(tmp_path: Path) -> None:
    runner = SteerableRunner(take=False)
    async with turn_rig(tmp_path, runner) as rig:
        first = await _send(rig, "first")
        await asyncio.wait_for(runner.started.wait(), timeout=3)
        second = await _send(rig, "afterthought", queueMode="steer")
        assert await _user_rows(rig) == ["first"]

        runner.release.set()
        await rig.runtime.wait(first["task_id"], timeout=3)
        await wait_for(lambda: len(runner.calls) == 2)
        record = await rig.runtime.wait(second["task_id"], timeout=3)
        rows = await _user_rows(rig)

    message, kwargs = runner.calls[1]
    assert message == "afterthought"
    assert kwargs.get("persisted_user_message_id")
    assert rows == ["first", "afterthought"]
    assert record.status == AgentTaskStatus.SUCCEEDED
    assert record.terminal_reason == "completed"


async def test_withdrawn_steer_message_leaves_no_trace(tmp_path: Path) -> None:
    runner = SteerableRunner()
    async with turn_rig(tmp_path, runner) as rig:
        first = await _send(rig, "first")
        await asyncio.wait_for(runner.started.wait(), timeout=3)
        second = await _send(rig, "never mind", queueMode="steer")

        answer = await _call(
            rig, "chat.abort", {"sessionKey": rig.key, "taskId": second["task_id"]}
        )
        history = await _call(rig, "chat.history", {"sessionKey": rig.key})
        runner.release.set()
        await rig.runtime.wait(first["task_id"], timeout=3)
        withdrawn = await rig.runtime.status(second["task_id"])
        first_record = await rig.runtime.status(first["task_id"])
        rows = await _user_rows(rig)

    assert answer["withdrawn"] is True
    assert answer["aborted"] == []
    assert history["pending_inputs"] == []
    assert runner.steered == []
    assert rows == ["first"]
    assert withdrawn.status == AgentTaskStatus.CANCELLED
    assert withdrawn.terminal_reason == "withdrawn"
    # Withdrawing one message does not stop the running turn.
    assert first_record.status == AgentTaskStatus.SUCCEEDED


async def test_withdrawn_follow_up_removes_its_user_row(tmp_path: Path) -> None:
    runner = SteerableRunner()
    async with turn_rig(tmp_path, runner) as rig:
        first = await _send(rig, "first")
        await asyncio.wait_for(runner.started.wait(), timeout=3)
        queued = await _send(rig, "later", queueMode="followup")
        history = await _call(rig, "chat.history", {"sessionKey": rig.key})
        rows_queued = await _user_rows(rig)

        answer = await _call(
            rig, "chat.abort", {"sessionKey": rig.key, "taskId": queued["task_id"]}
        )
        rows_withdrawn = await _user_rows(rig)
        runner.release.set()
        await rig.runtime.wait(first["task_id"], timeout=3)
        await asyncio.sleep(0.05)

    [pending] = history["pending_inputs"]
    assert pending["steer"] is False
    assert pending["message_id"]  # already in the transcript: clients dedupe on it
    assert rows_queued == ["first", "later"]
    assert answer["withdrawn"] is True
    assert answer["removed_message_id"] == pending["message_id"]
    assert rows_withdrawn == ["first"]
    assert len(runner.calls) == 1


async def test_abort_by_task_id_stops_only_the_running_turn(tmp_path: Path) -> None:
    runner = SteerableRunner()
    async with turn_rig(tmp_path, runner) as rig:
        first = await _send(rig, "first")
        await asyncio.wait_for(runner.started.wait(), timeout=3)
        queued = await _send(rig, "next", queueMode="followup")

        answer = await _call(rig, "chat.abort", {"sessionKey": rig.key, "taskId": first["task_id"]})
        stopped = await rig.runtime.wait(first["task_id"], timeout=3)
        await wait_for(lambda: len(runner.calls) == 2)
        runner.release.set()
        follow_up = await rig.runtime.wait(queued["task_id"], timeout=3)

    assert answer["aborted"] == [first["task_id"]]
    assert stopped.status == AgentTaskStatus.CANCELLED
    assert follow_up.status == AgentTaskStatus.SUCCEEDED


async def test_steer_into_a_turn_that_checks_no_queue_still_interrupts(tmp_path: Path) -> None:
    """A turn that never attached (a coding-harness turn) keeps steer = interrupt."""
    runner = ScriptedRunner(["working"])
    async with turn_rig(tmp_path, runner) as rig:
        first = await _send(rig, "first")
        await asyncio.wait_for(runner.streamed.wait(), timeout=3)
        second = await _send(rig, "replace it", queueMode="steer")
        runner.release.set()
        await rig.runtime.wait(second["task_id"], timeout=3)
        stopped = await rig.runtime.status(first["task_id"])
        rows = await _user_rows(rig)

    assert second["queue_mode"] == "interrupt"
    assert stopped.status == AgentTaskStatus.CANCELLED
    assert rows == ["first", "replace it"]


async def test_send_without_an_explicit_steer_keeps_the_old_default(tmp_path: Path) -> None:
    """The session default (``steer``) still interrupts; only an explicit ask steers."""
    runner = SteerableRunner()
    async with turn_rig(tmp_path, runner) as rig:
        first = await _send(rig, "first")
        await asyncio.wait_for(runner.started.wait(), timeout=3)
        second = await _send(rig, "plain message")
        await wait_for(lambda: len(runner.calls) == 2)
        runner.release.set()
        await rig.runtime.wait(second["task_id"], timeout=3)
        stopped = await rig.runtime.status(first["task_id"])

    assert second["queue_mode"] == "interrupt"
    assert stopped.status == AgentTaskStatus.CANCELLED
    assert runner.steered == []
    assert runner.calls[1][0] == "plain message"
