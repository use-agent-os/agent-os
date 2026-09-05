"""One declaration per model: price, windows, and capability flags.

A model's facts used to be restated in four places keyed four different ways --
tier defaults in ``agentos.gateway.config``, prices in ``agentos.engine.pricing``,
context and output windows in ``agentos.provider.model_catalog``, and the same
defaults again in ``agentos.toml.example``. Bumping a default meant editing all
of them by hand, and nothing checked that you did.

The failure mode was nasty because both lookup tables fail *open*, in different
directions: pricing prefix-matches down to an older model, and the catalog falls
through to a generic constant. A forgotten entry produced a plausible wrong
number, never an error. See issue #140 (and #139, where it actually happened).

This module is the single source. ``pricing.py`` and ``model_catalog.py`` derive
their tables from it, and ``config.py`` refuses at import time to ship a tier
default whose model is not declared here.

Deliberately **not** here:

* Generic prefix families in the pricing table (``gpt-4o``, ``claude-3-*``,
  ``ollama/``, embeddings). Those are not per-model facts and stay in
  ``pricing.py`` as an explicitly legacy prefix tail.
* ``description`` and ``thinking_level``. Those are per-profile routing policy,
  not properties of a model -- the same model is c2 at ``medium`` and c3 at
  ``high`` within one profile.
* Resolution policy constants (``DEFAULT_MAX_TOKENS`` and friends). Those belong
  to the catalog's fallback chain.

Stdlib only, on purpose: ``gateway.config`` imports this, and it must not pull in
``httpx`` or the provider package to read a tier default.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriceFacts:
    """USD per 1M tokens."""

    input_per_m: float
    output_per_m: float
    cached_input_per_m: float | None = None
    #: Wins over a live provider catalog. Set for gateway rates and for
    #: canonical rack rates that must not be replaced by a promotional or
    #: routed discount an aggregator happens to be advertising.
    beats_live_catalog: bool = False
    #: Where the number came from, for the next person who has to re-check it.
    source: str = ""


@dataclass(frozen=True, slots=True)
class ProviderWindowOverride:
    """Windows for one model id as served by specific providers.

    The same bare id is served by more than one endpoint with genuinely
    different limits -- a gateway caps output where the vendor's own API does
    not. That is legitimate, but it is indistinguishable from a typo unless
    somebody writes down why, so ``reason`` is required.
    """

    providers: tuple[str, ...]
    max_output_tokens: int
    context_window: int
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(f"provider window override for {self.providers} needs a reason")
        if not self.providers:
            raise ValueError("provider window override needs at least one provider")


@dataclass(frozen=True, slots=True)
class ModelFacts:
    """Everything the runtime needs to know about one model id.

    ``model_id`` is the id exactly as sent to the provider. Bare and
    vendor-prefixed spellings of the same model are separate entries, because
    they are separate wire ids; where their facts differ on purpose, say so in
    :data:`DELIBERATE_SPELLING_DIVERGENCES`.
    """

    model_id: str
    max_output_tokens: int
    context_window: int
    price: PriceFacts | None = None
    supports_image: bool = False
    provider_overrides: tuple[ProviderWindowOverride, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class SpellingDivergence:
    """A bare/vendor-prefixed pair whose facts differ on purpose.

    Issue #140's second ask. Without this list nothing separates "the gateway
    really does cap output lower" from "somebody updated one spelling and forgot
    the other", and the second one is silent.
    """

    bare_id: str
    prefixed_id: str
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(
                f"spelling divergence {self.bare_id}/{self.prefixed_id} needs a reason"
            )


def _gateway_windows(
    max_output_tokens: int, context_window: int, reason: str
) -> ProviderWindowOverride:
    """Bankr, OpenCAP and Surplus resell the same bare ids, so they override together."""
    return ProviderWindowOverride(
        providers=("bankr", "opencap", "surplus"),
        max_output_tokens=max_output_tokens,
        context_window=context_window,
        reason=reason,
    )


MODEL_FACTS: tuple[ModelFacts, ...] = (
    ModelFacts(
        "gpt-5.4-nano",
        max_output_tokens=128_000,
        context_window=400_000,
        price=PriceFacts(0.2, 1.25),
    ),
    ModelFacts(
        "gpt-5.4-mini",
        max_output_tokens=128_000,
        context_window=400_000,
        price=PriceFacts(0.75, 4.5),
    ),
    ModelFacts(
        "gpt-5.5",
        max_output_tokens=128_000,
        context_window=1_000_000,
        price=PriceFacts(5.0, 30.0),
    ),
    ModelFacts(
        "minimax/minimax-m2.7",
        max_output_tokens=8_192,
        context_window=196_608,
        price=PriceFacts(0.118, 0.99),
    ),
    ModelFacts(
        "minimax/minimax-m3",
        max_output_tokens=131_072,
        context_window=1_048_576,
        price=PriceFacts(0.0825, 0.33),
        supports_image=True,
    ),
    ModelFacts(
        "stepfun/step-3.5-flash",
        max_output_tokens=16_384,
        context_window=256_000,
        price=PriceFacts(0.1, 0.3),
    ),
    ModelFacts(
        "z-ai/glm-4.5-air",
        max_output_tokens=98_304,
        context_window=131_072,
        price=PriceFacts(0.13, 0.85),
    ),
    ModelFacts(
        "minimax/minimax-m2.5",
        max_output_tokens=65_536,
        context_window=196_608,
        price=PriceFacts(0.118, 0.99),
    ),
    ModelFacts(
        "openai/gpt-5.6-luna",
        max_output_tokens=128_000,
        context_window=1_050_000,
        price=PriceFacts(0.2, 1.25),
    ),
    ModelFacts(
        "deepseek/deepseek-v4-flash",
        max_output_tokens=16_384,
        context_window=1_048_576,
        price=PriceFacts(0.14, 0.28),
    ),
    ModelFacts(
        "deepseek/deepseek-v4-pro",
        max_output_tokens=16_384,
        context_window=1_048_576,
        price=PriceFacts(1.74, 3.48, beats_live_catalog=True),
    ),
    ModelFacts(
        "deepseek-v4-flash",
        max_output_tokens=393_216,
        context_window=1_048_576,
        price=PriceFacts(0.14, 0.28),
        provider_overrides=(
            _gateway_windows(
                128_000,
                1_000_000,
                "DeepSeek's own API serves 393K output for this id; the "
                "compatible gateways cap it at 128K, and sending the direct "
                "value over-asks and fails the request outright.",
            ),
        ),
    ),
    ModelFacts(
        "deepseek-v4-pro",
        max_output_tokens=393_216,
        context_window=1_048_576,
        price=PriceFacts(1.74, 3.48),
    ),
    ModelFacts(
        "deepseek/deepseek-v3.2",
        max_output_tokens=16_384,
        context_window=163_840,
        price=PriceFacts(0.26, 0.38),
    ),
    ModelFacts(
        "glm-4.7-flashx",
        max_output_tokens=128_000,
        context_window=200_000,
        price=PriceFacts(0.07, 0.4),
    ),
    ModelFacts(
        "glm-5",
        max_output_tokens=128_000,
        context_window=200_000,
        price=PriceFacts(0.72, 2.3),
    ),
    ModelFacts(
        "glm-5.1",
        max_output_tokens=128_000,
        context_window=200_000,
        price=PriceFacts(1.4, 4.4),
    ),
    ModelFacts(
        "z-ai/glm-5",
        max_output_tokens=80_000,
        context_window=80_000,
        price=PriceFacts(0.72, 2.3),
    ),
    ModelFacts(
        "z-ai/glm-5.1",
        max_output_tokens=202_752,
        context_window=202_752,
        price=PriceFacts(1.4, 4.4),
    ),
    ModelFacts(
        "z-ai/glm-5.2",
        max_output_tokens=131_072,
        context_window=1_048_576,
        price=PriceFacts(0.132, 0.429),
    ),
    ModelFacts(
        "moonshot-v1-8k",
        max_output_tokens=8_192,
        context_window=8_192,
    ),
    ModelFacts(
        "moonshot-v1-32k",
        max_output_tokens=32_768,
        context_window=32_768,
    ),
    ModelFacts(
        "moonshot-v1-128k",
        max_output_tokens=131_072,
        context_window=131_072,
    ),
    ModelFacts(
        "kimi-k2.5",
        max_output_tokens=32_768,
        context_window=262_144,
        price=PriceFacts(0.3827, 1.72),
        supports_image=True,
    ),
    ModelFacts(
        "kimi-k2.6",
        max_output_tokens=32_768,
        context_window=262_144,
        price=PriceFacts(0.95, 4.0),
        supports_image=True,
        provider_overrides=(
            _gateway_windows(
                65_536,
                256_000,
                "Moonshot direct serves 32K output / 262K context; the "
                "gateways list 64K output / 256K context for the same bare id.",
            ),
        ),
    ),
    ModelFacts(
        "moonshotai/kimi-k2.6",
        max_output_tokens=16_384,
        context_window=262_142,
        price=PriceFacts(0.95, 4.0),
        note=(
            "Carried over as the catalog's generic DEFAULT_MAX_TOKENS: the "
            "provider publishes no max output for this id."
        ),
    ),
    ModelFacts(
        "moonshotai/kimi-k2.5",
        max_output_tokens=65_535,
        context_window=262_144,
        price=PriceFacts(0.3827, 1.72),
    ),
    ModelFacts(
        "oc-uncensored-1.0",
        max_output_tokens=16_384,
        context_window=262_144,
        price=PriceFacts(0.2, 0.8, beats_live_catalog=True),
        note=(
            "Carried over as the catalog's generic DEFAULT_MAX_TOKENS: the "
            "provider publishes no max output for this id."
        ),
    ),
    ModelFacts(
        "glm-5.2",
        max_output_tokens=131_072,
        context_window=1_048_576,
        price=PriceFacts(0.132, 0.429, beats_live_catalog=True),
    ),
    ModelFacts(
        "glm-5.3",
        max_output_tokens=131_072,
        context_window=1_310_720,
        price=PriceFacts(1.54, 4.84),
    ),
    ModelFacts(
        "glm-5.3-flash",
        max_output_tokens=131_072,
        context_window=1_310_720,
        price=PriceFacts(0.0825, 0.275),
        supports_image=True,
    ),
    ModelFacts(
        "minimax-m3",
        max_output_tokens=131_072,
        context_window=1_048_576,
        price=PriceFacts(0.0825, 0.33, beats_live_catalog=True),
        supports_image=True,
    ),
    ModelFacts(
        "qwen3.6-flash",
        max_output_tokens=65_536,
        context_window=1_000_000,
        price=PriceFacts(0.029, 0.287),
    ),
    ModelFacts(
        "qwen3.7-max",
        max_output_tokens=32_768,
        context_window=256_000,
        price=PriceFacts(1.056, 3.168, beats_live_catalog=True),
    ),
    ModelFacts(
        "qwen3.7-plus",
        max_output_tokens=32_768,
        context_window=256_000,
        price=PriceFacts(0.115, 0.688),
    ),
    ModelFacts(
        "claude-opus-5",
        max_output_tokens=128_000,
        context_window=1_000_000,
        price=PriceFacts(1.375, 6.875, beats_live_catalog=True),
    ),
    ModelFacts(
        "anthropic/claude-opus-5",
        max_output_tokens=128_000,
        context_window=1_000_000,
        price=PriceFacts(5.0, 25.0),
    ),
    ModelFacts(
        "claude-opus-4.8",
        max_output_tokens=128_000,
        context_window=1_000_000,
        price=PriceFacts(1.375, 6.875, beats_live_catalog=True),
    ),
    ModelFacts(
        "claude-sonnet-5",
        max_output_tokens=64_000,
        context_window=1_000_000,
        price=PriceFacts(2.2, 11.0, beats_live_catalog=True),
    ),
    ModelFacts(
        "claude-sonnet-4.6",
        max_output_tokens=64_000,
        context_window=1_000_000,
        price=PriceFacts(0.825, 4.125, beats_live_catalog=True),
    ),
    ModelFacts(
        "claude-fable-5",
        max_output_tokens=128_000,
        context_window=1_000_000,
        price=PriceFacts(6.27, 31.35, beats_live_catalog=True),
    ),
    ModelFacts(
        "claude-haiku-4.5",
        max_output_tokens=64_000,
        context_window=200_000,
    ),
    ModelFacts(
        "gemini-3.1-flash-lite",
        max_output_tokens=65_536,
        context_window=1_000_000,
        price=PriceFacts(0.1, 0.4),
    ),
    ModelFacts(
        "gemini-3.5-flash",
        max_output_tokens=65_536,
        context_window=1_000_000,
        price=PriceFacts(0.275, 1.375, beats_live_catalog=True),
    ),
    ModelFacts(
        "gemini-3.1-pro-preview",
        max_output_tokens=32_768,
        context_window=1_000_000,
        price=PriceFacts(1.25, 10.0),
    ),
    ModelFacts(
        "gemini-2.5-pro",
        max_output_tokens=65_536,
        context_window=1_048_576,
        price=PriceFacts(1.25, 10.0),
    ),
    ModelFacts(
        "grok-4.3",
        max_output_tokens=128_000,
        context_window=1_000_000,
        price=PriceFacts(0.34375, 0.6875, beats_live_catalog=True),
    ),
    ModelFacts(
        "grok-4.5",
        max_output_tokens=16_384,
        context_window=500_000,
        note=(
            "Carried over as the catalog's generic DEFAULT_MAX_TOKENS: the "
            "provider publishes no max output for this id."
        ),
    ),
    ModelFacts(
        "grok-4.6",
        max_output_tokens=450_000,
        context_window=500_000,
        price=PriceFacts(2.2, 6.6),
        supports_image=True,
    ),
    ModelFacts(
        "kimi-k2.7-code",
        max_output_tokens=262_144,
        context_window=262_144,
    ),
    ModelFacts(
        "kimi-k3",
        max_output_tokens=943_718,
        context_window=1_048_576,
        price=PriceFacts(3.0, 15.0),
        supports_image=True,
    ),
    ModelFacts(
        "muse-spark-1.2",
        max_output_tokens=943_718,
        context_window=1_048_576,
        price=PriceFacts(1.25, 4.25),
        supports_image=True,
    ),
    ModelFacts(
        "gpt-5.6-luna",
        max_output_tokens=128_000,
        context_window=1_050_000,
        price=PriceFacts(0.2, 1.25),
    ),
    ModelFacts(
        "gpt-5.6-terra",
        max_output_tokens=128_000,
        context_window=1_050_000,
        price=PriceFacts(0.75, 4.5),
    ),
    ModelFacts(
        "gpt-5.6-sol",
        max_output_tokens=128_000,
        context_window=1_050_000,
        price=PriceFacts(5.0, 30.0),
    ),
    ModelFacts(
        "gpt-5.6-luna-pro",
        max_output_tokens=128_000,
        context_window=1_050_000,
    ),
    ModelFacts(
        "gpt-5.6-terra-pro",
        max_output_tokens=128_000,
        context_window=1_050_000,
    ),
    ModelFacts(
        "gpt-5.6-sol-pro",
        max_output_tokens=128_000,
        context_window=1_050_000,
    ),
    ModelFacts(
        "doubao-seed-2-0-mini-260215",
        max_output_tokens=32_768,
        context_window=256_000,
        price=PriceFacts(0.029, 0.287),
    ),
    ModelFacts(
        "doubao-seed-2-0-lite-260215",
        max_output_tokens=32_768,
        context_window=256_000,
        price=PriceFacts(0.086, 0.516),
    ),
    ModelFacts(
        "doubao-seed-2-0-pro-260215",
        max_output_tokens=32_768,
        context_window=256_000,
        price=PriceFacts(0.459, 2.294),
    ),
    ModelFacts(
        "doubao-seed-2-0-code-preview-260215",
        max_output_tokens=32_768,
        context_window=256_000,
        price=PriceFacts(0.459, 2.294),
    ),
)


# Issue #140's second ask. A bare id and its vendor-prefixed twin are different
# wire ids served by different endpoints, so their facts are allowed to differ --
# but only on purpose. Every difference the tables actually contain must appear
# here with a reason; anything else is drift, and the test enforces set equality
# in both directions so a stale entry is caught too.
DELIBERATE_SPELLING_DIVERGENCES: tuple[SpellingDivergence, ...] = (
    SpellingDivergence(
        "deepseek-v4-flash",
        "deepseek/deepseek-v4-flash",
        "DeepSeek direct publishes 393K max output; the OpenRouter route reports the generic 16K.",
    ),
    SpellingDivergence(
        "deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
        "Same split as deepseek-v4-flash: direct 393K output, routed 16K.",
    ),
    SpellingDivergence(
        "glm-5",
        "z-ai/glm-5",
        "Zhipu direct serves a 200K context; the OpenRouter route publishes 80K.",
    ),
    SpellingDivergence(
        "glm-5.1",
        "z-ai/glm-5.1",
        "Zhipu direct serves a 200K context; the OpenRouter route publishes "
        "202752 for both output and context.",
    ),
    SpellingDivergence(
        "kimi-k2.5",
        "moonshotai/kimi-k2.5",
        "Moonshot direct caps output at 32K; the OpenRouter route publishes 65535.",
    ),
    SpellingDivergence(
        "kimi-k2.6",
        "moonshotai/kimi-k2.6",
        "The OpenRouter route reports no max output (so the generic 16K stands) "
        "and a 262142 context, two off the direct 262144.",
    ),
    SpellingDivergence(
        "claude-opus-5",
        "anthropic/claude-opus-5",
        "The bare id is served by the Bankr/OpenCAP gateways at their discounted "
        "rate; the prefixed id is Anthropic's published rack rate.",
    ),
    SpellingDivergence(
        "claude-opus-4.8",
        "anthropic/claude-opus-4.8",
        "Same gateway-discount relationship as claude-opus-5. The prefixed id is "
        "not a registry member -- its rack rate lives in the pricing prefix tail.",
    ),
)


_BY_ID: dict[str, ModelFacts] = {facts.model_id: facts for facts in MODEL_FACTS}


def by_id(model_id: str) -> ModelFacts | None:
    """Look up a model's declared facts, or ``None`` if it is not declared."""
    return _BY_ID.get(str(model_id or "").strip().lower())


