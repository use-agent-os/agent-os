from __future__ import annotations

from pathlib import Path

from agentos.skills.hub import deps
from agentos.skills.loader import SkillLoader
from agentos.skills.types import SkillInstallSpec
from agentos.tools.builtin import skill_tools

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"


def test_every_bundled_skill_has_supported_install_kind() -> None:
    # Use a dummy snapshot path to bypass cache verification issues in tests
    loader = SkillLoader(bundled_dir=BUNDLED)
    skills = loader.load_all()

    # Canonical expected set of install kinds
    canonical_kinds = {"brew", "npm", "go", "uv", "download", "apt"}

    # Assert that deps module supports exactly the canonical kinds
    assert set(deps._INSTALLERS.keys()) == canonical_kinds

    # Assert that all install specs in all bundled skills use canonical kinds
    checked_count = 0
    for skill in skills:
        if skill.metadata and skill.metadata.install:
            for spec in skill.metadata.install:
                assert spec.kind in canonical_kinds, (
                    f"Skill '{skill.name}' declares unsupported install kind '{spec.kind}'"
                )
                checked_count += 1

                # Verify that skill_tools._argv_for_install_spec supports non-deferred kinds
                if spec.kind != "download":
                    # Create a valid minimal spec with a package/formula name to
                    # avoid validation errors
                    test_spec = SkillInstallSpec(
                        kind=spec.kind,
                        package="test-package",
                        formula="test-formula",
                        module="test-module",
                    )
                    argv = skill_tools._argv_for_install_spec(test_spec)
                    assert len(argv) > 0
                    assert argv[0] in {"brew", "npm", "go", "uv", "sudo"}

    assert checked_count > 0, "No bundled skills with install specifications were checked"
