"""Jinja2-based system prompt assembly for agent identity."""

from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .types import AgentProfile

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=1)
def _make_env() -> Environment:
    # One environment per process: Jinja caches compiled templates per
    # environment, and compiling system_prompt.j2 cost ~10 ms of event-loop
    # time on every turn. ``auto_reload`` (the default) still picks up an
    # edited template file.
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


def assemble_system_prompt(
    profile: AgentProfile,
    *,
    tools: list[str] | None = None,
    deferred_tools: Mapping[str, str] | Sequence[str] | None = None,
    mcp_servers: str | None = None,
    skills: list[str] | None = None,
    memory: str | None = None,
    runtime_info: dict[str, str] | None = None,
    docs_path: str | None = None,
    owner_line: str | None = None,
    heartbeat_prompt: str | None = None,
    model_aliases: list[str] | None = None,
    reasoning_tag_hint: str | None = None,
    channels_enabled: bool = False,
    unattended_context: bool = False,
) -> str:
    """Render the cacheable base of the system prompt for an agent profile.

    The output of this function is the prompt-cache-stable prefix.
    Per-turn volatile content (recalled-memory snippets, daily notes,
    workspace files, runtime extra_context) must NOT be injected here — it
    is appended by the runtime as a separate, uncached suffix via
    ``TurnRunner._render_volatile_block`` and ``_render_recall_block``.

    Args:
        profile: The agent profile containing identity and workspace context.
        tools: List of available tool names, deferred ones included, so the
            sections that depend on a tool stay on when it is one load away.
        deferred_tools: The subset of ``tools`` the request leaves out until
            ``tool_search`` loads them, as name -> one-line summary (a plain
            list of names renders without summaries). Listed under their own
            "load first" heading rather than with the loaded tools.
        mcp_servers: Pre-rendered body of the "Connected MCP Servers" section
            (``agentos.mcp.prompt.render_mcp_servers_section``); omitted when
            empty. Deferred MCP tools are described there, per server, and are
            left out of ``tools`` / ``deferred_tools`` by the caller.
        skills: List of available skill names.
        memory: Memory section content (omitted in minimal mode).
        runtime_info: OS/shell/workspace metadata for Runtime section.
        docs_path: Path to local documentation directory.
        owner_line: Authorized senders line (e.g. phone/ID allowlist).
        heartbeat_prompt: Heartbeat poll recognition and ack protocol text.
        model_aliases: List of model alias strings for Model Aliases section.
        reasoning_tag_hint: Reasoning format constraint (e.g. <think>/<final>).
        channels_enabled: At least one channel adapter is configured. Gates the
            channel-delivery sections (Reply Tags, Messaging, Reactions) so
            channel-less surfaces such as a pure WebUI do not carry them.
        unattended_context: This session can receive internal system events
            (heartbeat polls, unattended runs). Gates the Silent Replies
            sentinel guidance; a set ``heartbeat_prompt`` also implies it.

    Returns:
        Rendered system prompt string (cacheable base only).
    """
    env = _make_env()
    template = env.get_template("system_prompt.j2")

    soul_body: str | None = None
    if profile.identity.soul:
        soul_body = profile.identity.soul.body or None

    ctx: dict = {
        "profile": profile,
        "identity": profile.identity,
        "soul_body": soul_body,
        "agents_doc": profile.agents_doc,
        "tools": tools or [],
        "deferred_tools": (
            dict(deferred_tools)
            if isinstance(deferred_tools, Mapping)
            else dict.fromkeys(deferred_tools or (), "")
        ),
        "mcp_servers": mcp_servers or "",
        "skills": skills or [],
        "memory": memory,
        "prompt_mode": profile.prompt_mode,
        "runtime_info": runtime_info,
        "docs_path": docs_path,
        "owner_line": owner_line,
        "heartbeat_prompt": heartbeat_prompt,
        "model_aliases": model_aliases or [],
        "reasoning_tag_hint": reasoning_tag_hint,
        "channels_enabled": channels_enabled,
        "unattended_context": unattended_context,
    }

    return template.render(**ctx).strip()
