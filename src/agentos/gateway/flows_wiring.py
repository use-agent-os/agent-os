"""Composition root for Image Flows: real providers, LLM client and accounting.

``agentos.flows`` imports no other AgentOS package; this module is where it
meets the rest of the gateway:

* :class:`ProviderImageAdapter` calls the image-generation providers the
  gateway configures from ``[image_generation.providers.*]`` (OpenAI direct,
  OpenRouter), with the engine's retry policy.
* :class:`AuxTextAdapter` runs ``prompt_writer`` (task ``flow_prompt``) and
  ``describe_image`` (task ``vision``, same model hint as the ``image`` tool)
  through the auxiliary client.
* :func:`provider_auth` decides model availability from provider keys only.
* :func:`record_flow_cost` books node costs under the ``flow:<run_id>`` scope.

:func:`flow_service` is what every in-process caller uses (RPC handlers,
boot, agent tools): it builds the process-wide service on first use.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any

from agentos.flows.adapters import (
    AdapterRequest,
    AdapterResult,
    Sleep,
    TextResult,
    classify_http_error,
    with_retries,
)
from agentos.flows.models import ModelEntry, ModelRegistry
from agentos.flows.service import Broadcast, FlowService, get_flow_service
from agentos.flows.types import NodeError

PROVIDER_LABELS = {"openrouter": "OpenRouter", "openai": "OpenAI", "surplus": "Surplus"}

ProviderResolver = Callable[[str], Any]


def resolve_image_provider(provider_id: str, config: Any | None = None) -> Any:
    """The image provider behind a registry entry.

    All three (OpenAI, OpenRouter, Surplus) live in the shared image-generation
    registry, which the gateway configures from ``[image_generation.providers.*]``
    at boot and on every config commit. ``config`` is accepted for callers that
    hold one; the registry already reflects the live config.
    """
    from agentos.provider.image_generation import get_image_generation_provider

    del config
    return get_image_generation_provider(provider_id)


def provider_auth(
    provider_id: str, *, resolve: ProviderResolver | None = None
) -> tuple[bool, str | None]:
    """Whether the image provider has a key: ``(ok, reason_when_not)``.

    Reads the process-wide image-generation provider registry, configured
    from ``[image_generation.providers.*]`` (and the ``[llm]`` key when it is
    the same provider). ``[image_generation].enabled`` is deliberately not
    consulted: it gates only the ``image_generate`` tool.
    """
    provider = (resolve or resolve_image_provider)(provider_id)
    if provider is None:
        return False, f"No image provider registered for {provider_id}"
    env_name = str(getattr(provider, "_api_key_env", "") or "")
    resolve = getattr(provider, "_resolve_api_key", None)
    try:
        has_key = bool(resolve()) if callable(resolve) else True
    except Exception:  # noqa: BLE001 - a malformed key is "not available", not a crash
        has_key = False
    if has_key:
        return True, None
    return False, f"{env_name or provider_id + ' API key'} is not set"


def _offered(value: str | None, options: tuple[str, ...] | None) -> str | None:
    return value if value and options and value in options else None


def request_params(entry: ModelEntry, request: AdapterRequest, *, edit: bool) -> dict[str, Any]:
    """The optional request fields this model can honour, and nothing else.

    A field is sent only when the node set it and the entry exposes it (the
    registry already leaves out what a model cannot honour, e.g. resolution on
    GPT Image 2). An edit call is further limited to what its edit model lists.
    """
    params: dict[str, Any] = {
        "aspect_ratio": _offered(request.aspect_ratio, entry.ratios),
        "resolution": _offered(request.image_size, entry.tiers),
        "quality": _offered(request.quality, entry.qualities),
        "size": _offered(request.size, entry.sizes),
        "background": request.background if entry.transparent_bg else None,
    }
    if edit and entry.edit_api_model:
        if entry.edit_ratios is not None and params["aspect_ratio"] not in entry.edit_ratios:
            params["aspect_ratio"] = None
        if entry.edit_tiers is not None and params["resolution"] not in entry.edit_tiers:
            params["resolution"] = None
    return params


class ProviderImageAdapter:
    """One ``image`` node call against the configured image providers."""

    def __init__(
        self, *, sleep: Sleep = asyncio.sleep, resolve: ProviderResolver | None = None
    ) -> None:
        self._sleep = sleep
        self._resolve = resolve or resolve_image_provider

    async def generate(self, entry: ModelEntry, request: AdapterRequest) -> AdapterResult:
        from agentos.provider.image_generation import (
            ImageGenerationEmptyError,
            ImageGenerationRequest,
            ImageGenerationResult,
        )

        label = PROVIDER_LABELS.get(entry.provider, entry.provider)
        provider = self._resolve(entry.provider)
        if provider is None:
            raise NodeError("unavailable", f"No image provider registered for {entry.provider}")
        edit = bool(request.images)
        params = request_params(entry, request, edit=edit)
        provider_request = ImageGenerationRequest(
            prompt=request.prompt,
            model=entry.api_model_for(edit=edit),
            size=params["size"] or ("auto" if entry.provider == "surplus" else "1024x1024"),
            output_format="png",
            timeout_seconds=float(request.timeout_s or entry.timeout_s),
            images=list(request.images),
            n=max(1, int(request.n)),
            quality=params["quality"],
            background=params["background"],
            aspect_ratio=params["aspect_ratio"],
            image_size=params["resolution"],
        )
        try:
            result: ImageGenerationResult = await with_retries(
                lambda: provider.generate(provider_request), sleep=self._sleep
            )
        except asyncio.CancelledError:
            raise
        except ImageGenerationEmptyError as exc:
            text = (exc.text or "").strip()
            message = f"{label} returned no image: {text[:500]}" if text else str(exc)
            raise NodeError("empty_result", message) from exc
        except Exception as exc:
            raise classify_http_error(exc, provider=label) from exc
        images = list(result.images or [])
        if not images and result.image_bytes:
            images = [(result.image_bytes, result.mime_type)]
        return AdapterResult(images=images, cost_usd=result.cost_usd, text=result.text)


class AuxTextAdapter:
    """LLM nodes through the shared auxiliary client (accounted as ``aux:<task>``)."""

    async def complete(
        self,
        *,
        task: str,
        system: str,
        prompt: str,
        images: list[tuple[bytes, str]] | None = None,
        session_key: str | None = None,
    ) -> TextResult:
        from agentos.provider.auxiliary import AuxiliaryError, get_auxiliary_client
        from agentos.provider.types import (
            ChatConfig,
            ContentBlockImage,
            ContentBlockText,
            Message,
        )

        hint_provider = hint_model = default_model = ""
        content: Any
        if images:
            # The same model resolution as the ``image`` tool.
            from agentos.tools.builtin.media import (
                _DEFAULT_AUXILIARY_MEDIA_MODEL,
                _vision_model_hint,
            )

            hint_provider, hint_model = _vision_model_hint()
            default_model = _DEFAULT_AUXILIARY_MEDIA_MODEL
            blocks: list[Any] = [
                ContentBlockImage(media_type=mime, data=base64.b64encode(data).decode("ascii"))
                for data, mime in images
            ]
            blocks.append(ContentBlockText(text=prompt))
            content = blocks
        else:
            content = prompt
        try:
            result = await get_auxiliary_client().complete(
                task=task,
                messages=[Message(role="user", content=content)],
                preferred_provider=hint_provider,
                preferred_model=hint_model,
                default_model=default_model,
                chat_config=ChatConfig(max_tokens=1024, temperature=0.7, system=system),
                session_key=session_key,
            )
        except AuxiliaryError as exc:
            raise NodeError("unavailable", f"Model not available: {exc}") from exc
        return TextResult(text=result.text)


def record_flow_cost(
    tracker: Any,
    run_id: str,
    session_key: str | None,
    cost_usd: float,
    model_id: str,
    provider_id: str,
) -> None:
    """Book one node's cost in the usage tracker under scope ``flow:<run_id>``."""
    from agentos.engine.usage import usage_scope

    with usage_scope(f"flow:{run_id}"):
        tracker.add(
            session_key or f"flow:{run_id}",
            0,
            0,
            model_id=model_id,
            billed_cost=float(cost_usd),
            provider_id=provider_id,
        )


