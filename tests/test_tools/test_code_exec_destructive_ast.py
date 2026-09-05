"""Tests for AST-based detection of destructive operations in code_exec (Issue #848).

Verifies that static regex evasion techniques (getattr, string concatenation,
__import__, importlib, exec/eval, wildcard imports, and import aliasing)
are accurately caught without false positives on benign code.
"""

from __future__ import annotations

import pytest

from agentos.tools.builtin.code_exec import _check_code_destructive


@pytest.mark.parametrize(
    ("code", "expected_keyword"),
    [
        # Concatenated strings in getattr
        ('import os; getattr(os, "rem" + "ove")("/tmp/x")', "remove"),
        ('import os; getattr(os, "un" + "link")("/tmp/x")', "unlink"),
        ('import os; getattr(os, "rm" + "dir")("/tmp/x")', "rmdir"),
        ('import shutil; getattr(shutil, "rm" + "tree")("/tmp/x")', "rmtree"),
        # f-strings in getattr
        ('import os; getattr(os, f"{\'rem\'}ove")("/tmp/x")', "remove"),
        # Dynamic __import__
        ('__import__("os").remove("/tmp/x")', "remove"),
        ('__import__("os").unlink("/tmp/x")', "unlink"),
        ('__import__("shutil").rmtree("/tmp/x")', "rmtree"),
        # Dynamic importlib.import_module
        ('import importlib; importlib.import_module("os").remove("/tmp/x")', "remove"),
        ('import importlib; importlib.import_module("shutil").rmtree("/tmp/x")', "rmtree"),
        # exec / eval with nested destructive code
        ("exec(\"os.remove('/tmp/x')\")", "remove"),
        ("eval(\"os.remove('/tmp/x')\")", "remove"),
        ('exec(\'getattr(os, "rem" + "ove")("/tmp/x")\')', "remove"),
        # Wildcard imports
        ("from os import *; remove('/tmp/x')", "remove"),
        ("from os import *; unlink('/tmp/x')", "unlink"),
        ("from shutil import *; rmtree('/tmp/x')", "rmtree"),
        # Aliased imports
        ("from os import remove as delete_file; delete_file('/tmp/x')", "remove"),
        ("from shutil import rmtree as nukedir; nukedir('/tmp/x')", "rmtree"),
        ("import os as my_os; my_os.remove('/tmp/x')", "remove"),
        ("import shutil as s; s.rmtree('/tmp/x')", "rmtree"),
        # Path methods via getattr
        ('from pathlib import Path; getattr(Path("/tmp/x"), "unlink")()', "unlink"),
        ('from pathlib import Path; getattr(Path("/tmp/x"), "rmdir")()', "rmdir"),
        # Subprocess list invocation of rm
        ('import subprocess; subprocess.run(["rm", "-rf", "/tmp/x"])', "subprocess invoking rm"),
        ('import subprocess as sp; sp.call(["rmdir", "/tmp/x"])', "subprocess invoking rm"),
    ],
)
def test_destructive_ast_evasions_detected(code: str, expected_keyword: str) -> None:
    warning = _check_code_destructive(code)
    assert warning is not None, f"Expected warning for: {code}"
    assert "destructive Python operation detected:" in warning
    assert expected_keyword.lower() in warning.lower()


@pytest.mark.parametrize(
    "code",
    [
        # Standard list.remove should NOT trigger
        "items = [1, 2, 3]\nitems.remove(2)",
        # Set.remove should NOT trigger
        "s = {1, 2, 3}\ns.remove(2)",
        # Benign math/sys/os calls
        "import math\nx = math.sqrt(16)",
        "import os\ncwd = os.getcwd()",
        "import os\nfiles = os.listdir('.')",
        "import shutil\nshutil.copy('a.txt', 'b.txt')",
        "from pathlib import Path\np = Path('a.txt').read_text()",
        # Benign getattr
        "import os\npath_fn = getattr(os, 'getcwd')",
        "getattr(dict, 'get')",
    ],
)
def test_benign_code_does_not_trigger_warning(code: str) -> None:
    warning = _check_code_destructive(code)
    assert warning is None, f"Unexpected warning for safe code: {warning}"


