"""A colon in an unquoted description must not cost a skill its whole frontmatter.

`description: Trade tokens. Chains: Base, Solana.` is invalid YAML. The loader used
to answer with an empty frontmatter and skip the skill without a word, while the
installer reported success and the catalog card, which reads the field with a regex,
looked fine. The user saw a skill that "installs" and never appears.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from agentos.skills import loader as loader_module
from agentos.skills.hub.installer import SkillInstaller
from agentos.skills.hub.source import SkillBundle, SkillMeta
from agentos.skills.loader import SkillLoader, _parse_frontmatter, parse_frontmatter_yaml


def _doc(frontmatter: str) -> str:
    return f"---\n{frontmatter}\n---\n\n# Body\n"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            "description: Trade tokens. Chains: Base, Solana.",
            "Trade tokens. Chains: Base, Solana.",
            id="colon-mid-sentence",
        ),
        pytest.param("description: Chains: Base", "Chains: Base", id="colon-at-start"),
        pytest.param("description: ends with colon: ", "ends with colon:", id="trailing-space"),
        pytest.param(
            'description: Say "hi": then a\\b path',
            'Say "hi": then a\\b path',
            id="quotes-and-backslash",
        ),
        pytest.param("description: 价格: 中文 📺", "价格: 中文 📺", id="unicode"),
    ],
)
def test_unquoted_description_with_a_colon_keeps_the_skill(line: str, expected: str) -> None:
    frontmatter, body = _parse_frontmatter(_doc(f"name: demo\n{line}\nemoji: x"))

    assert frontmatter == {"name": "demo", "description": expected, "emoji": "x"}
    assert body == "# Body"


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        pytest.param('description: "a: b"', "a: b", id="double-quoted"),
        pytest.param("description: 'a: b'", "a: b", id="single-quoted"),
        pytest.param("description: |\n  a: b\n  c", "a: b\nc", id="block"),
        pytest.param("description: plain # comment: here", "plain", id="yaml-comment-stays"),
        pytest.param("description: 12", 12, id="scalar-type-stays"),
    ],
)
def test_valid_frontmatter_parses_exactly_as_written(frontmatter: str, expected: object) -> None:
    assert parse_frontmatter_yaml(frontmatter) == {"description": expected}


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param("name: evil: name\ndescription: fine", id="name-is-never-repaired"),
        pytest.param("name: demo\nhomepage: a: b", id="other-fields-are-not-repaired"),
        pytest.param(
            "name: demo\ndescription: a: b\nmetadata: {unterminated", id="second-error-remains"
        ),
        pytest.param('name: demo\ndescription: "quoted" then: more', id="malformed-quote"),
        pytest.param("name: demo\ndescription: a: b\n  continued line", id="multi-line-plain"),
        pytest.param(
            "name: demo\nmetadata:\n  description: a: b", id="nested-description-is-not-the-field"
        ),
    ],
)
def test_other_invalid_frontmatter_stays_invalid(frontmatter: str) -> None:
    assert parse_frontmatter_yaml(frontmatter) is None
    assert _parse_frontmatter(_doc(frontmatter))[0] == {}


def test_repair_is_linear_on_a_hostile_description() -> None:
    """The largest SKILL.md the loader reads, all whitespace and colons."""
    frontmatter = "description: a: " + " \t" * 128_000 + "x"
    started = time.perf_counter()
    assert parse_frontmatter_yaml(frontmatter) is not None
    assert time.perf_counter() - started < 5


def test_loader_loads_the_skill_and_reports_the_one_it_cannot(monkeypatch, tmp_path: Path) -> None:
    warnings: list[str] = []

    class _Log:
        def warning(self, message: str, *args: object) -> None:
            warnings.append(message % args)

        def __getattr__(self, _name: str):
            return lambda *_args, **_kwargs: None

    monkeypatch.setattr(loader_module, "log", _Log())

    for directory, frontmatter in (
        ("colon", "name: colon\ndescription: Trade. Chains: Base."),
        ("broken", "name: [unterminated"),
    ):
        skill_dir = tmp_path / directory
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(_doc(frontmatter), encoding="utf-8")

    skills = SkillLoader(managed_dir=tmp_path).load_all()

    assert [(s.name, s.description) for s in skills] == [("colon", "Trade. Chains: Base.")]
    assert warnings == ["skill broken has no usable frontmatter, skipping"]


class _Router:
    def __init__(self, skill_md: str) -> None:
        self._skill_md = skill_md

    async def fetch(self, identifier: str, source_id: str) -> SkillBundle:
        return SkillBundle(name="demo", files={"SKILL.md": self._skill_md})

    async def inspect(self, identifier: str, source_id: str) -> SkillMeta | None:
        return None


def _install(tmp_path: Path, skill_md: str):
    installer = SkillInstaller(
        router=_Router(skill_md),
        managed_dir=tmp_path / "managed",
        quarantine_dir=tmp_path / "quarantine",
        lockfile_path=tmp_path / "lock.json",
    )
    return asyncio.run(installer.install("demo", "github"))


def test_install_of_a_skill_with_a_colon_in_its_description_is_loadable(tmp_path: Path) -> None:
    result = _install(tmp_path, _doc("name: demo\ndescription: Trade. Chains: Base."))

    assert result.success, result.message
    assert SkillLoader(managed_dir=tmp_path / "managed").get_by_name("demo") is not None


@pytest.mark.parametrize(
    "skill_md",
    [
        pytest.param(_doc("name: [unterminated"), id="invalid-frontmatter"),
        pytest.param("# No frontmatter at all\n", id="no-frontmatter"),
        pytest.param(_doc("description: No name."), id="no-name"),
    ],
)
def test_install_refuses_a_skill_the_loader_would_skip(tmp_path: Path, skill_md: str) -> None:
    """Reporting success here is what made the failure invisible."""
    result = _install(tmp_path, skill_md)

    assert not result.success
    assert "cannot be loaded" in result.message
    assert not (tmp_path / "managed" / "demo").exists()
