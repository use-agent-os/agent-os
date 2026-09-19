"""ModelCatalog — in-memory cache of model metadata fetched from provider API."""

from __future__ import annotations

from math import isfinite
from typing import Any, cast

import httpx
import structlog

from agentos import model_registry
from agentos.env import trust_env as _trust_env
from agentos.secrets import clean_header_secret

from .openrouter_attribution import openrouter_app_headers
from .registry import UnknownProviderError, get_provider_spec
from .types import ModelCapabilities, ModelInfo

log = structlog.get_logger(__name__)

DEFAULT_MAX_TOKENS = 16384
SAFE_OPENROUTER_DEFAULT_MAX_TOKENS = 8192
DEFAULT_CONTEXT_WINDOW = 200_000

# Every model's windows are declared once in agentos.model_registry, alongside
# its price and capability flags. These two names stay so the resolution chain
# below (user override > live catalog > provider fallback > shared static >
# generic) is untouched -- only where the numbers come from has changed.
#
# Format: model_id -> (max_output_tokens, context_window). Used when the
# provider's own catalog is unreachable at boot.
_STATIC_FALLBACK: dict[str, tuple[int, int]] = model_registry.static_windows()

# Ids whose shared entry carries one endpoint's contract while another endpoint
# serves the same id with different limits. Each override declares its reason in
# the registry, next to the model.
_PROVIDER_STATIC_FALLBACK: dict[str, dict[str, tuple[int, int]]] = model_registry.provider_windows()


def _catalog_price_per_1k(value: object) -> float:
    """Convert a validated catalog USD-per-million value to USD per 1K tokens."""
    try:
        price_per_million = float(cast(Any, value))
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(price_per_million) or price_per_million < 0:
        return 0.0
    return price_per_million / 1000


# Offline capability fallbacks for the Surplus marketplace, taken from the
# families its published catalog marks with `reasoning` / `vision` in
# supported_features. Only consulted when a boot could not reach the catalog;
# a live fetch always wins.
_SURPLUS_REASONING_PREFIXES = (
    "claude-opus-",
    "claude-sonnet-",
    "deepseek-",
    "gemini-",
    "glm-5",
    "gpt-5",
    "grok-4",
    "kimi-k",
    "minimax-m2",
    "qwen3",
)
_SURPLUS_VISION_PREFIXES = (
    "claude-haiku-4.5",
    "gemini-",
    "glm-5.3-flash",
    "glm-5v",
    "gpt-5.6-",
    "grok-4.5",
    "grok-4.6",
    "kimi-k2.6",
    "kimi-k3",
    "qwen3-vl",
)


def _catalog_price_per_token(value: object) -> float:
    """Convert a validated catalog USD-per-token value to USD per 1K tokens."""
    try:
        price_per_token = float(cast(Any, value))
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(price_per_token) or price_per_token < 0:
        return 0.0
    return price_per_token * 1000


