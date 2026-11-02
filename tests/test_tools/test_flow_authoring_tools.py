"""``flow_get`` / ``flow_create`` / ``flow_update``: agents build Image Flows.

The service is the real FlowService (spec parsing, validation, auto-layout,
revisions) with fake image adapters: no network, no keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.artifacts import ArtifactStore
from agentos.flows import reset_flow_service, set_flow_service
from agentos.flows.models import ModelRegistry
from agentos.flows.service import FlowService
from agentos.tools.builtin import control
from agentos.tools.builtin import flow_authoring as authoring
from agentos.tools.registry import get_default_registry
from agentos.tools.types import CallerKind, ToolContext, ToolError, current_tool_context
from tests.test_flows.conftest import FakeImageAdapter, FakeTextAdapter, fake_auth, png_bytes

SESSION = "agent:main:webchat:session-1"


@pytest.fixture
async def service(tmp_path: Path, monkeypatch):
    config = SimpleNamespace(
        flows=SimpleNamespace(
            enabled=True, agent_cost_limit_usd=0.5, max_concurrent_runs=1, cache_max_mb=500
        ),
        image_generation=SimpleNamespace(primary=None),
    )
    monkeypatch.setattr(control, "_gateway_config", config)
    svc = FlowService(
        config,
        root=tmp_path / "flows",
        image_adapter=FakeImageAdapter(),
        text_adapter=FakeTextAdapter(),
        models=ModelRegistry(auth=fake_auth("openrouter", "openai")),
    )
    svc.start()
    set_flow_service(svc)
    yield svc
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
        session_key=SESSION,
    )
    token = current_tool_context.set(context)
    yield context
    current_tool_context.reset(token)


class FakeUploads:
    """The upload store's ``get`` (``POST /api/v1/files/upload`` → ``file_uuid``)."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, dict[str, Any]]] = {}

    async def get(self, file_uuid: str) -> tuple[bytes, dict[str, Any]]:
        return self.files[file_uuid]


@pytest.fixture
async def chat(ctx):
    """A real session the way ``chat.send`` writes it: transcript rows + material store."""
    from agentos.session.manager import SessionManager
    from agentos.session.storage import SessionStorage
    from agentos.tools.builtin import sessions

    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session, _ = await manager.get_or_create(SESSION)
    ctx.artifact_session_id = session.session_id
    previous = sessions._session_manager
    sessions.set_session_manager(manager)

    uploads = FakeUploads()

    async def send(text: str, *images: tuple[str, bytes], staged: bool = False) -> None:
        """One user message with *images*, through the gateway's ingest and persist."""
        import base64

        from agentos.gateway.attachment_ingest import ingest_attachments
        from agentos.gateway.transcripts import build_transcript_attachment_envelope

        raw = []
        for name, data in images:
            if staged:
                uploads.files[name] = (data, {"mime": "image/png", "name": name})
                raw.append({"file_uuid": name, "type": "image/png", "name": name})
            else:
                raw.append(
                    {"type": "image/png", "name": name, "data": base64.b64encode(data).decode()}
                )
        media_root = Path(ctx.artifact_media_root)
        ingested = await ingest_attachments(
            text,
            raw,
            store=uploads,
            failure_mode="raise",
            material_root=media_root,
            session_id=session.session_id,
        )
        content, _writes = build_transcript_attachment_envelope(
            text=ingested.text,
            attachments=ingested.attachments,
            session_id=session.session_id,
            media_root=media_root,
            persist_enabled=True,
        )
        await manager.append_message(SESSION, role="user", content=content)

    yield send
    sessions.set_session_manager(previous)
    await storage.close()


def _t2i_spec(**image_values: Any) -> dict[str, Any]:
    values = {"model": "openrouter/nano-banana-2", "count": 1, **image_values}
    return {
        "name": "Fox poster",
        "nodes": [
            {"id": "prompt", "type": "text", "values": {"text": "a red fox in snow"}},
            {"id": "gen", "type": "image", "values": values},
            {"id": "out", "type": "save"},
        ],
        "links": [["prompt.text", "gen.prompt"], ["gen.image", "out.images"]],
        "inputs": {"prompt": "prompt.text"},
    }


