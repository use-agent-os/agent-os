"""Model pricing lookup for cost estimation + OpenRouter live cache."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Any, cast

import httpx
import structlog

from agentos import model_registry
from agentos.env import trust_env as _trust_env
from agentos.provider.openrouter_attribution import openrouter_app_headers
from agentos.provider.registry import get_provider_spec
from agentos.secrets import clean_header_secret

log = structlog.get_logger(__name__)

_CACHE_TTL = 3600  # 1 hour
_HTTP_TIMEOUT = 3.0
_OPENROUTER_PRICING_BASE_URL = "https://openrouter.ai/api/v1"
_LIVE_PRICE_MISS_TTL = 300


@dataclass
class ModelPrice:
    """Per-token cost for a model (USD)."""

    input_per_token: float
    output_per_token: float

    def calculate_cost(self, input_tokens: int = 0, output_tokens: int = 0) -> float:
        """Calculate total USD cost for the given input and output token counts."""
        safe_input = max(0, int(input_tokens))
        safe_output = max(0, int(output_tokens))
        return safe_input * self.input_per_token + safe_output * self.output_per_token


class PricingCache:
    """Fetches and caches model pricing from OpenRouter /api/v1/models."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        ttl_seconds: int = _CACHE_TTL,
    ) -> None:
        self._api_key = clean_header_secret(api_key, label="OpenRouter API key")
        self._base_url = base_url.rstrip("/")
        self._ttl = ttl_seconds
        self._cache: dict[str, ModelPrice] = {}
        self._fetched_at: float = 0

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self._fetched_at > self._ttl

    def get_price_sync(self, model_id: str) -> ModelPrice | None:
        """Get cached price without refreshing."""
        override = _lookup_price_override(model_id)
        if override is not None:
            return _model_price_from_entry(override)
        return self._cache.get(model_id)

    async def get_price(self, model_id: str) -> ModelPrice | None:
        """Get price, refreshing cache if stale."""
        override = _lookup_price_override(model_id)
        if override is not None:
            return _model_price_from_entry(override)
        if self.is_stale:
            await self.refresh()
        return self._cache.get(model_id)

    async def refresh(self) -> None:
        """Fetch model list from OpenRouter and update cache."""
        url = f"{self._base_url}/models"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        headers.update(openrouter_app_headers(self._base_url))
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=_trust_env()) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            new_cache: dict[str, ModelPrice] = {}
            for model in data.get("data", []):
                model_id = model.get("id", "")
                pricing = model.get("pricing", {})
                override = _lookup_price_override(model_id)
                if override is not None:
                    new_cache[model_id] = _model_price_from_entry(override)
                    continue
                prompt_cost = pricing.get("prompt")
                completion_cost = pricing.get("completion")
                if prompt_cost is not None and completion_cost is not None:
                    try:
                        new_cache[model_id] = ModelPrice(
                            input_per_token=float(prompt_cost),
                            output_per_token=float(completion_cost),
                        )
                    except (ValueError, TypeError):
                        continue

            self._cache = new_cache
            self._fetched_at = time.monotonic()
            log.info("pricing.refreshed", models=len(new_cache))
        except Exception as exc:
            log.warning("pricing.refresh_failed", error=str(exc))


@dataclass
class PriceEntry:
    """Pricing per 1M tokens in USD."""

    input_per_m: float
    output_per_m: float
    cached_input_per_m: float | None = None

    def calculate_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> float:
        """Calculate USD cost for token counts using this PriceEntry."""
        return calculate_cost_usd(
            self,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
        )


def _entry(facts: model_registry.PriceFacts) -> PriceEntry:
    return PriceEntry(facts.input_per_m, facts.output_per_m, facts.cached_input_per_m)


