"""Validation codes, cache keys, the variation nonce, pruning and estimates."""

from __future__ import annotations

from typing import Any

from agentos.flows.models import REGISTRY, ModelEntry, ModelRegistry
from agentos.flows.planner import analyse, build_plan, estimate, outputs_to_json
from agentos.flows.types import ImageRef
from tests.test_flows.conftest import doc, fake_auth, link, node, t2i_doc

SHA = "a" * 64
REF = {"sha256": SHA, "mime": "image/png", "width": 8, "height": 8, "bytes": 10}


def _registry(*entries: ModelEntry, available: tuple[str, ...] = ("openrouter", "openai")):
    return ModelRegistry(entries or REGISTRY, auth=fake_auth(*available), primary=lambda: None)


def _errors(graph: dict[str, Any], *, models=None, media=lambda sha: True, to_node=None):
    analysis = analyse(graph, models=models or _registry(), media_exists=media, to_node=to_node)
    return {
        node_id: sorted(((e["code"], e.get("input")) for e in errs), key=str)
        for node_id, errs in analysis.node_errors().items()
    }


def test_valid_t2i_flow_has_no_errors() -> None:
    assert _errors(t2i_doc()) == {}


def test_structural_errors_are_all_reported_at_once() -> None:
    graph = doc(
        [
            node("t", "text", text="x"),
            node("d", "describe_image"),
            node("w", "prompt_writer", idea="x"),
            node("q", "mystery"),
            node("s", "save"),
        ],
        [
            link("l1", "t", "text", "d", "image"),  # TEXT into IMAGE
            link("l2", "t", "text", "w", "idea"),
            link("l3", "t", "text", "w", "idea"),  # second link into a single input
            link("l4", "t", "nope", "s", "images"),  # unknown output
            link("l5", "ghost", "text", "w", "context"),  # unknown node
            link("l6", "t", "text", "w", "style"),  # widget-only input has no socket
        ],
    )
    errors = _errors(graph)
    assert ("type_mismatch", "image") in errors["d"]
    assert ("multiple_links", "idea") in errors["w"]
    assert ("unknown_link", "style") in errors["w"]
    assert ("unknown_link", None) in errors["w"]  # the ghost node
    assert ("unknown_link", "images") in errors["s"]
    assert errors["q"] == [("unknown_type", None)]
    assert ("required", "image") in errors["d"]  # its only link was rejected


def test_cycle_is_an_error_on_every_node_in_it() -> None:
    graph = doc(
        [node("a", "join_text", a="x"), node("b", "join_text", a="y")],
        [link("l1", "a", "text", "b", "b"), link("l2", "b", "text", "a", "b")],
    )
    errors = _errors(graph)
    assert ("cycle", None) in errors["a"] and ("cycle", None) in errors["b"]


def test_required_inputs_and_widget_ranges() -> None:
    graph = doc(
        [
            node("t", "text", text="   "),
            node("i", "image", model="openrouter/nano-banana-2", count=9, aspect="7:3"),
            node("j", "join_text", template=3),
            node("p", "prompt_writer", idea="x", style="watercolor"),
            node("s", "save"),
            node("v", "image", prompt="x", model="openrouter/nano-banana-2", variation="maybe"),
            node("c", "image", prompt="x", model="openrouter/nano-banana-2", count="two"),
        ],
        [],
    )
    errors = _errors(graph)
    assert errors["t"] == [("required", "text")]
    assert ("required", "prompt") in errors["i"]
    assert ("out_of_range", "count") in errors["i"]
    assert ("invalid_option", "aspect") in errors["i"]
    assert ("invalid_value", "template") in errors["j"]
    assert ("required", "a") in errors["j"]
    assert errors["p"] == [("invalid_option", "style")]
    assert errors["s"] == [("required", "images")]
    assert errors["v"] == [("invalid_option", "variation")]
    assert errors["c"] == [("invalid_value", "count")]


def test_hidden_widget_values_are_ignored() -> None:
    # aspect/resolution belong to the Gemini family; on GPT Image they are hidden.
    graph = t2i_doc(model="openai/gpt-image-1", aspect="nonsense", resolution="9K")
    assert _errors(graph) == {}


