"""CDPSupervisor: dialog policy, capture/response, eval framing, registry.

Exercises the supervisor's real decision logic against a fake transport — no
Chromium required.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from agentos.tools.browser_supervisor import (
    DEFAULT_DIALOG_POLICY,
    DEFAULT_DIALOG_TIMEOUT_S,
    CDPSupervisor,
    SupervisorRegistry,
    parse_dialog_policy,
)


class FakeTransport:
    """Records CDP calls and lets a test push events into the supervisor."""

    def __init__(self, *, evaluate_result: Any = None, evaluate_error: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_event: Any = None
        self.started = False
        self.stopped = False
        self._evaluate_result = evaluate_result
        self._evaluate_error = evaluate_error

    def start(self, on_event: Any) -> None:
        self.on_event = on_event
        self.started = True

    def call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            if self._evaluate_error is not None:
                return {"exceptionDetails": {"text": self._evaluate_error}}
            return {"result": {"value": self._evaluate_result}}
        return {}

    def stop(self) -> None:
        self.stopped = True

    def push_dialog(self, dialog_type: str, message: str, default_prompt: str = "") -> None:
        assert self.on_event is not None
        self.on_event(
            "Page.javascriptDialogOpening",
            {"type": dialog_type, "message": message, "defaultPrompt": default_prompt},
        )

    def push_console(self, level: str, text: str) -> None:
        assert self.on_event is not None
        self.on_event("Runtime.consoleAPICalled", {"type": level, "args": [{"value": text}]})


def _supervisor(transport: FakeTransport, *, policy: str = "must_respond") -> CDPSupervisor:
    sup = CDPSupervisor("s1", "ws://127.0.0.1:9/x", dialog_policy=policy, transport=transport)
    sup.start()
    return sup


class TestDialogPolicyParsing:
    def test_defaults(self) -> None:
        assert parse_dialog_policy(None, None) == (DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S)

    def test_invalid_policy_falls_back(self) -> None:
        policy, _ = parse_dialog_policy("nonsense", 60)
        assert policy == DEFAULT_DIALOG_POLICY

    def test_invalid_timeout_falls_back(self) -> None:
        _, timeout = parse_dialog_policy("auto_dismiss", "not-a-number")
        assert timeout == DEFAULT_DIALOG_TIMEOUT_S

    def test_nonpositive_timeout_falls_back(self) -> None:
        _, timeout = parse_dialog_policy("auto_dismiss", -5)
        assert timeout == DEFAULT_DIALOG_TIMEOUT_S

    def test_valid_pair_preserved(self) -> None:
        assert parse_dialog_policy("auto_accept", 42.0) == ("auto_accept", 42.0)


class TestDialogCapture:
    def test_must_respond_leaves_dialog_pending(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport, policy="must_respond")
        transport.push_dialog("alert", "hi")
        snap = sup.snapshot()
        assert len(snap.pending_dialogs) == 1
        assert snap.pending_dialogs[0]["type"] == "alert"
        assert snap.pending_dialogs[0]["message"] == "hi"

    def test_snapshot_as_dict_surfaces_pending(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        transport.push_dialog("confirm", "ok?")
        assert "pending_dialogs" in sup.snapshot().as_dict()

    def test_auto_dismiss_resolves_immediately(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport, policy="auto_dismiss")
        transport.push_dialog("beforeunload", "leave?")
        assert sup.snapshot().pending_dialogs == []
        handled = [c for c in transport.calls if c[0] == "Page.handleJavaScriptDialog"]
        assert handled and handled[-1][1]["accept"] is False

    def test_auto_accept_resolves_immediately(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport, policy="auto_accept")
        transport.push_dialog("confirm", "sure?")
        assert sup.snapshot().pending_dialogs == []
        handled = [c for c in transport.calls if c[0] == "Page.handleJavaScriptDialog"]
        assert handled and handled[-1][1]["accept"] is True


class TestDialogResponse:
    def test_accept_prompt_with_text(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        transport.push_dialog("prompt", "name?", default_prompt="")
        result = sup.respond_to_dialog("accept", prompt_text="Ada")
        assert result["ok"] is True
        handled = [c for c in transport.calls if c[0] == "Page.handleJavaScriptDialog"]
        assert handled[-1][1] == {"accept": True, "promptText": "Ada"}
        assert sup.snapshot().pending_dialogs == []

    def test_dismiss(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        transport.push_dialog("confirm", "delete?")
        assert sup.respond_to_dialog("dismiss")["ok"] is True

    def test_no_dialog_open(self) -> None:
        sup = _supervisor(FakeTransport())
        result = sup.respond_to_dialog("accept")
        assert result["ok"] is False
        assert "no dialog" in result["error"]

    def test_multiple_dialogs_require_id(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        transport.push_dialog("alert", "one")
        transport.push_dialog("alert", "two")
        result = sup.respond_to_dialog("accept")
        assert result["ok"] is False
        assert "dialog_id" in result["error"]

    def test_invalid_action_rejected(self) -> None:
        sup = _supervisor(FakeTransport())
        assert sup.respond_to_dialog("frobnicate")["ok"] is False


class TestConsoleCapture:
    def test_console_events_in_snapshot(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        transport.push_console("error", "boom")
        console = sup.snapshot().console
        assert console and console[0]["level"] == "error"
        assert console[0]["text"] == "boom"


class TestNavigationTracking:
    """What the browser tool waits on before reading a page after an action."""

    @staticmethod
    def _attached(transport: FakeTransport, session: str = "S1", target: str = "T1") -> None:
        transport.on_event(
            "Target.attachedToTarget",
            {"sessionId": session, "targetInfo": {"targetId": target, "type": "page"}},
            None,
        )

    def test_a_main_frame_navigation_is_pending_until_it_stops_loading(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        self._attached(transport)
        assert sup.navigation_pending() is False
        transport.on_event(
            "Page.frameRequestedNavigation",
            {"frameId": "T1", "reason": "formSubmissionPost", "disposition": "currentTab"},
            "S1",
        )
        assert sup.navigation_pending() is True
        transport.on_event("Page.frameStartedLoading", {"frameId": "T1"}, "S1")
        assert sup.navigation_pending() is True
        transport.on_event("Page.frameStoppedLoading", {"frameId": "T1"}, "S1")
        assert sup.navigation_pending() is False

    def test_iframes_and_new_tabs_do_not_count(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        self._attached(transport)
        transport.on_event("Page.frameStartedLoading", {"frameId": "ad-frame"}, "S1")
        transport.on_event(
            "Page.frameRequestedNavigation",
            {"frameId": "T1", "disposition": "newTab"},
            "S1",
        )
        assert sup.navigation_pending() is False

    def test_a_load_that_never_finishes_stops_counting(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        self._attached(transport)
        transport.on_event("Page.frameStartedLoading", {"frameId": "T1"}, "S1")
        assert sup.navigation_pending(max_age_s=60.0) is True
        assert sup.navigation_pending(max_age_s=0.0) is False

    def test_a_detached_page_is_forgotten(self) -> None:
        transport = FakeTransport()
        sup = _supervisor(transport)
        self._attached(transport)
        transport.on_event("Page.frameStartedLoading", {"frameId": "T1"}, "S1")
        transport.on_event("Target.detachedFromTarget", {"sessionId": "S1"}, None)
        assert sup.navigation_pending() is False


class TestEvalFraming:
    def test_evaluate_returns_value(self) -> None:
        transport = FakeTransport(evaluate_result="Hello")
        sup = _supervisor(transport)
        result = sup.evaluate_runtime("document.title")
        assert result == {"ok": True, "result": "Hello"}
        method, params = next(c for c in transport.calls if c[0] == "Runtime.evaluate")
        assert params["returnByValue"] is True
        assert params["awaitPromise"] is True

    def test_js_exception_is_failure(self) -> None:
        transport = FakeTransport(evaluate_error="ReferenceError: x is not defined")
        sup = _supervisor(transport)
        result = sup.evaluate_runtime("x")
        assert result["ok"] is False
        assert "ReferenceError" in result["error"]

    def test_transport_error_is_signalled(self) -> None:
        class Broken(FakeTransport):
            def call(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
                if method == "Runtime.evaluate":
                    raise RuntimeError("socket closed")
                return {}

        transport = Broken()
        sup = _supervisor(transport)
        result = sup.evaluate_runtime("1+1")
        assert result["ok"] is False
        assert "transport" in result["error"].lower()


class TestRegistry:
    def test_get_or_start_is_idempotent(self) -> None:
        registry = SupervisorRegistry()
        t1 = FakeTransport()
        sup1 = registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=t1)
        sup2 = registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=t1)
        assert sup1 is sup2

    def test_stop_removes_and_tears_down(self) -> None:
        registry = SupervisorRegistry()
        transport = FakeTransport()
        registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=transport)
        registry.stop("k")
        assert registry.get("k") is None
        assert transport.stopped is True

    def test_stop_all(self) -> None:
        registry = SupervisorRegistry()
        registry.get_or_start("a", "ws://127.0.0.1:1/a", transport=FakeTransport())
        registry.get_or_start("b", "ws://127.0.0.1:1/b", transport=FakeTransport())
        registry.stop_all()
        assert registry.get("a") is None
        assert registry.get("b") is None

    def test_a_supervisor_that_fails_to_attach_is_not_kept(self) -> None:
        """Keeping the dead object would make every later lookup take it and
        never retry, so dialogs would go unanswered for the whole session."""

        class DeadTransport(FakeTransport):
            def start(self, on_event: Any) -> None:
                raise RuntimeError("connection refused")

        registry = SupervisorRegistry()
        with pytest.raises(RuntimeError):
            registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=DeadTransport())
        assert registry.get("k") is None

        # A later attempt with a working transport must succeed.
        good = registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=FakeTransport())
        assert good.active is True

    def test_replacing_a_dead_supervisor_does_not_hold_the_registry_lock(self) -> None:
        """Teardown must not stall every other session.

        `stop()` reaches `_WebSocketTransport.stop()`, bounded at 5s on the
        close call plus 5s on the thread join — and this branch runs exactly
        when the connection is dead, the case that pays both in full. `get`,
        `stop` and `stop_all` share one process-wide lock, so holding it across
        teardown queues the dialog and eval paths, `_drop_session`, the idle
        reaper and gateway shutdown behind one wedged socket.

        The lock is probed with a timeout rather than timed: a regression makes
        this fail instead of hang.
        """
        registry = SupervisorRegistry()
        teardown_started = threading.Event()
        release_teardown = threading.Event()

        class BlockingStopTransport(FakeTransport):
            def stop(self) -> None:
                teardown_started.set()
                release_teardown.wait(timeout=5)
                super().stop()

        registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=BlockingStopTransport())
        registry.get("k")._active = False  # force the "connection dead" branch

        worker = threading.Thread(
            target=registry.get_or_start,
            args=("k", "ws://127.0.0.1:1/a"),
            kwargs={"transport": FakeTransport()},
        )
        worker.start()
        try:
            assert teardown_started.wait(timeout=5), "teardown never ran"
            acquired = registry._lock.acquire(timeout=2)
            if acquired:
                registry._lock.release()
            assert acquired, "registry lock is held while a supervisor is torn down"
        finally:
            release_teardown.set()
            worker.join(timeout=5)

    def test_the_replaced_supervisor_is_still_torn_down(self) -> None:
        """Moving the stop out of the lock must not drop it."""
        registry = SupervisorRegistry()
        first = FakeTransport()
        registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=first)
        registry.get("k")._active = False

        registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=FakeTransport())

        assert first.stopped is True

    def test_the_old_connection_is_stopped_before_the_new_one_starts(self) -> None:
        """Two live CDP attachments to one page must not overlap."""
        order: list[str] = []
        registry = SupervisorRegistry()

        class Recording(FakeTransport):
            def __init__(self, label: str) -> None:
                super().__init__()
                self._label = label

            def start(self, on_event: Any) -> None:
                order.append(f"start:{self._label}")
                super().start(on_event)

            def stop(self) -> None:
                order.append(f"stop:{self._label}")
                super().stop()

        registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=Recording("old"))
        registry.get("k")._active = False
        registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=Recording("new"))

        assert order == ["start:old", "stop:old", "start:new"]

    def test_a_live_supervisor_on_the_same_url_is_reused_untouched(self) -> None:
        """The short-circuit still fires, so nothing is torn down needlessly."""
        registry = SupervisorRegistry()
        transport = FakeTransport()
        first = registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=transport)

        second = registry.get_or_start("k", "ws://127.0.0.1:1/a", transport=FakeTransport())

        assert second is first
        assert transport.stopped is False


def test_invalid_dialog_policy_rejected() -> None:
    with pytest.raises(ValueError, match="dialog_policy"):
        CDPSupervisor("s", "ws://127.0.0.1:1/x", dialog_policy="bogus", transport=FakeTransport())


class TestDialogWatchdog:
    """``must_respond`` must not wedge the page forever.

    A JavaScript dialog blocks the page's event loop, so an unanswered dialog
    leaves the browser unusable. ``dialog_timeout_s`` used to be parsed and then
    ignored — these lock the timer in.
    """

    def test_unanswered_dialog_is_dismissed_after_the_timeout(self) -> None:
        transport = FakeTransport()
        sup = CDPSupervisor(
            "s1",
            "ws://127.0.0.1:9/x",
            dialog_policy="must_respond",
            dialog_timeout_s=0.05,
            transport=transport,
        )
        sup.start()
        transport.push_dialog("confirm", "still there?")
        assert sup.snapshot().pending_dialogs, "dialog should start out pending"

        deadline = time.time() + 3.0
        while sup.snapshot().pending_dialogs and time.time() < deadline:
            time.sleep(0.02)

        assert sup.snapshot().pending_dialogs == []
        handled = [c for c in transport.calls if c[0] == "Page.handleJavaScriptDialog"]
        assert handled and handled[-1][1]["accept"] is False
        assert sup.snapshot().recent_dialogs[-1]["action"] == "timed_out"

    def test_answering_in_time_cancels_the_watchdog(self) -> None:
        transport = FakeTransport()
        sup = CDPSupervisor(
            "s2",
            "ws://127.0.0.1:9/x",
            dialog_policy="must_respond",
            dialog_timeout_s=0.05,
            transport=transport,
        )
        sup.start()
        transport.push_dialog("confirm", "answer me")
        assert sup.respond_to_dialog("accept")["ok"] is True

        time.sleep(0.2)  # past the timeout the watchdog would have fired at
        actions = [r["action"] for r in sup.snapshot().recent_dialogs]
        assert actions == ["accept"], "watchdog must not double-resolve an answered dialog"

    def test_transport_failure_rearms_the_watchdog(self) -> None:
        """Answering can fail transiently. The dialog goes back to pending, so
        its timer has to go back too — the engine runs with --no-auto-dialog, so
        nothing else would ever dismiss it and the page stays blocked."""

        class FlakyTransport(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = True

            def call(
                self,
                method: str,
                params: dict[str, Any],
                timeout: float,
                session_id: str | None = None,
            ) -> dict[str, Any]:
                if method == "Page.handleJavaScriptDialog" and self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("target detached")
                return super().call(method, params, timeout, session_id)

        transport = FlakyTransport()
        sup = CDPSupervisor(
            "s4",
            "ws://127.0.0.1:9/x",
            dialog_policy="must_respond",
            dialog_timeout_s=0.05,
            transport=transport,
        )
        sup.start()
        transport.push_dialog("confirm", "flaky")
        assert sup.respond_to_dialog("accept")["ok"] is False
        assert sup.snapshot().pending_dialogs, "dialog must be back to pending"

        deadline = time.time() + 3.0
        while sup.snapshot().pending_dialogs and time.time() < deadline:
            time.sleep(0.02)
        assert sup.snapshot().pending_dialogs == [], "re-armed watchdog should have fired"

    def test_stop_cancels_pending_watchdogs(self) -> None:
        transport = FakeTransport()
        sup = CDPSupervisor(
            "s3",
            "ws://127.0.0.1:9/x",
            dialog_policy="must_respond",
            dialog_timeout_s=5.0,
            transport=transport,
        )
        sup.start()
        transport.push_dialog("alert", "hi")
        sup.stop()
        assert sup._watchdogs == {}
