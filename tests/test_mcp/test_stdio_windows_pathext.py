from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agentos.mcp.stdio import MCPStdioClient
from agentos.mcp.types import MCPServerConfig


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = AsyncMock()
        self.stdout = AsyncMock()
        self.returncode: int | None = None

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_connect_resolves_command_via_shutil_which() -> None:
    config = MCPServerConfig(name="test", transport="stdio", command="npx", args=["-y", "test-mcp"])
    client = MCPStdioClient(config)

    fake_proc = _FakeProcess()
    spawn_args: list[Any] = []

    async def _mock_create_subprocess_exec(cmd: str, *args: Any, **kwargs: Any) -> _FakeProcess:
        spawn_args.append((cmd, args, kwargs))
        return fake_proc

    with (
        patch("shutil.which", return_value="C:\\nodejs\\npx.cmd") as mock_which,
        patch("asyncio.create_subprocess_exec", side_effect=_mock_create_subprocess_exec),
        patch.object(client, "_send_request", new_callable=AsyncMock) as mock_send,
        patch.object(client, "_send_notification", new_callable=AsyncMock) as mock_notify,
    ):
        await client.connect()

        mock_which.assert_called_once_with("npx", path=None)
        assert spawn_args[0][0] == "C:\\nodejs\\npx.cmd"
        assert spawn_args[0][1] == ("-y", "test-mcp")
        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == "initialize"
        mock_notify.assert_called_once_with("notifications/initialized")


@pytest.mark.asyncio
async def test_connect_raises_when_which_returns_none_naming_command() -> None:
    config = MCPServerConfig(name="test", transport="stdio", command="missing-mcp-cli", args=[])
    client = MCPStdioClient(config)

    with patch("shutil.which", return_value=None) as mock_which:
        with pytest.raises(FileNotFoundError) as exc_info:
            await client.connect()

        mock_which.assert_called_once_with("missing-mcp-cli", path=None)
        error_msg = str(exc_info.value)
        assert "missing-mcp-cli" in error_msg
        assert "MCP server command not found" in error_msg


@pytest.mark.asyncio
async def test_connect_passes_custom_path_from_env_to_which() -> None:
    custom_path = "D:\\custom\\tools\\bin"
    config = MCPServerConfig(
        name="test",
        transport="stdio",
        command="uvx",
        args=["mcp-server"],
        env={"PATH": custom_path},
    )
    client = MCPStdioClient(config)
    fake_proc = _FakeProcess()

    with (
        patch("shutil.which", return_value="D:\\custom\\tools\\bin\\uvx.exe") as mock_which,
        patch("asyncio.create_subprocess_exec", return_value=fake_proc),
        patch.object(client, "_send_request", new_callable=AsyncMock),
        patch.object(client, "_send_notification", new_callable=AsyncMock),
    ):
        await client.connect()

        mock_which.assert_called_once_with("uvx", path=custom_path)


@pytest.mark.asyncio
async def test_connect_handles_mixed_case_path_in_env() -> None:
    custom_path = "D:\\custom\\tools\\bin"
    config = MCPServerConfig(
        name="test",
        transport="stdio",
        command="pnpm",
        args=["run", "mcp"],
        env={"Path": custom_path},
    )
    client = MCPStdioClient(config)
    fake_proc = _FakeProcess()

    with (
        patch("shutil.which", return_value="D:\\custom\\tools\\bin\\pnpm.cmd") as mock_which,
        patch("asyncio.create_subprocess_exec", return_value=fake_proc),
        patch.object(client, "_send_request", new_callable=AsyncMock),
        patch.object(client, "_send_notification", new_callable=AsyncMock),
    ):
        await client.connect()

        mock_which.assert_called_once_with("pnpm", path=custom_path)
