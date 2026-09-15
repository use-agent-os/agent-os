from __future__ import annotations

from agentos.engine.progress_watchdog import (
    ProgressObservation,
    ProgressWatchdog,
    canonical_arguments,
    guidance_for,
    tool_call_signature,
)


def _call(tool: str, arguments: dict, result: str, *, is_error: bool = False):
    return tool_call_signature(tool, arguments, result, is_error=is_error)


def test_progress_watchdog_observes_progress_and_resets_repeated_errors() -> None:
    watchdog = ProgressWatchdog(repeated_tool_error_threshold=2)

    first = watchdog.observe(ProgressObservation(iteration=1, tool_error_signature="tool:error"))
    progress = watchdog.observe(ProgressObservation(iteration=2, successful_tool_result=True))
    after_reset = watchdog.observe(
        ProgressObservation(iteration=3, tool_error_signature="tool:error")
    )

    assert first.action == "observe"
    assert progress.reason == "progress"
    assert after_reset.action == "observe"


def test_progress_watchdog_warns_in_observe_only_mode() -> None:
    watchdog = ProgressWatchdog(repeated_tool_error_threshold=2, observe_only=True)

    watchdog.observe(ProgressObservation(iteration=1, tool_error_signature="same"))
    decision = watchdog.observe(ProgressObservation(iteration=2, tool_error_signature="same"))

    assert decision.action == "warn"
    assert decision.reason == "repeated_tool_error"
    assert decision.details["count"] == 2
    assert decision.details["iteration"] == 2
    assert decision.details["provider_call_count"] == 0


def test_progress_watchdog_blocks_only_when_enabled() -> None:
    watchdog = ProgressWatchdog(
        repeated_provider_failure_threshold=2,
        observe_only=False,
    )

    watchdog.observe(ProgressObservation(iteration=1, provider_failure_signature="timeout"))
    decision = watchdog.observe(
        ProgressObservation(iteration=2, provider_failure_signature="timeout")
    )

    assert decision.action == "block"
    assert decision.reason == "repeated_provider_failure"


def test_a_succeeding_call_repeated_with_the_same_result_is_flagged() -> None:
    # Every one of these "succeeds", so successful_tool_result is True each
    # time. Without the call signature the turn looks productive.
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3)
    call = _call("read_file", {"path": "/a.py"}, "contents")

    decisions = [
        watchdog.observe(
            ProgressObservation(iteration=i, successful_tool_result=True, tool_calls=(call,))
        )
        for i in range(1, 4)
    ]

    assert [d.reason for d in decisions[:2]] == ["progress", "progress"]
    assert decisions[2].reason == "repeated_tool_call"
    assert decisions[2].details["tool"] == "read_file"
    assert decisions[2].details["count"] == 3


def test_a_changed_result_resets_the_repeat_count() -> None:
    # Re-reading a file that changed is real work, not a loop.
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3)
    args = {"path": "/a.py"}

    watchdog.observe(ProgressObservation(iteration=1, tool_calls=(_call("read_file", args, "v1"),)))
    watchdog.observe(ProgressObservation(iteration=2, tool_calls=(_call("read_file", args, "v1"),)))
    watchdog.observe(ProgressObservation(iteration=3, tool_calls=(_call("read_file", args, "v2"),)))
    decision = watchdog.observe(
        ProgressObservation(iteration=4, tool_calls=(_call("read_file", args, "v2"),))
    )

    assert decision.reason != "repeated_tool_call"


def test_different_arguments_are_tracked_separately() -> None:
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=2)

    watchdog.observe(
        ProgressObservation(iteration=1, tool_calls=(_call("read_file", {"path": "/a"}, "x"),))
    )
    decision = watchdog.observe(
        ProgressObservation(iteration=2, tool_calls=(_call("read_file", {"path": "/b"}, "x"),))
    )

    assert decision.reason != "repeated_tool_call"


def test_argument_key_order_does_not_hide_a_repeat() -> None:
    first = tool_call_signature("grep_search", {"a": 1, "b": 2}, "hit")
    second = tool_call_signature("grep_search", {"b": 2, "a": 1}, "hit")

    assert first.arguments_hash == second.arguments_hash


def test_unserializable_arguments_still_produce_a_signature() -> None:
    signature = tool_call_signature("x", {"fn": object()}, "result")

    assert signature.arguments_hash
    assert canonical_arguments({"fn": object()})


def test_repeat_guidance_names_the_tool_and_the_count() -> None:
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=2)
    call = _call("web_search", {"q": "x"}, "same answer")

    watchdog.observe(ProgressObservation(iteration=1, tool_calls=(call,)))
    decision = watchdog.observe(ProgressObservation(iteration=2, tool_calls=(call,)))

    guidance = guidance_for(decision)
    assert "web_search" in guidance
    assert "2 times" in guidance


def test_guidance_is_empty_for_an_ordinary_observation() -> None:
    watchdog = ProgressWatchdog()

    decision = watchdog.observe(ProgressObservation(iteration=1, successful_tool_result=True))

    assert guidance_for(decision) == ""


# ── a repeated tool *error* must not be reported as a repeated tool call ─────
# Issue #2101: `_record_repeated_tool_calls` counted every signature in
# `observation.tool_calls`, error or not, and runs before the error branch --
# so a tool failing the same way three times returned early as
# `repeated_tool_call`, shadowing `repeated_tool_error` entirely. The guidance
# that reaches the model then says to "use what you already have" about a
# result that was a FileNotFoundError.


