"""The agent loop: request, stream, decide, run tools, repeat.

``run_agent_loop`` owns the order of one turn's iterations and nothing else.
Every step is a callback on a frozen :class:`LoopConfig`, so the loop itself
holds no provider, storage or gateway code, and the agent (the facade that
builds the config) supplies what each step does:

1. ``before_request``: per-iteration guards (iteration limit, turn deadline,
   cooperative abort) and the reset of the iteration's state.
2. ``stream``: one model response, including every retry, failover and
   compaction the attempt policy decides on.
3. ``commit_response``: check the final response and append it to the
   transcript.
4. ``on_final_answer``: the response called no tools. It decides between
   ending the turn and taking a steering message for another iteration.
5. ``execute_tools``: run the response's tool calls.
6. ``after_tools``: the guards after a tool batch (budgets, loop and sandbox
   checks), feeding the results back, steering, and whether the batch ends
   the turn.

A step stops the turn by setting ``state.stop`` (or, from ``on_final_answer``,
asks for one more iteration with ``state.next_iteration``). Exceptions are
not caught here: cancellation, the turn deadline and a cooperative abort
reach the caller, which seals the turn.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass

from agentos.engine.types import AgentEvent, ErrorEvent, ToolCall


class LoopState:
    """What the loop reads between steps; the facade's turn state extends it."""

    def __init__(self) -> None:
        #: Iterations started so far (``before_request`` counts them).
        self.iterations = 0
        #: Tool calls of the response ``commit_response`` committed.
        self.tool_calls: list[ToolCall] = []
        #: The error that ended the turn, if one did.
        self.terminal_error: ErrorEvent | None = None
        #: Set by a step to end the turn after it returns.
        self.stop = False
        #: Set by ``on_final_answer`` to run another iteration.
        self.next_iteration = False


#: One loop step: an async generator over the turn state.
type Step[S: LoopState] = Callable[[S], AsyncGenerator[AgentEvent, None]]


@dataclass(frozen=True)
class LoopConfig[S: LoopState]:
    """The steps of one turn, in the order :func:`run_agent_loop` runs them."""

    before_request: Step[S]
    stream: Step[S]
    commit_response: Step[S]
    on_final_answer: Step[S]
    execute_tools: Step[S]
    after_tools: Step[S]


async def run_agent_loop[S: LoopState](
    state: S, config: LoopConfig[S]
) -> AsyncGenerator[AgentEvent, None]:
    """Run iterations until a step stops the turn; yield every step's events.

    Each step runs under ``aclosing``: when the consumer closes the loop, the
    step that is running is closed right away (its ``finally`` blocks run
    before ``aclose`` returns), as if its code were inline in the loop.
    """
    while True:
        state.next_iteration = False
        for step in (config.before_request, config.stream, config.commit_response):
            async with aclosing(step(state)) as events:
                async for event in events:
                    yield event
            if state.stop:
                return
        if not state.tool_calls:
            async with aclosing(config.on_final_answer(state)) as events:
                async for event in events:
                    yield event
            if state.next_iteration and not state.stop:
                continue
            return
        for step in (config.execute_tools, config.after_tools):
            async with aclosing(step(state)) as events:
                async for event in events:
                    yield event
            if state.stop:
                return


__all__ = ["LoopConfig", "LoopState", "Step", "run_agent_loop"]
