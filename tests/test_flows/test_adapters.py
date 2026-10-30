"""Provider adapters over a mocked transport: t2i vs edit, multi-image, cost, retries."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import httpx
import pytest

from agentos.flows.adapters import AdapterRequest, classify_http_error, with_retries
from agentos.flows.models import REGISTRY
from agentos.flows.types import NodeError
from agentos.gateway.flows_wiring import ProviderImageAdapter
from agentos.provider.image_generation import (
    ImageGenerationEmptyError,
    ImageGenerationRequest,
    ImageGenerationResult,
    OpenAIImageGenerationProvider,
    OpenRouterImageGenerationProvider,
    register_image_generation_provider,
    reset_image_generation_providers,
)
from tests.test_flows.conftest import png_bytes

_REAL_CLIENT = httpx.AsyncClient
B64_A = base64.b64encode(b"image-a").decode()
B64_B = base64.b64encode(b"image-b").decode()


def _install(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> list:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        "agentos.provider.image_generation.httpx.AsyncClient",
        lambda **kwargs: _REAL_CLIENT(transport=httpx.MockTransport(record), **kwargs),
    )
    return seen


async def test_openai_text_to_image_uses_generations_with_n_quality_background(
    monkeypatch,
) -> None:
    seen = _install(
        monkeypatch,
        lambda r: httpx.Response(200, json={"data": [{"b64_json": B64_A}, {"b64_json": B64_B}]}),
    )
    provider = OpenAIImageGenerationProvider(api_key="sk-test")
    result = await provider.generate(
        ImageGenerationRequest(
            prompt="a fox",
            model="gpt-image-1",
            size="1024x1536",
            n=2,
            quality="high",
            background="transparent",
        )
    )
    assert [str(r.url) for r in seen] == ["https://api.openai.com/v1/images/generations"]
    assert json.loads(seen[0].content) == {
        "model": "gpt-image-1",
        "prompt": "a fox",
        "size": "1024x1536",
        "output_format": "png",
        "n": 2,
        "quality": "high",
        "background": "transparent",
    }
    assert result.images == [(b"image-a", "image/png"), (b"image-b", "image/png")]
    assert result.image_bytes == b"image-a" and result.cost_usd is None


async def test_openai_with_images_uses_edits_multipart(monkeypatch) -> None:
    seen = _install(
        monkeypatch, lambda r: httpx.Response(200, json={"data": [{"b64_json": B64_A}]})
    )
    provider = OpenAIImageGenerationProvider(api_key="sk-test")
    result = await provider.generate(
        ImageGenerationRequest(
            prompt="combine",
            model="gpt-image-1",
            size="auto",
            images=[(b"\x89PNG-one", "image/png"), (b"\xff\xd8-two", "image/jpeg")],
            quality="low",
        )
    )
    request = seen[0]
    assert str(request.url) == "https://api.openai.com/v1/images/edits"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert body.count(b'name="image[]"') == 2
    assert b'filename="image_0.png"' in body and b'filename="image_1.jpg"' in body
    assert b"\x89PNG-one" in body and b"\xff\xd8-two" in body
    for field, value in (("prompt", b"combine"), ("n", b"1"), ("quality", b"low")):
        assert f'name="{field}"'.encode() in body and value in body
    assert b'name="background"' not in body
    assert result.images == [(b"image-a", "image/png")]


async def test_openrouter_sends_reference_images_and_collects_every_image(monkeypatch) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": "Here you go.",
                    "images": [
                        {"image_url": {"url": f"data:image/png;base64,{B64_A}"}},
                        {"image_url": {"url": f"data:image/webp;base64,{B64_B}"}},
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "cost": 0.0412},
    }
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json=response))
    provider = OpenRouterImageGenerationProvider(api_key="or-test")
    result = await provider.generate(
        ImageGenerationRequest(
            prompt="put the cat on the sofa",
            model="google/gemini-3.1-flash-image-preview",
            size="1024x1024",
            images=[(b"cat", "image/png"), (b"sofa", "image/jpeg")],
            aspect_ratio="16:9",
            image_size="2K",
        )
    )
    payload = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert payload["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "put the cat on the sofa"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64.b64encode(b'cat').decode()}"
                    },
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64.b64encode(b'sofa').decode()}"
                    },
                },
            ],
        }
    ]
    assert payload["image_config"] == {"aspect_ratio": "16:9", "image_size": "2K"}
    assert payload["modalities"] == ["image", "text"]
    assert payload["usage"] == {"include": True}
    assert "n" not in payload
    assert result.images == [(b"image-a", "image/png"), (b"image-b", "image/webp")]
    assert result.cost_usd == pytest.approx(0.0412)
    assert result.text == "Here you go."


async def test_openrouter_without_images_keeps_the_plain_prompt(monkeypatch) -> None:
    ok = {
        "choices": [
            {"message": {"images": [{"image_url": {"url": f"data:image/png;base64,{B64_A}"}}]}}
        ]
    }
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json=ok))
    provider = OpenRouterImageGenerationProvider(api_key="or-test")
    result = await provider.generate(
        ImageGenerationRequest(prompt="a fox", model="m", size="1536x1024")
    )
    payload = json.loads(seen[0].content)
    assert payload["messages"] == [{"role": "user", "content": "a fox"}]
    assert payload["image_config"] == {"aspect_ratio": "3:2", "image_size": "1K"}
    assert result.image_bytes == b"image-a" and result.cost_usd is None


async def test_openrouter_refusal_surfaces_the_model_text(monkeypatch) -> None:
    refusal = {"choices": [{"message": {"content": "I can't help with that request."}}]}
    _install(monkeypatch, lambda r: httpx.Response(200, json=refusal))
    provider = OpenRouterImageGenerationProvider(api_key="or-test")
    with pytest.raises(ImageGenerationEmptyError) as excinfo:
        await provider.generate(ImageGenerationRequest(prompt="x", model="m", size="1024x1024"))
    assert excinfo.value.text == "I can't help with that request."
    # Through the flows adapter it becomes a node error carrying the model's words.
    register_image_generation_provider(provider)
    nb2 = next(entry for entry in REGISTRY if entry.id == "openrouter/nano-banana-2")
    try:
        with pytest.raises(NodeError) as failure:
            await ProviderImageAdapter().generate(nb2, AdapterRequest(prompt="x"))
    finally:
        reset_image_generation_providers()
    assert failure.value.code == "empty_result"
    assert failure.value.message == "OpenRouter returned no image: I can't help with that request."


def test_single_image_results_still_expose_images() -> None:
    result = ImageGenerationResult(image_bytes=b"x", mime_type="image/png", model="m", provider="p")
    assert result.images == [(b"x", "image/png")]


async def test_retries_honor_retry_after_then_succeed() -> None:
    sleeps: list[float] = []
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(503),
            httpx.Response(200),
        ]
    )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def call() -> str:
        response = next(responses)
        response.request = httpx.Request("POST", "https://example.test")
        response.raise_for_status()
        return "ok"

    assert await with_retries(call, sleep=sleep) == "ok"
    assert sleeps == [2.0, 2.0]  # Retry-After, then backoff 1 s x 2^1


async def test_retries_stop_after_three_and_cap_the_wait() -> None:
    sleeps: list[float] = []
    attempts = 0

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def call() -> None:
        nonlocal attempts
        attempts += 1
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(500, headers={"retry-after": "600"}, request=request)
        response.raise_for_status()

    with pytest.raises(httpx.HTTPStatusError):
        await with_retries(call, sleep=sleep)
    assert attempts == 4 and sleeps == [30.0, 30.0, 30.0]


async def test_client_errors_are_not_retried() -> None:
    attempts = 0

    async def call() -> None:
        nonlocal attempts
        attempts += 1
        request = httpx.Request("POST", "https://example.test")
        httpx.Response(400, request=request).raise_for_status()

    async def no_sleep(delay: float) -> None:
        raise AssertionError("must not sleep")

    with pytest.raises(httpx.HTTPStatusError):
        await with_retries(call, sleep=no_sleep)
    assert attempts == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "auth"),
        (402, "credits"),
        (429, "rate_limited"),
        (400, "provider_rejected"),
        (502, "provider_error"),
    ],
)
def test_http_errors_are_classified(status: int, code: str) -> None:
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(status, json={"error": {"message": "nope"}}, request=request)
    failure = classify_http_error(httpx.HTTPStatusError("x", request=request, response=response))
    assert failure.code == code and failure.message.endswith("nope")


async def test_provider_adapter_picks_edit_for_gpt_and_retries_429(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"data": [{"b64_json": B64_A}]})

    _install(monkeypatch, handler)
    register_image_generation_provider(OpenAIImageGenerationProvider(api_key="sk-test"))
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    try:
        gpt = next(entry for entry in REGISTRY if entry.provider == "openai")
        result = await ProviderImageAdapter(sleep=sleep).generate(
            gpt,
            AdapterRequest(prompt="edit", images=[(png_bytes(), "image/png")], size="auto"),
        )
        assert calls == ["/v1/images/edits", "/v1/images/edits"] and sleeps == [0.0]
        assert result.images == [(b"image-a", "image/png")]
        calls.clear()
        await ProviderImageAdapter(sleep=sleep).generate(gpt, AdapterRequest(prompt="t2i"))
        assert calls[-1] == "/v1/images/generations"
    finally:
        reset_image_generation_providers()


async def test_provider_adapter_maps_402_and_missing_keys(monkeypatch) -> None:
    _install(monkeypatch, lambda r: httpx.Response(402, json={"error": {"message": "low"}}))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    register_image_generation_provider(OpenRouterImageGenerationProvider(api_key="or-test"))
    nb2 = next(entry for entry in REGISTRY if entry.id == "openrouter/nano-banana-2")
    try:
        with pytest.raises(NodeError) as excinfo:
            await ProviderImageAdapter().generate(nb2, AdapterRequest(prompt="x"))
        assert excinfo.value.code == "credits"
        assert excinfo.value.message == "OpenRouter: insufficient credits: low"
        register_image_generation_provider(OpenRouterImageGenerationProvider(api_key=""))
        with pytest.raises(NodeError) as excinfo:
            await ProviderImageAdapter().generate(nb2, AdapterRequest(prompt="x"))
        assert excinfo.value.code == "unavailable"
        assert excinfo.value.message == "OPENROUTER_API_KEY is not set"
    finally:
        reset_image_generation_providers()
