"""Provider adapters for image generation."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field, replace
from typing import Protocol

import httpx

from agentos.env import trust_env as _trust_env
from agentos.provider.openrouter_attribution import openrouter_app_headers
from agentos.secrets import clean_header_secret


@dataclass
class ImageGenerationRequest:
    """One image request.

    Only ``prompt``, ``model`` and ``size`` are needed for plain text-to-image;
    the optional fields are what the Image Flows engine asks for. ``images``
    switches a provider into edit / multi-reference mode (``(bytes, mime)``
    pairs, already downscaled by the caller). ``aspect_ratio`` and
    ``image_size`` are the ratio/tier pair the Gemini image family takes;
    ``size`` stays the OpenAI-style ``WxH`` (or ``auto``).
    """

    prompt: str
    model: str
    size: str
    output_format: str = "png"
    timeout_seconds: float = 180.0
    images: list[tuple[bytes, str]] = field(default_factory=list)
    n: int = 1
    quality: str | None = None
    background: str | None = None
    aspect_ratio: str | None = None
    image_size: str | None = None


@dataclass
class ImageGenerationAttempt:
    provider: str
    model: str
    error: str


@dataclass
class ImageGenerationResult:
    """What a provider returned.

    ``image_bytes`` / ``mime_type`` are always the first image, so single-image
    callers (the ``image_generate`` tool) never look at ``images``. ``images``
    carries every returned image as ``(bytes, mime)``. ``cost_usd`` is the
    provider-reported cost when the API reports one, else ``None``; ``text`` is
    whatever text the model returned alongside (or instead of) the images.
    """

    image_bytes: bytes
    mime_type: str
    model: str
    provider: str
    revised_prompt: str | None = None
    attempts: list[ImageGenerationAttempt] = field(default_factory=list)
    images: list[tuple[bytes, str]] = field(default_factory=list)
    cost_usd: float | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        if not self.images and self.image_bytes:
            self.images = [(self.image_bytes, self.mime_type)]


class ImageGenerationEmptyError(RuntimeError):
    """The provider answered but returned no image (often a moderation refusal).

    ``text`` is the model's own explanation when it gave one.
    """

    def __init__(self, message: str, *, text: str | None = None) -> None:
        super().__init__(message)
        self.text = text


class ImageGenerationProvider(Protocol):
    provider_id: str
    default_model: str
    auth_env_vars: tuple[str, ...]

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult: ...


class OpenAIImageGenerationProvider:
    provider_id = "openai"
    default_model = "gpt-image-1"
    auth_env_vars: tuple[str, ...] = ("OPENAI_API_KEY",)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")

    def _resolve_api_key(self) -> str:
        return clean_header_secret(
            self._api_key or os.environ.get(self._api_key_env, ""),
            label=f"{self.provider_id} image API key",
        )

    def _api_url(self, path: str) -> str:
        if self._base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        return f"{self._base_url}{path}"

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        output_format = request.output_format.lower()
        options: dict[str, str] = {}
        if request.quality:
            options["quality"] = request.quality
        if request.background:
            options["background"] = request.background
        n = max(1, int(request.n or 1))
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
        ) as client:
            if request.images:
                # Edit / multi-reference: multipart with one ``image[]`` part per input.
                files = [
                    ("image[]", (f"image_{index}.{_ext_for_mime(mime)}", data, mime))
                    for index, (data, mime) in enumerate(request.images)
                ]
                form = {
                    "model": request.model,
                    "prompt": request.prompt,
                    "size": request.size,
                    "output_format": request.output_format,
                    "n": str(n),
                    **options,
                }
                response = await client.post(
                    self._api_url("/v1/images/edits"),
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=form,
                    files=files,
                )
            else:
                payload: dict[str, object] = {
                    "model": request.model,
                    "prompt": request.prompt,
                    "size": request.size,
                    "output_format": request.output_format,
                    "n": n,
                    **options,
                }
                response = await client.post(
                    self._api_url("/v1/images/generations"),
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()

        items = data.get("data") or []
        if not items:
            raise ImageGenerationEmptyError("Image generation provider returned no images")
        mime_type = "image/jpeg" if output_format in {"jpg", "jpeg"} else f"image/{output_format}"
        images: list[tuple[bytes, str]] = []
        for item in items:
            b64_json = item.get("b64_json") if isinstance(item, dict) else None
            if b64_json:
                images.append((base64.b64decode(b64_json), mime_type))
        if not images:
            raise ImageGenerationEmptyError("Image generation provider returned no b64_json")
        first = items[0] if isinstance(items[0], dict) else {}
        return ImageGenerationResult(
            image_bytes=images[0][0],
            mime_type=mime_type,
            model=request.model,
            provider=self.provider_id,
            revised_prompt=first.get("revised_prompt"),
            images=images,
        )


SURPLUS_BASE_URL = "https://api.surplusintelligence.ai/v1"
#: Header carrying what a Surplus call actually cost, in micro-USD (1 USD = 1_000_000).
SURPLUS_COST_HEADER = "x-si-buyer-cost-micro"


class SurplusImageGenerationProvider(OpenAIImageGenerationProvider):
    """Surplus Intelligence images: the OpenAI images API on another host.

    Differences from OpenAI that matter here:

    * ``response_format: b64_json`` is requested; ``output_format`` and
      ``background`` are not in the catalog's ``supported_parameters`` and are
      never sent.
    * ``aspect_ratio`` and ``resolution`` are sent when the caller sets them
      (catalog keys ``aspect_ratios`` / ``resolutions``; the request field names
      follow the catalog's ``supported_features`` and still need live
      confirmation).
    * Edits are multipart with one repeated ``image`` field per input (verified
      live for a single image; repeating it for several is not yet verified)
      and never carry ``n`` (the edit route rejects it: the engine fans out).
      When a multi-image edit is refused with 400/422 the call is repeated once
      with ``image[]`` parts; a failed call is charged zero, so that costs
      nothing extra.
    * The real cost comes from the ``x-si-buyer-cost-micro`` header (or
      ``usage.buyer_cost_micro`` in the body).

    Registered third in the shared provider registry (after OpenAI and
    OpenRouter), so the ``image_generate`` tool reaches it as
    ``surplus/venice-nano-banana-2`` only when routing names it or when
    auto-detection runs out of the other two.
    """

    provider_id = "surplus"
    default_model = "venice-nano-banana-2"
    auth_env_vars: tuple[str, ...] = ("SURPLUS_API_KEY",)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = "SURPLUS_API_KEY",
        base_url: str = SURPLUS_BASE_URL,
    ) -> None:
        super().__init__(api_key=api_key, api_key_env=api_key_env, base_url=base_url)

    def _fields(self, request: ImageGenerationRequest) -> dict[str, str]:
        fields: dict[str, str] = {
            "model": request.model,
            "prompt": request.prompt,
            "response_format": "b64_json",
        }
        if request.size and request.size != "auto":
            fields["size"] = request.size
        if request.quality and request.quality != "auto":
            fields["quality"] = request.quality
        if request.aspect_ratio:
            fields["aspect_ratio"] = request.aspect_ratio
        if request.image_size:
            fields["resolution"] = request.image_size
        return fields

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")
        headers = {"Authorization": f"Bearer {api_key}"}
        fields = self._fields(request)
        n = max(1, int(request.n or 1))
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
        ) as client:
            if request.images:
                response = await self._post_edit(client, headers, fields, request.images, "image")
                if response.status_code in (400, 422) and len(request.images) > 1:
                    response = await self._post_edit(
                        client, headers, fields, request.images, "image[]"
                    )
            else:
                response = await client.post(
                    self._api_url("/v1/images/generations"),
                    headers=headers,
                    json={**fields, "n": n},
                )
            response.raise_for_status()
            data = response.json()
            images = await _collect_openai_images(client, data)
        if not images:
            raise ImageGenerationEmptyError("Image generation provider returned no images")
        return ImageGenerationResult(
            image_bytes=images[0][0],
            mime_type=images[0][1],
            model=request.model,
            provider=self.provider_id,
            images=images,
            cost_usd=surplus_cost_usd(response, data),
        )

    async def _post_edit(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        fields: dict[str, str],
        images: list[tuple[bytes, str]],
        field_name: str,
    ) -> httpx.Response:
        files = [
            (field_name, (f"image_{index}.{_ext_for_mime(mime)}", data, mime))
            for index, (data, mime) in enumerate(images)
        ]
        return await client.post(
            self._api_url("/v1/images/edits"),
            headers=headers,
            data=fields,
            files=files,
        )


def surplus_cost_usd(response: httpx.Response, data: object) -> float | None:
    """USD from ``x-si-buyer-cost-micro`` (header first, then ``usage.buyer_cost_micro``)."""
    candidates: list[object] = [response.headers.get(SURPLUS_COST_HEADER)]
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict):
        candidates.append(usage.get("buyer_cost_micro"))
    for raw in candidates:
        if raw is None or isinstance(raw, bool):
            continue
        try:
            micro = float(str(raw).strip())
        except ValueError:
            continue
        if micro >= 0:
            return micro / 1_000_000
    return None


def sniff_image_mime(data: bytes, default: str = "image/png") -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] in (b"GIF8",):
        return "image/gif"
    return default


async def _collect_openai_images(
    client: httpx.AsyncClient, data: object
) -> list[tuple[bytes, str]]:
    """Every image of an OpenAI-shaped ``data[]`` answer (``b64_json`` or ``url``)."""
    items = data.get("data") if isinstance(data, dict) else None
    images: list[tuple[bytes, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        b64_json = item.get("b64_json")
        url = item.get("url")
        if isinstance(b64_json, str) and b64_json:
            raw = base64.b64decode(b64_json)
            images.append((raw, sniff_image_mime(raw)))
        elif isinstance(url, str) and url.startswith("data:"):
            mime, raw = _decode_data_url(url)
            images.append((raw, sniff_image_mime(raw, mime)))
        elif isinstance(url, str) and url.startswith("https://"):
            fetched = await client.get(url)
            fetched.raise_for_status()
            images.append((fetched.content, sniff_image_mime(fetched.content)))
    return images


def build_surplus_image_provider(
    image_config: object | None = None,
    *,
    llm_config: object | None = None,
) -> SurplusImageGenerationProvider:
    """A Surplus provider from ``[image_generation.providers.surplus]``.

    The key falls back to ``SURPLUS_API_KEY`` and then to ``[llm]`` when the
    configured LLM provider is ``surplus``, like the other image providers.
    """
    providers_config = getattr(image_config, "providers", None)
    surplus_config = getattr(providers_config, "surplus", None)
    return SurplusImageGenerationProvider(
        api_key=_resolve_configured_api_key(
            provider_id="surplus",
            provider_config=surplus_config,
            llm_config=llm_config,
            default_env="SURPLUS_API_KEY",
        ),
        api_key_env=_get_config_attr(surplus_config, "api_key_env", "SURPLUS_API_KEY")
        or "SURPLUS_API_KEY",
        base_url=_resolve_configured_base_url(
            provider_id="surplus",
            provider_config=surplus_config,
            llm_config=llm_config,
            default_base_url=SURPLUS_BASE_URL,
        ),
    )


class OpenRouterImageGenerationProvider:
    provider_id = "openrouter"
    default_model = "google/gemini-3.1-flash-image-preview"
    auth_env_vars: tuple[str, ...] = ("OPENROUTER_API_KEY",)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = "OPENROUTER_API_KEY",
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")

    def _resolve_api_key(self) -> str:
        return clean_header_secret(
            self._api_key or os.environ.get(self._api_key_env, ""),
            label=f"{self.provider_id} image API key",
        )

    def _api_url(self, path: str) -> str:
        if self._base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        return f"{self._base_url}{path}"

    @staticmethod
    def _image_config_for_size(size: str) -> dict[str, str]:
        aspect_ratio = {
            "1024x1024": "1:1",
            "1536x1024": "3:2",
            "1024x1536": "2:3",
        }.get(size, "1:1")
        return {"aspect_ratio": aspect_ratio, "image_size": "1K"}

    @classmethod
    def _image_config(cls, request: ImageGenerationRequest) -> dict[str, str]:
        config = cls._image_config_for_size(request.size)
        if request.aspect_ratio:
            config["aspect_ratio"] = request.aspect_ratio
        if request.image_size:
            config["image_size"] = request.image_size
        return config

    @staticmethod
    def _user_content(request: ImageGenerationRequest) -> str | list[dict[str, object]]:
        if not request.images:
            return request.prompt
        parts: list[dict[str, object]] = [{"type": "text", "text": request.prompt}]
        for data, mime in request.images:
            encoded = base64.b64encode(data).decode("ascii")
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
            )
        return parts

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        payload = {
            "model": request.model,
            "messages": [{"role": "user", "content": self._user_content(request)}],
            "modalities": ["image", "text"],
            "stream": False,
            "image_config": self._image_config(request),
            # Usage accounting: the response then carries ``usage.cost`` in USD.
            "usage": {"include": True},
        }
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
        ) as client:
            response = await client.post(
                self._api_url("/v1/chat/completions"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    **openrouter_app_headers(self._base_url),
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        urls = _extract_openrouter_image_urls(data)
        text = _extract_openrouter_text(data)
        if not urls:
            message = "Image generation provider returned no images"
            if text:
                message = f"{message}: {text[:500]}"
            raise ImageGenerationEmptyError(message, text=text)
        images = [_decode_data_url(url) for url in urls]
        return ImageGenerationResult(
            image_bytes=images[0][1],
            mime_type=images[0][0],
            model=request.model,
            provider=self.provider_id,
            images=[(image_bytes, mime) for mime, image_bytes in images],
            cost_usd=_extract_openrouter_cost(data),
            text=text,
        )


def _extract_openrouter_image_url(data: dict) -> str | None:
    urls = _extract_openrouter_image_urls(data)
    return urls[0] if urls else None


def _extract_openrouter_image_urls(data: dict) -> list[str]:
    """Every image URL in every choice, in order."""
    urls: list[str] = []
    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        for image in message.get("images") or []:
            image_url = image.get("image_url") or image.get("imageUrl") or {}
            url = image_url.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _extract_openrouter_text(data: dict) -> str | None:
    """The text the model returned with (or instead of) its images."""
    parts: list[str] = []
    for choice in data.get("choices") or []:
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    text = "\n".join(part.strip() for part in parts if part and part.strip())
    return text or None


def _extract_openrouter_cost(data: dict) -> float | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return None
    return float(cost)


def _ext_for_mime(mime: str) -> str:
    subtype = (mime or "image/png").split("/", 1)[-1].lower()
    return {"jpeg": "jpg", "svg+xml": "svg"}.get(subtype, subtype or "png")


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    prefix, sep, encoded = data_url.partition(",")
    if not sep or ";base64" not in prefix:
        raise RuntimeError("Image generation provider returned unsupported image URL")
    mime_type = prefix.removeprefix("data:").split(";", 1)[0] or "image/png"
    return mime_type, base64.b64decode(encoded)


_PROVIDERS: dict[str, ImageGenerationProvider] = {}


def _get_config_attr(config: object | None, name: str, default: str = "") -> str:
    value = getattr(config, name, default) if config is not None else default
    return value if isinstance(value, str) else default


def _field_was_set(config: object | None, name: str) -> bool:
    fields_set = getattr(config, "model_fields_set", None)
    return isinstance(fields_set, set) and name in fields_set


def _llm_provider_matches(llm_config: object | None, provider_id: str) -> bool:
    provider = _get_config_attr(llm_config, "provider").strip().lower()
    return provider == provider_id


def _resolve_configured_api_key(
    *,
    provider_id: str,
    provider_config: object | None,
    llm_config: object | None,
    default_env: str,
) -> str | None:
    env_name = _get_config_attr(provider_config, "api_key_env", default_env) or default_env
    explicit = _get_config_attr(provider_config, "api_key")
    if explicit:
        return explicit

    env_value = os.environ.get(env_name, "")
    if env_value:
        return env_value

    if _llm_provider_matches(llm_config, provider_id):
        return _get_config_attr(llm_config, "api_key") or None
    return None


def _resolve_configured_base_url(
    *,
    provider_id: str,
    provider_config: object | None,
    llm_config: object | None,
    default_base_url: str,
) -> str:
    base_url = _get_config_attr(provider_config, "base_url", default_base_url) or default_base_url
    if not _field_was_set(provider_config, "base_url") and _llm_provider_matches(
        llm_config, provider_id
    ):
        return _get_config_attr(llm_config, "base_url", base_url) or base_url
    return base_url


def register_image_generation_provider(provider: ImageGenerationProvider) -> None:
    _PROVIDERS[provider.provider_id] = provider


def reset_image_generation_providers(
    image_config: object | None = None,
    *,
    llm_config: object | None = None,
) -> None:
    _PROVIDERS.clear()
    providers_config = getattr(image_config, "providers", None)
    openai_config = getattr(providers_config, "openai", None)
    openrouter_config = getattr(providers_config, "openrouter", None)

    register_image_generation_provider(
        OpenAIImageGenerationProvider(
            api_key=_resolve_configured_api_key(
                provider_id="openai",
                provider_config=openai_config,
                llm_config=llm_config,
                default_env="OPENAI_API_KEY",
            ),
            api_key_env=_get_config_attr(openai_config, "api_key_env", "OPENAI_API_KEY")
            or "OPENAI_API_KEY",
            base_url=_resolve_configured_base_url(
                provider_id="openai",
                provider_config=openai_config,
                llm_config=llm_config,
                default_base_url="https://api.openai.com/v1",
            ),
        )
    )
    register_image_generation_provider(
        OpenRouterImageGenerationProvider(
            api_key=_resolve_configured_api_key(
                provider_id="openrouter",
                provider_config=openrouter_config,
                llm_config=llm_config,
                default_env="OPENROUTER_API_KEY",
            ),
            api_key_env=_get_config_attr(openrouter_config, "api_key_env", "OPENROUTER_API_KEY")
            or "OPENROUTER_API_KEY",
            base_url=_resolve_configured_base_url(
                provider_id="openrouter",
                provider_config=openrouter_config,
                llm_config=llm_config,
                default_base_url="https://openrouter.ai/api/v1",
            ),
        )
    )
    # Registered last: the ``image_generate`` tool only auto-detects it after
    # OpenAI and OpenRouter, and only without explicit model routing; naming
    # ``surplus/<model>`` in primary/fallbacks selects it directly.
    register_image_generation_provider(
        build_surplus_image_provider(image_config, llm_config=llm_config)
    )


def list_image_generation_providers() -> list[ImageGenerationProvider]:
    return list(_PROVIDERS.values())


def get_image_generation_provider(provider_id: str) -> ImageGenerationProvider | None:
    return _PROVIDERS.get(provider_id)


def parse_image_generation_model_ref(raw: str) -> tuple[str, str]:
    provider, sep, model = raw.partition("/")
    if not sep or not provider.strip() or not model.strip():
        raise ValueError(f"Invalid image generation model ref: {raw!r}")
    return provider.strip(), model.strip()


async def generate_with_fallbacks(
    *,
    request: ImageGenerationRequest,
    candidates: list[str],
) -> ImageGenerationResult:
    attempts: list[ImageGenerationAttempt] = []
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            provider_id, model = parse_image_generation_model_ref(candidate)
        except ValueError as exc:
            attempts.append(ImageGenerationAttempt(provider="", model=candidate, error=str(exc)))
            last_error = exc
            continue
        provider = get_image_generation_provider(provider_id)
        if provider is None:
            error = f"No image generation provider registered for {provider_id}"
            attempts.append(ImageGenerationAttempt(provider_id, model, error))
            last_error = RuntimeError(error)
            continue
        try:
            result = await provider.generate(replace(request, model=model))
            if not result.image_bytes:
                raise RuntimeError("Image generation provider returned empty image")
            result.attempts = attempts
            return result
        except Exception as exc:  # noqa: BLE001 - failures are summarized for fallback
            attempts.append(ImageGenerationAttempt(provider_id, model, str(exc)))
            last_error = exc if isinstance(exc, Exception) else RuntimeError(str(exc))

    if len(attempts) <= 1 and last_error is not None:
        raise last_error
    summary = " | ".join(
        f"{attempt.provider}/{attempt.model}: {attempt.error}" for attempt in attempts
    )
    raise RuntimeError(f"All image generation models failed ({len(attempts)}): {summary}")


reset_image_generation_providers()
