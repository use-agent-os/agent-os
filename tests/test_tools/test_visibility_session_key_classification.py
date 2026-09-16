"""Issue #2293: a canonical subagent key was classified as an interactive agent.

``effective_tool_context`` asked ``session_key.startswith("subagent:")``, which
recognises only the *legacy* key shape. ``build_subagent_session_key`` — what
``tools/builtin/sessions.py`` actually calls when it spawns a subagent — emits
the canonical ``agent:<agent_id>:subagent:<run_id>``, so every real subagent
turn fell through to the ``else`` branch and came back as
``CallerKind.AGENT`` / ``InteractionMode.INTERACTIVE`` with an empty
``denied_tools``. ``SUBAGENT_TOOL_DENY`` exists precisely to take those tools
away from an unattended run, and it was never applied.

``session.keys.is_subagent_key`` already answered this correctly for both
shapes. Two definitions of one question had drifted apart; this leaves one.

The same function hardcoded ``agent_id or "main"`` in all three branches, so a
session belonging to agent ``ops`` reported ``agent_id == "main"`` whenever the
caller passed only a key. That is not cosmetic: ``ctx.agent_id`` is what
``memory_tools`` uses to pick whose memory to read and write, and what
``agent_policy_from_config`` uses to pick whose tool policy applies — so an
``ops`` turn was reading main's memory under main's policy.
"""

from __future__ import annotations

import pytest

from agentos.session.keys import (
    build_cron_key,
    build_group_key,
    build_main_key,
    build_subagent_key,
    build_subagent_session_key,
    is_cron_key,
    is_subagent_key,
)
from agentos.tools.types import SUBAGENT_TOOL_DENY, CallerKind, InteractionMode
from agentos.tools.visibility import effective_tool_context

CANONICAL = build_subagent_session_key("main", "test1234")
LEGACY = build_subagent_key(build_main_key("main"))


# ── the reported bug ────────────────────────────────────────────────────────


def test_a_canonical_subagent_key_is_a_subagent() -> None:
    """The issue's own reproduction."""
    ctx = effective_tool_context(session_key=CANONICAL)

    assert ctx.caller_kind is CallerKind.SUBAGENT
    assert ctx.interaction_mode is InteractionMode.UNATTENDED


def test_a_canonical_subagent_key_gets_the_deny_list() -> None:
    """The consequence that matters: the tools an unattended run must not have.

    Classification is only a label; this is what the label was for.
    """
    ctx = effective_tool_context(session_key=CANONICAL)

    assert set(SUBAGENT_TOOL_DENY) <= ctx.denied_tools
    assert SUBAGENT_TOOL_DENY, "an empty deny list would make this test vacuous"


@pytest.mark.parametrize(
    "key",
    [
        CANONICAL,
        build_subagent_session_key("ops", "r1"),
        build_subagent_session_key("main", "a"),
        LEGACY,
        build_subagent_key(build_group_key("ops", "slack", "C1")),
    ],
    ids=["canonical", "canonical-other-agent", "short-run-id", "legacy", "legacy-group"],
)
def test_every_subagent_key_shape_is_recognised(key: str) -> None:
    """Canonical *and* legacy: the fix must not trade one for the other."""
    assert is_subagent_key(key) is True
    assert effective_tool_context(session_key=key).caller_kind is CallerKind.SUBAGENT


@pytest.mark.parametrize(
    "transform",
    [str.upper, str.title, lambda key: f"  {key}  ", lambda key: f"{key}\n"],
    ids=["upper", "title", "padded", "trailing-newline"],
)
def test_case_and_whitespace_do_not_change_the_verdict(transform) -> None:
    """``startswith`` on the raw string was also case- and whitespace-sensitive;
    ``is_subagent_key`` normalises, so the helper fixes this for free."""
    ctx = effective_tool_context(session_key=transform(CANONICAL))

    assert ctx.caller_kind is CallerKind.SUBAGENT


# ── the other kinds still classify correctly ────────────────────────────────


def test_a_cron_key_is_still_cron() -> None:
    ctx = effective_tool_context(session_key=build_cron_key("nightly", "r1"))

    assert ctx.caller_kind is CallerKind.CRON
    assert ctx.interaction_mode is InteractionMode.UNATTENDED


@pytest.mark.parametrize(
    "transform",
    [str.upper, lambda key: f"  {key}  "],
    ids=["upper", "padded"],
)
def test_a_cron_key_is_normalised_the_same_way(transform) -> None:
    """``is_cron_key`` is the counterpart added alongside, so a caller asking
    "what kind of session is this?" gets the same treatment either way."""
    ctx = effective_tool_context(session_key=transform(build_cron_key("nightly", "r1")))

    assert ctx.caller_kind is CallerKind.CRON


