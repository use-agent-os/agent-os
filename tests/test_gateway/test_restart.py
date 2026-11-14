"""``GatewayRestarter``: the in-place restart sequence, the exec, the announce.

The server, the task runtime, the pid lock, the exec and the operator secret
are all fakes that record what was done to them; the assertions are on the
order and the arguments, which is what the launchers (desktop, launchd,
systemd, the CLI pidfile) depend on.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos import __version__
from agentos.channels.types import OutgoingMessage
from agentos.gateway import restart as restart_mod
from agentos.gateway.agent_surface import OPERATOR_SECRET_FILE_ENV
from agentos.gateway.readiness import MIN_RESTART_EXPECTED_MS
from agentos.gateway.restart import (
    FORCE_DRAIN_TIMEOUT_S,
    SAFE_DRAIN_TIMEOUT_S,
    STALE_REQUEST_S,
    GatewayRestarter,
    RestartOrigin,
    RestartPendingError,
    RestartRequest,
    RestartUnsupportedError,
    announce_restart_completion,
    normalize_mode,
    read_and_clear_request,
    restart_request_path,
)

# ── fakes ────────────────────────────────────────────────────────────


class _FakeRuntime:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.admitting = True

    def stop_admission(self) -> None:
        self.admitting = False
        self.events.append("stop_admission")

    def resume_admission(self) -> None:
        self.admitting = True
        self.events.append("resume_admission")


class _FakeServer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.app = SimpleNamespace(state=SimpleNamespace(gateway_ready=True))
        self._services = SimpleNamespace(task_runtime=_FakeRuntime(events))
        self.close_calls: list[dict[str, Any]] = []
        self.close_error: BaseException | None = None

    async def close(self, reason: str = "shutdown", **kwargs: Any) -> None:
        self.events.append("close")
        self.close_calls.append({"reason": reason, **kwargs})
        if self.close_error is not None:
            raise self.close_error


class _FakeLock:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def release(self) -> None:
        self.events.append("pidlock_release")


class _FakeSurface:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def export_operator_secret_file(self) -> Path | None:
        return self.path


def _config(tmp_path: Path) -> Any:
    return SimpleNamespace(state_dir=str(tmp_path / "state"))


def _restarter(
    tmp_path: Path,
    *,
    events: list[str] | None = None,
    secret_path: Path | None = None,
    exec_fn: Any = None,
    supported: bool = True,
) -> tuple[GatewayRestarter, _FakeServer, list[str], list[tuple[str, list[str], dict]]]:
    events = events if events is not None else []
    server = _FakeServer(events)
    execs: list[tuple[str, list[str], dict]] = []

    def _record_exec(path: str, argv: list[str], env: dict) -> None:
        events.append("exec")
        execs.append((path, argv, env))

    async def _sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds}")

    restarter = GatewayRestarter(
        server,
        _config(tmp_path),
        pid_lock=_FakeLock(events),
        exec_fn=exec_fn or _record_exec,
        task_factory=asyncio.create_task,
        agent_surface=_FakeSurface(secret_path),
        sleep=_sleep,
        supported=supported,
    )
    return restarter, server, events, execs


# ── request() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_returns_at_once_and_schedules_one_task(tmp_path: Path) -> None:
    restarter, server, events, _ = _restarter(tmp_path)

    req = restarter.request(reason="test", mode="safe", origin=RestartOrigin(kind="none"))

    assert req.restart_id and req.mode == "safe" and req.pid
    assert restarter.pending is req
    assert restarter.task is not None
    # Nothing happened synchronously: the caller gets to acknowledge first.
    assert events == []
    assert server.close_calls == []

    with pytest.raises(RestartPendingError):
        restarter.request(reason="again", mode="safe")

    await restarter.task


def test_request_refused_where_unsupported(tmp_path: Path) -> None:
    restarter, _, _, _ = _restarter(tmp_path, supported=False)
    with pytest.raises(RestartUnsupportedError, match="Windows"):
        restarter.request(reason="test")
    assert restarter.pending is None


def test_windows_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restart_mod.os, "name", "nt")
    assert restart_mod.in_place_restart_supported() is False


@pytest.mark.parametrize(("raw", "mode"), [(None, "safe"), ("", "safe"), ("FORCE", "force")])
def test_normalize_mode_accepts_safe_and_force(raw: Any, mode: str) -> None:
    assert normalize_mode(raw) == mode


def test_normalize_mode_rejects_anything_else() -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        normalize_mode("hard")


# ── the background sequence ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_safe_restart_runs_the_sequence_in_order(tmp_path: Path) -> None:
    restarter, server, events, _ = _restarter(tmp_path, secret_path=tmp_path / "s")
    origin = RestartOrigin(kind="channel", channel_name="telegram-main", channel_id="42")

    req = restarter.request(reason="slash_command", mode="safe", origin=origin)
    await restarter.task

    assert events == [f"sleep:{restart_mod.ACK_GRACE_S}", "stop_admission", "close"]
    assert server.app.state.gateway_ready is False
    # The fake server has no readiness object, so the estimate is the floor.
    assert server.close_calls == [
        {
            "reason": "restart:safe",
            "drain_timeout": SAFE_DRAIN_TIMEOUT_S,
            "cancel_in_flight": False,
            "restart_expected_ms": MIN_RESTART_EXPECTED_MS,
        }
    ]
    plan = restarter.exec_plan
    assert plan is not None
    argv, env = plan
    assert argv == [sys.executable, "-m", "agentos.cli.main", *sys.argv[1:]]
    assert env[OPERATOR_SECRET_FILE_ENV] == str(tmp_path / "s")

    stored = json.loads(restart_request_path(_config(tmp_path)).read_text(encoding="utf-8"))
    assert stored["restartId"] == req.restart_id
    assert stored["origin"]["channelName"] == "telegram-main"
    assert stored["origin"]["channelId"] == "42"
    assert stored["fromVersion"] == __version__
    assert isinstance(stored["execAt"], float)


@pytest.mark.asyncio
async def test_force_restart_cancels_in_flight(tmp_path: Path) -> None:
    restarter, server, _, _ = _restarter(tmp_path)

    restarter.request(reason="cli", mode="force")
    await restarter.task

    assert server.close_calls == [
        {
            "reason": "restart:force",
            "drain_timeout": FORCE_DRAIN_TIMEOUT_S,
            "cancel_in_flight": True,
            "restart_expected_ms": MIN_RESTART_EXPECTED_MS,
        }
    ]


@pytest.mark.asyncio
async def test_scrubbed_gateway_token_is_handed_to_the_new_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot drops the env-sourced token from os.environ; the re-exec needs it back."""
    from agentos.gateway import credential_env

    credential_env.reset_scrubbed_credential_env()
    monkeypatch.setenv("AGENTOS_AUTH_TOKEN", "compose-token")
    credential_env.scrub_credential_env()
    try:
        assert "AGENTOS_AUTH_TOKEN" not in os.environ
        restarter, _, _, _ = _restarter(tmp_path, secret_path=None)
        restarter.request(reason="cli")
        await restarter.task
        assert restarter.exec_plan is not None
        assert restarter.exec_plan[1]["AGENTOS_AUTH_TOKEN"] == "compose-token"
    finally:
        credential_env.reset_scrubbed_credential_env()


