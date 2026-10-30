"""FlowService end to end with fake adapters: runs, events, cache, queue, recovery."""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.flows.adapters import AdapterRequest, AdapterResult
from agentos.flows.models import ModelEntry
from agentos.flows.pngmeta import read_flow_metadata
from agentos.flows.service import FlowService
from agentos.flows.types import FlowError
from tests.test_flows.conftest import (
    FakeImageAdapter,
    FakeTextAdapter,
    Recorder,
    doc,
    link,
    node,
    png_bytes,
    stored,
    t2i_doc,
)


async def _run(service: FlowService, flow_id: str, **kwargs: Any) -> dict[str, Any]:
    queued = await service.submit_run(flow_id, **kwargs)
    assert queued["status"] == "queued" and queued["run_id"].startswith("run_")
    return await service.wait_run(queued["run_id"], timeout=10)


def _upload(service: FlowService, color: str = "red", size=(40, 30)) -> dict[str, Any]:
    data = base64.b64encode(png_bytes(color, size)).decode()
    return service.upload("x.png", "image/png", data).to_wire()


async def test_text_to_image_run_end_to_end(service, images, recorder: Recorder) -> None:
    flow = await stored(service, t2i_doc(count=2))
    run = await _run(service, flow["id"], origin="cli")
    assert run["status"] == "done", run["error"]
    assert run["origin"] == "cli" and run["error"] is None
    assert {k: v["state"] for k, v in run["nodes"].items()} == {
        "t": "done",
        "i": "done",
        "s": "done",
    }
    assert run["nodes"]["i"]["cost_usd"] == pytest.approx(0.10)  # provider-reported
    assert run["cost_usd"] == pytest.approx(0.10)
    assert run["est_usd"] == pytest.approx(0.1344)
    assert run["image_count"] == 2
    assert run["outputs"]["t"] == {"text": "a lighthouse"}
    assert len(run["outputs"]["i"]["images"]) == 2
    save = run["outputs"]["s"]
    assert len(save["images"]) == 2 and len(save["files"]) == 2
    assert run["preview"] == save["images"][0]
    # Every saved PNG carries the graph.
    path = Path(save["files"][0])
    assert path.parent == service.outputs_root / flow["id"]
    assert path.name == f"Test_{run['run_id']}_1.png"
    meta = read_flow_metadata(path.read_bytes())
    assert meta is not None
    assert meta["flow_id"] == flow["id"] and meta["run_id"] == run["run_id"]
    assert meta["rev"] == flow["rev"] and meta["graph"]["nodes"][1]["id"] == "i"
    # Events: per-run seq strictly increasing; cached/done carry outputs.
    events = recorder.for_run(run["run_id"])
    seqs = [payload["seq"] for _, payload in events]
    assert seqs == list(range(1, len(seqs) + 1))
    assert run["seq"] == seqs[-1]  # the snapshot says which event it includes
    statuses = [p["status"] for e, p in events if e == "flows.run"]
    assert statuses == ["queued", "running", "done"]
    node_events = [(p["node_id"], p["state"]) for e, p in events if e == "flows.node"]
    assert node_events.index(("t", "done")) < node_events.index(("i", "running"))
    assert node_events.index(("i", "done")) < node_events.index(("s", "running"))
    progress = [p["progress"] for e, p in events if e == "flows.node" and "progress" in p]
    assert progress == [{"value": 1, "max": 2}, {"value": 2, "max": 2}]
    done_image = next(
        p for e, p in events if e == "flows.node" and p["node_id"] == "i" and p["state"] == "done"
    )
    assert len(done_image["outputs"]["images"]) == 2
    # Summary + history.
    runs = service.list_runs(flow["id"])
    assert [r["run_id"] for r in runs] == [run["run_id"]]
    assert set(runs[0]) == {
        "run_id",
        "flow_id",
        "rev",
        "status",
        "created",
        "started",
        "ended",
        "est_usd",
        "cost_usd",
        "preview",
        "origin",
        "error",
        "image_count",
    }
    assert service.list_flows()["flows"][0]["preview"] == run["preview"]