def test_model_errors() -> None:
    unavailable = _errors(t2i_doc(), models=_registry(available=("openai",)))
    assert unavailable["i"] == [("model_unavailable", "model")]
    assert _errors(t2i_doc(model="acme/x"))["i"] == [("unknown_model", "model")]
    no_default = _errors(t2i_doc(model="auto"), models=_registry(available=()))
    assert no_default["i"] == [("no_default_model", "model")]
    assert _errors(t2i_doc(model="auto"), models=_registry(available=("openai",))) == {}


def test_edit_unsupported_and_too_many_images() -> None:
    limited = ModelEntry(
        id="acme/one",
        display="One",
        provider="openrouter",
        api_model="acme/one",
        family="x",
        edit=False,
        max_images=1,
        per_image={"default": 0.01},
    )
    graph = doc(
        [
            node("a", "load_image", image=REF),
            node("b", "load_image", image=REF),
            node("i", "image", prompt="x", model="acme/one"),
            node("s", "save"),
        ],
        [
            link("l1", "a", "image", "i", "images"),
            link("l2", "b", "image", "i", "images"),
            link("l3", "i", "image", "s", "images"),
        ],
    )
    errors = _errors(graph, models=_registry(limited))
    assert errors["i"] == [("edit_unsupported", "images"), ("too_many_images", "images")]


def test_missing_upload_unknown_target_and_empty_flow() -> None:
    graph = doc([node("a", "load_image", image=REF)], [])
    assert _errors(graph, media=lambda sha: False)["a"] == [("missing_image", "image")]
    assert _errors(doc([node("a", "load_image")], []))["a"] == [("required", "image")]
    assert _errors(t2i_doc(), to_node="zz")["_flow"] == [("unknown_node", None)]
    assert _errors(doc([], []))["_flow"] == [("empty", None)]


def test_to_node_limits_validation_to_its_ancestors() -> None:
    graph = t2i_doc()
    graph["nodes"].append(node("x", "describe_image"))  # broken, but not upstream of "i"
    assert "x" in _errors(graph)
    assert _errors(graph, to_node="i") == {}


# ── planning ──────────────────────────────────────────────────────────────


def _plan(graph, *, run_id="run_000000000001", cache=None, to_node=None, models=None):
    analysis = analyse(
        graph, models=models or _registry(), media_exists=lambda sha: True, to_node=to_node
    )
    assert analysis.ok, analysis.node_errors()
    store = cache if cache is not None else {}
    plan = build_plan(
        analysis, run_id=run_id, cache_lookup=store.get, media_exists=lambda sha: True
    )
    return analysis, plan


def test_cache_keys_are_stable_and_ignore_layout() -> None:
    _, first = _plan(t2i_doc())
    moved = t2i_doc()
    for item in moved["nodes"]:
        item["pos"] = [123, 456]
        item["title"] = "renamed"
    moved["viewport"] = {"x": 9, "y": 9, "zoom": 2}
    _, second = _plan(moved, run_id="run_000000000002")
    assert {k: v.key for k, v in first.nodes.items() if k != "s"} == {
        k: v.key for k, v in second.nodes.items() if k != "s"
    }
    _, changed = _plan(t2i_doc(count=2))
    assert changed.nodes["i"].key != first.nodes["i"].key
    assert changed.nodes["t"].key == first.nodes["t"].key


def test_explicit_defaults_and_hidden_values_do_not_change_the_key() -> None:
    _, bare = _plan(t2i_doc(model="openai/gpt-image-1"))
    _, explicit = _plan(
        t2i_doc(model="openai/gpt-image-1", size="auto", quality="auto", aspect="16:9")
    )
    assert bare.nodes["i"].key == explicit.nodes["i"].key


def test_auto_model_keys_like_the_model_it_resolves_to() -> None:
    _, auto = _plan(t2i_doc(model="auto"))
    _, pinned = _plan(t2i_doc(model="openrouter/nano-banana-2"))
    assert auto.nodes["i"].key == pinned.nodes["i"].key


