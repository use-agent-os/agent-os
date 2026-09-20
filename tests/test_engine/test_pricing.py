from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from agentos.engine import pricing
from agentos.engine.pricing import (
    ModelPrice,
    PriceEntry,
    PricingCache,
    _parse_opencap_prices,
    _parse_surplus_prices,
    calculate_cost_usd,
    lookup_price,
    reset_live_price_cache_for_tests,
    seed_live_price_cache_for_tests,
    seed_opencap_price_cache,
    seed_surplus_price_cache,
)


@pytest.fixture(autouse=True)
def reset_pricing_cache() -> Iterator[None]:
    reset_live_price_cache_for_tests()
    yield
    reset_live_price_cache_for_tests()


def test_deepseek_v4_pro_uses_non_discount_price_when_live_pricing_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price("deepseek/deepseek-v4-pro")

    assert price.input_per_m == pytest.approx(1.74)
    assert price.output_per_m == pytest.approx(3.48)


def test_opencap_unseeded_lookup_uses_static_fallback_without_outbound_io() -> None:
    with patch.object(
        pricing.httpx,
        "Client",
        side_effect=AssertionError("synchronous pricing lookup must not perform I/O"),
    ):
        price = lookup_price("minimax-m3", provider_id="opencap")

    assert price == PriceEntry(0.0825, 0.33)


def test_opencap_live_price_is_scoped_away_from_other_gateway_bare_ids() -> None:
    seed_opencap_price_cache(
        {
            "data": [
                {
                    "id": "minimax-m3",
                    "pricing": {"input": 0.2541, "output": 1.0164, "cachedInput": 0.05082},
                }
            ]
        }
    )

    opencap = lookup_price("minimax-m3", provider_id="opencap")
    other_gateway = lookup_price("minimax-m3")

    assert opencap.input_per_m == pytest.approx(0.2541)
    assert other_gateway.input_per_m == pytest.approx(0.0825)


def test_opencap_boot_catalog_seeds_pricing_without_a_second_fetch() -> None:
    count = seed_opencap_price_cache(
        {
            "data": [
                {
                    "id": "minimax-m3",
                    "pricing": {"input": 0.2541, "output": 1.0164, "cachedInput": 0.05082},
                }
            ]
        }
    )

    with patch.object(
        pricing.httpx,
        "Client",
        side_effect=AssertionError("pricing should reuse the async boot catalog"),
    ):
        price = lookup_price("minimax-m3", provider_id="opencap")

    assert count == 1
    assert price == PriceEntry(0.2541, 1.0164, 0.05082)


def test_opencap_pricing_rejects_non_finite_and_negative_catalog_rates() -> None:
    prices = _parse_opencap_prices(
        {
            "data": [
                {"id": "nan", "pricing": {"input": float("nan"), "output": 1.0}},
                {"id": "infinite", "pricing": {"input": 1.0, "output": float("inf")}},
                {"id": "negative", "pricing": {"input": -0.1, "output": 1.0}},
                {
                    "id": "valid",
                    "pricing": {
                        "input": 0.2,
                        "output": 0.8,
                        "cachedInput": float("-inf"),
                    },
                },
            ]
        }
    )

    assert prices == {"valid": PriceEntry(0.2, 0.8)}


def test_opencap_missing_catalog_entry_falls_back_without_failing_usage() -> None:
    price = lookup_price("oc-uncensored-1.0", provider_id="opencap")

    assert price.input_per_m == pytest.approx(0.20)
    assert price.output_per_m == pytest.approx(0.80)


def test_calculate_cost_usd_applies_cached_input_rate() -> None:
    price = PriceEntry(input_per_m=0.20, output_per_m=0.80, cached_input_per_m=0.10)

    cost = calculate_cost_usd(
        price,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cached_input_tokens=200_000,
    )

    assert cost == pytest.approx(0.98)


