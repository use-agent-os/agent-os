"""WAL-03: AgentOS's own Python launches never import from the working directory.

The in-place restart re-exec'd ``python -m agentos.cli.main``; ``-m`` puts the
working directory at ``sys.path[0]``, ahead of site-packages, and the gateway's
working directory (``$HOME`` for the service units, the launch directory for
``agentos gateway run``) is writable by the agent's shell. A file named like a
module the gateway imports -- ``eth_account.py`` on the signing path -- ran
inside the process that holds the vault keys. Every launcher now passes
``-P`` (``agentos.safe_python``), and hands the child the ``PYTHON*``
variables this process was launched with rather than any a ``.env`` added.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentos import safe_python
from agentos.gateway.restart import default_reexec_argv

PLANT = "wal03_plant_module"


def _plant(directory: Path) -> None:
    (directory / f"{PLANT}.py").write_text("print('PLANTED')\n", encoding="utf-8")


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv
        argv, cwd=str(cwd), capture_output=True, text=True, timeout=60, check=False
    )


def test_reexec_argv_keeps_the_working_directory_off_sys_path() -> None:
    argv = default_reexec_argv()
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-P", "-m", "agentos.cli.main"]


@pytest.mark.parametrize("mode", ["-m", "-c"])
def test_a_module_planted_in_the_working_directory_is_not_imported(
    tmp_path: Path, mode: str
) -> None:
    _plant(tmp_path)
    args = [PLANT] if mode == "-m" else [f"import {PLANT}"]
    # Control: the old argv shape imports the plant ...
    unsafe = _run([sys.executable, mode, *args], tmp_path)
    assert "PLANTED" in unsafe.stdout
    # ... the launcher AgentOS uses now does not.
    safe = _run(safe_python.python_argv(mode, *args), tmp_path)
    assert "PLANTED" not in safe.stdout
    assert safe.returncode != 0 and PLANT in safe.stderr


def test_a_shadowed_dependency_resolves_to_the_installed_one(tmp_path: Path) -> None:
    """The audit's plant: a cwd ``structlog.py`` next to the real dependency."""
    (tmp_path / "structlog.py").write_text("raise SystemExit('PLANTED')\n", encoding="utf-8")
    probe = "import structlog; print(structlog.__file__)"
    result = _run(safe_python.python_argv("-c", probe), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "PLANTED" not in result.stdout + result.stderr
    assert Path(result.stdout.strip()).parent != tmp_path


def test_launch_env_drops_interpreter_variables_added_after_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(safe_python, "_LAUNCH_PYTHON_ENV", {"PYTHONIOENCODING": "utf-8"})
    env = safe_python.launch_env(
        {"PATH": "/bin", "PYTHONPATH": "/tmp/plant", "pythonstartup": "/tmp/x.py"}
    )
    assert "PYTHONPATH" not in env and "pythonstartup" not in env
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PATH"] == "/bin"


def test_the_restarter_hands_the_new_image_the_launch_environment() -> None:
    import inspect

    from agentos.gateway import restart

    # The re-exec env is built through launch_env, not dict(os.environ).
    source = inspect.getsource(restart.GatewayRestarter)
    assert "launch_env(os.environ)" in source
    assert "env = dict(os.environ)" not in source


def test_every_agentos_python_launcher_passes_the_safe_path_flag() -> None:
    from agentos.cli.gateway_service import resolve_agentos_program
    from agentos.gateway import rpc_updates

    program = resolve_agentos_program(argv0="", which=lambda _: None, executable="/py")
    assert program[:2] == ["/py", "-P"]
    command = rpc_updates.agentos_command()
    if command[0] == sys.executable:
        assert command[1] == "-P"
