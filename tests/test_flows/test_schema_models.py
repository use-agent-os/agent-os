"""``flows.nodes`` / ``flows.models`` wire shapes and the price lookup."""

from __future__ import annotations

import json

import pytest

from agentos.flows.models import REGISTRY, ModelRegistry
from agentos.flows.schema import InputSpec, node_catalog_wire
from agentos.flows.types import sum_prices
from tests.test_flows.conftest import fake_auth

EXPECTED_INPUTS = {
    "text": ["text"],
    "load_image": ["image"],
    "join_text": ["a", "b", "template"],
    "prompt_writer": ["idea", "context", "style"],
    "describe_image": ["image", "focus"],
    "image": [
        "prompt",
        "images",
        "model",
        "aspect",
        "resolution",
        "size",
        "quality",
        "background",
        "count",
        "variation",
    ],
    "save": ["images", "name"],
}


def test_catalog_has_the_seven_nodes_with_exact_input_names() -> None:
    wire = node_catalog_wire()
    assert wire["version"] == 1
    assert wire["types"] == {"TEXT": {"color": "#c4c4cc"}, "IMAGE": {"color": "#60a5fa"}}
    by_id = {n["id"]: n for n in wire["nodes"]}
    assert list(by_id) == list(EXPECTED_INPUTS)
    for node_id, names in EXPECTED_INPUTS.items():
        assert [i["name"] for i in by_id[node_id]["inputs"]] == names
    json.dumps(wire)  # serialisable as-is


def test_catalog_node_details_match_the_contract() -> None:
    by_id = {n["id"]: n for n in node_catalog_wire()["nodes"]}
    image = by_id["image"]
    assert image["group"] == "ai" and image["is_api"] is True and image["is_output"] is False
    assert image["outputs"] == [{"name": "image", "type": "IMAGE"}]
    assert image["price"] == {
        "depends_on": ["model", "resolution", "size", "quality", "count", "images.count"]
    }
    inputs = {i["name"]: i for i in image["inputs"]}
    assert inputs["prompt"]["socket"] is True and inputs["prompt"]["widget"] == "textarea"
    assert inputs["images"]["multi"] is True and inputs["images"]["optional"] is True
    assert inputs["model"] == {
        "name": "model",
        "widget": "model",
        "socket": False,
        "optional": False,
        "multi": False,
        "default": "auto",
        "advanced": False,
    }
    assert inputs["aspect"]["options_from"] == "model.ratios"
    assert inputs["aspect"]["show_when"] == {"model.has": "ratios"}
    assert inputs["background"]["show_when"] == {"model.has": "transparent_bg"}
    assert inputs["count"]["min"] == 1 and inputs["count"]["max"] == 4
    assert inputs["variation"]["default"] == "new"
    save = by_id["save"]
    assert save["is_output"] is True and save["outputs"] == [] and save["price"] is None
    assert by_id["text"]["price"] is None and by_id["join_text"]["price"] is None
    assert by_id["prompt_writer"]["is_api"] is True
    assert by_id["prompt_writer"]["inputs"][1] == {
        "name": "context",
        "type": "TEXT",
        "socket": True,
        "optional": True,
        "multi": False,
        "tooltip": "Optional reference text",
        "advanced": False,
    }
    assert by_id["join_text"]["inputs"][2]["default"] == "{a}, {b}"
    assert by_id["text"]["inputs"][0]["socket"] is False


def test_input_spec_socket_defaults_and_guards() -> None:
    assert InputSpec(name="x", type="TEXT").has_socket is True
    assert InputSpec(name="x", type="TEXT", socket=False).has_socket is False
    assert InputSpec(name="x", widget="number").has_socket is False
    with pytest.raises(ValueError):
        InputSpec(name="x", widget="slider")
    with pytest.raises(ValueError):
        InputSpec(name="x", type="MASK")


def test_models_wire_availability_and_default() -> None:
    registry = ModelRegistry(auth=fake_auth("openai"), primary=lambda: None)
    wire = registry.to_wire()
    by_id = {m["id"]: m for m in wire["models"]}
    assert list(by_id) == [
        "openrouter/nano-banana-2",
        "openrouter/nano-banana-2.1",
        "openrouter/nano-banana-pro",
        "openrouter/gpt-5-image-mini",
        "openai/gpt-image-1",
    ]
    assert wire["default"] == "openai/gpt-image-1"
    nb2 = by_id["openrouter/nano-banana-2"]
    assert nb2["available"] is False
    assert nb2["unavailable_reason"] == "OPENROUTER_API_KEY is not set"
    assert nb2["ops"] == {"t2i": True, "edit": True, "mask": False}
    assert nb2["max_images"] == 14 and nb2["output"]["tiers"] == ["1K", "2K", "4K"]
    assert nb2["output"]["n_max"] == 1 and nb2["price"]["approximate"] is True
    gpt = by_id["openai/gpt-image-1"]
    assert gpt["available"] is True and gpt["unavailable_reason"] is None
    assert gpt["output"]["sizes"] == ["auto", "1024x1024", "1536x1024", "1024x1536"]
    assert gpt["output"]["qualities"] == ["auto", "low", "medium", "high"]
    assert gpt["output"]["transparent_bg"] is True and gpt["output"]["n_max"] == 8
    assert gpt["max_images"] == 16
    assert all(m["price"]["source"] for m in wire["models"])


