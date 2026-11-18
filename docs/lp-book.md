# The liquidity book (`trading.lp.book`)

Every liquidity position the engine's wallets hold, across protocols, the way
a wallet tracker lists DeFi positions. The desktop's **Trade › Liquidity** tab
reads it. The engine keeps the book (`src/agentos/trading/lp_book.py`), the
readers fill it (`lp.py` for Uniswap V4, `lp_amm.py` for everything else).
This file is the contract between the engine and its clients; change it
before changing either side.

The book itself is read-only. A Uniswap V4 row in the desktop's tab carries
**Collect** (when fees are owed) and **Remove** (the whole position, fees
included): each calls the LP writes ([`lp-write.md`](lp-write.md)), which park
an order for approval, and the tab moves to Approvals where its card is. The
other protocols are listed, not managed.

## What is covered

| Protocol | Kind | Base (8453) | Robinhood Chain (4663) |
|---|---|---|---|
| Uniswap V4 | concentrated | yes | yes |
| Uniswap V3 | concentrated | yes | yes |
| Uniswap V2 | pair | yes | yes |
| Aero Slipstream | concentrated, stakeable | yes | yes |
| Aero Basic (stable and volatile pools) | pair, stakeable | yes | yes |
| Aerodrome Slipstream (three deployments) | concentrated, stakeable | yes | not deployed |
| Aerodrome V2 | pair, stakeable | yes | not deployed |

Aerodrome became Aero on 2026-10-21. The Aerodrome pools still hold
liquidity, but their gauges stopped earning that day, so both generations are
read and named apart.

A *concentrated* position is an NFT with a price range. A *pair* position is
a balance of the pool's own LP token; it has no range and no id. A *staked*
position sits in a gauge instead of the wallet: it earns the gauge's reward
token (xAERO on Aero, AERO on Aerodrome) and its trading fees go to the
gauge.

## How it stays cheap

The book is built to make few RPC calls.

- **Nothing is read until someone asks.** A call answers at once from the
  ledger's cache (`lp_positions`, `lp_reads` in `trading.sqlite`). When the
  cache is older than 60 seconds the engine starts one refresh behind the
  answer and announces its end as `trading.changed` with `reason: "lp"`. A
  client that is not showing the book should not call it: a closed tab costs
  nothing.
- **One pass for every wallet.** A refresh batches all wallets of a chain
  into the same Multicall3 calls, and the two chains run side by side. A
  wallet with nothing to show costs one multicall per chain.
- **Enumerable where possible.** Uniswap V3, Aero and Aerodrome position
  managers are enumerable, so a wallet's ids are read from the chain
  (`balanceOf`, `tokenOfOwnerByIndex`) with no log scan. The ids are cached
  with the wallet's count and newest id; while both are unchanged they are not
  listed again, and emptied NFTs are not re-read.
- **Pairs come from the ledger.** The candidates for an LP token are the
  ERC-20s the wallet sync already knows the wallet holds. Each is checked
  once: it must name a known factory, and that factory must name it back for
  its two tokens. The verdict is remembered either way.
- **Gauges are walked rarely.** A staked position can only be found by asking
  gauges. Every gauge is asked on a wallet's first read, after the wallet's
  balances moved (at most every 5 minutes), on an explicit refresh, and every
  6 hours. In between, only the gauges a wallet was last seen in are asked.
  Aerodrome's gauges are frozen, so they are walked on the first read and on
  an explicit refresh only.