@pytest.mark.asyncio
async def test_pricing_cache_refresh_adds_openrouter_app_attribution() -> None:
    import httpx as _httpx

    cache = PricingCache(api_key="test-key", ttl_seconds=60)
    captured: dict[str, object] = {}
    mock_response = _httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "openai/gpt-4o",
                    "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
                }
            ]
        },
        request=_httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
    )

    with patch("agentos.engine.pricing.httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()

        async def capture_get(url, *, headers):
            captured["url"] = url
            captured["headers"] = headers
            return mock_response

        mock_instance.get = AsyncMock(side_effect=capture_get)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

        await cache.refresh()

    assert captured["url"] == "https://openrouter.ai/api/v1/models"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://useagentos.dev",
        "X-OpenRouter-Title": "AgentOS",
        "X-OpenRouter-Categories": "cli-agent,personal-agent",
    }
    price = cache.get_price_sync("openai/gpt-4o")
    assert price is not None
    assert price.input_per_token == 0.0000025
    assert price.output_per_token == 0.00001


def test_deepseek_v4_pro_override_wins_over_discounted_live_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "1")
    seed_live_price_cache_for_tests("deepseek/deepseek-v4-pro", PriceEntry(0.435, 0.87))

    price = lookup_price("deepseek/deepseek-v4-pro")

    assert price.input_per_m == pytest.approx(1.74)
    assert price.output_per_m == pytest.approx(3.48)


def test_deepseek_v4_pro_override_covers_versioned_openrouter_model_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "1")
    seed_live_price_cache_for_tests(
        "deepseek/deepseek-v4-pro-20260423",
        PriceEntry(0.435, 0.87),
    )

    price = lookup_price("deepseek/deepseek-v4-pro-20260423")

    assert price.input_per_m == pytest.approx(1.74)
    assert price.output_per_m == pytest.approx(3.48)


def test_pricing_cache_returns_non_discount_deepseek_v4_pro_price() -> None:
    cache = PricingCache(api_key="test")

    price = cache.get_price_sync("deepseek/deepseek-v4-pro")

    assert price is not None
    assert price.input_per_token == pytest.approx(1.74 / 1_000_000)
    assert price.output_per_token == pytest.approx(3.48 / 1_000_000)


def test_glm_5_1_static_price_matches_openrouter_native_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price("z-ai/glm-5.1")

    assert price.input_per_m == pytest.approx(1.40)
    assert price.output_per_m == pytest.approx(4.40)


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("qwen-plus", 0.115, 0.287),
        ("qwen-flash", 0.022, 0.216),
        ("qwen-turbo", 0.044, 0.087),
        ("qwen-max", 0.345, 1.377),
    ],
)
def test_dashscope_beijing_qwen_static_prices_match_official_model_studio_pricing(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


def test_dashscope_beijing_qwen_plus_smoke_usage_estimates_cost_from_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")
    price = lookup_price("qwen-plus")

    estimated_cost = (31 * price.input_per_m + 6 * price.output_per_m) / 1_000_000

    assert estimated_cost == pytest.approx(0.000005287)


def test_provider_profile_models_do_not_use_default_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")
    default = PriceEntry(3.0, 15.0)
    models = [
        "qwen3.6-flash",
        "qwen3.6-plus",
        "qwen3-max",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "doubao-seed-1-6-flash-250828",
        "doubao-seed-1-6-251015",
        "doubao-seed-1-6-thinking-250715",
        "doubao-seed-2-0-mini-260215",
        "doubao-seed-2-0-lite-260215",
        "doubao-seed-2-0-pro-260215",
        "doubao-seed-2-0-code-preview-260215",
    ]

    for model in models:
        assert lookup_price(model) != default, model


def test_local_embedding_model_does_not_fetch_openrouter_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "1")

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("local embedding models should not hit OpenRouter pricing")

    monkeypatch.setattr("agentos.engine.pricing._fetch_openrouter_json_sync", fail_fetch)

    price = lookup_price("BAAI/bge-small-zh-v1.5")

    assert price.input_per_m == 0
    assert price.output_per_m == 0


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("gpt-5.4-nano", 0.20, 1.25),
        ("gpt-5.4-mini", 0.75, 4.50),
        ("gpt-5.5", 5.0, 30.0),
        ("glm-5", 0.72, 2.30),
        ("glm-5.1", 1.40, 4.40),
        ("kimi-k2.5", 0.3827, 1.72),
        ("kimi-k2.6", 0.95, 4.0),
    ],
)
def test_direct_provider_profile_estimate_prices_match_approved_static_entries(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("doubao-seed-2-0-mini-260215", 0.029, 0.287),
        ("doubao-seed-2-0-lite-260215", 0.086, 0.516),
        ("doubao-seed-2-0-pro-260215", 0.459, 2.294),
        ("doubao-seed-2-0-code-preview-260215", 0.459, 2.294),
    ],
)
def test_volcengine_seed_2_static_prices_match_under_32k_online_inference_pricing(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m"),
    [
        ("gpt-4.1", 2.0, 8.0),
        ("glm-4.5", 0.115, 0.287),
        ("kimi-k2.6", 0.95, 4.0),
        ("MiniMax-M2.7", 0.118, 0.99),
    ],
)
def test_direct_openai_zhipu_kimi_and_minimax_prices_do_not_fall_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    input_per_m: float,
    output_per_m: float,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model)

    assert price.input_per_m == pytest.approx(input_per_m)
    assert price.output_per_m == pytest.approx(output_per_m)


