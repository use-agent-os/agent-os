"""Surplus Intelligence images: catalog pairing, edit-id selection, multipart, cost, fallback."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agentos.flows.adapters import AdapterRequest
from agentos.flows.surplus import (
    CATALOG_TTL_S,
    FAMILY_MODEL_IDS,
    SNAPSHOT_ENTRIES,
    SurplusCatalog,
    entries_from_models,
)
from agentos.flows.surplus_snapshot import MODELS
from agentos.flows.types import NodeError
from agentos.gateway.config import GatewayConfig, LlmProviderConfig
from agentos.gateway.flows_wiring import ProviderImageAdapter, build_flow_service, provider_auth
from agentos.provider.image_generation import (
    ImageGenerationRequest,
    SurplusImageGenerationProvider,
    reset_image_generation_providers,
    surplus_cost_usd,
)
from tests.test_flows.conftest import png_bytes, t2i_doc

_REAL_CLIENT = httpx.AsyncClient
PNG = png_bytes("teal", (48, 32))
B64 = base64.b64encode(PNG).decode()
BY_ID = {entry.id: entry for entry in SNAPSHOT_ENTRIES}


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


def _ok(cost_micro: str | None = "12000") -> httpx.Response:
    headers = {"x-si-buyer-cost-micro": cost_micro} if cost_micro is not None else {}
    return httpx.Response(200, json={"created": 1, "data": [{"b64_json": B64}]}, headers=headers)


def _form_fields(request: httpx.Request) -> list[tuple[str, bytes]]:
    """``(name, value)`` of every multipart part, in order."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    parts = []
    for chunk in request.content.split(b"--" + boundary)[1:-1]:
        head, _, body = chunk.partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        parts.append((name, body.rstrip(b"\r\n")))
    return parts


# ── catalog → registry ────────────────────────────────────────────────────


def test_families_pair_text_to_image_and_edit_ids() -> None:
    pairs = {e.id: (e.api_model, e.edit_api_model, e.edit) for e in SNAPSHOT_ENTRIES}
    assert pairs == {
        "surplus/nano-banana-2": ("venice-nano-banana-2", "nano-banana-2-edit", True),
        "surplus/nano-banana-2.1": ("venice-nano-banana-2-1", "nano-banana-2-1-edit", True),
        "surplus/nano-banana-pro": ("venice-nano-banana-pro", "nano-banana-pro-edit", True),
        "surplus/gpt-image-2": ("venice-gpt-image-2", "gpt-image-2-edit", True),
        "surplus/seedream-v5-lite": ("venice-seedream-v5-lite", "seedream-v5-lite-edit", True),
        "surplus/flux-2-pro": ("venice-flux-2-pro", None, False),  # no edit twin
        "surplus/qwen-image-2": ("venice-qwen-image-2", "qwen-image-2-edit", True),
    }
    for entry in SNAPSHOT_ENTRIES:
        assert entry.provider == "surplus" and entry.n_max == 1 and entry.timeout_s >= 180
        assert entry.api_model_for(edit=False) == entry.api_model
        assert entry.api_model_for(edit=True) == (entry.edit_api_model or entry.api_model)
        assert entry.price_source.startswith("Surplus catalog 2026-10-29")


def test_caps_come_from_the_catalog() -> None:
    nb2 = BY_ID["surplus/nano-banana-2"]
    assert nb2.ratios == ("1:1", "3:2", "16:9", "21:9", "9:16", "2:3", "3:4", "4:5")
    assert nb2.tiers == ("1K", "2K", "4K") and nb2.sizes == () and nb2.qualities == ()
    assert nb2.max_images == 14
    assert nb2.edit_ratios is not None and "auto" in nb2.edit_ratios
    gpt = BY_ID["surplus/gpt-image-2"]
    assert gpt.qualities == ("high", "medium", "low") and gpt.max_images == 16
    nb21 = BY_ID["surplus/nano-banana-2.1"]
    assert nb21.tiers == ("1K", "2K", "4K") and nb21.edit_tiers == ()  # edit model has none
    assert BY_ID["surplus/seedream-v5-lite"].tiers == ()
    assert BY_ID["surplus/flux-2-pro"].max_images == 0