# Prices that must survive a live provider catalog: Bankr/OpenCAP gateway rates
# for bare ids, and canonical rack rates that OpenRouter's promotional or routed
# discounts would otherwise replace. Declared per model in the registry as
# ``beats_live_catalog``; consulted before the table below.
#
# Ids shared with direct-provider entries (gpt-5.4-mini, gpt-5.5,
# deepseek-v4-flash) are deliberately not marked: the direct rack rates keep
# pricing those ids, which overestimates gateway spend rather than
# underestimating direct spend.
_PRICE_OVERRIDES: list[tuple[str, PriceEntry]] = [
    (model_id, _entry(facts)) for model_id, facts in model_registry.price_override_rows()
]


_PRICE_LOCK = threading.RLock()
_LIVE_PRICE_CACHE: dict[str, PriceEntry] = {}
_LIVE_PRICE_FETCHED_AT: dict[str, float] = {}
_LIVE_PRICE_MISS_AT: dict[str, float] = {}


class _GatewayPriceCache:
    """Live price cache for one marketplace gateway.

    OpenCAP and Surplus both resell the same bare ids (``claude-opus-5``,
    ``deepseek-v4-flash``, …) at their own routed rates, so neither can be
    described by the shared static table -- and neither by the other's. Each
    gateway that publishes a catalog therefore gets its own cache, keyed only
    by model id and never consulted for a different provider.

    Because a gateway publishes one catalog covering every model, a single pair
    of timestamps replaces the per-key dicts the OpenRouter path needs: a TTL on
    success, and a shorter negative cache on failure so an unreachable catalog
    costs one bounded attempt rather than one per lookup.
    """

    def __init__(self, provider_id: str, env_flag: str) -> None:
        self._provider_id = provider_id
        self._env_flag = env_flag
        self._prices: dict[str, PriceEntry] = {}
        self._fetched_at = 0.0
        self._miss_at = 0.0
        # Model IDs already reported as falling back to the shared static
        # table. The warning is worth emitting once per model, not once
        # per turn.
        self._fallback_logged: set[str] = set()

    def live_pricing_enabled(self) -> bool:
        raw = os.environ.get(self._env_flag, "1").strip().lower()
        return raw not in {"0", "false", "off", "no"}

    def store(self, prices: dict[str, PriceEntry]) -> int:
        """Atomically replace the cache with validated entries."""
        if not prices:
            return 0
        with _PRICE_LOCK:
            self._prices.clear()
            self._prices.update(prices)
            self._fetched_at = time.monotonic()
            self._miss_at = 0.0
        log.info(f"pricing.{self._provider_id}_refreshed", models=len(prices))
        return len(prices)

    def refresh_if_needed(self, fetch: Callable[[], dict[str, PriceEntry]]) -> None:
        """Refresh when the cache is empty or past its TTL.

        ``fetch`` is only called once both the success TTL and the negative
        cache have been checked, so a cold lookup costs at most one request.
        """
        if not self.live_pricing_enabled():
            return
        now = time.monotonic()
        with _PRICE_LOCK:
            if self._prices and now - self._fetched_at <= _CACHE_TTL:
                return
            if self._miss_at and now - self._miss_at <= _LIVE_PRICE_MISS_TTL:
                return

        prices = fetch()
        if prices:
            self.store(prices)
            return
        with _PRICE_LOCK:
            self._miss_at = time.monotonic()

    def lookup(self, model_id: str) -> PriceEntry | None:
        """Return a cached price without performing outbound I/O."""
        key = str(model_id or "").strip().lower()
        if not key:
            return None
        with _PRICE_LOCK:
            return self._prices.get(key)

    def log_static_fallback(self, model_id: str) -> None:
        """Warn once per model when an estimate comes from the static table.

        The shared table holds another gateway's rates for these bare IDs,
        which can run several times below this one's. Without this the
        substituted number is indistinguishable from a catalog-backed one.
        """
        key = str(model_id or "").strip().lower()
        if not key:
            return
        with _PRICE_LOCK:
            if key in self._fallback_logged:
                return
            self._fallback_logged.add(key)
        log.warning(f"pricing.{self._provider_id}_static_fallback", model=model_id)

    def reset_for_tests(self) -> None:
        with _PRICE_LOCK:
            self._prices.clear()
            self._fallback_logged.clear()
            self._fetched_at = 0.0
            self._miss_at = 0.0


