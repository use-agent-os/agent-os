"""Regression tests for non-dict and string RPC error handling in bundled skills.

Verifies that RpcError in poolsfun and senior-unilp-manager, as well as _rpc_batch
in robinhood-rwa-addresses, safely handle string errors and non-dict payloads without
crashing with AttributeError.
"""

from __future__ import annotations

# ruff: noqa: E402
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_POOLS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src/agentos/skills/bundled/poolsdotfun-token-launcher/scripts"
)
_UNILP_DIR = (
    Path(__file__).resolve().parents[1] / "src/agentos/skills/bundled/senior-unilp-manager/scripts"
)

if str(_POOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_POOLS_DIR))
if str(_UNILP_DIR) not in sys.path:
    sys.path.insert(0, str(_UNILP_DIR))

from poolsfun.rpc import RpcClient as PoolsRpcClient
from poolsfun.rpc import RpcError as PoolsRpcError
from unilp.chains import CHAINS
from unilp.rpc import RpcClient as UnilpRpcClient
from unilp.rpc import RpcError as UnilpRpcError

_RWA_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/agentos/skills/bundled/robinhood-rwa-addresses/scripts/rwa_lookup.py"
)
_spec = importlib.util.spec_from_file_location("rwa_lookup", _RWA_SCRIPT)
assert _spec is not None and _spec.loader is not None
rwa_lookup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwa_lookup)


@pytest.mark.parametrize("error_cls", [PoolsRpcError, UnilpRpcError])
def test_rpc_error_handles_string_payload(error_cls) -> None:
    """A string error payload must not crash with AttributeError and must format cleanly."""
    exc = error_cls("eth_call", "rate limit exceeded")
    assert str(exc) == "eth_call: rate limit exceeded"
    assert exc.code is None
    assert exc.data is None
    assert exc.raw == "rate limit exceeded"


@pytest.mark.parametrize("error_cls", [PoolsRpcError, UnilpRpcError])
def test_rpc_error_handles_dict_payload(error_cls) -> None:
    """Dict error with message, code, and data behaves as expected."""
    payload = {"message": "execution reverted", "code": -32000, "data": "0xfe55"}
    exc = error_cls("eth_call", payload)
    assert str(exc) == "eth_call: execution reverted"
    assert exc.code == -32000
    assert exc.data == "0xfe55"
    assert exc.raw == payload


@pytest.mark.parametrize("error_cls", [PoolsRpcError, UnilpRpcError])
def test_rpc_error_handles_dict_without_message(error_cls) -> None:
    """Dict error without a 'message' key falls back to str(dict)."""
    payload = {"code": -32600}
    exc = error_cls("eth_call", payload)
    assert "eth_call: " in str(exc)
    assert "-32600" in str(exc)
    assert exc.code == -32600
    assert exc.data is None


@pytest.mark.parametrize("error_cls", [PoolsRpcError, UnilpRpcError])
def test_rpc_error_handles_primitive_payload(error_cls) -> None:
    """Integer or other non-dict primitives are converted to str without crashing."""
    exc = error_cls("eth_call", 502)
    assert str(exc) == "eth_call: 502"
    assert exc.code is None
    assert exc.data is None
    assert exc.raw == 502


def test_poolsfun_rpc_client_request_handles_string_error() -> None:
    client = PoolsRpcClient(rpc_url="http://localhost:8545")
    with patch.object(
        client,
        "_post",
        return_value={"jsonrpc": "2.0", "id": 1, "error": "rate limit exceeded"},
    ):
        with pytest.raises(PoolsRpcError) as exc_info:
            client.request("eth_call", [])
        assert "eth_call: rate limit exceeded" in str(exc_info.value)
        assert exc_info.value.code is None


def test_poolsfun_rpc_client_request_handles_dict_error() -> None:
    client = PoolsRpcClient(rpc_url="http://localhost:8545")
    with patch.object(
        client,
        "_post",
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"message": "execution reverted", "code": -32000},
        },
    ):
        with pytest.raises(PoolsRpcError) as exc_info:
            client.request("eth_call", [])
        assert "eth_call: execution reverted" in str(exc_info.value)
        assert exc_info.value.code == -32000


def test_unilp_rpc_client_request_handles_string_error() -> None:
    client = UnilpRpcClient(chain=CHAINS["base"], rpc_url="http://localhost:8545")
    with patch.object(
        client,
        "_post",
        return_value={"jsonrpc": "2.0", "id": 1, "error": "proxy connection refused"},
    ):
        with pytest.raises(UnilpRpcError) as exc_info:
            client.request("eth_call", [])
        assert "eth_call: proxy connection refused" in str(exc_info.value)


def test_unilp_rpc_client_request_handles_dict_error() -> None:
    client = UnilpRpcClient(chain=CHAINS["base"], rpc_url="http://localhost:8545")
    with patch.object(
        client,
        "_post",
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"message": "out of gas", "code": -32000},
        },
    ):
        with pytest.raises(UnilpRpcError) as exc_info:
            client.request("eth_call", [])
        assert "eth_call: out of gas" in str(exc_info.value)
        assert exc_info.value.code == -32000


def test_rwa_rpc_batch_handles_nondict_error() -> None:
    """rwa_lookup._rpc_batch surfaces string errors returned from provider."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"error": "batch requests disabled"}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(rwa_lookup.RpcError, match="batch requests disabled"):
            rwa_lookup._rpc_batch("http://localhost:8545", [{"id": 1}], timeout=5.0)


def test_rwa_rpc_batch_handles_dict_error() -> None:
    """rwa_lookup._rpc_batch extracts message from dict errors."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {"error": {"message": "method not allowed", "code": -32601}}
    ).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(rwa_lookup.RpcError, match="method not allowed"):
            rwa_lookup._rpc_batch("http://localhost:8545", [{"id": 1}], timeout=5.0)