def test_prices_per_image_edit_and_token_metered() -> None:
    gpt = BY_ID["surplus/gpt-image-2"]
    assert gpt.per_image == {"default": 0.27} and gpt.edit_per_image == {"default": 0.36}
    assert gpt.per_ref == 0.0
    assert gpt.estimate({"quality": "high"}, count=2, refs=0)["usd"] == pytest.approx(0.54)
    assert gpt.estimate({"quality": "high"}, count=2, refs=1)["usd"] == pytest.approx(0.72)
    seedream = BY_ID["surplus/seedream-v5-lite"]
    assert seedream.edit_per_image is None  # same price either way
    assert seedream.estimate({}, count=1, refs=3)["usd"] == pytest.approx(0.05)
    # venice-nano-banana-2 is token-metered: a range up to the edit model's price.
    nb2 = BY_ID["surplus/nano-banana-2"]
    assert nb2.per_image == {"1K": 0.0034, "2K": 0.005, "4K": 0.0076}
    assert nb2.estimate({"resolution": "1K"}, count=1, refs=0) == {
        "kind": "range",
        "min": 0.0034,
        "max": 0.1,
        "approximate": True,
    }
    assert nb2.estimate({"resolution": "4K"}, count=1, refs=2)["usd"] == pytest.approx(0.1)


def test_a_family_whose_t2i_model_is_missing_is_dropped() -> None:
    models = [m for m in MODELS if m["id"] != "venice-qwen-image-2"]
    ids = {e.id for e in entries_from_models(models, as_of="x")}
    assert "surplus/qwen-image-2" not in ids and "surplus/nano-banana-2" in ids


# ── catalog cache ─────────────────────────────────────────────────────────


def _catalog_client(handler: Callable[[httpx.Request], httpx.Response]):
    return lambda: _REAL_CLIENT(transport=httpx.MockTransport(handler))


async def test_catalog_falls_back_to_the_snapshot_then_caches_a_live_copy(tmp_path) -> None:
    now = [1_800_000_000.0]
    live = json.loads(json.dumps(MODELS))
    for model in live:
        if model["id"] == "venice-gpt-image-2":
            model["pricing"]["image"] = "0.25"
    other = {"id": "some-chat-model", "pricing": {"prompt": "0.1"}}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [*live, other]})

    catalog = SurplusCatalog(
        tmp_path / "surplus_catalog.json",
        base_url=lambda: "https://surplus.test/v1/",
        clock=lambda: now[0],
        client_factory=_catalog_client(handler),
    )
    assert catalog.stale() and catalog.entries() == SNAPSHOT_ENTRIES
    assert await catalog.refresh() is True
    assert seen == ["https://surplus.test/v1/models"]
    cached = json.loads((tmp_path / "surplus_catalog.json").read_text())
    assert {m["id"] for m in cached["data"]} == FAMILY_MODEL_IDS  # only the families
    entries = {e.id: e for e in catalog.entries()}
    assert entries["surplus/gpt-image-2"].per_image == {"default": 0.25}
    assert not catalog.stale()
    now[0] += CATALOG_TTL_S + 1
    assert catalog.stale()
    # Offline: the refresh fails, the stale copy keeps serving.
    offline = SurplusCatalog(
        tmp_path / "surplus_catalog.json",
        clock=lambda: now[0],
        client_factory=_catalog_client(lambda r: httpx.Response(503)),
    )
    assert await offline.refresh() is False
    assert {e.id: e for e in offline.entries()}["surplus/gpt-image-2"].per_image == {
        "default": 0.25
    }