_OPENCAP_PRICES = _GatewayPriceCache("opencap", "AGENTOS_OPENCAP_LIVE_PRICING")
_SURPLUS_PRICES = _GatewayPriceCache("surplus", "AGENTOS_SURPLUS_LIVE_PRICING")
# Gateways whose own catalog is canonical for their bare model ids, in the
# order lookup_price consults them.
_GATEWAY_PRICE_CACHES: dict[str, _GatewayPriceCache] = {
    "opencap": _OPENCAP_PRICES,
    "surplus": _SURPLUS_PRICES,
}


def _lookup_price_override(model_id: str) -> PriceEntry | None:
    model_lower = str(model_id or "").strip().lower()
    for prefix, entry in _PRICE_OVERRIDES:
        if model_lower.startswith(prefix):
            return entry
    return None


def _model_price_from_entry(entry: PriceEntry) -> ModelPrice:
    return ModelPrice(
        input_per_token=entry.input_per_m / 1_000_000,
        output_per_token=entry.output_per_m / 1_000_000,
    )


def _live_pricing_enabled() -> bool:
    raw = os.environ.get("AGENTOS_OPENROUTER_LIVE_PRICING", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _normalize_openrouter_base_url(base_url: str | None = None) -> str:
    base = base_url or os.environ.get("OPENROUTER_BASE_URL") or _OPENROUTER_PRICING_BASE_URL
    base = base.rstrip("/")
    if base.endswith("/v1"):
        return base
    if base.endswith("/api"):
        return f"{base}/v1"
    return base


def _openrouter_endpoint_url(model_id: str, base_url: str | None = None) -> str:
    base = _normalize_openrouter_base_url(base_url)
    return f"{base}/models/{model_id}/endpoints"


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _nonnegative_float_or_none(value: object) -> float | None:
    parsed = _float_or_none(value)
    if parsed is None or not isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _parse_opencap_prices(data: dict[str, Any]) -> dict[str, PriceEntry]:
    """Parse OpenCAP's public USD-per-1M-token model catalog."""
    models = data.get("data")
    if not isinstance(models, list):
        return {}
    prices: dict[str, PriceEntry] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "").strip().lower()
        pricing = model.get("pricing")
        if not model_id or not isinstance(pricing, dict):
            continue
        input_price = _nonnegative_float_or_none(pricing.get("input"))
        output_price = _nonnegative_float_or_none(pricing.get("output"))
        if input_price is None or output_price is None:
            continue
        prices[model_id] = PriceEntry(
            input_per_m=input_price,
            output_per_m=output_price,
            cached_input_per_m=_nonnegative_float_or_none(pricing.get("cachedInput")),
        )
    return prices


def _parse_surplus_prices(data: dict[str, Any]) -> dict[str, PriceEntry]:
    """Parse Surplus Intelligence's public model catalog.

    Surplus follows OpenRouter's catalog shape rather than OpenCAP's: rates
    live under ``pricing.prompt`` / ``pricing.completion`` and are USD **per
    token**, serialised as strings ("0.0000000700"). ``PriceEntry`` is per 1M
    tokens, so the scaling happens once here instead of at every call site.
    """
    models = data.get("data")
    if not isinstance(models, list):
        return {}
    prices: dict[str, PriceEntry] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "").strip().lower()
        pricing = model.get("pricing")
        if not model_id or not isinstance(pricing, dict):
            continue
        input_price = _per_million_or_none(pricing.get("prompt"))
        output_price = _per_million_or_none(pricing.get("completion"))
        if input_price is None or output_price is None:
            continue
        prices[model_id] = PriceEntry(
            input_per_m=input_price,
            output_per_m=output_price,
            cached_input_per_m=_per_million_or_none(pricing.get("input_cache_read")),
        )
    return prices


def _per_million_or_none(value: object) -> float | None:
    """Scale a validated USD-per-token catalog rate to USD per 1M tokens."""
    parsed = _nonnegative_float_or_none(value)
    if parsed is None:
        return None
    return parsed * 1_000_000


