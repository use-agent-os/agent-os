from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentos.engine.pricing import (
    PriceEntry,
    PricingCache,
    _parse_opencap_prices,
    calculate_cost_usd,
    lookup_price,
    reset_live_price_cache_for_tests,
    seed_live_price_cache_for_tests,
    seed_opencap_price_cache,
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


def test_opencap_pricing_fetches_public_catalog_and_caches_by_model() -> None:
    import httpx as _httpx

    response = _httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "minimax-m3",
                    "pricing": {
                        "input": 0.2541,
                        "output": 1.0164,
                        "cachedInput": 0.05082,
                    },
                }
            ]
        },
        request=_httpx.Request("GET", "https://gw.capminal.ai/api/public/models"),
    )

    with patch("agentos.engine.pricing.httpx.Client") as mock_client:
        client = MagicMock()
        client.get.return_value = response
        mock_client.return_value.__enter__.return_value = client

        first = lookup_price("minimax-m3", provider_id="opencap")
        second = lookup_price("minimax-m3", provider_id="opencap")

    client.get.assert_called_once_with(
        "https://gw.capminal.ai/api/public/models",
        headers={"Accept": "application/json"},
    )
    assert first == second
    assert first.input_per_m == pytest.approx(0.2541)
    assert first.output_per_m == pytest.approx(1.0164)
    assert first.cached_input_per_m == pytest.approx(0.05082)


def test_opencap_live_price_is_scoped_away_from_other_gateway_bare_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agentos.engine.pricing._fetch_opencap_prices_sync",
        lambda: {"minimax-m3": PriceEntry(0.2541, 1.0164, 0.05082)},
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

    with patch(
        "agentos.engine.pricing._fetch_opencap_prices_sync",
        side_effect=AssertionError("pricing should reuse the boot catalog"),
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


def test_opencap_live_pricing_failure_falls_back_without_failing_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fetch() -> dict[str, PriceEntry]:
        raise RuntimeError("offline")

    monkeypatch.setattr("agentos.engine.pricing._fetch_opencap_prices_sync", fail_fetch)

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
