"""Deferred tool loading: keep the long tail out of every request.

Every tool schema a session can use used to ride on every provider request.
Most turns touch a handful of them, so the long tail — voice and dubbing tools,
project admin, git helpers that ``exec_command`` already covers, skill
authoring — was paid for on every call and never used.

A tool whose :class:`~agentos.tools.types.ToolSpec` sets ``defer_loading`` is
left out of the request. One always-loaded tool, ``tool_search``, carries a
one-line catalog of what was held back; calling it *loads* the matches into
the session, and from then on they ride on every request like any other tool.
It is the same shape as skills: the system prompt lists skill names and
descriptions, ``skill_view`` loads one on demand. ``skill_view`` also loads the
deferred tools a skill declares in ``requires_tools``, so following a skill
never costs an extra round trip.

Lifetimes:

* The *deferred set* is a property of the session's policy (which tools it may
  see), so it is stable for the session and so is the ``tool_search``
  description that lists it.
* The *loaded set* is per session, ordered by load time, and persisted as a
  ``SessionContextState`` row (``state_kind="deferred_tools_loaded"``) so a
  reloaded or restarted session keeps what it loaded. A reset invalidates the
  row with every other context state, so a new chat starts lean again.
* A :class:`DeferredToolSurface` is the per-turn view of both. It is built by
  the turn runner when it lists the tools for the request, and it is what
  ``tool_search`` reads and writes during the turn.

What a session may use is never widened here: the deferred set is drawn from
the tools the context's policy already lets it see, so a tool the policy
forbids can neither be listed by ``tool_search`` nor loaded by it.

Which builtins are deferred, and why, is :data:`DEFERRED_BUILTIN_TOOLS`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import structlog

from agentos.util.bounded_registry import BoundedRegistry

if TYPE_CHECKING:
    from agentos.provider.types import ToolDefinition
    from agentos.tool_boundary import ToolCall, ToolResult
    from agentos.tools.registry import ToolRegistry
    from agentos.tools.types import RegisteredTool, ToolContext

log = structlog.get_logger(__name__)

TOOL_SEARCH_NAME = "tool_search"

#: ``SessionContextState`` coordinates of the persisted loaded set.
LOADED_STATE_KIND = "deferred_tools_loaded"
LOADED_STATE_PROVIDER = "portable"

#: Builtins registered with ``defer_loading=True``, and why each one is.
#:
#: Kept loaded on purpose: file read/write/edit/patch and search, the exec
#: tools, web search/fetch, memory, ask_user, sessions and subagent spawning,
#: skill_view/skill_list, publish_artifact, image_generate and the vision/pdf
#: readers, read_spreadsheet, message, router_control and cron. The system
#: prompt switches whole sections on several of those (image generation,
#: memory recall, artifact delivery), a bundled skill tells the model to call
#: ``cron`` directly, and the trading agent drives everything through
#: ``exec_command`` and skills — none of that may depend on a search first.
#:
#: The structured document builders (create_csv/xlsx/pdf_report/pptx) and the
#: git helpers (git_status/diff/log) were deferred at first and moved back:
#: replaying live QA turns ("make me an excel file", "what are the recent
#: commits") showed the model taking the shell or a skill script whenever they
#: were one load away, even with the "load first" list in the system prompt,
#: and calling them directly once they were in the request.
#:
#: Deferred: tools a typical turn does not touch and whose absence the model
#: can always repair with one ``tool_search`` call. 25 names, all visible on a
#: stock gateway, whose request drops from 64 to 40 schemas.
#: ``test_deferred_tools`` pins these numbers.
DEFERRED_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        # Audio generation. Every one is reached through a bundled studio
        # skill that lists it in ``requires_tools``; skill_view loads them.
        "audio_provider_capabilities",
        "dubbing_download",
        "dubbing_generate",
        "dubbing_status",
        "music_generate",
        "song_generate",
        "tts",
        "voice_clone",
        "voice_convert",
        "voice_search",
        # Project (session group) administration.
        "projects_create",
        "projects_list",
        "projects_move_session",
        "projects_update",
        # Skill authoring and the community hub.
        "skill_create",
        "skill_delete",
        "skill_edit",
        "skill_search_community",
        # Child-session control (list / kill / steer). ``sessions_spawn`` of a
        # coding harness loads it, so "stop it" is one call away afterwards.
        "subagents",
        # Niche integrations and admin.
        "agents_list",
        "browser",
        "env_list",
        "http_request",
        "session_rename",
        "x_search",
    }
)

_SUMMARY_MAX_CHARS = 90
_DEFAULT_SEARCH_LIMIT = 5
_MAX_SEARCH_LIMIT = 20
_WORD_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Catalog rendering
# ---------------------------------------------------------------------------


def summarize_description(text: str, limit: int = _SUMMARY_MAX_CHARS) -> str:
    """First sentence of a tool description, cut to ``limit`` characters."""

    flat = " ".join(str(text or "").split())
    if not flat:
        return ""
    match = re.search(r"(?<=[.!?])\s", flat)
    sentence = flat[: match.start()] if match else flat
    if len(sentence) <= limit:
        return sentence
    cut = sentence[: limit - 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space].rstrip()
    return f"{cut}…"


def render_tool_search_description(
    search_tool: RegisteredTool,
    deferred: Sequence[RegisteredTool],
) -> str:
    """``tool_search``'s description: what it does plus the deferred catalog."""

    lines = [search_tool.spec.description, "", "<deferred_tools>"]
    for rt in deferred:
        summary = summarize_description(rt.spec.description)
        lines.append(f"- {rt.spec.name}: {summary}" if summary else f"- {rt.spec.name}")
    lines.append("</deferred_tools>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------

#: The surface of the turn a session is running now. ``tool_search`` and the
#: prompt-side readers (skills gate, system prompt) look it up by session key.
_surfaces: BoundedRegistry[str, DeferredToolSurface] = BoundedRegistry(
    name="deferred_tools._surfaces",
    session_of=lambda key, _value: key,
)

#: Loaded tool names per session, for processes with no session store. With a
#: store, the persisted row is read back at the start of every turn instead.
_loaded: BoundedRegistry[str, list[str]] = BoundedRegistry(
    name="deferred_tools._loaded",
    session_of=lambda key, _value: key,
)


def _key(session_key: str | None) -> str:
    from agentos.session.keys import canonicalize_session_key

    return canonicalize_session_key(session_key)


def loaded_tool_names(session_key: str | None) -> list[str]:
    """Deferred tools ``session_key`` has loaded, oldest first."""

    key = _key(session_key)
    return list(_loaded.get(key) or []) if key else []


def surface_for(session_key: str | None) -> DeferredToolSurface | None:
    """The deferred-tool surface of the turn ``session_key`` is running."""

    key = _key(session_key)
    return _surfaces.get(key) if key else None


def deferred_tool_names(session_key: str | None) -> list[str]:
    """Every tool the session may load, whether it already has or not.

    For callers that ask "can this session use tool X?" — the skills gate and
    the system prompt's tool-dependent sections — a deferred tool counts as
    available: it is one ``tool_search`` away.
    """

    surface = surface_for(session_key)
    return surface.deferred_names if surface is not None else []


def prompt_tool_names(
    tool_defs: Iterable[Any] | None,
    session_key: str | None,
) -> tuple[list[str], dict[str, str]]:
    """Tool names for the system prompt: ``(available, deferred)``.

    ``available`` adds the deferred tools to the request's, so a prompt section
    that depends on a tool (artifact delivery, memory recall) does not vanish
    because that tool is one load away. ``deferred`` maps each deferred tool to
    its one-line summary, which the prompt lists under a "load first" heading:
    a name hidden in the ``tool_search`` description alone was not enough for
    the model to reach for it instead of the shell. Neither depends on what has
    been loaded, so loading a tool never changes the system prompt.
    """

    active = [name for name in (getattr(td, "name", "") for td in tool_defs or ()) if name]
    surface = surface_for(session_key)
    if surface is None or not surface.deferred:
        return active, {}
    deferred = {
        name: summarize_description(rt.spec.description) for name, rt in surface.deferred.items()
    }
    return sorted(set(active) | set(deferred)), deferred


def forget_session(session_key: str | None) -> None:
    """Drop in-memory state for ``session_key`` (tests, explicit resets)."""

    key = _key(session_key)
    if key:
        _surfaces.discard(key)
        _loaded.discard(key)


async def hydrate_loaded_tools(session_manager: Any, session_key: str | None) -> list[str]:
    """Read the persisted loaded set back before the turn lists its tools.

    The session store is the source of truth when there is one: a reset
    invalidates the row, and a gateway restart has no in-memory copy. Without a
    store (one-shot CLI, tests) the in-memory copy stands. Best-effort — a
    failed read leaves the turn with what memory has.
    """

    key = _key(session_key)
    if not key:
        return []
    get_states = getattr(session_manager, "get_context_states", None)
    if not callable(get_states):
        return loaded_tool_names(key)
    try:
        states = await get_states(
            key,
            provider=LOADED_STATE_PROVIDER,
            state_kind=LOADED_STATE_KIND,
        )
    except KeyError:
        states = []
    except Exception as exc:  # noqa: BLE001 - loading is best-effort
        log.warning("deferred_tools.hydrate_failed", session_key=key, error=str(exc))
        return loaded_tool_names(key)
    names: list[str] = []
    if states:
        payload = getattr(states[-1], "payload", None) or {}
        raw = payload.get("loaded") if isinstance(payload, dict) else None
        if isinstance(raw, list):
            names = [str(name) for name in raw if isinstance(name, str) and name]
    _loaded.set(key, names)
    return list(names)


def persist_loaded_with(
    session_manager: Any,
    session_key: str,
    session_id: str | None,
) -> Callable[[list[str]], Awaitable[None]] | None:
    """A callback that writes a session's loaded set to its session store."""

    save_state = getattr(session_manager, "save_context_state", None)
    if not callable(save_state):
        return None

    async def _persist(names: list[str]) -> None:
        from agentos.session.models import SessionContextState

        await save_state(
            SessionContextState(
                session_id=session_id or session_key,
                session_key=session_key,
                provider=LOADED_STATE_PROVIDER,
                state_kind=LOADED_STATE_KIND,
                payload={"loaded": list(names)},
                portable=True,
            )
        )

    return _persist


# ---------------------------------------------------------------------------
# The per-turn surface
# ---------------------------------------------------------------------------


def _terms(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("_", " "))


@dataclass(eq=False)
class DeferredToolSurface:
    """One turn's view of a session's deferred tools.

    ``deferred`` is what the policy lets the session load (name → tool, by
    name). ``loaded`` is what it has loaded, in load order. Lists bound with
    :meth:`bind` are the live tool lists of the running agent: :meth:`load`
    appends to them, so a tool loaded mid-turn is on the very next provider
    request of the same turn.
    """

    session_key: str
    registry: ToolRegistry
    ctx: ToolContext
    deferred: dict[str, RegisteredTool]
    loaded: list[str]
    persist: Callable[[list[str]], Awaitable[None]] | None = None
    _bound: list[tuple[list[Any], dict[str, Any] | None]] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        registry: ToolRegistry,
        ctx: ToolContext,
        *,
        persist: Callable[[list[str]], Awaitable[None]] | None = None,
    ) -> DeferredToolSurface | None:
        """The surface for ``ctx``'s session, registered for lookup.

        ``None`` (and nothing registered) when the context has no session or
        nothing is deferred; the caller then sends the full tool list.
        """

        key = _key(ctx.session_key)
        if not key:
            return None
        deferred = {rt.spec.name: rt for rt in registry.deferred_tools(ctx)}
        if not deferred:
            _surfaces.discard(key)
            return None
        loaded = [name for name in dict.fromkeys(loaded_tool_names(key)) if name in deferred]
        surface = cls(
            session_key=key,
            registry=registry,
            ctx=ctx,
            deferred=deferred,
            loaded=loaded,
            persist=persist,
        )
        _surfaces.set(key, surface)
        return surface

    # -- queries ------------------------------------------------------------

    @property
    def deferred_names(self) -> list[str]:
        return list(self.deferred)

    def is_pending(self, name: str) -> bool:
        """Deferred for this session and not loaded yet."""

        return name in self.deferred and name not in self.loaded

    def tool_definitions(self) -> list[ToolDefinition]:
        """The tool list for this session's next provider request."""

        return self.registry.request_tool_definitions(self.ctx, loaded=self.loaded)

    def search(
        self,
        query: str = "",
        names: Iterable[str] | None = None,
        *,
        limit: int = _DEFAULT_SEARCH_LIMIT,
    ) -> list[RegisteredTool]:
        """Deferred tools matching ``names`` (exact) and then ``query`` (keywords).

        Keyword scoring is deliberately plain: a query term found in the tool
        name weighs three, in its description one, and the whole query as a
        name substring adds five. The catalog is a few dozen entries the model
        has already read in the ``tool_search`` description, so the query only
        has to break ties, not discover anything.
        """

        limit = max(1, min(int(limit or _DEFAULT_SEARCH_LIMIT), _MAX_SEARCH_LIMIT))
        picked: list[RegisteredTool] = []
        for raw in names or ():
            name = str(raw or "").strip()
            rt = self.deferred.get(name)
            if rt is not None and rt not in picked:
                picked.append(rt)

        terms = _terms(query)
        if terms:
            needle = "_".join(terms)
            scored: list[tuple[int, str, RegisteredTool]] = []
            for name, rt in self.deferred.items():
                if rt in picked:
                    continue
                name_terms = set(_terms(name))
                desc_terms = set(_terms(rt.spec.description))
                score = 0
                for term in terms:
                    if term in name_terms:
                        score += 3
                    elif any(t.startswith(term) for t in name_terms):
                        score += 2
                    if term in desc_terms:
                        score += 1
                if needle and needle in name:
                    score += 5
                if score:
                    scored.append((-score, name, rt))
            scored.sort(key=lambda item: (item[0], item[1]))
            room = max(0, limit - len(picked))
            picked.extend(rt for _, _, rt in scored[:room])
        return picked

    # -- mutation -----------------------------------------------------------

    def bind(self, tool_definitions: list[Any], by_name: dict[str, Any] | None = None) -> None:
        """Track a running agent's tool list so loads reach its next request."""

        if not any(existing is tool_definitions for existing, _ in self._bound):
            self._bound.append((tool_definitions, by_name))

    async def load(self, names: Iterable[str]) -> list[str]:
        """Load deferred tools into the session. Returns the newly loaded names.

        Unknown, forbidden and already-loaded names are ignored: the deferred
        set is the policy-filtered one, so nothing outside it can be loaded.
        """

        newly = [name for name in dict.fromkeys(str(n) for n in names) if self.is_pending(name)]
        if not newly:
            return []
        self.loaded.extend(newly)
        _loaded.set(self.session_key, list(self.loaded))
        for tool_list, by_name in self._bound:
            present = {getattr(item, "name", None) for item in tool_list}
            for name in newly:
                if name in present:
                    continue
                definition = self.registry.definition_for(self.deferred[name], self.ctx)
                tool_list.append(definition)
                if by_name is not None:
                    by_name[name] = definition
        log.info(
            "deferred_tools.loaded",
            session_key=self.session_key,
            tools=newly,
            loaded_count=len(self.loaded),
        )
        if self.persist is not None:
            try:
                await self.persist(list(self.loaded))
            except Exception as exc:  # noqa: BLE001 - the load still holds for this process
                log.warning(
                    "deferred_tools.persist_failed",
                    session_key=self.session_key,
                    error=str(exc),
                )
        return newly

    def guard(
        self,
        handler: Callable[[ToolCall], Awaitable[ToolResult]],
    ) -> Callable[[ToolCall], Awaitable[ToolResult]]:
        """Wrap a tool handler so an unloaded deferred tool gets a clear error.

        The registry would happily run it — dispatch looks tools up by name —
        but the model only knows a deferred tool's schema once it is loaded,
        so a call before that is a guess at the arguments. The error tells it
        how to load the tool instead of calling it unknown.
        """

        surface = self

        async def _guarded(tool_call: ToolCall) -> ToolResult:
            if surface.is_pending(tool_call.tool_name):
                return not_loaded_result(tool_call)
            if surface.registry.get(tool_call.tool_name) is None:
                # Dispatch resolves a near-miss name; one that lands on an
                # unloaded deferred tool must hit this guard too.
                from agentos.tools.tool_name_resolution import resolve_tool_name

                resolution = resolve_tool_name(tool_call.tool_name, surface.registry, surface.ctx)
                if resolution is not None and surface.is_pending(resolution.resolved):
                    return not_loaded_result(replace(tool_call, tool_name=resolution.resolved))
            return await handler(tool_call)

        _guarded.deferred_tool_surface = surface  # type: ignore[attr-defined]
        return _guarded