def seed_opencap_price_cache(data: dict[str, Any]) -> int:
    """Seed pricing from an OpenCAP catalog payload already fetched at boot."""
    return _OPENCAP_PRICES.store(_parse_opencap_prices(data))


def seed_surplus_price_cache(data: dict[str, Any]) -> int:
    """Seed pricing from a Surplus catalog payload already fetched at boot."""
    return _SURPLUS_PRICES.store(_parse_surplus_prices(data))


def _fetch_gateway_catalog_sync(provider_id: str) -> dict[str, Any] | None:
    """Fetch a gateway's public catalog, returning None when it is unreachable."""
    url = get_provider_spec(provider_id).model_catalog_url
    if not url:
        return None
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, trust_env=_trust_env()) as client:
            resp = client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        log.debug(f"pricing.{provider_id}_fetch_failed", error=str(exc))
        return None
    if not isinstance(payload, dict):
        log.debug(f"pricing.{provider_id}_fetch_failed", error="catalog response is not an object")
        return None
    return cast(dict[str, Any], payload)


def _fetch_opencap_catalog_sync() -> dict[str, Any] | None:
    """Fetch OpenCAP's public catalog, returning None when it is unreachable."""
    return _fetch_gateway_catalog_sync("opencap")


def _fetch_surplus_catalog_sync() -> dict[str, Any] | None:
    """Fetch Surplus's public catalog, returning None when it is unreachable."""
    return _fetch_gateway_catalog_sync("surplus")


def _refresh_opencap_prices_if_needed() -> None:
    """Refresh the OpenCAP price cache when it is empty or past its TTL.

    The cache used to be seeded exactly once at boot, so a single timeout left
    every OpenCAP estimate on the shared static table — another gateway's rate
    sheet — for the life of the process.
    """

    # Resolved through module globals on every call so tests can monkeypatch
    # the fetcher, and so no request is issued while the cache is still fresh.
    def fetch() -> dict[str, PriceEntry]:
        data = _fetch_opencap_catalog_sync()
        return _parse_opencap_prices(data) if data is not None else {}

    _OPENCAP_PRICES.refresh_if_needed(fetch)


def _refresh_surplus_prices_if_needed() -> None:
    """Refresh the Surplus price cache when it is empty or past its TTL."""

    def fetch() -> dict[str, PriceEntry]:
        data = _fetch_surplus_catalog_sync()
        return _parse_surplus_prices(data) if data is not None else {}

    _SURPLUS_PRICES.refresh_if_needed(fetch)


_GATEWAY_PRICE_REFRESHERS: dict[str, Callable[[], None]] = {
    "opencap": _refresh_opencap_prices_if_needed,
    "surplus": _refresh_surplus_prices_if_needed,
}


def _lookup_opencap_price(model_id: str) -> PriceEntry | None:
    """Return a cached OpenCAP price without performing outbound I/O."""
    return _OPENCAP_PRICES.lookup(model_id)


def _lookup_surplus_price(model_id: str) -> PriceEntry | None:
    """Return a cached Surplus price without performing outbound I/O."""
    return _SURPLUS_PRICES.lookup(model_id)


def _apply_discount_inverse(price_per_token: float, discount: float) -> float:
    """Return the non-discounted token price when OpenRouter reports a discount.

    OpenRouter endpoint pricing also includes cache-read rates. Those are not
    used here: Pilot Router savings and AgentOS estimates must use the normal
    prompt/completion price, then remove any explicit endpoint discount.
    """
    if discount <= 0:
        return price_per_token
    rate = discount / 100 if discount > 1 else discount
    if rate <= 0 or rate >= 1:
        return price_per_token
    return price_per_token / (1 - rate)


