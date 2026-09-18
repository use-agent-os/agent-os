"""``poolsdotfun-token-launcher`` must not let a boolean flag before the
subcommand swallow it (#2863).

``parse_args`` decided whether a bare ``--flag`` took a value purely by
whether the next token started with ``--``, so ``--json preflight`` read
"preflight" as ``--json``'s value instead of running the ``preflight``
subcommand. The sibling ``senior-unilp-manager`` skill has the identical
vendored ``parse_args`` and the identical bug (#2864, fixed the same way).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "poolsdotfun-token-launcher"
    / "scripts"
)


def _load(name: str):
    """Import a module from the skill's scripts dir without touching PATH."""
    entry = str(_SCRIPTS)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        return importlib.import_module(name)
    finally:
        if added:
            sys.path.remove(entry)


@pytest.fixture(scope="module")
def fmt():
    return _load("poolsfun.fmt")


@pytest.fixture(scope="module")
def pools_read():
    return _load("pools_read")


@pytest.fixture(scope="module")
def pools_write():
    return _load("pools_write")


# --- the issue's own repro ------------------------------------------------


def test_json_before_preflight_no_longer_prints_usage_instead_of_running(pools_read) -> None:
    """Repro 1: `pools_read.py --json preflight` must dispatch to `preflight`,
    not fall through to the "no command" usage-text branch."""
    args = pools_read.parse_args(["--json", "preflight"], bool_flags=pools_read._BOOL_FLAGS)
    assert args["json"] is True
    assert args["_"] == ["preflight"]
    command = args["_"][0] if args["_"] else None
    assert command == "preflight"
    assert command in pools_read.COMMANDS


def test_json_before_token_with_a_positional_arg_is_unaffected(pools_read) -> None:
    """Repro 2: `pools_read.py --json token 0x...` must not report
    "unknown command: 0x...127" -- the address must land in the positional
    list behind `token`, not get swallowed as part of subcommand
    resolution."""
    args = pools_read.parse_args(
        ["--json", "token", "0x1234567890123456789012345678901234567890"],
        bool_flags=pools_read._BOOL_FLAGS,
    )
    assert args["json"] is True
    assert args["_"] == ["token", "0x1234567890123456789012345678901234567890"]


# --- every declared switch, both scripts ----------------------------------


@pytest.mark.parametrize(
    ("module_name", "flag"),
    [
        ("pools_read", "json"),
        ("pools_read", "allow-fallback-tick"),
        ("pools_read", "no-extract"),
        ("pools_read", "debug"),
        ("pools_write", "broadcast"),
        ("pools_write", "pin-metadata"),
        ("pools_write", "allow-fallback-tick"),
        ("pools_write", "debug"),
    ],
)
def test_every_declared_bool_flag_survives_before_the_subcommand(
    request: pytest.FixtureRequest, module_name: str, flag: str
) -> None:
    module = request.getfixturevalue(module_name)
    args = module.parse_args([f"--{flag}", "some-subcommand"], bool_flags=module._BOOL_FLAGS)
    assert args[flag] is True
    assert args["_"] == ["some-subcommand"]


# --- boundaries: what this fix must NOT change -----------------------------


def test_value_taking_flag_before_the_subcommand_is_unaffected(pools_write) -> None:
    """Boundary: a real value-taking flag (e.g. --confirm, which is
    deliberately excluded from _BOOL_FLAGS because it takes <PLAN_HASH>)
    keeps consuming its value when placed before the subcommand."""
    args = pools_write.parse_args(
        ["--confirm", "abc123", "launch"], bool_flags=pools_write._BOOL_FLAGS
    )
    assert args["confirm"] == "abc123"
    assert args["_"] == ["launch"]


def test_flags_after_the_subcommand_are_unaffected(pools_read) -> None:
    """Boundary: the documented usage shape (subcommand first, flags after)
    is byte-for-byte unchanged."""
    args = pools_read.parse_args(["token", "0xabc", "--json"], bool_flags=pools_read._BOOL_FLAGS)
    assert args == {"_": ["token", "0xabc"], "json": True}


def test_bool_flags_defaults_to_empty_and_changes_nothing(fmt) -> None:
    """Boundary: every other caller of parse_args (none currently pass
    bool_flags outside main()) is byte-for-byte identical to before."""
    assert fmt.parse_args(["--json", "preflight"]) == {"json": "preflight", "_": []}


def test_bool_flags_parameter_is_checked_before_the_lookahead_heuristic(fmt) -> None:
    """Pins the fix's own shape in this copy of parse_args: a declared bool
    flag is resolved before the old next-token-shape guess ever runs."""
    src = (_SCRIPTS / "poolsfun" / "fmt.py").read_text(encoding="utf-8")
    assert "bool_flags" in src
    assert "if body in bool_flags:" in src