async def test_a_corrupt_cache_or_catalog_without_families_uses_the_snapshot(tmp_path) -> None:
    path = tmp_path / "surplus_catalog.json"
    path.write_text("{not json")
    catalog = SurplusCatalog(
        path, client_factory=_catalog_client(lambda r: httpx.Response(200, json={"data": []}))
    )
    assert catalog.entries() == SNAPSHOT_ENTRIES
    assert await catalog.refresh() is False  # nothing usable: the cache is not replaced
    assert path.read_text() == "{not json"


async def test_ensure_fresh_refreshes_in_the_background_once(tmp_path, monkeypatch) -> None:
    calls = 0
    catalog_off = SurplusCatalog(tmp_path / "off.json")
    catalog_off.ensure_fresh()  # the suite runs with AGENTOS_SURPLUS_LIVE_PRICING=0
    assert catalog_off._task is None
    monkeypatch.setenv("AGENTOS_SURPLUS_LIVE_PRICING", "1")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": MODELS})

    catalog = SurplusCatalog(tmp_path / "c.json", client_factory=_catalog_client(handler))
    catalog.ensure_fresh()
    catalog.ensure_fresh()  # one in flight: not started twice
    assert catalog._task is not None
    assert await catalog._task is True and calls == 1
    catalog.ensure_fresh()  # fresh now: nothing to do
    assert calls == 1


# ── provider ──────────────────────────────────────────────────────────────


async def test_generation_payload_and_cost_header(monkeypatch) -> None:
    seen = _install(monkeypatch, lambda r: _ok("12000"))
    provider = SurplusImageGenerationProvider(api_key="inf_test")
    result = await provider.generate(
        ImageGenerationRequest(
            prompt="a fox",
            model="venice-nano-banana-2",
            size="auto",
            aspect_ratio="3:2",
            image_size="2K",
            n=2,
        )
    )
    request = seen[0]
    assert str(request.url) == "https://api.surplusintelligence.ai/v1/images/generations"
    assert request.headers["authorization"] == "Bearer inf_test"
    assert json.loads(request.content) == {
        "model": "venice-nano-banana-2",
        "prompt": "a fox",
        "response_format": "b64_json",
        "aspect_ratio": "3:2",
        "resolution": "2K",
        "n": 2,
    }
    assert result.images == [(PNG, "image/png")]
    assert result.cost_usd == pytest.approx(0.012)


def test_cost_parsing_header_then_body() -> None:
    def response(headers: dict[str, str]) -> httpx.Response:
        return httpx.Response(200, headers=headers)

    assert surplus_cost_usd(response({"x-si-buyer-cost-micro": "40000"}), {}) == 0.04
    assert surplus_cost_usd(response({}), {"usage": {"buyer_cost_micro": 1500}}) == 0.0015
    assert surplus_cost_usd(response({"x-si-buyer-cost-micro": "0"}), {}) == 0.0
    assert surplus_cost_usd(response({"x-si-buyer-cost-micro": "n/a"}), {}) is None
    assert surplus_cost_usd(response({}), {}) is None


async def test_edit_is_multipart_with_repeated_image_fields_and_no_n(monkeypatch) -> None:
    seen = _install(monkeypatch, lambda r: _ok("40000"))
    provider = SurplusImageGenerationProvider(api_key="inf_test")
    result = await provider.generate(
        ImageGenerationRequest(
            prompt="combine",
            model="seedream-v5-lite-edit",
            size="1024x1024",
            n=3,
            images=[(b"\x89PNG-one", "image/png"), (b"\xff\xd8-two", "image/jpeg")],
        )
    )
    request = seen[0]
    assert str(request.url) == "https://api.surplusintelligence.ai/v1/images/edits"
    fields = _form_fields(request)
    names = [name for name, _ in fields]
    assert names.count("image") == 2 and "image[]" not in names
    assert "n" not in names and "output_format" not in names
    values = dict(fields)
    assert values["model"] == b"seedream-v5-lite-edit"
    assert values["response_format"] == b"b64_json" and values["size"] == b"1024x1024"
    assert b"\x89PNG-one" in request.content and b"\xff\xd8-two" in request.content
    assert result.cost_usd == pytest.approx(0.04)