@pytest.mark.parametrize(
    "key",
    [build_main_key("main"), build_main_key("ops"), build_group_key("ops", "slack", "C1")],
    ids=["main", "main-other-agent", "group"],
)
def test_an_ordinary_key_is_an_interactive_agent(key: str) -> None:
    """The fix must not sweep ordinary sessions into SUBAGENT — that would take
    tools away from a user who is sitting right there.

    Asserted on the tools only a subagent loses. An interactive context already
    carries capability-based denials from ``resolve_runtime_tool_surface``
    (``cron``, ``gateway``, ``projects_*`` without a session manager), so an
    empty ``denied_tools`` is the wrong thing to check for.
    """
    ctx = effective_tool_context(session_key=key)

    assert ctx.caller_kind is CallerKind.AGENT
    assert ctx.interaction_mode is InteractionMode.INTERACTIVE
    assert "ask_user" not in ctx.denied_tools
    assert "publish_artifact" not in ctx.denied_tools
    assert not set(SUBAGENT_TOOL_DENY) <= ctx.denied_tools


def test_a_key_merely_mentioning_subagent_is_not_one() -> None:
    """``agent:main:slack:group:subagent-chat`` is a conversation about
    subagents, not a subagent run."""
    ctx = effective_tool_context(session_key=build_group_key("main", "slack", "subagent-chat"))

    assert ctx.caller_kind is CallerKind.AGENT


@pytest.mark.parametrize("key", ["", None], ids=["empty", "none"])
def test_no_key_falls_back_to_an_interactive_agent(key) -> None:
    ctx = effective_tool_context(session_key=key)

    assert ctx.caller_kind is CallerKind.AGENT
    assert ctx.agent_id == "main"


# ── an explicit caller_kind still wins ──────────────────────────────────────


def test_an_explicit_subagent_kind_wins_over_an_ordinary_key() -> None:
    ctx = effective_tool_context(session_key=build_main_key("ops"), caller_kind=CallerKind.SUBAGENT)

    assert ctx.caller_kind is CallerKind.SUBAGENT
    assert set(SUBAGENT_TOOL_DENY) <= ctx.denied_tools


def test_an_explicit_interaction_mode_overrides_the_default() -> None:
    """A subagent key defaults to UNATTENDED, but an explicit mode is the
    caller's to set."""
    ctx = effective_tool_context(
        session_key=CANONICAL, interaction_mode=InteractionMode.INTERACTIVE
    )

    assert ctx.caller_kind is CallerKind.SUBAGENT
    assert ctx.interaction_mode is InteractionMode.INTERACTIVE


def test_an_unknown_caller_kind_is_ignored_not_fatal() -> None:
    ctx = effective_tool_context(session_key=CANONICAL, caller_kind="not-a-kind")

    assert ctx.caller_kind is CallerKind.SUBAGENT


# ── agent_id is derived from the key, in every branch ───────────────────────


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (build_main_key("ops"), "ops"),
        (build_group_key("ops", "slack", "C1"), "ops"),
        (build_subagent_session_key("ops", "r1"), "ops"),
        (build_subagent_key(build_main_key("ops")), "ops"),
        (build_main_key("main"), "main"),
        (build_cron_key("nightly", "r1"), "main"),
    ],
    ids=["main-key", "group-key", "canonical-subagent", "legacy-subagent", "main-agent", "cron"],
)
def test_agent_id_comes_from_the_session_key(key: str, expected: str) -> None:
    """Hardcoding "main" sent an ``ops`` turn to main's memory and main's tool
    policy. Every branch derives it, not just the subagent one."""
    assert effective_tool_context(session_key=key).agent_id == expected


def test_an_explicit_agent_id_still_wins() -> None:
    """The caller knows better than the key when it says so."""
    ctx = effective_tool_context(session_key=build_main_key("ops"), agent_id="billing")

    assert ctx.agent_id == "billing"


def test_an_unparseable_key_still_falls_back_to_main() -> None:
    """``parse_agent_id`` already defaults to "main", so this change can only
    ever narrow a wrong answer into the right one — never widen it."""
    ctx = effective_tool_context(session_key="nonsense-key-with-no-structure")

    assert ctx.agent_id == "main"


# ── the shared helpers themselves ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (CANONICAL, True),
        (LEGACY, True),
        (build_subagent_session_key("ops", "r1"), True),
        (build_main_key("main"), False),
        (build_cron_key("n", "r"), False),
        ("agent:main:slack:group:subagent-chat", False),
        ("", False),
    ],
)
def test_is_subagent_key_matches_what_the_context_decides(key: str, expected: bool) -> None:
    """Pinned together so the two can never drift apart again — which is the
    defect this issue is, at root."""
    assert is_subagent_key(key) is expected
    if key:
        is_sub = effective_tool_context(session_key=key).caller_kind is CallerKind.SUBAGENT
        assert is_sub is expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (build_cron_key("nightly", "r1"), True),
        ("CRON:nightly:run:r1", True),
        ("  cron:n:run:r  ", True),
        (CANONICAL, False),
        (build_main_key("main"), False),
        ("", False),
    ],
)
def test_is_cron_key(key: str, expected: bool) -> None:
    assert is_cron_key(key) is expected
