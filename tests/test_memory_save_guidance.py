"""What the model is told about saving durable facts, and where they go.

The wording itself is tuned against a real model and will keep moving; these
tests pin only the properties a rewording must not lose:

- the periodic review checks every fact the user stated against what is
  already saved (asked for what "stands out" it saved one or two facts out of
  a window holding many more, and skipped facts mentioned inside a request);
- it names what not to save, so being thorough does not turn into saving
  small talk;
- an outdated value is replaced, not kept beside the new one;
- every place that says where a fact goes says the same thing: USER.md is a
  short profile, every other durable fact goes to MEMORY.md (the larger
  store). When they disagreed, nearly every fact went to USER.md, which filled
  up while MEMORY.md stayed mostly empty;
- all of it is English, like every shipped prompt.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.engine.runtime import _MEMORY_REVIEW_PROMPT
from agentos.identity.prompt import assemble_system_prompt
from agentos.identity.types import AgentProfile
from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry


class _NoStore:
    async def index_file(self, *, path: str, content: str, source: object) -> int:
        return 0

    async def remove_file(self, path: str) -> None:
        return None

    async def total_size(self) -> int:
        return 0


def _memory_tool_description(tmp_path: Path) -> str:
    registry = ToolRegistry()
    create_memory_tools(
        stores=_NoStore(),
        retrievers=SimpleNamespace(),
        memory_dir=str(tmp_path),
        registry=registry,
    )
    tool = registry.get("memory")
    assert tool is not None
    return tool.spec.description


def _write_guidance() -> str:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get", "memory", "write_file"],
    )
    start = prompt.index("## Memory Write Guidance")
    end = prompt.index("\n## ", start + 1)
    return prompt[start:end]


# Punctuation the prompts already use; anything else outside ASCII would be a
# non-English example phrase or a stray character.
_ALLOWED_NON_ASCII = frozenset("—’§")


@pytest.mark.parametrize("which", ["review", "tool", "guidance"])
def test_save_guidance_is_english_only(which: str, tmp_path: Path) -> None:
    text = {
        "review": lambda: _MEMORY_REVIEW_PROMPT,
        "tool": lambda: _memory_tool_description(tmp_path),
        "guidance": _write_guidance,
    }[which]()

    assert {ch for ch in text if not ch.isascii()} <= _ALLOWED_NON_ASCII


def test_review_walks_every_user_message_and_checks_it_against_memory() -> None:
    prompt = _MEMORY_REVIEW_PROMPT

    assert "every message the user sent" in prompt
    assert "USER PROFILE" in prompt and "MEMORY" in prompt
    for mark in ("'saved'", "'outdated'", "'missing'"):
        assert mark in prompt


def test_review_counts_facts_mentioned_inside_another_request() -> None:
    assert "in passing" in _MEMORY_REVIEW_PROMPT
    # The old wording told the review to skip anything tied to the current
    # task, which is exactly where most such facts are said.
    assert "specific to the current task" not in _MEMORY_REVIEW_PROMPT
    assert "stands out" not in _MEMORY_REVIEW_PROMPT


def test_review_names_what_not_to_save() -> None:
    prompt = _MEMORY_REVIEW_PROMPT

    for chatter in ("small talk", "passing moods", "one-off questions", "the task itself"):
        assert chatter in prompt


def test_review_replaces_an_outdated_value_instead_of_adding_beside_it() -> None:
    assert "replace every outdated entry" in _MEMORY_REVIEW_PROMPT


def test_review_keeps_an_explicit_way_to_decline() -> None:
    assert "Nothing to save." in _MEMORY_REVIEW_PROMPT


def test_every_placement_rule_sends_non_profile_facts_to_memory(tmp_path: Path) -> None:
    tool = _memory_tool_description(tmp_path)
    guidance = _write_guidance()
    review = _MEMORY_REVIEW_PROMPT

    # USER.md is a short profile everywhere ...
    assert "'user' = a short profile" in tool
    assert "`target: user` (backed by `USER.md`) for a short profile" in guidance
    assert "target 'user' for who the user is" in review
    # ... and the facts about the user's life go to the larger store.
    assert "'memory' = every other durable fact" in tool
    assert "`target: memory` (backed by `MEMORY.md`) for every other durable fact" in guidance
    assert "'memory' for everything else" in review
    for text in (tool, guidance):
        assert "people in their life" in text


def test_a_nearly_full_store_is_no_reason_to_skip_or_drop_a_fact(tmp_path: Path) -> None:
    # At the limit the model stopped saving, or made room by overwriting an
    # entry that was still true. The tool now says what to do instead.
    tool = _memory_tool_description(tmp_path)

    assert "no reason to skip a durable fact" in tool
    assert "other target if that one has room" in tool
    assert "Never remove or overwrite a fact that is still true" in tool


def test_a_replace_keeps_the_entrys_other_details(tmp_path: Path) -> None:
    # Most facts lost in the eval were not squeezed out by the limit: a
    # replace for one changed detail rewrote the entry with only the new
    # value, deleting the unrelated facts stored beside it.
    tool = _memory_tool_description(tmp_path)

    assert "keep every other detail of that entry" in tool
    assert "outdated entry, keeping its other details" in _MEMORY_REVIEW_PROMPT
    # Entries that mix unrelated facts are what a later replace tears apart.
    assert "one topic per entry" in _MEMORY_REVIEW_PROMPT


def test_a_fact_mentioned_in_passing_is_saved_in_that_turn(tmp_path: Path) -> None:
    # The review runs every few turns; the turns after the last one in a
    # session are only covered by saving in the turn itself.
    tool = _memory_tool_description(tmp_path)
    guidance = _write_guidance()

    assert "in passing while asking for something else" in tool
    assert "in passing while asking for something else" in guidance
    assert "Save it in the turn it comes up" in tool