async def test_a_refused_multi_image_edit_retries_once_with_image_brackets(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        names = [name for name, _ in _form_fields(request)]
        if "image[]" in names:
            return _ok()
        return httpx.Response(400, json={"error": {"code": "request_rejected", "message": "x"}})

    seen = _install(monkeypatch, handler)
    provider = SurplusImageGenerationProvider(api_key="inf_test")
    images = [(b"a", "image/png"), (b"b", "image/png")]
    result = await provider.generate(
        ImageGenerationRequest(prompt="p", model="m-edit", size="auto", images=images)
    )
    assert len(seen) == 2 and result.images == [(PNG, "image/png")]
    assert [n for n, _ in _form_fields(seen[1])].count("image[]") == 2

    # A single image is not retried: the refusal is the answer.
    seen.clear()
    with pytest.raises(httpx.HTTPStatusError):
        await provider.generate(
            ImageGenerationRequest(prompt="p", model="m-edit", size="auto", images=images[:1])
        )
    assert len(seen) == 1


# ── flows adapter on Surplus ──────────────────────────────────────────────


def _surplus(monkeypatch) -> Callable[[str], Any]:
    monkeypatch.setenv("SURPLUS_API_KEY", "inf_test")
    provider = SurplusImageGenerationProvider()
    return lambda provider_id: provider if provider_id == "surplus" else None


async def test_adapter_sends_the_edit_id_only_when_images_are_connected(monkeypatch) -> None:
    seen = _install(monkeypatch, lambda r: _ok())
    adapter = ProviderImageAdapter(resolve=_surplus(monkeypatch))
    nb21 = BY_ID["surplus/nano-banana-2.1"]
    t2i = await adapter.generate(
        nb21, AdapterRequest(prompt="x", aspect_ratio="16:9", image_size="2K")
    )
    body = json.loads(seen[0].content)
    assert body["model"] == "venice-nano-banana-2-1" and body["resolution"] == "2K"
    assert "size" not in body  # Surplus ratio models get aspect_ratio, not a size
    assert t2i.cost_usd == pytest.approx(0.012)
    await adapter.generate(
        nb21,
        AdapterRequest(
            prompt="x", images=[(PNG, "image/png")], aspect_ratio="16:9", image_size="2K"
        ),
    )
    fields = dict(_form_fields(seen[1]))
    assert fields["model"] == b"nano-banana-2-1-edit"
    assert fields["aspect_ratio"] == b"16:9"
    assert "resolution" not in fields  # the edit model lists no resolutions


async def test_adapter_error_wording(monkeypatch) -> None:
    body = {
        "error": {
            "type": "billing_error",
            "code": "insufficient_credit",
            "message": "Not enough credit. Top up to continue. Need help? Join our Discord.",
        },
        "request_id": "req_1",
    }
    _install(monkeypatch, lambda r: httpx.Response(402, json=body))
    adapter = ProviderImageAdapter(resolve=_surplus(monkeypatch))
    entry = BY_ID["surplus/flux-2-pro"]
    with pytest.raises(NodeError) as excinfo:
        await adapter.generate(entry, AdapterRequest(prompt="x"))
    assert excinfo.value.code == "credits"
    assert excinfo.value.message == (
        "Surplus: insufficient credits: Not enough credit. Top up to continue."
    )
    body["error"]["code"] = "budget_exceeded"
    with pytest.raises(NodeError) as excinfo:
        await adapter.generate(entry, AdapterRequest(prompt="x"))
    assert excinfo.value.code == "budget"
    _install(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "bad"}}))
    with pytest.raises(NodeError) as excinfo:
        await adapter.generate(entry, AdapterRequest(prompt="x"))
    assert excinfo.value.code == "auth" and excinfo.value.message.startswith("Surplus:")


