"""Chain registry and environment resolution.

Port of ``chains.mjs``, with the environment layer redesigned: the Node original
climbed four directories to a repo root and loaded ``projects/capu/.env``, which is
meaningless for a skill installed from a wheel. Here the process environment is the
primary source — AgentOS injects skill-declared variables into subprocesses — with an
optional dotenv fallback for running the scripts by hand.

The registry exists so another Uniswap v4 deployment can be added without touching
any other module.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

# Env var names this skill declares in its SKILL.md frontmatter. Keep the two in
# sync: `requires.env` there is what puts the skill in "Needs setup".
ENV_RPC_BASE = "RPC_BASE_URL"
ENV_RPC_ROBINHOOD = "RPC_ROBINHOOD_URL"
ENV_SIGNER = "UNIV4_LP_PRIVATE_KEY"

NATIVE = "0x0000000000000000000000000000000000000000"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

_env_loaded = False


def load_env() -> None:
    """Load a dotenv file if one is configured, without overriding the environment.

    Anything already in ``os.environ`` wins, so ``RPC_BASE_URL=… python3 lp_read.py``
    always beats a file. Under AgentOS this normally finds nothing and does nothing:
    the gateway has already put the declared variables in the process environment.
    """
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True

    candidates = [
        os.environ.get("UNILP_ENV"),
        os.environ.get("AGENTOS_HOME") and
        str(Path(os.environ["AGENTOS_HOME"]) / ".env"),
        str(Path.home() / ".agentos" / ".env"),
        ".env",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            if name.startswith("export "):
                name = name[7:].strip()
            if name in os.environ:
                continue  # never override an explicit value
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[name] = value
        return


CHAINS: dict[str, dict] = {
    "robinhood": {
        "key": "robinhood",
        "name": "Robinhood Chain",
        "chainId": 4663,
        "rpcEnv": [ENV_RPC_ROBINHOOD],
        "nativeCurrency": {"name": "Ether", "symbol": "ETH", "decimals": 18},
        "explorer": "https://robinhoodchain.blockscout.com",
        "poolManager": "0x8366a39CC670B4001A1121B8F6A443A643e40951",
        "positionManager": "0x58daec3116aae6D93017bAAea7749052E8a04fA7",
        "stateView": "0xF3334192D15450CdD385c8B70e03f9A6bD9E673b",
        "quoter": "0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94",
        "universalRouter": "0x8876789976dEcBfCbBbe364623C63652db8C0904",
        "permit2": PERMIT2,
        "multicall3": MULTICALL3,
        # NOT the canonical 0x1F98431c…F984 — that address holds unrelated bytecode here.
        "v3Factory": "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA",
        "v3FeeTiers": [100, 500, 3000, 10000],
        "wrappedNative": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "knownQuotes": {
            "0x0bd7d308f8e1639fab988df18a8011f41eacad73": "WETH",
            "0x5fc5360d0400a0fd4f2af552add042d716f1d168": "USDG",
            NATIVE: "ETH",
        },
        "geckoNetwork": "robinhood",
        "logScan": {"supportsFullRange": True, "chunkBlocks": 500_000, "fromBlock": 0},
        # Logs are cheap here and carry per-owner attribution, so prefer them.
        "rangeMode": "logs",
    },
    "base": {
        "key": "base",
        "name": "Base",
        "chainId": 8453,
        "rpcEnv": [ENV_RPC_BASE, "RPC_URL"],
        "nativeCurrency": {"name": "Ether", "symbol": "ETH", "decimals": 18},
        "explorer": "https://basescan.org",
        "poolManager": "0x498581fF718922c3f8e6A244956aF099B2652b2b",
        "positionManager": "0x7C5f5A4bBd8fD63184577525326123B519429bDc",
        "stateView": "0xA3c0c9b65baD0b08107Aa264b0f3dB444b867A71",
        "quoter": "0x0d5e0F971ED27FBfF6c2837bf31316121532048D",
        "universalRouter": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
        "permit2": PERMIT2,
        "multicall3": MULTICALL3,
        "v3Factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        "v3FeeTiers": [100, 500, 3000, 10000],
        "wrappedNative": "0x4200000000000000000000000000000000000006",
        "knownQuotes": {
            "0x4200000000000000000000000000000000000006": "WETH",
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
            NATIVE: "ETH",
        },
        "geckoNetwork": "base",
        # Public Base RPCs cap eth_getLogs ranges hard; always chunk.
        "logScan": {"supportsFullRange": False, "chunkBlocks": 9_000, "fromBlock": 25_350_000},
        # ~24M blocks at 9k a chunk is thousands of sequential requests, so a full
        # ModifyLiquidity scan is not viable. Read reserves from the tick bitmap
        # instead: it loses per-owner attribution but keeps exact totals.
        "rangeMode": "ticks",
    },
}

DEFAULT_CHAIN = "robinhood"


def resolve_chain(key_or_id: str | int | None = None) -> dict:
    wanted = str(key_or_id if key_or_id is not None else DEFAULT_CHAIN).lower()
    if wanted in CHAINS:
        return CHAINS[wanted]
    for chain in CHAINS.values():
        if str(chain["chainId"]) == wanted:
            return chain
    known = ", ".join(CHAINS)
    raise ValueError(f'unknown chain "{key_or_id}". Known: {known} (or their chain ids)')


def validate_http_url(url: str) -> str:
    """Validate and return a URL that must be http:// or https://.

    Rejects ``file://``, ``ftp://``, and any custom scheme that could leak
    local data or be abused as an SSRF oracle. Uses ``urlsplit`` for robust
    scheme detection (not a fragile ``startswith`` prefix check).
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError(f"empty URL: {url!r}")
    try:
        parsed = urllib.parse.urlsplit(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid URL {url!r}: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            f"invalid URL scheme {parsed.scheme!r} in {url!r}: must be http:// or https://"
        )
    if not parsed.netloc:
        raise ValueError(f"URL missing host {url!r}: must be http:// or https://")
    return cleaned


