from __future__ import annotations

import pytest

from agentos.gateway.config import GatewayConfig
from agentos.gateway.diagnostics import DiagnosticsState
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_logs import _handle_logs_status, _handle_logs_tail
from agentos.observability.trace import TraceContext, TraceEvent, write_trace_event


@pytest.mark.asyncio
async def test_logs_tail_uses_agentos_log_dir_and_filters_level(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_text(
        "2026-05-03 [DEBUG] agentos: ignored\n2026-05-03 [INFO] agentos: selected\n",
        encoding="utf-8",
    )

    result = await _handle_logs_tail({"limit": 10, "cursor": 0, "level": "INFO"}, None)  # type: ignore[arg-type]

    assert result["lines"] == ["2026-05-03 [INFO] agentos: selected"]
    assert result["cursor"] == log_file.stat().st_size
    assert result["has_more"] is False


def _line(i: int, level: str = "INFO") -> str:
    return f"2026-05-03 [{level}] agentos: line {i}"


async def _tail(**params: object) -> dict:
    return await _handle_logs_tail(dict(params), None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_logs_tail_first_poll_shows_the_newest_lines(tmp_path, monkeypatch) -> None:
    """The first poll is `tail`: a console opened on a big log shows what is
    happening now, not the top of the file. Returning the oldest window here
    would leave the Logs page paging through history for minutes."""
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_text("".join(_line(i) + "\n" for i in range(2000)), encoding="utf-8")

    first = await _tail(limit=500, cursor=0)

    assert first["lines"][-1] == _line(1999)
    assert first["lines"][0] == _line(1500)
    assert first["cursor"] == log_file.stat().st_size
    assert first["has_more"] is True


@pytest.mark.asyncio
async def test_logs_tail_recovers_a_burst_larger_than_limit(tmp_path, monkeypatch) -> None:
    """A burst bigger than `limit` between polls must arrive in full, oldest first."""
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_text(_line(0) + "\n", encoding="utf-8")
    cursor = (await _tail(limit=500, cursor=0))["cursor"]

    with log_file.open("a", encoding="utf-8") as fh:
        fh.write("".join(_line(i) + "\n" for i in range(1, 11)))

    seen: list[str] = []
    for _ in range(5):
        page = await _tail(limit=4, cursor=cursor)
        seen += page["lines"]
        cursor = page["cursor"]
        if not page["has_more"]:
            break

    assert seen == [_line(i) for i in range(1, 11)]
    assert cursor == log_file.stat().st_size


@pytest.mark.asyncio
async def test_logs_tail_holds_back_a_line_still_being_written(tmp_path, monkeypatch) -> None:
    """A trailing line with no newline yet is left for the next poll, whole."""
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_text(_line(0) + "\n", encoding="utf-8")
    cursor = (await _tail(limit=10, cursor=0))["cursor"]

    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(_line(1) + "\n" + "2026-05-03 [INFO] agentos: half-wri")
    page = await _tail(limit=10, cursor=cursor)
    assert page["lines"] == [_line(1)]

    with log_file.open("a", encoding="utf-8") as fh:
        fh.write("tten\n")
    page = await _tail(limit=10, cursor=page["cursor"])
    assert page["lines"] == ["2026-05-03 [INFO] agentos: half-written"]


@pytest.mark.asyncio
async def test_logs_tail_pages_a_level_filter_without_skipping(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_text("{marker}\n".replace("{marker}", _line(-1)), encoding="utf-8")
    cursor = (await _tail(limit=10, cursor=0))["cursor"]
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write("".join(_line(i, "ERROR" if i % 2 else "INFO") + "\n" for i in range(12)))

    errors: list[str] = []
    for _ in range(6):
        page = await _tail(limit=2, cursor=cursor, level="error")
        errors += page["lines"]
        cursor = page["cursor"]
        if not page["has_more"]:
            break

    assert errors == [_line(i, "ERROR") for i in range(1, 12, 2)]


@pytest.mark.asyncio
async def test_logs_tail_missing_file_returns_empty_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))

    result = await _handle_logs_tail({"limit": 10, "cursor": 0}, None)  # type: ignore[arg-type]

    assert result == {"lines": [], "cursor": 0, "has_more": False}


@pytest.mark.asyncio
async def test_logs_status_reports_raw_capture_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG", raising=False)
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_LOG_DIR", raising=False)

    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))

    assert result["raw_turn_call_log"]["enabled"] is False
    assert result["raw_turn_call_log"]["source"] == "off"
    assert result["raw_turn_call_log"]["enable_env"]["set"] is False
    assert result["raw_turn_call_log"]["enable_env"]["truthy"] is False
    assert result["raw_turn_call_log"]["directory"]["source"] == "default"
    assert result["diagnostics_enabled"]["configured"] is False
    assert result["diagnostics_enabled"]["effective"] is False
    assert result["diagnostics_enabled"]["detail"] == "off"
    assert result["diagnostics_enabled"]["controls_raw_turn_call"] is False