- **Uniswap V4 starts warm.** V4 has no enumerable index, so finding a
  position needs an indexer or a log search. The ids found are remembered;
  while they account for the wallet's NFT balance the search is skipped. A
  search that came up short and found nothing new is not repeated for 15
  minutes unless a refresh is asked for; one that found something goes on at
  the next refresh. The search asks the configured RPC endpoint
  (`trading.rpc_urls`) for the whole Transfer history first, then the chain's
  public node. A node that serves neither is walked back in windows: where it
  serves a one-wallet filter over a wider span than a many-wallet one
  (Robinhood Chain's public node: 10,000,000 blocks against 100,000), one
  wallet at a time. That node throttles the walk, so the first read of many
  wallets there fills in over a few refreshes.
- **The positions card shares those ids.** `trading.lp.positions`
  (`agentos trade lp positions`, what an agent runs) reads the chain live,
  but it verifies the remembered ids first and searches only for a wallet
  whose NFT balance they do not cover. What it finds for the vault's wallets
  is remembered for the book in turn.

What never changes (which token is a pair, where a pool is, the gauge
directory, cached id lists) is kept per chain under the state directory as
`lp/amm-CHAINID.json`. Deleting it is safe: the next refresh rebuilds it.

## Request

```jsonc
{
  "wallet": "0x… | label",   // optional: one vault wallet (default: all of them)
  "chainId": 8453,           // optional: one chain (default: both)
  "refresh": false           // optional: read the chain now (honoured every ~10 s)
}
```

Allowed for an agent (it is a read). Only vault wallets are covered; to read
any address, use `trading.lp.positions` (Uniswap V4 only).

## Answer

USD values are JSON numbers, or `null` when no price is known: never `0` for
"unknown". Amounts from the chain are decimal strings.

```jsonc
{
  "version": 1,
  "kind": "book",
  "ready": true,            // every wallet/chain in scope has been read at least once
  "refreshing": false,      // a refresh is running; trading.changed announces its end
  "readAt": 1794981234.5,   // unix seconds of the oldest read behind this answer; null until ready
  "partialScan": false,     // something could not be read; see warnings
  "warnings": ["string"],
  "wallets": [ { "address": "0x…", "label": "Main" } ],
  "chains": [ { "id": 8453, "key": "base", "name": "Base", "explorer": "https://basescan.org",
                "readAt": 1794981234.5, "asOfBlock": 58954977 } ],
  "protocols": [ { "key": "uniswap_v3", "name": "Uniswap V3", "count": 2, "valueUsd": 240.9 } ],
  "positions": [ BookPosition ],   // at most 200 rows; sorted, see below
  "totals": { "valueUsd": 3007.34, "feesUsd": 37.35, "rewardsUsd": 0.23,
              "count": 8, "outOfRange": 3, "unpriced": 0 }
}
```

`totals` cover every position, listed or not. A total no row could price is
`null`.

### BookPosition

A superset of the `Position` of [`lp-cards.md`](lp-cards.md); the shared
fields mean the same.

```jsonc
{
  "id": "8453:uniswap_v3:0x03a5…-5000150:0xowner",   // stable across refreshes
  "protocol": { "key": "uniswap_v3", "name": "Uniswap V3", "kind": "concentrated" | "pair" },
  "chain": Chain,
  "tokenId": "5000150" | null,         // null for a pair
  "manager": "0x…" | null,             // the NFT manager (not set for V4 or a pair)
  "owner": Wallet,                     // the wallet, also when the position is staked
  "token": Token, "quote": Token,
  "pool": { "poolId": "0x…",           // the pool address (V4: the poolId)
            "feePct": "0.05%" | "dynamic" | "stable" | "volatile" | null,
            "tickSpacing": 10 | null, "tick": -200000 | null, "liquidity": "raw",
            "priceUsd": number | null, "mcapUsd": number | null, "tvlUsd": number | null },
  "range": Range | null,               // null for a pair
  "status": "in-range" | "above-range" | "below-range" | "full-range" | "closed",
  "liquidity": "raw",                  // a pair: the LP-token balance
  "principal": { "base": Amount, "quote": Amount, "usd": number | null },
  "fees": { "base": Amount, "quote": Amount, "usd": number | null } | null,
  "staked": false,
  "gauge": "0x…" | null,
  "rewards": { "token": Token, "amount": Amount } | null,
  "share": 0.25 | null,                // a pair: share of the pool, 0-1
  "valueUsd": number | null,           // principal + fees + rewards, where priced
  "band": string | null,
  "distancePct": number | null
}
```

- `status` is `full-range` for every pair.
- `fees` is `null` where there is nothing to collect apart from the position
  (Uniswap V2 fees compound into the reserves; a staked pair's fees belong to
  the gauge). For a staked concentrated position it holds only what was
  already owed when it was staked.
- `rewards` is what the gauge would pay now. xAERO has no market of its own
  and redeems one-for-one for AERO, so on Base it is valued at AERO's price;
  where no price is known `amount.usd` is `null`.
- A token no source prices is valued at its pool's own ratio against the
  priced side.

### Order

Out of range first (`above-range`, `below-range`), then `in-range` and
`full-range`, then `closed`; each group by `valueUsd` descending, unpriced
rows last. Closed positions with nothing owed are left out.

## Events

`trading.changed` with `reason: "lp"` after every refresh. A client showing
the book re-reads it then; polling is only a backstop (the desktop asks every
30 seconds while the tab is open, which is also what keeps the cache warm).