async def test_count_fans_out_when_the_model_has_no_n(service, images) -> None:
    flow = await stored(service, t2i_doc(count=3))
    run = await _run(service, flow["id"])
    assert run["status"] == "done"
    assert [req.n for _, req in images.calls] == [1, 1, 1]
    assert [(req.aspect_ratio, req.image_size) for _, req in images.calls][0] == ("1:1", "1K")
    assert len(run["outputs"]["i"]["images"]) == 3


async def test_count_is_one_call_with_n_when_the_model_takes_n(service, images) -> None:
    flow = await stored(
        service, t2i_doc(model="openai/gpt-image-1", count=3, quality="low", size="1024x1536")
    )
    run = await _run(service, flow["id"])
    assert run["status"] == "done"
    assert len(images.calls) == 1
    request = images.calls[0][1]
    assert (request.n, request.quality, request.size) == (3, "low", "1024x1536")
    assert request.aspect_ratio is None and request.image_size is None  # hidden for GPT
    assert run["image_count"] == 3


async def test_connected_images_switch_to_edit_and_are_downscaled(service, images) -> None:
    big = _upload(service, "red", (3000, 1000))
    small = _upload(service, "blue", (64, 64))
    graph = doc(
        [
            node("a", "load_image", image=big),
            node("b", "load_image", image=small),
            node("t", "text", text="merge them"),
            node("i", "image", model="openrouter/nano-banana-2", variation="keep"),
        ],
        [
            link("l1", "a", "image", "i", "images"),
            link("l2", "b", "image", "i", "images"),
            link("l3", "t", "text", "i", "prompt"),
        ],
    )
    flow = await stored(service, graph)
    run = await _run(service, flow["id"])
    assert run["status"] == "done", run["error"]
    request = images.calls[0][1]
    assert len(request.images) == 2 and request.prompt == "merge them"
    from PIL import Image

    first = Image.open(io.BytesIO(request.images[0][0]))
    assert max(first.size) == 2048  # fitted into 2048 x 2048
    second = Image.open(io.BytesIO(request.images[1][0]))
    assert second.size == (64, 64)


async def test_at_most_two_concurrent_calls_per_provider(service, images) -> None:
    images.delay = 0.05
    nodes = [node("t", "text", text="x")]
    links = []
    for index in range(5):
        nodes.append(node(f"i{index}", "image", model="openrouter/nano-banana-2", variation="keep"))
        links.append(link(f"l{index}", "t", "text", f"i{index}", "prompt"))
    flow = await stored(service, doc(nodes, links))
    run = await _run(service, flow["id"])
    assert run["status"] == "done"
    assert len(images.calls) == 5
    assert images.peak["openrouter"] == 2


class _SelectiveAdapter(FakeImageAdapter):
    """Fails prompts containing 'boom' quickly; 'slow' prompts take a while."""

    async def generate(self, entry: ModelEntry, request: AdapterRequest) -> AdapterResult:
        if "boom" in request.prompt:
            from agentos.flows.types import NodeError

            raise NodeError("credits", "The provider account is out of credits")
        if "slow" in request.prompt:
            await asyncio.sleep(0.1)
        return await super().generate(entry, request)


async def test_an_error_stops_scheduling_lets_in_flight_finish_and_skips_the_rest(tmp_path):
    adapter = _SelectiveAdapter()
    from agentos.flows.models import ModelRegistry
    from tests.test_flows.conftest import fake_auth

    service = FlowService(
        None,
        root=tmp_path / "flows",
        image_adapter=adapter,
        text_adapter=FakeTextAdapter(),
        models=ModelRegistry(auth=fake_auth("openrouter")),
    )
    service.start()
    try:
        values = {"model": "openrouter/nano-banana-2", "variation": "keep"}
        graph = doc(
            [
                node("ta", "text", text="boom"),
                node("tb", "text", text="slow"),
                node("a", "image", **values),
                node("b", "image", **values),
                node("sa", "save"),
                node("sb", "save"),
            ],
            [
                link("l1", "ta", "text", "a", "prompt"),
                link("l2", "tb", "text", "b", "prompt"),
                link("l3", "a", "image", "sa", "images"),
                link("l4", "b", "image", "sb", "images"),
            ],
        )
        flow = await stored(service, graph)
        run = await _run(service, flow["id"])
        states = {k: v["state"] for k, v in run["nodes"].items()}
        assert run["status"] == "failed"
        assert run["error"] == {
            "code": "credits",
            "message": "The provider account is out of credits",
            "node_id": "a",
        }
        assert states["a"] == "error" and states["b"] == "done"
        assert states["sa"] == "skipped" and states["sb"] == "skipped"
        assert run["nodes"]["a"]["error"]["code"] == "credits"
        # The in-flight node was cached: a second run does not call it again.
        est = service.estimate(service.get_flow(flow["id"]), to_node="sb")
        assert "b" in est["cached"]
    finally:
        await service.close()