def resolve_rpc_url(chain: dict, override: str | None = None) -> str:
    if override:
        return validate_http_url(override)
    load_env()
    for name in chain["rpcEnv"]:
        value = os.environ.get(name)
        if value:
            return validate_http_url(value)
    names = " or ".join(chain["rpcEnv"])
    raise RuntimeError(
        f"no RPC url for {chain['name']}. Set {names} in the agent environment "
        f"(AgentOS: Skills page -> Set {chain['rpcEnv'][0]}), or pass --rpc <url>"
    )


def resolve_private_key(signer_env: str = ENV_SIGNER) -> str:
    """Read and normalise the signing key.

    Deliberately reads only from the environment and never from argv: a key on a
    command line lands in shell history and in the agent transcript, and AgentOS's
    redaction only masks the ``NAME=value`` shape, not a bare hex string.

    Only the derived address is ever printed; the key itself is never logged.
    """
    load_env()
    raw = os.environ.get(signer_env)
    if not raw:
        raise RuntimeError(
            f"env var {signer_env} is not set. Set it in the agent environment "
            f"(AgentOS: Skills page -> Set {signer_env}), or pass --signer-env <VAR>"
        )
    key = raw.strip().strip('"').strip("'")
    if not key.startswith("0x"):
        key = "0x" + key
    if len(key) != 66:
        raise RuntimeError(f"{signer_env} is not a 32-byte hex private key")
    return key


def resolve_signer_address(signer_env: str = ENV_SIGNER) -> str:
    """The wallet address the signing key derives — the answer to "which wallet is mine".

    Imports from ``account`` rather than ``secp256k1`` on purpose. An address is public
    information derived one-way from the key, so answering this question does not need a
    signing path anywhere in reach, and ``lp_read.py`` can therefore call it without
    becoming able to move funds. The distinction is structural: ``account`` has no
    ``sign_digest`` in it.

    The key itself never leaves this function.
    """
    from .account import account_from_private_key

    return account_from_private_key(resolve_private_key(signer_env))["address"]
