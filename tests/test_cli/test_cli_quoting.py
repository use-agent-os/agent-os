"""Platform-aware quoting for the commands AgentOS prints as hints.

The hints are meant to be pasted straight back into the user's shell.
``shlex.quote`` is POSIX-only, so on Windows a path with a space came back
single-quoted — ``--config 'C:\\Program Files\\Agent OS\\custom.toml'`` — which
neither ``cmd.exe`` nor PowerShell parses as one argument. These tests pin both
platforms by driving ``os.name`` directly, so the Windows behaviour is
covered on every CI leg rather than only the Windows one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agentos import cli_quoting
from agentos.cli_quoting import config_cli_arg, quote_cli_arg

WINDOWS_PATH = r"C:\Program Files\Agent OS\custom.toml"


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_quoting, "_is_windows_shell", lambda: True)


@pytest.fixture
def on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_quoting, "_is_windows_shell", lambda: False)


def test_windows_path_with_spaces_uses_double_quotes(on_windows: None) -> None:
    assert quote_cli_arg(WINDOWS_PATH) == '"C:\\Program Files\\Agent OS\\custom.toml"'


def test_windows_path_without_spaces_is_left_bare(on_windows: None) -> None:
    assert quote_cli_arg(r"C:\agentos\config.toml") == r"C:\agentos\config.toml"


def test_windows_backslashes_are_not_escaped(on_windows: None) -> None:
    """A quoted Windows path must keep single backslashes — they are separators."""
    quoted = quote_cli_arg(WINDOWS_PATH)

    assert "\\\\" not in quoted
    assert quoted.strip('"') == WINDOWS_PATH


def test_windows_never_emits_posix_single_quotes(on_windows: None) -> None:
    assert "'" not in quote_cli_arg(WINDOWS_PATH)


@pytest.mark.parametrize(
    "value",
    [
        r"C:\opt\a&b\config.toml",
        r"C:\opt\a;b\config.toml",
        r"C:\opt\a(b)\config.toml",
        r"C:\opt\a%b%\config.toml",
    ],
)
def test_windows_shell_metacharacters_are_quoted(value: str, on_windows: None) -> None:
    assert quote_cli_arg(value) == f'"{value}"'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (r"C:\home\Jo$hn\config.toml", r'"C:\home\Jo`$hn\config.toml"'),
        ("C:\\opt\\a`b\\config.toml", '"C:\\opt\\a``b\\config.toml"'),
        ("C:\\home\\Jo$hn\\cfg`n.toml", '"C:\\home\\Jo`$hn\\cfg``n.toml"'),
        ('C:\\a$b`c"d.toml', '"C:\\a`$b``c""d.toml"'),
    ],
)
def test_windows_powershell_expansions_are_escaped(
    value: str, expected: str, on_windows: None
) -> None:
    """Inside double quotes PowerShell expands ``$name`` and backtick escapes;
    a bare ``"C:\\home\\Jo$hn\\cfg`n.toml"`` pasted there opened
    ``C:\\home\\Jo\\cfg<newline>.toml`` (#2978). Backtick-escaping both is
    what PowerShell reads back as the original characters (verified in
    Windows PowerShell 5.1)."""
    assert quote_cli_arg(value) == expected


def test_windows_backtick_is_escaped_before_dollar(on_windows: None) -> None:
    """Escaping ``$`` inserts a backtick; escaping backticks afterwards would
    double that one too and hand PowerShell ``` ``$ ```, a literal backtick."""
    assert quote_cli_arg("$") == '"`$"'
    assert quote_cli_arg("`") == '"``"'
    assert quote_cli_arg("`$") == '"```$"'


def test_windows_embedded_double_quote_is_doubled(on_windows: None) -> None:
    assert quote_cli_arg('a"b') == '"a""b"'


def test_windows_empty_value_is_quoted(on_windows: None) -> None:
    assert quote_cli_arg("") == '""'


def test_posix_still_uses_shlex_quoting(on_posix: None) -> None:
    assert quote_cli_arg("/srv/agent os/custom.toml") == "'/srv/agent os/custom.toml'"


def test_posix_plain_value_is_left_bare(on_posix: None) -> None:
    assert quote_cli_arg("/srv/agentos/custom.toml") == "/srv/agentos/custom.toml"


def test_config_cli_arg_renders_a_leading_space_and_flag(on_windows: None) -> None:
    assert config_cli_arg(WINDOWS_PATH) == ' --config "C:\\Program Files\\Agent OS\\custom.toml"'


def test_config_cli_arg_accepts_a_path_object(on_posix: None) -> None:
    # ``str(Path(...))`` uses the native separator, so this compares against the
    # same rendering rather than hard-coding a POSIX path.
    path = Path("data") / "a b.toml"

    assert config_cli_arg(path) == config_cli_arg(str(path))
    assert config_cli_arg(path) == f" --config {quote_cli_arg(str(path))}"
    assert config_cli_arg(path).endswith("'")  # the space forced POSIX quoting


@pytest.mark.parametrize("empty", [None, ""])
def test_config_cli_arg_is_empty_without_a_path(empty: str | None) -> None:
    assert config_cli_arg(empty) == ""


# ── The sweep: no hint site may reach for shlex.quote again ─────────────────

_HINT_MODULES = (
    "cli/doctor_cmd.py",
    "cli/gateway_cmd.py",
    "cli/onboard_cmd.py",
    "health/evaluator.py",
    "health/recovery_commands.py",
    "onboarding/flow.py",
    "onboarding/next_steps.py",
)


@pytest.mark.parametrize("relative", _HINT_MODULES)
def test_hint_modules_do_not_use_shlex_quote(relative: str) -> None:
    source = (Path(cli_quoting.__file__).parent / relative).read_text(encoding="utf-8")

    assert "shlex.quote" not in source, (
        f"{relative} builds a copy-pasteable hint — use "
        "agentos.cli_quoting.quote_cli_arg so Windows gets double quotes"
    )


@pytest.mark.parametrize("relative", _HINT_MODULES)
def test_hint_modules_do_not_interpolate_a_bare_config_path(relative: str) -> None:
    """``--config {path}`` with no quoting helper is the same bug, unquoted."""
    source = (Path(cli_quoting.__file__).parent / relative).read_text(encoding="utf-8")

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.JoinedStr):
            continue
        parts = [
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        assert not any(part.rstrip().endswith("--config") for part in parts), (
            f"{relative} interpolates a path straight after --config; "
            "use agentos.cli_quoting.config_cli_arg"
        )


# ── The hint sites themselves, driven on both platforms ─────────────────────


def test_onboarding_next_step_hint_is_pasteable_on_windows(on_windows: None) -> None:
    from agentos.onboarding import next_steps

    assert next_steps._config_cli_arg(WINDOWS_PATH) == (
        ' --config "C:\\Program Files\\Agent OS\\custom.toml"'
    )


def test_onboarding_flow_hint_is_pasteable_on_windows(on_windows: None) -> None:
    from agentos.onboarding import flow

    assert flow._config_cli_arg(WINDOWS_PATH) == (
        ' --config "C:\\Program Files\\Agent OS\\custom.toml"'
    )


def test_onboard_cmd_hint_is_pasteable_on_windows(on_windows: None) -> None:
    from agentos.cli import onboard_cmd

    assert onboard_cmd._config_cli_arg(Path(WINDOWS_PATH)) == (
        ' --config "C:\\Program Files\\Agent OS\\custom.toml"'
    )


def test_doctor_onboard_commands_are_pasteable_on_windows(on_windows: None) -> None:
    from agentos.cli import doctor_cmd

    assert doctor_cmd._onboard_if_needed_command(WINDOWS_PATH) == (
        'agentos onboard --if-needed --config "C:\\Program Files\\Agent OS\\custom.toml"'
    )


def test_recovery_command_is_pasteable_on_windows(on_windows: None) -> None:
    from agentos.health.recovery_commands import command_with_config

    assert command_with_config("agentos gateway start", WINDOWS_PATH) == (
        'agentos gateway start --config "C:\\Program Files\\Agent OS\\custom.toml"'
    )


def test_recovery_command_keeps_posix_quoting_on_posix(on_posix: None) -> None:
    from agentos.health.recovery_commands import command_with_config

    assert command_with_config("agentos gateway start", "/srv/agent os/c.toml") == (
        "agentos gateway start --config '/srv/agent os/c.toml'"
    )


def test_platform_detection_follows_os_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures patch a seam — pin that the seam reads the real platform."""
    monkeypatch.setattr(cli_quoting.os, "name", "nt")
    assert cli_quoting._is_windows_shell() is True

    monkeypatch.setattr(cli_quoting.os, "name", "posix")
    assert cli_quoting._is_windows_shell() is False
