"""Direct Anthropic (``llm.provider = "anthropic"``) model facts.

The Messages API spells ids with hyphens (``claude-opus-5-5``). The shared
registry is keyed on the gateways' dotted spellings, so before these facts were
wired in a direct Claude session fell back to a 200K window, a 16K output cap
and gateway-discounted (or default) prices.
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.engine.pricing import lookup_price
from agentos.provider.anthropic import AnthropicProvider
from agentos.provider.model_catalog import ModelCatalog

# Anthropic's published rates and limits (Models overview, 2026-10).
_EXPECTED = {
    # model_id: (context_window, max_output_tokens, input $/MTok, output $/MTok)
    "claude-opus-5-5": (1_000_000, 128_000, 4.0, 20.0),
    "claude-sonnet-5-5": (1_000_000, 128_000, 2.0, 10.0),
    "claude-haiku-5-5": (1_000_000, 128_000, 0.10, 0.50),
    "claude-fable-5-1": (1_000_000, 128_000, 10.0, 50.0),
    "claude-fable-5": (1_000_000, 128_000, 10.0, 50.0),
    "claude-opus-5": (1_000_000, 128_000, 5.0, 25.0),
    "claude-sonnet-5": (1_000_000, 128_000, 2.0, 10.0),
    "claude-opus-4-8": (1_000_000, 128_000, 5.0, 25.0),
    "claude-opus-4-7": (1_000_000, 128_000, 5.0, 25.0),
    "claude-opus-4-6": (1_000_000, 128_000, 5.0, 25.0),
    "claude-sonnet-4-6": (1_000_000, 128_000, 3.0, 15.0),
    "claude-haiku-4-5": (200_000, 64_000, 1.0, 5.0),
}


def test_model_list_offers_every_current_model() -> None:
    provider = AnthropicProvider(api_key="test", model="claude-opus-5-5")
    listed = {m.model_id for m in asyncio.run(provider.list_models())}
    assert set(_EXPECTED) <= listed


@pytest.mark.parametrize("model", sorted(_EXPECTED))
def test_direct_session_uses_the_model_windows(model: str) -> None:
    context_window, max_output, _, _ = _EXPECTED[model]
    catalog = ModelCatalog()
    assert catalog.resolve_context_window(model, "anthropic") == context_window
    assert catalog.resolve_max_tokens(model, provider_name="anthropic") == max_output


@pytest.mark.parametrize("model", sorted(_EXPECTED))
def test_direct_session_is_priced_at_anthropic_rates(model: str) -> None:
    _, _, input_per_m, output_per_m = _EXPECTED[model]
    price = lookup_price(model, provider_id="anthropic")
    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


def test_gateway_spellings_keep_their_own_rates() -> None:
    # The dotted ids belong to the gateways; the direct table must not touch them.
    assert lookup_price("claude-opus-5", provider_id="").input_per_m == pytest.approx(1.375)