@pytest.mark.asyncio
async def test_no_desktop_secret_means_no_secret_in_env(tmp_path: Path) -> None:
    restarter, _, _, _ = _restarter(tmp_path, secret_path=None)
    restarter.request(reason="cli")
    await restarter.task
    assert restarter.exec_plan is not None
    assert OPERATOR_SECRET_FILE_ENV not in restarter.exec_plan[1]


@pytest.mark.asyncio
async def test_failure_before_close_keeps_the_gateway_running(tmp_path: Path) -> None:
    """The request file cannot be written: nothing has been closed, undo."""
    restarter, server, events, _ = _restarter(tmp_path)
    state_dir = Path(_config(tmp_path).state_dir)
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    state_dir.write_text("not a directory", encoding="utf-8")

    restarter.request(reason="cli")
    await restarter.task

    assert "close" not in events
    assert "resume_admission" in events
    assert server.app.state.gateway_ready is True
    assert restarter.pending is None
    assert restarter.task is None
    assert restarter.exec_plan is None
    # The gateway is back to normal: a new request is accepted.
    restarter.request(reason="cli")
    assert restarter.pending is not None


@pytest.mark.asyncio
async def test_close_failure_still_leads_to_the_exec(tmp_path: Path) -> None:
    """Once close() has started there is no way back; a bad adapter stop
    must not strand a half-closed gateway."""
    restarter, server, _, _ = _restarter(tmp_path)
    server.close_error = RuntimeError("adapter stop blew up")

    restarter.request(reason="cli")
    await restarter.task

    assert restarter.exec_plan is not None


