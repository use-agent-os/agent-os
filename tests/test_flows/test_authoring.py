"""Agent authoring: spec ⇄ doc, layout, revisions, restore, provenance, errors."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from agentos.flows import service as service_module
from agentos.flows.authoring import (
    COLUMN_WIDTH,
    NODE_GAP,
    NODE_WIDTH,
    diff_summary,
    estimate_height,
    layout,
)
from agentos.flows.models import ModelRegistry
from agentos.flows.service import FlowService
from agentos.flows.store import MAX_REVISIONS
from agentos.flows.types import FlowError
from tests.test_flows.conftest import fake_auth, png_bytes

SPEC: dict[str, Any] = {
    "name": "Infographic",
    "nodes": [
        {"id": "content", "type": "text", "values": {"text": "AgentOS in three steps"}},
        {
            "id": "writer",
            "type": "prompt_writer",
            "title": "Prompt",
            "values": {"style": "minimal"},
        },
        {
            "id": "gen",
            "type": "image",
            "values": {"model": "openrouter/nano-banana-2", "aspect": "16:9", "variation": "keep"},
        },
        {"id": "out", "type": "save"},
    ],
    "links": [
        ["content.text", "writer.idea"],
        ["writer.text", "gen.prompt"],
        ["gen.image", "out.images"],
    ],
    "inputs": {"content": "content.text"},
}


def _errors(excinfo: Any) -> dict[str, list[tuple[str, str]]]:
    assert excinfo.value.code == "flows.invalid"
    return {
        key: [(e["code"], e["message"]) for e in items]
        for key, items in excinfo.value.details["node_errors"].items()
    }


# ── spec ⇄ doc ────────────────────────────────────────────────────────────


async def test_create_from_spec_round_trips_and_records_provenance(service, recorder) -> None:
    result = await service.create_from_spec(SPEC, session_key="agent:main:web:s1")
    assert result["change"] == "created" and result["rev"] == 1 and result["node_count"] == 4
    assert result["summary"] == "created with 4 nodes and 3 links"
    assert result["estimate"]["kind"] == "usd" and result["warnings"] == {}
    flow = service.get_flow(result["flow_id"])
    assert flow["created_by"] == "agent" and flow["updated_by"] == "agent"
    assert all(isinstance(node["pos"], list) for node in flow["nodes"])
    spec = service.get_spec(result["flow_id"])
    assert spec["spec"] == {
        "name": "Infographic",
        "nodes": [
            {"id": "content", "type": "text", "values": {"text": "AgentOS in three steps"}},
            {
                "id": "writer",
                "type": "prompt_writer",
                "title": "Prompt",
                "values": {"style": "minimal"},
            },
            {
                "id": "gen",
                "type": "image",
                "values": {
                    "model": "openrouter/nano-banana-2",
                    "aspect": "16:9",
                    "variation": "keep",
                },
            },
            {"id": "out", "type": "save", "values": {}},
        ],
        "links": SPEC["links"],
        "inputs": {"content": "content.text"},
    }
    assert spec["public_inputs"] == [
        {
            "name": "content",
            "target": "content.text",
            "value": "AgentOS in three steps",
            "widget": "textarea",
        }
    ]
    assert (spec["created_by"], spec["updated_by"], spec["rev"]) == ("agent", "agent", 1)
    # Feeding the spec back creates an identical graph.
    again = await service.create_from_spec(spec["spec"])
    assert service.get_spec(again["flow_id"])["spec"] == spec["spec"]
    revisions = service.revisions(result["flow_id"])["revisions"]
    assert revisions == [
        {
            "rev": 1,
            "author": "agent",
            "session_key": "agent:main:web:s1",
            "kind": "create",
            "created": revisions[0]["created"],
            "summary": "created with 4 nodes and 3 links",
        }
    ]
    assert ("flows.changed", {"flow_id": result["flow_id"], "rev": 1, "author": "agent"}) in (
        recorder.events
    )
    listed = {f["id"]: f for f in service.list_flows()["flows"]}
    assert listed[result["flow_id"]]["created_by"] == "agent"


async def test_update_keeps_positions_places_new_nodes_and_summarises(service, recorder) -> None:
    user_flow = await service.create_flow("Mine", "text_to_image")
    moved = dict(user_flow)
    moved["nodes"] = [dict(n) for n in user_flow["nodes"]]
    moved["nodes"][0]["pos"] = [500, 900]
    moved = await service.save_flow(moved, 1)
    spec = service.get_spec(moved["id"])["spec"]
    spec["nodes"][1]["values"]["count"] = 2
    spec["nodes"].append({"id": "extra", "type": "image", "values": {"prompt": "a second take"}})
    spec["links"].append(["extra.image", "n3.images"])
    result = await service.update_from_spec(moved["id"], spec, session_key="agent:main:web:s2")
    assert result["change"] == "updated" and result["rev"] == 3
    assert result["summary"] == "added extra (image); changed n2.count 1 → 2; links +1 -0"
    flow = service.get_flow(moved["id"])
    by_id = {n["id"]: n for n in flow["nodes"]}
    assert by_id["n1"]["pos"] == [500, 900]  # kept by id
    assert by_id["n2"]["pos"] == user_flow["nodes"][1]["pos"]
    positions = [tuple(n["pos"]) for n in flow["nodes"]]
    assert len(set(positions)) == len(positions)  # the new node does not overlap
    assert flow["created_by"] == "user" and flow["updated_by"] == "agent"
    assert ("flows.changed", {"flow_id": moved["id"], "rev": 3, "author": "agent"}) in (
        recorder.events
    )
    top = service.revisions(moved["id"])["revisions"][0]
    assert (top["rev"], top["author"], top["kind"], top["session_key"]) == (
        3,
        "agent",
        "update",
        "agent:main:web:s2",
    )
    # By exact name works too; a rename shows in the summary.
    spec["name"] = "Renamed"
    renamed = await service.update_from_spec("Mine", spec)
    assert renamed["summary"] == 'renamed to "Renamed"' and renamed["name"] == "Renamed"


async def test_spec_errors_name_the_node_or_link_and_save_nothing(service) -> None:
    bad = {
        "name": "Broken",
        "nodes": [
            {"id": "a", "type": "text", "values": {"txt": "x"}},
            {"id": "a", "type": "text"},
            {"id": "b", "type": "mystery"},
            {"id": "c.d", "type": "text"},
            {"id": "e", "type": "image", "position": [1, 2]},
        ],
        "links": [["a.text"], ["a.text", "zz.prompt"], "a.text->e.prompt"],
        "inputs": {"idea": "q.text"},
        "extra": 1,
    }
    with pytest.raises(FlowError) as excinfo:
        await service.create_from_spec(bad)
    errors = _errors(excinfo)
    assert errors["a"][0] == ("unknown_input", "text has no input 'txt' (inputs: text)")
    assert errors["a"][1][0] == "duplicate_id"
    assert errors["b"][0][0] == "unknown_type"
    assert errors["e"][0] == (
        "unknown_key",
        "Unknown node key 'position' (use id, type, title, values)",
    )
    flow_errors = dict(errors["_flow"])
    assert "unknown_key" in flow_errors and "invalid_id" in flow_errors
    links = [m for code, m in errors["_flow"] if code in ("invalid_link", "unknown_link")]
    assert any("links[0]" in m for m in links) and any("links[2]" in m for m in links)
    assert any("links[1]" in m and "'zz'" in m for m in links)
    assert any("inputs['idea']" in m for code, m in errors["_flow"] if code == "invalid_input")
    assert service.list_flows() == {"flows": []}


async def test_graph_errors_come_from_the_validator(service) -> None:
    spec = {
        "name": "Mismatch",
        "nodes": [
            {"id": "t", "type": "text", "values": {"text": "x"}},
            {"id": "d", "type": "describe_image"},
        ],
        "links": [["t.text", "d.image"]],
    }
    with pytest.raises(FlowError) as excinfo:
        await service.create_from_spec(spec)
    errors = _errors(excinfo)
    assert ("type_mismatch", "image takes IMAGE, not TEXT") in errors["d"]
    assert service.list_flows() == {"flows": []}
    created = await service.create_from_spec(SPEC)
    spec["name"] = None
    with pytest.raises(FlowError):
        await service.update_from_spec(created["flow_id"], spec)
    assert service.get_flow(created["flow_id"])["rev"] == 1  # unchanged


async def test_unavailable_model_is_a_warning_not_an_error(tmp_path) -> None:
    service = FlowService(None, root=tmp_path / "flows", models=ModelRegistry(auth=fake_auth()))
    try:
        result = await service.create_from_spec(SPEC)
        assert result["rev"] == 1
        assert result["warnings"]["gen"][0]["code"] == "model_unavailable"
    finally:
        await service.close()


async def test_load_image_references_go_through_the_resolver(service) -> None:
    spec = {
        "name": "Edit",
        "nodes": [
            {"id": "ref", "type": "load_image", "values": {"image": {"attachment": "att_1"}}},
            {"id": "t", "type": "text", "values": {"text": "make it blue"}},
            {"id": "gen", "type": "image", "values": {"model": "openrouter/nano-banana-2"}},
        ],
        "links": [["ref.image", "gen.images"], ["t.text", "gen.prompt"]],
    }
    with pytest.raises(FlowError) as excinfo:
        await service.create_from_spec(spec)
    assert _errors(excinfo)["ref"][0][0] == "unresolved_image"
    seen: list[tuple[str, dict]] = []

    async def resolver(node_id: str, value: dict) -> dict:
        seen.append((node_id, value))
        return service.ingest_image(png_bytes("blue"), "image/png")

    result = await service.create_from_spec(spec, image_resolver=resolver)
    assert seen == [("ref", {"attachment": "att_1"})]
    stored = service.get_spec(result["flow_id"])["spec"]["nodes"][0]["values"]["image"]
    assert service.media.exists(stored["sha256"]) and stored["width"] == 32

    def refuse(node_id: str, value: dict) -> dict:
        raise ValueError("that path is outside the workspace")

    spec["nodes"][0]["values"]["image"] = {"path": "/etc/passwd"}
    with pytest.raises(FlowError) as excinfo:
        await service.create_from_spec(spec, image_resolver=refuse)
    assert _errors(excinfo)["ref"] == [("invalid_image", "that path is outside the workspace")]
    # An ImageRef already in the media store needs no resolver.
    spec["nodes"][0]["values"]["image"] = stored
    assert (await service.create_from_spec(spec))["rev"] == 1


# ── layout ────────────────────────────────────────────────────────────────


def _overlaps(doc: dict) -> list[tuple[str, str]]:
    boxes = [
        (n["id"], n["pos"][0], n["pos"][1], n["pos"][1] + estimate_height(n)) for n in doc["nodes"]
    ]
    return [
        (a, b)
        for i, (a, ax, at, ab) in enumerate(boxes)
        for (b, bx, bt, bb) in boxes[i + 1 :]
        if abs(ax - bx) < NODE_WIDTH and at < bb + NODE_GAP and bt < ab + NODE_GAP
    ]


def test_layout_columns_heights_and_determinism() -> None:
    doc = {
        "nodes": [
            {"id": "a", "type": "text"},
            {"id": "b", "type": "load_image"},
            {"id": "c", "type": "image"},
            {"id": "d", "type": "save"},
        ],
        "links": [
            {"id": "1", "from": ["a", "text"], "to": ["c", "prompt"]},
            {"id": "2", "from": ["b", "image"], "to": ["c", "images"]},
            {"id": "3", "from": ["c", "image"], "to": ["d", "images"]},
        ],
    }
    first = layout(doc)
    assert layout(doc) == first and "pos" not in doc["nodes"][0]  # pure, deterministic
    pos = {n["id"]: n["pos"] for n in first["nodes"]}
    height_a = estimate_height(doc["nodes"][0])
    assert pos == {
        "a": [40, 40],
        "b": [40, 40 + height_a + NODE_GAP],  # stacked by height, not a fixed pitch
        "c": [40 + COLUMN_WIDTH, 40],
        "d": [40 + 2 * COLUMN_WIDTH, 40],
    }
    assert all(n["auto_pos"] is True for n in first["nodes"])
    assert _overlaps(first) == []
    # Existing positions stay (and are not marked); new nodes go below, no overlap.
    placed = json.loads(json.dumps(first))
    for node in placed["nodes"]:
        node.pop("auto_pos")
    placed["nodes"].append({"id": "e", "type": "image"})
    placed["links"].append({"id": "4", "from": ["a", "text"], "to": ["e", "prompt"]})
    again = layout(placed)
    by_id = {n["id"]: n for n in again["nodes"]}
    assert {k: by_id[k]["pos"] for k in pos} == pos
    assert all("auto_pos" not in by_id[k] for k in pos) and by_id["e"]["auto_pos"] is True
    assert by_id["e"]["pos"][0] == 40 + COLUMN_WIDTH and _overlaps(again) == []


def test_layout_height_grows_with_text_and_previews() -> None:
    short = {"id": "t", "type": "text", "values": {"text": "hi"}}
    long = {"id": "t", "type": "text", "values": {"text": "x" * 2000}}
    assert estimate_height(long) > estimate_height(short)
    assert estimate_height({"id": "x", "type": "text", "values": {"text": "y" * 400}}) == (
        estimate_height(long)
    )  # capped
    assert estimate_height({"id": "s", "type": "save"}) >= 200
    # Two tall prompts feeding one image never overlap.
    doc = {
        "nodes": [
            {"id": "p1", "type": "text", "values": {"text": "word " * 80}},
            {"id": "p2", "type": "text", "values": {"text": "word " * 80}},
            {"id": "j", "type": "join_text"},
        ],
        "links": [
            {"id": "1", "from": ["p1", "text"], "to": ["j", "a"]},
            {"id": "2", "from": ["p2", "text"], "to": ["j", "b"]},
        ],
    }
    assert _overlaps(layout(doc)) == []


def test_diff_summary() -> None:
    old = {
        "name": "A",
        "nodes": [{"id": "x", "type": "text", "values": {"text": "1"}, "pos": [0, 0]}],
        "links": [],
    }
    assert diff_summary(None, old) == "created with 1 node and 0 links"
    moved = json.loads(json.dumps(old))
    moved["nodes"][0]["pos"] = [5, 5]
    assert diff_summary(old, moved) == "layout only"
    assert diff_summary(old, old) == "no changes"
    new = json.loads(json.dumps(old))
    new["nodes"][0]["values"]["text"] = "2"
    new["nodes"].append({"id": "y", "type": "save"})
    new["links"].append({"id": "1", "from": ["x", "text"], "to": ["y", "images"]})
    assert diff_summary(old, new) == "added y (save); changed x.text 1 → 2; links +1 -0"


# ── revisions and restore ─────────────────────────────────────────────────


async def test_every_write_keeps_a_revision_and_restore_adds_one(service, recorder) -> None:
    created = await service.create_from_spec(SPEC)
    flow_id = created["flow_id"]
    spec = service.get_spec(flow_id)["spec"]
    spec["nodes"][0]["values"]["text"] = "changed by the agent"
    await service.update_from_spec(flow_id, spec)
    current = service.get_flow(flow_id)
    current["nodes"][2]["values"]["aspect"] = "1:1"
    current = await service.save_flow(current, 2)  # the user, in the editor
    rows = service.revisions(flow_id)["revisions"]
    assert [(r["rev"], r["author"], r["kind"]) for r in rows] == [
        (3, "user", "save"),
        (2, "agent", "update"),
        (1, "agent", "create"),
    ]
    restored = await service.restore(flow_id, 1, 3)
    assert restored["rev"] == 4 and restored["updated_by"] == "user"
    assert restored["created_by"] == "agent"
    assert (
        service.get_spec(flow_id)["spec"]["nodes"]
        == service.get_spec({**restored, "nodes": restored["nodes"]})["spec"]["nodes"]
    )
    assert service.get_spec(flow_id)["spec"]["nodes"][0]["values"]["text"] == (
        "AgentOS in three steps"
    )
    top = service.revisions(flow_id)["revisions"][0]
    assert (top["rev"], top["kind"], top["author"]) == (4, "restore", "user")
    assert top["summary"] == (
        "restored rev 1: changed content.text changed by the agent → AgentOS in three steps, "
        "gen.aspect 1:1 → 16:9"
    )
    assert ("flows.changed", {"flow_id": flow_id, "rev": 4, "author": "user"}) in (recorder.events)
    with pytest.raises(FlowError) as excinfo:
        await service.restore(flow_id, 2, 3)  # stale base_rev
    assert excinfo.value.code == "flows.conflict"
    with pytest.raises(FlowError) as excinfo:
        await service.restore(flow_id, 99, 4)
    assert excinfo.value.code == "flows.not_found"


async def test_editor_autosaves_coalesce_into_one_revision(service, monkeypatch) -> None:
    flow = await service.create_flow("Typing", "text_to_image")
    clock = [1_000_000.0]
    monkeypatch.setattr(service_module.time, "time", lambda: clock[0])
    import agentos.flows.store as store_module

    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    for text in ("a", "ab", "abc"):
        flow["nodes"][0]["values"]["text"] = text
        flow = await service.save_flow(flow, flow["rev"])
        clock[0] += 10
    rows = service.revisions(flow["id"])["revisions"]
    assert [(r["rev"], r["kind"]) for r in rows] == [(4, "save"), (1, "create")]
    assert (
        rows[0]["summary"] == 'changed n1.text "A lighthouse on a rocky coast at dusk, w…" → "abc"'
    )
    clock[0] += service_module.REVISION_COALESCE_S + 1
    flow["nodes"][0]["values"]["text"] = "later"
    flow = await service.save_flow(flow, flow["rev"])
    assert [r["rev"] for r in service.revisions(flow["id"])["revisions"]] == [5, 4, 1]
    # An agent update in between is never folded into the user's row.
    spec = service.get_spec(flow["id"])["spec"]
    await service.update_from_spec(flow["id"], spec)
    flow = service.get_flow(flow["id"])
    flow = await service.save_flow(flow, flow["rev"])
    assert [(r["rev"], r["author"]) for r in service.revisions(flow["id"])["revisions"]][:3] == [
        (7, "user"),
        (6, "agent"),
        (5, "user"),
    ]


async def test_retention_keeps_the_last_fifty(service) -> None:
    created = await service.create_from_spec(SPEC)
    spec = service.get_spec(created["flow_id"])["spec"]
    for index in range(MAX_REVISIONS + 5):
        spec["nodes"][0]["values"]["text"] = f"version {index}"
        await service.update_from_spec(created["flow_id"], spec)
    rows = service.revisions(created["flow_id"])["revisions"]
    assert len(rows) == MAX_REVISIONS
    assert rows[0]["rev"] == MAX_REVISIONS + 6 and rows[-1]["rev"] == 7


async def test_old_docs_read_as_user_and_get_a_baseline_revision(service) -> None:
    created = await service.create_flow("Legacy", "text_to_image")
    path = service.store.root / f"{created['id']}.json"
    legacy = json.loads(path.read_text())
    legacy.pop("created_by")
    legacy.pop("updated_by")
    path.write_text(json.dumps(legacy))
    service.db.delete_revisions(created["id"])  # as if saved before history existed
    flow = service.get_flow(created["id"])
    assert (flow["created_by"], flow["updated_by"]) == ("user", "user")
    flow["nodes"][0]["values"]["text"] = "new"
    await service.save_flow(flow, flow["rev"])
    rows = service.revisions(created["id"])["revisions"]
    assert [(r["rev"], r["kind"]) for r in rows] == [(2, "save"), (1, "baseline")]
    restored = await service.restore(created["id"], 1, 2)
    assert restored["nodes"][0]["values"]["text"] == legacy["nodes"][0]["values"]["text"]


async def test_delete_drops_revisions_and_revision_blobs_survive_cache_eviction(service) -> None:
    flow = await service.create_flow("Gone")
    await service.delete_flow(flow["id"])
    assert service.db.list_revisions(flow["id"]) == []
    ref = service.upload("x", "image/png", base64.b64encode(png_bytes("pink")).decode())
    doc = {"id": "flw_rev00001", "rev": 1, "nodes": [{"values": {"image": ref.to_wire()}}]}
    service.db.add_revision(doc, author="user", session_key=None, kind="save", summary="")
    assert service._blob_in_use(ref.sha256)


def test_catalog_is_compact_and_lists_models(service) -> None:
    catalog = service.catalog()
    types = [n["type"] for n in catalog["nodes"]]
    assert types == [
        "text",
        "load_image",
        "join_text",
        "prompt_writer",
        "describe_image",
        "image",
        "save",
    ]
    image = next(n for n in catalog["nodes"] if n["type"] == "image")
    inputs = {i["name"]: i for i in image["inputs"]}
    assert inputs["prompt"]["link"] == "TEXT" and inputs["images"]["multi"] is True
    assert inputs["count"]["range"] == [1, 4] and inputs["variation"]["options"] == ["new", "keep"]
    assert image["outputs"] == [{"name": "image", "link": "IMAGE"}]
    models = {m["id"]: m for m in catalog["models"]}
    nb2 = models["openrouter/nano-banana-2"]
    assert nb2["available"] is True and nb2["tiers"] == ["1K", "2K", "4K"]
    assert nb2["price"]["per_image"]["1K"] == pytest.approx(0.0672)
    assert catalog["default_model"] == "openrouter/nano-banana-2"
    assert catalog["link_format"] == '["<node>.<output>", "<node>.<input>"]'


# ── round 2: stale updates, restore summary ───────────────────────────────


async def test_update_with_a_stale_base_rev_is_a_conflict(service) -> None:
    created = await service.create_from_spec(SPEC)
    flow_id = created["flow_id"]
    spec = service.get_spec(flow_id)["spec"]
    edited = service.get_flow(flow_id)
    edited["nodes"][2]["values"]["aspect"] = "1:1"
    await service.save_flow(edited, 1)  # the user moved on to rev 2
    spec["nodes"][0]["values"]["text"] = "agent edit"
    with pytest.raises(FlowError) as excinfo:
        await service.update_from_spec(flow_id, spec, base_rev=1)
    assert excinfo.value.code == "flows.conflict"
    assert excinfo.value.details == {
        "current_rev": 2,
        "summary_since": "changed gen.aspect 16:9 → 1:1",
    }
    assert service.get_flow(flow_id)["rev"] == 2  # nothing saved
    result = await service.update_from_spec(flow_id, spec, base_rev=2)
    assert result["rev"] == 3
    with pytest.raises(FlowError) as excinfo:
        await service.update_from_spec(flow_id, spec, base_rev="2")  # type: ignore[arg-type]
    assert excinfo.value.code == "flows.invalid"


async def test_summary_since_falls_back_to_revision_summaries(service) -> None:
    created = await service.create_from_spec(SPEC)
    flow = service.get_flow(created["flow_id"])
    service.db.delete_revisions(created["flow_id"])
    assert service.summary_since(flow, 1) == "rev 1 is not in the kept history"


async def test_restore_summary_describes_current_to_restored(service) -> None:
    created = await service.create_from_spec(SPEC)  # rev 1: aspect 16:9
    flow_id = created["flow_id"]
    spec = service.get_spec(flow_id)["spec"]
    spec["nodes"][2]["values"]["aspect"] = "1:1"
    await service.update_from_spec(flow_id, spec)  # rev 2: 1:1
    spec["nodes"][2]["values"]["aspect"] = "21:9"
    await service.update_from_spec(flow_id, spec)  # rev 3: 21:9
    await service.restore(flow_id, 2, 3)
    top = service.revisions(flow_id)["revisions"][0]
    assert top["rev"] == 4
    assert top["summary"] == "restored rev 2: changed gen.aspect 21:9 → 1:1"


# ── readable text changes ─────────────────────────────────────────────────


def _text_summary(old: str, new: str) -> str:
    doc = {"name": "N", "nodes": [{"id": "p", "type": "text", "values": {"text": old}}]}
    after = {"name": "N", "nodes": [{"id": "p", "type": "text", "values": {"text": new}}]}
    return diff_summary(doc, after)


def test_a_middle_edit_shows_the_changed_span() -> None:
    old = 'Hand-drawn marker infographic on cream paper. Title: "AGENTIC TRADING". Three panels.'
    new = (
        'Hand-drawn marker infographic on cream paper. Title: "CHANNELS ON AGENTOS". Three panels.'
    )
    assert _text_summary(old, new) == (
        'changed p.text "…. Title: \\"AGENTIC TRADING\\". Three p…" → '
        '"…. Title: \\"CHANNELS ON AGENTOS\\". Three p…"'
    )


def test_prefix_and_suffix_edits() -> None:
    base = "a calm harbour at dawn with fishing boats and gulls overhead"
    assert _text_summary(base, "A " + base[2:]) == (
        'changed p.text "a calm harb…" → "A calm harb…"'
    )
    assert _text_summary(base, base + ", in watercolor") == (
        'changed p.text "…s overhead" → "…s overhead, in watercolor"'
    )


def test_long_changes_are_capped_per_side() -> None:
    old = "intro " + "x" * 200
    new = "intro " + "y" * 200
    summary = _text_summary(old, new)
    left, right = summary.removeprefix("changed p.text ").split(" → ")
    assert left == json.dumps("intro " + "x" * 34 + "…", ensure_ascii=False)
    assert right == json.dumps("intro " + "y" * 34 + "…", ensure_ascii=False)


def test_multi_line_text_names_the_changed_line() -> None:
    old = "Title: AGENTOS\nSubtitle: one runtime\nFooter: 2026"
    new = "Title: AGENTOS\nSubtitle: one fast runtime\nFooter: 2026"
    assert _text_summary(old, new) == (
        'changed p.text line 2 "Subtitle: one runtime" → "Subtitle: one fast runtime"'
    )
    assert _text_summary("a\nb", "a\nb\nc\nd") == 'changed p.text lines 3-4 "" → "c\\nd"'


def test_short_values_stay_plain() -> None:
    assert _text_summary("one", "two") == "changed p.text one → two"