async def test_empty_result_is_a_node_error_with_the_model_text(service, images) -> None:
    images.empty_text = "I can't create that image."
    flow = await stored(service, t2i_doc())
    run = await _run(service, flow["id"])
    assert run["status"] == "failed"
    error = run["nodes"]["i"]["error"]
    assert error["code"] == "empty_result" and "I can't create that image." in error["message"]


async def _until(predicate, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def test_stop_cancels_at_once_and_books_the_detached_call(service, images, recorder) -> None:
    images.gate = asyncio.Event()
    flow = await stored(service, t2i_doc())
    queued = await service.submit_run(flow["id"])
    follower = await service.submit_run(flow["id"])
    await _until(lambda: images.calls)
    await service.stop_run(queued["run_id"])
    run = service.get_run(queued["run_id"])
    # The run is over for the user at once ...
    assert run["status"] == "cancelled" and run["ended"]
    assert run["nodes"]["s"]["state"] == "skipped"  # never scheduled
    # ... but the provider call is not aborted (it would be billed anyway).
    assert run["nodes"]["i"]["state"] == "running"
    assert images.active["openrouter"] == 1
    statuses = [p["status"] for e, p in recorder.for_run(queued["run_id"]) if e == "flows.run"]
    assert statuses == ["queued", "running", "cancelled"]
    # Its queue slot is free: the next run starts while the detached call is out.
    await _until(lambda: service.get_run(follower["run_id"])["status"] == "running")
    await service.stop_run(queued["run_id"])  # stopping again is a no-op

    images.gate.set()
    await _until(lambda: service.get_run(queued["run_id"])["nodes"]["i"]["state"] == "done")
    await _until(lambda: queued["run_id"] not in service._handles)
    run = service.get_run(queued["run_id"])
    assert run["status"] == "cancelled" and run["error"] is None
    assert run["nodes"]["i"]["cost_usd"] == pytest.approx(0.05)
    assert run["cost_usd"] == pytest.approx(0.05)
    assert run["nodes"]["s"]["state"] == "skipped"
    events = recorder.for_run(queued["run_id"])
    node_done = [
        p for e, p in events if e == "flows.node" and p["node_id"] == "i" and p["state"] == "done"
    ]
    assert node_done and node_done[0]["cost_usd"] == pytest.approx(0.05)
    assert "images" in node_done[0]["outputs"]
    last_run = [p for e, p in events if e == "flows.run"][-1]
    assert last_run["status"] == "cancelled" and last_run["cost_usd"] == pytest.approx(0.05)
    assert run["seq"] == events[-1][1]["seq"]
    await service.wait_run(follower["run_id"], timeout=5)
    # The paid-for image was cached: a locked rerun reuses it.
    calls = len(images.calls)
    again = await _run(service, flow["id"])
    assert again["nodes"]["i"] == {"state": "cached"} and len(images.calls) == calls
    with pytest.raises(FlowError) as excinfo:
        await service.stop_run("run_doesnotexist")
    assert excinfo.value.code == "flows.not_found"


async def test_stop_before_the_run_task_starts(service, images) -> None:
    flow = await stored(service, t2i_doc())
    queued = await service.submit_run(flow["id"])
    await service.stop_run(queued["run_id"])  # same tick: the task has not run yet
    run = service.get_run(queued["run_id"])
    assert run["status"] == "cancelled" and images.calls == []


async def test_shutdown_after_stop_keeps_the_run_cancelled(tmp_path, images) -> None:
    images.gate = asyncio.Event()
    from agentos.flows.models import ModelRegistry
    from tests.test_flows.conftest import fake_auth

    service = FlowService(
        None,
        root=tmp_path / "flows",
        image_adapter=images,
        models=ModelRegistry(auth=fake_auth("openrouter")),
    )
    flow = await stored(service, t2i_doc())
    queued = await service.submit_run(flow["id"])
    await _until(lambda: images.calls)
    await service.stop_run(queued["run_id"])
    await service.close()  # the detached call is still out
    reopened = FlowService(None, root=tmp_path / "flows")
    run = reopened.get_run(queued["run_id"])
    assert run["status"] == "cancelled" and run["nodes"]["i"]["state"] == "skipped"
    assert run["cost_usd"] is None  # unknown: the call never came back
    await reopened.close()


async def test_queue_is_fifo_with_one_run_at_a_time(service, images) -> None:
    images.gate = asyncio.Event()
    flow = await stored(service, t2i_doc())
    first = await service.submit_run(flow["id"])
    second = await service.submit_run(flow["id"])
    third = await service.submit_run(flow["id"])
    await asyncio.sleep(0.05)
    assert service.get_run(first["run_id"])["status"] == "running"
    waiting = service.get_run(second["run_id"])
    assert waiting["status"] == "queued" and waiting["seq"] == 1  # the "queued" event
    await service.stop_run(third["run_id"])  # cancelling a queued run never starts it
    assert service.get_run(third["run_id"])["status"] == "cancelled"
    images.gate.set()
    done_first = await service.wait_run(first["run_id"], timeout=5)
    done_second = await service.wait_run(second["run_id"], timeout=5)
    assert done_first["status"] == done_second["status"] == "done"
    assert done_first["ended"] <= done_second["started"]
    assert len(images.calls) == 2


async def test_max_concurrent_runs_from_config(tmp_path) -> None:
    adapter = FakeImageAdapter()
    adapter.gate = asyncio.Event()
    from agentos.flows.models import ModelRegistry
    from tests.test_flows.conftest import fake_auth

    config = SimpleNamespace(flows=SimpleNamespace(max_concurrent_runs=2), image_generation=None)
    service = FlowService(
        config,
        root=tmp_path / "flows",
        image_adapter=adapter,
        text_adapter=FakeTextAdapter(),
        models=ModelRegistry(auth=fake_auth("openrouter")),
    )
    try:
        flow = await stored(service, t2i_doc())
        runs = [await service.submit_run(flow["id"]) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert [service.get_run(r["run_id"])["status"] for r in runs] == [
            "running",
            "running",
            "queued",
        ]
        adapter.gate.set()
        for r in runs:
            assert (await service.wait_run(r["run_id"], timeout=5))["status"] == "done"
    finally:
        await service.close()


async def test_variation_keep_reuses_the_image_the_last_new_run_made(service, images) -> None:
    flow = await stored(service, t2i_doc(variation="new"))
    first = await _run(service, flow["id"])
    again = await _run(service, flow["id"])
    assert len(images.calls) == 2  # "new" always generates
    assert first["outputs"]["i"]["images"] != again["outputs"]["i"]["images"]
    current = service.get_flow(flow["id"])
    current["nodes"][1]["values"]["variation"] = "keep"
    current = await service.save_flow(current, current["rev"])
    locked = await _run(service, flow["id"])
    assert len(images.calls) == 2
    assert locked["nodes"]["i"] == {"state": "cached"}
    assert locked["outputs"]["i"]["images"] == again["outputs"]["i"]["images"]
    assert "t" not in locked["nodes"]  # pruned above the cached node
    assert locked["est_usd"] == 0.0 and locked["cost_usd"] is None


async def test_llm_nodes_use_the_text_adapter_tasks(service, images, texts) -> None:
    photo = _upload(service, "green")
    graph = doc(
        [
            node("l", "load_image", image=photo),
            node("d", "describe_image", focus="colors"),
            node("t", "text", text="a poster"),
            node("w", "prompt_writer", style="anime"),
            node("j", "join_text", template="{a} | {b}"),
            node("i", "image", model="openrouter/nano-banana-2", variation="keep"),
        ],
        [
            link("l1", "l", "image", "d", "image"),
            link("l2", "d", "text", "w", "context"),
            link("l3", "t", "text", "w", "idea"),
            link("l4", "w", "text", "j", "a"),
            link("l5", "t", "text", "j", "b"),
            link("l6", "j", "text", "i", "prompt"),
        ],
    )
    flow = await stored(service, graph)
    run = await _run(service, flow["id"])
    assert run["status"] == "done", run["error"]
    tasks = [call["task"] for call in texts.calls]
    assert tasks == ["vision", "flow_prompt"]
    vision, writer = texts.calls
    assert len(vision["images"]) == 1 and "color" in vision["prompt"].lower()
    assert "Idea: a poster" in writer["prompt"]
    assert "Reference context:\na detailed prompt" in writer["prompt"]
    assert "anime" in writer["system"].lower()
    assert images.calls[0][1].prompt == "a detailed prompt | a poster"
    assert run["nodes"]["w"]["cost_usd"] == pytest.approx(0.002)  # the estimate


async def test_join_text_drops_a_dangling_separator(service) -> None:
    from agentos.flows.nodes import join_texts

    assert join_texts("a cat", "", "{a}, {b}") == "a cat"
    assert join_texts("", "b", "{a}, {b}") == "b"
    assert join_texts("x", "y", "{a} and {b}") == "x and y"


async def test_invalid_run_raises_with_node_errors_and_queues_nothing(service, images) -> None:
    flow = await stored(service, t2i_doc())
    flow["nodes"][0]["values"]["text"] = ""
    flow = await service.save_flow(flow, flow["rev"])
    with pytest.raises(FlowError) as excinfo:
        await service.submit_run(flow["id"])
    assert excinfo.value.code == "flows.invalid"
    assert excinfo.value.details == {
        "node_errors": {"t": [{"code": "required", "message": "text is required", "input": "text"}]}
    }
    assert service.list_runs(flow["id"]) == [] and images.calls == []


async def test_public_inputs_override_and_coerce(service, images) -> None:
    flow = await stored(service, t2i_doc())
    run = await _run(service, flow["id"], inputs={"prompt": "a red barn", "count": "2"})
    assert run["status"] == "done"
    assert run["inputs"] == {"prompt": "a red barn", "count": "2"}
    assert images.calls[0][1].prompt == "a red barn" and len(images.calls) == 2
    assert run["graph"]["nodes"][0]["values"]["text"] == "a red barn"
    assert service.get_flow(flow["id"])["nodes"][0]["values"]["text"] == "a lighthouse"
    with pytest.raises(FlowError) as excinfo:
        await service.submit_run(flow["id"], inputs={"model": "x"})
    assert excinfo.value.code == "flows.invalid" and "public input" in excinfo.value.message


async def test_cost_gate(service) -> None:
    flow = await stored(service, t2i_doc(count=4))
    with pytest.raises(FlowError) as excinfo:
        await service.submit_run(flow["id"], max_cost_usd=0.10)
    assert excinfo.value.code == "flows.cost_gate"
    assert excinfo.value.details["estimate"]["total"]["usd"] == pytest.approx(0.2688)
    ok = await service.submit_run(flow["id"], max_cost_usd=0.50)
    assert (await service.wait_run(ok["run_id"], timeout=5))["status"] == "done"


async def test_disabled_flows_refuse_runs(tmp_path) -> None:
    config = SimpleNamespace(flows=SimpleNamespace(enabled=False))
    service = FlowService(config, root=tmp_path / "flows")
    try:
        flow = await stored(service, t2i_doc())
        with pytest.raises(FlowError) as excinfo:
            await service.submit_run(flow["id"])
        assert excinfo.value.code == "flows.unavailable"
    finally:
        await service.close()


async def test_save_rev_conflict_and_not_found(service) -> None:
    created = await service.create_flow("Doc", "text_to_image")
    assert created["rev"] == 1 and created["id"].startswith("flw_")
    first = await service.save_flow(created, 1)
    assert first["rev"] == 2
    with pytest.raises(FlowError) as excinfo:
        await service.save_flow(created, 1)
    assert excinfo.value.code == "flows.conflict"
    assert excinfo.value.details["rev"] == 2
    with pytest.raises(FlowError) as excinfo:
        service.get_flow("flw_missing1")
    assert excinfo.value.code == "flows.not_found"
    bad = dict(first, nodes=[{"id": "a", "type": "text"}, {"id": "a", "type": "text"}])
    with pytest.raises(FlowError) as excinfo:
        await service.save_flow(bad, 2)
    assert excinfo.value.code == "flows.invalid"
    await service.delete_flow(created["id"])
    assert service.list_flows() == {"flows": []}


async def test_templates_create_valid_graphs(service) -> None:
    for template in ("blank", "text_to_image", "edit_image", "describe_remix"):
        flow = await service.create_flow(f"T {template}", template)
        assert flow["version"] == 1 and flow["rev"] == 1
    t2i = next(f for f in service.list_flows()["flows"] if f["name"] == "T text_to_image")
    assert service.validate(service.get_flow(t2i["id"])) == {"ok": True, "node_errors": {}}
    remix = next(f for f in service.list_flows()["flows"] if f["name"] == "T describe_remix")
    errors = service.validate(service.get_flow(remix["id"]))["node_errors"]
    assert errors == {
        "n1": [{"code": "required", "message": "image is required", "input": "image"}]
    }
    with pytest.raises(FlowError):
        await service.create_flow("x", "nope")


async def test_png_import_round_trip(service) -> None:
    flow = await stored(service, t2i_doc())
    run = await _run(service, flow["id"])
    data = Path(run["outputs"]["s"]["files"][0]).read_bytes()
    imported = service.import_png(base64.b64encode(data).decode())
    assert imported["flow"]["id"] == flow["id"]
    assert [n["id"] for n in imported["flow"]["nodes"]] == ["t", "i", "s"]
    assert imported["image"]["mime"] == "image/png"
    plain = service.import_png(base64.b64encode(png_bytes("pink")).decode())
    assert plain["flow"] is None and plain["image"]["width"] == 32


async def test_media_and_thumbnails(service) -> None:
    ref = _upload(service, "red", (400, 200))
    full = service.media_wire(ref["sha256"])
    assert (full["width"], full["height"], full["mime"]) == (400, 200, "image/png")
    thumb = service.media_wire(ref["sha256"], max_side=100)
    assert (thumb["width"], thumb["height"]) == (100, 50)
    assert base64.b64decode(thumb["data_b64"])[:3] in (b"\xff\xd8\xff", b"\x89PN")
    with pytest.raises(FlowError) as excinfo:
        service.media_wire("b" * 64)
    assert excinfo.value.code == "flows.not_found"
    with pytest.raises(FlowError) as excinfo:
        service.upload("x", "image/png", base64.b64encode(b"not an image").decode())
    assert excinfo.value.code == "flows.invalid"


async def test_startup_recovery_marks_interrupted_runs_failed(tmp_path) -> None:
    root = tmp_path / "flows"
    first = FlowService(None, root=root)
    flow = await stored(first, t2i_doc())
    for run_id, status in (("run_000000000001", "running"), ("run_000000000002", "queued")):
        first.db.insert_run(
            {
                "run_id": run_id,
                "flow_id": flow["id"],
                "rev": 1,
                "status": status,
                "created": "2026-10-29T10:00:00Z",
                "graph": flow,
                "nodes": {"i": {"state": "running"}, "s": {"state": "queued"}},
            }
        )
    first.db.close()
    second = FlowService(None, root=root)
    second.start()
    try:
        for run_id in ("run_000000000001", "run_000000000002"):
            run = second.get_run(run_id)
            assert run["status"] == "failed"
            assert run["error"]["code"] == "flows.interrupted"
            assert {v["state"] for v in run["nodes"].values()} == {"skipped"}
            assert run["ended"]
    finally:
        await second.close()


async def test_close_interrupts_a_running_run(tmp_path, images) -> None:
    images.gate = asyncio.Event()
    from agentos.flows.models import ModelRegistry
    from tests.test_flows.conftest import fake_auth

    service = FlowService(
        None,
        root=tmp_path / "flows",
        image_adapter=images,
        models=ModelRegistry(auth=fake_auth("openrouter")),
    )
    flow = await stored(service, t2i_doc())
    queued = await service.submit_run(flow["id"])
    await asyncio.sleep(0.05)
    await service.close()
    reopened = FlowService(None, root=tmp_path / "flows")
    run = reopened.get_run(queued["run_id"])
    assert run["status"] == "failed" and run["error"]["code"] == "flows.interrupted"
    await reopened.close()


async def test_cache_is_lru_by_bytes(tmp_path, images) -> None:
    from agentos.flows.models import ModelRegistry
    from tests.test_flows.conftest import fake_auth

    config = SimpleNamespace(flows=SimpleNamespace(cache_max_mb=0))
    service = FlowService(
        config,
        root=tmp_path / "flows",
        image_adapter=images,
        models=ModelRegistry(auth=fake_auth("openrouter")),
    )
    try:
        flow = await stored(service, t2i_doc())
        first = await _run(service, flow["id"])
        assert service.db.cache_total_bytes() == 0  # nothing fits in 0 MB
        # The run still references its images, so their blobs survive eviction.
        sha = first["outputs"]["i"]["images"][0]["sha256"]
        assert service.media.exists(sha)
        await _run(service, flow["id"])
        assert len(images.calls) == 2  # evicted: the locked image ran again
    finally:
        await service.close()


async def test_costs_are_recorded_under_the_flow_scope(service, images) -> None:
    from agentos.engine.usage import _current_usage_scope

    calls: list[tuple[str, str | None, float, str]] = []

    class Tracker:
        def add(self, session_key, input_tokens, output_tokens, model_id="", **kw):
            calls.append((session_key, _current_usage_scope.get(), kw["billed_cost"], model_id))

    from agentos.gateway.flows_wiring import record_flow_cost

    service.usage_tracker = Tracker()
    service.cost_recorder = record_flow_cost
    flow = await stored(service, t2i_doc())
    run = await _run(service, flow["id"], session_key="agent:main:web:s1")
    assert calls == [
        (
            "agent:main:web:s1",
            f"flow:{run['run_id']}",
            pytest.approx(0.05),
            "google/gemini-3.1-flash-image",
        )
    ]


async def test_artifact_sink_receives_saved_files(service) -> None:
    seen: list[tuple[str, str]] = []

    def sink(path: Path, mime: str) -> dict[str, Any]:
        seen.append((path.name, mime))
        return {"id": "art_1", "name": path.name}

    flow = await stored(service, t2i_doc())
    run = await _run(service, flow["id"], origin="agent", artifact_sink=sink)
    assert run["origin"] == "agent"
    assert seen == [(Path(run["outputs"]["s"]["files"][0]).name, "image/png")]
    assert run["outputs"]["s"]["artifacts"] == [{"id": "art_1", "name": seen[0][0]}]


async def test_delete_is_refused_while_a_run_is_active(service, images) -> None:
    images.gate = asyncio.Event()
    flow = await stored(service, t2i_doc())
    queued = await service.submit_run(flow["id"])
    with pytest.raises(FlowError) as excinfo:
        await service.delete_flow(flow["id"])
    assert excinfo.value.code == "flows.busy"
    await service.stop_run(queued["run_id"])
    await service.delete_flow(flow["id"])


# ── round 2: aspect notes and produced images ──────────────────────────────


async def test_off_aspect_results_carry_a_note(service, images, recorder) -> None:
    # The fake provider always returns 32x24 (4:3) images.
    flow = await stored(service, t2i_doc(aspect="3:2"))
    run = await _run(service, flow["id"])
    note = "Provider returned 32×24 instead of 3:2"
    assert run["outputs"]["i"]["note"] == note
    done = [
        p for e, p in recorder.for_run(run["run_id"]) if e == "flows.node" and p["state"] == "done"
    ]
    assert next(p for p in done if p["node_id"] == "i")["outputs"]["note"] == note
    matching = await stored(service, t2i_doc(aspect="4:3"))
    assert "note" not in (await _run(service, matching["id"]))["outputs"]["i"]


def test_aspect_note_rules() -> None:
    from agentos.flows.nodes import aspect_note
    from agentos.flows.types import ImageRef

    wide = ImageRef("a" * 64, "image/png", 1024, 768, 1)
    assert aspect_note("1536x1024", None, [wide]) == (
        "Provider returned 1024×768 instead of 1536×1024"
    )
    assert aspect_note(None, "4:3", [wide]) is None
    assert aspect_note("1032x768", None, [wide]) is None  # within 3 %
    assert aspect_note("auto", None, [wide]) is None and aspect_note(None, None, [wide]) is None


async def test_images_count_only_what_the_run_made(service, images) -> None:
    flow = await stored(service, t2i_doc(count=2, variation="keep"))
    first = await _run(service, flow["id"])
    saved = first["outputs"]["s"]["images"]
    assert first["images"] == saved and first["image_count"] == 2  # saves cover the generated
    assert first["preview"] == saved[0]
    again = await _run(service, flow["id"])  # image cached: Save only re-wrote old pictures
    assert again["nodes"]["i"] == {"state": "cached"}
    assert len(again["outputs"]["s"]["files"]) == 2  # the files are still written ...
    assert again["images"] == [] and again["image_count"] == 0  # ... but nothing is new
    assert again["preview"] is None

    no_save = t2i_doc(variation="keep")
    no_save["nodes"] = no_save["nodes"][:2]
    no_save["links"] = no_save["links"][:1]
    bare = await stored(service, no_save)
    made = await _run(service, bare["id"])
    assert made["images"] == made["outputs"]["i"]["images"] and made["image_count"] == 1
    cached = await _run(service, bare["id"])
    assert cached["images"] == [] and cached["image_count"] == 0 and cached["preview"] is None
    summary = service.list_runs(bare["id"])[0]
    assert summary["image_count"] == 0 and summary["preview"] is None


def test_produced_images_dedupes_and_skips_inputs() -> None:
    from agentos.flows.executor import produced_images

    def ref(char: str) -> dict:
        return {"sha256": char * 64, "mime": "image/png", "width": 1, "height": 1, "bytes": 1}

    graph = {
        "nodes": [
            {"id": "l", "type": "load_image"},
            {"id": "a", "type": "image"},
            {"id": "b", "type": "image"},
            {"id": "s", "type": "save"},
        ],
        "links": [
            {"id": "1", "from": ["l", "image"], "to": ["a", "images"]},
            {"id": "2", "from": ["a", "image"], "to": ["s", "images"]},
        ],
    }
    nodes = {"l": {"state": "done"}, "a": {"state": "done"}, "b": {"state": "done"}}
    outputs = {
        "l": {"images": [ref("1")]},  # an input: never counted
        "a": {"images": [ref("2")]},  # saved below: represented by the saved file
        "b": {"images": [ref("3"), ref("3")]},  # duplicate sha: once
        "s": {"images": [ref("4")]},
    }
    assert [r["sha256"][0] for r in produced_images(graph, nodes, outputs)] == ["4", "3"]
    nodes["b"] = {"state": "cached"}
    assert [r["sha256"][0] for r in produced_images(graph, nodes, outputs)] == ["4"]
    # The saved file of a cached image is not new; the executed edit next to it is.
    nodes["a"] = {"state": "cached"}
    nodes["b"] = {"state": "done"}
    assert [r["sha256"][0] for r in produced_images(graph, nodes, outputs)] == ["3"]
    # Save fed by an upload and an executed image: only the latter's file counts.
    graph["links"].append({"id": "3", "from": ["b", "image"], "to": ["s", "images"]})
    graph["links"][1] = {"id": "2", "from": ["l", "image"], "to": ["s", "images"]}
    outputs["b"] = {"images": [ref("3")]}
    outputs["s"] = {"images": [ref("5"), ref("6")]}
    assert [r["sha256"][0] for r in produced_images(graph, nodes, outputs)] == ["6"]


async def test_resaving_a_cached_image_is_not_a_new_result(service, images) -> None:
    """Text → Image(keep) → Save, plus an edit Image not wired to Save (the live case)."""
    upload = _upload(service, "red")
    graph = t2i_doc(variation="keep")
    graph["nodes"] += [
        node("u", "load_image", image=upload),
        node("e", "image", prompt="paint it", model="openrouter/nano-banana-2", variation="new"),
    ]
    graph["links"].append(link("l9", "u", "image", "e", "images"))
    flow = await stored(service, graph)
    first = await _run(service, flow["id"])
    assert first["image_count"] == 2  # the saved fresh image + the edit
    second = await _run(service, flow["id"])
    assert second["nodes"]["i"] == {"state": "cached"} and second["nodes"]["e"]["state"] == "done"
    assert len(second["outputs"]["s"]["images"]) == 1  # Save still wrote its file
    assert second["images"] == second["outputs"]["e"]["images"]  # only the edit is new
    assert second["preview"] == second["outputs"]["e"]["images"][0]
    assert second["image_count"] == 1