def test_variation_new_adds_a_per_run_nonce_and_save_always_runs() -> None:
    _, a = _plan(t2i_doc(variation="new"), run_id="run_00000000000a")
    _, b = _plan(t2i_doc(variation="new"), run_id="run_00000000000b")
    assert a.nodes["i"].key != b.nodes["i"].key
    assert a.nodes["i"].stable_key == b.nodes["i"].stable_key
    _, c = _plan(t2i_doc(variation="keep"), run_id="run_00000000000a")
    _, d = _plan(t2i_doc(variation="keep"), run_id="run_00000000000b")
    assert c.nodes["i"].key == d.nodes["i"].key
    assert c.nodes["s"].key != d.nodes["s"].key  # save never hits the cache


def test_a_cached_node_prunes_its_ancestors() -> None:
    graph = doc(
        [
            node("t", "text", text="idea"),
            node("w", "prompt_writer"),
            node("i", "image", model="openrouter/nano-banana-2", variation="keep"),
            node("s", "save"),
        ],
        [
            link("l1", "t", "text", "w", "idea"),
            link("l2", "w", "text", "i", "prompt"),
            link("l3", "i", "image", "s", "images"),
        ],
    )
    _, cold = _plan(graph)
    assert cold.will_run == ["t", "w", "i", "s"] and cold.cached == []
    ref = ImageRef(SHA, "image/png", 8, 8, 10)
    cache = {
        cold.nodes["i"].key: {
            "outputs": outputs_to_json({"image": [ref]}),
            "artifact_ids": [SHA],
        }
    }
    analysis, warm = _plan(graph, cache=cache)
    assert warm.cached == ["i"] and warm.will_run == ["s"]
    assert "w" not in warm.topo and "t" not in warm.topo
    est = estimate(analysis, warm)
    assert est["total"] == {"kind": "free", "approximate": False}
    assert est["nodes"]["i"]["kind"] == "usd"  # the badge still shows the price


def test_a_cached_prompt_writer_feeds_a_content_key_downstream() -> None:
    graph = doc(
        [
            node("t", "text", text="idea"),
            node("w", "prompt_writer"),
            node("i", "image", model="openrouter/nano-banana-2", variation="keep"),
        ],
        [link("l1", "t", "text", "w", "idea"), link("l2", "w", "text", "i", "prompt")],
    )
    _, cold = _plan(graph)
    cache = {cold.nodes["w"].key: {"outputs": {"text": "a prompt"}, "artifact_ids": []}}
    _, warm = _plan(graph, cache=cache)
    assert warm.cached == ["w"] and warm.will_run == ["i"]
    # The image key now refers to the cached prompt's content, not its key.
    assert warm.nodes["i"].key != cold.nodes["i"].key


def test_estimate_totals_skip_cached_and_propagate_unknown() -> None:
    unpriced = ModelEntry(
        id="acme/free", display="?", provider="openrouter", api_model="acme", family="x"
    )
    graph = t2i_doc(count=2, resolution="4K")
    analysis, plan = _plan(graph)
    est = estimate(analysis, plan)
    assert est["will_run"] == ["t", "i", "s"] and est["cached"] == []
    assert est["nodes"]["t"] == {"kind": "free", "approximate": False}
    assert est["total"] == {"kind": "usd", "usd": 0.3024, "approximate": True}
    analysis, plan = _plan(t2i_doc(model="acme/free"), models=_registry(*REGISTRY, unpriced))
    assert estimate(analysis, plan)["total"] == {"kind": "unknown", "approximate": True}


def test_edit_reference_count_reaches_the_price() -> None:
    graph = doc(
        [
            node("a", "load_image", image=REF),
            node("b", "load_image", image=REF),
            node("t", "text", text="merge"),
            node("i", "image", model="openai/gpt-image-1", quality="low", size="1024x1024"),
        ],
        [
            link("l1", "a", "image", "i", "images"),
            link("l2", "b", "image", "i", "images"),
            link("l3", "t", "text", "i", "prompt"),
        ],
    )
    analysis, plan = _plan(graph)
    # 0.011 per image + 0.01 per reference image
    assert estimate(analysis, plan)["nodes"]["i"] == {
        "kind": "usd",
        "usd": 0.031,
        "approximate": True,
    }