def test_registry_api_models_match_the_openrouter_catalog() -> None:
    api = {entry.id: (entry.provider, entry.api_model) for entry in REGISTRY}
    assert api == {
        "openrouter/nano-banana-2": ("openrouter", "google/gemini-3.1-flash-image"),
        "openrouter/nano-banana-2.1": ("openrouter", "google/gemini-nano-banana-2.1"),
        "openrouter/nano-banana-pro": ("openrouter", "google/gemini-3-pro-image"),
        "openrouter/gpt-5-image-mini": ("openrouter", "openai/gpt-5-image-mini"),
        "openai/gpt-image-1": ("openai", "gpt-image-1"),
    }
    for entry in REGISTRY:
        assert entry.price_approximate and entry.price_source
        if entry.provider == "openrouter":
            assert entry.price_source.startswith("OpenRouter catalog 2026-10-29")


def test_default_prefers_an_available_primary_then_registry_order() -> None:
    both = fake_auth("openai", "openrouter")
    assert ModelRegistry(auth=both, primary=lambda: None).default_id() == (
        "openrouter/nano-banana-2"
    )
    # The primary may name the provider/api-model pair the image tool uses.
    assert (
        ModelRegistry(auth=both, primary=lambda: "openai/gpt-image-1").default_id()
        == "openai/gpt-image-1"
    )
    assert (
        ModelRegistry(
            auth=both, primary=lambda: "openrouter/google/gemini-3.1-flash-image-preview"
        ).default_id()
        == "openrouter/nano-banana-2"
    )
    # A primary without a key falls through to the first available entry.
    only_or = fake_auth("openrouter")
    assert (
        ModelRegistry(auth=only_or, primary=lambda: "openai/gpt-image-1").default_id()
        == "openrouter/nano-banana-2"
    )
    assert ModelRegistry(auth=fake_auth(), primary=lambda: None).default_id() is None


def test_availability_reads_the_image_provider_registry(monkeypatch) -> None:
    from agentos.gateway.config import ImageGenerationConfig
    from agentos.gateway.flows_wiring import provider_auth
    from agentos.provider.image_generation import reset_image_generation_providers

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = ImageGenerationConfig()  # enabled = False: flows must not care
    config.providers.openai.api_key = "sk-test-openai"
    reset_image_generation_providers(config)
    try:
        assert provider_auth("openai") == (True, None)
        assert provider_auth("openrouter") == (False, "OPENROUTER_API_KEY is not set")
    finally:
        reset_image_generation_providers()


def test_price_lookup_tier_quality_size_and_auto_range() -> None:
    by_id = {entry.id: entry for entry in REGISTRY}
    nb2, gpt = by_id["openrouter/nano-banana-2"], by_id["openai/gpt-image-1"]
    assert nb2.per_image == {"1K": 0.0672, "2K": 0.1008, "4K": 0.1512}
    assert by_id["openrouter/nano-banana-pro"].per_image == {
        "1K": 0.1344,
        "2K": 0.1344,
        "4K": 0.24,
    }
    assert by_id["openrouter/nano-banana-2.1"].per_image["1K"] == 0.0336
    assert by_id["openrouter/gpt-5-image-mini"].per_image == {"default": 0.0333}
    assert nb2.estimate({"resolution": "2K"}, count=3, refs=2) == {
        "kind": "usd",
        "usd": 0.3024,
        "approximate": True,
    }
    assert gpt.estimate({"quality": "high", "size": "1024x1024"}, count=2, refs=1) == {
        "kind": "usd",
        "usd": 0.344,
        "approximate": True,
    }
    auto = gpt.estimate({"quality": "auto", "size": "auto"}, count=1, refs=0)
    assert auto == {"kind": "range", "min": 0.011, "max": 0.25, "approximate": True}


def test_sum_prices_rules() -> None:
    free = {"kind": "free", "approximate": False}
    usd = {"kind": "usd", "usd": 0.1, "approximate": True}
    rng = {"kind": "range", "min": 0.01, "max": 0.2, "approximate": True}
    unknown = {"kind": "unknown", "approximate": True}
    assert sum_prices([]) == free
    assert sum_prices([free, free]) == free
    assert sum_prices([usd, free, usd]) == {"kind": "usd", "usd": 0.2, "approximate": True}
    assert sum_prices([usd, rng]) == {
        "kind": "range",
        "min": 0.11,
        "max": 0.3,
        "approximate": True,
    }
    assert sum_prices([unknown, free]) == unknown
    assert sum_prices([unknown, usd]) == {"kind": "unknown", "approximate": True, "min": 0.1}
