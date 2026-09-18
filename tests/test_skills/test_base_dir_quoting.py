"""``{baseDir}`` survives an install path with a space in it.

``skill_python`` already double-quotes an interpreter path that carries
whitespace, and bundled skills live under the same install prefix
(``%LOCALAPPDATA%\\agentos`` on Windows, under a user name that may well have a
space). The ``{baseDir}/scripts/...`` word right after it was expanded raw, so
the shell split it and the script was not found. Command text now gets the
word quoted; file references in prose are left alone for ``read_file``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentos.skills.loader import SkillLoader
from agentos.skills.resources import expand_skill_placeholders
from agentos.tools.builtin import skill_tools as skill_tools_module
from agentos.tools.registry import get_default_registry

SPACED = "/opt/Jane Doe/skills/deck"
PY = "/venv/bin/python"


def _expand(text: str, base_dir: str = SPACED) -> str:
    return expand_skill_placeholders(text, base_dir, python=PY)


def test_fenced_command_quotes_the_script_path() -> None:
    body = "```bash\n{python} {baseDir}/scripts/run.py --check\n```\n"

    assert _expand(body) == (f'```bash\n{PY} "{SPACED}/scripts/run.py" --check\n```\n')


def test_every_path_word_on_a_command_line_is_quoted() -> None:
    body = "```\ncp {baseDir}/scripts/a.py {baseDir}/scripts/b.py ~/.agentos/scripts/\n```"

    assert _expand(body) == (
        f'```\ncp "{SPACED}/scripts/a.py" "{SPACED}/scripts/b.py" ~/.agentos/scripts/\n```'
    )


def test_inline_command_span_is_quoted_even_after_an_apostrophe() -> None:
    body = "Run the skill's `{python} {baseDir}/scripts/run.py deck.pptx` now."

    assert _expand(body) == f'Run the skill\'s `{PY} "{SPACED}/scripts/run.py" deck.pptx` now.'


def test_file_references_in_prose_are_left_as_paths() -> None:
    body = (
        "Architecture: `{baseDir}/assets/v4-reference.md`.\n"
        "Scripts live under {baseDir}/scripts.\n"
        "```\n{python} {baseDir}/scripts/run.py\n```\n"
        "Then read {baseDir}/references/notes.md.\n"
    )

    assert _expand(body) == (
        f"Architecture: `{SPACED}/assets/v4-reference.md`.\n"
        f"Scripts live under {SPACED}/scripts.\n"
        f'```\n{PY} "{SPACED}/scripts/run.py"\n```\n'
        f"Then read {SPACED}/references/notes.md.\n"
    )


def test_a_word_already_inside_quotes_is_not_quoted_twice() -> None:
    body = '```bash\nS="{baseDir}/scripts"\npython3 "$S"/lp_read.py\n```'

    assert _expand(body) == f'```bash\nS="{SPACED}/scripts"\npython3 "$S"/lp_read.py\n```'


def test_a_path_without_whitespace_is_expanded_as_before() -> None:
    body = "```\n{python} {baseDir}/scripts/run.py\n```\nRun `{python} {baseDir}/x.py`."

    assert _expand(body, "/skills/deck") == (
        f"```\n{PY} /skills/deck/scripts/run.py\n```\nRun `{PY} /skills/deck/x.py`."
    )


def test_the_expanded_command_runs_from_a_spaced_directory(tmp_path: Path) -> None:
    skill_dir = tmp_path / "Jane Doe" / "deck"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text(
        "import sys\nprint('ran', sys.argv[1:])\n", encoding="utf-8"
    )
    body = "```\n{python} {baseDir}/scripts/run.py --check\n```"

    command = expand_skill_placeholders(body, str(skill_dir)).splitlines()[1]
    completed = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=60, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ran ['--check']"


@pytest.fixture()
def spaced_skill(tmp_path: Path) -> Iterator[Path]:
    workspace = tmp_path / "Jane Doe" / "workspace"
    skill_dir = workspace / "deck"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deck\ndescription: Deck helper\n---\n"
        "```bash\n{python} {baseDir}/scripts/run.py --check\n```\n",
        encoding="utf-8",
    )
    loader = SkillLoader(
        bundled_dir=tmp_path / "bundled",
        workspace_dir=workspace,
        managed_dir=tmp_path / "managed",
        personal_agents_dir=tmp_path / "personal",
        project_agents_dir=tmp_path / "project",
        snapshot_path=tmp_path / "skills.snapshot.json",
    )
    previous = skill_tools_module._loader
    skill_tools_module.create_skill_tools(loader)
    try:
        yield skill_dir.resolve()
    finally:
        skill_tools_module._loader = previous


async def test_skill_view_hands_the_model_a_quoted_script_path(spaced_skill: Path) -> None:
    registered = get_default_registry().get("skill_view")
    assert registered is not None

    result = await registered.handler(name="deck")

    assert f'"{spaced_skill}/scripts/run.py" --check' in result
