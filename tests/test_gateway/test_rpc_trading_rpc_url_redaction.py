"""WAL-24: a keyed RPC URL never reaches a ``wallet.*`` / ``trading.*`` caller in an error.

Provider keys live in the URL path (Alchemy, Infura) or query (dRPC). The LP
engine hands the configured URL to the bundled skill library, whose URL guard
used to quote it in full; the error went back verbatim to any caller of the LP
reads, an agent-bound connection included.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos.trading import lp
from tests.test_gateway.test_rpc_trading import _agent_ctx, call, ctx, stack  # noqa: F401

RPC_KEY = "DUMMYALCHEMYKEY0123456789abcdef"
WSS = f"wss://base-mainnet.g.alchemy.com/v2/{RPC_KEY}"
NO_HOST = f"https:///v2/{RPC_KEY}"
DRPC_WSS = f"wss://lb.drpc.org/ogws?network=base&dkey={RPC_KEY}"

LP_READS = (
    ("trading.lp.position", {"chainId": 8453, "tokenId": "1"}),
    ("trading.lp.pool", {"chainId": 8453, "target": "ETH/USDC"}),
    ("trading.lp.ranges", {"chainId": 8453, "target": "ETH/USDC"}),
    ("trading.lp.positions", {"chainIds": [8453], "wallets": ["0x" + "11" * 20]}),
)


@pytest.mark.parametrize("url", [WSS, NO_HOST, DRPC_WSS])
@pytest.mark.parametrize(("method", "params"), LP_READS)
async def test_lp_read_error_names_the_host_only(
    stack: dict[str, Any],  # noqa: F811
    url: str,
    method: str,
    params: dict[str, Any],
) -> None:
    stack["config"].trading.rpc_urls = {"8453": url}
    res = await call(method, params, _agent_ctx(stack))
    text = repr(res.error) if not res.ok else repr(res.payload)
    assert RPC_KEY not in text, text


async def test_env_fallback_url_is_scrubbed_too(
    stack: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RPC_BASE_URL", WSS)
    stack["config"].trading.rpc_urls = {}
    res = await call("trading.lp.position", {"chainId": 8453, "tokenId": "1"}, _agent_ctx(stack))
    assert res.ok is False
    assert RPC_KEY not in res.error.message
    assert "base-mainnet.g.alchemy.com" in res.error.message


async def test_any_handler_error_quoting_the_url_is_scrubbed(
    stack: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scrub sits on every trading handler, not on one library call."""
    from agentos.trading.service import TradingError

    stack["config"].trading.rpc_urls = {"8453": f"https://base.example/v2/{RPC_KEY}"}

    async def leaky(service: Any, **kwargs: Any) -> dict[str, Any]:
        raise TradingError(
            "trading.rpc",
            f"upstream https://base.example/v2/{RPC_KEY} said no (path /v2/{RPC_KEY})",
        )

    monkeypatch.setattr(lp, "lp_pool", leaky)
    res = await call("trading.lp.pool", {"chainId": 8453, "target": "ETH/USDC"}, _agent_ctx(stack))
    assert res.ok is False and res.error.code == "trading.rpc"
    assert RPC_KEY not in res.error.message
    assert "https://base.example/…" in res.error.message


def test_scrub_leaves_unrelated_text_alone() -> None:
    from agentos.trading.chains import configured_rpc_urls, scrub_rpc_urls

    urls = configured_rpc_urls({"8453": f"https://base.example/v2/{RPC_KEY}"})
    assert scrub_rpc_urls("swap failed: slippage", urls) == "swap failed: slippage"
    assert scrub_rpc_urls("", urls) == ""


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SKILLS = Path(__file__).resolve().parents[2] / "src" / "agentos" / "skills" / "bundled"


@pytest.mark.parametrize(
    ("relative", "function"),
    [
        ("senior-unilp-manager/scripts/unilp/chains.py", "require_http_url"),
        ("poolsdotfun-token-launcher/scripts/poolsfun/chains.py", "require_http_url"),
    ],
)
@pytest.mark.parametrize("url", [WSS, NO_HOST, f"file:///v2/{RPC_KEY}"])
def test_skill_url_guards_do_not_echo_the_url(relative: str, function: str, url: str) -> None:
    module = _load(SKILLS / relative, f"_wal24_{relative.replace('/', '_')[:-3]}")
    with pytest.raises(ValueError) as caught:
        getattr(module, function)(url, "--rpc")
    assert RPC_KEY not in str(caught.value)
    assert "must be http" in str(caught.value)


def test_chain_stocks_and_rwa_guards_do_not_echo_the_url() -> None:
    stocks = _load(SKILLS / "robinhood-chain-stocks/scripts/chain_stocks.py", "_wal24_chain_stocks")
    for url in (WSS, NO_HOST):
        with pytest.raises(ValueError) as caught:
            stocks._validate_http_url(url)
        assert RPC_KEY not in str(caught.value)
    rwa_path = SKILLS / "robinhood-rwa-addresses/scripts/rwa_lookup.py"
    rwa = _load(rwa_path, "_wal24_rwa_lookup")
    with pytest.raises(ValueError) as caught:
        rwa.validate_rpc_url(WSS)
    assert RPC_KEY not in str(caught.value)
