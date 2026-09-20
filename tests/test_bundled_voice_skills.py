"""Contract tests for bundled voice-production skills."""

from __future__ import annotations

import re
from pathlib import Path

from agentos.skills.loader import SkillLoader
from agentos.tools.registry import get_default_registry

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"

#: A workflow step that spells out an exact call, e.g. "Call `voice_convert`
#: with `source_audio`, `target_voice`, ...". Distinct from a step that
#: describes the same call in plain words ("with the sample, name,
#: description") -- only a step that already commits to exact backtick names
#: is checked, so a looser, correct description isn't flagged as missing one.
_EXACT_CALL_RE = re.compile(r"Call `(\w+)` with ((?:`\w+`(?:, )?)+)")

VOICE_SKILLS = {
    "voiceover-studio": {
        "tools": {"tts", "voice_search", "audio_provider_capabilities"},
        "risk": "medium",
        "must_include": [
            "Request triage",
            "Preview-first",
            "Tool-result handling",
            "locale-appropriate accent",
            "普通话",
            "playable audio artifact",
        ],
    },
    "voice-clone-lab": {
        "tools": {"voice_clone", "audio_provider_capabilities"},
        "risk": "high",
        "must_include": [
            "Request triage",
            "Tool-result handling",
            "consent",
            "授权",
            "版权",
            "locale-appropriate accent",
        ],
    },
    "voice-conversion-studio": {
        "tools": {"voice_convert", "audio_provider_capabilities"},
        "risk": "high",
        "must_include": [
            "Request triage",
            "Preview-first",
            "Tool-result handling",
            "consent",
            "授权",
            "版权",
            "locale-appropriate accent",
        ],
    },
    "advanced-dubbing-studio": {
        "tools": {
            "dubbing_generate",
            "dubbing_status",
            "dubbing_download",
            "audio_provider_capabilities",
        },
        "risk": "high",
        "must_include": [
            "Request triage",
            "Preview-first",
            "Tool-result handling",
            "版权",
            "playable audio artifact",
            "locale-appropriate accent",
        ],
    },
    "music-and-singing-studio": {
        "tools": {"music_generate", "song_generate", "audio_provider_capabilities"},
        "risk": "medium",
        "must_include": [
            "版权",
            "lyrics",
            "playable audio artifact",
            "locale-appropriate accent",
            "Do not claim credits are insufficient",
            "API key quota",
            "Request triage",
            "Preview-first",
            "Tool-result handling",
            "short demo",
        ],
    },
}


def test_bundled_voice_skills_are_parseable_and_tool_scoped() -> None:
    loader = SkillLoader(bundled_dir=BUNDLED)
    by_name = {spec.name: spec for spec in loader.load_all()}

    for name, expected in VOICE_SKILLS.items():
        spec = by_name.get(name)
        assert spec is not None, f"{name} should be bundled"
        assert spec.metadata is not None
        assert spec.metadata.risk_level == expected["risk"]
        assert set(spec.requires_tools) == expected["tools"]
        assert "network-read" in spec.metadata.capabilities
        assert "filesystem-write" in spec.metadata.capabilities
        assert spec.provenance.origin == "agentos-original"
        assert spec.provenance.license == "MIT"


def test_bundled_voice_skills_document_rights_and_locale_accent_constraints() -> None:
    for name, expected in VOICE_SKILLS.items():
        skill_md = BUNDLED / name / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")
        lowered = text.lower()

        assert "copyright" in lowered or "版权" in text
        assert "授权" in text or "consent" in lowered
        assert "public figure" in lowered or "公众人物" in text
        assert "openrouter" in lowered
        assert "target language" in lowered or "目标语种" in text

        for phrase in expected["must_include"]:
            assert phrase in text


def test_bundled_voice_skills_call_examples_name_the_real_required_params() -> None:
    """A "Call `tool` with `a`, `b`, ..." step commits to exact parameter
    names -- if one of them is wrong, the agent following the skill literally
    sends a bad call (missing a required field, and/or an argument the tool's
    schema has no slot for). ``voice-conversion-studio`` told the agent to
    pass `voice`; the tool's real required parameter is `target_voice`."""
    registry = get_default_registry()

    for name in VOICE_SKILLS:
        text = (BUNDLED / name / "SKILL.md").read_text(encoding="utf-8")
        for tool_name, params_blob in _EXACT_CALL_RE.findall(text):
            entry = registry.get(tool_name)
            assert entry is not None, f"{name} calls unregistered tool {tool_name!r}"
            named = set(re.findall(r"`(\w+)`", params_blob))
            required = set(entry.spec.required or [])
            missing = required - named
            assert not missing, (
                f"{name}'s SKILL.md calls `{tool_name}` without its required "
                f"param(s) {missing} (named instead: {named})"
            )
