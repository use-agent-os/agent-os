"""Tests for the updates.check gateway RPC handler."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentos.cli import update_notice
from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.config import GatewayConfig, UpdatesConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher


@pytest.fixture(autouse=True)
def _state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTOS_NO_UPDATE_NOTICE", raising=False)
    for var in update_notice._CI_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _mock_latest(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setattr("agentos.compat.pypi_client.latest_version", lambda timeout=2.0: value)


@pytest.mark.asyncio
async def test_updates_check_control_only() -> None:
    entry = get_dispatcher().get_entry("updates.check")
    assert entry is not None
    assert entry.audiences == CONTROL_ONLY


@pytest.mark.asyncio
async def test_updates_check_outdated(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_latest(monkeypatch, "2099.1.1")
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(),
    )

    response = await get_dispatcher().dispatch("req-1", "updates.check", {}, ctx)
    assert response.ok is True
    assert response.payload["latest"] == "2099.1.1"
    assert response.payload["status"] == "outdated"


@pytest.mark.asyncio
async def test_updates_check_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_latest(monkeypatch, "0.0.0+unknown")
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(),
    )

    response = await get_dispatcher().dispatch("req-1", "updates.check", {}, ctx)
    assert response.ok is True
    assert response.payload["latest"] == "0.0.0+unknown"
    assert response.payload["status"] == "up-to-date"


@pytest.mark.asyncio
async def test_updates_check_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_latest(monkeypatch, None)
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(),
    )

    response = await get_dispatcher().dispatch("req-1", "updates.check", {}, ctx)
    assert response.ok is True
    assert response.payload["latest"] is None
    assert response.payload["status"] == "offline"


@pytest.mark.asyncio
async def test_updates_check_respects_notify_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_latest(monkeypatch, "2099.1.1")
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(updates=UpdatesConfig(notify=False)),
    )

    response = await get_dispatcher().dispatch("req-1", "updates.check", {}, ctx)
    assert response.ok is True
    assert response.payload["latest"] is None
    assert response.payload["status"] == "offline"


@pytest.mark.asyncio
async def test_updates_check_respects_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_latest(monkeypatch, "2099.1.1")
    monkeypatch.setenv("AGENTOS_NO_UPDATE_NOTICE", "1")
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(),
    )

    response = await get_dispatcher().dispatch("req-1", "updates.check", {}, ctx)
    assert response.ok is True
    assert response.payload["latest"] is None
    assert response.payload["status"] == "offline"


@pytest.mark.asyncio
async def test_updates_check_throttling_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_latest(monkeypatch, "2099.1.1")
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(),
    )

    response1 = await get_dispatcher().dispatch("req-1", "updates.check", {}, ctx)
    assert response1.ok is True
    assert response1.payload["latest"] == "2099.1.1"
    assert response1.payload["status"] == "outdated"

    # Mock a newer version on PyPI, but check should hit cache and still return 2099.1.1
    _mock_latest(monkeypatch, "2099.2.2")
    response2 = await get_dispatcher().dispatch("req-2", "updates.check", {}, ctx)
    assert response2.ok is True
    assert response2.payload["latest"] == "2099.1.1"


@pytest.mark.asyncio
async def test_updates_check_force_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_latest(monkeypatch, "2099.1.1")
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    response1 = await get_dispatcher().dispatch("req-1", "updates.check", {}, ctx)
    assert response1.payload["latest"] == "2099.1.1"

    # A manual click passes force=True and must see the newer PyPI release
    # even though the throttle window has not elapsed.
    _mock_latest(monkeypatch, "2099.2.2")
    response2 = await get_dispatcher().dispatch("req-2", "updates.check", {"force": True}, ctx)
    assert response2.ok is True
    assert response2.payload["latest"] == "2099.2.2"
    assert response2.payload["status"] == "outdated"

    # Forcing still respects the opt-out.
    monkeypatch.setenv("AGENTOS_NO_UPDATE_NOTICE", "1")
    response3 = await get_dispatcher().dispatch("req-3", "updates.check", {"force": True}, ctx)
    assert response3.payload["status"] == "offline"


@pytest.mark.asyncio
async def test_updates_check_namespaced_from_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.compat.pypi_client import notice_state_path, read_state

    _mock_latest(monkeypatch, "2099.1.1")
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(),
    )

    # 1. Run web UI update check.
    response = await get_dispatcher().dispatch("req-1", "updates.check", {}, ctx)
    assert response.ok is True
    assert response.payload["latest"] == "2099.1.1"

    # Verify state has "webui" namespace and "latest" at root.
    path = notice_state_path()
    state = read_state(path)
    assert "webui" in state
    assert "last_checked" in state["webui"]
    assert state["latest"] == "2099.1.1"

    # 2. Run CLI update check immediately after.
    # Even though Web UI just checked, CLI check is still due since it's namespaced separately.
    _mock_latest(monkeypatch, "2099.2.2")  # mock a newer one to verify it actually checks
    monkeypatch.setattr(update_notice, "_stderr_is_tty", lambda: True)
    msg = update_notice.maybe_emit_update_notice(current_version="2026.7.18")
    assert msg is not None
    assert "2099.2.2" in msg

    # State should now contain both "webui" and "cli" namespaces, with shared "latest".
    state = read_state(path)
    assert "cli" in state
    assert "last_checked" in state["cli"]
    assert "webui" in state
    assert state["latest"] == "2099.2.2"


# --- updates.apply / updates.status / updates.verifyData --------------------

from agentos.gateway import rpc_updates  # noqa: E402


def _ctx() -> RpcContext:
    return RpcContext(conn_id="test", config=GatewayConfig())


@pytest.mark.asyncio
async def test_apply_status_and_verify_are_control_only() -> None:
    for name in ("updates.apply", "updates.status", "updates.verifyData"):
        entry = get_dispatcher().get_entry(name)
        assert entry is not None, name
        assert entry.audiences == CONTROL_ONLY, name


@pytest.mark.asyncio
async def test_status_is_idle_without_a_job() -> None:
    response = await get_dispatcher().dispatch("r", "updates.status", {}, _ctx())
    assert response.ok is True
    assert response.payload == {"status": "idle"}


@pytest.mark.asyncio
async def test_apply_spawns_a_detached_runner_and_records_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: dict[str, object] = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProc:
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(rpc_updates.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rpc_updates, "_pid_alive", lambda pid: pid == 4242)

    response = await get_dispatcher().dispatch("r", "updates.apply", {"source": "github"}, _ctx())
    assert response.ok is True
    assert response.payload["started"] is True
    assert response.payload["status"] == "running"
    assert response.payload["pid"] == 4242
    assert response.payload["source"] == "github"

    argv = spawned["argv"]
    assert isinstance(argv, list)
    assert argv[0] == rpc_updates.sys.executable and argv[1] == "-c"
    runner_job = json.loads(argv[3])
    assert runner_job["command"][-4:] == ["upgrade", "--json", "--source", "github"]
    assert runner_job["env"]["AGENTOS_NO_UPDATE_NOTICE"] == "1"
    assert runner_job["log"] == str(rpc_updates.log_path())
    kwargs = spawned["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["start_new_session"] is (os.name != "nt")

    # A second click while it runs must not spawn again.
    again = await get_dispatcher().dispatch("r", "updates.apply", {}, _ctx())
    assert again.payload["started"] is False
    assert again.payload["status"] == "running"


@pytest.mark.asyncio
async def test_apply_rejects_an_unknown_source() -> None:
    response = await get_dispatcher().dispatch("r", "updates.apply", {"source": "ftp"}, _ctx())
    assert response.ok is False


@pytest.mark.asyncio
async def test_status_reports_result_from_the_finished_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc_updates._write_job(
        {"status": "done", "pid": 1, "source": "auto", "startedAt": 1.0, "exitCode": 0}
    )
    log = rpc_updates.log_path()
    log.write_text(
        "Upgrading use-agent-os via uv-tool from pypi…\n"
        '{"old": "1.0.0", "new": "1.0.1", "restarted": true, "verified": true}\n',
        encoding="utf-8",
    )
    response = await get_dispatcher().dispatch("r", "updates.status", {}, _ctx())
    assert response.payload["status"] == "done"
    assert response.payload["exitCode"] == 0
    assert response.payload["result"] == {
        "old": "1.0.0",
        "new": "1.0.1",
        "restarted": True,
        "verified": True,
    }
    assert response.payload["logTail"][0].startswith("Upgrading")


@pytest.mark.asyncio
async def test_status_marks_a_vanished_runner_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    rpc_updates._write_job({"status": "running", "pid": 999999, "startedAt": 1.0})
    monkeypatch.setattr(rpc_updates, "_pid_alive", lambda pid: False)
    response = await get_dispatcher().dispatch("r", "updates.status", {}, _ctx())
    assert response.payload["status"] == "failed"
    assert response.payload["exitCode"] is None
    assert rpc_updates._read_job()["status"] == "failed"


@pytest.mark.asyncio
async def test_verify_data_reports_databases_and_latest_snapshot(tmp_path: Path) -> None:
    from agentos.cli import upgrade_snapshot

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "bad.db").write_bytes(b"nope" * 100)
    snap = upgrade_snapshot.create_snapshot(version="1")
    response = await get_dispatcher().dispatch("r", "updates.verifyData", {}, _ctx())
    assert response.ok is True
    assert response.payload["ok"] is False
    assert response.payload["problems"][0]["path"] == str(state / "bad.db")
    assert response.payload["snapshot"] == str(snap.path)


def test_agentos_command_prefers_the_sibling_console_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")
    script = tmp_path / "bin" / "agentos"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(rpc_updates.sys, "executable", str(python))
    assert rpc_updates.agentos_command() == [str(script)]

    script.unlink()
    monkeypatch.setattr(rpc_updates.shutil, "which", lambda name: None)
    assert rpc_updates.agentos_command() == [str(python), "-m", "agentos.cli.main"]
