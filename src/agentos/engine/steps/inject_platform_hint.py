"""Inject channel-specific rendering hints into the dynamic prompt suffix."""

from __future__ import annotations

from typing import Any

from agentos.engine.pipeline import TurnContext


def _channel_type(config: Any, channel_kind: str) -> str:
    """Return the channel type behind ``channel_kind``, lower-cased.

    For a channel turn ``channel_kind`` is the configured entry name, and that
    name is free-form (``agentos channels add email --name inbox``), while the
    render hints are keyed by type. Resolve the name through the gateway's
    channel entries; anything that is not an entry name (``web``, ``cli``,
    ``cron``) is already its own kind.
    """
    channels = getattr(getattr(config, "channels", None), "channels", None)
    if isinstance(channels, list | tuple):
        for entry in channels:
            if getattr(entry, "name", None) != channel_kind:
                continue
            entry_type = getattr(entry, "type", None)
            if isinstance(entry_type, str) and entry_type.strip():
                return entry_type.strip().lower()
    return channel_kind.lower()


async def inject_platform_hint(ctx: TurnContext) -> TurnContext:
    """Append a channel rendering hint to the uncached suffix when needed."""

    prompt_cfg = getattr(ctx.config, "prompt", None) if ctx.config else None
    if not getattr(prompt_cfg, "platform_hint_enabled", True):
        ctx.metadata["inject_platform_hint__applied"] = False
        return ctx

    from agentos.channels.registry import markdown_render_hint_for

    channel_kind = _channel_type(ctx.config, str(ctx.metadata.get("channel_kind") or "").strip())
    hint = markdown_render_hint_for(channel_kind)
    if not hint:
        ctx.metadata["inject_platform_hint__applied"] = False
        return ctx

    if isinstance(ctx.system_prompt, str):
        base, suffix = ctx.system_prompt, ""
    else:
        base, suffix = ctx.system_prompt

    block = f"## Channel Rendering\n\n{hint}"
    ctx.system_prompt = (base, f"{suffix}\n\n{block}" if suffix else block)
    ctx.metadata["platform_markdown_hint"] = channel_kind
    return ctx
