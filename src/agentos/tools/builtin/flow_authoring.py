"""Image Flows authoring tools: ``flow_get``, ``flow_create``, ``flow_update``.

The agent builds and edits the user's Image Flows from chat (``docs/flows.md``,
"Agents can build flows"). Authoring costs nothing, so there is no approval
step; running stays behind ``flow_run``'s cost gate. There is no delete tool,
and every save keeps a revision in the engine, so the user can restore any
earlier version from the editor.

The tools speak the *agent spec* format: nodes without positions (the engine
lays them out), links as ``["node.output", "node.input"]`` pairs, and
``load_image`` values that may name a chat image (``{"attachment": ...}``) or
a workspace file (``{"path": ...}``); this module hands the engine an
``image_resolver`` that turns those into media-store ImageRefs (a problem is
reported on that node like any other spec error). Writes go through
:class:`agentos.flows.FlowService` in-process, never through the ``flows.*``
write RPCs (operator-only on an agent-bound connection).

A successful write publishes an ``application/vnd.agentos.flow-change+json``
card into the session.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from agentos.tools.builtin.flows import (
    _flow_error,
    _public_inputs,
    _service,
    accepts,
    price_text,
    publish_json_card,
)
from agentos.tools.registry import tool
from agentos.tools.types import ToolContext, ToolError, current_tool_context

log = structlog.get_logger(__name__)

FLOW_CHANGE_MIME = "application/vnd.agentos.flow-change+json"
CHANGE_CARD_VERSION = 1
AUTHOR = "agent"
#: Largest image a load_image may take from a chat attachment or a file.
MAX_IMAGE_BYTES = 18 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LATEST = {"latest", "last", "newest", "recent"}

_GUIDANCE = (
    "Authoring is free: nothing was run and nothing was spent. Offer to run it; call "
    "flow_run only when the user wants the images now (running costs money and may "
    "ask for approval)."
)


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def _problems(node_errors: Any) -> list[dict[str, Any]]:
    out = []
    for node_id, errors in (node_errors or {}).items():
        for error in errors if isinstance(errors, list) else []:
            if isinstance(error, dict):
                out.append(
                    {
                        "node": node_id,
                        "input": error.get("input"),
                        "code": error.get("code"),
                        "message": error.get("message"),
                    }
                )
    return out


def _invalid(exc: Any, *, action: str) -> str | None:
    """A validation failure as a result the model can fix from; ``None`` otherwise."""
    if getattr(exc, "code", None) != "flows.invalid":
        return None
    details = getattr(exc, "details", None) or {}
    node_errors = details.get("node_errors") if isinstance(details, dict) else None
    return json.dumps(
        {
            "status": "invalid",
            "saved": False,
            "message": (
                f"Nothing was saved: {getattr(exc, 'message', '') or exc}. Fix the spec "
                f"and call {action} again. flow_get (no argument) lists the node types, "
                "their inputs and the models."
            ),
            "problems": _problems(node_errors),
        },
        ensure_ascii=False,
    )


#: A complete, valid spec (flow_get shows it; a test saves it as is): the
#: "style reference + content" infographic pattern.
EXAMPLE_SPEC: dict[str, Any] = {
    "name": "Infographic in a reference style",
    "nodes": [
        {
            "id": "ref",
            "type": "load_image",
            "title": "Style reference",
            "values": {"image": {"attachment": "latest"}},
        },
        {
            "id": "topic",
            "type": "text",
            "title": "Content",
            "values": {"text": "AgentOS: one gateway, many channels, a sandbox around every tool"},
        },
        {
            "id": "style",
            "type": "text",
            "title": "Style note",
            "values": {"text": "an infographic in the style, palette and layout of the reference"},
        },
        {"id": "brief", "type": "join_text", "values": {"template": "{a}. {b}"}},
        {
            "id": "gen",
            "type": "image",
            "values": {"model": "surplus/gpt-image-2", "aspect": "21:9", "count": 1},
        },
        {"id": "out", "type": "save"},
    ],
    "links": [
        ["ref.image", "gen.images"],
        ["topic.text", "brief.a"],
        ["style.text", "brief.b"],
        ["brief.text", "gen.prompt"],
        ["gen.image", "out.images"],
    ],
    "inputs": {"topic": "topic.text"},
}

_SPEC_HELP = {
    "how": (
        "flow_create's spec is an object shaped exactly like 'example': name, nodes, "
        "links, inputs. Every node setting (text, image, model, aspect, count, ...) "
        "goes inside that node's 'values', never next to 'id' and 'type'. Swap the "
        "model for an available one from 'models' if needed."
    ),
    "example": EXAMPLE_SPEC,
    "load_image": (
        'values.image: {"attachment": "latest"} for the image the user attached in this '
        'chat ("2" = the one before, or its file name); {"attachment": "<artifact_id>"} '
        'for an image a tool published in this chat; {"path": "<file in your '
        "workspace>\"} for a file you made. Never search the disk for the user's "
        "images (no find, ls or copying files): if an image cannot be found, ask the "
        "user to attach it."
    ),
    "variation": (
        "On an image node, 'new' calls the model on every run. 'keep' only reuses the "
        "cached result of an earlier run with exactly the same inputs: no new image, "
        "and it does not make a new image look like an earlier one. To stay close to "
        "an image, link it into the node's 'images' input."
    ),
    "notes": [
        "Positions are optional: new nodes are laid out automatically.",
        "An image node edits or uses references when images are linked to its "
        "'images' input; otherwise it is text to image.",
        "Pick a model from 'models' that is available and supports the operation "
        "(ops.edit for references); 'auto' uses the default model.",
        "Add a save node to keep the results as PNG files.",
        "'inputs' names the values flow_run may override (for example the prompt).",
        "flow_update needs base_rev: the rev flow_get (or your last create/update) "
        "returned. Call flow_get right before an update and change only what the user "
        "asked for.",
    ],
}


# ---------------------------------------------------------------------------
# Images in a spec
# ---------------------------------------------------------------------------


def _sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass
class ChatImage:
    """One image the user sent in this chat (newest first in :func:`chat_images`)."""

    name: str
    mime: str
    sha256: str
    data: bytes


#: User messages scanned for attachments, newest first.
_TRANSCRIPT_WINDOW = 200


def _material_root(ctx: ToolContext) -> Path | None:
    return Path(ctx.artifact_media_root) if ctx.artifact_media_root else None


def _image_from_attachment(
    attachment: dict[str, Any], *, media_root: Path | None, session_id: str
) -> ChatImage | None:
    """Bytes of one stored attachment, as ``build_transcript_attachment_envelope`` wrote it.

    Two shapes reach the transcript: an inline one (``{"type", "name", "data":
    base64}``, what a composer sends for an image up to 2 MB) and a staged one
    (``{"sha256_ref", "name", "mime"}``, whose bytes live in the transcript
    material store ``<media_root>/transcripts/<session_id>/<sha256>``).
    """
    import base64
    import binascii
    import hashlib

    from agentos.attachment_refs import transcript_material_path

    mime = str(attachment.get("type") or attachment.get("mime") or "").lower()
    name = str(attachment.get("name") or "image")
    data: bytes | None = None
    raw = attachment.get("data")
    sha_ref = attachment.get("sha256_ref") or attachment.get("sha256")
    if isinstance(raw, str) and raw:
        try:
            data = base64.b64decode(raw.split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError):
            return None
    elif isinstance(sha_ref, str) and media_root is not None:
        try:
            data = transcript_material_path(media_root, session_id, sha_ref).read_bytes()
        except (OSError, ValueError):
            return None
    if not data:
        return None
    sniffed = _sniff_image(data[:16])
    if sniffed is None or (mime and not mime.startswith("image/")):
        return None
    return ChatImage(name=name, mime=sniffed, sha256=hashlib.sha256(data).hexdigest(), data=data)


async def chat_images(ctx: ToolContext | None) -> list[ChatImage]:
    """The images the user sent in this chat, newest first.

    Read from the session transcript, where the gateway records every user
    message with its attachments (``rpc_sessions`` → ``transcripts``); a
    staged upload's bytes come from the transcript material store. The last
    image of the newest message is ``latest``.
    """
    if ctx is None or not ctx.session_key:
        return []
    from agentos.tools.builtin import sessions

    read = getattr(sessions._session_manager, "get_recent_transcript", None)
    if read is None:
        return []
    try:
        entries = await read(ctx.session_key, _TRANSCRIPT_WINDOW)
    except Exception:  # noqa: BLE001 - no session yet / storage hiccup: no images
        log.debug("flow_authoring.transcript_read_failed", exc_info=True)
        return []
    media_root = _material_root(ctx)
    images: list[ChatImage] = []
    seen: set[str] = set()
    for entry in reversed(list(entries or [])):
        if getattr(entry, "role", None) != "user":
            continue
        content = getattr(entry, "content", None)
        if not isinstance(content, str) or '"attachments"' not in content:
            continue
        try:
            envelope = json.loads(content)
        except ValueError:
            continue
        attachments = envelope.get("attachments") if isinstance(envelope, dict) else None
        if not isinstance(attachments, list):
            continue
        session_id = str(getattr(entry, "session_id", "") or ctx.artifact_session_id or "")
        for attachment in reversed(attachments):
            if not isinstance(attachment, dict):
                continue
            image = _image_from_attachment(attachment, media_root=media_root, session_id=session_id)
            if image is not None and image.sha256 not in seen:
                seen.add(image.sha256)
                images.append(image)
    return images


def _describe(images: list[ChatImage]) -> str:
    return ", ".join(
        f"{i + 1}) {img.name} ({len(img.data) // 1024} KB)" for i, img in enumerate(images[:6])
    )


async def _attachment_bytes(ctx: ToolContext | None, ref: str) -> tuple[bytes, str]:
    value = ref.strip()
    if ctx is None:
        raise ToolError("This chat has no attachments the flow tools can read")
    if value.startswith("art-"):
        from agentos.artifacts import ArtifactStore

        if not ctx.artifact_media_root or not ctx.artifact_session_id:
            raise ToolError(f"No image artifact {value!r} in this chat")
        try:
            artifact, artifact_path = ArtifactStore(ctx.artifact_media_root).resolve_for_download(
                value, session_id=ctx.artifact_session_id
            )
        except Exception as exc:  # noqa: BLE001 - not found / integrity: one clear message
            raise ToolError(f"No image artifact {value!r} in this chat") from exc
        if not str(artifact.mime).startswith("image/"):
            raise ToolError(f"Artifact {value!r} is {artifact.mime}, not an image")
        return artifact_path.read_bytes(), str(artifact.mime)
    images = await chat_images(ctx)
    found: ChatImage | None = None
    lowered = value.lower()
    if lowered in _LATEST:
        found = images[0] if images else None
    elif lowered.isdigit():
        index = int(lowered)
        found = images[index - 1] if 1 <= index <= len(images) else None
    elif _SHA256_RE.match(lowered):
        found = next((img for img in images if img.sha256 == lowered), None)
    else:
        found = next((img for img in images if img.name.lower() == lowered), None)
    if found is None:
        raise ToolError(
            f"No chat image matches {value!r}. "
            + (
                f"Images the user sent in this chat, newest first: {_describe(images)}. "
                'Use {"attachment": "latest"}, a number from this list, or the file name.'
                if images
                else "The user has not sent an image in this chat; ask them to attach "
                "it. Do not search the disk for it."
            )
        )
    return found.data, found.mime


def _workspace_file(ctx: ToolContext | None, raw: str) -> tuple[bytes, str]:
    if ctx is None or not ctx.workspace_dir:
        raise ToolError("No agent workspace: a load_image path cannot be read here")
    root = Path(ctx.workspace_dir).expanduser().resolve()
    candidate = Path(raw).expanduser()
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ToolError(
            f"{raw!r} is outside your workspace; load_image may only read files in it"
        ) from exc
    if not target.is_file():
        raise ToolError(f"No file {raw!r} in your workspace")
    if target.stat().st_size > MAX_IMAGE_BYTES:
        raise ToolError(f"{raw!r} is larger than {MAX_IMAGE_BYTES >> 20} MB")
    data = target.read_bytes()
    mime = _sniff_image(data[:16])
    if mime is None:
        raise ToolError(f"{raw!r} is not a PNG, JPEG, GIF or WebP image")
    return data, mime


def _image_resolver(service: Any, ctx: ToolContext | None) -> Any:
    """The engine's ``image_resolver``: ``{"attachment"|"path": ...}`` → ImageRef wire.

    A problem raises ``ValueError``; the engine reports it on that node, next
    to the spec's other errors, and saves nothing.
    """

    async def resolve(node_id: str, value: dict[str, Any]) -> dict[str, Any]:
        del node_id
        try:
            if "attachment" in value:
                data, mime = await _attachment_bytes(ctx, str(value["attachment"]))
            elif "path" in value:
                data, mime = _workspace_file(ctx, str(value["path"]))
            else:  # pragma: no cover - the engine only hands over these two
                raise ToolError('load_image takes {"attachment": ...} or {"path": ...}')
        except ToolError as exc:
            raise ValueError(str(exc)) from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"the image is larger than {MAX_IMAGE_BYTES >> 20} MB")
        return dict(service.ingest_image(data, mime))

    return resolve


def _clean_spec(spec: Any) -> dict[str, Any]:
    """The spec as an object; its shape is checked by the engine, per node."""
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as exc:
            raise ToolError("spec must be a JSON object (see flow_get)") from exc
    if not isinstance(spec, dict):
        raise ToolError("spec must be an object with name, nodes, links and inputs")
    return spec


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def _finish(result: dict[str, Any], ctx: ToolContext | None) -> str:
    from agentos.flows.types import utc_now_iso

    raw_doc = result.get("flow")
    doc: dict[str, Any] = raw_doc if isinstance(raw_doc, dict) else {}
    flow_id = str(result.get("flow_id") or doc.get("id"))
    change = str(result.get("change") or "updated")
    total = result.get("estimate") if isinstance(result.get("estimate"), dict) else None
    card = {
        "version": CHANGE_CARD_VERSION,
        "kind": "flow_change",
        "flow_id": flow_id,
        "name": result.get("name") or doc.get("name") or flow_id,
        "rev": int(result.get("rev") or doc.get("rev") or 0),
        "change": change,
        "summary": result.get("summary") or "",
        "node_count": int(result.get("node_count") or len(doc.get("nodes") or [])),
        "estimate": total,
        "fetchedAt": utc_now_iso(),
    }
    artifact = publish_json_card(
        ctx, card, name=f"flow-{change}-{flow_id}-r{card['rev']}.json", mime=FLOW_CHANGE_MIME
    )
    payload: dict[str, Any] = {
        "status": "ok",
        "saved": True,
        "flow_id": flow_id,
        "name": card["name"],
        "rev": card["rev"],
        "change": change,
        "summary": card["summary"],
        "node_count": card["node_count"],
        "estimate_per_run": price_text(total) if total else "unknown",
        "estimate": total,
        "inputs": _public_inputs(doc) if doc else [],
        "note": _GUIDANCE,
    }
    warnings = _problems(result.get("warnings"))
    if warnings:
        payload["warnings"] = warnings
        reasons = "; ".join(sorted({str(w["message"]) for w in warnings if w.get("message")}))
        payload["note_warnings"] = (
            f"Saved, but it cannot run yet: {reasons}. Tell the user what is missing "
            "(for example the API key named here) instead of running it."
        )
    if artifact is not None:
        payload["artifacts"] = [{k: v for k, v in artifact.items() if k != "download_url"}]
        payload["note_delivery"] = "A card for this change is already in the chat."
    return json.dumps(payload, ensure_ascii=False, default=str)


def _summary_since(service: Any, flow_id: str, base_rev: int) -> str:
    try:
        rows = service.revisions(flow_id).get("revisions") or []
    except Exception:  # noqa: BLE001 - the summary is a courtesy
        return ""
    newer = [r for r in rows if int(r.get("rev") or 0) > base_rev]
    return "; ".join(
        f"rev {r.get('rev')} by {r.get('author')}: {r.get('summary')}" for r in reversed(newer)
    )


def _conflict(current_rev: int | None, summary_since: str) -> str:
    now = f"now rev {current_rev}" if current_rev is not None else "it has a newer rev"
    since = f": {summary_since}" if summary_since else ""
    return json.dumps(
        {
            "status": "conflict",
            "saved": False,
            "current_rev": current_rev,
            "summary_since": summary_since or None,
            "message": (
                f"The flow changed since you read it ({now}{since}). Nothing was saved. "
                "Call flow_get and re-apply only the requested change."
            ),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    name="flow_get",
    concurrency="parallel",
    read_only=True,
    defer_loading=True,
    description=(
        "Read what you need to create or edit an Image Flow (a node-graph image "
        "workflow or pipeline). Without flow: the node types (inputs, widgets, options, "
        "defaults, outputs), the image models with their capabilities and prices, and "
        "the spec format flow_create and flow_update take. With flow (id or exact "
        "name): that flow as a spec plus its public inputs, to edit it or add a node."
    ),
    params={
        "flow": {
            "type": "string",
            "description": "Flow id (flw_...) or exact name; omit for the catalog.",
        },
    },
)
async def flow_get(flow: str | None = None) -> str:
    from agentos.flows import FlowError

    service = _service()
    if not isinstance(flow, str) or not flow.strip():
        return json.dumps(
            {**service.catalog(), "spec_help": _SPEC_HELP}, ensure_ascii=False, default=str
        )
    try:
        payload = service.get_spec(flow.strip())
    except FlowError as exc:
        raise _flow_error(exc) from exc
    return json.dumps(payload, ensure_ascii=False, default=str)


@tool(
    name="flow_create",
    defer_loading=True,
    description=(
        "Create a new Image Flow (build an image workflow or pipeline to automate "
        "images) from a spec: nodes, links and public inputs; positions are laid out "
        "for you. Authoring is free and needs no approval; nothing runs. Read the "
        "catalog with flow_get first and pick an available model. Prefer flow_update "
        "when the user means a flow they already have. After creating, offer to run it "
        "with flow_run; run it right away only if the user asked for the image now. A "
        "spec that does not validate is not saved; fix the reported problems and retry."
    ),
    params={
        "spec": {
            "type": "object",
            "description": (
                'The flow: {"name", "nodes": [{"id", "type", "title"?, "values"?}], '
                '"links": [["node.output", "node.input"]], "inputs": {"name": '
                '"node.input"}}. Values go inside each node\'s "values" (e.g. {"id": "gen", '
                '"type": "image", "values": {"model": "...", "aspect": "21:9"}}), never '
                "next to id and type; flow_get() has a complete example under "
                'spec_help.example. load_image takes values.image {"attachment": "latest"} '
                "for the image the user attached in this chat (never search the disk for "
                'it), {"attachment": "<artifact_id>"} or {"path": "<workspace file>"}.'
            ),
        },
    },
    required=["spec"],
)
async def flow_create(spec: dict[str, Any]) -> str:
    from agentos.flows import FlowError

    service = _service()
    ctx = current_tool_context.get()
    try:
        result = await service.create_from_spec(
            _clean_spec(spec),
            author=AUTHOR,
            session_key=ctx.session_key if ctx is not None else None,
            image_resolver=_image_resolver(service, ctx),
        )
    except FlowError as exc:
        invalid = _invalid(exc, action="flow_create")
        if invalid is not None:
            return invalid
        raise _flow_error(exc) from exc
    return _finish(result, ctx)


@tool(
    name="flow_update",
    defer_loading=True,
    description=(
        "Edit an existing Image Flow (add a node, change a prompt, model or link) by "
        "replacing its graph with a new spec. Always call flow_get(flow) right before, "
        "start from the spec it returns (never from memory: the user may have edited or "
        "restored the flow), change only what the user asked for, and pass its rev as "
        "base_rev. Nodes keep their places by id and new ones are laid out. Works on any "
        "flow, the user's or yours; the previous version is kept and the user can "
        "restore it. Authoring is free and needs no approval; nothing runs. There is no "
        "way to delete a flow. For a one-off variant (other model, more images) do not "
        "edit the flow: use flow_run with overrides. A spec that does not validate is "
        "not saved; fix the reported problems and retry."
    ),
    params={
        "flow": {
            "type": "string",
            "description": "Flow id (flw_...) or exact name of the flow to edit.",
        },
        "spec": {
            "type": "object",
            "description": "The complete new spec (same format as flow_create).",
        },
        "base_rev": {
            "type": "integer",
            "description": (
                "The rev flow_get returned (or your last flow_create/flow_update). If the "
                "flow changed since, nothing is saved and you are told what changed."
            ),
        },
    },
    required=["flow", "spec", "base_rev"],
)
async def flow_update(flow: str, spec: dict[str, Any], base_rev: int | None = None) -> str:
    from agentos.flows import FlowError

    if not isinstance(flow, str) or not flow.strip():
        raise ToolError("flow must be a flow id or name (see flow_list)")
    if isinstance(base_rev, bool) or not isinstance(base_rev, int):
        raise ToolError(
            "base_rev is required: call flow_get(flow) and pass the rev it returns, "
            "then re-apply only the change the user asked for"
        )
    service = _service()
    ctx = current_tool_context.get()
    try:
        current = service.resolve_flow(flow.strip())
    except FlowError as exc:
        raise _flow_error(exc) from exc
    if not accepts(service.update_from_spec, "base_rev"):
        # An engine without the check of its own: refuse a stale write here.
        now = int(current.get("rev") or 0)
        if now != base_rev:
            return _conflict(now, _summary_since(service, current["id"], base_rev))
    extra = {"base_rev": base_rev} if accepts(service.update_from_spec, "base_rev") else {}
    try:
        result = await service.update_from_spec(
            flow.strip(),
            _clean_spec(spec),
            author=AUTHOR,
            session_key=ctx.session_key if ctx is not None else None,
            image_resolver=_image_resolver(service, ctx),
            **extra,
        )
    except FlowError as exc:
        if exc.code == "flows.conflict":
            details = exc.details or {}
            reported = details.get("current_rev")
            return _conflict(
                int(reported) if isinstance(reported, int) else None,
                str(details.get("summary_since") or ""),
            )
        invalid = _invalid(exc, action="flow_update")
        if invalid is not None:
            return invalid
        raise _flow_error(exc) from exc
    return _finish(result, ctx)
