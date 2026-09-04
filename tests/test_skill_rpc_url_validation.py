"""Regression tests for RPC URL scheme validation in poolsdotfun and senior-unilp-manager."""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

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

from poolsfun.chains import (  # noqa: E402
    resolve_rpc_url as pools_resolve,
)
from poolsfun.chains import (
    validate_http_url as pools_validate,
)
from poolsfun.rpc import RpcClient as PoolsRpcClient  # noqa: E402
from unilp.chains import (  # noqa: E402
    CHAINS,
)
from unilp.chains import (
    resolve_rpc_url as unilp_resolve,
)
from unilp.chains import (
    validate_http_url as unilp_validate,
)
from unilp.rpc import RpcClient as UnilpRpcClient  # noqa: E402


@pytest.mark.parametrize("validator", [pools_validate, unilp_validate])
def test_validate_http_url_rejects_non_http_schemes(validator) -> None:
    """file://, ftp://, gopher://, javascript: are all rejected."""
    for invalid in [
        "file:///etc/passwd",
        "file:///c:/windows/system32/drivers/etc/hosts",
        "ftp://rpc.example.com",
        "gopher://example.com",
        "javascript:alert(1)",
    ]:
        with pytest.raises(ValueError, match="must be http:// or https://"):
            validator(invalid)
    for empty in ["", "   "]:
        with pytest.raises(ValueError, match="empty URL"):
            validator(empty)


@pytest.mark.parametrize("validator", [pools_validate, unilp_validate])
def test_validate_http_url_rejects_missing_host(validator) -> None:
    with pytest.raises(ValueError, match="missing host"):
        validator("http://")
    with pytest.raises(ValueError, match="missing host"):
        validator("https://")


@pytest.mark.parametrize("validator", [pools_validate, unilp_validate])
def test_validate_http_url_accepts_valid_urls(validator) -> None:
    assert validator("http://example.com") == "http://example.com"
    assert validator("http://127.0.0.1:8545") == "http://127.0.0.1:8545"
    assert validator("http://[::1]:8545") == "http://[::1]:8545"
    assert (
        validator("https://rpc.mainnet.chain.robinhood.com")
        == "https://rpc.mainnet.chain.robinhood.com"
    )
    assert validator("https://user:pass@host.com:8545") == "https://user:pass@host.com:8545"


def test_poolsfun_resolve_rpc_url_override_validation() -> None:
    with pytest.raises(ValueError, match="must be http:// or https://"):
        pools_resolve("file:///etc/passwd")
    assert pools_resolve("http://localhost:8545") == "http://localhost:8545"
    # Default without override returns validated default RPC_URL
    assert pools_resolve() == "https://rpc.mainnet.chain.robinhood.com/"


def test_unilp_resolve_rpc_url_override_validation() -> None:
    chain = CHAINS["base"]
    with pytest.raises(ValueError, match="must be http:// or https://"):
        unilp_resolve(chain, "file:///etc/passwd")
    assert unilp_resolve(chain, "http://localhost:8545") == "http://localhost:8545"


def test_poolsfun_rpc_client_rejects_file_scheme() -> None:
    with pytest.raises(ValueError, match="must be http:// or https://"):
        PoolsRpcClient(rpc_url="file:///etc/shadow")


def test_unilp_rpc_client_rejects_file_scheme() -> None:
    with pytest.raises(ValueError, match="must be http:// or https://"):
        UnilpRpcClient(chain=CHAINS["base"], rpc_url="file:///etc/shadow")
