from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import agentos.engine.steps.skills_filter as skills_filter_step
from agentos.engine.pipeline import TurnContext
from agentos.engine.steps.skills_filter import filter_skills
from agentos.gateway.config import GatewayConfig
from agentos.skills.eligibility import EligibilityContext
from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
AUDIO_DEFAULTS = {
    "advanced-dubbing-studio",
    "music-and-singing-studio",
    "voice-clone-lab",
    "voice-conversion-studio",
    "voiceover-studio",
}
GMGN_DEFAULTS = {
    "gmgn-cooking",
    "gmgn-holder-analysis",
    "gmgn-market",
    "gmgn-portfolio",
    "gmgn-swap",
    "gmgn-token",
    "gmgn-track",
    "gmgn-wallet-analysis",
    "gmgn-wallet-score",
}
DEFAULTS = (
    {
        "agentos",
        "ai-video-script",
        "cron",
        "cron-watchers",
        "deep-research",
        "docx",
        "git-diff",
        "github",
        "history-explorer",
        "html-to-pdf",
        "http-fetch",
        "memory",
        "multi-search-engine",
        "musebook",
        "nano-banana-pro",
        "nano-pdf",
        "pdf-toolkit",
        "poolsdotfun-token-launcher",
        "pptx",
        "robinhood-agentic-trading",
        "robinhood-chain-stocks",
        "robinhood-rwa-addresses",
        "seedance-2-prompt",
        "senior-unilp-manager",
        "srt-from-script",
        "sub-agent",
        "subtitle-burner",
        "summarize",
        "text-file-read",
        "title-card-image",
        "token-burner",
        "tmux",
        "video-merger",
        "video-still-animator",
        "wallet-trading",
        "weather",
        "xlsx",
    }
    | AUDIO_DEFAULTS
    | GMGN_DEFAULTS
)
# Bundled but gated out of the prompt until their environment is configured: they declare
# `requires.env`, so `gate_skills` drops them while any of those variables is unset. That is
# the intended "Needs setup" behaviour, not a regression. The gmgn-* skills additionally
# require the third-party `gmgn-cli` binary, which AgentOS does not ship.
ENV_GATED_DEFAULTS = {"senior-unilp-manager", "poolsdotfun-token-launcher"} | GMGN_DEFAULTS
PROMPT_DEFAULTS_WITHOUT_AUDIO_TOOLS = DEFAULTS - AUDIO_DEFAULTS - ENV_GATED_DEFAULTS
INTERNAL_HELPERS = {
    "stack-trace-generic-probe",
    "stack-trace-go-probe",
    "stack-trace-js-probe",
    "stack-trace-python-probe",
    "stack-trace-rust-probe",
}


def _ctx(
    loader: SkillLoader,
    tools: set[str] | None = None,
    *,
    message: str = "please summarize weather and github state",
    skills_config: SimpleNamespace | None = None,
) -> TurnContext:
    tool_defs = [
        SimpleNamespace(name=name)
        for name in (
            tools
            or {
                "background_process",
                "cron",
                "exec_command",
                "memory_get",
                "memory_save",
                "memory_search",
                "process",
            }
        )
    ]
    if skills_config is None:
        skills_config = SimpleNamespace(
            filter_enabled=False,
            max_skills_prompt_chars=100_000,
            injection_mode="system",
        )
    return TurnContext(
        message=message,
        session_key="agent:main:webchat:default",
        config=SimpleNamespace(
            tools=SimpleNamespace(profile="standard"),
            skills=skills_config,
        ),
        provider=None,
        model="test-model",
        tool_defs=tool_defs,
        system_prompt="base",
        metadata={"skill_loader": loader},
    )


def _injected(ctx: TurnContext) -> str:
    """Everything the pipeline put in the system prompt, whichever slot it used.

    Which slot the skills block lands in is its own contract (see
    ``test_skills_injector``); tests that only ask "was this skill listed"
    should not have to care.
    """
    prompt = ctx.system_prompt
    return prompt if isinstance(prompt, str) else "\n\n".join(prompt)


def test_bundled_directory_only_contains_retained_default_skills() -> None:
    bundled_names = {
        path.name for path in BUNDLED.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
    }

    assert bundled_names == DEFAULTS | INTERNAL_HELPERS


def test_skill_filter_defaults_are_release_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_SKILLS_FILTER_ENABLED", raising=False)
    monkeypatch.delenv("AGENTOS_SKILLS_FILTER_STRATEGY", raising=False)

    cfg = GatewayConfig()

    assert cfg.skills.filter_enabled is False
    assert cfg.skills.filter_strategy == "lexical"


