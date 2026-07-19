"""Skew policy + passive notice wired into the shared run_gateway_call seam."""

from __future__ import annotations

from typing import Any

import pytest
import typer

import agentos
from agentos.cli import gateway_rpc


class _FakeClient:
    def __init__(self, server_version: str | None) -> None:
        self.server_version = server_version
        self.closed = False

    async def connect(self, url: str, token: str | None = None) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTOS_ALLOW_VERSION_SKEW", raising=False)
    # Never touch the network in this test module.
    monkeypatch.setattr("agentos.cli.pypi_client.latest_version", lambda timeout=2.0: None)


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, version: str | None) -> _FakeClient:
    client = _FakeClient(version)
    from agentos.cli import gateway_client as gcm

    monkeypatch.setattr(gcm, "GatewayClient", lambda: client)
    return client


async def _noop(_client: Any) -> str:
    return "ok"


@pytest.mark.asyncio
async def test_gateway_older_warns_but_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    older = "0.0.1"  # guaranteed older than the installed CLI
    assert agentos.__version__ != older
    _install_fake_client(monkeypatch, older)
    result = await gateway_rpc.run_gateway_call(_noop)
    assert result == "ok"
    assert "OLDER" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_gateway_newer_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    newer = "99999.1.1"
    _install_fake_client(monkeypatch, newer)
    with pytest.raises(typer.Exit) as exc:
        await gateway_rpc.run_gateway_call(_noop)
    assert exc.value.exit_code == 3


@pytest.mark.asyncio
async def test_gateway_newer_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_ALLOW_VERSION_SKEW", "1")
    _install_fake_client(monkeypatch, "99999.1.1")
    assert await gateway_rpc.run_gateway_call(_noop) == "ok"


@pytest.mark.asyncio
async def test_no_version_no_skew(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, None)
    assert await gateway_rpc.run_gateway_call(_noop) == "ok"