def build_flow_service(config: Any | None) -> FlowService:
    """A FlowService wired to the real providers, LLM client and cost tracker."""
    holder: list[FlowService] = []

    def resolve(provider_id: str) -> Any:
        return resolve_image_provider(provider_id, holder[0].config if holder else config)

    service = FlowService(
        config,
        image_adapter=ProviderImageAdapter(resolve=resolve),
        text_adapter=AuxTextAdapter(),
        cost_recorder=record_flow_cost,
    )
    holder.append(service)
    service.models = ModelRegistry(
        service.default_entries,
        auth=lambda provider_id: provider_auth(provider_id, resolve=resolve),
        primary=service.primary_model,
    )
    service.surplus_catalog.base_url = lambda: _surplus_base_url(service.config)
    return service


def _surplus_base_url(config: Any | None) -> str:
    from agentos.flows.surplus import SURPLUS_BASE_URL

    providers = getattr(getattr(config, "image_generation", None), "providers", None)
    base = getattr(getattr(providers, "surplus", None), "base_url", None)
    return base if isinstance(base, str) and base.strip() else SURPLUS_BASE_URL


def flow_service(
    config: Any | None = None,
    *,
    broadcast: Broadcast | None = None,
    usage_tracker: Any | None = None,
) -> FlowService:
    """The process-wide FlowService (built and recovered on first use)."""
    service = get_flow_service(
        config,
        broadcast=broadcast,
        usage_tracker=usage_tracker,
        factory=build_flow_service,
    )
    # The Surplus catalog is public; a stale copy refreshes in the background.
    service.surplus_catalog.ensure_fresh()
    return service