def test_syntax_error_code_falls_back_to_regex() -> None:
    # Syntax error with os.remove() still caught by regex fallback
    bad_syntax_destructive = "os.remove( unclosed string"
    warning = _check_code_destructive(bad_syntax_destructive)
    assert warning is not None
    assert "os.remove()" in warning

    # Benign syntax error returns None
    bad_syntax_benign = "def foo( unclosed"
    assert _check_code_destructive(bad_syntax_benign) is None


@pytest.mark.parametrize(
    ("code", "expected_keyword"),
    [
        # `compile()` is a code carrier: the source must be scanned like the
        # string literal `exec("…")` already is. Each source below is split so
        # the flat regex pass cannot see the destructive spelling.
        ("exec(compile('os.re' + 'move(\"/etc/x\")', '', 'exec'))", "remove"),
        ("eval(compile('os.re' + 'move(\"/etc/x\")', '', 'eval'))", "remove"),
        ("exec(compile('shutil.rm' + 'tree(\"/etc/x\")', '', 'exec'))", "rmtree"),
        ("exec(compile('os.sys' + 'tem(\"rm -rf /etc/x\")', '', 'exec'))", "os.system with rm"),
        (
            "exec(compile(source='os.re' + 'move(\"/etc/x\")', filename='', mode='exec'))",
            "remove",
        ),
        ("c = compile\nexec(c('os.re' + 'move(\"/etc/x\")', '', 'exec'))", "remove"),
        # `__import__` reached indirectly — the importer itself is fetched
        # dynamically, so the callee is an ast.Call, not a Name/Attribute.
        ("getattr(__builtins__, '__import__')('os').remove('/etc/x')", "remove"),
        ("import builtins; getattr(builtins, '__import__')('shutil').rmtree('/etc/x')", "rmtree"),
        ("import builtins; builtins.__import__('os').unlink('/etc/x')", "unlink"),
        ("getattr(__builtins__, '__imp' + 'ort__')('os').remove('/etc/x')", "remove"),
        # Shell-exec attrs via getattr: `system`, `popen`, and the subprocess
        # entrypoints are not in _ALL_DESTRUCTIVE_NAMES, so the getattr branch
        # skipped them entirely.
        ("import os; getattr(os, 'system')('rm -rf /etc/x')", "os.system with rm"),
        ("import os; getattr(os, 'sys' + 'tem')('rm -rf /etc/x')", "os.system with rm"),
        ("import os; getattr(os, 'popen')('rm -rf /etc/x')", "os.popen with rm"),
        (
            "import subprocess; getattr(subprocess, 'run')(['rm', '-rf', '/etc/x'])",
            "subprocess invoking rm",
        ),
        (
            "import subprocess; getattr(subprocess, 'Popen')('rm -rf /etc/x')",
            "subprocess invoking rm",
        ),
        # Combinations of both indirections.
        ("getattr(__import__('os'), 'system')('rm -rf /etc/x')", "os.system with rm"),
        (
            "exec(compile('getattr(os, \"sys\" + \"tem\")(\"rm -rf /etc/x\")', '', 'exec'))",
            "os.system with rm",
        ),
    ],
)
def test_indirect_destructive_calls_detected(code: str, expected_keyword: str) -> None:
    """Indirection must not bypass the gate (residual bypasses of #848/#890)."""
    warning = _check_code_destructive(code)
    assert warning is not None, f"Expected warning for: {code}"
    assert "destructive Python operation detected:" in warning
    assert expected_keyword.lower() in warning.lower()


@pytest.mark.parametrize(
    "code",
    [
        # compile() of harmless source stays allowed.
        "exec(compile('print(1)', '', 'exec'))",
        "exec(compile('x = sum([1, 2, 3])', '', 'exec'))",
        # getattr of a non-destructive attr stays allowed, including the
        # shell-exec attrs when the command itself is harmless.
        "import os; getattr(os, 'getcwd')()",
        "import os; getattr(os, 'system')('echo hi')",
        "import subprocess; getattr(subprocess, 'run')(['ls', '-la'])",
        "getattr(__builtins__, 'len')('abc')",
        # An indirect import that never reaches a destructive call.
        "getattr(__builtins__, '__import__')('json').dumps({'a': 1})",
        "import builtins; builtins.__import__('math').sqrt(16)",
    ],
)
def test_indirect_benign_code_does_not_trigger_warning(code: str) -> None:
    warning = _check_code_destructive(code)
    assert warning is None, f"Unexpected warning for safe code: {warning}"
