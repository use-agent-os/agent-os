"""Cron ``script`` jobs — the one mode that never reaches a model.

Covers the path confinement, the runner's three outcomes (output / silence /
failure), and the payload contract the RPC, CLI, and tool surfaces share.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_script_run_handler
from agentos.scheduler.payloads import (
    SCRIPT_KIND,
    make_script_payload,
    normalize_contract,
    payload_script,
    payload_text,
    payload_workdir,
)
from agentos.scheduler.scripts import (
    MAX_SCRIPT_OUTPUT_CHARS,
    ScriptPathError,
    has_actionable_output,
    normalize_script_value,
    resolve_script_path,
    run_job_script,
    scripts_dir,
    validate_script_path,
)
from agentos.scheduler.types import (
    CronJob,
    DeliveryConfig,
    DeliveryMode,
    SessionTarget,
)


@pytest.fixture
def agentos_home(tmp_path, monkeypatch):
    """Point the state root at a temp dir so scripts/ is isolated per test."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_script(home: Path, name: str, body: str) -> Path:
    path = home / "scripts" / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path


def _script_job(script: str, *, workdir: str = "", timeout: float = 30.0) -> CronJob:
    return CronJob(
        id="watchdog",
        name="Watchdog",
        handler_key="script_run",
        payload=make_script_payload(script, "main", workdir),
        session_target=SessionTarget.ISOLATED,
        timeout_seconds=timeout,
    )


class _FakeChannelManager:
    async def send(self, *args, **kwargs) -> bool:  # pragma: no cover - unused
        return True


# ── path confinement ────────────────────────────────────────────────────────


def test_relative_path_resolves_under_scripts_dir(agentos_home):
    _write_script(agentos_home, "watch.py", "print('ok')")

    assert resolve_script_path("watch.py") == (agentos_home / "scripts" / "watch.py")


def test_scripts_dir_follows_state_dir(agentos_home):
    assert scripts_dir() == agentos_home / "scripts"


def test_traversal_out_of_scripts_dir_is_blocked(agentos_home):
    with pytest.raises(ScriptPathError, match="outside"):
        resolve_script_path("../../etc/passwd")


def test_absolute_path_inside_scripts_dir_is_allowed(agentos_home):
    path = _write_script(agentos_home, "inside.py", "print('ok')")

    assert resolve_script_path(str(path)) == path


def test_absolute_path_outside_scripts_dir_is_blocked(agentos_home):
    with pytest.raises(ScriptPathError, match="outside"):
        resolve_script_path("/etc/passwd")


def test_symlink_escape_is_blocked(agentos_home, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("print('pwned')", encoding="utf-8")
    (agentos_home / "scripts" / "link.py").symlink_to(outside)

    with pytest.raises(ScriptPathError, match="outside"):
        resolve_script_path("link.py")


def test_empty_path_is_rejected(agentos_home):
    with pytest.raises(ScriptPathError, match="required"):
        resolve_script_path("   ")


# ── API-boundary validation ─────────────────────────────────────────────────


def test_validate_accepts_relative_path(agentos_home):
    assert validate_script_path("watch.sh") is None


def test_validate_treats_empty_as_no_script(agentos_home):
    assert validate_script_path("") is None
    assert validate_script_path(None) is None


@pytest.mark.parametrize("raw", ["/etc/passwd", "~/evil.sh", "../escape.sh"])
def test_validate_rejects_paths_that_leave_the_scripts_dir(agentos_home, raw):
    assert validate_script_path(raw) is not None


def test_validate_does_not_require_the_file_to_exist(agentos_home):
    """A job may be scheduled before its script is written."""
    assert validate_script_path("not-written-yet.py") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"watch.sh"', "watch.sh"),
        ("'watch.sh'", "watch.sh"),
        ('  "watch.sh"  ', "watch.sh"),
        ("watch.sh", "watch.sh"),
        ('"', '"'),
        ("", ""),
        (None, ""),
        ('watch"quoted".sh', 'watch"quoted".sh'),
    ],
)
def test_a_quoted_path_is_unwrapped(raw, expected):
    """Asking an agent for a script job gets you `"watch.sh"`, quotes included.

    Storing that verbatim buys a job that saves cleanly and then fails at fire
    time against a path with quote characters in it.
    """
    assert normalize_script_value(raw) == expected