@pytest.mark.asyncio
async def test_close_timeout_still_leads_to_the_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart_mod, "CLOSE_SLACK_S", 0.0)
    monkeypatch.setattr(restart_mod, "FORCE_DRAIN_TIMEOUT_S", 0.0)
    restarter, server, _, _ = _restarter(tmp_path)

    async def hang(reason: str = "shutdown", **kwargs: Any) -> None:
        await asyncio.sleep(5)

    server.close = hang  # type: ignore[method-assign]
    restarter.request(reason="cli", mode="force")
    await restarter.task

    assert restarter.exec_plan is not None


# ── exec_if_pending() ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_releases_the_pid_lock_then_execs(tmp_path: Path) -> None:
    restarter, _, events, execs = _restarter(tmp_path, secret_path=tmp_path / "s")
    restarter.request(reason="cli")
    await restarter.task

    restarter.exec_if_pending()

    assert events[-2:] == ["pidlock_release", "exec"]
    path, argv, env = execs[0]
    assert path == sys.executable
    assert argv[:3] == [sys.executable, "-m", "agentos.cli.main"]
    assert env[OPERATOR_SECRET_FILE_ENV] == str(tmp_path / "s")


def test_exec_without_a_plan_is_a_no_op(tmp_path: Path) -> None:
    restarter, _, events, execs = _restarter(tmp_path)
    restarter.exec_if_pending()
    assert events == [] and execs == []


@pytest.mark.asyncio
async def test_exec_failure_exits_one_and_drops_the_request(tmp_path: Path) -> None:
    def boom(path: str, argv: list[str], env: dict) -> None:
        raise OSError("no such interpreter")

    restarter, _, _, _ = _restarter(tmp_path, exec_fn=boom)
    restarter.request(reason="cli")
    await restarter.task
    assert restart_request_path(_config(tmp_path)).exists()

    with pytest.raises(SystemExit) as exc:
        restarter.exec_if_pending()

    assert exc.value.code == 1
    assert not restart_request_path(_config(tmp_path)).exists()


@pytest.mark.asyncio
async def test_plan_failure_after_close_is_fatal(tmp_path: Path) -> None:
    def bad_argv() -> list[str]:
        raise RuntimeError("no argv")

    events: list[str] = []
    server = _FakeServer(events)
    restarter = GatewayRestarter(
        server,
        _config(tmp_path),
        pid_lock=_FakeLock(events),
        exec_fn=lambda *a: events.append("exec"),
        argv_factory=bad_argv,
        task_factory=asyncio.create_task,
        agent_surface=_FakeSurface(None),
        sleep=lambda s: asyncio.sleep(0),
        supported=True,
    )
    restarter.request(reason="cli")
    await restarter.task

    assert "close" in events
    assert restarter.exec_plan is None
    with pytest.raises(SystemExit):
        restarter.exec_if_pending()
    assert "exec" not in events


# ── the request file after boot ─────────────────────────────────────


