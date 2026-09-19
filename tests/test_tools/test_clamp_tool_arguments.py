from __future__ import annotations

from agentos.result_budget import (
    WEB_FETCH_MIN_MAX_CHARS,
    ToolRunBudgetPolicy,
    _parse_budget_int,
    clamp_tool_arguments,
)


def test_parse_budget_int() -> None:
    assert _parse_budget_int(42) == 42
    assert _parse_budget_int(42.0) == 42
    assert _parse_budget_int("42") == 42
    assert _parse_budget_int("  100  ") == 100
    assert _parse_budget_int(True) is None
    assert _parse_budget_int(False) is None
    assert _parse_budget_int(None) is None
    assert _parse_budget_int("invalid") is None
    assert _parse_budget_int([]) is None


def test_clamp_web_fetch_numeric_strings_and_bounds() -> None:
    policy = ToolRunBudgetPolicy(max_single_fetch_chars=50_000)

    # String integer exceeding cap is clamped to cap
    args = clamp_tool_arguments("web_fetch", {"max_chars": "100000"}, policy)
    assert args["max_chars"] == 50_000

    # String integer below minimum is clamped to WEB_FETCH_MIN_MAX_CHARS
    args = clamp_tool_arguments("web_fetch", {"max_chars": "25"}, policy)
    assert args["max_chars"] == WEB_FETCH_MIN_MAX_CHARS

    # None falls back to policy cap
    args = clamp_tool_arguments("web_fetch", {}, policy)
    assert args["max_chars"] == 50_000

    # Boolean is rejected and falls back to policy cap
    args = clamp_tool_arguments("web_fetch", {"max_chars": True}, policy)
    assert args["max_chars"] == 50_000

    # Invalid string falls back to policy cap
    args = clamp_tool_arguments("web_fetch", {"max_chars": "unlimited"}, policy)
    assert args["max_chars"] == 50_000


def test_clamp_web_search_numeric_strings_and_bounds() -> None:
    policy = ToolRunBudgetPolicy(max_web_search_results=10)

    # String integer exceeding cap is clamped to cap
    args = clamp_tool_arguments("web_search", {"max_results": "50"}, policy)
    assert args["max_results"] == 10

    # Value below 1 clamped to 1
    args = clamp_tool_arguments("web_search", {"max_results": "0"}, policy)
    assert args["max_results"] == 1

    # None falls back to cap
    args = clamp_tool_arguments("web_search", {}, policy)
    assert args["max_results"] == 10

    # Boolean rejected and falls back to cap
    args = clamp_tool_arguments("web_search", {"max_results": False}, policy)
    assert args["max_results"] == 10

    # Valid string in range
    args = clamp_tool_arguments("web_search", {"max_results": "5"}, policy)
    assert args["max_results"] == 5


def test_clamp_tool_arguments_unaffected_tool() -> None:
    policy = ToolRunBudgetPolicy()
    original = {"command": "echo test"}
    args = clamp_tool_arguments("exec_command", original, policy)
    assert args == original
