"""Overlap the independent I/O that runs before a turn's first provider request.

Data dependencies between the pre-provider stages of ``TurnRunner._run_turn``
(what each one reads -> what it produces):

* ``InputStage``: message, tool_context -> runtime_message, semantic_input,
  extra_prompt_context, persisted_entry. It may append the user row to the
  transcript, so every transcript read below must start after it.
* memory provider recall (``prefetch_all``): runtime_message, session_id ->
  the ``Memory Context`` block of extra_prompt_context. Network bound.
* project knowledge: session_key -> the ``Project Knowledge`` block of
  extra_prompt_context. One session + one project row read.
* ``hydrate_loaded_tools``: session_key -> the process-wide deferred-tool set
  that ``ProviderAndToolsStage`` reads, so it must finish before that stage.
* ``ProviderAndToolsStage``: tool_context -> provider, selector, tool_defs.
* router context (``_router_previous_assistant_context``): the persisted
  transcript -> prev assistant text / recent user texts for the pipeline.
  Needs nothing produced after ``InputStage``.
* ``PromptAssemblerStage``: tool_defs, extra_prompt_context, router context ->
  turn, final prompt. ``_assemble_prompt`` reads ``_memory_snapshots``, so the
  snapshot capture must stay after it.
* ``AgentBootstrapStage``: turn, final prompt -> Agent. Its memory step is two
  parts: ``sync_manager.warm_session`` (index sync on the first turn of a
  session; independent of everything above) and the MEMORY.md snapshot capture
  (must stay after ``_assemble_prompt``).
* ``HistoryLoadStage``: agent -> history (transcript read).
* ``AttachmentStage``: post-pipeline message, attachments -> extra messages.

So recall, project knowledge, hydrate, the router-context read and the memory
warm are mutually independent and can all start right after ``InputStage``;
the warm only has to be done before the snapshot capture. (The one visible
difference: a turn that fails before ``AgentBootstrapStage``, e.g. "no
provider", still warms the index. The warm is idempotent per session and the
next turn would run it anyway.) :class:`PreProviderIO` starts those as tasks and hands the
results back at the exact point the serial code used them, in the same order,
so the assembled prompt is unchanged. With ``concurrent=False`` it runs each
piece lazily at its old call site, i.e. the previous serial behaviour.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Hashable
from functools import partial
from typing import Any, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")

MEMORY_CONTEXT_KEY = "Memory Context"
PROJECT_KNOWLEDGE_KEY = "Project Knowledge"


def merge_extra_context_block(
    extra_context: dict[str, str] | None,
    key: str,
    block: str | None,
) -> dict[str, str] | None:
    """Return ``extra_context`` with ``block`` stored under ``key``.

    Returns the input object unchanged when there is nothing to add, and a new
    dict otherwise (callers rely on both: identity for "nothing injected",
    a copy so the caller's dict is never mutated).
    """
    if not block:
        return extra_context
    merged = dict(extra_context) if extra_context else {}
    merged[key] = block
    return merged


def _consume_background_failure(task: asyncio.Task[Any], *, name: str) -> None:
    """Retrieve a detached task's exception so it is logged, not warned about."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.debug("turn_runner.pre_provider_task_failed", task=name, error=str(exc))


class RecallBudget:
    """Hard deadline for provider recall, without cancelling the recall itself.

    A recall that misses the deadline keeps running in the background (its
    store may be mid-search on a worker thread; cancelling the await would not
    stop that anyway). When it finishes, its block is kept for the same
    ``key`` and offered on the next turn if that turn's recall also misses the
    deadline. Providers are specified to serve the previous turn's background
    recall from ``prefetch`` (see ``MemoryProvider.queue_prefetch``), so a
    one-turn-old block is within the contract.

    While a recall for a key is still running, the next turn waits on that one
    instead of starting another, so a stuck provider holds at most one worker
    per session.

    ``key=None`` (no session identity to scope by) keeps only the deadline:
    nothing is shared between calls, so one session's recall can never reach
    another session's prompt.
    """

    def __init__(self, *, max_keys: int = 256) -> None:
        self._max_keys = max_keys
        self._inflight: dict[Hashable, asyncio.Task[str]] = {}
        self._late: OrderedDict[Hashable, str] = OrderedDict()
        self._caching: set[asyncio.Task[str]] = set()

    async def recall(
        self,
        key: Hashable | None,
        fetch: Callable[[], Coroutine[Any, Any, str]],
        *,
        timeout: float,
        log_fields: dict[str, Any] | None = None,
    ) -> str:
        if key is None:
            stale, task = None, None
        else:
            stale = self._late.pop(key, None)
            task = self._inflight.get(key)
        reused = task is not None and not task.done()
        if task is None or task.done():
            task = asyncio.create_task(fetch())
            if key is not None:
                self._inflight[key] = task
            task.add_done_callback(partial(self._forget, key))
        # A reused task that missed an earlier deadline caches its block when
        # it completes; if this turn consumes it, that copy must go.
        will_cache = task in self._caching
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            if will_cache and key is not None:
                self._late.pop(key, None)
            if task.cancelled():
                return ""
            exc = task.exception()
            if exc is not None:
                log.warning(
                    "turn_runner.memory_prefetch_failed",
                    error=str(exc),
                    **(log_fields or {}),
                )
                return ""
            return task.result() or ""
        log.warning(
            "memory.recall_timeout",
            timeout_seconds=timeout,
            reused_inflight=reused,
            stale_fallback=bool(stale),
            **(log_fields or {}),
        )
        if key is not None and task not in self._caching:
            self._caching.add(task)
            task.add_done_callback(partial(self._cache_late, key))
        return stale or ""

    def _forget(self, key: Hashable | None, task: asyncio.Task[str]) -> None:
        if key is not None and self._inflight.get(key) is task:
            del self._inflight[key]
        _consume_background_failure(task, name="memory_recall")

    def _cache_late(self, key: Hashable, task: asyncio.Task[str]) -> None:
        self._caching.discard(task)
        if task.cancelled() or task.exception() is not None:
            return
        block = task.result()
        if not block:
            return
        self._late[key] = block
        self._late.move_to_end(key)
        while len(self._late) > self._max_keys:
            self._late.popitem(last=False)


class PreProviderIO:
    """Runs the independent pre-provider reads concurrently, applies them in order.

    Each piece is a zero-argument coroutine factory. In concurrent mode all of
    them start immediately as tasks; in serial mode nothing starts until the
    accessor for it is awaited, which reproduces the old call order exactly.
    Results are always *applied* in the serial order (recall block, then
    project knowledge), so the extra prompt context is byte-identical.

    Tasks whose turn ends before they are awaited (an early error return, a
    cancelled turn) are read-only or idempotent; they run to completion and
    their errors are logged at debug level.
    """

    def __init__(
        self,
        *,
        concurrent: bool,
        recall: Callable[[], Coroutine[Any, Any, str]] | None,
        project_knowledge: Callable[[], Coroutine[Any, Any, str | None]],
        hydrate_tools: Callable[[], Coroutine[Any, Any, Any]],
        router_context: Callable[[], Coroutine[Any, Any, dict[str, Any]]] | None = None,
        memory_warm: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._concurrent = concurrent
        self._recall = recall
        self._project_knowledge = project_knowledge
        self._hydrate_tools = hydrate_tools
        self._router_context = router_context
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        if concurrent:
            if recall is not None:
                self._start("recall", recall)
            self._start("project_knowledge", project_knowledge)
            self._start("hydrate_tools", hydrate_tools)
            if router_context is not None:
                self._start("router_context", router_context)
            if memory_warm is not None:
                self._start("memory_warm", memory_warm)

    @property
    def concurrent(self) -> bool:
        return self._concurrent

    def _start(self, name: str, factory: Callable[[], Coroutine[Any, Any, T]]) -> asyncio.Task[T]:
        task = asyncio.create_task(factory())
        task.add_done_callback(partial(_consume_background_failure, name=name))
        self._tasks[name] = task
        return task

    async def _take(self, name: str, factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        task = self._tasks.pop(name, None)
        if task is None:
            return await factory()
        result: T = await task
        return result

    async def extra_prompt_context(
        self,
        extra_context: dict[str, str] | None,
    ) -> dict[str, str] | None:
        """Apply the recall block, then the project-knowledge block."""
        if self._recall is not None:
            block = await self._take("recall", self._recall)
            extra_context = merge_extra_context_block(extra_context, MEMORY_CONTEXT_KEY, block)
        knowledge = await self._take("project_knowledge", self._project_knowledge)
        return merge_extra_context_block(extra_context, PROJECT_KNOWLEDGE_KEY, knowledge)

    async def hydrate_tools(self) -> None:
        await self._take("hydrate_tools", self._hydrate_tools)

    def router_context(self) -> Awaitable[dict[str, Any]] | None:
        """The prefetched router-context task, or ``None`` to fetch it in-stage."""
        return self._tasks.pop("router_context", None)

    def memory_warm(self) -> Awaitable[None] | None:
        """The running memory index warm, or ``None`` to warm in-stage."""
        return self._tasks.pop("memory_warm", None)