def _edit_spec(image: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "Infographic",
        "nodes": [
            {"id": "ref", "type": "load_image", "title": "Style", "values": {"image": image}},
            {"id": "content", "type": "text", "values": {"text": "AgentOS in 3 steps"}},
            {"id": "gen", "type": "image", "values": {"model": "openai/gpt-image-1"}},
            {"id": "out", "type": "save"},
        ],
        "links": [
            ["ref.image", "gen.images"],
            ["content.text", "gen.prompt"],
            ["gen.image", "out.images"],
        ],
        "inputs": {"content": "content.text"},
    }


def _cards(ctx: ToolContext) -> list[dict[str, Any]]:
    store = ArtifactStore(ctx.artifact_media_root)
    out = []
    for artifact in ctx.published_artifacts:
        if artifact["mime"] == authoring.FLOW_CHANGE_MIME:
            _ref, path = store.resolve_for_download(
                artifact["id"], session_id=ctx.artifact_session_id
            )
            out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


# ── flow_get ───────────────────────────────────────────────────────────────


async def test_flow_get_without_a_flow_is_the_catalog(service, ctx) -> None:
    payload = json.loads(await authoring.flow_get())

    types = {node["type"] for node in payload["nodes"]}
    assert {"text", "load_image", "image", "save", "prompt_writer"} <= types
    image = next(node for node in payload["nodes"] if node["type"] == "image")
    assert {"prompt", "images", "model", "count"} <= {i["name"] for i in image["inputs"]}
    models = {m["id"]: m for m in payload["models"]}
    assert models["openrouter/nano-banana-2"]["available"] is True
    assert payload["default_model"]
    assert "spec" not in payload  # the help must not look like a spec to paste
    assert "attachment" in payload["spec_help"]["load_image"]
    assert "inside that node's 'values'" in payload["spec_help"]["how"]


async def test_flow_get_with_a_flow_returns_its_spec(service, ctx) -> None:
    created = json.loads(await authoring.flow_create(spec=_t2i_spec()))

    payload = json.loads(await authoring.flow_get(flow="Fox poster"))

    assert payload["flow_id"] == created["flow_id"]
    assert payload["created_by"] == "agent"
    assert ["prompt.text", "gen.prompt"] in payload["spec"]["links"]
    assert {n["id"] for n in payload["spec"]["nodes"]} == {"prompt", "gen", "out"}
    assert all("pos" not in n for n in payload["spec"]["nodes"])
    (public,) = payload["public_inputs"]
    assert (public["name"], public["value"]) == ("prompt", "a red fox in snow")


# ── flow_create ────────────────────────────────────────────────────────────


async def test_flow_create_saves_publishes_a_card_and_says_authoring_is_free(service, ctx) -> None:
    payload = json.loads(await authoring.flow_create(spec=_t2i_spec()))

    assert payload["status"] == "ok" and payload["saved"] is True
    assert payload["change"] == "created" and payload["node_count"] == 3
    assert payload["estimate_per_run"].startswith("~$")
    assert "free" in payload["note"] and "flow_run" in payload["note"]
    stored = service.get_flow(payload["flow_id"])
    assert stored["name"] == "Fox poster" and stored["created_by"] == "agent"
    # Laid out by the engine: every node has a position.
    assert all(node.get("pos") for node in stored["nodes"])
    (revision,) = service.revisions(payload["flow_id"])["revisions"]
    assert (revision["author"], revision["session_key"]) == ("agent", SESSION)
    # Nothing ran.
    assert service.list_runs() == []
    (card,) = _cards(ctx)
    assert card["kind"] == "flow_change" and card["change"] == "created"
    assert card["flow_id"] == payload["flow_id"] and card["rev"] == payload["rev"]
    assert card["summary"] == payload["summary"] == "created with 3 nodes and 2 links"
    assert card["estimate"]["kind"] in ("usd", "range")


