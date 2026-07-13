from __future__ import annotations

from pathlib import Path

import yaml

from agentos.identity.prompt import assemble_system_prompt
from agentos.identity.types import AgentProfile

GOLDEN_PATH = Path("tests/golden/public_release_open.yaml")


def _golden_cases() -> list[dict]:
    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    cases = data.get("cases")
    assert isinstance(cases, list)
    return cases


def test_public_golden_file_uses_public_schema_and_no_removed_tool_names() -> None:
    cases = _golden_cases()

    assert {case["id"] for case in cases} == {
        "canonical-image-tool",
        "session-vs-channel-message",
        "session-spawn-canonical-name",
        "raw-http-is-owner-only",
    }
    for case in cases:
        assert isinstance(case.get("prompt"), str) and case["prompt"]
        assert "generate_image" not in " ".join(case.get("must_contain", []))
        assert "spawn_subagent" not in " ".join(case.get("must_contain", []))
        assert "send_message" not in " ".join(case.get("must_contain", []))


def test_public_golden_static_prompt_contract_matches_tool_policy() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["image_generate", "sessions_spawn", "sessions_send", "message"],
    )

    assert "`image_generate`" in prompt
    assert "`sessions_send`" in prompt
    assert "`message` only for channel adapter delivery" in prompt
    assert "generate_image" not in prompt
    assert "spawn_subagent" not in prompt
    assert "send_message" not in prompt
