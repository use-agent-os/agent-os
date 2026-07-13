import re

import pytest

from agentos.cli.init_cmd import _default_model_for_provider, _env_key_for_provider
from agentos.onboarding import get_provider_setup_spec

_INIT_PROVIDER_CHOICES = (
    "openrouter",
    "bankr",
    "openai",
    "anthropic",
    "deepseek",
)


def test_init_uses_bankr_gateway_model_default() -> None:
    assert _default_model_for_provider("bankr") == "minimax-m3"


def test_init_uses_direct_deepseek_model_default() -> None:
    assert _default_model_for_provider("deepseek") == "deepseek-v4-flash"


def test_init_keeps_openrouter_model_default() -> None:
    assert _default_model_for_provider("openrouter") == "deepseek/deepseek-v4-pro"


def test_init_unknown_provider_falls_back_to_openai() -> None:
    assert _default_model_for_provider("custom") == "openai/gpt-4o-mini"


@pytest.mark.parametrize("provider", _INIT_PROVIDER_CHOICES)
def test_init_env_key_matches_the_variable_the_runtime_reads(provider: str) -> None:
    assert _env_key_for_provider(provider) == get_provider_setup_spec(provider).env_key


@pytest.mark.parametrize("provider", [*_INIT_PROVIDER_CHOICES, "custom", "bogus"])
def test_init_env_key_is_a_valid_shell_variable_name(provider: str) -> None:
    # A hyphenated provider id must not leak into the env var name: a
    # `FOO-BAR_API_KEY` cannot be exported and is never read back.
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _env_key_for_provider(provider))


def test_init_custom_and_unknown_providers_use_the_generic_key() -> None:
    assert _env_key_for_provider("custom") == "AGENTOS_LLM_API_KEY"
    assert _env_key_for_provider("bogus") == "AGENTOS_LLM_API_KEY"
