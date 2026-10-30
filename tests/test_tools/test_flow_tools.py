"""``flow_list`` / ``flow_run``: Image Flows from an agent turn (docs/flows.md).

Fake adapters, no keys, no network: the service is the real FlowService with
the shared doubles from ``tests/test_flows/conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.artifacts import ArtifactStore
from agentos.flows import reset_flow_service, set_flow_service
from agentos.flows.models import ModelRegistry
from agentos.flows.service import FlowService
from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.tools.builtin import control
from agentos.tools.builtin import flows as flow_tools
from agentos.tools.policy_runtime import ToolSurfaceCapabilities, resolve_runtime_tool_surface
from agentos.tools.registry import get_default_registry
from agentos.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    ToolError,
    UnsupportedSurfaceError,
    current_tool_context,
)
from tests.test_flows.conftest import FakeImageAdapter, FakeTextAdapter, fake_auth, stored, t2i_doc


def _config(limit: float = 0.50, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        flows=SimpleNamespace(
            enabled=enabled, agent_cost_limit_usd=limit, max_concurrent_runs=1, cache_max_mb=500
        ),
        image_generation=SimpleNamespace(primary=None),
    )


@pytest.fixture(autouse=True)
def _clean_queue():
    reset_approval_queue()
    flow_tools._denials.clear()
    flow_tools._outstanding.clear()
    yield
    reset_approval_queue()
    flow_tools._denials.clear()
    flow_tools._outstanding.clear()


@pytest.fixture
def images() -> FakeImageAdapter:
    return FakeImageAdapter()


@pytest.fixture
async def make_service(tmp_path: Path, images: FakeImageAdapter, monkeypatch):
    created: list[FlowService] = []

    def build(*, limit: float = 0.50, enabled: bool = True, keys=("openrouter", "openai")):
        config = _config(limit, enabled)
        monkeypatch.setattr(control, "_gateway_config", config)
        svc = FlowService(
            config,
            root=tmp_path / "flows",
            image_adapter=images,
            text_adapter=FakeTextAdapter(),
            models=ModelRegistry(auth=fake_auth(*keys)),
        )
        svc.start()
        set_flow_service(svc)
        created.append(svc)
        return svc

    yield build
    for svc in created:
        await svc.close()
    reset_flow_service()


@pytest.fixture
def ctx(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolContext(
        caller_kind=CallerKind.WEB,
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:webchat:session-1",
    )
    token = current_tool_context.set(context)
    yield context
    current_tool_context.reset(token)


async def test_flow_list_reports_public_inputs_with_current_values(make_service, ctx) -> None:
    service = make_service()
    flow = await stored(service, t2i_doc(count=2))

    payload = json.loads(await flow_tools.flow_list())

    assert payload["count"] == 1
    row = payload["flows"][0]
    assert row["id"] == flow["id"] and row["name"] == "Test"
    assert {i["name"]: i["value"] for i in row["inputs"]} == {"prompt": "a lighthouse", "count": 2}
    assert payload["default_model"] == "openrouter/nano-banana-2"
    assert payload["agent_cost_limit_usd"] == 0.50


async def test_flow_run_under_the_limit_runs_and_publishes_card_then_images(
    make_service, ctx, images
) -> None:
    service = make_service()
    flow = await stored(service, t2i_doc())

    payload = json.loads(await flow_tools.flow_run(flow="Test", inputs={"prompt": "a red fox"}))

    assert payload["status"] == "done", payload
    assert payload["flow"] == {"id": flow["id"], "name": "Test"}
    assert payload["image_count"] == 1
    image = payload["images"][0]
    assert Path(image["path"]).is_file() and image["artifact_id"].startswith("art-")
    assert payload["cost_usd"] == pytest.approx(0.05)
    assert payload["nodes"] == {"done": 3, "total": 3} and payload["cached"] == 0
    # The model's prompt reached the image node.
    assert images.calls[0][1].prompt == "a red fox"
    # Card first, then the image, in the call's artifact list (the chat order).
    mimes = [a["mime"] for a in ctx.published_artifacts]
    assert mimes == [flow_tools.FLOW_RUN_MIME, "image/png"]
    assert [a["mime"] for a in payload["artifacts"]] == mimes
    assert all("download_url" not in a for a in payload["artifacts"])
    card_ref = ctx.published_artifacts[0]
    store = ArtifactStore(ctx.artifact_media_root)
    _ref, card_path = store.resolve_for_download(card_ref["id"], session_id="session-1")
    card = json.loads(card_path.read_text())
    assert card["run_id"] == payload["run_id"] and card["flow_name"] == "Test"
    assert card["status"] == "done" and card["progress"] == {"done": 3, "total": 3}
    assert card["images"][0]["name"] == Path(image["path"]).name
    assert card_ref["name"] == f"flow-run-{payload['run_id']}.json"
    # The run is the agent's: origin and session are recorded.
    run = service.get_run(payload["run_id"])
    assert run["origin"] == "agent"


async def test_second_run_reports_cached_nodes(make_service, ctx) -> None:
    service = make_service()
    await stored(service, t2i_doc())

    first = json.loads(await flow_tools.flow_run(flow="Test"))
    second = json.loads(await flow_tools.flow_run(flow="Test"))

    assert first["status"] == second["status"] == "done"
    assert second["cached"] >= 1
    assert second["cost_usd"] in (None, 0, 0.0)


async def test_over_the_limit_asks_for_approval_and_runs_once_approved(make_service, ctx) -> None:
    service = make_service(limit=0.01)
    flow = await stored(service, t2i_doc())

    first = json.loads(await flow_tools.flow_run(flow=flow["id"]))

    assert first["status"] == "approval_required"
    assert first["estimate_usd"] == pytest.approx(0.0672)
    assert "above the $0.010" in first["warning"]
    assert service.list_runs() == []  # nothing queued, nothing spent
    entry = get_approval_queue().get(first["approval_id"])
    assert entry.namespace == "flow"
    assert entry.params["toolName"] == "flow_run"
    assert entry.params["args"] == {"flow_id": flow["id"], "rev": flow["rev"], "inputs": {}}

    get_approval_queue().resolve(first["approval_id"], True)
    retried = json.loads(
        await flow_tools.flow_run(flow=flow["id"], approval_id=first["approval_id"])
    )

    assert retried["status"] == "done", retried
    assert get_approval_queue().get(first["approval_id"]).consumed is True
    assert len(service.list_runs()) == 1


async def test_denied_approval_is_a_clear_refusal(make_service, ctx) -> None:
    service = make_service(limit=0.01)
    flow = await stored(service, t2i_doc())
    first = json.loads(await flow_tools.flow_run(flow=flow["id"]))
    get_approval_queue().resolve(first["approval_id"], False)

    denied = json.loads(
        await flow_tools.flow_run(flow=flow["id"], approval_id=first["approval_id"])
    )

    assert denied["status"] == "approval_denied"
    assert "declined" in denied["message"]
    # The model is told plainly not to retry or work around the refusal.
    assert "Do not run this flow again" in denied["message"]
    assert "fewer images" in denied["message"] and "image_generate" in denied["message"]
    assert service.list_runs() == []


async def test_approval_is_bound_to_the_inputs(make_service, ctx) -> None:
    service = make_service(limit=0.01)
    flow = await stored(service, t2i_doc())
    first = json.loads(await flow_tools.flow_run(flow=flow["id"]))
    get_approval_queue().resolve(first["approval_id"], True)

    with pytest.raises(ToolError, match="does not match"):
        await flow_tools.flow_run(
            flow=flow["id"], inputs={"count": 4}, approval_id=first["approval_id"]
        )
    assert service.list_runs() == []


async def test_unattended_run_over_the_limit_refuses_without_queuing(make_service, ctx) -> None:
    service = make_service(limit=0.01)
    flow = await stored(service, t2i_doc())
    ctx.interaction_mode = InteractionMode.UNATTENDED

    with pytest.raises(UnsupportedSurfaceError, match="unattended"):
        await flow_tools.flow_run(flow=flow["id"])
    assert get_approval_queue().list_pending("flow") == []
    assert service.list_runs() == []


async def test_auto_approve_policy_runs_without_a_prompt(make_service, ctx) -> None:
    service = make_service(limit=0.01)
    flow = await stored(service, t2i_doc())
    get_approval_queue().set_settings(mode="auto-approve")

    payload = json.loads(await flow_tools.flow_run(flow=flow["id"]))

    assert payload["status"] == "done"


async def test_no_image_model_is_a_clear_error_and_hides_the_tools(make_service, ctx) -> None:
    service = make_service(keys=())
    await stored(service, t2i_doc())

    assert flow_tools.flows_available() is False
    with pytest.raises(ToolError, match="No image model is available.*API_KEY is not set"):
        await flow_tools.flow_run(flow="Test")
    with pytest.raises(ToolError, match="No image model"):
        await flow_tools.flow_list()

    surface = resolve_runtime_tool_surface(
        ToolContext(), capabilities=ToolSurfaceCapabilities(flows=False)
    )
    assert {"flow_list", "flow_run"} <= surface.denied_tools


async def test_disabled_flows_are_unavailable(make_service, ctx) -> None:
    make_service(enabled=False)

    assert flow_tools.flows_available() is False
    with pytest.raises(ToolError, match="disabled"):
        await flow_tools.flow_list()


async def test_unknown_flow_and_bad_inputs_are_tool_errors(make_service, ctx) -> None:
    service = make_service()
    await stored(service, t2i_doc())

    with pytest.raises(ToolError, match="flows.not_found"):
        await flow_tools.flow_run(flow="Nope")
    with pytest.raises(ToolError, match="not a public input"):
        await flow_tools.flow_run(flow="Test", inputs={"seed": "1"})
    with pytest.raises(ToolError, match="inputs.prompt must be"):
        await flow_tools.flow_run(flow="Test", inputs={"prompt": ["x"]})


async def test_a_stopped_turn_stops_the_run(make_service, ctx, images) -> None:
    service = make_service()
    flow = await stored(service, t2i_doc())
    images.gate = asyncio.Event()

    task = asyncio.create_task(flow_tools.flow_run(flow=flow["id"]))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if images.calls:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    runs = service.list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "cancelled"


async def test_wait_timeout_returns_running_with_a_note(make_service, ctx, images) -> None:
    service = make_service()
    flow = await stored(service, t2i_doc())
    images.gate = asyncio.Event()

    payload = json.loads(await flow_tools.flow_run(flow=flow["id"], wait_seconds=0))

    assert payload["status"] == "running"
    assert "keeps running" in payload["note"]
    images.gate.set()
    await service.wait_run(payload["run_id"], timeout=5)


def test_tools_are_registered_deferred_and_flow_run_may_need_approval() -> None:
    registry = get_default_registry()
    flow_list = registry.get("flow_list")
    flow_run = registry.get("flow_run")

    assert flow_list is not None and flow_run is not None
    assert flow_list.spec.read_only and flow_list.spec.defer_loading
    assert flow_run.spec.defer_loading and flow_run.spec.requires_approval
    assert "approval_id" in flow_run.spec.parameters
    assert flow_run.spec.required == ["flow"]
    # The description steers one-off pictures to image_generate.
    assert "image_generate" in flow_run.spec.description


def test_direct_http_tool_calls_never_run_a_paid_flow() -> None:
    from agentos.gateway.tools_invoke import is_hard_denied

    assert is_hard_denied("flow_run")
    assert not is_hard_denied("flow_list")


def test_card_payload_prefers_saved_images_with_their_file_names() -> None:
    record: dict[str, Any] = {
        "run_id": "run_abc123",
        "flow_id": "flw_x1y2",
        "status": "done",
        "graph": {"nodes": [{"id": "i", "type": "image"}, {"id": "s", "type": "save"}]},
        "nodes": {"i": {"state": "cached"}, "s": {"state": "done"}},
        "outputs": {
            "i": {"images": [{"sha256": "a" * 64, "width": 8, "height": 8}]},
            "s": {
                "images": [{"sha256": "b" * 64, "width": 8, "height": 8}],
                "files": ["/tmp/out/Test_run_1.png"],
            },
        },
        "est_usd": 0.04,
        "cost_usd": 0.0,
    }

    card = flow_tools.card_payload(record, "Test")

    assert card["kind"] == "flow_run" and card["version"] == 1
    assert card["progress"] == {"done": 2, "total": 2} and card["cached"] == 1
    assert [i["sha256"] for i in card["images"]] == ["b" * 64]
    assert card["images"][0]["name"] == "Test_run_1.png"


async def _deny_first(service, flow) -> dict[str, Any]:
    """An over-the-limit run (count 2) the user declines; returns the denial result."""
    first = json.loads(await flow_tools.flow_run(flow=flow["id"], inputs={"count": 2}))
    assert first["status"] == "approval_required"
    get_approval_queue().resolve(first["approval_id"], False)
    return json.loads(
        await flow_tools.flow_run(
            flow=flow["id"], inputs={"count": 2}, approval_id=first["approval_id"]
        )
    )


async def test_a_denial_sticks_even_for_a_cheaper_variant(make_service, ctx) -> None:
    # count 2 is ~$0.13, over the $0.10 limit; count 1 (~$0.07) is under it.
    service = make_service(limit=0.10)
    flow = await stored(service, t2i_doc())
    assert (await _deny_first(service, flow))["status"] == "approval_denied"

    cheaper = json.loads(await flow_tools.flow_run(flow=flow["id"], inputs={"count": 1}))

    assert cheaper["status"] == "approval_required"
    assert "You declined a run" in cheaper["warning"]
    assert service.list_runs() == []  # the agent could not work around the "no"

    get_approval_queue().resolve(cheaper["approval_id"], True)
    ran = json.loads(
        await flow_tools.flow_run(
            flow=flow["id"], inputs={"count": 1}, approval_id=cheaper["approval_id"]
        )
    )
    assert ran["status"] == "done"
    # A fresh yes lifts it: the next cheap run needs no prompt.
    again = json.loads(await flow_tools.flow_run(flow=flow["id"], inputs={"count": 1}))
    assert again["status"] == "done"


async def test_a_denial_given_after_the_engine_stopped_waiting_still_counts(
    make_service, ctx
) -> None:
    service = make_service(limit=0.10)
    flow = await stored(service, t2i_doc())
    first = json.loads(await flow_tools.flow_run(flow=flow["id"], inputs={"count": 2}))
    # Nobody retried with the approval_id; the user says no later.
    get_approval_queue().resolve(first["approval_id"], False)

    cheaper = json.loads(await flow_tools.flow_run(flow=flow["id"], inputs={"count": 1}))

    assert cheaper["status"] == "approval_required"
    assert service.list_runs() == []


async def test_a_denial_is_per_session_and_per_flow_and_expires(
    make_service, ctx, monkeypatch
) -> None:
    service = make_service(limit=0.10)
    flow = await stored(service, t2i_doc())
    other = await stored(service, {**t2i_doc(), "name": "Other"})
    assert (await _deny_first(service, flow))["status"] == "approval_denied"

    # Another flow in this session is not blocked by this flow's "no".
    assert json.loads(await flow_tools.flow_run(flow=other["id"]))["status"] == "done"
    # Another session is not affected.
    token = current_tool_context.set(ToolContext(session_key="agent:main:webchat:other"))
    try:
        assert json.loads(await flow_tools.flow_run(flow=flow["id"]))["status"] == "done"
    finally:
        current_tool_context.reset(token)
    # After the window, a cheap run goes through again.
    real = flow_tools.time.monotonic
    monkeypatch.setattr(
        flow_tools.time, "monotonic", lambda: real() + flow_tools.DENIAL_WINDOW_S + 1
    )
    assert json.loads(await flow_tools.flow_run(flow=flow["id"]))["status"] == "done"


async def test_a_policy_denial_also_sticks(make_service, ctx) -> None:
    service = make_service(limit=0.10)
    flow = await stored(service, t2i_doc())
    get_approval_queue().set_settings(mode="auto-deny")
    denied = json.loads(await flow_tools.flow_run(flow=flow["id"], inputs={"count": 2}))
    assert denied["status"] == "approval_denied"
    get_approval_queue().set_settings(mode="prompt")

    cheaper = json.loads(await flow_tools.flow_run(flow=flow["id"], inputs={"count": 1}))

    assert cheaper["status"] == "approval_required"


async def test_a_spend_approval_never_elevates_or_remembers(make_service, ctx) -> None:
    service = make_service(limit=0.01)
    flow = await stored(service, t2i_doc())
    first = json.loads(await flow_tools.flow_run(flow=flow["id"]))
    queue = get_approval_queue()

    # Whatever a surface sends ("Stop Asking", "Always Allow"), it is one yes.
    queue.resolve(first["approval_id"], True, allow_always=True, elevated_mode="bypass")

    assert queue.get_elevated_mode(ctx.session_key) is None
    assert "elevatedMode" not in queue.get(first["approval_id"]).params
    retried = json.loads(
        await flow_tools.flow_run(flow=flow["id"], approval_id=first["approval_id"])
    )
    assert retried["status"] == "done"
    # The next run asks again: nothing was remembered.
    fresh = await flow_tools.flow_run(flow=flow["id"], inputs={"prompt": "a new prompt"})
    assert json.loads(fresh)["status"] == "approval_required"


def test_price_text_matches_the_desktop_shapes() -> None:
    assert flow_tools.price_text({"kind": "free", "approximate": False}) == "free"
    assert flow_tools.price_text({"kind": "usd", "usd": 0.0672, "approximate": True}) == "~$0.067"
    assert (
        flow_tools.price_text({"kind": "range", "min": 0.0034, "max": 0.1, "approximate": True})
        == "~$0.0034–$0.10"
    )
    assert flow_tools.price_text({"kind": "unknown", "approximate": True}) == "unknown"


def test_flow_tools_are_found_by_image_words() -> None:
    from agentos.tools.deferred import DeferredToolSurface

    registry = get_default_registry()
    deferred = {name: registry.get(name) for name in ("flow_run", "flow_list", "tts", "browser")}
    surface = DeferredToolSurface(
        session_key="s",
        registry=registry,
        ctx=ToolContext(),
        deferred={k: v for k, v in deferred.items() if v is not None},
        loaded=[],
    )
    for query in ("draw a picture", "generate image", "photo", "illustration"):
        names = [rt.spec.name for rt in surface.search(query, limit=2)]
        assert "flow_run" in names, (query, names)


def _prompt(tools: list[str]) -> str:
    from agentos.identity.prompt import assemble_system_prompt
    from agentos.identity.types import AgentIdentity, AgentProfile

    return assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full", identity=AgentIdentity(name="Bin")),
        tools=tools,
    )


def test_prompt_points_image_requests_at_flows_when_image_generate_is_off() -> None:
    base = ["exec_command", "read_file"]

    with_flows = _prompt([*base, "flow_list", "flow_run"])
    assert "you can create images by running one of the user's saved Image Flows" in with_flows
    assert "create one in the Flows editor" in with_flows
    assert "Image generation is not available in this session" not in with_flows

    without = _prompt(base)
    assert "Image generation is not available in this session" in without
    assert "flow_run" not in without

    both = _prompt([*base, "image_generate", "flow_list", "flow_run"])
    assert "you MUST call the `image_generate` tool" in both
    assert "use `flow_run`" in both
