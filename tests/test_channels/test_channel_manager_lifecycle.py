"""ChannelManager lifecycle diagnostics."""

from __future__ import annotations

import pytest

from agentos.channels.manager import ChannelManager


class _FailingChannel:
    async def start(self) -> None:
        raise RuntimeError("Feishu adapter dependency missing — reinstall AgentOS")


class _SlowChannel:
    startup_timeout_s = 0.001
    stopped = False

    async def start(self) -> None:
        await __import__("asyncio").sleep(0.05)

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_start_all_retains_start_exception_details():
    manager = ChannelManager({"feishu": _FailingChannel()}, None, None)

    results = await manager.start_all()

    assert results == {"feishu": False}
    assert manager.start_errors()["feishu"] == {
        "error_type": "RuntimeError",
        "error": "Feishu adapter dependency missing — reinstall AgentOS",
        "exception": (
            "RuntimeError('Feishu adapter dependency missing — reinstall AgentOS')"
        ),
    }


@pytest.mark.asyncio
async def test_start_all_honors_adapter_startup_timeout():
    channel = _SlowChannel()
    manager = ChannelManager({"feishu": channel}, None, None)

    results = await manager.start_all()

    assert results == {"feishu": False}
    assert manager.start_errors()["feishu"]["error_type"] == "TimeoutError"
    assert channel.stopped is True
