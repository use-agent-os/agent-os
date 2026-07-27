"""Tests for what an agent is told when a skill's requirements are unmet.

The rule being enforced: never promise an action the surface cannot perform.
A secret cannot be collected over a chat channel without landing in the chat
log, and an unattended run has nobody to collect it from — in both cases an
instruction to "ask the user" is a lie the agent would faithfully repeat.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentos import credential_sources
from agentos.skills.loader import SkillLoader
from agentos.tools.builtin import skill_tools
from agentos.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    current_tool_context,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BASE_RPC_URL", raising=False)
    credential_sources.reset_probe_cache()
    yield
    credential_sources.reset_probe_cache()


@pytest.fixture
def gated_skill(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "onchain").mkdir(parents=True)
    (skills / "onchain" / "SKILL.md").write_text(
        "---\n"
        "name: onchain\n"
        "description: Query an L2\n"
        "metadata:\n"
        "  agentos:\n"
        "    requires:\n"
        "      env:\n"
        "        - name: BASE_RPC_URL\n"
        "          description: Base L2 RPC endpoint\n"
        "          url: https://docs.example.invalid/\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=skills, snapshot_path=tmp_path / "snap.json")
    return next(s for s in loader.load_all() if s.name == "onchain")


class TestWhatIsMissing:
    def test_names_the_variable_with_its_purpose_and_source(self, gated_skill) -> None:
        note = skill_tools._skill_setup_note(gated_skill)
        assert "BASE_RPC_URL is not set" in note
        assert "Base L2 RPC endpoint" in note
        assert "https://docs.example.invalid/" in note

    def test_a_satisfied_skill_says_nothing(
        self, gated_skill, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BASE_RPC_URL", "https://rpc.example.invalid")
        assert skill_tools._skill_setup_note(gated_skill) == ""

    def test_an_unactionable_reason_produces_no_instruction(self, tmp_path: Path) -> None:
        # Wrong OS is not something the operator can fix from here; inventing
        # an instruction would be worse than staying quiet.
        skills = tmp_path / "skills"
        (skills / "winonly").mkdir(parents=True)
        (skills / "winonly" / "SKILL.md").write_text(
            "---\nname: winonly\ndescription: d\nmetadata:\n  agentos:\n"
            "    os: [nosuchos]\n---\nbody\n",
            encoding="utf-8",
        )
        loader = SkillLoader(bundled_dir=skills, snapshot_path=tmp_path / "snap.json")
        skill = next(s for s in loader.load_all() if s.name == "winonly")
        assert skill_tools._skill_setup_note(skill) == ""


class TestSurfaceAwareness:
    def test_a_chat_channel_is_told_not_to_collect_the_secret(
        self, gated_skill, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(skill_tools, "_setup_surface", lambda: "channel")
        note = skill_tools._skill_setup_note(gated_skill)
        assert "chat channel" in note
        assert "stored in the conversation" in note
        # It must not tell the agent to ask for the value here.
        assert "Ask the user to set it" not in note

    def test_an_unattended_run_is_told_to_continue_degraded(
        self, gated_skill, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(skill_tools, "_setup_surface", lambda: "unattended")
        note = skill_tools._skill_setup_note(gated_skill)
        assert "unattended" in note
        assert "which parts do not" in note

    def test_an_interactive_run_gets_a_real_action(
        self, gated_skill, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(skill_tools, "_setup_surface", lambda: "interactive")
        note = skill_tools._skill_setup_note(gated_skill)
        assert "agentos env set" in note
        assert "Environment screen" in note


class TestSurfaceDetection:
    def test_channel_callers_are_detected(self) -> None:
        ctx = ToolContext(caller_kind=CallerKind.CHANNEL)
        token = current_tool_context.set(ctx)
        try:
            assert skill_tools._setup_surface() == "channel"
        finally:
            current_tool_context.reset(token)

    def test_unattended_runs_are_detected(self) -> None:
        ctx = ToolContext(caller_kind=CallerKind.CRON, interaction_mode=InteractionMode.UNATTENDED)
        token = current_tool_context.set(ctx)
        try:
            assert skill_tools._setup_surface() == "unattended"
        finally:
            current_tool_context.reset(token)

    def test_no_context_is_treated_as_interactive(self) -> None:
        token = current_tool_context.set(None)
        try:
            assert skill_tools._setup_surface() == "interactive"
        finally:
            current_tool_context.reset(token)


class TestImportOffer:
    def test_mentions_a_credential_that_already_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skills = tmp_path / "skills"
        (skills / "repo").mkdir(parents=True)
        (skills / "repo" / "SKILL.md").write_text(
            "---\nname: repo\ndescription: d\nmetadata:\n  agentos:\n"
            "    requires:\n      env: [GITHUB_TOKEN]\n---\nbody\n",
            encoding="utf-8",
        )
        loader = SkillLoader(bundled_dir=skills, snapshot_path=tmp_path / "snap.json")
        skill = next(s for s in loader.load_all() if s.name == "repo")

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(credential_sources, "_run", lambda argv, timeout: (0, "Logged in"))

        note = skill_tools._skill_setup_note(skill)
        assert "already available from GitHub CLI" in note
        assert "agentos env import GITHUB_TOKEN" in note

    def test_discovery_failure_does_not_break_the_note(
        self, gated_skill, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*args, **kwargs):
            raise RuntimeError("registry is broken")

        monkeypatch.setattr(credential_sources, "available_for", explode)
        note = skill_tools._skill_setup_note(gated_skill)
        assert "BASE_RPC_URL is not set" in note


class TestNoPhantomTool:
    def test_the_note_never_points_at_a_hidden_tool(self, gated_skill) -> None:
        # env_set is hidden by default, so naming it here would be the same
        # dead-end this feature exists to remove.
        note = skill_tools._skill_setup_note(gated_skill)
        assert "env_set(" not in note

    def test_skill_list_no_longer_points_at_it_either(self) -> None:
        source = Path(skill_tools.__file__).read_text(encoding="utf-8")
        assert "env_set(name=" not in source


class TestOsEnvironIsolation:
    def test_the_note_reads_the_live_environment(
        self, gated_skill, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Eligibility is rebuilt per call, so a value set moments ago counts.
        assert "BASE_RPC_URL is not set" in skill_tools._skill_setup_note(gated_skill)
        os.environ["BASE_RPC_URL"] = "https://rpc.example.invalid"
        try:
            assert skill_tools._skill_setup_note(gated_skill) == ""
        finally:
            os.environ.pop("BASE_RPC_URL", None)