async def test_an_invalid_spec_is_not_saved_and_names_the_problems(service, ctx) -> None:
    spec = _t2i_spec()
    spec["links"] = [["gen.image", "out.images"]]  # the image node has no prompt
    spec["nodes"][0] = {"id": "prompt", "type": "no_such_node"}

    payload = json.loads(await authoring.flow_create(spec=spec))

    assert payload["status"] == "invalid" and payload["saved"] is False
    assert "Nothing was saved" in payload["message"]
    nodes = {p["node"] for p in payload["problems"]}
    assert "prompt" in nodes
    assert all(p["message"] for p in payload["problems"])
    assert service.list_flows()["flows"] == []
    assert _cards(ctx) == []


async def test_a_spec_that_is_not_an_object_is_refused(service, ctx) -> None:
    with pytest.raises(ToolError, match="spec"):
        await authoring.flow_create(spec="not json")  # type: ignore[arg-type]
    payload = json.loads(await authoring.flow_create(spec={"name": "x"}))
    assert payload["status"] == "invalid" and payload["problems"]


# ── load_image values ──────────────────────────────────────────────────────


def _ref_pixels(service, payload: dict[str, Any]) -> bytes:
    doc = service.get_flow(payload["flow_id"])
    ref = next(n for n in doc["nodes"] if n["id"] == "ref")["values"]["image"]
    data, _ = service.media.read(ref["sha256"])
    return data


async def test_the_image_the_user_just_attached_is_latest(service, ctx, chat) -> None:
    # An 801 KB PNG from the desktop composer goes inline (base64 on the frame).
    await chat("an older one", ("old.png", png_bytes("navy")))
    await chat("make a flow with this", ("brand.png", png_bytes("gold", (640, 480))))

    payload = json.loads(await authoring.flow_create(spec=_edit_spec({"attachment": "latest"})))

    assert payload["status"] == "ok", payload
    assert _ref_pixels(service, payload) == png_bytes("gold", (640, 480))


async def test_a_staged_upload_resolves_too(service, ctx, chat) -> None:
    # Images over 2 MB are POSTed first and sent as a file_uuid: their bytes land
    # in the transcript material store and the row keeps a sha256 reference.
    await chat("big one", ("poster.png", png_bytes("plum")), staged=True)

    payload = json.loads(await authoring.flow_create(spec=_edit_spec({"attachment": "latest"})))

    assert payload["status"] == "ok", payload
    assert _ref_pixels(service, payload) == png_bytes("plum")


async def test_older_chat_images_by_number_name_or_sha(service, ctx, chat) -> None:
    import hashlib

    await chat("first", ("navy.png", png_bytes("navy")))
    await chat("second", ("gold.png", png_bytes("gold")))
    sha = hashlib.sha256(png_bytes("navy")).hexdigest()

    for ref in ("2", "navy.png", sha):
        payload = json.loads(await authoring.flow_create(spec=_edit_spec({"attachment": ref})))
        assert payload["status"] == "ok", (ref, payload)
        assert _ref_pixels(service, payload) == png_bytes("navy")


async def test_an_image_artifact_of_this_chat_can_be_the_reference(service, ctx) -> None:
    store = ArtifactStore(ctx.artifact_media_root)
    ref = store.publish_bytes(
        png_bytes("plum"),
        session_id=ctx.artifact_session_id,
        session_key=SESSION,
        name="earlier.png",
        mime="image/png",
        source="flow_run",
    )

    payload = json.loads(await authoring.flow_create(spec=_edit_spec({"attachment": ref.id})))

    assert payload["status"] == "ok"


def _problem(payload: dict[str, Any]) -> str:
    assert payload["status"] == "invalid" and payload["saved"] is False, payload
    (problem,) = [p for p in payload["problems"] if p["node"] == "ref"]
    return str(problem["message"])


async def test_unknown_attachments_get_a_clear_error(service, ctx, chat) -> None:
    async def attempt(image: dict[str, Any]) -> str:
        return _problem(json.loads(await authoring.flow_create(spec=_edit_spec(image))))

    missing = await attempt({"attachment": "latest"})
    assert "has not sent an image" in missing and "Do not search the disk" in missing
    await chat("here", ("navy.png", png_bytes("navy")))
    listed = await attempt({"attachment": "5"})
    assert "newest first: 1) navy.png" in listed
    assert "No image artifact" in await attempt({"attachment": "art-doesnotexist"})
    assert service.list_flows()["flows"] == []


