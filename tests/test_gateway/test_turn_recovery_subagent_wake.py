"""A crash under a parent that had children running gives the parent one turn.

After the crash, restart recovery resumes the parent's interrupted turn and the
restart announce reports the abandoned children to the same parent. Each used
to enqueue a turn of its own (the wake is ``internal_system``, which the
recovery does not treat as the user writing first), so the parent ran twice:
a "continue" turn and an "your children were abandoned" turn, in either order.
While a recovery of the parent is pending or running, the children are
recorded on the parent and the wake is held back: the resumed turn carries on
with them. When the recovery ends without a resumed turn (every attempt failed
and it was tombstoned), the held wake is sent after all (#3867): before, it
was dropped for good and nothing ever told the parent about those children.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.subagent_announce import (
    _tracker,
    announce_restart_abandoned_children,
    announce_subagent_completion,
    reconcile_awaiting_wakes,
)
from agentos.gateway.subagent_wakes import SubagentWakeOutbox, group_key, set_wake_outbox
from agentos.gateway.turn_recovery import RECOVERY_KEY, TurnRecovery
from agentos.session.models import AgentTaskRecord, AgentTaskStatus
from tests.test_gateway.test_turn_recovery import KEY, _Env, _interrupted_session

CHILD = "agent:main:subagent:abandoned-child"


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    environment = _Env(tmp_path)
    await environment.open()
    yield environment
    await environment.close()


async def _abandoned_child(env: _Env, parent_task_id: str) -> None:
    assert env.manager is not None and env.storage is not None
    await env.manager.create(
        CHILD,
        spawned_by=KEY,
        origin={"kind": "subagent", "parent_task_id": parent_task_id},
    )
    await env.manager.append_message(CHILD, "user", "Look into it.")
    now = int(time.time() * 1000)
    await env.storage.create_agent_task(
        AgentTaskRecord(
            session_key=CHILD,
            source_kind="system",
            run_kind="subagent",
            status=AgentTaskStatus.ABANDONED,
            terminal_reason="process_restart",
            created_at=now - 500,
            started_at=now - 500,
            details={
                "source_name": "test",
                "metadata": {"parent_session_key": KEY, "parent_task_id": parent_task_id},
            },
        )
    )


async def _parent_turns(env: _Env) -> list[dict[str, Any]]:
    assert env.runner is not None
    for _ in range(100):
        await asyncio.sleep(0.02)
    return [turn for turn in env.runner.turns if turn["session_key"] == KEY]


async def test_a_recovered_parent_is_not_also_woken_for_its_abandoned_children(
    env: _Env,
) -> None:
    assert env.manager is not None and env.storage is not None
    parent = await _interrupted_session(env, tail="round_complete")
    await _abandoned_child(env, parent.task_id)
    _tracker.evict(KEY)
    recovery = env.recovery()

    async def listener(event: Any) -> None:
        await announce_subagent_completion(
            event, session_manager=env.manager, task_runtime=env.runtime
        )

    # Boot order: the recovery scan first, then the restart announce.
    await recovery.scan()
    announced = await announce_restart_abandoned_children(
        storage=env.storage,
        listener=listener,
        max_age_ms=60_000,
        turn_recovery=recovery,
    )
    await recovery.wait_idle(timeout=5)
    turns = await _parent_turns(env)

    assert announced == 1
    assert len(turns) == 1, [turn["message"][:60] for turn in turns]
    assert turns[0]["provenance"].get("source_tool") != "subagent_completion"
    # The child is still on the parent's record for the resumed turn to read.
    transcript = await env.manager.read_transcript(KEY, limit=50)
    assert any(CHILD in str(row.get("content")) for row in transcript)
    _tracker.evict(KEY)


async def test_without_a_recovery_the_parent_is_still_woken(env: _Env) -> None:
    assert env.manager is not None and env.storage is not None
    parent = await _interrupted_session(env, tail="finished")
    await _abandoned_child(env, parent.task_id)
    _tracker.evict(KEY)
    recovery = env.recovery()

    async def listener(event: Any) -> None:
        await announce_subagent_completion(
            event, session_manager=env.manager, task_runtime=env.runtime
        )

    await recovery.scan()  # the parent's turn had finished: nothing to resume
    assert not recovery.is_recovering(KEY)
    await announce_restart_abandoned_children(
        storage=env.storage, listener=listener, max_age_ms=60_000, turn_recovery=recovery
    )
    turns = await _parent_turns(env)

    assert len(turns) == 1
    assert turns[0]["provenance"].get("source_tool") == "subagent_completion"
    _tracker.evict(KEY)


# ---------------------------------------------------------------------------
# A recovery that ends without a resumed turn
# ---------------------------------------------------------------------------


async def _drain(env: _Env) -> None:
    """Wait for every task the runtime holds, including ones they enqueue."""
    assert env.runtime is not None
    while env.runtime._tasks:
        for task_id in list(env.runtime._tasks):
            await env.runtime.wait(task_id, timeout=10)


async def _boot_announce(env: _Env, recovery: TurnRecovery) -> int:
    """What ``boot._recover_subagent_wakes`` runs after the recovery scan."""
    assert env.manager is not None

    async def listener(event: Any) -> None:
        await announce_subagent_completion(
            event, session_manager=env.manager, task_runtime=env.runtime
        )

    announced = await announce_restart_abandoned_children(
        storage=env.storage, listener=listener, max_age_ms=60_000, turn_recovery=recovery
    )
    await reconcile_awaiting_wakes(
        session_manager=env.manager, task_runtime=env.runtime, turn_recovery=recovery
    )
    return announced


async def _recovery_state(env: _Env, task_id: str) -> str:
    assert env.storage is not None
    record = await env.storage.get_agent_task(task_id)
    assert record is not None
    return str(record.details[RECOVERY_KEY]["state"])


@pytest.fixture
async def failing_env(tmp_path: Path) -> AsyncIterator[_Env]:
    """Every resume attempt fails; the wake outbox is installed, as at boot."""
    environment = _Env(tmp_path)
    await environment.open(fail=3)
    assert environment.storage is not None
    set_wake_outbox(SubagentWakeOutbox(environment.storage))
    _tracker.evict(KEY)
    try:
        yield environment
    finally:
        set_wake_outbox(None)
        _tracker.evict(KEY)
        await environment.close()


async def test_a_parent_whose_recovery_is_tombstoned_is_woken_for_its_children(
    failing_env: _Env,
) -> None:
    env = failing_env
    assert env.storage is not None and env.runner is not None
    parent = await _interrupted_session(env, tail="round_complete")
    await _abandoned_child(env, parent.task_id)
    env.runner.release = asyncio.Event()  # hold the first resume until announced
    recovery = env.recovery()

    await recovery.scan()
    assert recovery.is_recovering(KEY)
    assert await _boot_announce(env, recovery) == 1
    # Held back while the parent's own turn is being resumed.
    row = await env.storage.get_subagent_wake(group_key(KEY, parent.task_id))
    assert row is not None and row["state"] == "dropped"

    env.runner.release.set()
    await recovery.wait_idle(timeout=10)
    await _drain(env)

    assert await _recovery_state(env, parent.task_id) == "tombstoned"
    turns = [turn for turn in env.runner.turns if turn["session_key"] == KEY]
    resumes = [t for t in turns if t["provenance"].get("source_tool") == "restart_recovery"]
    wakes = [t for t in turns if t["provenance"].get("source_tool") == "subagent_completion"]
    assert len(resumes) == 3
    assert len(wakes) == 1, [turn["message"][:60] for turn in turns]
    assert CHILD in wakes[0]["message"]
    assert wakes[0]["provenance"].get("parent_task_id") == parent.task_id
    row = await env.storage.get_subagent_wake(group_key(KEY, parent.task_id))
    assert row is not None and row["state"] == "delivered"

    # The next boot neither resumes the run again nor wakes the parent twice.
    _tracker.evict(KEY)
    again = env.recovery()
    await again.scan()
    await _boot_announce(env, again)
    await again.wait_idle(timeout=5)
    await _drain(env)
    assert len([turn for turn in env.runner.turns if turn["session_key"] == KEY]) == 4


async def test_a_wake_held_by_one_boot_is_sent_when_a_later_boot_settles_it(
    failing_env: _Env,
) -> None:
    # The wake is held on the first boot; the gateway stops before the resume
    # runs. The next boot does not announce the child again (it already was),
    # so only the durable outbox row knows the parent still owes this wake.
    env = failing_env
    assert env.storage is not None and env.runner is not None
    parent = await _interrupted_session(env, tail="round_complete")
    await _abandoned_child(env, parent.task_id)
    first = env.recovery(backoff_s=(3600.0,))
    await first.scan()
    assert await _boot_announce(env, first) == 1
    await first.stop()  # still in its backoff: nothing ran
    assert env.runner.turns == []

    _tracker.evict(KEY)  # a new process
    second = env.recovery()
    await second.scan()
    assert await _boot_announce(env, second) == 0
    await second.wait_idle(timeout=10)
    await _drain(env)

    # No resumed turn completed on either boot. (The second boot closes the
    # run rather than retry it: the child's completion row the first boot
    # wrote now ends the transcript, so the scan reads the turn as finished.)
    assert await _recovery_state(env, parent.task_id) in {"closed", "tombstoned"}
    wakes = [
        turn
        for turn in env.runner.turns
        if turn["session_key"] == KEY
        and turn["provenance"].get("source_tool") == "subagent_completion"
    ]
    assert len(wakes) == 1
    assert CHILD in wakes[0]["message"]
    row = await env.storage.get_subagent_wake(group_key(KEY, parent.task_id))
    assert row is not None and row["state"] == "delivered"


async def test_a_resumed_parent_still_gets_no_second_turn_with_the_outbox(
    tmp_path: Path,
) -> None:
    # The resume succeeds: the held wake stays dropped, as before, and is
    # final before the recovery's own "resumed" state is written (after that
    # no boot settles the run again, so a crash in between must not leave
    # the hold for a later recovery of the parent to reopen).
    env = _Env(tmp_path)
    await env.open()
    assert env.storage is not None and env.runner is not None
    storage = env.storage
    set_wake_outbox(SubagentWakeOutbox(storage))
    _tracker.evict(KEY)
    try:
        parent = await _interrupted_session(env, tail="round_complete")
        await _abandoned_child(env, parent.task_id)
        reasons_when_resumed: list[Any] = []
        update_agent_task = storage.update_agent_task

        async def update_and_look(task_id: str, **fields: Any) -> Any:
            details = fields.get("details") or {}
            if (details.get(RECOVERY_KEY) or {}).get("state") == "resumed":
                row = await storage.get_subagent_wake(group_key(KEY, parent.task_id))
                reasons_when_resumed.append(row and row["reason"])
            return await update_agent_task(task_id, **fields)

        storage.update_agent_task = update_and_look  # type: ignore[method-assign]
        env.runner.release = asyncio.Event()
        recovery = env.recovery()
        await recovery.scan()
        await _boot_announce(env, recovery)
        env.runner.release.set()
        await recovery.wait_idle(timeout=10)
        await _drain(env)

        assert await _recovery_state(env, parent.task_id) == "resumed"
        turns = [turn for turn in env.runner.turns if turn["session_key"] == KEY]
        assert len(turns) == 1
        assert turns[0]["provenance"].get("source_tool") == "restart_recovery"
        row = await env.storage.get_subagent_wake(group_key(KEY, parent.task_id))
        assert row is not None and (row["state"], row["reason"]) == ("dropped", "parent_resumed")
        assert reasons_when_resumed == ["parent_resumed"]
    finally:
        set_wake_outbox(None)
        _tracker.evict(KEY)
        await env.close()


async def test_a_user_message_that_cancels_the_recovery_gets_the_wake_after_it(
    failing_env: _Env,
) -> None:
    # The user writes before the resume starts: the resume is cancelled and
    # the user's own turn runs. The children's wake follows it, as it would
    # have without the restart.
    env = failing_env
    assert env.storage is not None and env.runner is not None and env.runtime is not None
    env.runner.fail = 0
    parent = await _interrupted_session(env, tail="round_complete")
    await _abandoned_child(env, parent.task_id)
    recovery = env.recovery(backoff_s=(3600.0,))
    await recovery.scan()
    await _boot_announce(env, recovery)

    user = RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="test", agent_id="main", session_key=KEY
    )
    await env.runtime.enqueue(user, "never mind, do this instead")
    await recovery.wait_idle(timeout=10)
    await _drain(env)

    assert await _recovery_state(env, parent.task_id) == "cancelled"
    turns = [turn for turn in env.runner.turns if turn["session_key"] == KEY]
    assert turns[0]["message"] == "never mind, do this instead"
    assert [turn["provenance"].get("source_tool") for turn in turns[1:]] == ["subagent_completion"]


# ---------------------------------------------------------------------------
# A hold a resumed turn settled is final
# ---------------------------------------------------------------------------


async def _later_interrupted_turn(env: _Env) -> AgentTaskRecord:
    """Another turn of the parent, which a later crash stopped after its reply."""
    assert env.manager is not None and env.storage is not None
    await env.manager.append_message(KEY, "user", "Now do the next thing.")
    await env.manager.append_message(KEY, "assistant", "Done with that too.")
    now = int(time.time() * 1000)
    record = AgentTaskRecord(
        session_key=KEY,
        source_kind="web",
        run_kind="default",
        status=AgentTaskStatus.ABANDONED,
        terminal_reason="process_restart",
        # Newer than the first boot's resume task.
        created_at=now + 1000,
        started_at=now + 1000,
        details={
            "source_name": "test",
            "metadata": {},
            "route": {"interaction_mode": "interactive"},
        },
    )
    await env.storage.create_agent_task(record)
    return record


@pytest.mark.parametrize(
    ("with_outbox", "new_process"),
    [(True, True), (True, False), (False, False)],
    ids=["outbox-next-boot", "outbox-same-process", "memory-same-process"],
)
async def test_a_later_recovery_does_not_reopen_a_hold_a_resumed_turn_settled(
    tmp_path: Path, with_outbox: bool, new_process: bool
) -> None:
    # The first recovery resumes the parent's turn, which carries on with the
    # abandoned children: their wake stays held. A later recovery of the same
    # parent (another interrupted turn) ends without a resumed turn. It must
    # not wake the parent about children the resumed turn already took up.
    env = _Env(tmp_path)
    await env.open()
    assert env.storage is not None and env.runner is not None
    if with_outbox:
        set_wake_outbox(SubagentWakeOutbox(env.storage))
    _tracker.evict(KEY)
    try:
        parent = await _interrupted_session(env, tail="round_complete")
        await _abandoned_child(env, parent.task_id)
        env.runner.release = asyncio.Event()
        first = env.recovery()
        await first.scan()
        assert await _boot_announce(env, first) == 1
        env.runner.release.set()
        await first.wait_idle(timeout=10)
        await _drain(env)
        assert await _recovery_state(env, parent.task_id) == "resumed"
        # Settled for good: nothing of the group is kept in memory.
        assert not _tracker.is_held(KEY, parent.task_id)

        later = await _later_interrupted_turn(env)
        if new_process:
            _tracker.evict(KEY)
        second = env.recovery()
        await second.scan()
        assert await _boot_announce(env, second) == 0
        await second.wait_idle(timeout=10)
        await _drain(env)

        assert await _recovery_state(env, later.task_id) == "closed"
        turns = [turn for turn in env.runner.turns if turn["session_key"] == KEY]
        assert [turn["provenance"].get("source_tool") for turn in turns] == ["restart_recovery"], [
            turn["message"][:60] for turn in turns
        ]
        if with_outbox:
            row = await env.storage.get_subagent_wake(group_key(KEY, parent.task_id))
            assert row is not None and row["state"] == "dropped"
    finally:
        set_wake_outbox(None)
        _tracker.evict(KEY)
        await env.close()