def _endpoint_price(entry: dict) -> PriceEntry | None:
    pricing = entry.get("pricing") or {}
    prompt = _float_or_none(pricing.get("prompt"))
    completion = _float_or_none(pricing.get("completion"))
    if prompt is None or completion is None:
        return None
    discount = _float_or_none(pricing.get("discount")) or 0.0
    return PriceEntry(
        input_per_m=_apply_discount_inverse(prompt, discount) * 1_000_000,
        output_per_m=_apply_discount_inverse(completion, discount) * 1_000_000,
    )


def _normalize_provider_token(value: object) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _official_provider_tokens(model_id: str) -> set[str]:
    namespace = model_id.split("/", 1)[0]
    normalized = _normalize_provider_token(namespace)
    aliases = {
        "zai": {"zai"},
        "moonshotai": {"moonshotai", "moonshot"},
    }
    return aliases.get(normalized, {normalized})


def _is_official_endpoint(model_id: str, endpoint: dict) -> bool:
    official = _official_provider_tokens(model_id)
    provider_name = _normalize_provider_token(endpoint.get("provider_name"))
    tag_root = str(endpoint.get("tag") or "").split("/", 1)[0]
    tag = _normalize_provider_token(tag_root)
    return provider_name in official or tag in official


def _select_official_endpoint_price(data: dict, model_id: str) -> PriceEntry | None:
    """Select a live OpenRouter price from model endpoint metadata.

    The public ``/models`` list can expose a cheap routed/top-provider price.
    For savings display we need the official provider's non-cache,
    non-discount prompt/completion price. Prefer the endpoint whose
    ``provider_name`` or tag matches the model namespace, then fall back to the
    first priced endpoint if OpenRouter has no owner endpoint for that model.
    """
    model = data.get("data") or data
    endpoints = model.get("endpoints") or []
    if not endpoints:
        return _endpoint_price(model)

    for endpoint in endpoints:
        if _is_official_endpoint(model_id, endpoint):
            price = _endpoint_price(endpoint)
            if price is not None:
                return price
    for endpoint in endpoints:
        price = _endpoint_price(endpoint)
        if price is not None:
            return price
    return None


def _fetch_openrouter_json_sync(url: str) -> dict:
    with httpx.Client(timeout=_HTTP_TIMEOUT, trust_env=_trust_env()) as client:
        resp = client.get(url, headers=openrouter_app_headers(url))
        resp.raise_for_status()
        return cast(dict[Any, Any], resp.json())


def _fetch_live_openrouter_price(model_id: str, base_url: str | None = None) -> PriceEntry | None:
    override = _lookup_price_override(model_id)
    if override is not None:
        return override
    try:
        data = _fetch_openrouter_json_sync(_openrouter_endpoint_url(model_id, base_url))
    except Exception as exc:
        log.debug("pricing.live_lookup_failed", model=model_id, error=str(exc))
        return None
    price = _select_official_endpoint_price(data, model_id)
    if price is not None:
        log.debug(
            "pricing.live_lookup_ready",
            model=model_id,
            input_per_m=price.input_per_m,
            output_per_m=price.output_per_m,
        )
    return price


def refresh_live_prices(
    model_ids: list[str] | tuple[str, ...] | set[str],
    base_url: str | None = None,
) -> None:
    """Preload live OpenRouter endpoint prices for known model IDs."""
    for model_id in sorted({str(mid).strip() for mid in model_ids if str(mid).strip()}):
        override = _lookup_price_override(model_id)
        if override is not None:
            now = time.monotonic()
            key = model_id.lower()
            with _PRICE_LOCK:
                _LIVE_PRICE_CACHE[key] = override
                _LIVE_PRICE_FETCHED_AT[key] = now
                _LIVE_PRICE_MISS_AT.pop(key, None)
            continue
        if not _should_fetch_live_price(model_id):
            continue
        price = _fetch_live_openrouter_price(model_id, base_url)
        now = time.monotonic()
        key = model_id.lower()
        with _PRICE_LOCK:
            if price is None:
                _LIVE_PRICE_MISS_AT[key] = now
                continue
            _LIVE_PRICE_CACHE[key] = price
            _LIVE_PRICE_FETCHED_AT[key] = now
            _LIVE_PRICE_MISS_AT.pop(key, None)


