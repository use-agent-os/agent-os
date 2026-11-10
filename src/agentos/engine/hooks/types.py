"""Hook protocol definitions for Agent + TurnRunner lifecycle.

These Protocols are structural — implementations do not need to inherit. A
concrete hook just needs to provide the named methods. Default no-op hooks live
in :mod:`agentos.engine.hooks.defaults`.

Value objects passed to hooks are intentionally narrow and frozen so a hook
cannot accidentally mutate caller state. Mutation paths must be explicit
(returning a value or going through a side-effect-bearing hook method).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agentos.observability.trace import TraceContext
    from agentos.tool_boundary import ToolCall, ToolResult

# ---------------------------------------------------------------------------
# Turn lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnHookContext:
    """Per-turn context handed to every TurnHook method.

    ``trace_context`` is optional because a turn may run without a trace context
    (e.g. unit tests). ``extra`` is a free-form bag of metadata; producers and
    consumers agree on field names out-of-band so the protocol stays loose.
    """

    session_key: str
    agent_id: str
    turn_id: str | None = None
    run_kind: str | None = None
    input_mode: str | None = None
    trace_context: TraceContext | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnEvent:
    """A trace-bearing turn event emitted by the runtime.

    ``kind`` mirrors the ``_write_trace_event`` ``kind`` argument
    (``turn_start``, ``turn_end``, ``turn_error``, ``turn_cancelled``).
    """

    kind: str
    seq: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnHookResult:
    """Result of a turn handed to ``TurnHook.after_turn``.

    Fields carry the data the transcript-persist path needs:
    final assistant text, structured turn segments, published artifacts, and
    any terminal error message captured during streaming.
    """

    final_text: str = ""
    turn_segments: list[dict] = field(default_factory=list)
    turn_artifacts: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    done_event: Any | None = None
    runtime_message: str = ""
    tool_context: Any | None = None
    input_provenance: dict[str, Any] | None = None
    no_memory_capture: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TurnHook(Protocol):
    """Lifecycle observer for a single turn.

    Implementations should be cheap and side-effect-isolated; one slow hook
    must not stall the turn loop. The runtime invokes hooks in registration
    order and swallows exceptions raised by hooks (logging at WARN) so a buggy
    hook cannot break a turn.
    """

    name: str

    async def before_turn(self, ctx: TurnHookContext) -> None:
        """Fire once at the very start of a turn."""

    async def after_turn(self, ctx: TurnHookContext, result: TurnHookResult) -> None:
        """Fire after the turn finishes successfully."""

    async def on_error(self, ctx: TurnHookContext, exc: BaseException) -> None:
        """Fire when the turn terminates with an exception."""

    def on_event(self, ctx: TurnHookContext, event: TurnEvent) -> None:
        """Fire for each structured turn event (synchronous, observability only)."""


# ---------------------------------------------------------------------------
# Tool dispatch surround
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolHookCall:
    """The call presented to ``ToolHook.before_tool`` and ``after_tool``.

    Carries the ``ToolCall`` plus the resolved :class:`ToolContext` so hooks
    can correlate to a session/agent without re-walking the call. Each hook
    gets its own copy of the arguments: editing them in place changes nothing.
    To run the call with other arguments, return :class:`Rewrite`.
    """

    tool_call: ToolCall
    ctx: Any | None  # ToolContext | None — kept loose to avoid an import cycle


@dataclass(frozen=True)
class ToolHookResult:
    """The outcome presented to ``ToolHook.after_tool``.

    ``result`` is the settled :class:`ToolResult` the model and transcript
    will see unless a hook replaces it: its ``content`` is always a string,
    with ``is_error`` and ``execution_status`` already set. It is also set
    when the call never ran -- a block, a refused rewrite, a policy denial or
    a run-budget skip -- and then holds that error or control envelope.
    Earlier hooks' replacements are already applied.

    ``exception`` is set when the tool handler raised (``result`` is then the
    error envelope built from it). ``raw_result`` is what the handler itself
    returned before finalization, ``None`` when it did not run or raised.

    ``result`` is ``None`` only when there is no result to replace: the turn
    was cancelled while the tool ran (``exception`` is the ``CancelledError``),
    or a dry run (``tools.invoke`` with ``dry_run``) found the call would run.
    """

    result: ToolResult | None = None
    exception: BaseException | None = None
    raw_result: Any = None


@dataclass(frozen=True)
class Allow:
    """``before_tool`` decision: run the call as it is (same as returning None)."""


@dataclass(frozen=True)
class Block:
    """``before_tool`` decision: do not run the call; the model sees ``reason``."""

    reason: str


@dataclass(frozen=True)
class Rewrite:
    """``before_tool`` decision: run the call with these arguments instead.

    The rewritten arguments are validated against the tool's schema again and
    still go through the policy chain and approval: a rewrite cannot bypass
    either.
    """

    arguments: dict[str, Any]


@dataclass(frozen=True)
class ReplaceResult:
    """``after_tool`` decision: replace the result the model and transcript see.

    The replacement keeps the call's ``tool_use_id``; ``is_error=None`` keeps
    the original error flag. It applies on every path ``after_tool`` runs on,
    denials included, and is held to the same single-result size cap as a
    tool's own output. When it flips the error flag, the execution status is
    reset to match (reason ``hook_replaced``).
    """

    content: str
    is_error: bool | None = None


ToolHookDecision = Allow | Block | Rewrite


# ---------------------------------------------------------------------------
# Provider request surround
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestHookCall:
    """What ``RequestHook.before_request`` sees about the next provider call."""

    messages: tuple[Any, ...]  # the request's provider Messages, read-only
    session_key: str | None
    iteration: int
    attempt: int


@dataclass(frozen=True)
class Amend:
    """``before_request`` decision: add a request-scoped context note.

    The note goes at the end of the request, after the cache-stable prefix,
    and is not persisted to the transcript.
    """

    context: str


@dataclass(frozen=True)
class Veto:
    """``before_request`` decision: do not call the provider; fail the turn."""

    reason: str


@runtime_checkable
class RequestHook(Protocol):
    """Runs before every provider request of the agent loop.

    Returns ``None`` (send as is), :class:`Amend` or :class:`Veto`. A hook that
    raises is logged and ignored unless it sets ``fail_closed = True``, in which
    case its failure vetoes the request.
    """

    name: str

    def before_request(self, call: RequestHookCall) -> Amend | Veto | None: ...


@runtime_checkable
class ToolHook(Protocol):
    """Surround a single tool dispatch; observe it or decide on it.

    ``before_tool`` runs after argument admission and before the policy chain.
    It may return ``None`` / :class:`Allow` (run), :class:`Block` (do not run)
    or :class:`Rewrite` (run with other arguments). Hooks run in registration
    order; the first block wins. ``after_tool`` runs after result finalization,
    sees the settled :class:`ToolResult` and may return :class:`ReplaceResult`.
    Every hook whose ``before_tool`` ran gets ``after_tool`` exactly once, also
    when the call was blocked, refused or denied before it ran.

    A hook that raises is logged and treated as "no decision", unless it sets
    ``fail_closed = True``: then a failing ``before_tool`` blocks the call, and
    a failing ``after_tool`` withholds the result (the model gets an error in
    its place). Hooks written against the observe-only interface (returning
    ``None``) keep working unchanged.
    """

    name: str

    def before_tool(self, call: ToolHookCall) -> ToolHookDecision | None: ...

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> ReplaceResult | None: ...


# ---------------------------------------------------------------------------
# Compaction lifecycle. This hook is active only when supplied to TurnRunner.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionState:
    """Snapshot of compaction inputs handed to ``CompactionHook.before_compact``.

    The runtime supplies the stable fields available at the pre-turn
    compaction call site. Hooks that need richer compaction telemetry should
    read it from ``extra`` when a specific producer documents that field.
    """

    session_key: str
    agent_id: str
    total_tokens: int = 0
    threshold_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CompactionHook(Protocol):
    name: str

    async def before_compact(self, state: CompactionState) -> None: ...

    async def after_compact(self, state: CompactionState, outcome: Any) -> None: ...
