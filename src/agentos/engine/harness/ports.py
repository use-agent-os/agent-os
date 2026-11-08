"""Stage ports that need no ``TurnRunner`` state (#3819).

Each stage takes Protocol-shaped ports through its constructor. ``TurnRunner``
implements the ports that need its sessions, memory, config or tool registry
itself (the stages are built with ``port=self``); the four here are complete
on their own, so the runner passes an instance of each.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentos.compat.inspect_utils import accepts_keyword_arg
from agentos.engine.turn_runner.history_load_stage import (
    RequestContextPrependPort,
)
from agentos.engine.turn_runner.prompt_assembler_stage import (
    PromptReportBuilderPort,
)
from agentos.engine.turn_runner.stream_consumer_stage import (
    AgentRunPort,
    MemorySyncNotifyPort,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agentos.engine.agent import Agent
    from agentos.engine.types import AgentEvent
    from agentos.observability.prompt_report import PromptReport


class PromptReportBuilder(PromptReportBuilderPort):
    """Pure shim around the module-level ``build_prompt_report`` helper.

    No runner reference needed — the helper is a free function. The lazy
    import keeps the harness module import-cycle-free.
    """

    def build_prompt_report(
        self,
        *,
        turn_id: str,
        session_key: str,
        session_id: str | None,
        agent_id: str,
        system_prompt: str,
        tool_defs: list[Any],
        metadata: dict[str, Any],
        tool_profile: str | None,
    ) -> PromptReport:
        from agentos.observability.prompt_report import build_prompt_report

        return build_prompt_report(
            turn_id=turn_id,
            session_key=session_key,
            session_id=session_id,
            agent_id=agent_id,
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            metadata=metadata,
            tool_profile=tool_profile,
        )


class RequestContextPrepender(RequestContextPrependPort):
    """Pure shim around the module-level ``_prepend_request_context_prompt``.

    No runner reference needed — the helper is a free function. The
    lazy import keeps the harness module import-cycle-free.
    """

    def prepend(
        self,
        *,
        existing: str | None,
        prepended: str | None,
    ) -> str | None:
        from agentos.engine.runtime import _prepend_request_context_prompt

        return _prepend_request_context_prompt(existing, prepended)


class AgentTurnRunner(AgentRunPort):
    """Bind ``agent.run_turn(turn_input, extra_messages=..., **kwargs)``.

    Folds the ``accepts_keyword_arg(agent.run_turn, "semantic_message")``
    introspection here so the stage body never imports
    ``inspect``. ``semantic_message`` is forwarded only when the agent
    accepts the keyword; otherwise the call uses the two-arg
    invocation. The agent is supplied per-call so the stage can be
    instantiated once and reused across turns.
    """

    def run_turn(
        self,
        agent: Agent,
        *,
        turn_input: str,
        extra_messages: list[Any] | None,
        semantic_message: str | None,
    ) -> AsyncIterator[AgentEvent]:
        kwargs: dict[str, Any] = {}
        if accepts_keyword_arg(agent.run_turn, "semantic_message"):
            kwargs["semantic_message"] = semantic_message
        return agent.run_turn(
            turn_input,
            extra_messages=extra_messages,
            **kwargs,
        )


class MemorySyncNotifier(MemorySyncNotifyPort):
    """Notify ``sync_manager.notify_message(byte_count)`` post-stream.

    It folds the ``sync_manager is None`` guard so the stage
    body has no conditional. Byte count is the UTF-8 length of the
    effective runtime message.
    """

    def notify_message_bytes(
        self,
        sync_manager: Any | None,
        runtime_message: str,
    ) -> None:
        if sync_manager is None:
            return
        byte_count = len(runtime_message.encode("utf-8"))
        sync_manager.notify_message(byte_count)


class CompactionPersistRejectedError(RuntimeError):
    """``persist_compaction_result`` wrote nothing for an inline compaction.

    Raised by ``TurnRunner.persist_and_notify`` so the stream consumer stage takes its
    failed-persist path -- a ``failed`` lifecycle notification and no
    post-persist refresh -- instead of announcing a durable compaction that
    never reached the transcript. The usual cause is a stale snapshot: the
    session was reset, truncated or compacted from under the running turn.
    """