def reset_live_price_cache_for_tests() -> None:
    with _PRICE_LOCK:
        _LIVE_PRICE_CACHE.clear()
        _LIVE_PRICE_FETCHED_AT.clear()
        _LIVE_PRICE_MISS_AT.clear()
    for cache in _GATEWAY_PRICE_CACHES.values():
        cache.reset_for_tests()


def seed_live_price_cache_for_tests(model_id: str, price: PriceEntry) -> None:
    with _PRICE_LOCK:
        key = model_id.lower()
        _LIVE_PRICE_CACHE[key] = price
        _LIVE_PRICE_FETCHED_AT[key] = time.monotonic()
        _LIVE_PRICE_MISS_AT.pop(key, None)


# Prefix families that are not single models: model generations, vendor
# namespaces, and free local runtimes. A per-model declaration would be fiction
# here, so these keep the historical ``startswith`` behaviour and stay ordered by
# hand -- longest prefix first within a family. Anything that *is* one model is
# declared in agentos.model_registry instead, and a test keeps the two disjoint.
_LEGACY_PRICING_PREFIXES: list[tuple[str, PriceEntry]] = [
    # Direct provider smoke estimates.
    ("gpt-4.1", PriceEntry(2.0, 8.0)),
    # Zhipu docs quote GLM-4.5 series API prices in CNY; converted to USD at
    # roughly 6.975 CNY/USD for AgentOS estimates only.
    ("glm-4.5", PriceEntry(0.115, 0.287)),
    ("minimax-m2.7", PriceEntry(0.118, 0.99)),
    ("gemini-2.5-flash-lite", PriceEntry(0.10, 0.40)),
    ("gemini-2.5-flash", PriceEntry(0.15, 0.60)),
    ("gemini-2.0-flash", PriceEntry(0.10, 0.40, 0.025)),
    ("gemini-1.5-pro", PriceEntry(1.25, 5.0, 0.3125)),
    ("qwen3.6-plus", PriceEntry(0.115, 0.688)),
    ("qwen3-max", PriceEntry(0.359, 1.434)),
    ("doubao-seed-1-6-flash", PriceEntry(0.15, 0.60)),
    ("doubao-seed-1-6-thinking", PriceEntry(0.60, 2.40)),
    ("doubao-seed-1-6", PriceEntry(0.30, 1.20)),
    # DeepSeek.
    ("deepseek/deepseek-r1", PriceEntry(0.70, 2.50)),
    ("deepseek/deepseek-v3", PriceEntry(0.26, 0.38)),
    ("deepseek/deepseek-chat", PriceEntry(0.14, 0.28)),
    ("deepseek-reasoner", PriceEntry(0.70, 2.50, 0.14)),
    ("deepseek-chat", PriceEntry(0.14, 0.28, 0.014)),
    # OpenAI (OpenRouter prices).
    ("openai/gpt-4.1-mini", PriceEntry(0.40, 1.60)),
    ("openai/gpt-4.1", PriceEntry(2.0, 8.0)),
    ("openai/gpt-4o-mini", PriceEntry(0.15, 0.60)),
    ("openai/gpt-4o", PriceEntry(2.50, 10.0)),
    ("openai/text-embedding-3-small", PriceEntry(0.02, 0.0)),
    ("openai/text-embedding-3-large", PriceEntry(0.13, 0.0)),
    ("gpt-4o-mini", PriceEntry(0.15, 0.60)),
    ("gpt-4o", PriceEntry(2.50, 10.0)),
    ("text-embedding-3-small", PriceEntry(0.02, 0.0)),
    ("text-embedding-3-large", PriceEntry(0.13, 0.0)),
    ("gpt-4-turbo", PriceEntry(10.0, 30.0)),
    ("gpt-4-", PriceEntry(30.0, 60.0)),
    ("o3-mini", PriceEntry(1.10, 4.40)),
    ("o1-mini", PriceEntry(3.0, 12.0)),
    ("o1", PriceEntry(15.0, 60.0)),
    # Anthropic Claude.
    ("anthropic/claude-opus-4.8", PriceEntry(5.0, 25.0)),
    ("anthropic/claude-opus-4.7", PriceEntry(5.0, 25.0)),
    ("anthropic/claude-opus-4.5", PriceEntry(5.0, 25.0)),
    ("anthropic/claude-opus-4", PriceEntry(15.0, 75.0)),
    ("anthropic/claude-sonnet-4", PriceEntry(3.0, 15.0)),
    ("anthropic/claude-3-7-sonnet", PriceEntry(3.0, 15.0, 0.30)),
    ("anthropic/claude-3-5-sonnet", PriceEntry(3.0, 15.0)),
    ("anthropic/claude-3-5-haiku", PriceEntry(0.80, 4.0)),
    ("anthropic/claude-3-opus", PriceEntry(15.0, 75.0)),
    ("anthropic/claude-3-sonnet", PriceEntry(3.0, 15.0)),
    ("anthropic/claude-3-haiku", PriceEntry(0.25, 1.25)),
    ("claude-opus-4", PriceEntry(15.0, 75.0)),
    ("claude-sonnet-4", PriceEntry(3.0, 15.0)),
    ("claude-3-7-sonnet", PriceEntry(3.0, 15.0, 0.30)),
    ("claude-3-5-sonnet", PriceEntry(3.0, 15.0)),
    ("claude-3-5-haiku", PriceEntry(0.80, 4.0)),
    ("claude-3-opus", PriceEntry(15.0, 75.0)),
    ("claude-3-sonnet", PriceEntry(3.0, 15.0)),
    ("claude-3-haiku", PriceEntry(0.25, 1.25)),
    # Google Gemini.
    ("google/gemini-2.5-flash", PriceEntry(0.15, 0.60)),
    ("google/gemini-2.5-pro", PriceEntry(1.25, 10.0)),
    ("google/gemini-2.0-flash", PriceEntry(0.10, 0.40)),
    ("google/gemini-1.5-pro", PriceEntry(1.25, 5.0, 0.3125)),
    # Alibaba Cloud Model Studio / DashScope, Chinese Mainland (Beijing).
    # OpenAI-compatible Chat Completions returns token usage, not billed cost.
    # These prices are used only for AgentOS estimates and must not be
    # reported as provider-billed amounts. Source: Alibaba Cloud Model Studio
    # model pricing, checked 2026-05-03. Prices are USD per 1M tokens.
    ("qwen-plus", PriceEntry(0.115, 0.287)),
    ("qwen-flash", PriceEntry(0.022, 0.216)),
    ("qwen-turbo", PriceEntry(0.044, 0.087)),
    ("qwen-max", PriceEntry(0.345, 1.377)),
    # Ollama / local (free).
    ("baai/", PriceEntry(0.0, 0.0)),
    ("sentence-transformers/", PriceEntry(0.0, 0.0)),
    ("ollama/", PriceEntry(0.0, 0.0)),
    ("local/", PriceEntry(0.0, 0.0)),
]

