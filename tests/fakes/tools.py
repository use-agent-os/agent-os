"""Fake tools with test-controlled timing and failures.

``FakeToolbox`` hands an ``Agent`` everything it needs to run tools without the
real registry: ``tool_definitions``, a ``tool_handler`` and a minimal
``registry`` that only answers the concurrency lookup. Each :class:`FakeTool`
returns a fixed result after an optional delay, raises, or ignores
cancellation, and the toolbox records when every call started and finished.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from agentos.engine import ToolResult
from agentos.provider import ToolDefinition, ToolInputSchema
from agentos.tool_boundary import ToolCall
from agentos.tools.types import ToolSpec

Concurrency = Literal["exclusive", "parallel"]


@dataclass
class FakeTool:
    name: str
    result: str = "ok"
    #: Seconds the handler sleeps before answering.
    delay: float = 0.0
    #: Raised from the handler (after ``delay``) when set.
    raises: BaseException | None = None
    #: Keep running for ``delay`` seconds even when the call is cancelled.
    ignores_abort: bool = False
    concurrency: Concurrency = "exclusive"
    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)


@dataclass
class _Registered:
    spec: ToolSpec


class FakeToolbox:
    """Tools, a handler and a lookup-only registry for ``Agent``."""

    def __init__(self, *tools: FakeTool) -> None:
        self.tools = {tool.name: tool for tool in tools}
        #: ``(event, tool_use_id)`` in the order things happened:
        #: ``start``, ``finish``, ``raise`` or ``cancelled``.
        self.timeline: list[tuple[str, str]] = []
        self.calls: list[ToolCall] = []

    @property
    def tool_definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=tool.name,
                description=f"fake tool {tool.name}",
                input_schema=ToolInputSchema(properties=tool.properties, required=tool.required),
            )
            for tool in self.tools.values()
        ]

    def _spec(self, tool: FakeTool) -> ToolSpec:
        return ToolSpec(
            name=tool.name,
            description=f"fake tool {tool.name}",
            parameters=tool.properties,
            required=tool.required,
            concurrency=tool.concurrency,
        )

    @property
    def registry(self) -> Any:
        specs = {tool.name: _Registered(self._spec(tool)) for tool in self.tools.values()}
        return SimpleNamespace(get=specs.get)

    def agent_kwargs(self, *, dispatch: bool = False) -> dict[str, Any]:
        """Keyword arguments to splat into ``Agent(...)``.

        By default the agent calls :meth:`handle` directly, which keeps tool
        timing exact. With ``dispatch=True`` every call goes through the real
        ``tools.dispatch.build_tool_handler`` over a real ``ToolRegistry``
        (argument admission, policy chain, result finalisation), the way the
        gateway runs tools; the timeline then keys calls by tool name.
        """

        if not dispatch:
            return {
                "tool_definitions": self.tool_definitions,
                "tool_handler": self.handle,
                "tool_registry": self.registry,
            }
        from agentos.tools.dispatch import build_tool_handler  # noqa: PLC0415
        from agentos.tools.registry import ToolRegistry  # noqa: PLC0415

        registry = ToolRegistry()
        for tool in self.tools.values():
            registry.register(self._spec(tool), self._kwargs_handler(tool.name))
        return {
            "tool_definitions": self.tool_definitions,
            "tool_handler": build_tool_handler(registry, None),
            "tool_registry": registry,
        }

    def _kwargs_handler(self, name: str) -> Any:
        async def _handler(**arguments: Any) -> str:
            result = await self.handle(
                ToolCall(tool_use_id=name, tool_name=name, arguments=arguments)
            )
            return result.content

        return _handler

    def events_for(self, tool_use_id: str) -> list[str]:
        return [event for event, call_id in self.timeline if call_id == tool_use_id]

    async def handle(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        tool = self.tools[call.tool_name]
        self.timeline.append(("start", call.tool_use_id))
        try:
            if tool.delay:
                if tool.ignores_abort:
                    await _sleep_through_cancel(tool.delay)
                else:
                    await asyncio.sleep(tool.delay)
        except asyncio.CancelledError:
            self.timeline.append(("cancelled", call.tool_use_id))
            raise
        if tool.raises is not None:
            self.timeline.append(("raise", call.tool_use_id))
            raise tool.raises
        self.timeline.append(("finish", call.tool_use_id))
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=tool.result
        )


async def _sleep_through_cancel(seconds: float) -> None:
    """Sleep *seconds* even if cancelled meanwhile; re-raise the cancel after."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    cancelled = False
    while (remaining := deadline - loop.time()) > 0:
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
