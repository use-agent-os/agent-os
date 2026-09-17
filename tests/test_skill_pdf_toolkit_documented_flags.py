"""pdf-toolkit ``SKILL.md`` only advertises flags its scripts actually accept.

``SKILL.md`` used to tell the agent to "strip signatures explicitly with
``--clear-signatures``"; ``form_fill.py`` never defined that flag, so an agent
following the instruction hit ``error: unrecognized arguments`` (#2103). The
same drift retired ``extract.py``'s advertised ``--tables-strategy`` earlier.
Every ``--flag`` the skill mentions is checked against the union of options
the four scripts' parsers define.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "agentos" / "skills" / "bundled" / "pdf-toolkit"
SCRIPTS = SKILL_DIR / "scripts"
SCRIPT_NAMES = ("extract.py", "form_fill.py", "merge.py", "split.py")

# A ``--flag`` wherever it appears: backticked in prose, bare in a fenced
# example, or in a table cell. ``---`` rules and ``--`` dashes do not match
# because a letter must follow the two hyphens.
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)\b")


class _ParserCapturedError(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser


def _load_script(name: str, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location(f"pdf_toolkit_{name[:-3]}", SCRIPTS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass defined in the script resolves its annotations through
    # ``sys.modules[__module__]``, so the module must be registered first.
    monkeypatch.setitem(sys.modules, spec.name, module)
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _accepted_flags(name: str, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Every ``--option`` the script's parser defines, without running it."""
    module = _load_script(name, monkeypatch)

    def _capture(self: argparse.ArgumentParser, *_args: object, **_kwargs: object) -> None:
        raise _ParserCapturedError(self)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _capture)
    try:
        module._parse_args()
    except _ParserCapturedError as captured:
        parser = captured.parser
    else:  # pragma: no cover - the script's parser must go through parse_args
        raise AssertionError(f"{name}: _parse_args() did not call parse_args()")
    return {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--")
    }


def _documented_flags() -> set[str]:
    return set(_FLAG.findall((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")))


def test_every_flag_skill_md_mentions_is_accepted_by_a_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted: set[str] = set()
    for name in SCRIPT_NAMES:
        accepted |= _accepted_flags(name, monkeypatch)

    documented = _documented_flags()
    assert documented, "SKILL.md documents no flags at all; the regex has drifted"
    assert documented <= accepted, (
        f"SKILL.md advertises flags no script accepts: {sorted(documented - accepted)}"
    )


def test_clear_signatures_is_not_advertised() -> None:
    # The specific flag from #2103, pinned by name so the failure reads clearly.
    assert "--clear-signatures" not in (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
