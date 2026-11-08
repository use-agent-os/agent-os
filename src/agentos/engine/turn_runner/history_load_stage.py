"""Stage object for the before-turn transcript history load.

Owns the per-turn slice between the agent-bootstrap stage boundary (Agent
construction) and the stream-consumer stage boundary (the
``async for event in agent.run_turn(...)`` loop). The harness invokes
``HistoryLoadStage.run`` once per turn, AFTER
AgentBootstrapStage and BEFORE the attachment-message build +
stream-consumer loop.

Compaction does not run here. Every compaction trigger goes through
:class:`agentos.engine.compaction_coordinator.CompactionCoordinator`, which
checks the live request before every provider call -- including the first
one of the turn -- and persists through the in-turn ``CompactionEvent`` path
(``persist_compaction_result`` + the durable checkpoint receipt). The stage
was called ``CompactionAndHistoryStage`` while it still compacted; its
turn-timing key stays ``compaction_and_history`` so existing diagnostics
keep their stage name.

Side-effect contract:

- ``history_loader.load`` mutates ``agent._history`` via
  ``agent.set_history`` and returns a (possibly ``None``) durable
  compaction-summary context string.
- ``request_context_prepender.prepend`` is a pure string function whose
  return value the harness applies as
  ``agent.config.request_context_prompt = ...``.

NEVER terminates. Always returns ``StageOutcome.success(...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agentos.engine.agent import Agent
    from agentos.engine.turn_runner.outcome import StageOutcome

# ---------------------------------------------------------------------------
# Ports -- two narrow Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class HistoryLoaderPort(Protocol):
    """Wraps ``TurnRunner._load_history``.

    Reads the persisted transcript, reconstructs per-entry messages,
    and mutates ``agent._history`` via ``agent.set_history(history)``
    when any history was reconstructed.

    Returns ``str | None`` — the formatted compaction-summary context
    if durable summaries existed, else ``None``. The stage body feeds
    the return value into the prepender port.

    NOT a compaction port; does NOT fire ``CompactionHook``. History
    load is a read of already-persisted (possibly already-compacted)
    state.
    """

    async def load(
        self,
        *,
        agent: Agent,
        session_key: str,
        trim_last_user: bool,
        input_message_id: str | None = None,
    ) -> str | None: ...


@runtime_checkable
class RequestContextPrependPort(Protocol):
    """Wraps ``_prepend_request_context_prompt`` (module-level pure helper).

    Pure string function. Returns the prepended context (or the existing
    value unchanged when there is nothing to prepend). The harness
    applies the result with ``agent.config.request_context_prompt =
    ...`` after the stage returns; the stage itself does NOT mutate the
    agent.

    Reason for promoting a small pure function to a port: testability.
    A recording fake lets us assert "prepender was called with exactly
    these two arguments" without parsing log lines.
    """

    def prepend(
        self,
        *,
        existing: str | None,
        prepended: str | None,
    ) -> str | None: ...


@runtime_checkable
class TurnStartObserverPort(Protocol):
    """Told right before a turn loads its history.

    Wraps ``ProactiveCompactionScheduler.before_history_load``: it invalidates
    a between-turns compaction job that has not persisted yet and waits for
    one that is persisting, so the load never sees a half-written rewrite.
    """

    async def before_history_load(
        self, *, session_key: str, agent: Agent, agent_id: str
    ) -> None: ...


# ---------------------------------------------------------------------------
# Stage I/O dataclasses (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryLoadStageInput:
    """Inputs the stage needs at its boundary.

    ``agent`` comes from AgentBootstrapStage; ``session_key``, ``agent_id``
    and ``history_has_persisted_user`` come from the ``_run_turn`` call site.
    """

    agent: Agent
    session_key: str
    agent_id: str
    history_has_persisted_user: bool
    # The persisted user row this turn answers, when the caller knows it.
    input_message_id: str | None = None


@dataclass(frozen=True)
class HistoryLoadStageOutput:
    """The pieces of state subsequent stages and the harness consume.

    - ``compaction_summary_context``: the string passed to the
      prepender. ``None`` when no durable summaries exist.
    - ``final_request_context_prompt``: the result of the prepend.
      The harness applies this to
      ``agent.config.request_context_prompt`` after the stage returns.
      Surfaced (not applied inside the stage) so the harness remains
      the only place that mutates ``agent.config``.
    """

    compaction_summary_context: str | None
    final_request_context_prompt: str | None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class HistoryLoadStage:
    """Load history, prepend durable compaction context.

    Stable boundary: runs ONCE per turn, after ``AgentBootstrapStage``
    and before the attachment-build + stream-consumer steps:

    0. ``turn_start_observer.before_history_load`` (when wired).
    1. ``history_loader.load`` (always called).
    2. ``request_context_prepender.prepend`` (always called; pure).

    Exception model: history/prepender exceptions propagate to the outer
    terminal handler in ``_run_turn``.
    """

    name = "history_load_stage"

    def __init__(
        self,
        *,
        history_loader: HistoryLoaderPort,
        request_context_prepender: RequestContextPrependPort,
        turn_start_observer: TurnStartObserverPort | None = None,
    ) -> None:
        self._history_loader = history_loader
        self._request_context_prepender = request_context_prepender
        self._turn_start_observer = turn_start_observer

    async def run(
        self,
        inp: HistoryLoadStageInput,
    ) -> StageOutcome[HistoryLoadStageOutput]:
        # Local import keeps the module import-cycle-free.
        from agentos.engine.turn_runner.outcome import StageOutcome

        # 0. A between-turns compaction must not rewrite the history under us.
        if self._turn_start_observer is not None:
            await self._turn_start_observer.before_history_load(
                session_key=inp.session_key,
                agent=inp.agent,
                agent_id=inp.agent_id,
            )

        # 1. Load history (transcript + structured/legacy messages + durable summary).
        load_kwargs: dict[str, Any] = {}
        if inp.input_message_id:
            load_kwargs["input_message_id"] = inp.input_message_id
        compaction_summary_context = await self._history_loader.load(
            agent=inp.agent,
            session_key=inp.session_key,
            trim_last_user=inp.history_has_persisted_user,
            **load_kwargs,
        )

        # 2. Prepend compaction summary context to request_context_prompt (pure).
        final_request_context_prompt = self._request_context_prepender.prepend(
            existing=inp.agent.config.request_context_prompt,
            prepended=compaction_summary_context,
        )

        return StageOutcome.success(
            HistoryLoadStageOutput(
                compaction_summary_context=compaction_summary_context,
                final_request_context_prompt=final_request_context_prompt,
            )
        )
