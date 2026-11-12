"""Invariants for the single model declaration (issue #140).

Model facts used to live in four tables keyed four different ways, and nothing
tied them together. They are now derived from ``agentos.model_registry``; these
tests guard the properties that derivation alone does not give you -- that the
declaration is well formed, that the derived pricing rows cannot shadow each
other, that the legacy prefix tail and the registry stay disjoint, and that
every bare/vendor-prefixed spelling split is deliberate rather than forgotten.
"""

from __future__ import annotations

import pytest

from agentos import model_registry
from agentos.engine import pricing
from agentos.gateway.config import ROUTER_TIER_PROFILE_IDS, _router_tier_profile_defaults, _tier
from agentos.model_registry import ProviderWindowOverride, SpellingDivergence
from agentos.provider.model_catalog import _PROVIDER_STATIC_FALLBACK, _STATIC_FALLBACK


def _bare(model_id: str) -> str:
    """Strip the vendor namespace: ``z-ai/glm-5.2`` -> ``glm-5.2``."""
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def test_every_model_is_declared_once_lowercase_and_with_positive_windows() -> None:
    """Lookups lowercase the incoming id, so an upper-case key is unreachable."""
    ids = [facts.model_id for facts in model_registry.MODEL_FACTS]

    assert len(ids) == len(set(ids)), "duplicate model ids in the registry"
    for facts in model_registry.MODEL_FACTS:
        assert facts.model_id == facts.model_id.lower(), facts.model_id
        assert facts.model_id.strip() == facts.model_id, facts.model_id
        assert facts.max_output_tokens > 0, facts.model_id
        assert facts.context_window > 0, facts.model_id


def test_catalog_fallback_tables_are_derived_from_the_registry() -> None:
    assert _STATIC_FALLBACK == model_registry.static_windows()
    assert _PROVIDER_STATIC_FALLBACK == model_registry.provider_windows()


def test_pricing_tables_are_derived_from_the_registry() -> None:
    """The override list is the set of prices that beat a live provider catalog.

    Membership matters beyond the numbers: an override also *bypasses* the live
    fetch, so adding or dropping one changes online behavior, not just an
    offline estimate.
    """
    override_ids = [model_id for model_id, _ in pricing._PRICE_OVERRIDES]
    declared = [model_id for model_id, _ in model_registry.price_override_rows()]

    assert override_ids == declared

    registry_prices = {
        model_id: (facts.input_per_m, facts.output_per_m, facts.cached_input_per_m)
        for model_id, facts in model_registry.exact_price_rows()
    }
    table_prices = {
        prefix: (entry.input_per_m, entry.output_per_m, entry.cached_input_per_m)
        for prefix, entry in pricing._PRICING_TABLE
        if prefix in registry_prices
    }

    assert table_prices == registry_prices


def test_no_pricing_row_is_shadowed_by_an_earlier_row_with_a_different_price() -> None:
    """Generalizes the hand-written "must precede any shorter glm-4.7 prefix".

    ``_lookup_static_price`` takes the first ``startswith`` match, so a shorter
    prefix sitting earlier silently prices every longer id that shares it. Three
    rows were in exactly that state before the registry landed -- ``glm-5.2``
    under ``glm-5``, ``claude-opus-4.8`` under ``claude-opus-4``, and
    ``claude-sonnet-4.6`` under ``claude-sonnet-4`` -- and only produced the
    right number because an override happened to rescue them.
    """
    shadowed: list[str] = []
    seen: list[tuple[str, tuple[float, float]]] = []
    for prefix, entry in pricing._PRICING_TABLE:
        value = (entry.input_per_m, entry.output_per_m)
        for earlier, earlier_value in seen:
            if prefix.startswith(earlier) and value != earlier_value:
                how = "duplicated by" if prefix == earlier else "unreachable behind"
                shadowed.append(f"{prefix} is {how} {earlier}")
        seen.append((prefix, value))

    assert not shadowed, shadowed