# ── availability and the default model ──────────────────────────────────


def _clear_keys(monkeypatch) -> None:
    for name in ("SURPLUS_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    reset_image_generation_providers()


async def test_surplus_key_makes_it_available_and_the_auto_default(tmp_path, monkeypatch) -> None:
    _clear_keys(monkeypatch)
    service = build_flow_service(GatewayConfig(state_dir=str(tmp_path / "state")))
    try:
        wire = service.models_wire()
        by_id = {m["id"]: m for m in wire["models"]}
        assert wire["default"] is None
        assert by_id["surplus/nano-banana-2"]["unavailable_reason"] == "SURPLUS_API_KEY is not set"
        assert list(by_id)[:7] == [e.id for e in SNAPSHOT_ENTRIES]  # Surplus first
        assert "openrouter/nano-banana-2" in by_id and "openai/gpt-image-1" in by_id

        monkeypatch.setenv("SURPLUS_API_KEY", "inf_test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        reset_image_generation_providers()
        wire = service.models_wire()
        assert (
            wire["default"] == "surplus/nano-banana-2"
        )  # the persisted default primary is ignored
        assert {m["id"]: m for m in wire["models"]}["surplus/gpt-image-2"]["price"][
            "edit_per_image"
        ] == {"default": 0.36}

        service.config.image_generation.primary = "openai/gpt-image-1"
        assert service.models_wire()["default"] == "surplus/nano-banana-2"
        service.config.image_generation.primary = "surplus/gpt-image-2"
        assert service.models_wire()["default"] == "surplus/gpt-image-2"
    finally:
        await service.close()
        reset_image_generation_providers()


def test_the_llm_surplus_key_is_reused(monkeypatch) -> None:
    _clear_keys(monkeypatch)
    config = GatewayConfig()
    config.llm = LlmProviderConfig(provider="surplus", api_key="inf_from_llm")
    try:
        reset_image_generation_providers(config.image_generation, llm_config=config.llm)
        assert provider_auth("surplus") == (True, None)
        reset_image_generation_providers(GatewayConfig().image_generation)
        assert provider_auth("surplus") == (False, "SURPLUS_API_KEY is not set")
    finally:
        reset_image_generation_providers()


# ── what each model is sent ───────────────────────────────────────────────


def test_caps_follow_the_catalog_with_live_overrides() -> None:
    gpt = BY_ID["surplus/gpt-image-2"]
    assert gpt.tiers == () and gpt.edit_tiers == ()  # cannot honour a resolution tier
    assert gpt.ratios and gpt.qualities == ("high", "medium", "low")
    flux = BY_ID["surplus/flux-2-pro"]
    # Upstream ignores both aspect_ratio and size: the output shape is fixed.
    assert flux.ratios == () and flux.sizes == ()
    nb2 = BY_ID["surplus/nano-banana-2"]
    assert nb2.ratios and "16:9" in nb2.ratios and nb2.sizes == ()  # honours aspect_ratio


def test_the_honours_rule_is_data_driven() -> None:
    from agentos.flows.surplus import Family

    plain = Family("surplus/x", "X", "x", None, "x", 0)
    assert plain.honours({"supported_parameters": ["size"]}, "size")
    assert plain.honours({"supported_features": ["aspect_ratio"]}, "aspect_ratio")
    assert not plain.honours({"supported_parameters": ["size"]}, "aspect_ratio")
    pinned = Family("surplus/x", "X", "x", None, "x", 0, overrides=(("aspect_ratio", False),))
    assert not pinned.honours({"supported_features": ["aspect_ratio"]}, "aspect_ratio")
    model = {
        "id": "x",
        "pricing": {"image": "0.01", "media_unit": "image"},
        "supported_parameters": ["prompt", "size"],
        "supported_features": ["image"],
        "sizes": ["512x512"],
        "aspect_ratios": ["1:1"],
    }
    from agentos.flows.surplus import entry_for

    entry = entry_for(plain, {"x": model}, as_of="t")
    assert entry is not None and entry.ratios == () and entry.sizes == ("512x512",)


def test_only_honoured_values_are_sent() -> None:
    from agentos.gateway.flows_wiring import request_params

    gpt = BY_ID["surplus/gpt-image-2"]
    sent = request_params(
        gpt,
        AdapterRequest(prompt="x", image_size="1K", aspect_ratio="3:2", quality="high"),
        edit=False,
    )
    assert sent == {
        "aspect_ratio": "3:2",
        "resolution": None,
        "quality": "high",
        "size": None,
        "background": None,
    }
    flux = BY_ID["surplus/flux-2-pro"]
    sent = request_params(
        flux, AdapterRequest(prompt="x", aspect_ratio="1:1", size="768x1024"), edit=False
    )
    assert sent["aspect_ratio"] is None and sent["size"] is None
    import dataclasses

    sized = dataclasses.replace(flux, sizes=("512x512", "768x1024"))
    sent = request_params(sized, AdapterRequest(prompt="x", size="768x1024"), edit=False)
    assert sent["size"] == "768x1024"
    assert request_params(sized, AdapterRequest(prompt="x", size="9x9"), edit=False)["size"] is None
    nb2 = BY_ID["surplus/nano-banana-2"]
    sent = request_params(nb2, AdapterRequest(prompt="x", aspect_ratio="16:9"), edit=False)
    assert sent["aspect_ratio"] == "16:9" and sent["quality"] is None


async def test_gpt_image_2_request_carries_no_resolution(monkeypatch) -> None:
    seen = _install(monkeypatch, lambda r: _ok())
    adapter = ProviderImageAdapter(resolve=_surplus(monkeypatch))
    await adapter.generate(
        BY_ID["surplus/gpt-image-2"],
        AdapterRequest(prompt="x", aspect_ratio="1:1", image_size="1K", quality="high"),
    )
    body = json.loads(seen[0].content)
    assert "resolution" not in body and body["quality"] == "high" and body["aspect_ratio"] == "1:1"
    await adapter.generate(
        BY_ID["surplus/flux-2-pro"],
        AdapterRequest(prompt="x", aspect_ratio="1:1", size="1024x1024"),
    )
    body = json.loads(seen[1].content)
    assert "aspect_ratio" not in body and "size" not in body


async def test_end_to_end_run_on_surplus_records_the_header_cost(tmp_path, monkeypatch) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv("SURPLUS_API_KEY", "inf_test")
    seen = _install(monkeypatch, lambda r: _ok("12000"))
    service = build_flow_service(GatewayConfig(state_dir=str(tmp_path / "state")))
    service.start()
    try:
        created = await service.create_flow("S")
        graph = t2i_doc(model="auto", count=2, aspect="3:2", resolution="1K")
        graph["id"] = created["id"]
        flow = await service.save_flow(graph, created["rev"])
        est = service.estimate(flow)
        assert est["total"] == {"kind": "range", "min": 0.0068, "max": 0.2, "approximate": True}
        queued = await service.submit_run(flow["id"])
        run = await service.wait_run(queued["run_id"], timeout=10)
        assert run["status"] == "done", run["error"]
        assert len(seen) == 2  # count 2 fans out: Surplus models take n=1 here
        assert {json.loads(r.content)["model"] for r in seen} == {"venice-nano-banana-2"}
        assert run["nodes"]["i"]["cost_usd"] == pytest.approx(0.024)
        assert run["cost_usd"] == pytest.approx(0.024)
        assert run["est_usd"] == pytest.approx(0.2)
        image = run["outputs"]["i"]["images"][0]
        assert (image["width"], image["height"]) == (48, 32)  # read from the bytes
    finally:
        await service.close()
