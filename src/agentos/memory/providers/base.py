"""Abstract base class for pluggable memory providers.

Memory providers give the agent persistent recall across sessions. The
provider manager (Task B2) enforces a one-external-provider limit to prevent
tool-schema bloat and conflicting memory backends.

External providers (mem0, and future backends) are registered and activated
via the ``memory.provider`` config key. Only one external provider runs at a
time. AgentOS wires the provider lifecycle through its async engine, so every
hook that touches the backend is ``async def`` and expected to be
non-blocking: queue latency-bound work rather than awaiting it inline on the
turn path.

Lifecycle (called by the provider manager, wired into the engine runtime):
  initialize()           — connect, create resources, warm up
  system_prompt_block()  — static text for the system prompt (sync, cheap)
  prefetch(query)        — background recall injected before each turn
  queue_prefetch(query)  — schedule the next turn's recall
  sync_turn(user, asst)  — async write after each turn
  get_tool_schemas()     — tool schemas to expose to the model
  handle_tool_call()     — dispatch a tool call
  shutdown()             — clean exit (drain queues, close connections)

Optional hooks (override to opt in):
  on_session_end(messages)                    — end-of-session extraction
  on_session_switch(new_session_id, **kwargs) — mid-process session_id rotation
  on_pre_compress(messages) -> str            — extract before compression
  on_memory_write(action, target, content)    — mirror curated memory writes

Future hooks (present in the hermes-agent original, deferred as YAGNI for v1
and intentionally not ported here): ``on_delegation`` (parent-side observation
of subagent work), ``backup_paths`` (extra on-disk paths to include in backup),
``get_config_schema`` / ``save_config`` (interactive setup walkthrough).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. ``'mem0'``)."""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured and ready to activate.

        Called while deciding whether to activate the provider. Must NOT make
        network calls — check config and installed dependencies only (e.g.
        lazily import the backend SDK and confirm credentials are present).
        """

    @abstractmethod
    async def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Initialize for a session.

        Called once at engine startup. May create resources (banks, tables),
        establish connections, start background tasks, etc.

        kwargs always include:
          - ``agent_state_dir`` (str): The active per-profile state directory.
            Use this for scoped storage instead of hardcoding a home path.
          - ``platform`` (str): "cli", "gateway", "cron", etc.

        kwargs may also include:
          - ``agent_context`` (str): "primary", "subagent", "cron", or "flush".
            Providers should skip writes for non-primary contexts (cron system
            prompts would corrupt user representations).
          - ``agent_identity`` (str): Profile name. Use for per-profile
            provider identity scoping.
          - ``parent_session_id`` (str): For subagents, the parent's session_id.
          - ``user_id`` (str): Platform user identifier (gateway sessions).
        """

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt.

        Called during system-prompt assembly. Return empty string to skip.
        This is for STATIC provider info (instructions, status). Prefetched
        recall context is injected separately via :meth:`prefetch`.
        """
        return ""

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call. Return formatted text to inject as
        context, or empty string if nothing relevant. Implementations should
        be fast — do the real recall in the background (see
        :meth:`queue_prefetch`) and return cached results here.

        ``session_id`` is provided for providers serving concurrent sessions
        (gateway group chats). Providers that don't need per-session scoping
        can ignore it.
        """
        return ""

    async def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. The result is consumed by
        :meth:`prefetch` on the next turn. Default is a no-op — providers that
        do background prefetching should override this.
        """

    async def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a completed turn to the backend.

        Called after each turn. Should be non-blocking — queue for background
        processing if the backend has latency.

        ``messages`` is the OpenAI-style conversation message list as of the
        completed turn, including assistant tool calls and tool results.
        Providers that do not need raw turn context can ignore it.
        """

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas this provider exposes.

        Each schema follows the OpenAI function-calling format:
        ``{"name": ..., "description": ..., "parameters": {...}}``.

        Return an empty list if this provider has no tools (context-only).
        """
        return []

    async def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        """Handle a tool call for one of this provider's tools.

        Must return a JSON string (the tool result). Only called for tool
        names returned by :meth:`get_tool_schemas`. The default raises
        :class:`NotImplementedError` naming this provider and the tool.
        """
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    async def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections.

        Default is a no-op. Providers with background tasks should drain them
        here; the manager bounds this with a timeout (Task B2).
        """

    # -- Optional hooks (override to opt in) ---------------------------------

    async def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Called when a session ends (explicit exit or timeout).

        Use for end-of-session fact extraction, summarization, etc.
        ``messages`` is the full conversation history.

        NOT called after every turn — only at actual session boundaries
        (CLI exit, ``/reset``, gateway session expiry).
        """

    async def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        """Called when the engine switches session_id mid-process.

        Fires on session resume / branch / reset / new (and the gateway
        equivalents) and on context compression — any path that reassigns the
        active session_id without tearing the provider down.

        Providers that cache per-session state in :meth:`initialize`
        (``_session_id``, accumulated turn buffers, counters) should update or
        reset that state here so subsequent writes land in the correct
        session's record.

        Parameters
        ----------
        new_session_id:
            The session_id the engine just switched to.
        parent_session_id:
            The previous session_id, when meaningful — set for branch (fork
            lineage), compression (continuation lineage), and resume (the
            session being left). Empty string when no lineage applies.
        reset:
            ``True`` when this is a genuinely new conversation, not a
            resumption. Providers should flush accumulated per-session buffers
            when set. ``False`` for resume / branch / compression where the
            logical conversation continues under the new id.
        rewound:
            ``True`` if session_id is unchanged but the transcript was
            truncated; providers caching per-turn state should invalidate.

        Default is a no-op.
        """

    async def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Called before context compression discards old messages.

        Use to extract insights from messages about to be compressed.
        ``messages`` is the list that will be summarized/discarded.

        Return text to fold into the compression summary prompt so the
        compressor preserves provider-extracted insights. Return empty string
        for no contribution (default).
        """
        return ""

    async def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Called when the built-in curated memory tool writes an entry.

        ``action``: ``'add'``, ``'replace'``, or ``'remove'``.
        ``target``: ``'memory'`` or ``'user'``.
        ``content``: the new entry text (``''`` for ``'remove'``).
        ``metadata``: structured provenance for the write, when available.
          For ``'replace'`` and ``'remove'`` it carries ``old_entry`` (the
          full text of the entry that was replaced or removed) and
          ``old_text`` (the substring the caller matched it with). Other
          common keys: ``write_origin``, ``execution_context``,
          ``session_id``, ``parent_session_id``, ``platform``, ``tool_name``.

        Use to mirror curated memory writes to your backend. A provider that
        keeps its own copy of curated entries should drop the copy of
        ``old_entry`` on replace/remove so it does not keep recalling an
        edited or deleted fact. Called on the manager's background worker;
        failures are logged and never reach the tool call. Default no-op.
        """
