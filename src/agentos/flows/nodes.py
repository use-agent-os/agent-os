"""The v1 node catalog: text, load_image, join_text, prompt_writer,
describe_image, image, save.

Each node gets ``args``: every input by name, with the link value when the
input is linked and the resolved widget value otherwise (defaults applied,
``auto`` model resolved, widgets hidden by ``show_when`` removed). TEXT values
are ``str``; IMAGE values are lists of :class:`~agentos.flows.types.ImageRef`.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from agentos.flows.adapters import AdapterRequest, AdapterResult
from agentos.flows.media import downscale, to_png
from agentos.flows.pngmeta import embed_flow_metadata
from agentos.flows.schema import (
    FlowNode,
    InputSpec,
    NodeContext,
    NodeResult,
    NodeSchema,
    OutputSpec,
    register_node,
)
from agentos.flows.types import IMAGE, TEXT, ImageRef, NodeError

STYLES = ("photographic", "illustration", "3d render", "anime", "minimal", "none")
FOCUSES = ("everything", "composition", "style", "subject", "colors")
BACKGROUNDS = ("auto", "opaque", "transparent")
VARIATIONS = ("new", "keep")
DEFAULT_SAVE_NAME = "{flow}_{run}_{n}"

#: Upper bound on images sent to the vision model by ``describe_image``.
DESCRIBE_MAX_IMAGES = 4


# ── input ─────────────────────────────────────────────────────────────────


@register_node
class TextNode(FlowNode):
    schema = NodeSchema(
        id="text",
        display="Text",
        group="input",
        description="A piece of text: an idea, a prompt or an instruction.",
        inputs=(
            InputSpec(
                name="text",
                widget="textarea",
                socket=False,
                default="",
                placeholder="Type some text",
            ),
        ),
        outputs=(OutputSpec("text", TEXT),),
    )
    pure = True

    def evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"text": str(args.get("text") or "")}

    async def run(self, ctx: NodeContext, args: dict[str, Any]) -> NodeResult:
        return NodeResult(outputs=self.evaluate(args))


@register_node
class LoadImageNode(FlowNode):
    schema = NodeSchema(
        id="load_image",
        display="Load Image",
        group="input",
        description="An image you upload or drop onto the canvas.",
        inputs=(InputSpec(name="image", widget="image_upload", socket=False, default=None),),
        outputs=(OutputSpec("image", IMAGE),),
    )
    pure = True

    def evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = args.get("image")
        if not isinstance(ref, ImageRef):
            raise NodeError("required", "Choose an image", input="image")
        return {"image": [ref]}

    async def run(self, ctx: NodeContext, args: dict[str, Any]) -> NodeResult:
        outputs = self.evaluate(args)
        ref = outputs["image"][0]
        if not ctx.media.exists(ref.sha256):
            raise NodeError("missing_image", "The uploaded image is gone", input="image")
        return NodeResult(outputs=outputs)


# ── utility ───────────────────────────────────────────────────────────────

DEFAULT_JOIN_TEMPLATE = "{a}, {b}"
_DANGLING = re.compile(r"^[\s,;]+|[\s,;]+$")


def join_texts(a: str, b: str, template: str) -> str:
    """Fill ``{a}`` / ``{b}``; a missing ``b`` leaves no dangling separator."""
    text = (template or DEFAULT_JOIN_TEMPLATE).replace("{a}", a or "").replace("{b}", b or "")
    return _DANGLING.sub("", text)


@register_node
class JoinTextNode(FlowNode):
    schema = NodeSchema(
        id="join_text",
        display="Join Text",
        group="utility",
        description="Combine two texts with a template such as '{a}, {b}'.",
        inputs=(
            InputSpec(name="a", type=TEXT, widget="textarea", default=""),
            InputSpec(name="b", type=TEXT, widget="textarea", optional=True, default=""),
            InputSpec(
                name="template",
                widget="text",
                socket=False,
                default=DEFAULT_JOIN_TEMPLATE,
                tooltip="{a} and {b} are replaced by the two texts",
            ),
        ),
        outputs=(OutputSpec("text", TEXT),),
    )
    pure = True

    def evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "text": join_texts(
                str(args.get("a") or ""),
                str(args.get("b") or ""),
                str(args.get("template") or DEFAULT_JOIN_TEMPLATE),
            )
        }

    async def run(self, ctx: NodeContext, args: dict[str, Any]) -> NodeResult:
        return NodeResult(outputs=self.evaluate(args))


# ── ai: text ──────────────────────────────────────────────────────────────

PROMPT_WRITER_SYSTEM = (
    "You write prompts for an image generation model. Turn the user's idea into one "
    "vivid, concrete prompt that names the subject, setting, composition, lighting, "
    "colors and mood. When reference context is given, use what fits the idea. "
    "Reply with the prompt only: no preamble, no quotes, no lists, no markdown, "
    "at most 120 words."
)
_STYLE_CLAUSES = {
    "photographic": "Make it a photograph; mention camera, lens or film details when they help.",
    "illustration": "Make it an illustration; name the medium and the line and color treatment.",
    "3d render": "Make it a 3D render; name materials, lighting setup and render style.",
    "anime": "Make it anime-style art; name the shading and line style.",
    "minimal": "Keep it minimal: few elements, clean shapes, generous empty space.",
    "none": "",
}
_PREAMBLE = re.compile(r"^(?:here(?:'s| is) (?:the|your|a) prompt[:\s]*|prompt\s*:\s*)", re.I)


def clean_prompt(text: str) -> str:
    out = (text or "").strip()
    out = _PREAMBLE.sub("", out).strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'`":
        out = out[1:-1].strip()
    return out


@register_node
class PromptWriterNode(FlowNode):
    schema = NodeSchema(
        id="prompt_writer",
        display="Prompt Writer",
        group="ai",
        description="An LLM turns a short idea into a detailed image prompt.",
        inputs=(
            InputSpec(
                name="idea",
                type=TEXT,
                widget="textarea",
                default="",
                placeholder="A short idea, e.g. a lighthouse at dusk",
            ),
            InputSpec(name="context", type=TEXT, optional=True, tooltip="Optional reference text"),
            InputSpec(
                name="style",
                widget="combo",
                socket=False,
                options=STYLES,
                default="photographic",
            ),
        ),
        outputs=(OutputSpec("text", TEXT),),
        is_api=True,
        price_depends_on=(),
    )

    async def run(self, ctx: NodeContext, args: dict[str, Any]) -> NodeResult:
        idea = str(args.get("idea") or "").strip()
        context = str(args.get("context") or "").strip()
        style = str(args.get("style") or "photographic")
        system = PROMPT_WRITER_SYSTEM
        clause = _STYLE_CLAUSES.get(style, "")
        if clause:
            system = f"{system} {clause}"
        prompt = f"Idea: {idea}"
        if context:
            prompt += f"\n\nReference context:\n{context}"
        async with ctx.provider_slot("auxiliary"):
            result = await ctx.text_adapter.complete(
                task="flow_prompt",
                system=system,
                prompt=prompt,
                session_key=ctx.session_key,
            )
        text = clean_prompt(result.text)
        if not text:
            raise NodeError("empty_result", "The model returned an empty prompt")
        return NodeResult(outputs={"text": text}, cost_usd=result.cost_usd)


DESCRIBE_SYSTEM = (
    "You describe images so that another model can recreate or remix them. Be concrete "
    "and visual. Reply with the description only: one paragraph, no preamble, no markdown, "
    "at most 120 words."
)
_FOCUS_PROMPTS = {
    "everything": "Describe the subject, setting, composition, lighting, colors and style.",
    "composition": "Describe the composition: framing, camera angle, layout and depth.",
    "style": "Describe the visual style: medium, technique, rendering and mood.",
    "subject": "Describe the main subject in detail: what it is, its look and its pose.",
    "colors": "Describe the color palette, contrast and lighting.",
}


@register_node
class DescribeImageNode(FlowNode):
    schema = NodeSchema(
        id="describe_image",
        display="Describe Image",
        group="ai",
        description="A vision model describes an image as text.",
        inputs=(
            InputSpec(name="image", type=IMAGE),
            InputSpec(
                name="focus",
                widget="combo",
                socket=False,
                options=FOCUSES,
                default="everything",
            ),
        ),
        outputs=(OutputSpec("text", TEXT),),
        is_api=True,
        price_depends_on=(),
    )

    async def run(self, ctx: NodeContext, args: dict[str, Any]) -> NodeResult:
        refs: list[ImageRef] = list(args.get("image") or [])[:DESCRIBE_MAX_IMAGES]
        if not refs:
            raise NodeError("required", "Connect an image", input="image")
        images = [downscale(*ctx.media.read(ref.sha256)) for ref in refs]
        focus = str(args.get("focus") or "everything")
        prompt = _FOCUS_PROMPTS.get(focus, _FOCUS_PROMPTS["everything"])
        async with ctx.provider_slot("auxiliary"):
            result = await ctx.text_adapter.complete(
                task="vision",
                system=DESCRIBE_SYSTEM,
                prompt=prompt,
                images=images,
                session_key=ctx.session_key,
            )
        text = (result.text or "").strip()
        if not text:
            raise NodeError("empty_result", "The vision model returned no description")
        return NodeResult(outputs={"text": text}, cost_usd=result.cost_usd)


# ── ai: image ─────────────────────────────────────────────────────────────


@register_node
class ImageNode(FlowNode):
    schema = NodeSchema(
        id="image",
        display="Image",
        group="ai",
        description=(
            "Generate an image from a prompt, or edit / combine images when any are connected."
        ),
        inputs=(
            InputSpec(
                name="prompt",
                type=TEXT,
                widget="textarea",
                default="",
                placeholder="Describe the image",
            ),
            InputSpec(
                name="images",
                type=IMAGE,
                optional=True,
                multi=True,
                tooltip="Connect images to edit them or use them as references",
            ),
            InputSpec(name="model", widget="model", socket=False, default="auto"),
            InputSpec(
                name="aspect",
                widget="combo",
                socket=False,
                default="1:1",
                options_from="model.ratios",
                show_when={"model.has": "ratios"},
            ),
            InputSpec(
                name="resolution",
                widget="combo",
                socket=False,
                default="1K",
                options_from="model.tiers",
                show_when={"model.has": "tiers"},
            ),
            InputSpec(
                name="size",
                widget="combo",
                socket=False,
                default="auto",
                options_from="model.sizes",
                show_when={"model.has": "sizes"},
            ),
            InputSpec(
                name="quality",
                widget="combo",
                socket=False,
                default="auto",
                options_from="model.qualities",
                show_when={"model.has": "qualities"},
            ),
            InputSpec(
                name="background",
                widget="combo",
                socket=False,
                default="auto",
                options=BACKGROUNDS,
                show_when={"model.has": "transparent_bg"},
                advanced=True,
            ),
            InputSpec(name="count", widget="number", socket=False, default=1, min=1, max=4, step=1),
            InputSpec(
                name="variation",
                widget="variation",
                socket=False,
                default="new",
                options=VARIATIONS,
                tooltip="new: a fresh image every run; keep: reuse the last one",
            ),
        ),
        outputs=(OutputSpec("image", IMAGE),),
        is_api=True,
        price_depends_on=("model", "resolution", "size", "quality", "count", "images.count"),
    )

    async def run(self, ctx: NodeContext, args: dict[str, Any]) -> NodeResult:
        entry = ctx.models.get(str(args.get("model") or ""))
        if entry is None:
            raise NodeError("unknown_model", "Choose a model", input="model")
        prompt = str(args.get("prompt") or "").strip()
        refs: list[ImageRef] = list(args.get("images") or [])
        count = max(1, int(args.get("count") or 1))
        images = [downscale(*ctx.media.read(ref.sha256)) for ref in refs]
        base = AdapterRequest(
            prompt=prompt,
            images=images,
            aspect_ratio=args.get("aspect"),
            image_size=args.get("resolution"),
            size=args.get("size"),
            quality=args.get("quality"),
            background=args.get("background"),
            timeout_s=entry.timeout_s,
        )
        # One call with n=count when the model takes n, else count parallel calls.
        calls = [count] if entry.n_max >= count else [1] * count
        produced = 0

        async def one(n: int) -> AdapterResult:
            nonlocal produced
            request = AdapterRequest(**{**base.__dict__, "n": n})
            async with ctx.provider_slot(entry.provider):
                result = await ctx.image_adapter.generate(entry, request)
            if not result.images:
                text = (result.text or "").strip()
                raise NodeError(
                    "empty_result",
                    f"The model returned no image: {text[:500]}"
                    if text
                    else "The model returned no image",
                )
            produced += len(result.images)
            await ctx.progress(min(produced, count), count)
            return result

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(one(n)) for n in calls]
        except BaseExceptionGroup as failures:
            # The first failure is the node's error; the sibling calls were cancelled.
            raise failures.exceptions[0] from None
        results = [task.result() for task in tasks]
        stored: list[ImageRef] = []
        for result in results:
            for data, mime in result.images:
                if len(stored) >= count:
                    break
                try:
                    stored.append(ctx.media.put(data, mime))
                except ValueError as exc:
                    raise NodeError("bad_image", f"The provider returned {exc}") from exc
        costs = [result.cost_usd for result in results]
        cost = sum(c for c in costs if c is not None) if all(c is not None for c in costs) else None
        extra: dict[str, Any] = {}
        note = aspect_note(args.get("size"), args.get("aspect"), stored)
        if note:
            extra["note"] = note
        return NodeResult(outputs={"image": stored}, extra=extra, cost_usd=cost)


#: Relative difference in width/height ratio above which a result counts as off-aspect.
ASPECT_TOLERANCE = 0.03


def _requested_shape(size: Any, aspect: Any) -> tuple[float, str] | None:
    """``(width/height, label)`` the node asked for, from ``size`` (WxH) or ``aspect`` (W:H)."""
    for raw, sep, label_sep in ((size, "x", "\u00d7"), (aspect, ":", ":")):
        if not isinstance(raw, str) or sep not in raw:
            continue
        left, _, right = raw.partition(sep)
        try:
            width, height = float(left), float(right)
        except ValueError:
            continue
        if width > 0 and height > 0:
            return width / height, f"{left}{label_sep}{right}"
    return None


def aspect_note(size: Any, aspect: Any, images: list[ImageRef]) -> str | None:
    """A note when the provider returned another shape than the node asked for (> 3 %)."""
    wanted = _requested_shape(size, aspect)
    if wanted is None:
        return None
    ratio, label = wanted
    for ref in images:
        if ref.width <= 0 or ref.height <= 0:
            continue
        got = ref.width / ref.height
        if abs(got - ratio) / ratio > ASPECT_TOLERANCE:
            return f"Provider returned {ref.width}\u00d7{ref.height} instead of {label}"
    return None


# ── output ────────────────────────────────────────────────────────────────

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str, fallback: str) -> str:
    slug = _UNSAFE_NAME.sub("-", text.strip()).strip("-.")
    return slug[:60] or fallback


def render_save_name(template: str, *, flow: str, run: str, n: int) -> str:
    raw = (template or DEFAULT_SAVE_NAME).replace("{flow}", flow).replace("{run}", run)
    raw = raw.replace("{n}", str(n))
    return _slug(raw, f"{flow}_{run}_{n}")


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


@register_node
class SaveNode(FlowNode):
    schema = NodeSchema(
        id="save",
        display="Save",
        group="output",
        description="Write images as PNG files that carry the flow inside them.",
        inputs=(
            InputSpec(name="images", type=IMAGE, multi=True),
            InputSpec(
                name="name",
                widget="text",
                socket=False,
                default=DEFAULT_SAVE_NAME,
                tooltip="{flow}, {run} and {n} are filled in",
            ),
        ),
        outputs=(),
        is_output=True,
    )
    always_run = True

    async def run(self, ctx: NodeContext, args: dict[str, Any]) -> NodeResult:
        refs: list[ImageRef] = list(args.get("images") or [])
        if not refs:
            raise NodeError("required", "Connect at least one image", input="images")
        ctx.outputs_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "flow_id": ctx.flow_id,
            "rev": ctx.rev,
            "run_id": ctx.run_id,
            "graph": ctx.graph,
        }
        flow_slug = _slug(ctx.flow_name, ctx.flow_id)
        saved: list[ImageRef] = []
        files: list[str] = []
        artifacts: list[dict[str, Any]] = []
        for ref in refs:
            data, _mime = ctx.media.read(ref.sha256)
            png = embed_flow_metadata(to_png(data), metadata)
            stored = ctx.media.put(png, "image/png")
            n = ctx.save_index() if ctx.save_index is not None else len(saved) + 1
            stem = render_save_name(
                str(args.get("name") or DEFAULT_SAVE_NAME), flow=flow_slug, run=ctx.run_id, n=n
            )
            path = _unique_path(ctx.outputs_dir, stem, ".png")
            path.write_bytes(png)
            saved.append(stored)
            files.append(str(path))
            if ctx.artifact_sink is not None:
                artifact = ctx.artifact_sink(path, "image/png")
                if artifact:
                    artifacts.append(artifact)
        extra: dict[str, Any] = {"images": [ref.to_wire() for ref in saved], "files": files}
        if artifacts:
            extra["artifacts"] = artifacts
        return NodeResult(outputs={}, extra=extra)