async def test_a_workspace_image_path_is_read(service, ctx) -> None:
    (Path(ctx.workspace_dir) / "brand").mkdir()
    (Path(ctx.workspace_dir) / "brand" / "style.png").write_bytes(png_bytes("olive"))

    payload = json.loads(await authoring.flow_create(spec=_edit_spec({"path": "brand/style.png"})))

    assert payload["status"] == "ok"


async def test_paths_outside_the_workspace_and_non_images_are_refused(
    service, ctx, tmp_path: Path
) -> None:
    outside = tmp_path / "secret.png"
    outside.write_bytes(png_bytes("black"))
    (Path(ctx.workspace_dir) / "notes.txt").write_text("hello")
    (Path(ctx.workspace_dir) / "link.png").symlink_to(outside)

    async def attempt(image: dict[str, Any]) -> str:
        return _problem(json.loads(await authoring.flow_create(spec=_edit_spec(image))))

    for raw in (str(outside), "../secret.png", "link.png"):
        assert "outside your workspace" in await attempt({"path": raw})
    assert "not a PNG" in await attempt({"path": "notes.txt"})
    assert "No file" in await attempt({"path": "missing.png"})
    payload = json.loads(await authoring.flow_create(spec=_edit_spec({"url": "https://x"})))
    assert payload["status"] == "invalid"
    assert service.list_flows()["flows"] == []


# ── flow_update ────────────────────────────────────────────────────────────


async def test_flow_update_edits_the_named_flow_and_summarises_the_change(service, ctx) -> None:
    created = json.loads(await authoring.flow_create(spec=_t2i_spec()))
    read = json.loads(await authoring.flow_get(flow=created["flow_id"]))
    spec = read["spec"]
    spec["nodes"][0]["values"]["text"] = "a white owl"
    spec["nodes"].append({"id": "writer", "type": "prompt_writer", "values": {"style": "anime"}})
    spec["links"] = [
        ["prompt.text", "writer.idea"],
        ["writer.text", "gen.prompt"],
        ["gen.image", "out.images"],
    ]

    payload = json.loads(
        await authoring.flow_update(flow="Fox poster", spec=spec, base_rev=read["rev"])
    )

    assert payload["status"] == "ok" and payload["change"] == "updated"
    assert payload["flow_id"] == created["flow_id"]
    assert payload["rev"] == created["rev"] + 1
    assert "added writer (prompt_writer)" in payload["summary"]
    assert "changed prompt" in payload["summary"]
    revisions = service.revisions(created["flow_id"])["revisions"]
    assert [r["author"] for r in revisions] == ["agent", "agent"]
    # Nodes that existed keep their place.
    stored = {n["id"]: n for n in service.get_flow(created["flow_id"])["nodes"]}
    assert stored["writer"]["pos"]
    cards = _cards(ctx)
    assert [c["change"] for c in cards] == ["created", "updated"]
    assert cards[-1]["summary"] == payload["summary"]


async def test_flow_update_of_an_unknown_flow_or_with_errors_saves_nothing(service, ctx) -> None:
    with pytest.raises(ToolError, match="flows.not_found"):
        await authoring.flow_update(flow="Nope", spec=_t2i_spec(), base_rev=1)
    created = json.loads(await authoring.flow_create(spec=_t2i_spec()))
    broken = _t2i_spec()
    broken["links"] = []

    payload = json.loads(
        await authoring.flow_update(flow=created["flow_id"], spec=broken, base_rev=created["rev"])
    )

    assert payload["status"] == "invalid" and payload["saved"] is False
    assert service.get_flow(created["flow_id"])["rev"] == created["rev"]


# ── registration and surfaces ──────────────────────────────────────────────


def test_tools_are_deferred_and_never_delete() -> None:
    registry = get_default_registry()
    for name in ("flow_get", "flow_create", "flow_update"):
        registered = registry.get(name)
        assert registered is not None and registered.spec.defer_loading
        assert not registered.spec.requires_approval
    assert registry.get("flow_get").spec.read_only
    assert not any("delete" in name for name in registry.list_names() if name.startswith("flow"))
    create = registry.get("flow_create").spec.description
    assert "free" in create and "flow_run" in create and "flow_update" in create
    assert "no way to delete" in registry.get("flow_update").spec.description


