---
name: robinhood-chain-stocks
description: "Read live on-chain state for Robinhood Chain tokenized stocks (Stock Tokens). Use when: the user asks for the on-chain price of a tokenized stock or ETF, a wallet's holding/value of one, total supply, whether a token is a genuine Robinhood Stock Token, the corporate-action multiplier, or whether the price oracle is paused (e.g. 'AAPL price on Robinhood Chain', 'giá TSLA on-chain', 'is 0x… a real Robinhood stock token', 'how much NVDA does 0x… hold'). Read-only: never signs or sends a transaction. NOT for: placing trades (use robinhood-agentic-trading) or plain address lookup (use robinhood-rwa-addresses). No API key needed."
homepage: https://docs.robinhood.com/chain/
provenance:
  origin: agentos-original
  license: MIT
  maintained_by: AgentOS
publisher:
  id: robinhood
metadata:
  {
    "agentos":
      {
        "emoji": "📈",
        "homepage": "https://docs.robinhood.com/chain/",
        "requires": { "anyBins": ["python3", "python"] },
      },
  }
entrypoint:
  command: "{python} {baseDir}/scripts/chain_stocks.py"
  args:
    - --query
    - "{{ with.query | default(inputs.user_message) }}"
    - --timeout
    - "{{ with.timeout | default(15) }}"
  parse: json
  timeout: 40
---

# Robinhood Chain Stock Tokens (on-chain)

Read tokenized-stock state **directly from Robinhood Chain** (chainId `4663`)
over plain JSON-RPC: Chainlink USD price, `uiMultiplier()`, oracle pause state,
total supply, and wallet balances.

This skill is **read-only**. It issues only `eth_call`, never signs, sends, or
holds a private key. Trade execution belongs to `robinhood-agentic-trading`.

## What a Stock Token is

Each Stock Token is a standard **ERC-20 with 18 decimals**, one per underlying
equity or ETF, deployed behind an OpenZeppelin beacon proxy (all tokens share
one upgradeable implementation). Two non-ERC-20 functions matter:

| Function | Selector | Meaning |
|---|---|---|
| `uiMultiplier()` | `0xa60bf13d` | ERC-8056 scaled-UI ratio. Dividends and splits move this multiplier instead of rebasing balances, so a raw balance stays constant until redemption. |
| `oraclePaused()` | `0x7706ba52` | `true` while a corporate action is processing — the Chainlink feed stops publishing fresh prices. **Treat any price as stale when this is true.** |

Prices come from a per-asset **Chainlink `AggregatorV3Interface`** feed (8
decimals) and already incorporate the multiplier, so the feed answer is the
price *per token* — do not multiply it by `uiMultiplier()` again.

## Verifying a token is genuine — do this before quoting an address

Robinhood Chain is permissionless, and community tokens on it **reuse listed
companies' names and symbols**. The CoinGecko token list carries both: only
Stock Tokens have the `• Robinhood Token` name suffix. For example two entries
share the symbol `GME` and the name "GameStop":

- `0x1b0e319c6a659f002271b69db8a7df2f911c153e` — real, answers `uiMultiplier()`
- `0x7e86381a763f0ecca2bdf27c54eac403ddd48123` — impersonator, call reverts

`uiMultiplier()` answering is the authoritative on-chain check, and this script
applies it. `isStockToken` has **three** states, and the difference matters:

| Value | Meaning | What to say |
|---|---|---|
| `true` | The call returned a multiplier | Genuine Stock Token |
| `false` | The node answered and the call **reverted** | Not a Stock Token — say so plainly, and do not hand over the address |
| `null` | The node was **unreachable** | Unverified, *not* disproven. Say the check could not run; never call the token fake on this basis |

A network fault is not evidence about a contract. Report `null` as uncertainty.

## Run it

```bash
# Price + on-chain state by name or ticker
{python} {baseDir}/scripts/chain_stocks.py --query "Apple"
{python} {baseDir}/scripts/chain_stocks.py --query TSLA

# A wallet's holding and its USD value
{python} {baseDir}/scripts/chain_stocks.py --query NVDA --holder 0xYourWallet

# Verify an arbitrary address without resolving a name
{python} {baseDir}/scripts/chain_stocks.py --address 0x7e86381a763f0ecca2bdf27c54eac403ddd48123

# Skip the price read (fewer calls, no feed fetch)
{python} {baseDir}/scripts/chain_stocks.py --query MSFT --no-price

# Point at a dedicated provider instead of the rate-limited public RPC
{python} {baseDir}/scripts/chain_stocks.py --query SPY --rpc-url https://your-provider/rpc
```