def test_a_quoted_path_still_resolves(agentos_home):
    _write_script(agentos_home, "watch.sh", "echo hi")

    assert resolve_script_path('"watch.sh"') == agentos_home / "scripts" / "watch.sh"


# ── the runner ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_python_script_returns_stdout(agentos_home):
    _write_script(agentos_home, "hello.py", "print('disk 91% full')")

    ok, output = await run_job_script("hello.py", timeout=30)

    assert ok is True
    assert output == "disk 91% full"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="bash is not guaranteed on Windows")
async def test_shell_script_runs_under_bash(agentos_home):
    _write_script(agentos_home, "watch.sh", "echo 'load high'")

    ok, output = await run_job_script("watch.sh", timeout=30)

    assert ok is True
    assert output == "load high"


@pytest.mark.asyncio
async def test_non_zero_exit_reports_failure_with_stderr(agentos_home):
    _write_script(
        agentos_home,
        "broken.py",
        "import sys; sys.stderr.write('boom'); sys.exit(3)",
    )

    ok, output = await run_job_script("broken.py", timeout=30)

    assert ok is False
    assert "exited with code 3" in output
    assert "boom" in output


@pytest.mark.asyncio
async def test_timeout_kills_the_script_and_reports(agentos_home):
    _write_script(agentos_home, "slow.py", "import time; time.sleep(30)")

    ok, output = await run_job_script("slow.py", timeout=0.5)

    assert ok is False
    assert "timed out" in output


@pytest.mark.asyncio
async def test_missing_script_is_reported_not_raised(agentos_home):
    ok, output = await run_job_script("nope.py", timeout=30)

    assert ok is False
    assert "not found" in output


@pytest.mark.asyncio
async def test_output_is_clipped(agentos_home):
    _write_script(agentos_home, "loud.py", "print('x' * 50_000)")

    ok, output = await run_job_script("loud.py", timeout=30)

    assert ok is True
    assert len(output) <= MAX_SCRIPT_OUTPUT_CHARS + 32
    assert output.endswith("[output truncated]")


@pytest.mark.asyncio
async def test_workdir_becomes_the_subprocess_cwd(agentos_home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_script(agentos_home, "cwd.py", "import os; print(os.getcwd())")

    ok, output = await run_job_script("cwd.py", timeout=30, workdir=str(elsewhere))

    assert ok is True
    assert output == str(elsewhere.resolve())


@pytest.mark.asyncio
async def test_missing_workdir_falls_back_to_the_script_directory(agentos_home):
    _write_script(agentos_home, "cwd.py", "import os; print(os.getcwd())")

    ok, output = await run_job_script("cwd.py", timeout=30, workdir="/nope/not/here")

    assert ok is True
    assert output == str((agentos_home / "scripts").resolve())


@pytest.mark.asyncio
async def test_gateway_token_is_withheld_from_the_script(agentos_home, monkeypatch):
    monkeypatch.setenv("AGENTOS_GATEWAY_TOKEN", "secret-token")
    _write_script(
        agentos_home,
        "env.py",
        "import os; print(os.environ.get('AGENTOS_GATEWAY_TOKEN', 'ABSENT'))",
    )

    ok, output = await run_job_script("env.py", timeout=30)

    assert ok is True
    assert output == "ABSENT"


# ── silence gate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("", False),
        ("   \n ", False),
        ("disk 91% full", True),
        ('{"wakeAgent": false}', False),
        ('all quiet\n{"wakeAgent": false}', False),
        ('{"wakeAgent": true}', True),
        ("{not json}", True),
        ("[1, 2, 3]", True),
    ],
)
def test_silence_gate(output, expected):
    assert has_actionable_output(output) is expected


