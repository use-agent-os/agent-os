"""A run whose script closed the wake gate keeps what the script printed.

``{"wakeAgent": false}`` keeps a tick off the channel. The run record used to
replace the whole stdout with ``silent: script produced no output to deliver``,
which is false for a script that printed a payload above its gate, and left
``cron output`` nothing to show. senior-unilp-manager's ``ratchet tick --json
--alert-only`` prints its whole payload above the gate precisely so that "the
cron run history keeps it, so a quiet tick stays auditable".
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_agent_run_handler, make_script_run_handler
from agentos.scheduler.jobs import execute_with_timeout
from agentos.scheduler.payloads import make_agent_turn_payload, make_script_payload
from agentos.scheduler.types import CronJob, DeliveryConfig, SessionTarget

_GATE = json.dumps({"wakeAgent": False})
_PAYLOAD = '{"chain": "robinhood", "results": [{"mandateId": "m-1", "action": "noop"}]}'


@pytest.fixture
def agentos_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_script(home: Path, name: str, *lines: str) -> None:
    path = home / "scripts" / name
    path.write_text("".join(f"print({line!r})\n" for line in lines), encoding="utf-8")
    path.chmod(0o700)


def _script_job(script: str) -> CronJob:
    job = CronJob(
        id="ratchet",
        name="Ratchet",
        handler_key="script_run",
        payload=make_script_payload(script, "main", ""),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=30.0,
    )
    job.origin_session_key = "webchat:abc"
    return job


def _script_handler(forwarded: list[dict]):
    async def forwarder(**kwargs) -> None:
        forwarded.append(kwargs)

    return make_script_run_handler(DeliveryChain(session_forwarder=forwarder))


# ── script jobs ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_record_keeps_the_payload_printed_above_a_closed_gate(agentos_home):
    _write_script(agentos_home, "tick.py", _PAYLOAD, _GATE)
    forwarded: list[dict] = []

    execution = await execute_with_timeout(_script_job("tick.py"), _script_handler(forwarded))

    assert execution.success is True
    assert execution.delivery_status == "skipped"
    assert forwarded == []
    assert execution.summary is not None
    assert _PAYLOAD in execution.summary
    assert execution.summary.endswith(_GATE)


@pytest.mark.asyncio
async def test_a_gated_run_is_not_recorded_as_having_no_output(agentos_home):
    """The ``silent:`` line stays first, and names the gate rather than an empty stdout."""
    _write_script(agentos_home, "tick.py", "3 mandates checked", _GATE)

    execution = await execute_with_timeout(_script_job("tick.py"), _script_handler([]))

    first_line = (execution.summary or "").splitlines()[0]
    assert first_line == "silent: script closed its wakeAgent gate; nothing delivered"


@pytest.mark.asyncio
async def test_a_bare_gate_is_still_recorded_as_silent(agentos_home):
    _write_script(agentos_home, "tick.py", _GATE)

    execution = await execute_with_timeout(_script_job("tick.py"), _script_handler([]))

    assert execution.summary == (
        f"silent: script closed its wakeAgent gate; nothing delivered\n\n{_GATE}"
    )


@pytest.mark.asyncio
async def test_empty_stdout_keeps_its_silent_summary(agentos_home):
    """Unchanged: a script that printed nothing is recorded exactly as before."""
    (agentos_home / "scripts" / "quiet.py").write_text("pass\n", encoding="utf-8")

    execution = await execute_with_timeout(_script_job("quiet.py"), _script_handler([]))

    assert execution.summary == "silent: script produced no output to deliver"
    assert execution.delivery_status == "skipped"


@pytest.mark.asyncio
async def test_a_delivered_run_is_recorded_as_before(agentos_home):
    """Positive control: an open run still delivers its stdout and records it verbatim."""
    _write_script(agentos_home, "tick.py", "m-1 fired: tx 0xabc")
    forwarded: list[dict] = []

    execution = await execute_with_timeout(_script_job("tick.py"), _script_handler(forwarded))

    assert execution.summary == "m-1 fired: tx 0xabc"
    assert [item["text"] for item in forwarded] == ["m-1 fired: tx 0xabc"]


# ── agent_turn pre-run scripts ──────────────────────────────────────────────


class _ExplodingTurnRunner:
    def run(self, **kwargs):
        raise AssertionError("the pre-run script closed the gate; no turn may run")


class _SessionManager:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def get_or_create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(**kwargs)


def _agent_job(script: str) -> CronJob:
    return CronJob(
        id="triage",
        name="Triage",
        handler_key="agent_run",
        payload=make_agent_turn_payload("Report anything odd.", "main", script, "", None),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=30.0,
        delivery=DeliveryConfig(best_effort=True),
    )


@pytest.mark.asyncio
async def test_a_gated_prerun_records_what_the_script_printed(agentos_home):
    _write_script(agentos_home, "collect.py", "inbox: 0 new, 14 read", _GATE)
    session_manager = _SessionManager()
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: _ExplodingTurnRunner(),
        session_manager_ref=lambda: session_manager,
    )

    execution = await execute_with_timeout(_agent_job("collect.py"), handler)

    assert execution.delivery_status == "skipped"
    assert session_manager.created == []
    # splitlines: a Windows child writes \r\n, which the runner passes through.
    assert (execution.summary or "").splitlines() == [
        "silent: pre-run script reported nothing to act on",
        "",
        "inbox: 0 new, 14 read",
        _GATE,
    ]


@pytest.mark.asyncio
async def test_an_empty_prerun_keeps_its_silent_summary(agentos_home):
    (agentos_home / "scripts" / "collect.py").write_text("pass\n", encoding="utf-8")
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: _ExplodingTurnRunner(),
        session_manager_ref=lambda: _SessionManager(),
    )

    execution = await execute_with_timeout(_agent_job("collect.py"), handler)

    assert execution.summary == "silent: pre-run script reported nothing to act on"
