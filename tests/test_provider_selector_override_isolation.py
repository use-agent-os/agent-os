"""``ModelSelector.override_model`` must keep per-turn fallbacks off the shared config.

Regression tests for issue #1717: ``clone()`` passes the base ``SelectorConfig``
by reference and ``__init__`` rebuilds the chain from ``config.fallbacks``, so a
turn that overrode its fallbacks used to leak that chain into every clone made
afterward.
"""

from __future__ import annotations

from agentos.provider.selector import ModelSelector, ProviderConfig, SelectorConfig


def _base_config() -> SelectorConfig:
    return SelectorConfig(
        primary=ProviderConfig("anthropic", "claude-sonnet-4-6", api_key="k"),
        fallbacks=[ProviderConfig("openai", "gpt-5", api_key="k")],
    )


def _fallback_models(selector: ModelSelector) -> list[str]:
    return [cfg.model for cfg in selector._chain[1:]]


def test_override_fallbacks_do_not_leak_into_later_clones() -> None:
    config = _base_config()
    original = ModelSelector(config)

    turn_a = original.clone()
    turn_a.override_model("claude-opus-4-7", fallbacks=[ProviderConfig("ollama", "llama3")])
    assert _fallback_models(turn_a) == ["llama3"]

    # The shared config object is untouched...
    assert [cfg.model for cfg in config.fallbacks] == ["gpt-5"]
    # ...so an unrelated later turn starts from the configured chain.
    turn_b = original.clone()
    assert _fallback_models(turn_b) == ["gpt-5"]
    assert _fallback_models(original) == ["gpt-5"]


def test_override_fallbacks_on_base_selector_do_not_leak_into_clones() -> None:
    config = _base_config()
    original = ModelSelector(config)
    original.override_model("", fallbacks=[ProviderConfig("ollama", "llama3")])

    assert _fallback_models(original) == ["llama3"]
    assert [cfg.model for cfg in config.fallbacks] == ["gpt-5"]


def test_override_fallbacks_still_govern_failover_within_the_turn() -> None:
    """The override is stored privately, but must still drive ``next_fallback_after_failure``."""
    turn = ModelSelector(_base_config()).clone()
    turn.override_model("claude-opus-4-7", fallbacks=[ProviderConfig("opencap", "glm-5.3")])

    turn.next_fallback_after_failure(RuntimeError("primary down"))

    assert turn.active_provider_id == "opencap"
    assert turn.current_config.model == "glm-5.3"


def test_override_model_without_fallbacks_keeps_configured_chain() -> None:
    config = _base_config()
    turn = ModelSelector(config).clone()
    turn.override_model("claude-opus-4-7")

    assert turn.current_config.model == "claude-opus-4-7"
    assert _fallback_models(turn) == ["gpt-5"]
    assert config.primary.model == "claude-sonnet-4-6"


def test_sync_primary_on_base_selector_still_reaches_future_clones() -> None:
    """Copy-on-write must not break the documented ``sync_primary`` -> clone contract."""
    original = ModelSelector(_base_config())
    original.sync_primary(ProviderConfig("openai", "gpt-5.6", api_key="k2"))

    assert original.clone().current_config.model == "gpt-5.6"