# ── payload contract ────────────────────────────────────────────────────────


def test_normalize_contract_maps_script_kind_to_script_run_handler():
    handler_key, payload, target, session_key = normalize_contract(
        handler_key="script_run",
        payload=make_script_payload("watch.sh", "main", "/tmp"),
        session_target=SessionTarget.ISOLATED,
    )

    assert handler_key == "script_run"
    assert payload["kind"] == SCRIPT_KIND
    assert payload_script(payload) == "watch.sh"
    assert payload_workdir(payload) == "/tmp"
    assert target == SessionTarget.ISOLATED
    assert session_key == ""


def test_script_payload_text_falls_back_to_the_script_name():
    payload = make_script_payload("watch.sh")

    assert payload_text(payload, SessionTarget.ISOLATED) == "watch.sh"


def test_script_payload_requires_a_script():
    with pytest.raises(ValueError, match="script path"):
        normalize_contract(
            handler_key="script_run",
            payload={"kind": SCRIPT_KIND, "script": "", "agent_id": "main"},
            session_target=SessionTarget.ISOLATED,
        )


def test_script_payload_cannot_target_main():
    with pytest.raises(ValueError, match="sessionTarget='main'"):
        normalize_contract(
            handler_key="script_run",
            payload=make_script_payload("watch.sh"),
            session_target=SessionTarget.MAIN,
        )


def test_script_payload_survives_a_lenient_reload():
    """Persistence re-normalizes on load; a stored script job must round-trip."""
    handler_key, payload, _, _ = normalize_contract(
        handler_key="script_run",
        payload=make_script_payload("watch.sh"),
        session_target=SessionTarget.ISOLATED,
        strict=False,
    )

    assert handler_key == "script_run"
    assert payload_script(payload) == "watch.sh"


# ── the handler ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_delivers_stdout_verbatim(agentos_home):
    forwarded: list[dict] = []

    async def forwarder(**kwargs) -> None:
        forwarded.append(kwargs)

    _write_script(agentos_home, "watch.py", "print('memory at 93%')")
    job = _script_job("watch.py")
    job.origin_session_key = "webchat:abc"
    handler = make_script_run_handler(DeliveryChain(session_forwarder=forwarder))

    result = await handler(job)

    assert result.summary == "memory at 93%"
    assert result.delivery_status.startswith("skipped|ws:skipped|fwd:")
    assert forwarded[0]["text"] == "memory at 93%"


@pytest.mark.asyncio
async def test_handler_stays_silent_on_empty_stdout(agentos_home):
    forwarded: list[dict] = []

    async def forwarder(**kwargs) -> None:
        forwarded.append(kwargs)

    _write_script(agentos_home, "quiet.py", "pass")
    job = _script_job("quiet.py")
    job.origin_session_key = "webchat:abc"
    handler = make_script_run_handler(DeliveryChain(session_forwarder=forwarder))

    result = await handler(job)

    assert result.delivery_status == "skipped"
    assert forwarded == []


@pytest.mark.asyncio
async def test_handler_stays_silent_on_wake_gate(agentos_home):
    forwarded: list[dict] = []

    async def forwarder(**kwargs) -> None:
        forwarded.append(kwargs)

    _write_script(agentos_home, "gated.py", "print('{\"wakeAgent\": false}')")
    job = _script_job("gated.py")
    job.origin_session_key = "webchat:abc"
    handler = make_script_run_handler(DeliveryChain(session_forwarder=forwarder))

    result = await handler(job)

    assert result.delivery_status == "skipped"
    assert forwarded == []


@pytest.mark.asyncio
async def test_handler_delivers_the_error_and_fails_the_job(agentos_home):
    """A broken watchdog must not look like a quiet one."""
    forwarded: list[dict] = []

    async def forwarder(**kwargs) -> None:
        forwarded.append(kwargs)

    _write_script(agentos_home, "broken.py", "import sys; sys.exit(2)")
    job = _script_job("broken.py")
    job.origin_session_key = "webchat:abc"
    handler = make_script_run_handler(DeliveryChain(session_forwarder=forwarder))

    with pytest.raises(RuntimeError, match="exited with code 2"):
        await handler(job)

    assert len(forwarded) == 1
    assert "failed" in forwarded[0]["text"]


