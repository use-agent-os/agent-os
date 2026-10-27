from __future__ import annotations

from agentos.engine.progress_watchdog import (
    ProgressObservation,
    ProgressWatchdog,
    canonical_arguments,
    guidance_for,
    is_poll_call,
    proposed_tool_call,
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


def test_after_the_fact_signals_only_ever_warn() -> None:
    # Blocking is decided before a batch runs (``admit``); what ``observe``
    # sees has already happened, so it can only warn.
    watchdog = ProgressWatchdog(
        repeated_provider_failure_threshold=2,
        observe_only=False,
    )

    watchdog.observe(ProgressObservation(iteration=1, provider_failure_signature="timeout"))
    decision = watchdog.observe(
        ProgressObservation(iteration=2, provider_failure_signature="timeout")
    )

    assert decision.action == "warn"
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


def test_repeated_tool_errors_do_not_trigger_repeated_tool_call() -> None:
    watchdog = ProgressWatchdog(
        repeated_tool_call_threshold=3,
        repeated_tool_error_threshold=3,
    )
    call = _call("read_file", {"path": "missing.txt"}, "FileNotFoundError", is_error=True)
    obs = ProgressObservation(
        iteration=1,
        tool_error_signature="read_file:FileNotFoundError",
        tool_calls=(call,),
    )

    d1 = watchdog.observe(obs)
    d2 = watchdog.observe(
        ProgressObservation(
            iteration=2, tool_error_signature="read_file:FileNotFoundError", tool_calls=(call,)
        )
    )
    d3 = watchdog.observe(
        ProgressObservation(
            iteration=3, tool_error_signature="read_file:FileNotFoundError", tool_calls=(call,)
        )
    )

    assert d1.action == "observe"
    assert d2.action == "observe"
    assert d3.reason == "repeated_tool_error"
    assert d3.details["count"] == 3
    guidance = guidance_for(d3)
    assert "The same tool error has repeated 3 times" in guidance
    assert "use what you already have" not in guidance


def test_failing_calls_do_not_increment_succeeding_call_repeat_count() -> None:
    watchdog = ProgressWatchdog(repeated_tool_call_threshold=2)
    succeeding_call = _call("read_file", {"path": "/a.py"}, "content", is_error=False)
    failing_call = _call("read_file", {"path": "/a.py"}, "content", is_error=True)

    # First call succeeds
    d1 = watchdog.observe(
        ProgressObservation(iteration=1, successful_tool_result=True, tool_calls=(succeeding_call,))
    )
    assert d1.reason == "progress"

    # Second call fails with the same result string/args
    d2 = watchdog.observe(
        ProgressObservation(
            iteration=2, tool_error_signature="read_file:error", tool_calls=(failing_call,)
        )
    )
    # Must not be flagged as repeated_tool_call
    assert d2.reason != "repeated_tool_call"


def _observe_calls(watchdog: ProgressWatchdog, *calls, iteration: int = 1):
    return watchdog.observe(
        ProgressObservation(iteration=iteration, successful_tool_result=True, tool_calls=calls)
    )


def test_admit_refuses_a_call_that_keeps_returning_the_same_result() -> None:
    watchdog = ProgressWatchdog()
    args = {"path": "/a.py"}
    for i in range(3):
        _observe_calls(watchdog, _call("read_file", args, "same"), iteration=i)

    # Three identical results: warned, still allowed once more.
    assert watchdog.admit([proposed_tool_call("read_file", args)]) is None
    _observe_calls(watchdog, _call("read_file", args, "same"), iteration=4)

    first = watchdog.admit([proposed_tool_call("read_file", args)], iteration=5)
    assert first is not None
    assert first.action == "block"
    assert first.reason == "repeated_tool_call"
    assert first.terminal is False
    assert first.targets == (proposed_tool_call("read_file", args).key,)
    # The refused batch never ran; its synthetic results carry no signal.
    assert _observe_calls(watchdog, _call("read_file", args, "Not run")).reason == "batch_blocked"

    second = watchdog.admit([proposed_tool_call("read_file", args)], iteration=6)
    assert second is not None
    assert second.action == "block"
    assert second.terminal is True


def test_admit_lets_a_different_call_through_after_a_block() -> None:
    watchdog = ProgressWatchdog()
    for i in range(4):
        _observe_calls(watchdog, _call("read_file", {"path": "/a"}, "same"), iteration=i)

    assert watchdog.admit([proposed_tool_call("read_file", {"path": "/b"})]) is None


def test_changing_results_are_never_refused() -> None:
    watchdog = ProgressWatchdog()
    args = {"action": "log", "session_id": "bg"}
    for i in range(30):
        assert watchdog.admit([proposed_tool_call("process", args)]) is None
        decision = _observe_calls(watchdog, _call("process", args, f"line {i}"), iteration=i)
        assert decision.reason == "progress"


def test_polls_get_a_larger_allowance_before_being_refused() -> None:
    watchdog = ProgressWatchdog(repeated_poll_block_threshold=12)
    args = {"action": "poll", "session_id": "bg"}
    for i in range(11):
        _observe_calls(watchdog, _call("process", args, "running"), iteration=i)
        assert watchdog.admit([proposed_tool_call("process", args)]) is None
    _observe_calls(watchdog, _call("process", args, "running"), iteration=12)

    decision = watchdog.admit([proposed_tool_call("process", args)])
    assert decision is not None and decision.action == "block"


def test_poll_shapes_are_recognised() -> None:
    assert is_poll_call("process", {"action": "poll"})
    assert is_poll_call("dubbing_status", {"id": "x"})
    assert is_poll_call("subagents", {"action": "list"})
    assert not is_poll_call("process", {"action": "kill"})
    assert not is_poll_call("read_file", {"path": "x"})


def test_a_state_change_resets_other_calls_repeat_counts() -> None:
    watchdog = ProgressWatchdog()
    read = {"path": "/a.py"}
    for i in range(4):
        _observe_calls(watchdog, _call("read_file", read, "old"), iteration=i)
    _observe_calls(watchdog, _call("edit_file", {"path": "/a.py"}, "ok"), iteration=5)

    # The file may have changed, so re-reading it is real work again.
    assert watchdog.admit([proposed_tool_call("read_file", read)]) is None


def test_rerunning_the_same_command_still_counts_after_it_changes_state() -> None:
    watchdog = ProgressWatchdog()
    command = {"command": "make"}
    for i in range(4):
        _observe_calls(watchdog, _call("exec_command", command, "same output"), iteration=i)

    decision = watchdog.admit([proposed_tool_call("exec_command", command)])
    assert decision is not None and decision.action == "block"


def test_ping_pong_warns_even_when_results_drift() -> None:
    watchdog = ProgressWatchdog(ping_pong_threshold=6)
    decisions = []
    for i in range(6):
        call = (
            _call("read_file", {"path": "/a"}, f"a{i}")
            if i % 2 == 0
            else _call("grep_search", {"pattern": "x"}, f"b{i}")
        )
        decisions.append(_observe_calls(watchdog, call, iteration=i))

    assert decisions[4].reason == "progress"
    assert decisions[5].reason == "ping_pong"
    assert decisions[5].details["no_new_results"] is False
    assert "alternated between read_file and grep_search" in guidance_for(decisions[5])
    # Results kept changing: nothing is refused.
    assert watchdog.admit([proposed_tool_call("read_file", {"path": "/a"})]) is None


def test_ping_pong_with_no_new_results_is_refused() -> None:
    watchdog = ProgressWatchdog(ping_pong_block_threshold=8)
    a = _call("read_file", {"path": "/a"}, "A")
    b = _call("grep_search", {"pattern": "x"}, "B")
    for i in range(7):
        _observe_calls(watchdog, a if i % 2 == 0 else b, iteration=i)

    # Breaking the alternation is fine; continuing it is not.
    assert watchdog.admit([proposed_tool_call("list_dir", {"path": "/"})]) is None
    decision = watchdog.admit([proposed_tool_call("grep_search", {"pattern": "x"})])
    assert decision is not None
    assert decision.action == "block"
    assert decision.reason == "ping_pong"


def test_observe_only_reports_what_it_would_have_refused() -> None:
    watchdog = ProgressWatchdog(observe_only=True)
    args = {"path": "/a.py"}
    for i in range(6):
        _observe_calls(watchdog, _call("read_file", args, "same"), iteration=i)

    for _ in range(2):
        decision = watchdog.admit([proposed_tool_call("read_file", args)])
        assert decision is not None
        assert decision.action == "warn"
        assert decision.details["would_block"] is True
        assert decision.terminal is False
    assert watchdog.critical_count == 0