def _write_request(tmp_path: Path, req: RestartRequest) -> None:
    path = restart_request_path(_config(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(req.to_json()), encoding="utf-8")


def test_read_and_clear_returns_a_fresh_request_once(tmp_path: Path) -> None:
    now = time.time()
    req = RestartRequest(
        restart_id="abc",
        reason="slash_command",
        mode="safe",
        origin=RestartOrigin(kind="web", session_key="agent:main:webchat:x"),
        requested_at=now - 20,
        exec_at=now - 5,
    )
    _write_request(tmp_path, req)

    loaded = read_and_clear_request(_config(tmp_path), now=now)

    assert loaded is not None
    assert loaded.restart_id == "abc"
    assert loaded.origin == req.origin
    assert not restart_request_path(_config(tmp_path)).exists()
    assert read_and_clear_request(_config(tmp_path), now=now) is None


def test_read_and_clear_drops_a_stale_request(tmp_path: Path) -> None:
    now = time.time()
    req = RestartRequest(
        restart_id="old", reason="cli", mode="safe", exec_at=now - STALE_REQUEST_S - 1
    )
    _write_request(tmp_path, req)

    assert read_and_clear_request(_config(tmp_path), now=now) is None
    assert not restart_request_path(_config(tmp_path)).exists()


def test_read_and_clear_drops_malformed_json(tmp_path: Path) -> None:
    path = restart_request_path(_config(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert read_and_clear_request(_config(tmp_path)) is None
    assert not path.exists()


def test_read_and_clear_without_a_file() -> None:
    assert read_and_clear_request(SimpleNamespace(state_dir="/nonexistent/agentos")) is None


# ── announcing ───────────────────────────────────────────────────────


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


class _FakeChannelManager:
    def __init__(self, adapter: Any, *, channel_type: str) -> None:
        self.adapter = adapter
        self.channel_type = channel_type
        self.calls: list[dict[str, str]] = []

    def resolve_delivery_target(
        self, *, target: str, to: str = "", account_id: str = "", thread_id: str = ""
    ) -> Any:
        self.calls.append(
            {"target": target, "to": to, "account_id": account_id, "thread_id": thread_id}
        )
        return SimpleNamespace(
            ok=True,
            adapter=self.adapter,
            channel_type=self.channel_type,
            to=to,
            thread_id=thread_id,
        )


@pytest.mark.asyncio
async def test_announce_posts_to_the_telegram_topic_that_asked(tmp_path: Path) -> None:
    now = time.time()
    req = RestartRequest(
        restart_id="r1",
        reason="slash_command",
        mode="safe",
        origin=RestartOrigin(
            kind="channel",
            channel_name="telegram-main",
            channel_type="telegram",
            channel_id="-100123",
            thread_id="77",
        ),
        requested_at=now - 12,
        exec_at=now - 3,
        from_version=__version__,
    )
    _write_request(tmp_path, req)
    adapter = _FakeAdapter()
    cm = _FakeChannelManager(adapter, channel_type="telegram")

    ok = await announce_restart_completion(
        _config(tmp_path), channel_manager=cm, session_manager=None, now=now
    )

    assert ok is True
    assert cm.calls == [
        {"target": "telegram-main", "to": "-100123", "account_id": "", "thread_id": "77"}
    ]
    (message,) = adapter.sent
    assert message.content == f"Gateway restarted (v{__version__}, 12s)."
    assert message.reply_to == "77"
    assert message.metadata == {"channel": "-100123"}


@pytest.mark.asyncio
async def test_announce_mentions_a_version_change(tmp_path: Path) -> None:
    now = time.time()
    req = RestartRequest(
        restart_id="r2",
        reason="cli",
        mode="safe",
        origin=RestartOrigin(kind="channel", channel_name="slack-main", channel_id="C1"),
        requested_at=now - 40,
        exec_at=now - 1,
        from_version="2000.1.1",
    )
    _write_request(tmp_path, req)
    adapter = _FakeAdapter()

    await announce_restart_completion(
        _config(tmp_path),
        channel_manager=_FakeChannelManager(adapter, channel_type="slack"),
        session_manager=None,
        now=now,
    )

    (message,) = adapter.sent
    assert message.content.endswith(" Version changed from 2000.1.1.")
    assert message.metadata["channel"] == "C1"


@pytest.mark.asyncio
async def test_announce_appends_to_the_web_session(tmp_path: Path) -> None:
    now = time.time()
    req = RestartRequest(
        restart_id="r3",
        reason="slash_command",
        mode="safe",
        origin=RestartOrigin(kind="web", session_key="agent:main:webchat:abc"),
        requested_at=now - 9,
        exec_at=now - 2,
    )
    _write_request(tmp_path, req)
    appended: list[dict[str, Any]] = []

    class _Sessions:
        async def get_session(self, key: str) -> Any:
            return object() if key == "agent:main:webchat:abc" else None

        async def append_message(self, key: str, **kwargs: Any) -> None:
            appended.append({"key": key, **kwargs})

    ok = await announce_restart_completion(
        _config(tmp_path), channel_manager=None, session_manager=_Sessions(), now=now
    )

    assert ok is True
    assert appended == [
        {
            "key": "agent:main:webchat:abc",
            "role": "assistant",
            "content": f"Gateway restarted (v{__version__}, 9s).",
            "provenance": {"kind": "system", "source": "gateway.restart"},
        }
    ]


@pytest.mark.asyncio
async def test_announce_skips_a_web_session_that_is_gone(tmp_path: Path) -> None:
    req = RestartRequest(
        restart_id="r4",
        reason="cli",
        mode="safe",
        origin=RestartOrigin(kind="web", session_key="agent:main:webchat:gone"),
    )
    _write_request(tmp_path, req)

    class _Sessions:
        async def get_session(self, key: str) -> Any:
            return None

        async def append_message(self, key: str, **kwargs: Any) -> None:
            raise AssertionError("must not append to a missing session")

    assert (
        await announce_restart_completion(
            _config(tmp_path), channel_manager=None, session_manager=_Sessions()
        )
        is False
    )


@pytest.mark.asyncio
async def test_announce_with_no_origin_only_logs(tmp_path: Path) -> None:
    _write_request(tmp_path, RestartRequest(restart_id="r5", reason="cli", mode="safe"))
    assert (
        await announce_restart_completion(
            _config(tmp_path), channel_manager=None, session_manager=None
        )
        is False
    )
    assert not restart_request_path(_config(tmp_path)).exists()


# ── origins ──────────────────────────────────────────────────────────


def test_origin_from_tool_context_distinguishes_web_and_channel() -> None:
    web = RestartOrigin.from_tool_context(
        SimpleNamespace(
            channel_kind="webchat",
            channel_id="webchat:k",
            session_key="agent:main:webchat:k",
            source_kind="webui",
            sender_id="control",
        )
    )
    assert web.kind == "web" and web.session_key == "agent:main:webchat:k"

    chan = RestartOrigin.from_tool_context(
        SimpleNamespace(
            channel_kind="telegram-main",
            channel_id="123",
            session_key="agent:main:telegram:123",
            source_kind="channel",
            sender_id="u1",
        )
    )
    assert chan.kind == "channel"
    assert chan.channel_name == "telegram-main" and chan.channel_id == "123"

    assert RestartOrigin.from_tool_context(None).kind == "none"


def test_origin_round_trips_through_json() -> None:
    origin = RestartOrigin(
        kind="channel",
        session_key="k",
        channel_name="discord-main",
        channel_type="discord",
        channel_id="c",
        thread_id="t",
        account_id="a",
        sender_id="s",
    )
    assert RestartOrigin.from_json(origin.to_json()) == origin
    assert RestartOrigin.from_json({"kind": "bogus"}).kind == "none"
    assert RestartOrigin.from_json("nope").kind == "none"