def test_direct_http_tool_calls_never_write_flows() -> None:
    from agentos.gateway.tools_invoke import is_hard_denied

    assert is_hard_denied("flow_create") and is_hard_denied("flow_update")
    assert not is_hard_denied("flow_get")


def test_the_tools_hide_with_the_other_flow_tools() -> None:
    from agentos.tools.policy_runtime import (
        ToolSurfaceCapabilities,
        resolve_runtime_tool_surface,
    )

    surface = resolve_runtime_tool_surface(
        ToolContext(), capabilities=ToolSurfaceCapabilities(flows=False)
    )
    assert {"flow_get", "flow_create", "flow_update"} <= surface.denied_tools


def test_authoring_tools_are_found_by_workflow_words() -> None:
    from agentos.tools.deferred import DeferredToolSurface

    registry = get_default_registry()
    names = ("flow_get", "flow_create", "flow_update", "flow_list", "flow_run", "tts", "browser")
    surface = DeferredToolSurface(
        session_key="s",
        registry=registry,
        ctx=ToolContext(),
        deferred={n: registry.get(n) for n in names if registry.get(n) is not None},
        loaded=[],
    )
    expectations = {
        "create flow": "flow_create",
        "build workflow": "flow_create",
        "pipeline": "flow_create",
        "automate images": "flow_create",
        "edit flow": "flow_update",
        "add node": "flow_update",
    }
    for query, expected in expectations.items():
        found = [rt.spec.name for rt in surface.search(query, limit=3)]
        assert expected in found, (query, found)


def test_prompt_describes_authoring_when_the_tools_are_there() -> None:
    from agentos.identity.prompt import assemble_system_prompt
    from agentos.identity.types import AgentIdentity, AgentProfile

    def prompt(tools: list[str]) -> str:
        return assemble_system_prompt(
            AgentProfile(agent_id="main", prompt_mode="full", identity=AgentIdentity(name="B")),
            tools=tools,
        )

    flows = ["flow_list", "flow_run", "flow_get", "flow_create", "flow_update"]
    text = prompt(["read_file", *flows])
    assert "## Image Flows" in text
    assert "Authoring is free and needs no approval" in text
    assert "Never delete a flow" in text
    assert "only if the user asked for the image now" in text
    assert "build one with `flow_create`" in text
    assert "pass its `rev` as `base_rev`" in text and "never from memory" in text
    assert '`{"attachment": "latest"}`' in text and "Never search the disk" in text
    assert "pass `overrides` to `flow_run` instead of editing the saved flow" in text
    assert "## Image Flows" not in prompt(["read_file", "flow_list", "flow_run"])


async def test_the_skill_md_worked_example_is_a_valid_spec(tmp_path: Path, ctx, chat, monkeypatch):
    """The infographic example the bundled agentos skill teaches must save as is."""
    import re

    skill = Path(__file__).resolve().parents[2] / "src/agentos/skills/bundled/agentos/SKILL.md"
    text = skill.read_text(encoding="utf-8")
    section = text[text.index("Worked example:") :]
    spec = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))
    config = SimpleNamespace(
        flows=SimpleNamespace(
            enabled=True, agent_cost_limit_usd=0.5, max_concurrent_runs=1, cache_max_mb=500
        ),
        image_generation=SimpleNamespace(primary=None),
    )
    monkeypatch.setattr(control, "_gateway_config", config)
    svc = FlowService(
        config,
        root=tmp_path / "flows-example",
        image_adapter=FakeImageAdapter(),
        text_adapter=FakeTextAdapter(),
    )
    svc.models = ModelRegistry(svc.default_entries, auth=fake_auth("surplus"))
    svc.start()
    set_flow_service(svc)
    try:
        await chat("in this style", ("style.png", png_bytes("teal")))
        payload = json.loads(await authoring.flow_create(spec=spec))
        assert payload["status"] == "ok", payload
        assert "warnings" not in payload
        assert payload["node_count"] == 4
    finally:
        await svc.close()
        reset_flow_service()