# Every model that has one declared price, most specific id first, ahead of the
# prefix families. Ordering by id length is what makes specificity structural:
# the scan below takes the first match, so a shorter prefix added later can no
# longer quietly swallow a longer id it happens to be a prefix of.
_PRICING_TABLE: list[tuple[str, PriceEntry]] = [
    (model_id, _entry(facts)) for model_id, facts in model_registry.exact_price_rows()
] + _LEGACY_PRICING_PREFIXES

_DEFAULT_PRICING = PriceEntry(3.0, 15.0)

_VENDOR_PREFIXES = ("anthropic/", "google/", "deepseek/", "openai/")


def _lookup_static_price(model_id: str) -> PriceEntry:
    override = _lookup_price_override(model_id)
    if override is not None:
        return override
    model_lower = model_id.lower()
    for prefix, entry in _PRICING_TABLE:
        if model_lower.startswith(prefix):
            return entry
    for vendor_prefix in _VENDOR_PREFIXES:
        if model_lower.startswith(vendor_prefix):
            bare = model_lower[len(vendor_prefix) :]
            for prefix, entry in _PRICING_TABLE:
                if bare.startswith(prefix):
                    return entry
            break
    return _DEFAULT_PRICING


def _log_opencap_static_fallback(model_id: str) -> None:
    """Warn once per model when an OpenCAP estimate comes from the static table."""
    _OPENCAP_PRICES.log_static_fallback(model_id)