def not_loaded_result(tool_call: ToolCall) -> ToolResult:
    """The error a call to a deferred tool that is not loaded yet gets back."""

    from agentos.execution_status import normalize_execution_status
    from agentos.tool_boundary import ToolResult
    from agentos.tools.envelope import build_tool_failure_envelope

    name = tool_call.tool_name
    message = (
        f"Tool '{name}' is installed but not loaded in this session. "
        f'Call {TOOL_SEARCH_NAME}(names=["{name}"]) to load it, then call {name} again.'
    )
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=name,
        content=json.dumps(
            build_tool_failure_envelope(
                LookupError("deferred tool not loaded"),
                name,
                policy_denial=True,
                error_class_override="ToolNotLoaded",
                user_message_override=message,
            )
        ),
        is_error=True,
        execution_status=normalize_execution_status(
            {
                "version": 1,
                "status": "error",
                "exit_code": None,
                "timed_out": False,
                "truncated": False,
                "reason": "denied",
                "source": "tool_runtime",
                "preservation_class": "diagnostic",
            }
        ),
    )


def defer_loading_enabled(config: Any) -> bool:
    """``tools.defer_loading`` from the gateway config; on unless set false."""

    tools_config = getattr(config, "tools", None) if config is not None else None
    return getattr(tools_config, "defer_loading", True) is not False


