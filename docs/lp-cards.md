# LP cards

Artifact type: `application/vnd.agentos.lp+json`.

Uniswap V4 liquidity read-outs rendered as cards in the chat transcript.
The engine produces the payload (`agentos trade lp …`), the transcript
renders it (`frontend/src/views/chat/transcript/lp.ts`). This file is the
contract between the two; change it before changing either side.

Phase 1 is read-only. Chains: Base (8453) and Robinhood Chain (4663).

## How a card reaches the chat

```
agentos trade lp pool PEPE --chain base --json
```

1. The CLI calls the gateway (`trading.lp.pool` …), which runs the read in
   the engine with the engine's own RPC configuration (`trading/chains.py`),
   not the skill's env variables.
2. With `--json` the result is printed as JSON on stdout (the agent reads
   that) and — unless `--no-card` is given — also written to
   `lp-cards/<kind>-<slug>-<utc stamp>.json` under the working directory
   (the agent's workspace; the 20 newest files are kept), followed by one
   last stdout line

   ```
   publish_artifact path=<file> mime=application/vnd.agentos.lp+json
   ```

   which the gateway auto-publishes as a turn artifact (see
   `src/agentos/tools/builtin/artifacts.py`, "Inline artifacts announced on
   stdout"). No `publish_artifact` tool call is needed. Human (non-`--json`)
   runs print a table and write no card.
3. The transcript sees an artifact with that MIME type, fetches the body and
   mounts a card in place of the artifact chip.

Errors (pool not found, RPC down, a token address given as `--wallet` →
`trading.lp.not_a_wallet`) are `{"error": {"code", "message"}}` on
**stderr** with a non-zero exit and **no card**: the agent answers in text.
An empty `positions` result **is** a card (the empty state).

## Payload

All amounts that come from the chain are decimal **strings** (`raw` is the
integer in base units, `human` is already divided by decimals). USD values are
JSON numbers or `null` when no price is known — never `0` for "unknown".

### Envelope (every kind)

```jsonc
{
  "version": 1,
  "kind": "pool" | "ranges" | "position" | "positions",
  "chain": Chain | null,          // null only for kind=positions (each row carries its own)
  "asOfBlock": 21044901,          // block the reads were made against (0 if unknown)
  "fetchedAt": "2026-09-27T09:30:00Z",
  "partialScan": false,           // true when any scan was truncated; the card shows a badge
  "warnings": ["string"],         // human-readable, may be empty
  "request": { "kind": "pool", "params": { "target": "0x…", "chainId": 8453 } }  // what ↻ refresh re-runs: the read RPC's own params (position: tokenId+chainId; positions: chainIds/wallets/all/budgetSeconds)
}
```

```jsonc
Chain    = { "id": 8453, "key": "base" | "robinhood", "name": "Base", "explorer": "https://basescan.org" }
Token    = { "address": "0x…", "symbol": "PEPE", "decimals": 18, "priceUsd": number | null }
Amount   = { "raw": "1240000000000000000000000", "human": "1240000", "usd": number | null }
Range    = { "tickLower": -201000, "tickUpper": -180000,
             "priceLower": number, "priceUpper": number,   // quote per 1 base token
             "mcapLower": number | null, "mcapUpper": number | null }  // USD, null when base has no supply/price
Pool     = { "poolId": "0x…", "hook": "0x…" | null, "tickSpacing": 200,
             "feePct": "1%",                              // lpFee already formatted; dynamic-fee pools: "dynamic"
             "tick": -190000, "liquidity": "raw string",
             "priceUsd": number | null,                  // base token, USD
             "mcapUsd": number | null, "tvlUsd": number | null }
Wallet   = { "address": "0x…", "label": string | null, "inApp": boolean }
Status   = "in-range" | "above-range" | "below-range" | "closed"
           // above-range: current price is above the position's upper bound (all quote); below: all base
```

`base` is the token the user asked about (or `currency0` when a poolId was
given), `quote` is the other side. Prices are quote-per-base; USD values
come from the engine's price lookup.

### kind = "pool"

```jsonc
{
  ...envelope,
  "token": Token, "quote": Token,
  "pool": Pool,
  "reserves": { "base": Amount, "quote": Amount },
  "safety": {
    "launcher": { "name": "Clanker" | null, "address": "0x…" | null },
    "locked": true | false | null,      // null = unknown
    "note": string | null               // e.g. "LP owned by launchpad locker"
  },
  "topRanges": [ { ...Range, "liquidity": "raw", "share": 0.42, "owner": "0x…" | null } ]  // ≤ 5, by liquidity desc; may be empty
}
```

### kind = "ranges"

Liquidity distribution.

```jsonc
{
  ...envelope,
  "token": Token, "quote": Token,
  "pool": Pool,
  "current": { "tick": -190000, "priceUsd": number | null, "mcapUsd": number | null },
  "segments": [
    { ...Range, "liquidity": "raw", "share": 0.0–1.0,      // share of the sum of segment liquidity
      "base": Amount, "quote": Amount, "active": boolean }    // active = contains the current tick
  ],
  "scan": { "mode": "ticks" | "logs", "scannedWords": 12, "fullWords": 40, "truncated": false }
}
```

Segments are contiguous and sorted by `tickLower` ascending. The chart's x-axis
is market cap (`mcapLower`/`mcapUpper`); when those are `null` for every
segment the renderer falls back to price.

### kind = "position"

```jsonc
{
  ...envelope,
  "position": Position
}

Position = {
  "chain": Chain,
  "tokenId": "48213",
  "owner": Wallet,
  "token": Token, "quote": Token,
  "pool": Pool,
  "range": Range,
  "status": Status,
  "liquidity": "raw",
  "principal": { "base": Amount, "quote": Amount, "usd": number | null },
  "fees":      { "base": Amount, "quote": Amount, "usd": number | null },
  "valueUsd": number | null,          // principal.usd + fees.usd, null when either side is unpriced
  "band": string | null,              // the skill's mcap band label, e.g. "$2.1M – $9.8M"
  "distancePct": number | null        // how far the current price is outside the range, in %, null when in range
}
```

### kind = "positions"

```jsonc
{
  ...envelope,
  "chain": null,
  "asOfBlocks": { "base": 21044901, "robinhood": 62942097 },  // per scanned chain; asOfBlock is the first
  "wallets": [ Wallet ],              // the wallets that were scanned
  "chains": [ Chain ],                // the chains that were scanned
  "positions": [ Position ],          // already sorted, see below
  "totals": { "valueUsd": number | null, "feesUsd": number | null, "count": 3, "outOfRange": 1 }
}
```

`totals` cover every position found. `valueUsd` sums the priced positions
(a warning counts the unpriced ones). `feesUsd` sums the priced fees; when
nothing priced remains except zeros it is `null` rather than a misleading
`$0.00`, with a warning.

`positions` holds at most 50 rows; `totals` always cover every position
found, and a warning says how many were left out. Sort order is fixed by
the engine and preserved by the renderer:
out-of-range first (`above-range`, `below-range`), then in-range, each group
by `valueUsd` desc; positions with `valueUsd: null` last within their group
and flagged "no price"; `closed` positions are omitted unless `--all`.

## CLI surface

```
agentos trade lp pool      <token|poolId> [--chain base|robinhood] [--quote SYMBOL] [--json] [--no-card]
agentos trade lp ranges    <token|poolId> [--chain base|robinhood] [--json] [--no-card]
agentos trade lp position  <tokenId>      --chain base|robinhood   [--json] [--no-card]
agentos trade lp positions [--wallet ADDR]… [--chain base|robinhood]… [--budget-seconds N] [--all] [--json] [--no-card]
```

- `<token>` accepts an address or a ticker the engine can resolve on that chain.
- `positions` with no `--wallet` scans every wallet in the engine vault on both
  chains. With `--wallet` only the named addresses (or vault labels) are
  scanned; an address outside the vault gets `inApp: false`. A `--wallet`
  that is an ERC-20 contract is refused with `trading.lp.not_a_wallet`.
- `--chain` is optional on `pool`/`ranges`: Base is tried first, then
  Robinhood Chain. On `positions` it is repeatable (no `--chain` = both);
  a repeat on the other commands is refused with `INVALID_ARGUMENT`.
- `positions` stops discovering at `--budget-seconds` (default 25, 5–300)
  and returns what it has, flagged `partialScan` with a warning naming what
  was skipped. USD prices are looked up for the listed rows' tokens plus a
  bounded batch; the rest are valued at their own pool's price.
- Every command is read-only and allowed on the agent surface.
- A read takes 5–15 s typically, up to 30 s on two chains; run once, in the foreground.

## Rendering rules (frontend)

- One renderer, four layouts, keyed by `kind`.
- Header: base/quote symbols, chain, fee, and a status pill (`in-range`
  green, `above-range`/`below-range` amber, `closed` grey).
- Footer: `#tokenId · owner (short) · as of block N · <relative time>`, plus
  copy-address and open-in-explorer actions. Relative time is computed from
  `fetchedAt` at mount and refreshed once a minute.
- `partialScan: true` → a "partial scan" badge next to the chart, never a
  silent chart.
- USD `null` → render "—" with a "no price" hint; never `$0`.
- The distribution chart is plain SVG (no new dependency): bars per segment,
  x-axis in market cap (fallback price), a marker for the current value,
  hover shows the segment's range, liquidity share and both amounts.
- Empty `positions` → an empty-state card: "No Uniswap V4 positions in
  `<wallets>` on `<chains>`".
- The renderer lives in the shared frontend; the desktop restyles it through
  `desktop/src/renderer/src/views/chat/chat.css` using `data-lp-*` hooks so the
  desktop card looks distinct from the web one.