def supports_image(model_id: str) -> bool:
    facts = by_id(model_id)
    return bool(facts and facts.supports_image)


def static_windows() -> dict[str, tuple[int, int]]:
    """``model_id -> (max_output_tokens, context_window)`` for every model."""
    return {
        facts.model_id: (facts.max_output_tokens, facts.context_window) for facts in MODEL_FACTS
    }


def provider_windows() -> dict[str, dict[str, tuple[int, int]]]:
    """``provider -> {model_id: (max_output_tokens, context_window)}``.

    Inverted from each model's ``provider_overrides``, so a provider-specific
    window is declared next to the model it belongs to rather than in a separate
    table someone has to remember to update.
    """
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for facts in MODEL_FACTS:
        for override in facts.provider_overrides:
            for provider in override.providers:
                out.setdefault(provider, {})[facts.model_id] = (
                    override.max_output_tokens,
                    override.context_window,
                )
    return out


def _price_rows(only_live_beating: bool) -> list[tuple[str, PriceFacts]]:
    rows = [
        (facts.model_id, facts.price)
        for facts in MODEL_FACTS
        if facts.price is not None and (facts.price.beats_live_catalog or not only_live_beating)
    ]
    # Longest id first. Both pricing lists are ``startswith`` scans where the
    # first match wins, so specificity used to depend on hand-maintained ordering
    # guarded by a comment ("must precede any shorter glm-4.7 prefix"). Sorting
    # here makes that structural: declaration order above is free, and a newly
    # added shorter id can no longer swallow a longer one.
    rows.sort(key=lambda row: (-len(row[0]), row[0]))
    return rows


def price_override_rows() -> list[tuple[str, PriceFacts]]:
    """Prices that beat a live provider catalog, most specific id first."""
    return _price_rows(only_live_beating=True)


def exact_price_rows() -> list[tuple[str, PriceFacts]]:
    """Every declared price, most specific id first."""
    return _price_rows(only_live_beating=False)