def _should_fetch_live_price(model_id: str) -> bool:
    model_lower = model_id.lower().strip()
    if not _live_pricing_enabled():
        return False
    if "/" not in model_lower:
        return False
    if model_lower.startswith(("baai/", "sentence-transformers/", "ollama/", "local/")):
        return False
    return True


def lookup_price(model_id: str, provider_id: str = "") -> PriceEntry:
    """Look up provider-aware pricing, preferring live catalog prices.

    OpenCAP and Surplus use their own public model catalogs because their bare
    model IDs overlap with other gateways whose rates differ. OpenRouter live
    lookup uses ``prompt``/``completion`` endpoint prices, explicitly not
    cache-read prices. If either service is unreachable, the static table is a
    fail-open fallback so cost estimation keeps working offline.
    """
    model_id = str(model_id or "").strip()
    normalized_provider = str(provider_id or "").strip().lower()

    gateway = _GATEWAY_PRICE_CACHES.get(normalized_provider)
    if gateway is not None:
        # The gateway's public catalog is canonical for this provider. Shared
        # bare IDs intentionally bypass the static override entries below.
        # Refreshing first is a no-op while the cache is fresh; it only fetches
        # when the boot seed never landed or has aged past the TTL.
        _GATEWAY_PRICE_REFRESHERS[normalized_provider]()
        gateway_price = gateway.lookup(model_id)
        if gateway_price is not None:
            return gateway_price
        gateway.log_static_fallback(model_id)
        return _lookup_static_price(model_id)
    override = _lookup_price_override(model_id)
    if override is not None:
        return override

    if not _should_fetch_live_price(model_id):
        return _lookup_static_price(model_id)

    now = time.monotonic()
    key = model_id.lower()
    with _PRICE_LOCK:
        cached = _LIVE_PRICE_CACHE.get(key)
        fetched_at = _LIVE_PRICE_FETCHED_AT.get(key, 0.0)
        if cached is not None and now - fetched_at <= _CACHE_TTL:
            return cached
        miss_at = _LIVE_PRICE_MISS_AT.get(key, 0.0)
        if miss_at and now - miss_at <= _LIVE_PRICE_MISS_TTL:
            return _lookup_static_price(model_id)

    price = _fetch_live_openrouter_price(model_id)
    with _PRICE_LOCK:
        if price is None:
            _LIVE_PRICE_MISS_AT[key] = time.monotonic()
            return _lookup_static_price(model_id)
        _LIVE_PRICE_CACHE[key] = price
        _LIVE_PRICE_FETCHED_AT[key] = time.monotonic()
        _LIVE_PRICE_MISS_AT.pop(key, None)
        return price


def calculate_cost_usd(
    price: PriceEntry,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """Calculate USD cost from USD-per-1M-token rates.

    ``input_tokens`` follows OpenAI-compatible usage semantics and includes
    cached prompt tokens. When a catalog publishes ``cached_input_per_m``, the
    cached portion is removed from normal input and charged at that rate.
    """
    safe_input = max(0, int(input_tokens))
    safe_output = max(0, int(output_tokens))
    cached_input = min(safe_input, max(0, int(cached_input_tokens)))
    regular_input = safe_input - cached_input
    cached_rate = (
        price.cached_input_per_m if price.cached_input_per_m is not None else price.input_per_m
    )
    return (
        regular_input * price.input_per_m
        + cached_input * cached_rate
        + safe_output * price.output_per_m
    ) / 1_000_000
