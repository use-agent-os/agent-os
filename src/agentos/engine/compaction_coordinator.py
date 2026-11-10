"""The one entry point every compaction trigger goes through.

Four places ask for compaction:

* ``preflight`` -- the agent loop, before a provider call whose request is
  over the threshold (``Agent._preflight_context_overflow``);
* ``overflow`` -- the agent loop, after the provider rejected a request as
  too large (forced);
* ``timeout`` -- the agent loop, after a request that fills a meaningful
  share of the window timed out twice (forced; see
  ``agentos.engine.policy.attempt``);
* ``proactive`` -- between turns, from
  :class:`~agentos.engine.proactive_compaction.ProactiveCompactionScheduler`.

They used to call :class:`~agentos.engine.compaction_service.CompactionService`
each in their own way. The coordinator builds the service for the caller,
runs it, and records one ``compaction.decision`` log entry per request with
the trigger and the result (at debug level when the request was simply under
the threshold), so every compaction is attributable to the reason it ran.
The in-turn triggers also travel on ``CompactionEvent.trigger`` to the
durable record.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import structlog

from agentos.engine.compaction_service import (
    CompactionService,
    auxiliary_chunk_summarizer,
)

log = structlog.get_logger(__name__)

CompactionTrigger = Literal["preflight", "overflow", "timeout", "proactive"]
TRIGGERS: tuple[CompactionTrigger, ...] = ("preflight", "overflow", "timeout", "proactive")


class CompactionCoordinator:
    """Builds the compaction service for an agent and runs it per trigger."""

    def __init__(
        self,
        agent_config: Any,
        provider: Any,
        session_key: str | None,
        *,
        service_factory: Callable[..., CompactionService] = CompactionService,
    ) -> None:
        self._config = agent_config
        self._provider = provider
        self._session_key = session_key
        self._service_factory = service_factory

    def service(self, *, phase: str | None = None) -> CompactionService:
        hint = getattr(self._config, "compaction_summary_model_hint", None) or ("", "")
        kwargs: dict[str, Any] = {
            "provider": self._provider,
            "ratio": float(self._config.context_overflow_threshold),
            "session_key": self._session_key,
            # Summaries go through the auxiliary side-task client (cheap model,
            # no thinking, accounted as aux:compaction), never the turn's own
            # model, and are bounded by their own timeout.
            "chunk_summarizer": auxiliary_chunk_summarizer(
                hint=(str(hint[0] or ""), str(hint[1] or "")),
                session_key=self._session_key,
            ),
            "timeout": float(
                getattr(self._config, "compaction_summary_timeout_seconds", 45.0) or 45.0
            ),
        }
        if phase is not None:
            kwargs["phase"] = phase
        return self._service_factory(**kwargs)

    async def compact(
        self,
        messages: list[Any],
        *,
        trigger: CompactionTrigger,
        phase: str | None = None,
        **compact_kwargs: Any,
    ) -> Any:
        """Run one compaction for *trigger*; return the service's result."""

        if trigger not in TRIGGERS:
            raise ValueError(f"unknown compaction trigger: {trigger!r}")
        result = await self.service(phase=phase).compact(messages, **compact_kwargs)
        reason = getattr(result, "reason", None)
        # The preflight check runs before every provider request; a request
        # under the threshold is the normal case, not a decision worth an
        # info line per tool round.
        emit = log.debug if reason == "under_threshold" else log.info
        emit(
            "compaction.decision",
            session_key=self._session_key,
            trigger=trigger,
            status=getattr(result, "status", None),
            reason=reason,
            compacted=bool(getattr(result, "compacted", False)),
            forced=bool(compact_kwargs.get("force", False)),
        )
        return result