def surface_for_turn(
    registry: ToolRegistry,
    ctx: ToolContext | None,
    *,
    config: Any = None,
    session_manager: Any = None,
) -> DeferredToolSurface | None:
    """Build the turn's surface, or ``None`` to send every tool as before.

    ``None`` when ``tools.defer_loading`` is off, there is no tool context or
    session, or the session's policy leaves nothing to defer.
    """

    if ctx is None or not defer_loading_enabled(config):
        forget_surface(ctx.session_key if ctx is not None else None)
        return None
    key = _key(ctx.session_key)
    if not key:
        return None
    persist = (
        persist_loaded_with(session_manager, key, ctx.artifact_session_id)
        if session_manager is not None
        else None
    )
    return DeferredToolSurface.build(registry, ctx, persist=persist)


def forget_surface(session_key: str | None) -> None:
    """Drop the registered surface (deferral off for this turn)."""

    key = _key(session_key)
    if key:
        _surfaces.discard(key)


def bind_agent_tools(tool_handler: Any, agent: Any) -> None:
    """Point a turn's deferred-tool surface at the agent that runs the turn.

    The agent keeps its own tool list (the pipeline may have copied the one the
    runner built), so the surface appends there. No-op when the handler is not
    a deferred-tool guard.
    """

    surface = getattr(tool_handler, "deferred_tool_surface", None)
    if not isinstance(surface, DeferredToolSurface):
        return
    tool_definitions = getattr(agent, "tool_definitions", None)
    if not isinstance(tool_definitions, list):
        return
    by_name = getattr(agent, "_tool_definition_by_name", None)
    surface.bind(tool_definitions, by_name if isinstance(by_name, dict) else None)


async def load_for_current_session(names: Iterable[str]) -> list[str]:
    """Load deferred tools into the session the current tool call belongs to.

    For tools other than ``tool_search`` that know which tools come next —
    ``skill_view`` loading a skill's ``requires_tools``. Returns the newly
    loaded names; empty when there is no surface or nothing was pending.
    """

    from agentos.tools.types import current_tool_context

    ctx = current_tool_context.get()
    surface = surface_for(ctx.session_key if ctx is not None else None)
    if surface is None:
        return []
    return await surface.load(names)


__all__ = [
    "DEFERRED_BUILTIN_TOOLS",
    "LOADED_STATE_KIND",
    "TOOL_SEARCH_NAME",
    "DeferredToolSurface",
    "bind_agent_tools",
    "defer_loading_enabled",
    "deferred_tool_names",
    "forget_session",
    "forget_surface",
    "hydrate_loaded_tools",
    "load_for_current_session",
    "loaded_tool_names",
    "not_loaded_result",
    "persist_loaded_with",
    "prompt_tool_names",
    "render_tool_search_description",
    "summarize_description",
    "surface_for",
    "surface_for_turn",
]
