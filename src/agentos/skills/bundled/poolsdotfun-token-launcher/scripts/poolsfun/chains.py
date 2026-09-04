"""Chain constants and environment resolution.

Trimmed from the ``senior-unilp-manager`` skill's ``unilp/chains.py``. Two
deliberate simplifications relative to that module:

* **One chain, no registry.** pools.fun's PartyFactory exists on Robinhood Chain
  and nowhere else, so a lookup table keyed by chain name would be a table with
  one row and a resolver that can only ever fail one way.
* **No RPC env var.** The endpoint is a constant here. A configurable RPC is
  worth its friction when a skill reads thousands of logs across chains; this one
  makes a handful of ``eth_call``s against a single public endpoint, so an env var
  would be one more thing to set up before a launch can happen and one more thing
  to get wrong. ``--rpc`` remains for debugging.

The environment layer keeps the property that matters: the signing key comes from
the process environment only, never from argv.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

# Declared in SKILL.md frontmatter under `requires.env`. Keep the two in sync —
# that block is what puts the skill in "Needs setup" in the AgentOS Skills page.
ENV_SIGNER = "POOLSFUN_PRIVATE_KEY"
# Optional. Only needed to attach a token image; every other path works unset.
ENV_PINATA_JWT = "PINATA_JWT"

# Public Robinhood Chain endpoint. No API key, no rate limit worth planning
# around at this call volume. Verified to support JSON-RPC batching (used to mine
# salts) and eth_call state overrides (used to simulate a dev buy from a wallet
# that has not been funded yet).
RPC_URL = "https://rpc.mainnet.chain.robinhood.com/"

CHAIN_ID = 4663
CHAIN_NAME = "Robinhood Chain"
EXPLORER = "https://robinhoodchain.blockscout.com"

NATIVE = "0x0000000000000000000000000000000000000000"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# pools.fun deployment.
PARTY_FACTORY = "0x626C3d09B65bF5d1D40E0D5F25e19fa49783B3D4"
PARTY_LOCKER = "0x35E41f84d3fD61d4648F0c8B41a1E7d301bCd75E"
# Read off the factory's immutables; pinned here so preflight can name them
# without a round trip, and so selftest can assert they have not moved.
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
NPM = "0x51d0e5188afe12d502e29D982d20C190e7816107"
SUSHI_V3_FACTORY = "0xE51960f1B45f1C9FB6D166E6a884F866fC70433B"

# The only paired assets the factory allowlists. `pools_read.py assets` reads the
# live allowlist rather than trusting this map; it exists to turn a --paired
# shorthand into an address and an address back into a label.
# Kept as a dict, rather than dissolved into module constants, purely so the
# inherited `tx.py` and `rpc.py` helpers — which take a `chain` mapping — can be
# vendored verbatim from senior-unilp-manager and stay diffable against it.
CHAIN: dict = {
    "key": "robinhood",
    "name": CHAIN_NAME,
    "chainId": CHAIN_ID,
    "explorer": EXPLORER,
    "multicall3": MULTICALL3,
    "nativeCurrency": {"name": "Ether", "symbol": "ETH", "decimals": 18},
}

PAIRED_ASSETS: dict[str, str] = {"weth": WETH, "usdg": USDG}
ASSET_LABELS: dict[str, str] = {WETH.lower(): "WETH", USDG.lower(): "USDG"}
ASSET_DECIMALS: dict[str, int] = {WETH.lower(): 18, USDG.lower(): 18}

_env_loaded = False


def load_env() -> None:
    """Load a dotenv file if one is configured, without overriding the environment.

    Anything already in ``os.environ`` wins, so ``POOLSFUN_PRIVATE_KEY=… python3
    pools_write.py`` always beats a file. Under AgentOS this normally finds nothing
    and does nothing: the gateway has already put the declared variables in the
    process environment.
    """
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True

    candidates = [
        os.environ.get("POOLSFUN_ENV"),
        os.environ.get("AGENTOS_HOME") and str(Path(os.environ["AGENTOS_HOME"]) / ".env"),
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


def resolve_rpc_url(override: str | None = None) -> str:
    """The RPC endpoint. Constant unless ``--rpc`` overrides it for debugging."""
    return validate_http_url(override or RPC_URL)


def resolve_paired_asset(value: str | None) -> str:
    """Turn ``--paired`` into a checksummed address. Defaults to WETH.

    WETH is the default because it is what every pools.fun launch to date has
    paired against, and because it is the only asset a native-ETH dev buy works
    with — pairing against USDG silently forces the ERC20 dev-buy path, which
    needs a prior approval.
    """
    from .hexutil import checksum_address

    # `parse_args` yields True for a bare `--paired`; without this the .strip()
    # below dies with a bare AttributeError instead of saying what is wrong.
    if value is True:
        raise ValueError("--paired needs a value (weth, usdg, or an address)")
    if not value:
        return WETH
    key = value.strip().lower()
    if key in PAIRED_ASSETS:
        return PAIRED_ASSETS[key]
    if key.startswith("0x") and len(key) == 42:
        return checksum_address(value)
    known = ", ".join(PAIRED_ASSETS)
    raise ValueError(f'unknown paired asset "{value}". Use one of: {known} (or an address)')


def asset_label(address: str) -> str:
    """A human name for a paired asset, falling back to the short address."""
    return ASSET_LABELS.get(address.lower(), address[:10] + "…")


def asset_decimals(address: str) -> int:
    return ASSET_DECIMALS.get(address.lower(), 18)


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
    # Validate the alphabet here rather than letting int(key, 16) do it downstream.
    # ValueError embeds the offending string in its message, so a key with one
    # mistyped character would be printed in full by the top-level error handler
    # and land in the agent transcript. Never let key material reach an exception.
    if any(character not in "0123456789abcdefABCDEF" for character in key[2:]):
        raise RuntimeError(
            f"{signer_env} is not valid hex (32 bytes, 0-9/a-f). "
            "The value is not shown here on purpose."
        )
    return key


def resolve_signer_address(signer_env: str = ENV_SIGNER) -> str:
    """The wallet address the signing key derives — the answer to "which wallet is mine".

    Imports from ``account`` rather than ``secp256k1`` on purpose. An address is public
    information derived one-way from the key, so answering this question does not need a
    signing path anywhere in reach, and ``pools_read.py`` can therefore call it without
    becoming able to move funds. The distinction is structural: ``account`` has no
    ``sign_digest`` in it.

    The key itself never leaves this function.
    """
    from .account import account_from_private_key

    return account_from_private_key(resolve_private_key(signer_env))["address"]


def pinata_jwt() -> str | None:
    """The Pinata JWT, or None when unset.

    Returning None rather than raising is the whole contract of this function:
    ``PINATA_JWT`` is optional, and a launch with no token image must work on a
    machine where it was never configured. Callers that genuinely need it — only
    the ``--image`` path does — raise their own error with instructions.
    """
    load_env()
    raw = os.environ.get(ENV_PINATA_JWT)
    if not raw:
        return None
    return raw.strip().strip('"').strip("'") or None
