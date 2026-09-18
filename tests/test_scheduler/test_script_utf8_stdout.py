"""Cron scripts write their output as UTF-8, whatever the gateway's code page.

``run_job_script`` decodes a script's stdout and stderr as UTF-8, but a Python
child writing to a pipe encodes with the locale code page: cp1252 on a Western
Windows install, cp936 on a Chinese one. A feed title with an emoji or CJK
text then raised ``UnicodeEncodeError`` inside the script. The job failed, and
repeated failures end in ``FAILED``. A title the code page *could* encode
("Café") arrived as U+FFFD instead.

``PYTHONIOENCODING`` on the gateway side stands in for that Windows code page
here. A piped child picks it up exactly as it would pick up the console's.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_agent_run_handler, make_script_run_handler
from agentos.scheduler.payloads import make_agent_turn_payload, make_script_payload
from agentos.scheduler.scripts import run_job_script
from agentos.scheduler.types import CronJob, DeliveryConfig, SessionTarget

_CASES = [
    pytest.param("cp1252", "New post: Café opening", id="cp1252-latin"),
    pytest.param("cp1252", "New post: Launch day \U0001f680", id="cp1252-emoji"),
    pytest.param("cp1252", "新着: リリースノート", id="cp1252-cjk"),
    pytest.param("cp936", "新着: リリースノート", id="cp936-cjk"),
    pytest.param("cp936", "New post: Launch day \U0001f680", id="cp936-emoji"),
]


@pytest.fixture
def agentos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_script(home: Path, name: str, body: str) -> None:
    (home / "scripts" / name).write_text(body, encoding="utf-8")


def _print_script(text: str) -> str:
    return f"print({text!r})\n"


def _script_job(script: str) -> CronJob:
    job = CronJob(
        id="watcher",
        name="Watcher",
        handler_key="script_run",
        payload=make_script_payload(script, "main", ""),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=30.0,
    )
    job.origin_session_key = "webchat:abc"
    return job


def _script_handler(forwarded: list[dict[str, Any]]) -> Any:
    async def forwarder(**kwargs: Any) -> None:
        forwarded.append(kwargs)

    return make_script_run_handler(DeliveryChain(session_forwarder=forwarder))


class _FakeSessionManager:
    async def get_or_create(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    async def append_message(self, session_key: str, role: str, content: str) -> Any:
        return SimpleNamespace(role=role, content=content)

    async def read_transcript(self, session_key: str) -> list[dict[str, Any]]:
        return []


class _RecordingTurnRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        async def events() -> Any:
            yield SimpleNamespace(kind="message", text="reported")
            yield SimpleNamespace(kind="done")

        return events()


# --- Fail on main: the child encoded with the gateway's code page -------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("codepage", "text"), _CASES)
async def test_script_job_delivers_the_text_the_script_printed(
    agentos_home: Path, monkeypatch: pytest.MonkeyPatch, codepage: str, text: str
) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", codepage)
    _write_script(agentos_home, "watch.py", _print_script(text))
    forwarded: list[dict[str, Any]] = []

    result = await _script_handler(forwarded)(_script_job("watch.py"))

    assert result.summary == text
    assert forwarded[0]["text"] == text


@pytest.mark.asyncio
@pytest.mark.parametrize(("codepage", "text"), _CASES)
async def test_prerun_script_output_reaches_the_turn_intact(
    agentos_home: Path, monkeypatch: pytest.MonkeyPatch, codepage: str, text: str
) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", codepage)
    _write_script(agentos_home, "watch.py", _print_script(text))
    turn_runner = _RecordingTurnRunner()
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: _FakeSessionManager(),
    )
    job = CronJob(
        id="triage",
        name="Triage",
        handler_key="agent_run",
        payload=make_agent_turn_payload("Summarise the news.", "main", "watch.py", "", None),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=30.0,
        delivery=DeliveryConfig(best_effort=True),
    )

    await handler(job)

    sent = turn_runner.calls[0]["message"]
    assert text in sent
    assert "UnicodeEncodeError" not in sent


@pytest.mark.asyncio
async def test_wake_gate_is_read_after_non_ascii_output(
    agentos_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiet tick stays quiet even when the script logged an emoji first."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    body = _print_script("checked \U0001f680") + "print('{\"wakeAgent\": false}')\n"
    _write_script(agentos_home, "gated.py", body)
    forwarded: list[dict[str, Any]] = []

    result = await _script_handler(forwarded)(_script_job("gated.py"))

    assert result.delivery_status == "skipped"
    assert forwarded == []


@pytest.mark.asyncio
async def test_failure_report_keeps_non_ascii_stderr(
    agentos_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alert for a broken watcher quotes its stderr as written."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    body = "import sys\nsys.stderr.write('feed 新着 unreachable\\n')\nsys.exit(3)\n"
    _write_script(agentos_home, "broken.py", body)

    ok, output = await run_job_script("broken.py", timeout=30)

    assert ok is False
    assert "Script exited with code 3" in output
    assert "feed 新着 unreachable" in output


# --- Guards: pass on main and with the fix, by design ---------------------


@pytest.mark.asyncio
async def test_ascii_output_is_unchanged(
    agentos_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: the common case never depended on the code page."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    _write_script(agentos_home, "watch.py", _print_script("memory at 93%"))

    assert await run_job_script("watch.py", timeout=30) == (True, "memory at 93%")


@pytest.mark.asyncio
async def test_utf8_gateway_output_is_unchanged(
    agentos_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: a gateway already on UTF-8 sees the same text as before."""
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    text = "新着 \U0001f680 Café"
    _write_script(agentos_home, "watch.py", _print_script(text))

    assert await run_job_script("watch.py", timeout=30) == (True, text)


@pytest.mark.asyncio
async def test_a_script_that_rewraps_stdout_itself_still_works(
    agentos_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: scripts that already write UTF-8 bytes are unaffected."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    text = "新着 \U0001f680"
    body = f"import sys\nsys.stdout.buffer.write({text!r}.encode('utf-8'))\n"
    _write_script(agentos_home, "bytes.py", body)

    assert await run_job_script("bytes.py", timeout=30) == (True, text)
