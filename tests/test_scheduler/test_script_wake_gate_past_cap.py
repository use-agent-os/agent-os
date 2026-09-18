"""A ``{"wakeAgent": false}`` gate still closes a script's output past the 16k cap.

The runner clips a successful run's stdout to ``MAX_SCRIPT_OUTPUT_CHARS`` and both
handlers then read the gate from the *last* line of what it returns. Clipping to
the head dropped a gate that closed a longer output: the script job delivered
16k characters of a tick that asked to stay quiet, and an ``agent_turn`` job ran
the turn its pre-run script had called off.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.scheduler import scripts
from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_agent_run_handler, make_script_run_handler
from agentos.scheduler.payloads import make_agent_turn_payload, make_script_payload
from agentos.scheduler.scripts import (
    MAX_SCRIPT_OUTPUT_CHARS,
    has_actionable_output,
    run_job_script,
)
from agentos.scheduler.types import CronJob, DeliveryConfig, SessionTarget

# One status line per checked host, then the gate: ~18k characters of stdout,
# the shape a watcher that reports what it checked before deciding produces.
_LONG_THEN = (
    "import json\n"
    "for i in range(300):\n"
    "    print(f'checked host-{{i:03d}}: ok, latency 12ms, disk 41%, load 0.20')\n"
    "print({gate!r})\n"
)
_CLOSED = json.dumps({"wakeAgent": False})
_OPEN = json.dumps({"wakeAgent": True})


@pytest.fixture
def agentos_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_script(home: Path, name: str, body: str) -> None:
    path = home / "scripts" / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def _script_job(script: str) -> CronJob:
    job = CronJob(
        id="watchdog",
        name="Watchdog",
        handler_key="script_run",
        payload=make_script_payload(script, "main", ""),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=30.0,
    )
    job.origin_session_key = "webchat:abc"
    return job


# ── the runner ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_closed_gate_past_the_cap_still_reads_as_nothing_to_report(agentos_home):
    _write_script(agentos_home, "watch.py", _LONG_THEN.format(gate=_CLOSED))

    ok, output = await run_job_script("watch.py", timeout=30)

    assert ok
    assert "[output truncated]" in output
    assert output.splitlines()[-1] == _CLOSED
    assert has_actionable_output(output) is False


@pytest.mark.asyncio
async def test_an_open_gate_past_the_cap_is_clipped_exactly_as_before(agentos_home):
    """Only a closed gate is carried: anything else still ends on the marker."""
    _write_script(agentos_home, "watch.py", _LONG_THEN.format(gate=_OPEN))

    ok, output = await run_job_script("watch.py", timeout=30)

    assert ok
    assert output.endswith("[output truncated]")
    assert len(output) <= MAX_SCRIPT_OUTPUT_CHARS + 32
    assert has_actionable_output(output) is True


@pytest.mark.asyncio
async def test_a_closed_gate_under_the_cap_is_returned_untouched(agentos_home):
    _write_script(agentos_home, "watch.py", f"print('all quiet')\nprint({_CLOSED!r})\n")

    ok, output = await run_job_script("watch.py", timeout=30)

    # splitlines: a Windows child writes \r\n, which the runner passes through.
    assert ok
    assert output.splitlines() == ["all quiet", _CLOSED]


def test_clipping_never_changes_the_gate_verdict():
    """Fuzz: clipped output answers has_actionable_output like the whole stdout."""
    rng = random.Random(2026_09_19)
    last_lines = [
        _CLOSED,
        _OPEN,
        '{"wakeAgent": false, "checked": 300}',
        '{"wakeAgent": "false"}',
        '{"other": 1}',
        "[1, 2]",
        "done",
        "{not json",
    ]
    for _ in range(2000):
        body_len = rng.choice([0, 10, MAX_SCRIPT_OUTPUT_CHARS - 40, MAX_SCRIPT_OUTPUT_CHARS + 1])
        body_len += rng.randrange(0, 5000)
        body = "".join(rng.choices("ab \n", k=body_len)).strip()
        stdout = f"{body}\n{rng.choice(last_lines)}".strip()

        clipped = scripts._clip_keeping_wake_gate(stdout)

        assert has_actionable_output(clipped) is has_actionable_output(stdout), stdout[-80:]


# ── the handlers ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_script_job_delivers_nothing_when_a_long_output_closes_the_gate(agentos_home):
    forwarded: list[dict] = []

    async def forwarder(**kwargs) -> None:
        forwarded.append(kwargs)

    _write_script(agentos_home, "watch.py", _LONG_THEN.format(gate=_CLOSED))
    handler = make_script_run_handler(DeliveryChain(session_forwarder=forwarder))

    result = await handler(_script_job("watch.py"))

    assert result.delivery_status == "skipped"
    assert forwarded == []


@pytest.mark.asyncio
async def test_script_job_still_delivers_a_long_output_with_no_gate(agentos_home):
    """Positive control: the cap still clips, and an ungated long run is delivered."""
    forwarded: list[dict] = []

    async def forwarder(**kwargs) -> None:
        forwarded.append(kwargs)

    _write_script(agentos_home, "watch.py", _LONG_THEN.format(gate="3 hosts degraded"))
    handler = make_script_run_handler(DeliveryChain(session_forwarder=forwarder))

    await handler(_script_job("watch.py"))

    assert len(forwarded) == 1
    assert forwarded[0]["text"].endswith("[output truncated]")


class _ExplodingTurnRunner:
    def run(self, **kwargs):
        raise AssertionError("the pre-run script closed the gate; no turn may run")


class _SessionManager:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def get_or_create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(**kwargs)


@pytest.mark.asyncio
async def test_agent_turn_is_skipped_when_a_long_prerun_output_closes_the_gate(agentos_home):
    _write_script(agentos_home, "collect.py", _LONG_THEN.format(gate=_CLOSED))
    session_manager = _SessionManager()
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: _ExplodingTurnRunner(),
        session_manager_ref=lambda: session_manager,
    )
    job = CronJob(
        id="triage",
        name="Triage",
        handler_key="agent_run",
        payload=make_agent_turn_payload("Report anything odd.", "main", "collect.py", "", None),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=30.0,
        delivery=DeliveryConfig(best_effort=True),
    )

    result = await handler(job)

    assert result.delivery_status == "skipped"
    assert session_manager.created == []