@pytest.mark.asyncio
async def test_default_prompt_only_injects_retained_bundled_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MockBinCache(dict):
        def __contains__(self, key: object) -> bool:
            return True

        def __getitem__(self, key: str) -> bool:
            return True

    monkeypatch.setattr(
        skills_filter_step,
        "_eligibility_context",
        lambda: EligibilityContext(
            os_name="linux",
            has_bin_cache=MockBinCache(),
        ),
    )
    # A developer who happens to have these exported would otherwise see a different
    # prompt than CI does.
    for name in (
        "RPC_BASE_URL",
        "RPC_ROBINHOOD_URL",
        "UNIV4_LP_PRIVATE_KEY",
        "GMGN_API_KEY",
        "GMGN_PRIVATE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")

    ctx = await filter_skills(_ctx(loader))

    prompt = _injected(ctx)
    for name in PROMPT_DEFAULTS_WITHOUT_AUDIO_TOOLS:
        assert f"<name>{name}</name>" in prompt
    for name in AUDIO_DEFAULTS:
        assert f"<name>{name}</name>" not in prompt
    for name in INTERNAL_HELPERS:
        assert f"<name>{name}</name>" not in prompt
    for name in ENV_GATED_DEFAULTS:
        assert f"<name>{name}</name>" not in prompt
    assert "<name>healthcheck</name>" not in prompt


@pytest.mark.asyncio
async def test_allowlist_does_not_hide_managed_or_workspace_skills(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    skill_dir = managed / "custom-community"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: custom-community\n"
        "description: Use when testing managed skills.\n"
        "---\n\n# Custom\n",
        encoding="utf-8",
    )
    loader = SkillLoader(
        bundled_dir=BUNDLED,
        managed_dir=managed,
        snapshot_path=tmp_path / "snapshot.json",
    )

    ctx = await filter_skills(_ctx(loader))

    assert "<name>custom-community</name>" in _injected(ctx)


@pytest.mark.asyncio
async def test_lexical_skill_filter_is_opt_in_and_dependency_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    for name, description, triggers in (
        ("weather-local", "Fetch weather forecasts.", "[weather, forecast]"),
        ("github-local", "Inspect GitHub pull requests.", "[github, pull request]"),
    ):
        skill_dir = workspace / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"triggers: {triggers}\n"
            "---\n\n"
            f"# {name}\n",
            encoding="utf-8",
        )

    def fail_get_embedder(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("lexical skill filtering must not load embeddings")

    monkeypatch.setattr(skills_filter_step, "_retriever", None)
    monkeypatch.setattr("agentos.skills.retrieval.embedder.get_embedder", fail_get_embedder)
    loader = SkillLoader(workspace_dir=workspace, snapshot_path=tmp_path / "snapshot.json")

    ctx = await filter_skills(
        _ctx(
            loader,
            message="please check the weather forecast",
            skills_config=SimpleNamespace(
                filter_enabled=True,
                filter_top_k=1,
                filter_strategy="lexical",
                filter_lexical_top_n=20,
                filter_semantic_top_n=20,
                filter_rrf_k=60,
                filter_embedding_model="BAAI/bge-small-zh-v1.5",
                max_skills_prompt_chars=100_000,
                injection_mode="system",
            ),
        )
    )

    prompt = ctx.system_prompt[1]
    assert "<name>weather-local</name>" in prompt
    assert "<name>github-local</name>" not in prompt
    assert ctx.metadata["filtered_skill_ids"] == ["weather-local"]
    assert ctx.metadata["skill_count"] == 1


def test_workspace_overrides_managed_and_bundled_precedence(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    for root, desc in ((managed, "managed desc"), (workspace, "workspace desc")):
        skill_dir = root / "github"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: github\ndescription: {desc}\n---\n\n# {desc}\n",
            encoding="utf-8",
        )

    loader = SkillLoader(
        bundled_dir=BUNDLED,
        managed_dir=managed,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )

    skill = loader.get_by_name("github")

    assert skill is not None
    assert skill.description == "workspace desc"


@pytest.mark.asyncio
async def test_disable_model_invocation_hides_from_prompt_but_not_loader(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "hidden-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: hidden-skill\n"
        "description: Use when hidden testing.\n"
        "disable-model-invocation: true\n"
        "---\n\n# Hidden\n",
        encoding="utf-8",
    )
    loader = SkillLoader(
        bundled_dir=BUNDLED,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )

    skill = loader.get_by_name("hidden-skill")
    ctx = await filter_skills(_ctx(loader))

    assert skill is not None
    assert skill.content == "# Hidden"
    assert "<name>hidden-skill</name>" not in _injected(ctx)


def test_retained_default_skills_are_parseable_and_not_disabled(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    skills = {skill.name: skill for skill in loader.load_all()}

    for name in DEFAULTS:
        assert name in skills
        assert skills[name].disable_model_invocation is False
        assert skills[name].description