def test_zhipu_c0_default_is_priced_instead_of_hitting_the_generic_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price("glm-4.7-flashx")

    assert price.input_per_m == pytest.approx(0.07)
    assert price.output_per_m == pytest.approx(0.40)
    assert price != pricing._DEFAULT_PRICING


def test_every_router_tier_default_has_explicit_price_and_catalog_entries() -> None:
    """No shipped tier default may rely on a prefix match or a generic default.

    Both tables fail open: pricing falls through a ``startswith`` scan to an
    older model or ``_DEFAULT_PRICING``, and the catalog falls through an
    exact-key miss to ``DEFAULT_CONTEXT_WINDOW``/``DEFAULT_MAX_TOKENS``. Neither
    logs, so a miss only shows up as a plausible-looking wrong number.

    Both tables are now derived from the registry, so this asks the question one
    level up: is the model declared, and declared completely? ``config.py``
    raises on an undeclared id at import time, which makes the first assertion
    belt-and-braces -- the price and window assertions are the ones with teeth.
    """
    from agentos import model_registry
    from agentos.gateway.config import (
        ROUTER_TIER_PROFILE_IDS,
        _router_tier_profile_defaults,
    )

    tier_models = {
        str(tier["model"]).lower()
        for profile in ROUTER_TIER_PROFILE_IDS
        for tier in _router_tier_profile_defaults(profile).values()
        if tier.get("model")
    }

    assert tier_models, "expected at least one tier default to audit"

    undeclared = sorted(m for m in tier_models if model_registry.by_id(m) is None)
    assert not undeclared, f"tier defaults not declared in the model registry: {undeclared}"

    unpriced = sorted(
        m for m in tier_models if (facts := model_registry.by_id(m)) and facts.price is None
    )
    assert not unpriced, f"tier defaults declared with no price: {unpriced}"

    windowless = sorted(
        m for m in tier_models if (facts := model_registry.by_id(m)) and facts.context_window <= 0
    )
    assert not windowless, f"tier defaults declared with no context window: {windowless}"


def _opencap_catalog_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "claude-opus-5",
                "pricing": {"input": 4.305, "output": 21.525, "cachedInput": 0.4305},
            }
        ]
    }


