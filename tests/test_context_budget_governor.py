from __future__ import annotations

from agentos.context_budget import (
    ContextBudgetClass,
    ContextBudgetGovernor,
)


def test_context_budget_governor_derives_large_window_caps() -> None:
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=200_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot()

    assert budget.provider_request_max_chars > 500_000
    assert budget.default_tool_argument_max_chars > 8_000
    assert budget.default_tool_result_provider_max_chars > 96_000
    assert budget.external_tool_result_provider_max_chars < (
        budget.default_tool_result_provider_max_chars
    )


def test_context_budget_governor_keeps_small_windows_guarded() -> None:
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=8_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot()

    assert 4_000 <= budget.provider_request_max_chars <= 32_000
    assert 2_000 <= budget.default_tool_argument_max_chars <= 16_000
    assert budget.default_tool_result_provider_max_chars <= 32_000


def test_context_budget_governor_honors_explicit_overrides() -> None:
    governor = ContextBudgetGovernor.from_values(
        context_window_tokens=200_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
        provider_request_proof_max_chars=123_456,
        tool_use_argument_provider_request_max_chars=12_345,
        tool_result_provider_request_max_chars=54_321,
    )

    budget = governor.snapshot()

    assert budget.provider_request_max_chars == 123_456
    assert governor.tool_argument_chars_for(ContextBudgetClass.LOCAL) == 12_345
    assert governor.tool_result_provider_chars_for(ContextBudgetClass.LOCAL) == 54_321


def test_context_budget_governor_external_caps_stay_stricter_than_local() -> None:
    governor = ContextBudgetGovernor.from_values(
        context_window_tokens=200_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    )

    assert governor.tool_argument_chars_for(ContextBudgetClass.EXTERNAL) < (
        governor.tool_argument_chars_for(ContextBudgetClass.LOCAL)
    )
    assert governor.tool_result_provider_chars_for(ContextBudgetClass.EXTERNAL) < (
        governor.tool_result_provider_chars_for(ContextBudgetClass.LOCAL)
    )