## The card renders itself

Every run writes a Web-chat card next to the JSON (`<SYMBOL>.cards.json`) and
`exec_command` publishes it. **There is nothing for you to do** — no extra
command, no flag, no `publish_artifact` call. Price, supply, multiplier and a
42-character address read far better as a card than as a bullet list.

So: answer in prose and let the card carry the numbers. Do **not** also
hand-write a table of the same fields.

Two things would silently break this, so do neither:

- **Never append `2>/dev/null`** (or any stderr redirect) to the command. The
  publish marker travels on stderr; discarding it discards the card.
- **Never pass `--no-cards`.** It exists for humans running the script by hand.

`--cards FILE` picks the filename if you need a specific one.
`scripts/chain_cards.py` builds the same payload from the JSON on stdin.

The badge is derived from the reading, worst case first — `unverified` (chain
unreachable) → `not-a-stock-token` → `price stale` → `oracle paused` →
`verified`. Still state the caveat in your own words as well; the card
supplements the answer, it does not replace it.

Answer in prose plus the card. Do not also hand-write a table of the same
fields.

### Output (JSON, trimmed)

```json
{
  "query": "Apple",
  "chainId": 4663,
  "explorer": "https://robin.etherscan.io/token/0xaf3d...93f9",
  "token": {
    "address": "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9",
    "symbol": "AAPL",
    "onchainSymbol": "AAPL",
    "isStockToken": true,
    "uiMultiplierFormatted": 1.0005660800610925,
    "oraclePaused": false,
    "totalSupplyFormatted": 10301.40749861,
    "price": {
      "feedAddress": "0x6B22A786bAa607d76728168703a39Ea9C99f2cD0",
      "usd": 317.47461437,
      "decimals": 8,
      "updatedAt": 1788206389,
      "ageSeconds": 41231,
      "stale": false,
      "heartbeatSeconds": 86400,
      "deviationThresholdPercent": 0.5
    }
  }
}
```

## Reporting rules

- **Check `price.stale` before quoting.** It is `true` when the answer is older
  than the feed's heartbeat or the oracle is paused. When it is `true`, give the
  age and call the number a last-known quote, never "the current price".
- Use `price.ageSeconds` rather than doing date maths on `updatedAt` yourself.
  Stock feeds update **24/5**, following equity market hours, and carry an
  86400s heartbeat — a day-old answer is normal, not an error.
- `price.unusableAnswer: true` means the feed returned a non-positive value.
  There is no price; do not report `$0`.
- Say `oraclePaused: true` out loud when it happens, and do not present a price.
- Report `readErrors` and `notes` rather than silently omitting a field. A note
  that says something was *unverified* never becomes a claim that it is false.
- These are **tokenized debt securities, not shares**. They are unavailable to
  US persons and restricted in several other jurisdictions. Never frame output
  as investment advice or as ownership of the underlying equity.

## Network reference

| | |
|---|---|
| Mainnet chain ID | `4663` |
| Public RPC | `https://rpc.mainnet.chain.robinhood.com` (rate-limited, no archive) |
| Explorer | `https://robin.etherscan.io` |
| Testnet chain ID | `46630` (`https://rpc.testnet.chain.robinhood.com`) |
| Gas token | ETH |

Data sources, both public and key-free: the CoinGecko Robinhood token list for
name→address resolution, and Chainlink's reference-data directory for feed
addresses. Feed addresses are read from that directory rather than hardcoded —
Chainlink's list is the source of truth.

## Notes

- Only a subset of tokens have an official Chainlink feed; when none exists the
  payload carries a note and omits `price` instead of guessing.
- The public RPC has no archive data. Historical reads and `eth_getLogs` sweeps
  need a dedicated provider — pass `--rpc-url`.
- Every failure path still prints JSON with an `error` or `readErrors` field;
  the script does not crash on a network fault.