@pytest.mark.asyncio
async def test_handler_rejects_a_job_with_no_script(agentos_home):
    job = _script_job("")
    handler = make_script_run_handler(DeliveryChain())

    with pytest.raises(RuntimeError, match="no script"):
        await handler(job)


@pytest.mark.asyncio
async def test_handler_uses_the_job_timeout(agentos_home):
    _write_script(agentos_home, "slow.py", "import time; time.sleep(30)")
    job = _script_job("slow.py", timeout=0.5)
    job.delivery = DeliveryConfig(mode=DeliveryMode.NONE)
    handler = make_script_run_handler(DeliveryChain())

    with pytest.raises(RuntimeError, match="timed out after 0.5s"):
        await handler(job)


@pytest.mark.asyncio
async def test_handler_never_touches_a_turn_runner(agentos_home):
    """The point of a script job: no model, no tokens, no agent machinery."""
    _write_script(agentos_home, "watch.py", "print('ok')")
    job = _script_job("watch.py")
    handler = make_script_run_handler(DeliveryChain())

    result = await handler(job)

    assert result.session_key.startswith("cron:watchdog:run:")
    assert result.summary == "ok"


def test_the_handler_has_no_way_to_reach_a_provider():
    """A structural guarantee, not a behavioural one.

    The other handlers are wired to a turn runner and a task runtime. This one
    takes a delivery chain and nothing else, so no argument, config, or payload
    can route a script job into a model call.
    """
    params = inspect.signature(make_script_run_handler).parameters

    assert list(params) == ["delivery_chain"]


@pytest.mark.asyncio
async def test_relative_workdir_resolves_under_the_script_directory(agentos_home):
    """`--workdir data` means the script's own `data/`, not the gateway's CWD."""
    _write_script(agentos_home, "cwd.py", "import os; print(os.getcwd())")
    wanted = agentos_home / "scripts" / "data"
    wanted.mkdir()

    ok, output = await run_job_script("cwd.py", timeout=30, workdir="data")

    assert ok is True
    assert output == str(wanted.resolve())


@pytest.mark.asyncio
async def test_relative_workdir_is_not_resolved_against_the_process_cwd(
    agentos_home, tmp_path, monkeypatch
):
    """A same-named directory beside the gateway's CWD must not win.

    The scheduler runs wherever the gateway was started, which is routinely not
    the script's directory; resolving there ran the job somewhere the caller
    never named.
    """
    _write_script(agentos_home, "cwd.py", "import os; print(os.getcwd())")
    wanted = agentos_home / "scripts" / "data"
    wanted.mkdir()
    decoy_parent = tmp_path / "elsewhere"
    (decoy_parent / "data").mkdir(parents=True)
    monkeypatch.chdir(decoy_parent)

    ok, output = await run_job_script("cwd.py", timeout=30, workdir="data")

    assert ok is True
    assert output == str(wanted.resolve())


@pytest.mark.asyncio
async def test_dot_workdir_matches_the_documented_default(agentos_home):
    """`workdir="."` and an unset workdir name the same directory."""
    _write_script(agentos_home, "cwd.py", "import os; print(os.getcwd())")

    ok, output = await run_job_script("cwd.py", timeout=30, workdir=".")

    assert ok is True
    assert output == str((agentos_home / "scripts").resolve())


@pytest.mark.asyncio
async def test_missing_relative_workdir_falls_back_to_the_script_directory(agentos_home):
    _write_script(agentos_home, "cwd.py", "import os; print(os.getcwd())")

    ok, output = await run_job_script("cwd.py", timeout=30, workdir="no/such/dir")

    assert ok is True
    assert output == str((agentos_home / "scripts").resolve())
