"""A provider that plays back scripted turns, for deterministic harness tests.

Each provider request consumes the next scripted turn. A turn is a list of
steps, emitted in order:

* provider stream events built with :func:`text`, :func:`thinking`,
  :func:`tool_call`, :func:`done` and :func:`error`;
* :class:`Raise` -- raise an exception from the stream at that point;
* :class:`Hang` -- block (for ``seconds``, or until cancelled) at that point,
  to exercise timeouts and cancellation.

Requests past the end of the script replay the last turn, so a test that
expects "retry until the budget runs out" can script the failure once.

Example::

    provider = ScriptedProvider(
        [
            [tool_call("read", {"path": "a"}), done("tool_use")],
            [text("all done"), done()],
        ]
    )
    events = [e async for e in Agent(provider=provider).run_turn("go")]
    assert len(provider.calls) == 2
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentos.provider import ChatConfig, Message, StopReason
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import ErrorEvent as ProviderError
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.provider.types import ThinkingDeltaEvent as ProviderThinking

_tool_ids = itertools.count(1)


@dataclass(frozen=True)
class Raise:
    """Raise *exc* from the provider stream when this step is reached."""

    exc: BaseException


@dataclass(frozen=True)
class Hang:
    """Block the stream: ``seconds=None`` waits until the consumer cancels."""

    seconds: float | None = None


@dataclass
class CallRecord:
    """One provider request as the harness made it."""

    messages: list[Message]
    tools: list[Any] | None
    config: ChatConfig | None
    #: Set once the stream for this request was closed before it finished
    #: (the consumer cancelled it or stopped iterating).
    closed_early: bool = False


def text(value: str) -> ProviderText:
    return ProviderText(text=value)


def thinking(value: str) -> ProviderThinking:
    return ProviderThinking(text=value)


def tool_call(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    tool_use_id: str | None = None,
) -> list[Any]:
    """The start/end event pair of one complete tool call.

    Returns a list; turns are flattened, so it can sit among other steps.
    """

    call_id = tool_use_id or f"call_{next(_tool_ids)}"
    return [
        ProviderToolUseStart(tool_use_id=call_id, tool_name=name),
        ProviderToolUseEnd(tool_use_id=call_id, tool_name=name, arguments=dict(arguments or {})),
    ]


def done(
    stop: str = "end_turn",
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    **kwargs: Any,
) -> ProviderDone:
    return ProviderDone(
        stop_reason=StopReason(stop),
        raw_stop_reason=stop,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        **kwargs,
    )


def error(
    code: str,
    message: str | None = None,
    *,
    retry_after: float | None = None,
) -> ProviderError:
    return ProviderError(
        code=code,
        message=message or f"HTTP {code}: scripted failure",
        retry_after_seconds=retry_after,
    )


def _flatten(steps: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for step in steps:
        if isinstance(step, list):
            out.extend(_flatten(step))
        else:
            out.append(step)
    return out


@dataclass
class ScriptedProvider:
    """An ``LLMProvider`` stand-in that replays *turns* in order."""

    turns: list[list[Any]]
    provider_name: str = "scripted"
    calls: list[CallRecord] = field(default_factory=list)
    fallback_reasons: list[str] = field(default_factory=list)
    #: What ``fallback_after_invalid_response`` answers.
    offers_fallback: bool = False

    def __post_init__(self) -> None:
        if not self.turns:
            raise ValueError("ScriptedProvider needs at least one scripted turn")
        self.turns = [_flatten(turn) for turn in self.turns]

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        record = CallRecord(messages=list(messages), tools=tools, config=config)
        index = min(len(self.calls), len(self.turns) - 1)
        self.calls.append(record)
        return self._stream(self.turns[index], record)

    async def _stream(self, steps: list[Any], record: CallRecord) -> AsyncIterator[Any]:
        finished = False
        try:
            for step in steps:
                if isinstance(step, Raise):
                    raise step.exc
                if isinstance(step, Hang):
                    if step.seconds is None:
                        await asyncio.Event().wait()
                    else:
                        await asyncio.sleep(step.seconds)
                    continue
                yield step
            finished = True
        finally:
            if not finished:
                record.closed_early = True

    def fallback_after_invalid_response(self, reason: str) -> bool:
        self.fallback_reasons.append(reason)
        return self.offers_fallback

    async def list_models(self) -> list[Any]:
        return []