def test_opencap_cold_cache_refreshes_instead_of_using_another_gateways_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed boot seed must not pin OpenCAP to Bankr rates for the process."""
    monkeypatch.setenv("AGENTOS_OPENCAP_LIVE_PRICING", "1")
    calls: list[int] = []

    def fake_fetch() -> dict[str, object]:
        calls.append(1)
        return _opencap_catalog_payload()

    monkeypatch.setattr(pricing, "_fetch_opencap_catalog_sync", fake_fetch)

    price = lookup_price("claude-opus-5", provider_id="opencap")

    assert len(calls) == 1
    assert price.input_per_m == pytest.approx(4.305)
    assert price.output_per_m == pytest.approx(21.525)
    # The Bankr static entry for the same bare id is ~3x lower.
    assert lookup_price("claude-opus-5").input_per_m == pytest.approx(1.375)


def test_opencap_cache_is_not_refetched_while_still_within_its_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENCAP_LIVE_PRICING", "1")
    calls: list[int] = []

    def fake_fetch() -> dict[str, object]:
        calls.append(1)
        return _opencap_catalog_payload()

    monkeypatch.setattr(pricing, "_fetch_opencap_catalog_sync", fake_fetch)

    lookup_price("claude-opus-5", provider_id="opencap")
    lookup_price("claude-opus-5", provider_id="opencap")
    lookup_price("claude-opus-5", provider_id="opencap")

    assert len(calls) == 1


def test_opencap_boot_seed_counts_toward_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENCAP_LIVE_PRICING", "1")
    monkeypatch.setattr(
        pricing,
        "_fetch_opencap_catalog_sync",
        lambda: pytest.fail("a fresh boot seed must not trigger a refresh"),
    )

    seed_opencap_price_cache(_opencap_catalog_payload())
    price = lookup_price("claude-opus-5", provider_id="opencap")

    assert price.input_per_m == pytest.approx(4.305)


def test_opencap_unreachable_catalog_is_negative_cached_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENCAP_LIVE_PRICING", "1")
    calls: list[int] = []

    def failing_fetch() -> None:
        calls.append(1)
        return None

    monkeypatch.setattr(pricing, "_fetch_opencap_catalog_sync", failing_fetch)

    first = lookup_price("claude-opus-5", provider_id="opencap")
    second = lookup_price("claude-opus-5", provider_id="opencap")

    assert len(calls) == 1, "a failed fetch must be negative cached, not retried per lookup"
    assert first == second == PriceEntry(1.375, 6.875)


def test_opencap_static_fallback_is_reported_once_per_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[tuple[str, dict[str, object]]] = []

    class RecordingLog:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

        def info(self, event: str, **kwargs: object) -> None: ...

        def debug(self, event: str, **kwargs: object) -> None: ...

    monkeypatch.setattr(pricing, "log", RecordingLog())

    lookup_price("minimax-m3", provider_id="opencap")
    lookup_price("minimax-m3", provider_id="opencap")
    lookup_price("glm-5.2", provider_id="opencap")

    events = [
        kwargs["model"] for event, kwargs in warnings if event == "pricing.opencap_static_fallback"
    ]
    assert events == ["minimax-m3", "glm-5.2"]


def _surplus_catalog_payload() -> dict[str, object]:
    """Surplus publishes OpenRouter-shaped, USD-per-token rates as strings."""
    return {
        "data": [
            {
                "id": "claude-opus-5",
                "pricing": {
                    "prompt": "0.0000050000",
                    "completion": "0.0000250000",
                    "input_cache_read": "0.0000005000",
                },
            }
        ]
    }


def test_surplus_pricing_scales_per_token_catalog_rates_to_per_million() -> None:
    prices = _parse_surplus_prices(_surplus_catalog_payload())

    assert list(prices) == ["claude-opus-5"]
    entry = prices["claude-opus-5"]
    assert entry.input_per_m == pytest.approx(5.0)
    assert entry.output_per_m == pytest.approx(25.0)
    assert entry.cached_input_per_m == pytest.approx(0.5)


def test_surplus_pricing_rejects_non_finite_and_negative_catalog_rates() -> None:
    prices = _parse_surplus_prices(
        {
            "data": [
                {"id": "nan", "pricing": {"prompt": float("nan"), "completion": "0.000001"}},
                {"id": "infinite", "pricing": {"prompt": "0.000001", "completion": float("inf")}},
                {"id": "negative", "pricing": {"prompt": "-0.000001", "completion": "0.000001"}},
                {"id": "unpriced", "pricing": {}},
                {
                    "id": "valid",
                    "pricing": {
                        "prompt": "0.0000002",
                        "completion": "0.0000008",
                        "input_cache_read": float("-inf"),
                    },
                },
            ]
        }
    )

    assert list(prices) == ["valid"]
    assert prices["valid"].input_per_m == pytest.approx(0.2)
    assert prices["valid"].output_per_m == pytest.approx(0.8)
    assert prices["valid"].cached_input_per_m is None


def test_surplus_live_price_is_scoped_away_from_the_other_gateways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three gateways resell the same bare id at their own rates, so each
    cache has to answer only for its own provider."""
    monkeypatch.setenv("AGENTOS_SURPLUS_LIVE_PRICING", "1")
    monkeypatch.setattr(
        pricing,
        "_fetch_surplus_catalog_sync",
        lambda: pytest.fail("a fresh boot seed must not trigger a refresh"),
    )
    seed_surplus_price_cache(_surplus_catalog_payload())
    seed_opencap_price_cache(
        {"data": [{"id": "claude-opus-5", "pricing": {"input": 4.305, "output": 21.525}}]}
    )

    surplus = lookup_price("claude-opus-5", provider_id="surplus")
    opencap = lookup_price("claude-opus-5", provider_id="opencap")
    static = lookup_price("claude-opus-5")

    assert surplus.input_per_m == pytest.approx(5.0)
    assert opencap.input_per_m == pytest.approx(4.305)
    assert static.input_per_m == pytest.approx(1.375)


