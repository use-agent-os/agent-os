"""``_check_code_destructive`` sees through shell wrappers and PowerShell flags (#2096).

Two holes let a delete command through without approval. A PowerShell flag
that takes a value (``-ExecutionPolicy Bypass``) was skipped but its value was
read as the command, so ``Bypass`` was inspected instead of ``Remove-Item``.
And the Unix shells (``bash -c``, ``sh -c``, ...) were not recognised as
wrappers at all, so whatever they carried was never looked at.
"""

from __future__ import annotations

import time

import pytest

from agentos.tools.builtin.code_exec import _check_code_destructive, _powershell_flag_takes_value


def _argv(*args: str) -> str:
    return f"import subprocess; subprocess.run({list(args)!r})"


@pytest.mark.parametrize(
    ("code", "expected_keyword"),
    [
        # PowerShell flags that take a value
        (
            _argv("powershell", "-ExecutionPolicy", "Bypass", "-c", "Remove-Item", "C:\\d"),
            "subprocess",
        ),
        (
            _argv("pwsh", "-ep", "bypass", "-NoProfile", "-Command", "Remove-Item", "C:\\d"),
            "subprocess",
        ),
        (
            _argv("powershell.exe", "-exec", "bypass", "-w", "hidden", "-c", "rm", "-r", "C:\\d"),
            "subprocess",
        ),
        (
            _argv(
                "powershell",
                "-ConfigurationName",
                "x",
                "-WindowStyle",
                "Hidden",
                "-Command",
                "rd",
                "/s",
                "C:\\d",
            ),
            "subprocess",
        ),
        (
            _argv(
                "powershell", "-ExecutionPolicy", "Bypass", "-Command", "Remove-Item -Recurse C:\\d"
            ),
            "subprocess",
        ),
        (
            r'import os; os.system("powershell -ExecutionPolicy Bypass -c Remove-Item C:\\data")',
            "os.system",
        ),
        (r'import os; os.popen("pwsh -ep bypass -Command \"Remove-Item C:\\data\"")', "os.popen"),
        (
            r'import subprocess; subprocess.run("pwsh -ep Bypass -c rm -r C:\\data", shell=True)',
            "subprocess",
        ),
        # Unix shell wrappers
        (_argv("bash", "-c", "rm -rf /data"), "subprocess"),
        (_argv("sh", "-c", "rm -rf /data"), "subprocess"),
        (_argv("zsh", "-c", "rm -rf /data"), "subprocess"),
        (_argv("dash", "-c", "rm -rf /data"), "subprocess"),
        (_argv("/bin/bash", "-lc", "rm -rf /data"), "subprocess"),
        (_argv("bash", "-o", "pipefail", "-c", "rm -rf /data"), "subprocess"),
        (_argv("bash", "-c", "cd /data && rm -rf *"), "subprocess"),
        (_argv("sudo", "bash", "-c", "rm -rf /data"), "subprocess"),
        (_argv("bash", "-c", "sudo rm -rf /data"), "subprocess"),
        ("import os; os.system(\"sh -c 'rm -rf /data'\")", "os.system"),
        ('import os; os.system("bash -c \\"rm -rf /data\\"")', "os.system"),
        ('import os; os.system("bash -lc rm\\ -rf\\ /data")', "os.system"),
        ("import os; os.system(\"/bin/sh -c 'rm -rf /data'\")", "os.system"),
        ("import subprocess; subprocess.Popen(\"sh -c 'rm -rf /data'\", shell=True)", "subprocess"),
        ('import os; getattr(os, "system")("bash -c \'rm -rf /data\'")', "os.system"),
        # Newline-separated scripts, long options, and the other shells
        (_argv("bash", "-c", "set -e\ncd build\nrm -rf *"), "subprocess"),
        (
            'import subprocess; subprocess.run(["bash", "-c", """\nset -e\nrm -rf /data\n"""])',
            "subprocess",
        ),
        (_argv("bash", "--noprofile", "--norc", "-c", "rm -rf /data"), "subprocess"),
        ("import os; os.system(\"bash --noprofile --norc -c 'rm -rf /data'\")", "os.system"),
        ("import os; os.system(\"bash --rcfile ~/.rc -c 'rm -rf /data'\")", "os.system"),
        (_argv("fish", "-c", "rm -rf /data"), "subprocess"),
        (_argv("csh", "-c", "rm -rf /data"), "subprocess"),
        (_argv("tcsh", "-c", "rm -rf /data"), "subprocess"),
        ("import os; os.system(\"csh -c 'rm -rf /data'\")", "os.system"),
    ],
)
def test_wrapped_delete_commands_are_detected(code: str, expected_keyword: str) -> None:
    warning = _check_code_destructive(code)
    assert warning is not None, f"Expected warning for: {code}"
    assert expected_keyword.lower() in warning.lower()


@pytest.mark.parametrize(
    "code",
    [
        _argv("bash", "-c", "ls -la /data"),
        _argv("sh", "script.sh"),
        _argv("bash", "-c", "echo rd"),
        _argv("ssh", "host", "rm", "-rf", "/data"),
        _argv("fish", "-c", "ls"),
        _argv("powershell", "-ExecutionPolicy", "Bypass", "-c", "Get-ChildItem"),
        _argv("pwsh", "-NoProfile", "-Command", "Get-Item", "rd"),
        'import os; os.system("powershell -ExecutionPolicy Bypass -c Get-ChildItem C:\\\\data")',
        "import os; os.system(\"bash -c 'ls /data'\")",
        'import os; os.system("dashboard --rm-cache")',
        'import os; os.system("bash-completion rm")',
    ],
)
def test_wrapped_benign_commands_do_not_trigger(code: str) -> None:
    warning = _check_code_destructive(code)
    assert warning is None, f"Unexpected warning for: {code}"


@pytest.mark.parametrize("wrapper", ["bash", "powershell", "sudo", "nice", "xargs"])
def test_a_long_run_of_flags_is_checked_in_linear_time(wrapper: str) -> None:
    # Every ``-x`` token used to be readable both as a flag and as the value of
    # the previous flag, so a long flag run with no delete command behind it
    # backtracked exponentially — inside the event loop.
    flags = " ".join(["-a"] * 400)
    code = f'import os; os.system("{wrapper} {flags} x")'

    started = time.perf_counter()
    assert _check_code_destructive(code) is None
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("flag", "takes_value"),
    [
        ("-ExecutionPolicy", True),
        ("-executionpolicy", True),
        ("-exec", True),
        ("-ep", True),
        ("-ex", True),
        ("-File", True),
        ("-f", True),
        ("-WindowStyle", True),
        ("-w", True),
        ("-EncodedCommand", True),
        ("-e", True),
        ("-Command", False),
        ("-c", False),
        ("-NoProfile", False),
        ("-nop", False),
        ("-NonInteractive", False),
        ("-no", False),  # ambiguous prefix: NoExit / NoLogo / NoProfile / ...
        ("-", False),
        ("-bogus", False),
    ],
)
def test_powershell_flag_value_resolution(flag: str, takes_value: bool) -> None:
    assert _powershell_flag_takes_value(flag) is takes_value
