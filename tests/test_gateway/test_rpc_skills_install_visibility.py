from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentos.gateway import rpc_skills
from agentos.gateway.rpc import RpcContext
from agentos.skills.hub.installer import InstallResult
from agentos.skills.loader import SkillLoader


def test_rpc_skill_install_uses_loader_managed_dir_and_list_sees_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        managed_dir = tmp_path / "managed"
        loader = SkillLoader(
            managed_dir=managed_dir,
            snapshot_path=tmp_path / "snapshot.json",
        )
        ctx = RpcContext(conn_id="test", skill_loader=loader)
        captured: dict[str, Path | None] = {}

        class FakeInstaller:
            def __init__(self, managed_dir: Path) -> None:
                self.managed_dir = managed_dir

            async def install(
                self,
                identifier: str,
                source_id: str,
                force: bool = False,
            ) -> InstallResult:
                skill_dir = self.managed_dir / identifier
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\n"
                    f"name: {identifier}\n"
                    "description: Installed from chat\n"
                    "---\n"
                    "Installed body.\n",
                    encoding="utf-8",
                )
                return InstallResult(
                    success=True,
                    name=identifier,
                    message="installed",
                    path=str(skill_dir),
                )

        def fake_builder(*, managed_dir: Path | None = None) -> FakeInstaller:
            assert managed_dir is not None
            captured["managed_dir"] = managed_dir
            return FakeInstaller(managed_dir)

        monkeypatch.setattr(rpc_skills, "build_default_skill_installer", fake_builder)
        assert await rpc_skills._handle_skills_list(None, ctx) == {"skills": []}
        assert loader._cached is not None

        installed = await rpc_skills._handle_skills_install(
            {"identifier": "plotter", "source": "clawhub"},
            ctx,
        )
        listed = await rpc_skills._handle_skills_list(None, ctx)

        assert captured["managed_dir"] == managed_dir
        assert installed["success"] is True
        assert Path(installed["path"]).name == "plotter"
        row = next(skill for skill in listed["skills"] if skill["name"] == "plotter")
        assert row["layer"] == "managed"
        assert row["description"] == "Installed from chat"

    asyncio.run(run())


def test_video_merger_declares_ffmpeg_binaries() -> None:
    bundled = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "agentos"
        / "skills"
        / "bundled"
    )
    loader = SkillLoader(bundled_dir=bundled)
    skills = loader.load_all()
    skill_index = {skill.name: skill for skill in skills}
    ctx = rpc_skills.EligibilityContext.auto()
    spec = skill_index["video-merger"]
    payload = rpc_skills._skill_to_dict(
        spec,
        rpc_skills.diagnose_eligibility(spec, ctx),
        ctx.os_name,
        skill_index=skill_index,
        eligibility_ctx=ctx,
    )

    own_requirements = next(
        item for item in payload["requirements"]["items"] if item["source"] == "self"
    )
    assert own_requirements["requires_bins"] == ["ffmpeg", "ffprobe"]