def test_surplus_cold_cache_refreshes_instead_of_using_another_gateways_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_SURPLUS_LIVE_PRICING", "1")
    calls: list[int] = []

    def fake_fetch() -> dict[str, object]:
        calls.append(1)
        return _surplus_catalog_payload()

    monkeypatch.setattr(pricing, "_fetch_surplus_catalog_sync", fake_fetch)

    price = lookup_price("claude-opus-5", provider_id="surplus")
    lookup_price("claude-opus-5", provider_id="surplus")

    assert len(calls) == 1, "the cache must not refetch while still within its TTL"
    assert price.output_per_m == pytest.approx(25.0)


def test_surplus_unreachable_catalog_is_negative_cached_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_SURPLUS_LIVE_PRICING", "1")
    calls: list[int] = []

    def failing_fetch() -> None:
        calls.append(1)
        return None

    monkeypatch.setattr(pricing, "_fetch_surplus_catalog_sync", failing_fetch)

    first = lookup_price("claude-opus-5", provider_id="surplus")
    second = lookup_price("claude-opus-5", provider_id="surplus")

    assert len(calls) == 1, "a failed fetch must be negative cached, not retried per lookup"
    assert first == second == PriceEntry(1.375, 6.875)


def test_surplus_live_pricing_can_be_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_SURPLUS_LIVE_PRICING", "0")
    monkeypatch.setattr(
        pricing,
        "_fetch_surplus_catalog_sync",
        lambda: pytest.fail("live pricing is disabled; no fetch may be issued"),
    )

    assert lookup_price("claude-opus-5", provider_id="surplus") == PriceEntry(1.375, 6.875)


def test_surplus_static_fallback_is_reported_once_per_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[tuple[str, dict[str, object]]] = []

    class RecordingLog:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

        def info(self, event: str, **kwargs: object) -> None: ...

        def debug(self, event: str, **kwargs: object) -> None: ...

    monkeypatch.setenv("AGENTOS_SURPLUS_LIVE_PRICING", "0")
    monkeypatch.setattr(pricing, "log", RecordingLog())

    lookup_price("minimax-m3", provider_id="surplus")
    lookup_price("minimax-m3", provider_id="surplus")
    lookup_price("glm-5.2", provider_id="surplus")

    events = [
        kwargs["model"] for event, kwargs in warnings if event == "pricing.surplus_static_fallback"
    ]
    assert events == ["minimax-m3", "glm-5.2"]