async def test_a_model_without_a_key_saves_with_a_warning(service, ctx) -> None:
    service.models = ModelRegistry(auth=fake_auth("openrouter"))  # no OpenAI key
    spec = _t2i_spec(model="openai/gpt-image-1")

    payload = json.loads(await authoring.flow_create(spec=spec))

    assert payload["status"] == "ok" and payload["saved"] is True, payload
    assert payload["warnings"][0]["node"] == "gen"
    assert "cannot run yet" in payload["note_warnings"]
    assert "OPENAI_API_KEY" in payload["note_warnings"]


async def test_flow_update_needs_base_rev(service, ctx) -> None:
    created = json.loads(await authoring.flow_create(spec=_t2i_spec()))

    with pytest.raises(ToolError, match="base_rev is required"):
        await authoring.flow_update(flow=created["flow_id"], spec=_t2i_spec())
    assert "base_rev" in get_default_registry().get("flow_update").spec.required


async def test_a_stale_update_never_undoes_the_users_restore(service, ctx) -> None:
    created = json.loads(await authoring.flow_create(spec=_t2i_spec()))
    read = json.loads(await authoring.flow_get(flow=created["flow_id"]))
    # The user edits (or restores) the flow in the editor after the agent read it.
    doc = service.get_flow(created["flow_id"])
    doc["name"] = "Fox poster (user)"
    await service.save_flow(doc, doc["rev"])
    stale = read["spec"]
    stale["nodes"][0]["values"]["text"] = "from memory"

    payload = json.loads(
        await authoring.flow_update(flow=created["flow_id"], spec=stale, base_rev=read["rev"])
    )

    assert payload["status"] == "conflict" and payload["saved"] is False
    assert payload["current_rev"] == read["rev"] + 1
    assert "changed since you read it" in payload["message"]
    assert "Call flow_get and re-apply only the requested change" in payload["message"]
    assert service.get_flow(created["flow_id"])["name"] == "Fox poster (user)"


def test_descriptions_forbid_disk_search_and_explain_variation() -> None:
    registry = get_default_registry()
    create = registry.get("flow_create").spec
    assert "never search the disk" in create.parameters["spec"]["description"]
    update = registry.get("flow_update").spec.description
    assert "flow_get(flow) right before" in update and "never from memory" in update
    assert "overrides" in update
    help_text = json.dumps(authoring._SPEC_HELP)
    assert "Never search the disk" in help_text
    assert "does not make a new image look like an earlier one" in help_text


async def test_the_example_spec_flow_get_shows_validates_and_saves(
    tmp_path: Path, ctx, chat, monkeypatch
) -> None:
    config = SimpleNamespace(
        flows=SimpleNamespace(
            enabled=True, agent_cost_limit_usd=0.5, max_concurrent_runs=1, cache_max_mb=500
        ),
        image_generation=SimpleNamespace(primary=None),
    )
    monkeypatch.setattr(control, "_gateway_config", config)
    svc = FlowService(
        config,
        root=tmp_path / "flows-help",
        image_adapter=FakeImageAdapter(),
        text_adapter=FakeTextAdapter(),
    )
    svc.models = ModelRegistry(svc.default_entries, auth=fake_auth("surplus"))
    svc.start()
    set_flow_service(svc)
    try:
        await chat("in this style", ("style.png", png_bytes("teal")))
        shown = json.loads(await authoring.flow_get())["spec_help"]["example"]
        types = {n["type"] for n in shown["nodes"]}
        assert {"load_image", "text", "join_text", "image", "save"} <= types
        assert all(set(n) <= {"id", "type", "title", "values"} for n in shown["nodes"])

        payload = json.loads(await authoring.flow_create(spec=shown))

        assert payload["status"] == "ok", payload
        assert "warnings" not in payload
        assert payload["node_count"] == 6
    finally:
        await svc.close()
        reset_flow_service()


def test_flow_create_says_values_go_inside_values() -> None:
    spec = get_default_registry().get("flow_create").spec.parameters["spec"]["description"]
    assert 'Values go inside each node\'s "values"' in spec
    assert "spec_help.example" in spec
