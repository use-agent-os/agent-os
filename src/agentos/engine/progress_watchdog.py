"""Observe-first progress watchdog for agent turns.

Two failure shapes stall a turn without raising anything.

The first is repetition of a *failing* call — the model retries what just
broke, unchanged. Counting identical error signatures catches that.

The second is repetition of a *succeeding* call: the model reads the same file,
or runs the same search, over and over. Every one of those returns cleanly, so
by any "did a tool succeed" measure the turn is making progress while burning
iterations and context on nothing. That shape needs the call itself as the
signal, not its outcome.

A repeat is only counted when the result is byte-identical to the previous one
for the same tool and arguments. Re-reading a file that changed is legitimate
work, and its differing result resets the count — the guard fires on calls that
demonstrably produced nothing new, rather than on any repetition at all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from agentos.util.bounded_registry import BoundedSessionRegistry

ProgressAction = Literal["observe", "warn", "block"]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def canonical_arguments(arguments: Any) -> str:
    """Stable text for a tool's arguments, so key order cannot hide a repeat."""

    if arguments is None:
        return ""
    try:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(arguments)


@dataclass(frozen=True)
class ToolCallSignature:
    """One executed tool call, reduced to what identifies a repeat."""

    tool_name: str
    arguments_hash: str
    result_hash: str
    is_error: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.tool_name, self.arguments_hash)


def tool_call_signature(
    tool_name: str,
    arguments: Any,
    result: str,
    *,
    is_error: bool = False,
) -> ToolCallSignature:
    return ToolCallSignature(
        tool_name=str(tool_name or ""),
        arguments_hash=_digest(canonical_arguments(arguments)),
        result_hash=_digest(str(result or "")),
        is_error=is_error,
    )


@dataclass(frozen=True)
class ProgressObservation:
    iteration: int
    provider_call_count: int = 0
    successful_tool_result: bool = False
    user_visible_output: bool = False
    artifact_completed: bool = False
    tool_error_signature: str | None = None
    provider_failure_signature: str | None = None
    tool_calls: tuple[ToolCallSignature, ...] = ()


@dataclass(frozen=True)
class ProgressDecision:
    action: ProgressAction
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProgressWatchdog:
    """Detect repeated no-progress loops without owning the main turn loop."""

    def __init__(
        self,
        *,
        repeated_tool_error_threshold: int = 3,
        repeated_provider_failure_threshold: int = 2,
        repeated_tool_call_threshold: int = 3,
        observe_only: bool = True,
    ) -> None:
        self.repeated_tool_error_threshold = repeated_tool_error_threshold
        self.repeated_provider_failure_threshold = repeated_provider_failure_threshold
        self.repeated_tool_call_threshold = repeated_tool_call_threshold
        self.observe_only = observe_only
        self._last_tool_error: str | None = None
        self._tool_error_count = 0
        self._last_provider_failure: str | None = None
        self._provider_failure_count = 0
        self._repeat_counts: BoundedSessionRegistry[tuple[str, str], int] = (
            BoundedSessionRegistry(max_entries=500, ttl_seconds=3600)
        )
        self._repeat_results: BoundedSessionRegistry[tuple[str, str], str] = (
            BoundedSessionRegistry(max_entries=500, ttl_seconds=3600)
        )

    def observe(self, observation: ProgressObservation) -> ProgressDecision:
        # Checked before the progress test on purpose: a repeated *successful*
        # call reads as progress by every other measure, which is exactly why
        # it goes unnoticed.
        repeat_decision = self._record_repeated_tool_calls(observation)
        if repeat_decision is not None:
            return repeat_decision

        if _has_progress(observation):
            self._reset_progress_sensitive_counts()
            return ProgressDecision("observe", "progress")

        tool_decision = self._record_repeated_tool_error(observation)
        if tool_decision is not None:
            return tool_decision

        provider_decision = self._record_repeated_provider_failure(observation)
        if provider_decision is not None:
            return provider_decision

        return ProgressDecision("observe", "no_signal")

    def _record_repeated_tool_calls(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        flagged: ToolCallSignature | None = None
        flagged_count = 0
        for signature in observation.tool_calls:
            key = signature.key
            previous = self._repeat_results.get(key)
            if previous is not None and previous == signature.result_hash:
                self._repeat_counts[key] = self._repeat_counts.get(key, 1) + 1
            else:
                # A different answer means the call earned its place.
                self._repeat_counts[key] = 1
                self._repeat_results[key] = signature.result_hash
            count = self._repeat_counts[key]
            if count >= self.repeated_tool_call_threshold and count > flagged_count:
                flagged = signature
                flagged_count = count

        if flagged is None:
            return None
        return self._decision(
            "repeated_tool_call",
            {
                "tool": flagged.tool_name,
                "arguments_hash": flagged.arguments_hash,
                "result_hash": flagged.result_hash,
                "count": flagged_count,
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
            },
        )

    def _record_repeated_tool_error(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        signature = observation.tool_error_signature
        if not signature:
            return None
        if signature == self._last_tool_error:
            self._tool_error_count += 1
        else:
            self._last_tool_error = signature
            self._tool_error_count = 1
        if self._tool_error_count < self.repeated_tool_error_threshold:
            return None
        return self._decision(
            "repeated_tool_error",
            self._decision_details(observation, signature, self._tool_error_count),
        )

    def _record_repeated_provider_failure(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        signature = observation.provider_failure_signature
        if not signature:
            return None
        if signature == self._last_provider_failure:
            self._provider_failure_count += 1
        else:
            self._last_provider_failure = signature
            self._provider_failure_count = 1
        if self._provider_failure_count < self.repeated_provider_failure_threshold:
            return None
        return self._decision(
            "repeated_provider_failure",
            self._decision_details(observation, signature, self._provider_failure_count),
        )

    def _decision_details(
        self,
        observation: ProgressObservation,
        signature: str,
        count: int,
    ) -> dict[str, Any]:
        return {
            "signature": signature,
            "count": count,
            "iteration": observation.iteration,
            "provider_call_count": observation.provider_call_count,
        }

    def _decision(self, reason: str, details: dict[str, Any]) -> ProgressDecision:
        if self.observe_only:
            return ProgressDecision("warn", reason, details)
        return ProgressDecision("block", reason, details)

    def _reset_progress_sensitive_counts(self) -> None:
        self._last_tool_error = None
        self._tool_error_count = 0
        self._last_provider_failure = None
        self._provider_failure_count = 0


def guidance_for(decision: ProgressDecision) -> str:
    """A sentence the runtime can put in front of the model, or "".

    Kept out of the controller so this module stays a pure decision-maker:
    whether guidance becomes a log line, a synthetic tool result, or nothing at
    all is the runtime's call.
    """

    details = decision.details
    if decision.reason == "repeated_tool_call":
        tool = details.get("tool") or "that tool"
        count = details.get("count") or 0
        return (
            f"{tool} has now returned the same result {count} times for the same"
            " arguments. Repeating it will not produce anything new — use what you"
            " already have, or change the arguments."
        )
    if decision.reason == "repeated_tool_error":
        count = details.get("count") or 0
        return (
            f"The same tool error has repeated {count} times. Retrying it unchanged"
            " will fail the same way; change the arguments or take a different route."
        )
    if decision.reason == "repeated_provider_failure":
        return (
            "The provider has failed the same way more than once. Continuing to call"
            " it as-is is unlikely to succeed."
        )
    return ""


def _has_progress(observation: ProgressObservation) -> bool:
    return (
        observation.successful_tool_result
        or observation.user_visible_output
        or observation.artifact_completed
    )
