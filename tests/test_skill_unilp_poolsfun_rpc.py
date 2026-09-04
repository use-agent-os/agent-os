"""Unit and regression tests for RpcError handling in unilp and poolsfun."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNILP_DIR = str(
    ROOT / "src" / "agentos" / "skills" / "bundled" / "senior-unilp-manager" / "scripts"
)
POOLSFUN_DIR = str(
    ROOT / "src" / "agentos" / "skills" / "bundled" / "poolsdotfun-token-launcher" / "scripts"
)

if UNILP_DIR not in sys.path:
    sys.path.insert(0, UNILP_DIR)

if POOLSFUN_DIR not in sys.path:
    sys.path.insert(0, POOLSFUN_DIR)

import poolsfun.rpc as poolsfun_rpc  # noqa: E402
import unilp.rpc as unilp_rpc  # noqa: E402


@pytest.mark.parametrize("rpc_mod", [unilp_rpc, poolsfun_rpc])
def test_rpc_error_dict_payload(rpc_mod: Any) -> None:
    err = rpc_mod.RpcError(
        "eth_call",
        {"code": -32000, "message": "execution reverted", "data": "0x1234"},
    )
    assert str(err) == "eth_call: execution reverted"
    assert err.code == -32000
    assert err.data == "0x1234"
    assert err.raw == {"code": -32000, "message": "execution reverted", "data": "0x1234"}


@pytest.mark.parametrize("rpc_mod", [unilp_rpc, poolsfun_rpc])
def test_rpc_error_string_payload(rpc_mod: Any) -> None:
    err = rpc_mod.RpcError("eth_call", "rate limit exceeded")
    assert str(err) == "eth_call: rate limit exceeded"
    assert err.code is None
    assert err.data is None
    assert err.raw == "rate limit exceeded"


@pytest.mark.parametrize("rpc_mod", [unilp_rpc, poolsfun_rpc])
def test_rpc_error_non_dict_payloads(rpc_mod: Any) -> None:
    for payload in [None, 42, ["unexpected", "list"]]:
        err = rpc_mod.RpcError("eth_call", payload)
        assert str(err) == f"eth_call: {payload}"
        assert err.code is None
        assert err.data is None
        assert err.raw == payload


@pytest.mark.parametrize("rpc_mod", [unilp_rpc, poolsfun_rpc])
def test_rpc_client_request_raises_rpc_error_on_string_error(
    monkeypatch: pytest.MonkeyPatch, rpc_mod: Any
) -> None:
    client = rpc_mod.RpcClient(
        chain={"name": "test", "chainId": 1, "rpcEnv": ["TEST_RPC"]},
        rpc_url="http://127.0.0.1:8545",
    )
    monkeypatch.setattr(
        client,
        "_post",
        lambda *args, **kwargs: {"jsonrpc": "2.0", "id": 1, "error": "rate limit exceeded"},
    )
    with pytest.raises(rpc_mod.RpcError) as excinfo:
        client.request("eth_blockNumber")
    assert "rate limit exceeded" in str(excinfo.value)
    assert excinfo.value.raw == "rate limit exceeded"