@pytest.mark.asyncio
async def test_logs_status_reports_truthy_and_falsy_raw_capture_env(monkeypatch) -> None:
    for value in ("1", "TRUE", " yes ", "on"):
        monkeypatch.setenv("AGENTOS_TURN_CALL_LOG", value)
        result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))
        assert result["raw_turn_call_log"]["enabled"] is True
        assert result["raw_turn_call_log"]["source"] == "env"
        assert result["raw_turn_call_log"]["enable_env"]["truthy"] is True

    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("AGENTOS_TURN_CALL_LOG", value)
        result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))
        assert result["raw_turn_call_log"]["enabled"] is False
        assert result["raw_turn_call_log"]["source"] == "off"
        assert result["raw_turn_call_log"]["enable_env"]["truthy"] is False


@pytest.mark.asyncio
async def test_logs_status_reports_runtime_raw_capture_source(monkeypatch) -> None:
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig())
    state.set_runtime(enabled=True, raw=True)

    result = await _handle_logs_status(
        {},
        RpcContext(conn_id="test", config=GatewayConfig(), diagnostics_state=state),
    )

    assert result["raw_turn_call_log"]["enabled"] is True
    assert result["raw_turn_call_log"]["source"] == "runtime"
    assert result["diagnostics_enabled"]["effective"] is True
    assert result["diagnostics_enabled"]["detail"] == "raw"


@pytest.mark.asyncio
async def test_logs_status_reports_env_source_when_env_and_runtime_raw_are_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENTOS_TURN_CALL_LOG", "1")
    state = DiagnosticsState.from_config(GatewayConfig())
    state.set_runtime(enabled=True, raw=True)

    result = await _handle_logs_status(
        {},
        RpcContext(conn_id="test", config=GatewayConfig(), diagnostics_state=state),
    )

    assert result["raw_turn_call_log"]["enabled"] is True
    assert result["raw_turn_call_log"]["source"] == "env"
    assert result["diagnostics_enabled"]["raw_source"] == "env"


@pytest.mark.asyncio
async def test_logs_status_resolves_raw_directory_precedence_without_creating_paths(
    tmp_path, monkeypatch
) -> None:
    raw_dir = tmp_path / "raw"
    shared_log_dir = tmp_path / "shared"
    monkeypatch.setenv("AGENTOS_TURN_CALL_LOG_DIR", str(raw_dir))
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(shared_log_dir))

    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))

    assert result["raw_turn_call_log"]["directory"] == {
        "path": str(raw_dir),
        "source": "AGENTOS_TURN_CALL_LOG_DIR",
        "exists": False,
    }
    assert not raw_dir.exists()
    assert not shared_log_dir.exists()

    monkeypatch.setenv("AGENTOS_TURN_CALL_LOG_DIR", " ")
    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))

    assert result["raw_turn_call_log"]["directory"] == {
        "path": str(shared_log_dir),
        "source": "AGENTOS_LOG_DIR",
        "exists": False,
    }
    assert not raw_dir.exists()
    assert not shared_log_dir.exists()


@pytest.mark.asyncio
async def test_logs_status_reports_gateway_file_log_path_and_existence(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_text("2026-05-03 [INFO] agentos: selected\n", encoding="utf-8")
    config = GatewayConfig(log_file_enabled=False, log_level="INFO", diagnostics_enabled=True)

    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=config))

    assert result["gateway_file_log"]["enabled"] is False
    assert result["gateway_file_log"]["level"] == "INFO"
    assert result["gateway_file_log"]["path"] == str(log_file)
    assert result["gateway_file_log"]["path_source"] == "AGENTOS_LOG_DIR"
    assert result["gateway_file_log"]["exists"] is True
    assert result["gateway_file_log"]["active_tail_path"] == str(log_file)
    assert result["gateway_file_log"]["active_tail_path_exists"] is True
    assert result["diagnostics_enabled"]["configured"] is True
    assert result["diagnostics_enabled"]["effective"] is True
    assert result["diagnostics_enabled"]["detail"] == "standard"
    assert result["diagnostics_enabled"]["controls_raw_turn_call"] is False


@pytest.mark.asyncio
async def test_logs_status_reports_trace_log_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    write_trace_event(
        TraceEvent(kind="turn_start", context=TraceContext.new(trace_id="trace-1")),
        log_dir=tmp_path,
    )

    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))

    assert result["trace_log"] == {
        "directory": {
            "path": str(tmp_path),
            "source": "AGENTOS_LOG_DIR",
            "exists": True,
        },
        "file_count": 1,
        "latest_path": str(next(tmp_path.glob("traces-*.jsonl"))),
    }


@pytest.mark.asyncio
async def test_logs_trace_returns_persisted_trace_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
    write_trace_event(
        TraceEvent(
            kind="turn_start",
            context=TraceContext.new(
                trace_id="trace-1",
                session_key="agent:main:test",
                turn_id="turn-1",
            ),
            seq=1,
        ),
        log_dir=tmp_path,
    )
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    response = await get_dispatcher().dispatch("req-1", "logs.trace", {"trace_id": "trace-1"}, ctx)

    assert response.ok is True
    assert response.payload["trace_id"] == "trace-1"
    assert response.payload["count"] == 1
    assert response.payload["events"][0]["kind"] == "turn_start"
    assert response.payload["events"][0]["session_key"] == "agent:main:test"


@pytest.mark.asyncio
async def test_logs_status_is_mounted_on_dispatcher(monkeypatch) -> None:
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG", raising=False)
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    response = await get_dispatcher().dispatch("req-1", "logs.status", {}, ctx)

    assert response.ok is True
    assert isinstance(response.payload, dict)
    assert response.payload["raw_turn_call_log"]["enabled"] is False