@pytest.mark.parametrize(
    ("model", "provider", "expected_input", "expected_output", "expected_cached"),
    [
        ("deepseek-chat", "deepseek", 0.14, 0.28, 0.014),
        ("deepseek-reasoner", "deepseek", 0.70, 2.50, 0.14),
        ("claude-3-7-sonnet", "anthropic", 3.0, 15.0, 0.30),
        ("claude-3-7-sonnet-20250219", "anthropic", 3.0, 15.0, 0.30),
        ("claude-3-5-sonnet-20241022", "anthropic", 3.0, 15.0, None),
        ("claude-3-5-haiku-20241022", "anthropic", 0.80, 4.0, None),
        ("claude-3-opus-20240229", "anthropic", 15.0, 75.0, None),
        ("gemini-2.5-flash", "gemini", 0.15, 0.60, None),
        ("gemini-2.5-pro", "gemini", 1.25, 10.0, None),
        ("gemini-2.0-flash", "gemini", 0.10, 0.40, 0.025),
        ("gemini-1.5-pro", "gemini", 1.25, 5.0, 0.3125),
        ("gpt-4o", "openai", 2.50, 10.0, None),
        ("gpt-4o-2024-08-06", "openai", 2.50, 10.0, None),
        ("gpt-4o-mini-2024-07-18", "openai", 0.15, 0.60, None),
        ("o3-mini", "openai", 1.10, 4.40, None),
        # Vendor-prefix normalisation mapping onto bare entries
        ("anthropic/claude-3-7-sonnet", "anthropic", 3.0, 15.0, 0.30),
        ("google/gemini-2.0-flash", "google", 0.10, 0.40, None),
        ("deepseek/deepseek-reasoner", "deepseek", 0.70, 2.50, 0.14),
        ("openai/gpt-4o", "openai", 2.50, 10.0, None),
    ],
)
def test_direct_provider_pricing_resolves_accurately(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    provider: str,
    expected_input: float,
    expected_output: float,
    expected_cached: float | None,
) -> None:
    monkeypatch.setenv("AGENTOS_OPENROUTER_LIVE_PRICING", "0")

    price = lookup_price(model, provider_id=provider)

    assert price.input_per_m == pytest.approx(expected_input)
    assert price.output_per_m == pytest.approx(expected_output)
    if expected_cached is not None:
        assert price.cached_input_per_m is not None
        assert price.cached_input_per_m == pytest.approx(expected_cached)
    else:
        assert price.cached_input_per_m is None


def test_calculate_cost_usd_with_deepseek_prompt_cache_hit() -> None:
    price = lookup_price("deepseek-chat", provider_id="deepseek")

    # 100k input tokens (80k cache hit + 20k regular), 10k output tokens
    cost = calculate_cost_usd(
        price,
        input_tokens=100_000,
        output_tokens=10_000,
        cached_input_tokens=80_000,
    )

    expected = (20_000 * 0.14 + 80_000 * 0.014 + 10_000 * 0.28) / 1_000_000
    assert cost == pytest.approx(expected)


def test_calculate_cost_usd_with_anthropic_prompt_cache_hit() -> None:
    price = lookup_price("claude-3-7-sonnet-20250219", provider_id="anthropic")

    # 50k input tokens (40k cache read + 10k regular), 5k output tokens
    cost = calculate_cost_usd(
        price,
        input_tokens=50_000,
        output_tokens=5_000,
        cached_input_tokens=40_000,
    )

    expected = (10_000 * 3.0 + 40_000 * 0.30 + 5_000 * 15.0) / 1_000_000
    assert cost == pytest.approx(expected)


def test_price_entry_calculate_cost_method() -> None:
    price = lookup_price("deepseek-chat", provider_id="deepseek")
    cost = price.calculate_cost(
        input_tokens=100_000,
        output_tokens=10_000,
        cached_input_tokens=80_000,
    )
    expected = (20_000 * 0.14 + 80_000 * 0.014 + 10_000 * 0.28) / 1_000_000
    assert cost == pytest.approx(expected)


def test_model_price_calculate_cost_method() -> None:
    model_price = ModelPrice(input_per_token=0.000002, output_per_token=0.000005)
    cost = model_price.calculate_cost(input_tokens=1_000, output_tokens=500)
    assert cost == pytest.approx(0.000002 * 1_000 + 0.000005 * 500)