def _failing(tool: str = "read_file", path: str = "missing.txt", error: str = "FileNotFoundError"):
    return _call(tool, {"path": path}, error, is_error=True)


def _error_observation(iteration: int, signature=None, *, error_text: str = "FileNotFoundError"):
    call = signature if signature is not None else _failing(error=error_text)
    return ProgressObservation(
        iteration=iteration,
        tool_error_signature=f"read_file:{error_text}",
        tool_calls=(call,),
    )


def test_a_repeated_failing_call_is_reported_as_a_tool_error() -> None:
    """The reproduction from the issue, verbatim in behaviour."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3, repeated_tool_error_threshold=3)

    watchdog.observe(_error_observation(1))
    watchdog.observe(_error_observation(2))
    decision = watchdog.observe(_error_observation(3))

    assert decision.reason == "repeated_tool_error"


def test_the_guidance_no_longer_tells_the_model_to_reuse_a_failure() -> None:
    """The user-visible half: "use what you already have" about an error is
    what corrupts the model's recovery attempt."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3, repeated_tool_error_threshold=3)

    for iteration in (1, 2, 3):
        decision = watchdog.observe(_error_observation(iteration))

    guidance = guidance_for(decision)
    assert "use what you already have" not in guidance
    assert "change the arguments or take a different route" in guidance


def test_a_failing_call_never_reaches_the_repeated_call_threshold() -> None:
    """Even well past the threshold, errors must not accumulate into the
    succeeding-call guard."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3, repeated_tool_error_threshold=99)

    reasons = {watchdog.observe(_error_observation(i)).reason for i in range(1, 9)}

    assert "repeated_tool_call" not in reasons


def test_a_repeated_succeeding_call_is_still_flagged() -> None:
    """The guard this check exists for must keep working."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3)
    call = _call("read_file", {"path": "a.py"}, "same content")

    for iteration in (1, 2, 3):
        decision = watchdog.observe(ProgressObservation(iteration=iteration, tool_calls=(call,)))

    assert decision.reason == "repeated_tool_call"
    assert decision.details["tool"] == "read_file"
    assert decision.details["count"] == 3


def test_a_failing_call_does_not_advance_a_succeeding_call_of_the_same_key() -> None:
    """Same tool, same arguments, but one run errored: the error must not count
    toward the identical-result tally, or two successes plus an unrelated
    failure would trip the loop guard a run early."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3)
    ok = _call("read_file", {"path": "a.py"}, "content")
    failed = _call("read_file", {"path": "a.py"}, "content", is_error=True)

    watchdog.observe(ProgressObservation(iteration=1, tool_calls=(ok,)))
    watchdog.observe(ProgressObservation(iteration=2, tool_calls=(failed,)))
    decision = watchdog.observe(ProgressObservation(iteration=3, tool_calls=(ok,)))

    assert decision.reason != "repeated_tool_call", "the failure was counted as a repeat"


def test_a_succeeding_call_alongside_a_failing_one_is_still_counted() -> None:
    """Skipping errors must not skip the whole observation: a genuine repeat in
    the same batch still has to be caught."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3)
    looping = _call("list_dir", {"path": "."}, "same listing")

    for iteration in (1, 2, 3):
        decision = watchdog.observe(
            ProgressObservation(iteration=iteration, tool_calls=(_failing(), looping))
        )

    assert decision.reason == "repeated_tool_call"
    assert decision.details["tool"] == "list_dir"


def test_a_tool_that_starts_failing_does_not_inherit_its_success_count() -> None:
    """Two identical successes then persistent failure is an error loop, not a
    repeated-result loop, and must be named as one."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3, repeated_tool_error_threshold=2)
    ok = _call("read_file", {"path": "a.py"}, "content")

    watchdog.observe(ProgressObservation(iteration=1, tool_calls=(ok,)))
    watchdog.observe(ProgressObservation(iteration=2, tool_calls=(ok,)))
    watchdog.observe(_error_observation(3))
    decision = watchdog.observe(_error_observation(4))

    assert decision.reason == "repeated_tool_error"


def test_a_changed_error_restarts_the_error_tally() -> None:
    """A different failure is a different problem: the agent is still moving,
    even if it is not succeeding."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3, repeated_tool_error_threshold=3)

    watchdog.observe(_error_observation(1, error_text="FileNotFoundError"))
    watchdog.observe(_error_observation(2, error_text="FileNotFoundError"))
    decision = watchdog.observe(_error_observation(3, error_text="PermissionError"))

    assert decision.reason != "repeated_tool_error"
    assert decision.reason != "repeated_tool_call"


def test_a_successful_result_still_resets_the_error_tally() -> None:
    """Unchanged behaviour, pinned next to the new skip."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=3, repeated_tool_error_threshold=2)

    watchdog.observe(_error_observation(1))
    progress = watchdog.observe(ProgressObservation(iteration=2, successful_tool_result=True))
    after = watchdog.observe(_error_observation(3))

    assert progress.reason == "progress"
    assert after.reason != "repeated_tool_error"


def test_an_error_only_turn_with_no_error_signature_yields_no_false_call_alarm() -> None:
    """Belt and braces: if the engine ever hands over failing calls without a
    `tool_error_signature`, the right answer is "no signal", not a
    `repeated_tool_call` about an error."""
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=2, repeated_tool_error_threshold=2)
    call = _failing()

    for iteration in (1, 2, 3):
        decision = watchdog.observe(ProgressObservation(iteration=iteration, tool_calls=(call,)))

    assert decision.reason == "no_signal"
