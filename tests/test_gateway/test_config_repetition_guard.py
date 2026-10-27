"""``[llm.repetition_guard]`` config section."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentos.engine.repetition_guard import (
    RepetitionGuardSettings,
    repetition_guard_settings_from,
)
from agentos.gateway.config import GatewayConfig


def test_repetition_guard_defaults_match_the_engine_defaults() -> None:
    config = GatewayConfig()

    assert config.llm.repetition_guard.enabled is True
    assert repetition_guard_settings_from(config.llm.repetition_guard) == (
        RepetitionGuardSettings()
    )


def test_repetition_guard_section_is_read() -> None:
    config = GatewayConfig.model_validate(
        {
            "llm": {
                "repetition_guard": {
                    "enabled": False,
                    "first_check_chars": 32_000,
                    "min_repeats": 10,
                    "min_coverage": 0.8,
                }
            }
        }
    )

    settings = repetition_guard_settings_from(config.llm.repetition_guard)
    assert settings.enabled is False
    assert settings.first_check_chars == 32_000
    assert settings.tail_chars == 64_000
    assert settings.min_repeats == 10
    assert settings.min_coverage == 0.8


@pytest.mark.parametrize(
    "section",
    [
        {"min_repeats": 1},
        {"min_coverage": 0},
        {"min_coverage": 1.5},
        {"window_chars": 2},
        {"first_check_chars": 10},
    ],
)
def test_repetition_guard_rejects_unworkable_thresholds(section: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({"llm": {"repetition_guard": section}})