class ModelCatalog:
    """In-memory cache of model metadata fetched from provider API.

    Priority chain for max_tokens:
      1. User config override (>0)
      2. API-fetched catalog value
      3. Static fallback table
      4. DEFAULT_MAX_TOKENS (16384)
      → then clamp to min(value, context_window)
    """

    def __init__(self) -> None:
        self._models: dict[str, ModelInfo] = {}

    def __len__(self) -> int:
        return len(self._models)

    def list_models(self, provider_id: str = "") -> list[ModelInfo]:
        """Return cached models, optionally restricted to one provider."""
        provider = provider_id.strip().lower()
        return [
            model
            for model in self._models.values()
            if not provider or model.provider.strip().lower() == provider
        ]

    def _populate_from_data(
        self,
        models: list[dict],
        *,
        provider_id: str = "openrouter",
        pricing_per_token: bool = False,
    ) -> None:
        """Parse a list of OpenRouter-shaped model dicts into ModelInfo entries.

        Surplus Intelligence publishes this same shape rather than the flatter
        gateway one, so it reuses this parser. Two things differ and are opted
        into rather than assumed: its ``pricing`` rates are USD **per token**
        (OpenRouter's live pricing is resolved separately, in engine.pricing),
        and it carries an extra ``supported_features`` array that names
        ``vision`` / ``reasoning`` / ``tools`` directly instead of leaving them
        to be inferred from ``supported_parameters``.
        """
        for m in models:
            model_id = m.get("id", "")
            if not model_id:
                continue
            top_provider = m.get("top_provider") or {}
            max_completion = top_provider.get("max_completion_tokens") or 0
            supported = set(m.get("supported_parameters", []))
            features = {str(item).lower() for item in m.get("supported_features", [])}
            architecture = m.get("architecture") or {}
            input_modalities = {
                str(item).lower() for item in architecture.get("input_modalities", [])
            }
            pricing = m.get("pricing") if pricing_per_token else None
            if not isinstance(pricing, dict):
                pricing = {}
            self._models[model_id] = ModelInfo(
                provider=provider_id,
                model_id=model_id,
                display_name=m.get("name", model_id),
                context_window=m.get("context_length", 0),
                max_output_tokens=max_completion,
                supports_reasoning=(
                    "reasoning" in supported
                    or "reasoning_effort" in supported
                    or "reasoning" in features
                ),
                supports_tools=(
                    "tools" in supported or "tool_choice" in supported or "tools" in features
                ),
                supports_vision="image" in input_modalities or "vision" in features,
                input_cost_per_1k=_catalog_price_per_token(pricing.get("prompt")),
                output_cost_per_1k=_catalog_price_per_token(pricing.get("completion")),
            )

    def _populate_from_surplus(self, models: list[dict]) -> None:
        """Parse Surplus Intelligence marketplace model dicts into ModelInfo entries."""
        self._populate_from_data(
            models,
            provider_id="surplus",
            pricing_per_token=True,
        )

    def _populate_from_gateway(
        self,
        models: list[dict],
        *,
        provider_id: str,
        pricing_per_million: bool = False,
    ) -> None:
        """Parse OpenAI-compatible gateway model dicts into ModelInfo entries.

        Gateway catalogs expose an OpenAI-compatible ``/models`` list. Field names are
        read defensively (``context_length``/``contextLength``,
        ``max_output``/``maxOutput``) and missing values fall back to the static
        table / default via ``resolve_max_tokens``. The catalog carries no
        tool/reasoning flags, so tools default on (every gateway llm supports
        them) and reasoning stays off — capability resolution for bankr is
        handled in get_capabilities. OpenCAP's public catalog additionally
        publishes USD-per-million pricing; callers opt into that unit explicitly.
        """
        for m in models:
            model_id = m.get("id", "")
            if not model_id:
                continue
            modality = m.get("modality") or {}
            input_modalities = {str(item).lower() for item in modality.get("input", [])}
            context_window = m.get("context_length") or m.get("contextLength") or 0
            max_output = m.get("max_output") or m.get("maxOutput") or 0
            pricing = m.get("pricing") if pricing_per_million else None
            if not isinstance(pricing, dict):
                pricing = {}
            self._models[model_id] = ModelInfo(
                provider=provider_id,
                model_id=model_id,
                display_name=m.get("name", model_id),
                context_window=context_window,
                max_output_tokens=max_output,
                supports_reasoning=False,
                supports_tools=True,
                supports_vision="image" in input_modalities,
                input_cost_per_1k=_catalog_price_per_1k(pricing.get("input")),
                output_cost_per_1k=_catalog_price_per_1k(pricing.get("output")),
            )

    def _populate_from_bankr(self, models: list[dict]) -> None:
        """Parse Bankr LLM Gateway model dicts into ModelInfo entries."""
        self._populate_from_gateway(models, provider_id="bankr")

    def _populate_from_opencap(self, models: list[dict]) -> None:
        """Parse OpenCAP gateway model dicts into ModelInfo entries."""
        self._populate_from_gateway(
            models,
            provider_id="opencap",
            pricing_per_million=True,
        )

    def get_capabilities(
        self,
        model_id: str,
        provider_name: str = "openrouter",
        base_url: str = "",
    ) -> ModelCapabilities:
        """Resolve ModelCapabilities for a model based on provider and catalog data."""
        # ``llm.provider`` is a plain string, so "OpenAI" and "openai" are the
        # same provider; every branch below matches on the normalised id.
        provider_id = provider_name.strip().lower()
        if provider_id == "anthropic":
            return ModelCapabilities()
        if provider_id == "ollama":
            return ModelCapabilities()
        try:
            provider_spec = get_provider_spec(provider_id)
        except UnknownProviderError:
            provider_spec = None

        if provider_id == "openai" and "deepseek" in base_url.lower():
            return ModelCapabilities(
                supports_reasoning=True, supports_tools=True, reasoning_format="deepseek"
            )
        info = self._models.get(model_id)
        if info and info.supports_reasoning:
            return ModelCapabilities(
                supports_reasoning=True,
                supports_tools=info.supports_tools,
                supports_vision=info.supports_vision,
                reasoning_format="openrouter",
            )
        model_l = model_id.strip().lower()
        if (
            provider_id == "openai"
            and "api.openai.com" in base_url.lower()
            and model_l.startswith(("gpt-5", "o1", "o3", "o4"))
        ):
            return ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openai",
            )
        if provider_spec and provider_spec.reasoning_shape == "deepseek":
            return ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            )
        if provider_spec and provider_spec.reasoning_shape == "gemini":
            supports_reasoning = model_l.startswith(("gemini-2.5", "gemini-3"))
            return ModelCapabilities(
                supports_reasoning=supports_reasoning,
                supports_tools=True,
                supports_vision=True,
                reasoning_format="gemini" if supports_reasoning else "none",
            )
        if provider_spec and provider_spec.reasoning_shape == "zai":
            supports_reasoning = model_l.startswith(("glm-4.5", "glm-4.6", "glm-4.7", "glm-5"))
            return ModelCapabilities(
                supports_reasoning=supports_reasoning,
                supports_tools=True,
                reasoning_format="zai" if supports_reasoning else "none",
            )
        if provider_id == "surplus":
            # Surplus is an OpenRouter-shaped marketplace over bare ids. When
            # its catalog has been fetched, the live-info branch above has
            # already answered; this is the offline path, so the shipped tier
            # defaults still route correctly on a boot whose catalog fetch
            # failed. The format stays "openrouter" even for DeepSeek and GLM
            # ids -- unlike the OpenCAP gateway, Surplus normalizes reasoning
            # params and advertises `reasoning`/`include_reasoning` in
            # supported_parameters for exactly these models, so sending a
            # vendor-native switch instead would be the wrong shape.
            supports_reasoning = (
                info.supports_reasoning
                if info is not None
                else model_l.startswith(_SURPLUS_REASONING_PREFIXES)
            )
            return ModelCapabilities(
                supports_reasoning=supports_reasoning,
                supports_tools=info.supports_tools if info is not None else True,
                supports_vision=(
                    info.supports_vision
                    if info is not None
                    else model_l.startswith(_SURPLUS_VISION_PREFIXES)
                ),
                reasoning_format="openrouter" if supports_reasoning else "none",
            )
        if provider_id in {"bankr", "opencap"}:
            # Gateway catalog ids are bare (e.g. "minimax-m3"); legacy Bankr
            # configs may still carry the namespaced "virtuals/<id>" form, so
            # strip the prefix before matching. Prefer live catalog modality data
            # when a fetch populated it; otherwise fall back to a prefix heuristic
            # (gpt-5.5 has image input but gpt-5.4-mini does not). The glm entry
            # is the flash line only: OpenCAP publishes glm-5.3-flash as natively
            # multimodal while glm-5.2/glm-5.3 stay text-in, text-out.
            basename = model_l.split("/", 1)[1] if "/" in model_l else model_l
            supports_vision = (
                info.supports_vision
                if info is not None
                else basename.startswith(
                    (
                        "minimax-m3",
                        "gemini-",
                        "kimi-",
                        "claude-",
                        "grok-",
                        "gpt-5.5",
                        "gpt-5.6",
                        "glm-5.3-flash",
                        "muse-spark-",
                    )
                )
            )
            # DeepSeek V4 through these gateways honors the DeepSeek-native
            # thinking payload and streams reasoning_content deltas (verified
            # live against OpenCAP). Without this, tier thinking_level settings
            # silently no-op for gateway DeepSeek routes.
            if basename.startswith("deepseek-v4"):
                return ModelCapabilities(
                    supports_reasoning=True,
                    supports_tools=True,
                    supports_vision=supports_vision,
                    reasoning_format="deepseek",
                )
            # GLM 5.x is OpenCAP's c2 default and reasons by default, emitting
            # reasoning_content whether or not it is asked to. The gateway
            # accepts Z.ai's own {"thinking": {"type": ...}} switch for it in
            # both positions (verified live against OpenCAP), so declaring the
            # format is what turns a tier's thinking_level from a no-op into a
            # real control. Scoped to opencap: the Bankr gateway serves an
            # overlapping catalog on a separate deployment that has not been
            # verified to accept the same switch, and guessing wrong there sends
            # a rejected parameter on every turn.
            if provider_id == "opencap" and basename.startswith("glm-5"):
                return ModelCapabilities(
                    supports_reasoning=True,
                    supports_tools=True,
                    supports_vision=supports_vision,
                    reasoning_format="zai",
                )
            return ModelCapabilities(
                supports_reasoning=False,
                supports_tools=True,
                supports_vision=supports_vision,
                reasoning_format="none",
            )
        if provider_id == "dashscope":
            supports_reasoning = model_l.startswith(
                (
                    "qwen3",
                    "qwen-plus",
                    "qwen-flash",
                    "qwen-turbo",
                    "qwen-max",
                    "qwq",
                )
            )
            return ModelCapabilities(
                supports_reasoning=supports_reasoning,
                supports_tools=True,
                supports_vision=model_l.startswith(("qwen3.5", "qwen3.6", "qwen-vl")),
                reasoning_format="dashscope" if supports_reasoning else "none",
            )
        if provider_id == "moonshot":
            supports_reasoning = model_l.startswith(("kimi-k2.5", "kimi-k2.6", "kimi-k2-thinking"))
            return ModelCapabilities(
                supports_reasoning=supports_reasoning,
                supports_tools=True,
                supports_vision=model_l.startswith(("kimi-k2.5", "kimi-k2.6")),
                reasoning_format="moonshot" if supports_reasoning else "none",
            )
        if provider_id in {"volcengine", "byteplus"}:
            supports_reasoning = (
                "thinking" in model_l
                or model_l.startswith("doubao-seed-2")
                or model_l.startswith("doubao-seed-1-8")
            )
            return ModelCapabilities(
                supports_reasoning=supports_reasoning,
                supports_tools=True,
                supports_vision=model_l.startswith(("doubao-seed-1-8", "doubao-seed-2")),
                reasoning_format="volcengine" if supports_reasoning else "none",
            )
        return ModelCapabilities(
            supports_tools=info.supports_tools if info else True,
            supports_vision=info.supports_vision if info else False,
        )

    async def fetch_openrouter(self, api_key: str, base_url: str, proxy: str = "") -> None:
        """Fetch model list from OpenRouter /api/v1/models endpoint.

        ``base_url`` MUST NOT end with ``/v1`` — boot.py strips it.
        URL constructed as: ``f"{base_url}/v1/models"``
        """
        url = f"{base_url}/v1/models"
        headers = {
            "Authorization": f"Bearer {clean_header_secret(api_key, label='OpenRouter API key')}"
        }
        headers.update(openrouter_app_headers(base_url))
        async with httpx.AsyncClient(
            timeout=10.0, trust_env=_trust_env(), proxy=proxy or None
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        self._populate_from_data(data.get("data", []))
        log.debug("model_catalog.fetched", count=len(self._models))

    async def fetch_bankr(self, base_url: str, api_key: str = "", proxy: str = "") -> None:
        """Fetch the live model list from the Bankr LLM Gateway.

        Bankr exposes an OpenAI-compatible ``{base_url}/models`` endpoint (the
        ``base_url`` already ends in ``/v1``). The key is sent both as
        ``Authorization: Bearer`` and ``X-API-Key`` so either accepted auth shape
        works; the endpoint tolerates missing keys for the public catalog.
        """
        url = f"{base_url.rstrip('/')}/models"
        headers = {}
        if api_key:
            cleaned = clean_header_secret(api_key, label="Bankr API key")
            headers["Authorization"] = f"Bearer {cleaned}"
            headers["X-API-Key"] = cleaned
        async with httpx.AsyncClient(
            timeout=10.0, trust_env=_trust_env(), proxy=proxy or None
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        self._populate_from_bankr(data.get("data", []))
        log.debug("model_catalog.fetched_bankr", count=len(self._models))

    async def fetch_opencap(self, proxy: str = "") -> dict[str, Any]:
        """Fetch OpenCAP's public, unauthenticated live model catalog."""
        url = get_provider_spec("opencap").model_catalog_url
        if not url:
            raise RuntimeError("OpenCAP model catalog URL is not configured")
        async with httpx.AsyncClient(
            timeout=10.0, trust_env=_trust_env(), proxy=proxy or None
        ) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            payload = resp.json()

        if not isinstance(payload, dict):
            raise ValueError("OpenCAP model catalog response must be a JSON object")
        data = cast(dict[str, Any], payload)

        self._populate_from_opencap(data.get("data", []))
        log.debug("model_catalog.fetched_opencap", count=len(self._models))
        return data

    async def fetch_surplus(self, proxy: str = "") -> dict[str, Any]:
        """Fetch Surplus Intelligence's public, unauthenticated live catalog."""
        url = get_provider_spec("surplus").model_catalog_url
        if not url:
            raise RuntimeError("Surplus model catalog URL is not configured")
        async with httpx.AsyncClient(
            timeout=10.0, trust_env=_trust_env(), proxy=proxy or None
        ) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            payload = resp.json()

        if not isinstance(payload, dict):
            raise ValueError("Surplus model catalog response must be a JSON object")
        data = cast(dict[str, Any], payload)

        self._populate_from_surplus(data.get("data", []))
        log.debug("model_catalog.fetched_surplus", count=len(self._models))
        return data

    def get(self, model_id: str) -> ModelInfo | None:
        """Look up model metadata by ID."""
        return self._models.get(model_id)

    def resolve_max_tokens(
        self, model_id: str, user_override: int = 0, provider_name: str = ""
    ) -> int:
        """Resolve max_tokens: user > catalog > static fallback > default, then clamp.

        A provider-specific fallback entry beats the shared static table when
        the same model id has different limits per provider.
        """
        context_window = self.resolve_context_window(model_id, provider_name)
        info = self._models.get(model_id)
        provider_fallback = _PROVIDER_STATIC_FALLBACK.get(provider_name.strip().lower(), {})

        using_user_override = user_override > 0
        if using_user_override:
            effective = user_override
        elif info and info.max_output_tokens > 0:
            effective = info.max_output_tokens
        elif model_id in provider_fallback:
            effective = provider_fallback[model_id][0]
        elif model_id in _STATIC_FALLBACK:
            effective = _STATIC_FALLBACK[model_id][0]
        else:
            effective = DEFAULT_MAX_TOKENS

        # Clamp to context window. Some provider catalogs report a model's
        # max_completion_tokens as almost the entire context window; using that
        # value as max_tokens leaves no room for ordinary prompt/tool/image input
        # and causes preventable context-limit failures.
        if context_window > 0:
            effective = min(effective, context_window)
            if (
                not using_user_override
                and context_window > DEFAULT_MAX_TOKENS
                and effective >= context_window - DEFAULT_MAX_TOKENS
            ):
                effective = min(effective, SAFE_OPENROUTER_DEFAULT_MAX_TOKENS)

        return effective

    def resolve_context_window(self, model_id: str, provider_name: str = "") -> int:
        """Resolve context window: catalog > provider fallback > static fallback > default."""
        info = self._models.get(model_id)
        if info and info.context_window > 0:
            return info.context_window
        provider_fallback = _PROVIDER_STATIC_FALLBACK.get(provider_name.strip().lower(), {})
        if model_id in provider_fallback:
            return provider_fallback[model_id][1]
        if model_id in _STATIC_FALLBACK:
            return _STATIC_FALLBACK[model_id][1]
        return DEFAULT_CONTEXT_WINDOW
