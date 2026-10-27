"""Turns a restart or crash cut short are found on boot and resumed (or closed).

A real ``SessionStorage`` on a SQLite file, a real ``SessionManager`` and a
real ``TaskRuntime`` whose handler is the gateway's own
``dispatch_task_runtime_turn``; only the turn runner is a stub that records
the tool context it was given and writes a reply. Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

import agentos.tools.builtin  # noqa: F401 - registers the built-in tools
from agentos.engine.turn_progress import TURN_PROGRESS_KEY
from agentos.gateway.boot import dispatch_task_runtime_turn
from agentos.gateway.config import GatewayConfig
from agentos.gateway.readiness import GatewayReadiness
from agentos.gateway.routing import ReplyTarget, RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import SHUTDOWN_INTERRUPTED_REASON, TaskRuntime
from agentos.gateway.turn_recovery import (
    RECOVERY_KEY,
    RESTART_SAFE_TOOL_ALLOW,
    TurnRecovery,
    restart_safe_tool_names,
    set_turn_recovery,
)
from agentos.session.manager import SessionManager
from agentos.session.models import AgentTaskRecord, AgentTaskStatus
from agentos.session.storage import SessionStorage
from agentos.tools.registry import get_default_registry

KEY = "agent:main:webchat:direct:recovery"
HOUR_MS = 60 * 60 * 1000


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Runner:
    """Stub turn runner: records each turn, then writes a reply or fails."""

    def __init__(self, manager: SessionManager, *, fail: int = 0) -> None:
        self.manager = manager
        self.fail = fail
        self.turns: list[dict[str, Any]] = []
        self.release: asyncio.Event | None = None

    async def run(self, message: str, session_key: str, **kwargs: Any):
        ctx = kwargs["tool_context"]
        self.turns.append(
            {
                "message": message,
                "session_key": session_key,
                "allowed_tools": None if ctx.allowed_tools is None else set(ctx.allowed_tools),
                "provenance": dict(kwargs.get("input_provenance") or {}),
                "persisted_user_message_id": kwargs.get("persisted_user_message_id"),
            }
        )
        if self.release is not None:
            await self.release.wait()
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("provider unavailable")
        if not kwargs.get("persisted_user_message_id"):
            # A recovery turn's input row is written by the recovery itself.
            await self.manager.append_message(
                session_key,
                "user",
                message,
                provenance=kwargs.get("input_provenance"),
            )
        await self.manager.append_message(session_key, "assistant", "Picked it back up.")
        if False:  # pragma: no cover - makes this an async generator
            yield None


async def _no_emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
    return None


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.db = tmp_path / "sessions.db"
        self.storage: SessionStorage | None = None
        self.manager: SessionManager | None = None
        self.runtime: TaskRuntime | None = None
        self.runner: _Runner | None = None

    async def open(self, *, fail: int = 0) -> None:
        self.storage = SessionStorage(str(self.db))
        await self.storage.connect()
        self.manager = SessionManager(
            self.storage, inject_time_prefix=False, checkpoint_workspace_dir=self.tmp_path
        )
        self.runner = _Runner(self.manager, fail=fail)
        config = GatewayConfig(
            workspace_dir=str(self.tmp_path / "ws"),
            agent_stream_heartbeat_interval_seconds=0.0,
            agent_stream_idle_timeout_seconds=5.0,
        )
        runner = self.runner
        manager = self.manager

        async def _handler(run: Any) -> None:
            await dispatch_task_runtime_turn(
                run,
                config=config,
                session_manager=manager,
                turn_runner=runner,
                event_emitter=_no_emit,
            )

        self.runtime = TaskRuntime(storage=self.storage, turn_handler=_handler)

    async def close(self) -> None:
        if self.runtime is not None:
            await self.runtime.shutdown(cancel=True, timeout=1.0)
        if self.storage is not None:
            await self.storage.close()

    def recovery(self, **kwargs: Any) -> TurnRecovery:
        kwargs.setdefault("backoff_s", (0.0,))
        return TurnRecovery(
            storage=self.storage,
            session_manager=self.manager,
            task_runtime=self.runtime,
            **kwargs,
        )


@pytest.fixture
async def env(tmp_path: Path):
    environment = _Env(tmp_path)
    await environment.open()
    yield environment
    await environment.close()


def _record(messages: list[dict[str, Any]], *, round_index: int, phase: str) -> dict[str, Any]:
    return {
        "kind": "agentos.provider_messages",
        "v": 1,
        "mode": "append",
        "messages": messages,
        TURN_PROGRESS_KEY: {"state": "running", "round": round_index, "phase": phase},
    }


_CALL = {
    "role": "assistant",
    "content": [{"type": "tool_use", "id": "c1", "name": "act", "input": {"n": 1}}],
}
_RESULT = {
    "role": "user",
    "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}],
}


async def _interrupted_session(
    env: _Env,
    *,
    tail: str,
    key: str = KEY,
    source_kind: str = "web",
    run_kind: str = "default",
    created_at: int | None = None,
    details: dict[str, Any] | None = None,
    reason: str = "process_restart",
    session_origin: dict[str, Any] | None = None,
) -> AgentTaskRecord:
    """A session whose newest task the gateway stopped under, with *tail*."""
    assert env.manager is not None and env.storage is not None
    await env.manager.create(key, **({"origin": session_origin} if session_origin else {}))
    await env.manager.append_message(key, "user", "Do the thing.")
    if tail == "finished":
        await env.manager.append_message(key, "assistant", "Done.")
    elif tail == "round_complete":
        await env.manager.write_turn_progress(
            key,
            message_id="progress-1",
            tool_calls=None,
            provider_messages=_record([_CALL, _RESULT], round_index=1, phase="round_complete"),
        )
    elif tail == "unknown_outcome":
        await env.manager.write_turn_progress(
            key,
            message_id="progress-1",
            tool_calls=None,
            provider_messages=_record([_CALL], round_index=1, phase="tools_running"),
        )
    now = int(time.time() * 1000)
    record = AgentTaskRecord(
        session_key=key,
        source_kind=source_kind,
        run_kind=run_kind,
        status=AgentTaskStatus.ABANDONED,
        terminal_reason=reason,
        created_at=created_at or now - 1000,
        started_at=created_at or now - 1000,
        details={
            "source_name": "test",
            "metadata": {},
            "route": {"interaction_mode": "interactive"},
            **(details or {}),
        },
    )
    await env.storage.create_agent_task(record)
    return record


async def _state(env: _Env, task_id: str) -> dict[str, Any]:
    assert env.storage is not None
    record = await env.storage.get_agent_task(task_id)
    assert record is not None and isinstance(record.details, dict)
    return dict(record.details.get(RECOVERY_KEY) or {})


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------


async def test_graceful_shutdown_marks_a_running_turn_interrupted(env: _Env) -> None:
    assert env.runtime is not None and env.runner is not None and env.storage is not None
    env.runner.release = asyncio.Event()  # the turn runs until the shutdown
    envelope = RouteEnvelope(
        source_kind=SourceKind.CHANNEL,
        source_name="telegram",
        agent_id="main",
        session_key=KEY,
        sender_id="u-1",
        channel_name="telegram",
        channel_id="chat-9",
        reply_target=ReplyTarget(kind="channel", channel_name="telegram", to="chat-9"),
    )
    handle = await env.runtime.enqueue(envelope, "hello")
    for _ in range(100):
        if env.runner.turns:
            break
        await asyncio.sleep(0.01)
    assert env.runner.turns

    await env.runtime.shutdown(cancel=True, timeout=1.0)

    record = await env.storage.get_agent_task(handle.task_id)
    assert record is not None
    assert record.status == AgentTaskStatus.ABANDONED
    assert record.terminal_reason == SHUTDOWN_INTERRUPTED_REASON
    route = record.details["route"]
    assert route["channel_name"] == "telegram"
    assert route["reply_target"]["to"] == "chat-9"


async def test_crash_orphan_is_swept_on_open_and_found_by_recovery(tmp_path: Path) -> None:
    environment = _Env(tmp_path)
    await environment.open()
    assert environment.manager is not None and environment.storage is not None
    await environment.manager.create(KEY)
    await environment.manager.append_message(KEY, "user", "Do the thing.")
    running = AgentTaskRecord(
        session_key=KEY,
        source_kind="web",
        status=AgentTaskStatus.RUNNING,
        details={"source_name": "test", "metadata": {}, "route": {}},
    )
    await environment.storage.create_agent_task(running)
    await environment.storage.close()
    environment.storage = None

    # The "restarted" process: opening the database sweeps the orphan.
    await environment.open()
    try:
        assert environment.storage is not None
        swept = await environment.storage.get_agent_task(running.task_id)
        assert swept is not None and swept.status == AgentTaskStatus.ABANDONED
        assert swept.terminal_reason == "process_restart"

        recovery = environment.recovery()
        await recovery.scan()
        await recovery.wait_idle(timeout=5)
        assert (await _state(environment, running.task_id))["state"] == "resumed"
        assert environment.runner is not None and len(environment.runner.turns) == 1
    finally:
        await environment.close()


# ---------------------------------------------------------------------------
# Classification outcomes
# ---------------------------------------------------------------------------


async def test_finished_turn_is_closed_without_a_resume(env: _Env) -> None:
    record = await _interrupted_session(env, tail="finished")
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    assert (await _state(env, record.task_id))["state"] == "closed"
    assert env.runner is not None and env.runner.turns == []
    assert recovery.snapshot()["closed"] == 1


async def test_unanswered_message_resumes_with_the_full_tool_set(env: _Env) -> None:
    record = await _interrupted_session(env, tail="none")
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)

    assert env.runner is not None
    [turn] = env.runner.turns
    assert turn["allowed_tools"] is None
    assert "Answer it now" in turn["message"]
    assert turn["provenance"]["kind"] == "internal_system"
    assert turn["provenance"]["origin_task_id"] == record.task_id
    state = await _state(env, record.task_id)
    assert (state["state"], state["reason"], state["attempts"]) == (
        "resumed",
        "unanswered_input",
        1,
    )


async def test_completed_round_resumes_normally(env: _Env) -> None:
    record = await _interrupted_session(env, tail="round_complete")
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)

    assert env.runner is not None
    [turn] = env.runner.turns
    assert turn["allowed_tools"] is None
    assert "Carry on from where it stopped" in turn["message"]
    assert (await _state(env, record.task_id))["reason"] == "round_complete"


async def test_unknown_outcome_resumes_restricted_and_the_next_user_turn_is_not(
    env: _Env,
) -> None:
    record = await _interrupted_session(env, tail="unknown_outcome")
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)

    assert env.runner is not None and env.runtime is not None
    [turn] = env.runner.turns
    safe = restart_safe_tool_names()
    assert turn["allowed_tools"] == set(safe)
    assert "outcome is unknown" in turn["message"]
    state = await _state(env, record.task_id)
    assert (state["state"], state["restricted"]) == ("resumed", True)

    # The next user turn on the session builds its own context: all tools.
    user = RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="test", agent_id="main", session_key=KEY
    )
    handle = await env.runtime.enqueue(user, "and now?")
    await env.runtime.wait(handle.task_id, timeout=5)
    assert env.runner.turns[-1]["allowed_tools"] is None


async def test_own_continuation_row_is_not_taken_for_an_unanswered_message(env: _Env) -> None:
    record = await _interrupted_session(env, tail="finished")
    assert env.manager is not None
    # The previous boot's resume attempt wrote its continuation, then died.
    await env.manager.append_message(
        KEY,
        "user",
        "[Restart recovery] ...",
        provenance={"kind": "internal_system", "source_tool": "restart_recovery"},
    )
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    assert env.runner is not None
    [turn] = env.runner.turns
    assert "Carry on from where it stopped" in turn["message"]
    assert (await _state(env, record.task_id))["state"] == "resumed"


def test_restricted_context_hides_and_refuses_everything_else() -> None:
    from agentos.gateway.routing import tool_context_from_envelope
    from agentos.gateway.turn_recovery import apply_restart_recovery_tool_policy
    from agentos.tools.visibility import is_tool_visible

    registry = get_default_registry()
    envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="main",
        session_key=KEY,
        metadata={RECOVERY_KEY: {"origin_task_id": "t-1", "restricted": True}},
    )
    ctx = tool_context_from_envelope(envelope)
    assert apply_restart_recovery_tool_policy(ctx, envelope) is True
    read_file = registry.get("read_file")
    write_file = registry.get("write_file")
    assert read_file is not None and write_file is not None
    assert is_tool_visible(read_file, ctx) is True
    assert is_tool_visible(write_file, ctx) is False

    # A configured policy that widens the allowlist cannot bring tools back.
    from agentos.tools.policy_config import ToolPolicy
    from agentos.tools.policy_helpers import apply_tool_policy

    widened = apply_tool_policy(
        ctx,
        available_tools=registry.list_names(),
        global_policy=ToolPolicy(profile="full", allow=frozenset({"write_file", "bash"})),
        agent_policy=ToolPolicy(profile="full"),
    )
    assert is_tool_visible(write_file, widened) is False
    assert is_tool_visible(read_file, widened) is True
    assert widened.allowed_tools is None or "write_file" not in widened.allowed_tools
    assert "write_file" in widened.denied_tools

    # Not restricted: a normal resume and every ordinary turn keep their tools.
    plain = RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="test", agent_id="main", session_key=KEY
    )
    plain_ctx = tool_context_from_envelope(plain)
    assert apply_restart_recovery_tool_policy(plain_ctx, plain) is False
    assert plain_ctx.allowed_tools is None


def test_restricted_set_is_read_only_and_excludes_side_effects() -> None:
    registry = get_default_registry()
    safe = restart_safe_tool_names()
    assert safe, "the replay-safe set must not be empty once built-ins are registered"
    assert safe <= RESTART_SAFE_TOOL_ALLOW
    for name in safe:
        registered = registry.get(name)
        assert registered is not None and registered.spec.read_only is True, name
    for name in (
        "bash",
        "exec",
        "write_file",
        "edit_file",
        "apply_patch",
        "http_request",
        "web_fetch",
        "message",
        "sessions_send",
        "sessions_spawn",
        "cron",
        "gateway",
    ):
        assert name not in safe, name


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


async def test_three_failed_attempts_leave_a_tombstone(tmp_path: Path) -> None:
    environment = _Env(tmp_path)
    await environment.open(fail=5)
    try:
        record = await _interrupted_session(environment, tail="round_complete")
        recovery = environment.recovery()
        await recovery.scan()
        await recovery.wait_idle(timeout=10)

        assert environment.runner is not None and len(environment.runner.turns) == 3
        state = await _state(environment, record.task_id)
        assert (state["state"], state["attempts"]) == ("tombstoned", 3)
        assert len(state["resume_task_ids"]) == 3
        assert environment.manager is not None
        rows = await environment.manager.get_transcript(KEY)
        assert rows[-1].role == "system"
        assert "could not be resumed automatically (3 attempts)" in str(rows[-1].content)
        assert recovery.snapshot()["tombstoned"] == 1

        # A later boot does not start another cycle for the same run.
        again = environment.recovery()
        await again.scan()
        await again.wait_idle(timeout=5)
        assert len(environment.runner.turns) == 3
    finally:
        await environment.close()


async def test_retried_attempts_reuse_one_continuation_row(tmp_path: Path) -> None:
    environment = _Env(tmp_path)
    await environment.open(fail=2)
    try:
        record = await _interrupted_session(environment, tail="round_complete")
        recovery = environment.recovery()
        await recovery.scan()
        await recovery.wait_idle(timeout=10)

        assert environment.runner is not None and len(environment.runner.turns) == 3
        state = await _state(environment, record.task_id)
        assert (state["state"], state["attempts"]) == ("resumed", 3)
        assert environment.manager is not None
        rows = await environment.manager.get_transcript(KEY)
        continuations = [
            row
            for row in rows
            if row.role == "user" and str(row.content).startswith("[Restart recovery]")
        ]
        assert len(continuations) == 1
        # Every attempt ran with that one row as its input.
        ids = {turn["persisted_user_message_id"] for turn in environment.runner.turns}
        assert ids == {continuations[0].message_id}
        assert state["continuation_id"] == continuations[0].message_id
        assert rows[-1].content == "Picked it back up."
    finally:
        await environment.close()


async def test_attempts_count_across_restarts(tmp_path: Path) -> None:
    environment = _Env(tmp_path)
    await environment.open(fail=5)
    try:
        origin = await _interrupted_session(
            environment,
            tail="round_complete",
            details={RECOVERY_KEY: {"state": "running", "attempts": 2, "restricted": True}},
        )
        # The previous boot's resume turn was itself cut short by a restart.
        assert environment.storage is not None
        resume = AgentTaskRecord(
            session_key=KEY,
            source_kind="web",
            status=AgentTaskStatus.ABANDONED,
            terminal_reason=SHUTDOWN_INTERRUPTED_REASON,
            created_at=int(time.time() * 1000),
            details={
                "source_name": "test",
                "metadata": {RECOVERY_KEY: {"origin_task_id": origin.task_id}},
                "route": {},
            },
        )
        await environment.storage.create_agent_task(resume)

        recovery = environment.recovery()
        await recovery.scan()
        await recovery.wait_idle(timeout=10)

        assert environment.runner is not None and len(environment.runner.turns) == 1
        assert environment.runner.turns[0]["allowed_tools"] == set(restart_safe_tool_names())
        state = await _state(environment, origin.task_id)
        assert (state["state"], state["attempts"]) == ("tombstoned", 3)
    finally:
        await environment.close()


async def test_turn_older_than_a_day_is_not_resumed(env: _Env) -> None:
    old = int(time.time() * 1000) - 25 * HOUR_MS
    record = await _interrupted_session(env, tail="round_complete", created_at=old)
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    assert env.runner is not None and env.runner.turns == []
    assert (await _state(env, record.task_id))["state"] == "expired"
    assert recovery.snapshot()["expired"] == 1


async def test_a_new_user_message_cancels_a_pending_recovery(env: _Env) -> None:
    record = await _interrupted_session(env, tail="round_complete")
    recovery = env.recovery(backoff_s=(30.0,))
    await recovery.scan()
    assert recovery.snapshot()["pending"] == 1

    assert env.runtime is not None and env.runner is not None
    user = RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="test", agent_id="main", session_key=KEY
    )
    handle = await env.runtime.enqueue(user, "never mind, do this instead")
    await env.runtime.wait(handle.task_id, timeout=5)
    await recovery.wait_idle(timeout=5)

    assert [turn["message"] for turn in env.runner.turns] == ["never mind, do this instead"]
    assert (await _state(env, record.task_id))["state"] == "cancelled"


# ---------------------------------------------------------------------------
# Exclusions, report-only mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "source_kind", "run_kind", "origin"),
    [
        ("agent:main:cron:nightly", "cron", "cron_turn", None),
        ("agent:main:webchat:direct:acp-1", "web", "acp", None),
        ("agent:main:webchat:direct:acp-2", "web", "default", {"kind": "acp"}),
        ("agent:main:subagent:child", "subagent", "subagent", None),
    ],
)
async def test_cron_acp_and_subagent_turns_are_never_resumed(
    env: _Env,
    key: str,
    source_kind: str,
    run_kind: str,
    origin: dict[str, Any] | None,
) -> None:
    await _interrupted_session(
        env,
        tail="round_complete",
        key=key,
        source_kind=source_kind,
        run_kind=run_kind,
        session_origin=origin,
    )
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    assert recovery.snapshot()["found"] == 0
    assert env.runner is not None and env.runner.turns == []


async def test_report_only_mode_leaves_a_note_and_does_not_resume(env: _Env) -> None:
    record = await _interrupted_session(env, tail="round_complete")
    recovery = env.recovery(auto_resume=False)
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    assert env.runner is not None and env.runner.turns == []
    assert (await _state(env, record.task_id))["state"] == "reported"
    assert env.manager is not None
    rows = await env.manager.get_transcript(KEY)
    assert "Automatic resume is off" in str(rows[-1].content)


async def test_safe_mode_only_reports(env: _Env) -> None:
    record = await _interrupted_session(env, tail="round_complete")
    recovery = env.recovery(safe_mode_active=lambda: True)
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    assert env.runner is not None and env.runner.turns == []
    assert (await _state(env, record.task_id))["state"] == "reported"


# ---------------------------------------------------------------------------
# Channel delivery
# ---------------------------------------------------------------------------


class _Adapter:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _PairingStore:
    def __init__(self, valid: bool) -> None:
        self.valid = valid

    def validate_admission(self, admission: Any) -> bool:
        return self.valid


class _ChannelManager:
    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter
        self.resolved: list[dict[str, Any]] = []

    def get(self, name: str) -> Any:
        return self.adapter

    def resolve_delivery_target(self, **kwargs: Any) -> Any:
        from agentos.channels.types import DeliveryTargetResolution

        self.resolved.append(kwargs)
        return DeliveryTargetResolution(
            ok=True,
            adapter=self.adapter,
            adapter_name="telegram",
            channel_type="telegram",
            to=kwargs["to"],
            thread_id=kwargs.get("thread_id", ""),
        )


def _channel_details() -> dict[str, Any]:
    return {
        "source_name": "telegram",
        "route": {
            "sender_id": "u-1",
            "channel_name": "telegram",
            "channel_id": "chat-9",
            "interaction_mode": "unattended",
            "reply_target": {"kind": "channel", "channel_name": "telegram", "to": "chat-9"},
            "channel_admission": {
                "channel_name": "telegram",
                "sender_id": "u-1",
                "grant_id": "g-1",
                "grant_version": 1,
            },
        },
    }


async def test_resumed_channel_turn_replies_in_the_original_chat(env: _Env) -> None:
    adapter = _Adapter()
    adapter.pairing_store = _PairingStore(valid=True)  # type: ignore[attr-defined]
    manager = _ChannelManager(adapter)
    await _interrupted_session(
        env,
        tail="round_complete",
        key="agent:main:telegram:direct:u-1",
        source_kind="channel",
        run_kind="channel_turn",
        details=_channel_details(),
    )
    recovery = env.recovery(channel_manager_ref=lambda: manager)
    await recovery.scan()
    await recovery.wait_idle(timeout=5)

    assert manager.resolved[-1]["target"] == "telegram"
    assert manager.resolved[-1]["to"] == "chat-9"
    assert [message.content for message in adapter.sent] == ["Picked it back up."]


async def test_revoked_pairing_closes_without_resuming_or_replying(env: _Env) -> None:
    adapter = _Adapter()
    adapter.pairing_store = _PairingStore(valid=False)  # type: ignore[attr-defined]
    manager = _ChannelManager(adapter)
    record = await _interrupted_session(
        env,
        tail="round_complete",
        key="agent:main:telegram:direct:u-1",
        source_kind="channel",
        run_kind="channel_turn",
        details=_channel_details(),
    )
    recovery = env.recovery(channel_manager_ref=lambda: manager)
    await recovery.scan()
    await recovery.wait_idle(timeout=5)

    assert env.runner is not None and env.runner.turns == []
    assert adapter.sent == []
    state = await _state(env, record.task_id)
    assert (state["state"], state["reason"]) == ("closed", "pairing_revoked")


# ---------------------------------------------------------------------------
# /ready and doctor
# ---------------------------------------------------------------------------


async def test_ready_and_doctor_report_recovery_counts(env: _Env) -> None:
    from agentos.gateway.rpc_doctor import _turn_recovery_findings

    await _interrupted_session(env, tail="round_complete")
    recovery = env.recovery(auto_resume=False)
    await recovery.scan()

    readiness = GatewayReadiness()
    readiness.attach_turn_recovery(recovery.snapshot)
    readiness.mark_serving()
    snapshot = readiness.snapshot()
    assert snapshot["ready"] is True
    assert snapshot["turn_recovery"]["found"] == 1
    assert snapshot["turn_recovery"]["reported"] == 1
    assert snapshot["turn_recovery"]["auto_resume"] is False

    set_turn_recovery(recovery)
    try:
        findings = _turn_recovery_findings()
    finally:
        set_turn_recovery(None)
    [finding] = findings
    assert finding.id == "gateway.turn_recovery.not_resumed"
    assert finding.severity == "warn"
    assert finding.evidence["sessions"][0]["session_key"] == KEY
    assert any(
        step.command == "agentos config set task_runtime.resume_interrupted_turns true"
        for step in finding.fix_steps
    )


# ---------------------------------------------------------------------------
# A turn that asked for the restart is not resumed
# ---------------------------------------------------------------------------


async def _session_with_calls(env: _Env, calls: list[dict[str, Any]], *, answered: bool):
    record = await _interrupted_session(env, tail="none")
    assert env.manager is not None
    messages: list[dict[str, Any]] = [{"role": "assistant", "content": calls}]
    if answered:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call["id"], "content": "ok"}
                    for call in calls
                ],
            }
        )
    await env.manager.write_turn_progress(
        KEY,
        message_id="progress-restart",
        tool_calls=None,
        provider_messages=_record(
            messages, round_index=1, phase="round_complete" if answered else "tools_running"
        ),
    )
    return record


@pytest.mark.parametrize(
    ("call", "answered"),
    [
        ({"name": "gateway", "input": {"action": "restart", "mode": "safe"}}, False),
        ({"name": "gateway", "input": {"action": "restart"}}, True),
        ({"name": "exec_command", "input": {"command": "agentos gateway restart"}}, True),
        ({"name": "exec_command", "input": {"command": "uv tool upgrade use-agent-os"}}, False),
    ],
)
async def test_turn_that_asked_for_the_restart_is_reported_not_resumed(
    env: _Env, call: dict[str, Any], answered: bool
) -> None:
    record = await _session_with_calls(
        env, [{"type": "tool_use", "id": "r1", **call}], answered=answered
    )
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)

    assert env.runner is not None and env.runner.turns == []
    state = await _state(env, record.task_id)
    assert (state["state"], state["reason"]) == ("reported", "restart_requested")
    assert env.manager is not None
    rows = await env.manager.get_transcript(KEY)
    assert "restarted as this conversation asked" in str(rows[-1].content)


async def test_session_named_by_the_restart_request_is_not_resumed(env: _Env) -> None:
    record = await _interrupted_session(env, tail="round_complete")
    recovery = env.recovery(restart_requested_by=KEY)
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    assert env.runner is not None and env.runner.turns == []
    assert (await _state(env, record.task_id))["reason"] == "restart_requested"


async def test_other_shell_commands_still_resume(env: _Env) -> None:
    calls = [
        {"type": "tool_use", "id": "s1", "name": "exec_command", "input": {"command": "ls -la"}},
        {"type": "tool_use", "id": "s2", "name": "grep_search", "input": {"q": "gateway restart"}},
    ]
    record = await _session_with_calls(env, calls, answered=True)
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    assert env.runner is not None and len(env.runner.turns) == 1
    assert (await _state(env, record.task_id))["state"] == "resumed"


def test_peek_request_leaves_the_restart_request_for_the_announce(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from agentos.gateway.restart import (
        RestartOrigin,
        RestartRequest,
        peek_request,
        read_and_clear_request,
        restart_request_path,
    )

    config = SimpleNamespace(state_dir=str(tmp_path))
    path = restart_request_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    request = RestartRequest(
        restart_id="r-1",
        mode="safe",
        reason="test",
        requested_at=time.time(),
        origin=RestartOrigin.web(KEY, None),
    )
    import json

    path.write_text(json.dumps(request.to_json()), encoding="utf-8")
    peeked = peek_request(config)
    assert peeked is not None and peeked.origin.session_key == KEY
    assert path.exists()
    assert read_and_clear_request(config) is not None
    assert peek_request(config) is None


async def test_doctor_names_the_actual_reason_a_turn_was_not_resumed(env: _Env) -> None:
    from agentos.gateway.rpc_doctor import _turn_recovery_findings

    await _session_with_calls(
        env,
        [{"type": "tool_use", "id": "r1", "name": "gateway", "input": {"action": "restart"}}],
        answered=False,
    )
    recovery = env.recovery()
    await recovery.scan()
    await recovery.wait_idle(timeout=5)
    set_turn_recovery(recovery)
    try:
        [finding] = _turn_recovery_findings()
    finally:
        set_turn_recovery(None)
    assert finding.id == "gateway.turn_recovery.not_resumed"
    assert "the turn asked for the restart itself" in finding.detail
    assert "attempts" not in finding.detail
    assert "safe mode" not in finding.detail
