"""``flows.*`` RPC through the real dispatcher, with a fake-adapter service."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.flows.service import FlowService, reset_flow_service, set_flow_service
from agentos.gateway.access import CONTROL_ONLY, ConnectionSurface
from agentos.gateway.auth import AccessContext
from agentos.gateway.rpc import RpcContext, get_dispatcher
from tests.test_flows.conftest import png_bytes, t2i_doc

METHODS = (
    "flows.nodes",
    "flows.models",
    "flows.list",
    "flows.create",
    "flows.get",
    "flows.save",
    "flows.delete",
    "flows.validate",
    "flows.estimate",
    "flows.run",
    "flows.stop",
    "flows.runs",
    "flows.run.get",
    "flows.media",
    "flows.upload",
    "flows.import_png",
)
WRITES = (
    "flows.create",
    "flows.save",
    "flows.delete",
    "flows.run",
    "flows.stop",
    "flows.upload",
    "flows.import_png",
)


@pytest.fixture
async def rpc_service(service: FlowService, recorder, monkeypatch) -> AsyncIterator[FlowService]:
    # Handlers hand the service the gateway broadcast on every call; record it instead.
    monkeypatch.setattr("agentos.gateway.rpc_flows.broadcast_flow_event", recorder)
    set_flow_service(service)
    try:
        yield service
    finally:
        reset_flow_service()


@pytest.fixture
def ctx() -> RpcContext:
    return RpcContext(conn_id="test-conn")


async def call(method: str, params: dict[str, Any] | None, ctx: RpcContext):
    return await get_dispatcher().dispatch("r1", method, params, ctx)


async def ok(method: str, params: dict[str, Any] | None, ctx: RpcContext) -> Any:
    res = await call(method, params, ctx)
    assert res.ok, (method, res.error)
    return res.payload


def _agent_ctx() -> RpcContext:
    from agentos.gateway.agent_surface import AgentSurface

    surface = AgentSurface()
    token = surface.mint_token("agent:main:webchat:t", "main")
    access = surface.attach(
        AccessContext(surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=True),
        {"agentToken": token},
    )
    return RpcContext(conn_id="agent-conn", access=access)


def test_every_method_is_registered_on_the_control_surface_only() -> None:
    registry = get_dispatcher()
    assert set(METHODS) <= set(registry.methods())
    for method in METHODS:
        entry = registry.get_entry(method)
        assert entry is not None and entry.audiences == CONTROL_ONLY, method


async def test_channel_surface_is_refused(rpc_service) -> None:
    channel = RpcContext(
        conn_id="chan",
        access=AccessContext(
            surface=ConnectionSurface.CHANNEL, admitted=True, credential_verified=True
        ),
    )
    res = await call("flows.list", {}, channel)
    assert res.ok is False and res.error.code == "UNAUTHORIZED"


async def test_happy_path_create_save_run_inspect(rpc_service, ctx, recorder) -> None:
    nodes = await ok("flows.nodes", None, ctx)
    assert len(nodes["nodes"]) == 7
    models = await ok("flows.models", None, ctx)
    assert models["default"] == "openrouter/nano-banana-2"

    flow = (await ok("flows.create", {"name": "Lighthouse", "template": "text_to_image"}, ctx))[
        "flow"
    ]
    assert flow["rev"] == 1 and flow["name"] == "Lighthouse"
    assert ("flows.changed", {"flow_id": flow["id"], "rev": 1}) in recorder.events
    listed = (await ok("flows.list", {}, ctx))["flows"]
    assert listed == [
        {
            "id": flow["id"],
            "name": "Lighthouse",
            "rev": 1,
            "updated": flow["updated"],
            "node_count": 3,
            "preview": None,
        }
    ]
    assert (await ok("flows.get", {"id": flow["id"]}, ctx))["flow"] == flow

    flow["nodes"][1]["values"]["model"] = "openrouter/nano-banana-2"
    saved = (await ok("flows.save", {"flow": flow, "base_rev": 1}, ctx))["flow"]
    assert saved["rev"] == 2

    assert await ok("flows.validate", {"flow": saved}, ctx) == {"ok": True, "node_errors": {}}
    est = await ok("flows.estimate", {"flow": saved, "inputs": {"count": 2}}, ctx)
    assert est["total"] == {"kind": "usd", "usd": 0.1344, "approximate": True}
    assert est["will_run"] == ["n1", "n2", "n3"] and est["cached"] == []

    # Saving through flows.run: base_rev is the flow's own rev.
    saved["name"] = "Lighthouse study"
    queued = await ok("flows.run", {"flow_id": saved["id"], "flow": saved}, ctx)
    assert queued["status"] == "queued"
    await rpc_service.wait_run(queued["run_id"], timeout=5)
    assert (await ok("flows.get", {"id": flow["id"]}, ctx))["flow"]["rev"] == 3

    run = (await ok("flows.run.get", {"run_id": queued["run_id"]}, ctx))["run"]
    assert run["status"] == "done" and run["origin"] == "canvas" and run["rev"] == 3
    assert set(run["nodes"]) == {"n1", "n2", "n3"}
    sha = run["outputs"]["n3"]["images"][0]["sha256"]
    runs = (await ok("flows.runs", {"flow_id": flow["id"]}, ctx))["runs"]
    assert [r["run_id"] for r in runs] == [queued["run_id"]]
    assert runs[0]["image_count"] == 1 and runs[0]["preview"]["sha256"] == sha

    media = await ok("flows.media", {"sha256": sha, "max_side": 16}, ctx)
    assert (
        media["mime"] in ("image/png", "image/jpeg") and max(media["width"], media["height"]) == 16
    )

    png = base64.b64decode((await ok("flows.media", {"sha256": sha}, ctx))["data_b64"])
    imported = await ok("flows.import_png", {"data_b64": base64.b64encode(png).decode()}, ctx)
    assert imported["flow"]["id"] == flow["id"] and imported["image"]["sha256"] == sha

    upload = await ok(
        "flows.upload",
        {"name": "a.png", "mime": "image/png", "data_b64": base64.b64encode(png_bytes()).decode()},
        ctx,
    )
    assert upload["image"]["width"] == 32 and upload["image"]["mime"] == "image/png"

    assert await ok("flows.stop", {"run_id": queued["run_id"]}, ctx) == {"ok": True}
    assert await ok("flows.delete", {"id": flow["id"]}, ctx) == {"ok": True}
    assert (await ok("flows.list", {}, ctx))["flows"] == []
    assert (
        "flows.changed",
        {"flow_id": flow["id"], "rev": 3, "deleted": True},
    ) in recorder.events


async def test_not_found_conflict_and_invalid(rpc_service, ctx) -> None:
    res = await call("flows.get", {"id": "flw_nothere1"}, ctx)
    assert res.error.code == "flows.not_found"
    res = await call("flows.run.get", {"run_id": "run_nothere0001"}, ctx)
    assert res.error.code == "flows.not_found"
    res = await call("flows.media", {"sha256": "c" * 64}, ctx)
    assert res.error.code == "flows.not_found"

    flow = (await ok("flows.create", {"name": "X", "template": "text_to_image"}, ctx))["flow"]
    await ok("flows.save", {"flow": flow, "base_rev": 1}, ctx)
    res = await call("flows.save", {"flow": flow, "base_rev": 1}, ctx)
    assert res.error.code == "flows.conflict" and res.error.details["rev"] == 2

    broken = dict(flow, rev=2)
    broken["nodes"][0]["values"]["text"] = ""
    res = await call("flows.run", {"flow_id": flow["id"], "flow": broken}, ctx)
    assert res.error.code == "flows.invalid"
    assert res.error.details["node_errors"]["n1"][0]["code"] == "required"

    res = await call("flows.create", {}, ctx)
    assert res.error.code == "flows.invalid" and "name" in res.error.message
    res = await call("flows.save", {"flow": flow}, ctx)
    assert res.error.code == "flows.invalid" and res.error.details == {"node_errors": {}}
    res = await call("flows.create", {"name": "Y", "template": "nope"}, ctx)
    assert res.error.code == "flows.invalid"
    res = await call("flows.run", {"flow_id": flow["id"], "origin": "agent"}, ctx)
    assert res.error.code == "flows.invalid"
    res = await call("flows.upload", {"name": "x", "mime": "image/png", "data_b64": "@@"}, ctx)
    assert res.error.code == "flows.invalid"


async def test_validate_returns_node_errors_without_raising(rpc_service, ctx) -> None:
    graph = t2i_doc(model="acme/unknown")
    res = await ok("flows.validate", {"flow": graph}, ctx)
    assert res["ok"] is False
    assert res["node_errors"]["i"] == [
        {"code": "unknown_model", "message": "Unknown model 'acme/unknown'", "input": "model"}
    ]


async def test_agent_bound_connection_reads_but_cannot_write(rpc_service, ctx) -> None:
    flow = (await ok("flows.create", {"name": "A", "template": "text_to_image"}, ctx))["flow"]
    agent = _agent_ctx()
    for method in ("flows.nodes", "flows.models", "flows.list"):
        assert (await call(method, {}, agent)).ok, method
    assert (await call("flows.get", {"id": flow["id"]}, agent)).ok
    params = {
        "flows.create": {"name": "B"},
        "flows.save": {"flow": flow, "base_rev": 1},
        "flows.delete": {"id": flow["id"]},
        "flows.run": {"flow_id": flow["id"]},
        "flows.stop": {"run_id": "run_000000000001"},
        "flows.upload": {"name": "x", "mime": "image/png", "data_b64": ""},
        "flows.import_png": {"data_b64": ""},
    }
    for method in WRITES:
        res = await call(method, params[method], agent)
        assert res.ok is False and res.error.code == "flows.operator_required", method
    assert rpc_service.list_runs() == []


async def test_stop_through_rpc_cancels(rpc_service, ctx, images) -> None:
    images.gate = asyncio.Event()
    flow = (await ok("flows.create", {"name": "S", "template": "text_to_image"}, ctx))["flow"]
    queued = await ok("flows.run", {"flow_id": flow["id"], "origin": "cli"}, ctx)
    await asyncio.sleep(0.05)
    assert await ok("flows.stop", {"run_id": queued["run_id"]}, ctx) == {"ok": True}
    run = (await ok("flows.run.get", {"run_id": queued["run_id"]}, ctx))["run"]
    assert run["status"] == "cancelled" and run["origin"] == "cli"


async def test_list_and_get_say_up_front_whether_writes_are_allowed(rpc_service, ctx) -> None:
    from agentos.gateway.rpc_flows import READ_ONLY_REASON

    flow = (await ok("flows.create", {"name": "W"}, ctx))["flow"]
    listed = await ok("flows.list", {}, ctx)
    got = await ok("flows.get", {"id": flow["id"]}, ctx)
    for result in (listed, got):
        assert result["writable"] is True and result["read_only_reason"] is None
    agent = _agent_ctx()
    for method, params in (("flows.list", {}), ("flows.get", {"id": flow["id"]})):
        res = await call(method, params, agent)
        assert res.ok and res.payload["writable"] is False
        assert res.payload["read_only_reason"] == READ_ONLY_REASON
    denied = await call("flows.create", {"name": "X"}, agent)
    assert denied.error.code == "flows.operator_required"
    assert denied.error.message == (
        "This window is connected as an agent, so it can't change or run flows. "
        "Open Flows in the AgentOS app that manages this gateway."
    )