def test_the_legacy_prefix_tail_never_restates_a_declared_model() -> None:
    """Stops a "keep both lists in sync" block from growing back.

    The tail is for prefix *families* -- model generations, vendor namespaces,
    ``ollama/``. A row there whose prefix is exactly a declared model id is a
    second declaration of that model, which is the whole bug.
    """
    declared = {facts.model_id for facts in model_registry.MODEL_FACTS}
    duplicated = sorted(
        prefix for prefix, _ in pricing._LEGACY_PRICING_PREFIXES if prefix in declared
    )

    assert not duplicated, f"declared models restated in the legacy prefix tail: {duplicated}"


def test_every_bare_and_prefixed_spelling_split_is_declared_deliberate() -> None:
    """Issue #140's second ask.

    A gateway genuinely serving a different output cap than the vendor's own API
    is legitimate; one spelling being updated and the other forgotten is a silent
    bug. They look identical in the tables, so every split must be written down
    with a reason -- and the comparison runs both ways, so an allowlist entry
    left behind after the two spellings converged fails too.
    """
    facts_by_id = {facts.model_id: facts for facts in model_registry.MODEL_FACTS}
    prices = dict(pricing._PRICING_TABLE)

    def windows(model_id: str) -> tuple[int, int] | None:
        facts = facts_by_id.get(model_id)
        return None if facts is None else (facts.max_output_tokens, facts.context_window)

    def price(model_id: str) -> tuple[float, float] | None:
        entry = prices.get(model_id)
        return None if entry is None else (entry.input_per_m, entry.output_per_m)

    # Both spellings of a pair may be declared models, or one may only carry a
    # price in the legacy tail. Each fact is compared only where both spellings
    # actually state it: "one side declares windows and the other does not" is
    # not a divergence, it just means one spelling is not a shipped model.
    known = {facts.model_id for facts in model_registry.MODEL_FACTS}
    known |= {prefix for prefix, _ in pricing._LEGACY_PRICING_PREFIXES}

    observed: set[tuple[str, str]] = set()
    for model_id in known:
        if "/" not in model_id:
            continue
        bare = _bare(model_id)
        if bare not in known:
            continue
        both = (bare, model_id)
        window_split = all(windows(i) is not None for i in both) and windows(bare) != windows(
            model_id
        )
        price_split = all(price(i) is not None for i in both) and price(bare) != price(model_id)
        if window_split or price_split:
            observed.add(both)

    declared = {(d.bare_id, d.prefixed_id) for d in model_registry.DELIBERATE_SPELLING_DIVERGENCES}

    assert observed - declared == set(), (
        "undeclared bare/prefixed divergence -- deliberate, or drift? "
        f"{sorted(observed - declared)}"
    )
    assert declared - observed == set(), (
        f"declared divergences whose facts now agree; drop them: {sorted(declared - observed)}"
    )


def test_a_provider_window_override_cannot_be_declared_without_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        ProviderWindowOverride(
            providers=("bankr",), max_output_tokens=1, context_window=2, reason="  "
        )
    with pytest.raises(ValueError, match="provider"):
        ProviderWindowOverride(
            providers=(), max_output_tokens=1, context_window=2, reason="because"
        )


def test_a_spelling_divergence_cannot_be_declared_without_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        SpellingDivergence("glm-5.2", "z-ai/glm-5.2", "")


def test_every_tier_default_takes_supports_image_from_the_registry() -> None:
    """Text tiers read the flag from the registry. An ``image_only`` tier always
    carries it: a vision-capable model that doubles as a text tier is declared
    ``supports_image=False`` there (``gpt-6-luna``), and the image tier must
    still take the image."""
    for profile in ROUTER_TIER_PROFILE_IDS:
        for name, tier in _router_tier_profile_defaults(profile).items():
            model = str(tier["model"])
            expected = bool(tier.get("image_only")) or model_registry.supports_image(model)
            assert tier["supports_image"] == expected, f"{profile}.{name} ({model})"


def test_a_tier_cannot_be_built_from_an_undeclared_model() -> None:
    """The import-time guard: bumping a default to an unknown id fails loudly."""
    with pytest.raises(ValueError, match="model_registry"):
        _tier(provider="openrouter", model="vendor/not-a-real-model", description="nope")
