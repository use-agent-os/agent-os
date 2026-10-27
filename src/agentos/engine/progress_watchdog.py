"""Progress watchdog for agent turns: notice loops, then warn and stop them.

Two failure shapes stall a turn without raising anything.

The first is repetition of a *failing* call — the model retries what just
broke, unchanged. Counting identical error signatures catches that.

The second is repetition of a *succeeding* call: the model reads the same file,
or runs the same search, over and over. Every one of those returns cleanly, so
by any "did a tool succeed" measure the turn is making progress while burning
iterations and context on nothing. That shape needs the call itself as the
signal, not its outcome. A variant is two calls taking turns (A, B, A, B …),
where neither repeats back to back.

A repeat is only counted when the result is byte-identical to the previous one
for the same tool and arguments. Re-reading a file that changed is legitimate
work, and its differing result resets the count — the guard fires on calls that
demonstrably produced nothing new, rather than on any repetition at all. Status
polls (a background process, a child session) are expected to return the same
answer for a while, so they get a much larger allowance before being refused.

The watchdog has two tiers. ``observe`` runs after a batch of tool calls and
returns a ``warn`` decision the runtime turns into guidance for the model.
``admit`` runs before the next batch: when a proposed call would repeat an
action that has already been seen to produce nothing new, it returns a
``block`` decision so the batch is answered with synthetic results instead of
being run. The second block in one turn is marked ``terminal``. One watchdog
lives for one turn, so a call the user asks for again in a new message starts
from zero.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from agentos.util.bounded_registry import BoundedRegistry

ProgressAction = Literal["observe", "warn", "block"]

CallKey = tuple[str, str]

# Tools (and the actions of multi-action tools) whose job is to report a state
# that changes on its own schedule: asking again is how they are used.
_POLL_TOOL_ACTIONS: Mapping[str, frozenset[str]] = {
    "process": frozenset({"poll", "log", "list"}),
    "subagents": frozenset({"list", "status"}),
    "acp": frozenset({"status", "sessions", "agents"}),
}
# Tools that change the workspace. Once one of them succeeds, what any *other*
# call returns may differ, so their repeat counts start over; the changing call
# keeps its own count, which is how re-running the same command is still seen.
_STATE_CHANGING_TOOLS = frozenset(
    {
        "apply_patch",
        "background_process",
        "edit_file",
        "exec_command",
        "execute_code",
        "git_commit",
        "install_skill_deps",
        "write_file",
    }
)
_SEQUENCE_WINDOW = 32


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


def call_key(tool_name: str, arguments: Any) -> CallKey:
    """What identifies "the same call": the tool and its canonical arguments."""

    return (str(tool_name or ""), _digest(canonical_arguments(arguments)))


def is_poll_call(tool_name: str, arguments: Any) -> bool:
    """True for a status poll, which is meant to be asked again and again."""

    name = str(tool_name or "")
    if name.endswith("_status"):
        return True
    actions = _POLL_TOOL_ACTIONS.get(name)
    if actions is None or not isinstance(arguments, Mapping):
        return False
    return str(arguments.get("action") or "").strip().lower() in actions


@dataclass(frozen=True)
class ToolCallSignature:
    """One executed tool call, reduced to what identifies a repeat."""

    tool_name: str
    arguments_hash: str
    result_hash: str
    is_error: bool = False
    is_poll: bool = False

    @property
    def key(self) -> CallKey:
        return (self.tool_name, self.arguments_hash)


def tool_call_signature(
    tool_name: str,
    arguments: Any,
    result: str,
    *,
    is_error: bool = False,
) -> ToolCallSignature:
    name, arguments_hash = call_key(tool_name, arguments)
    return ToolCallSignature(
        tool_name=name,
        arguments_hash=arguments_hash,
        result_hash=_digest(str(result or "")),
        is_error=is_error,
        is_poll=is_poll_call(name, arguments),
    )


@dataclass(frozen=True)
class ProposedToolCall:
    """A call the model has asked for and that has not run yet."""

    tool_name: str
    arguments_hash: str
    is_poll: bool = False

    @property
    def key(self) -> CallKey:
        return (self.tool_name, self.arguments_hash)


def proposed_tool_call(tool_name: str, arguments: Any) -> ProposedToolCall:
    name, arguments_hash = call_key(tool_name, arguments)
    return ProposedToolCall(
        tool_name=name,
        arguments_hash=arguments_hash,
        is_poll=is_poll_call(name, arguments),
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
    # The calls the decision is about, as ``(tool_name, arguments_hash)``:
    # where warn guidance belongs, or which calls of a batch tripped a block.
    targets: tuple[CallKey, ...] = ()
    # Set on the block that should end the turn rather than let it go on.
    terminal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProgressWatchdog:
    """Detect repeated no-progress loops without owning the main turn loop.

    ``observe_only`` keeps every decision advisory: ``admit`` still reports
    what it would have refused, as a ``warn`` with ``would_block`` set, and the
    runtime only logs it.
    """

    def __init__(
        self,
        *,
        repeated_tool_error_threshold: int = 3,
        repeated_provider_failure_threshold: int = 2,
        repeated_tool_call_threshold: int = 3,
        repeated_tool_call_block_threshold: int = 4,
        repeated_poll_block_threshold: int = 12,
        ping_pong_threshold: int = 6,
        ping_pong_block_threshold: int = 8,
        observe_only: bool = False,
    ) -> None:
        self.repeated_tool_error_threshold = repeated_tool_error_threshold
        self.repeated_provider_failure_threshold = repeated_provider_failure_threshold
        self.repeated_tool_call_threshold = repeated_tool_call_threshold
        # A call that has already returned the same result this many times in
        # a row is not run again.
        self.repeated_tool_call_block_threshold = repeated_tool_call_block_threshold
        self.repeated_poll_block_threshold = repeated_poll_block_threshold
        # Warn once two calls have alternated this many times; refuse the call
        # that would stretch an alternation with no new results to the block
        # threshold.
        self.ping_pong_threshold = ping_pong_threshold
        self.ping_pong_block_threshold = ping_pong_block_threshold
        self.observe_only = observe_only
        self.critical_count = 0
        # Set by a block: the batch it answered never ran, so the next
        # observation carries synthetic results and no signal.
        self._blocked_batch_pending = False
        self._last_tool_error: str | None = None
        self._tool_error_count = 0
        self._last_provider_failure: str | None = None
        self._provider_failure_count = 0
        # Time-scoped: a repeat signature is only interesting while the
        # loop it describes is still running.
        self._repeat_counts: BoundedRegistry[CallKey, int] = BoundedRegistry(
            shape="cache",
            name="ProgressWatchdog._repeat_counts",
        )
        self._repeat_results: BoundedRegistry[CallKey, str] = BoundedRegistry(
            shape="cache",
            name="ProgressWatchdog._repeat_results",
        )
        # Every executed call in order, for the alternation check.
        self._sequence: deque[ToolCallSignature] = deque(maxlen=_SEQUENCE_WINDOW)

    # -- after a batch ----------------------------------------------------

    def observe(self, observation: ProgressObservation) -> ProgressDecision:
        if self._blocked_batch_pending:
            self._blocked_batch_pending = False
            return ProgressDecision("observe", "batch_blocked")
        self._sequence.extend(observation.tool_calls)
        # Checked before the progress test on purpose: a repeated *successful*
        # call reads as progress by every other measure, which is exactly why
        # it goes unnoticed.
        repeat_decision = self._record_repeated_tool_calls(observation)
        ping_pong_decision = self._ping_pong_decision(observation)
        if ping_pong_decision is not None:
            return ping_pong_decision
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

    # -- before a batch ---------------------------------------------------

    def admit(
        self,
        calls: Sequence[ProposedToolCall],
        *,
        iteration: int = 0,
    ) -> ProgressDecision | None:
        """Return a block decision when the proposed batch repeats a dead end.

        Only calls already seen to return the same result again and again are
        refused; a call whose last result differed from the one before is
        always let through, however often it has run.
        """

        if not calls:
            return None
        repeated = [call for call in calls if self._would_repeat_no_new_result(call)]
        if repeated:
            first = repeated[0]
            return self._critical(
                "repeated_tool_call",
                {
                    "tool": first.tool_name,
                    "arguments_hash": first.arguments_hash,
                    "count": self._repeat_counts.get(first.key, 0),
                    "iteration": iteration,
                },
                targets=tuple(dict.fromkeys(call.key for call in repeated)),
            )
        tail = self._alternation_tail()
        if (
            len(tail) >= self.ping_pong_block_threshold - 1
            and not (tail[-1].is_poll or tail[-2].is_poll)
            and calls[0].key == tail[-2].key
            and _no_new_results(tail)
        ):
            pair = (tail[-2], tail[-1])
            return self._critical(
                "ping_pong",
                {
                    "tool": pair[0].tool_name,
                    "paired_tool": pair[1].tool_name,
                    "count": len(tail),
                    "iteration": iteration,
                },
                targets=(calls[0].key,),
            )
        return None

    def _would_repeat_no_new_result(self, call: ProposedToolCall) -> bool:
        threshold = (
            self.repeated_poll_block_threshold
            if call.is_poll
            else self.repeated_tool_call_block_threshold
        )
        if threshold <= 0:
            return False
        return self._repeat_counts.get(call.key, 0) >= threshold

    def _critical(
        self,
        reason: str,
        details: dict[str, Any],
        *,
        targets: tuple[CallKey, ...],
    ) -> ProgressDecision:
        if self.observe_only:
            return ProgressDecision(
                "warn", reason, {**details, "would_block": True}, targets=targets
            )
        self.critical_count += 1
        self._blocked_batch_pending = True
        terminal = self.critical_count >= 2
        return ProgressDecision(
            "block",
            reason,
            {**details, "critical_count": self.critical_count},
            targets=targets,
            terminal=terminal,
        )

    # -- detectors --------------------------------------------------------

    def _record_repeated_tool_calls(
        self, observation: ProgressObservation
    ) -> ProgressDecision | None:
        flagged: ToolCallSignature | None = None
        flagged_count = 0
        for signature in observation.tool_calls:
            if signature.is_error:
                continue
            key = signature.key
            previous = self._repeat_results.get(key)
            if previous is not None and previous == signature.result_hash:
                self._repeat_counts[key] = self._repeat_counts.get(key, 1) + 1
            else:
                # A different answer means the call earned its place.
                self._repeat_counts[key] = 1
                self._repeat_results[key] = signature.result_hash
            if signature.tool_name in _STATE_CHANGING_TOOLS:
                self._repeat_counts.discard_where(lambda other, _count: other != key)
                self._repeat_results.discard_where(lambda other, _result: other != key)
                if flagged is not None and flagged.key != key:
                    flagged, flagged_count = None, 0
            count = self._repeat_counts[key]
            if count >= self.repeated_tool_call_threshold and count > flagged_count:
                flagged = signature
                flagged_count = count

        if flagged is None:
            return None
        return ProgressDecision(
            "warn",
            "repeated_tool_call",
            {
                "tool": flagged.tool_name,
                "arguments_hash": flagged.arguments_hash,
                "result_hash": flagged.result_hash,
                "count": flagged_count,
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
            },
            targets=(flagged.key,),
        )

    def _alternation_tail(self) -> list[ToolCallSignature]:
        """The longest run at the end of the sequence that alternates A, B, A, B."""

        sequence = list(self._sequence)
        if len(sequence) < 2 or sequence[-1].key == sequence[-2].key:
            return []
        start = len(sequence) - 2
        while (
            start > 0
            and sequence[start - 1].key == sequence[start + 1].key
            and sequence[start - 1].key != sequence[start].key
        ):
            start -= 1
        return sequence[start:]

    def _ping_pong_decision(self, observation: ProgressObservation) -> ProgressDecision | None:
        if not observation.tool_calls or self.ping_pong_threshold <= 0:
            return None
        tail = self._alternation_tail()
        if len(tail) < self.ping_pong_threshold:
            return None
        first, second = tail[-2], tail[-1]
        if first.is_poll or second.is_poll:
            return None
        return ProgressDecision(
            "warn",
            "ping_pong",
            {
                "tool": first.tool_name,
                "paired_tool": second.tool_name,
                "count": len(tail),
                "no_new_results": _no_new_results(tail),
                "iteration": observation.iteration,
                "provider_call_count": observation.provider_call_count,
            },
            targets=(first.key, second.key),
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
        return ProgressDecision(
            "warn",
            "repeated_tool_error",
            self._decision_details(observation, signature, self._tool_error_count),
            targets=tuple(
                dict.fromkeys(call.key for call in observation.tool_calls if call.is_error)
            ),
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
        return ProgressDecision(
            "warn",
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
    if decision.reason == "ping_pong":
        tool = details.get("tool") or "one tool"
        paired = details.get("paired_tool") or "another"
        count = details.get("count") or 0
        if tool == paired:
            calls = f"two different {tool} calls"
        else:
            calls = f"{tool} and {paired}"
        return (
            f"The last {count} tool calls alternated between {calls}. Going back and"
            " forth is not moving the task forward — decide from what those calls"
            " already returned, or take a different step."
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


def _no_new_results(tail: Sequence[ToolCallSignature]) -> bool:
    """True when every call in *tail* returned what that call returned before."""

    first_result: dict[CallKey, str] = {}
    for signature in tail:
        if signature.is_error:
            continue
        seen = first_result.setdefault(signature.key, signature.result_hash)
        if seen != signature.result_hash:
            return False
    return True


def _has_progress(observation: ProgressObservation) -> bool:
    return (
        observation.successful_tool_result
        or observation.user_visible_output
        or observation.artifact_completed
    )
