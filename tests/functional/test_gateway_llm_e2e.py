"""Opt-in gateway-level live LLM e2e.

Runs a real gateway, creates a session over the public WebSocket client, sends
one prompt through the normal session path, and verifies the provider response.
It skips unless explicitly enabled and credentialed.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from agentos.cli.gateway_client import GatewayClient

pytestmark = [pytest.mark.llm, pytest.mark.llm_smoke, pytest.mark.llm_gateway]

_EXPECTED_TOKEN = "agentos-gateway-live-ok"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(port: int, server: subprocess.Popen[str]) -> None:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 45.0
    last_error = ""
    while time.monotonic() < deadline:
        if server.poll() is not None:
            stdout = server.stdout.read() if server.stdout else ""
            stderr = server.stderr.read() if server.stderr else ""
            raise AssertionError(
                f"gateway exited early code={server.returncode}\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200 and response.json().get("ok") is True:
                return
        except Exception as exc:  # noqa: BLE001 - surfaced on timeout.
            last_error = str(exc)
        time.sleep(0.2)
    raise AssertionError(f"gateway did not become healthy: {last_error}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value))


def _write_gateway_config(
    path: Path,
    *,
    port: int,
    state_dir: Path,
    workspace_dir: Path,
) -> None:
    path.write_text(
        textwrap.dedent(
            f"""
            host = "127.0.0.1"
            port = {port}
            state_dir = {_toml_string(state_dir)}
            workspace_dir = {_toml_string(workspace_dir)}

            [auth]
            mode = "none"
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _write_gateway_server_script(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            import asyncio
            import os

            from agentos.gateway.boot import start_gateway_server
            from agentos.gateway.config import GatewayConfig, LlmProviderConfig
            from agentos.gateway.websocket import SubscriptionManager

            config = GatewayConfig.load(os.environ["AGENTOS_GATEWAY_CONFIG_PATH"])
            config.llm = LlmProviderConfig(
                provider="openrouter",
                model=os.environ.get("LLM_TEST_MODEL", "deepseek/deepseek-v4-flash"),
                api_key=os.environ["OPENROUTER_API_KEY"],
                base_url=os.environ.get(
                    "OPENROUTER_BASE_URL",
                    "https://openrouter.ai/api/v1",
                ),
            )

            async def main():
                await start_gateway_server(
                    config=config,
                    subscription_manager=SubscriptionManager(),
                    run=True,
                )
                await asyncio.Event().wait()

            asyncio.run(main())
            """
        ),
        encoding="utf-8",
    )


async def _send_live_prompt(port: int) -> list[dict]:
    client = GatewayClient()
    await client.connect(f"ws://127.0.0.1:{port}/ws")
    try:
        session_key = await client.create_session(display_name="live-gateway-llm")
        return [
            event
            async for event in client.send_message(
                session_key,
                f"Reply with exactly {_EXPECTED_TOKEN}.",
            )
        ]
    finally:
        await client.close()


def _event_text(events: list[dict]) -> str:
    chunks: list[str] = []
    for event in events:
        for key in ("text", "delta", "message", "content"):
            value = event.get(key)
            if isinstance(value, str):
                chunks.append(value)
    return "\n".join(chunks).lower()


def test_gateway_llm_e2e_uses_explicit_temp_gateway_config(tmp_path: Path) -> None:
    port = 18891
    config_path = tmp_path / "gateway.toml"
    server_script = tmp_path / "gateway_llm_server.py"
    state_dir = tmp_path / "state"
    workspace_dir = tmp_path / "workspace"

    _write_gateway_config(
        config_path,
        port=port,
        state_dir=state_dir,
        workspace_dir=workspace_dir,
    )
    _write_gateway_server_script(server_script)

    config_source = config_path.read_text(encoding="utf-8")
    script_source = server_script.read_text(encoding="utf-8")

    assert f"state_dir = {_toml_string(state_dir)}" in config_source
    assert f"workspace_dir = {_toml_string(workspace_dir)}" in config_source
    assert 'GatewayConfig.load(os.environ["AGENTOS_GATEWAY_CONFIG_PATH"])' in script_source


@pytest.mark.asyncio
async def test_gateway_session_send_reaches_live_llm(tmp_path: Path) -> None:
    if os.environ.get("AGENTOS_GATEWAY_LLM_E2E") != "1":
        pytest.skip("set AGENTOS_GATEWAY_LLM_E2E=1 to run gateway LLM e2e")
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")

    port = _free_port()
    config_path = tmp_path / "gateway.toml"
    server_script = tmp_path / "gateway_llm_server.py"
    state_dir = tmp_path / "state"
    workspace_dir = tmp_path / "workspace"
    _write_gateway_config(
        config_path,
        port=port,
        state_dir=state_dir,
        workspace_dir=workspace_dir,
    )
    _write_gateway_server_script(server_script)
    env = os.environ.copy()
    env["AGENTOS_GATEWAY_CONFIG_PATH"] = str(config_path)
    env["AGENTOS_STATE_DIR"] = str(state_dir)
    env["AGENTOS_LOG_DIR"] = str(tmp_path / "logs")
    env["AGENTOS_TURN_CALL_LOG"] = "0"
    server = subprocess.Popen(
        [sys.executable, str(server_script)],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _wait_for_health(port, server)
        events = await asyncio.wait_for(_send_live_prompt(port), timeout=120)
    finally:
        _stop_process(server)

    assert _EXPECTED_TOKEN in _event_text(events)
