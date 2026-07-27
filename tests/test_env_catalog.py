"""Tests for the env-var catalog and the skill manifest schema behind it."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos import env_catalog
from agentos.env_catalog import CATEGORY_CUSTOM, CATEGORY_PROVIDER, CATEGORY_SKILL
from agentos.skills.eligibility import EligibilityContext, diagnose_eligibility
from agentos.skills.loader import SkillLoader
from agentos.skills.types import SkillEnvVar, SkillRequires


class TestSkillEnvVarCoercion:
    def test_plain_string_still_works(self) -> None:
        # The pre-existing manifest form must keep loading untouched.
        requires = SkillRequires(env=["BASE_RPC_URL"])  # type: ignore[list-item]
        assert requires.env_names == ["BASE_RPC_URL"]
        assert requires.env[0].description == ""

    def test_rich_mapping_is_parsed(self) -> None:
        requires = SkillRequires(
            env=[  # type: ignore[list-item]
                {
                    "name": "BASE_RPC_URL",
                    "description": "Base L2 RPC endpoint",
                    "url": "https://docs.example.invalid/",
                    "secret": False,
                }
            ]
        )
        declared = requires.env[0]
        assert declared.name == "BASE_RPC_URL"
        assert declared.description == "Base L2 RPC endpoint"
        assert declared.url == "https://docs.example.invalid/"
        assert declared.secret is False

    def test_mixed_forms_coexist(self) -> None:
        requires = SkillRequires(
            env=["PLAIN_TOKEN", {"name": "RICH_TOKEN", "description": "d"}]  # type: ignore[list-item]
        )
        assert requires.env_names == ["PLAIN_TOKEN", "RICH_TOKEN"]

    @pytest.mark.parametrize("bad", [None, "", "   ", {}, {"name": ""}, 42, []])
    def test_unusable_entries_are_dropped_not_raised(self, bad: object) -> None:
        # One malformed line in a manifest must not stop the skill from loading.
        requires = SkillRequires(env=[bad, "GOOD"])  # type: ignore[list-item]
        assert requires.env_names == ["GOOD"]

    def test_round_trips_through_the_cache_dict_form(self) -> None:
        original = SkillEnvVar(name="K", description="d", url="u", secret=True, required=False)
        restored = SkillRequires(env=[original.to_dict()])  # type: ignore[list-item]
        assert restored.env[0] == original


def _write_skill(root: Path, name: str, frontmatter_env: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Test skill {name}\n"
        "metadata:\n"
        "  agentos:\n"
        "    requires:\n"
        f"{frontmatter_env}"
        "---\n"
        "body\n",
        encoding="utf-8",
    )


@pytest.fixture
def loader_with_env_skill(tmp_path: Path) -> SkillLoader:
    skills = tmp_path / "skills"
    skills.mkdir()
    _write_skill(
        skills,
        "onchain",
        "      env:\n"
        "        - name: BASE_RPC_URL\n"
        "          description: Base L2 RPC endpoint\n"
        "          url: https://docs.example.invalid/\n"
        "          secret: false\n",
    )
    return SkillLoader(bundled_dir=skills, snapshot_path=tmp_path / "snapshot.json")


class TestManifestParsing:
    def test_rich_declaration_survives_loading(self, loader_with_env_skill: SkillLoader) -> None:
        skill = next(s for s in loader_with_env_skill.load_all() if s.name == "onchain")
        assert skill.metadata is not None
        assert skill.metadata.requires is not None
        declared = skill.metadata.requires.env[0]
        assert declared.name == "BASE_RPC_URL"
        assert declared.description == "Base L2 RPC endpoint"
        assert declared.secret is False

    def test_eligibility_reports_names_and_detail(
        self, loader_with_env_skill: SkillLoader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BASE_RPC_URL", raising=False)
        skill = next(s for s in loader_with_env_skill.load_all() if s.name == "onchain")
        report = diagnose_eligibility(skill, EligibilityContext.auto())
        assert report.eligible is False
        # The plain list keeps its old shape for existing callers...
        assert report.missing_env == ["BASE_RPC_URL"]
        # ...while the detail carries what a surface needs to offer a real fix.
        assert report.missing_env_detail[0].url == "https://docs.example.invalid/"

    def test_satisfied_env_makes_the_skill_eligible(
        self, loader_with_env_skill: SkillLoader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BASE_RPC_URL", "https://rpc.example.invalid")
        skill = next(s for s in loader_with_env_skill.load_all() if s.name == "onchain")
        assert diagnose_eligibility(skill, EligibilityContext.auto()).eligible is True


class TestCatalog:
    def test_provider_keys_come_from_the_setup_specs(self) -> None:
        # Derived, not hand-listed: adding a provider must not require also
        # remembering to add its key here.
        catalog = env_catalog.build_catalog()
        assert "OPENAI_API_KEY" in catalog
        entry = catalog["OPENAI_API_KEY"]
        assert entry.category == CATEGORY_PROVIDER
        assert entry.secret is True
        assert entry.description

    def test_skill_declarations_are_attributed_to_their_skill(
        self, loader_with_env_skill: SkillLoader
    ) -> None:
        catalog = env_catalog.build_catalog(loader_with_env_skill)
        entry = catalog["BASE_RPC_URL"]
        assert entry.category == CATEGORY_SKILL
        assert entry.owner == "onchain"
        assert entry.url == "https://docs.example.invalid/"
        assert entry.secret is False

    def test_undeclared_names_from_the_env_file_appear_as_custom(self) -> None:
        catalog = env_catalog.build_catalog(present_names={"MY_OWN_THING"})
        entry = catalog["MY_OWN_THING"]
        assert entry.category == CATEGORY_CUSTOM
        # Unknown names default to secret: masking something harmless is a
        # smaller mistake than printing something sensitive.
        assert entry.secret is True

    def test_a_declared_name_is_not_downgraded_to_custom(self) -> None:
        catalog = env_catalog.build_catalog(present_names={"OPENAI_API_KEY"})
        assert catalog["OPENAI_API_KEY"].category == CATEGORY_PROVIDER

    def test_provider_categories_need_a_restart_and_skill_ones_do_not(
        self, loader_with_env_skill: SkillLoader
    ) -> None:
        catalog = env_catalog.build_catalog(loader_with_env_skill)
        # A provider client is built once at boot with the key it had then.
        assert catalog["OPENAI_API_KEY"].restart_required is True
        # A skill's variable is read by a process spawned after the change.
        assert catalog["BASE_RPC_URL"].restart_required is False

    def test_describe_synthesizes_an_entry_for_an_unknown_name(self) -> None:
        entry = env_catalog.describe("SOME_UNKNOWN_TOKEN")
        assert entry.category == CATEGORY_CUSTOM
        assert entry.secret is True

    def test_a_broken_loader_degrades_instead_of_raising(self) -> None:
        class ExplodingLoader:
            def load_all(self) -> list[object]:
                raise RuntimeError("unreadable skills dir")

        catalog = env_catalog.build_catalog(ExplodingLoader())  # type: ignore[arg-type]
        assert "OPENAI_API_KEY" in catalog


class TestSentinelEnvKeys:
    def test_oauth_providers_do_not_become_a_variable(self) -> None:
        """``env_key`` is not always a variable name.

        Providers that authenticate by OAuth carry the literal string
        ``"OAuth"`` there, meaning "no API key involved". Taking it at face
        value put a variable called ``OAuth`` on the Environment screen that
        nobody could ever set.
        """
        catalog = env_catalog.build_catalog()
        assert "OAuth" not in catalog
        assert not [name for name in catalog if not name.isupper()]

    def test_every_provider_that_does_use_a_key_is_still_listed(self) -> None:
        catalog = env_catalog.build_catalog()
        for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "BRAVE_SEARCH_API_KEY"):
            assert name in catalog
