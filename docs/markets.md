# Stock Token markets

Artifact type: `application/vnd.agentos.markets+json`.

*Which tokens trade against NVDA?* On Robinhood Chain a Stock Token is not
only something you buy: it is the **quote asset** for hundreds of other
tokens. Launchpads (Bankr, long.xyz, Pons V2) let a memecoin launch *priced
in NVDA*, and those pools are the bulk of the stock's on-chain market —
measured 2026-10-06, 172 of the 200 NVDA pools GeckoTerminal lists have NVDA
on the quote side, and the deepest NVDA pool of all is AI/NVDA (Artificial
Inu, $4.7 M), bigger than NVDA/USDG on Uniswap v3.

The **markets** read answers that question for one token: every pool it is
in, on every DEX the chain has, split into *priced in NVDA* (NVDA is the
quote) and *NVDA priced in* (NVDA is the base: NVDA/USDG, NVDA/WETH), with
TVL, 24 h volume, price, age and a Swap button on each row.

This file is the contract between the engine, the gateway, the CLI, the
shared chat renderer and the desktop. Change it before changing any of
them. Whatever it does not say, `docs/lp-cards.md` says (the card plumbing
is the same).

Chains: Robinhood Chain (4663) first; Base (8453) works the same way (the
sources index both). Not in scope: swapping *through a named pool* (a swap
from a row is a best-route swap, see "Swap"), LP writes, on-chain pool
enumeration from logs (the public Robinhood node caps `eth_getLogs` at 10 M
blocks per call and a full scan took longer than 15 minutes).

## Vocabulary

| word | meaning |
|---|---|
| **token** | the token the user asked about (`NVDA`); the card is about it |
| **pool** | one liquidity pool on one DEX holding the token and one other token |
| **counterparty** | the other token in the pool (`AI` in `AI/NVDA`) |
| **side** | `quote` when the token is the pool's quote asset (the counterparty is priced in it: `AI/NVDA`); `base` when the token is the base (`NVDA/USDG`) |
| **priced in** | the `quote` section: *tokens priced in NVDA* |
| **lookalike** | a counterparty that borrows a listed company's symbol or name but is not a Robinhood Stock Token (`memestock TSLA`, ` NVIDIA Robinhood Token ` without the bullet) |
| **via Uniswap** | the pool is on Uniswap v2/v3/v4, so the Trading API's best route can use it |

## Prerequisites this feature ships with

Today the desk cannot swap a Stock Token on Robinhood Chain at all, so a
Swap button would be decoration. Two engine changes land with the feature:

1. **Universal Router pins** (`trading/uniswap.py`, `UNIVERSAL_ROUTERS`).
   The Trading API now sends swaps to Universal Router **v2.1.2**; the table
   pinned only Base's v2.0 and nothing for 4663, so the engine refused every
   Uniswap swap on Robinhood Chain and, since the API moved, on Base too.
   The table becomes a per-chain **set** of verified routers:

   | chain | routers (lowercase) | source |
   |---|---|---|
   | 8453 | `0x6ff5693b99212da76ad316178a184ab56d299b43` (v2.0), `0xd6145b2d3f379919e8cdeda7b97e37c4b2ca9c40` (v2.1.2) | `Uniswap/universal-router` `deploy-addresses/base.json` |
   | 4663 | `0x8876789976decbfcbbbe364623c63652db8c0904` (v2.1.1), `0x204faca1764b154221e35c0d20abb3c525710498` (v2.1.2) | `deploy-addresses/robinhood.json`; `/v1/swap` answered `to = 0x204F…` on 2026-10-06 |

   `trusted_targets` returns the chain's set plus the proxy; a chain with no
   entry still gets the empty set (refuse, never guess). Verified 2026-10-06:
   both 4663 addresses hold code (24 380 and 24 546 bytes).

2. **Stock Token routing** (`trading/service.py`, `trading/aggregator.py`).
   0x refuses the Stock Tokens for legal reasons
   (`trading.token_not_tradeable`); Uniswap trades them (checked: ETH→AAPL
   via a v4 0.08 % pool, NVDA→AI, NVDA→ORBIO). The AgentOS DEX carries
   Uniswap as a second liquidity source, asked for with `source=uniswap` on
   `/v1/quote` and executed through the same Router. So:
   - `quote`, `swap`, and every path that quotes for an order (DCA runs,
     trigger fires, bracket legs, the re-quote at execution) name that
     source when either token is a tokenized stock (`stock_token`). No
     Uniswap API key is needed, and the quote dict and the order row say
     `provider: "aggregator"`.
   - Every other pair is asked the way it always was, with no `source`,
     and answered by 0x.
   - If 0x still answers `token_not_tradeable` for a pair the flag missed,
     the engine asks the Uniswap source once for that quote.
   - If the Uniswap source cannot take the pair (the gateway's
     `SOURCE_UNAVAILABLE`), the DEX's error is returned as it is for a
     stock, and 0x's `token_not_tradeable` for a pair the flag missed. The
     AgentOS DEX is the only swap provider: there is no other to fall back
     to, and no Uniswap API key to configure.

3. **Stock Token detection** (`trading/prices.py`, `trading/token_lists.py`).
   A row of a chain's stock list (`robinhood_stock_tokens.json`,
   `base_stock_tokens.json` in the AgentOS
   [token lists](https://github.com/use-agent-os/agent-os-token-list),
   tagged `stock`) is a Stock Token. The ledger's own token rows keep no
   tag, so a Robinhood Stock Token is also known by its name: one helper,
   `is_stock_token_name(name)`, matches `… • Robinhood Token` and the
   truncated forms an older registry stored (`… • Robinhood Toke`,
   `… • Robinhood T`: a bullet followed by any prefix of `Robinhood Token`,
   the regexes of `robinhood-chain-stocks/scripts/chain_stocks.py`). A name
   without the bullet is **not** a Stock Token however it is spelt
   (` NVIDIA Robinhood Token ` is a lookalike with a $649 k pool).

## Sources, and why in this order

| source | what it gives | limits |
|---|---|---|
| **GeckoTerminal** `GET /api/v2/networks/{robinhood\|base}/tokens/{address}/pools?page=N&include=base_token,quote_token,dex&sort=h24_volume_usd_desc` | the only listing that returns pools on **both** sides of the token; 20 per page, up to page 10; per pool: `address`, `name` (`NVDA / USDG 0.01%` — the fee is only in the name), `reserve_in_usd`, `volume_usd.h24`, `price_change_percentage.h24`, `transactions.h24.{buys,sells}`, `pool_created_at`, `base_token_price_usd`, `quote_token_price_usd`, `base_token_price_quote_token`, `quote_token_price_base_token`, relationships `base_token`, `quote_token` (`robinhood_0x…`), `dex` (`uniswap-v4-robinhood`, `bankr-robinhood`, `pons-v2-dex`, `ramses-v3-robinhood` …); `included[]` carries each token's `name`, `symbol`, `decimals`, `image_url` | **30 requests / minute per IP**; 429 seen during research. Header `accept: application/json;version=20230302` |
| **DexScreener** `GET /token-pairs/v1/{robinhood\|base}/{address}` | enrichment for pools GeckoTerminal also has: `labels` (`v2`/`v3`/`v4`), `liquidity.usd`, `volume.h24`, `url`; matched on `pairAddress` (lowercase; a v4 pair address is the poolId) | 30 pairs, **base side only** — it never lists `AI/NVDA` for NVDA; 300 / min |
| **chain** | for a Stock Token: the Chainlink feed price (`oracle`), `oraclePaused()`; ported from `chain_stocks.py` (feed directory `https://reference-data-directory.vercel.app/feeds-robinhood-mainnet.json`, `latestRoundData`, 8 decimals; `uiMultiplier()` `0xa60bf13d`; `oraclePaused()` `0x7706ba52`) | `null` on any failure, never an error |

DexScreener alone was the old plan; it is wrong for this feature because
the pools the user asks about (`AI/NVDA`, `ORBIO/NVDA`) have the token on
the quote side. The engine's own V4 discovery (`lp.discover_pools`) finds
8 NVDA pools, all against USDG/ETH, for the same reason.

### Engine behaviour

`trading/markets.py`:

```python
async def markets(service, *, chain, target, side="all", min_tvl_usd=10_000.0,
                  limit=50, lookalikes=False, deep=False) -> dict
```

- `target` resolves through `service.resolve_token` (an address, `ETH`, or a
  symbol; a symbol that matches one Stock Token picks it over the
  community tokens of the same symbol — the existing rule).
- Pages GeckoTerminal until `limit` rows survive the filters, a page comes
  back short, or the page cap: **5 pages** (100 pools) by default, **10**
  with `deep`. Pages are read in **bursts of up to 5 concurrent requests**
  (a page GeckoTerminal's edge has not cached takes 11–13 s, a cached one
  0.2 s, measured 2026-10-06; one page at a time cost 40 s per token) with
  a **120 s cache per `(chain, token)`** of the raw pages, so a refresh or a
  second section costs nothing. Pages are kept as a contiguous prefix: a 429
  drops that page and every later one; what was read is returned with
  `partial: true` and the warning *"GeckoTerminal rate limit: showing the
  first N pools"*. No pages at all (network, 5xx) →
  `trading.markets.unavailable`. `partial` is also set when the page cap
  stopped a read that had more pools (warning points at `deep`).
  GeckoTerminal's limit bites earlier than its stated 30/min (a 429 after
  ~11 requests in 90 s was seen): the first 429 inside a burst is retried
  once after 15 s; a second 429 ends the read (`rateLimited: true`).
  An uncached page was measured at up to 18.5 s (`cf-cache-status: MISS`),
  so the per-page timeout is **60 s**, and a page that timed out inside a
  burst is retried **once** after the burst before the read gives up on it.
  Clients show *"A first read of a token can take up to a minute"* while
  loading.
- One DexScreener call enriches matches; its failure is a warning.
- Rows below `min_tvl_usd` are dropped and counted (`counts.belowMinTvl`).
  Lookalike counterparties are dropped and counted unless `lookalikes`
  (`counts.hiddenLookalikes`). A **lookalike** is a counterparty whose
  symbol (case-insensitive) or name (minus the suffix) matches a Stock
  Token in the chain's token list but whose address is not that token's.
- `side` filters to one section; `all` returns both. Rows are sorted by
  `tvlUsd` descending within each section.
- Counterparty metadata comes from `included[]` first, then
  `prices.known_token` for `verified`/`stockToken`.
- `launcher` labels a pool by its DEX id: `bankr-*` → `Bankr`, `pons-*` →
  `Pons`, `clanker-*` → `Clanker`, `long-*`/`virtuals-*` as named; the
  table lives in `markets.py` and is best-effort (`null` otherwise).
- `viaUniswap` is `dex.id` starting with `uniswap-`.
- `feePct` is parsed from the trailing `N%` of GeckoTerminal's pool name
  (`"0.01%"` → `0.01`); `null` when absent.
- Prices: for `side = quote`, `priceUsd` is the **counterparty's** USD price
  and `priceInToken` the counterparty priced in the token. For `side =
  base`, `priceUsd` is the **token's** USD price in that pool and
  `priceInToken` is the token priced in the counterparty. `priceInToken`
  is computed as the **ratio of the two USD prices** GeckoTerminal gives
  for the pool (`base_token_price_usd`, `quote_token_price_usd`) — its
  `base_token_price_quote_token` field was measured 45–77 % off real
  quotes on launchpad pools (2026-10-06) and is used only when one USD
  price is missing. Strings from the source become JSON numbers; unknown →
  `null`, never `0`.
- A counterparty at the zero address is the chain's native coin:
  `{address: "0x000…0", symbol: "ETH", name: "Ether", native: true}`, never
  "WETH". `dex.version` falls back to `"v4"` when the pool address is 32
  bytes (a v4 poolId) and to the version in the DEX id (`uniswap-v3-…`).
- `token.oracle` is read only on 4663 and only when `token.stockToken`;
  `premiumPct` on a `base` row is `(priceUsd / oracle.usd - 1) * 100` when
  both exist, else `null` (never on `quote` rows: a memecoin has no oracle).

## Payload

```jsonc
{
  "version": 1,
  "kind": "markets",
  "chain": { "id": 4663, "key": "robinhood", "name": "Robinhood Chain", "explorer": "https://robin.etherscan.io" },
  "fetchedAt": "2026-10-06T04:12:00Z",
  "partial": false,                       // paging stopped early (rate limit / cap): the card shows a badge
  "warnings": ["string"],
  "token": {
    "address": "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec",
    "symbol": "NVDA", "name": "NVIDIA • Robinhood Token", "decimals": 18,
    "logoUrl": "https://…" | null,
    "verified": true, "stockToken": true,
    "priceUsd": 240.30 | null,            // from the engine's price feed (DexScreener best pair)
    "oracle": { "usd": 239.74, "updatedAt": "2026-10-05T20:38:49Z", "ageSeconds": 47269, "stale": false, "paused": false } | null
  },
  "counts": {
    "scanned": 100, "shown": 34, "belowMinTvl": 61, "hiddenLookalikes": 5,
    "limited": 0,                       // rows above the floor that `limit` cut (the counts line says "· 12 more over the limit")
    "pages": 5, "pageCap": 5,
    "pageCapHit": false,                // the page cap, not `limit`, ended a read that had more pools: *Deeper* is offered when true (and not yet `deep`)
    "rateLimited": false                // a 429 cut the read short: *Deeper* (or *Read again*) is offered again even when `deep`, so the user can retry after a minute
  },
  "sections": {
    "quote": [ Pool, … ],                 // "Priced in NVDA" — empty array when side=base
    "base":  [ Pool, … ]                  // "NVDA priced in" — empty array when side=quote
  },
  "request": { "kind": "markets", "params": { "target": "0xd060…", "chainId": 4663, "side": "all", "minTvlUsd": 10000, "limit": 50, "lookalikes": false, "deep": false } }
}
```

`Pool`:

```jsonc
{
  "poolAddress": "0xcbdfea90…ce27",      // GeckoTerminal address; for v4 the poolId
  "pair": "AI/NVDA",                     // counterparty first on quote rows, token first on base rows
  "side": "quote" | "base",
  "dex": { "id": "bankr-robinhood", "label": "Bankr", "version": "v4" | "v3" | "v2" | null },
  "launcher": "Bankr" | null,
  "viaUniswap": false,
  "feePct": 1.0 | null,
  "counterparty": { "address": "0x2e8c…1e18", "symbol": "AI", "name": "Artificial Inu", "decimals": 18, "logoUrl": null, "verified": false, "stockToken": false, "lookalike": false },
  "tvlUsd": 4732293.0 | null,
  "volume24hUsd": 806473.0 | null,
  "txns24h": { "buys": 812, "sells": 790 } | null,
  "priceUsd": 0.1131 | null,             // see "Prices" above
  "priceInToken": 0.000471 | null,
  "change24hPct": -3.2 | null,
  "premiumPct": null,                    // base rows of a Stock Token only; rounded to 2 decimals, never negative zero (|x| < 0.005 → 0.0)
  "createdAt": "2026-07-25T00:52:36Z" | null,
  "url": "https://www.geckoterminal.com/robinhood/pools/0xcbdf…" ,
  "swap": { "chainId": 4663, "tokenIn": "0xd060…", "tokenOut": "0x2e8c…" }   // what the Swap button prefills: sell the token, buy the counterparty
}
```

Every row has `swap`; the renderer decides whether to show the button (see
"Swap"). `swap.tokenIn` is the token on `quote` rows (spend NVDA, get AI)
and on `base` rows too (sell NVDA for USDG); the desktop's swap panel has a
flip control for the other direction.

## Gateway

`trading.markets` — read-only, allowed from an agent.

| param | type | default |
|---|---|---|
| `target` | string, required | symbol, address or `ETH` (also accepts `token`) |
| `chainId` | 8453 \| 4663 | 4663 |
| `side` | `all` \| `quote` \| `base` | `all` |
| `minTvlUsd` | number ≥ 0 | 10000 |
| `limit` | 1–200 | 50 |
| `lookalikes` | bool | false |
| `deep` | bool | false |

Errors: `trading.invalid` (bad params, ambiguous symbol — with
`details.candidates` as `tokens.resolve` does), `trading.not_found`
(unknown token), `trading.markets.unavailable` (no page could be read).
Registered in `gateway/rpc_trading.py` next to `trading.lp.*`, with the
same `_with_request` echo.

## CLI

```
agentos trade markets <token> [--chain base|robinhood] [--side all|quote|base]
                              [--min-tvl 10000] [--limit 50] [--lookalikes] [--deep]
                              [--json] [--no-card]
```

Default chain **robinhood** (the only `trade` command with that default —
say so in `--help`). Human output: the token line (symbol, price, oracle
and premium when known), then one table per section with columns
`PAIR  DEX  TVL  VOL 24H  PRICE  IN NVDA  AGE  FLAGS` (flags: `uni`,
`lookalike`, `stock`), then one line of counts
(*34 of 100 pools shown · 61 under $10k · 5 lookalikes hidden · 12 more
over the limit · partial*). Prices are printed with at most 6 significant
digits and the table folds rather than truncates in an 80-column terminal.
An ambiguous symbol lists its candidates (symbol, name, address) in human
mode too.
With `--json`: JSON on stdout, the card written to
`markets-cards/markets-<symbol>-<utc stamp>.json` (20 newest kept) and the
`publish_artifact path=… mime=application/vnd.agentos.markets+json` line,
exactly as `docs/lp-cards.md` describes for LP cards. Errors are the
`{"error": {...}}` envelope on stderr, exit 1, no card.

## Chat card

Shared renderer: `frontend/src/views/chat/transcript/markets.ts`.

Mounted by `createMarketsMounter` from `useTranscript.ts` beside
`createLpMounter`. One card per payload:

- Head: token logo, symbol, name, chain pill, `priceUsd`, and for a Stock
  Token the oracle price with a `stale`/`paused` badge when set.
- Two sections in this order, each with a title and a count: **Priced in
  NVDA** (`sections.quote`) and **NVDA priced in** (`sections.base`). An
  empty section shows one line (*No pools against NVDA above $10k*).
- A row: pair, DEX label + version pill, launcher pill when set, TVL,
  24 h volume, price (USD) and price in token, 24 h change coloured, age
  (`3d`, `2mo`), flags. Lookalike rows (only when requested) wear a warning
  pill. `premiumPct` shows on base rows as `+0.4 % vs oracle`.
- Footer: counts line (including `limited` when > 0), `partial` badge, ↻
  refresh (re-runs `trading.markets` with `request.params`, like LP cards),
  *Show lookalikes* (only when `hiddenLookalikes` > 0 and the read did not
  include them) and *Deeper* (only when `counts.pageCapHit` and not `deep`)
  links that re-run with `lookalikes: true` / `deep: true`.
- The launcher pill is hidden when it would repeat the DEX label (Bankr on
  Bankr). A lookalike row (and any row whose counterparty symbol equals the
  token's) shows the counterparty's **name** after the pair so two "GME/GME"
  rows can be told apart.
- **Swap** button per row, rendered only when the mounter is given an
  `onSwap(swap: { chainId, tokenIn, tokenOut })` dep; the web console passes
  none (no button), the desktop passes one.

Rows are capped at 40 per section with *+N more* that expands in place.

Narrow cards (under ~480 px): the **pair column is never starved** — it
keeps at least 9 characters before any other column shrinks; the Price
column gives way first, then Vol and Age disappear (≤ 420 px), the venue
pills wrap as a group under the pair, and "Uniswap" never breaks mid-word.
A premium of ±0.0 % is shown as `0.0%` with the flat tone.

### CSS hooks

Verbatim; the desktop skins these, the web styles them.

```
.mk-card                       the card root; data-chain="4663"; data-partial="true" when partial
.mk-head  .mk-logo  .mk-symbol  .mk-name  .mk-chain  .mk-price  .mk-oracle  .mk-oracle-badge[data-tone=warn|danger]
.mk-section[data-side=quote|base]  .mk-section-title  .mk-section-count  .mk-empty
.mk-rows                       the row list
.mk-row[data-lookalike=true]   one pool
.mk-pair  .mk-cp-name (the counterparty's name on lookalike / same-symbol rows)  .mk-dex  .mk-version  .mk-launcher  .mk-tvl  .mk-vol  .mk-px  .mk-px-in  .mk-change[data-tone=up|down|flat]  .mk-age  .mk-flags  .mk-flag[data-kind=uni|stock|lookalike]  .mk-premium[data-tone=up|down|flat]
.mk-swap                       the Swap button (absent when no onSwap)
.mk-more                       the "+N more" control
.mk-foot  .mk-counts  .mk-partial  .mk-refresh  .mk-link[data-action=lookalikes|deep|again]
```

The exact nesting is documented verbatim in `MARKETS_DOM` (exported from
`markets.ts`); the desktop skin is tested against the real DOM.

Text and numbers use the renderer's existing money/percent formatters
(`formatMoney`, compact for ≥ $100k: `$4.7M`).

## Desktop

- **BOOK tab `markets`** (`stores/trading-ui.ts` `BookTab` gains
  `'markets'`; icon `Layers`), placed after `swap`. **Full desk**
  (`TradingView.tsx`) gains the same tab after `holdings`.
- `views/trading/Markets.tsx`: a search field (symbol or address; the
  current chain from the desk's chain control, defaulting to Robinhood
  Chain when the user types a known Stock Token symbol on Base — i.e. the
  component looks the symbol up with `useTokenSearch` on both chains and
  prefers the verified Stock Token; among several verified exact matches
  on the chain it prefers a Stock Token, then the highest liquidity, and
  shows which token it picked — it must never silently take the first
  match; the *N matches* menu is wide enough to show each token's full
  name, liquidity and short address), filters (*Min TVL* segmented
  `$1k / $10k / $100k`, *Lookalikes* toggle), the two sections as lists,
  the counts footer, *Deeper* control. Rows match the card's content but
  are built in the desktop's own markup and `.mac-*` / `trd-*` vocabulary
  (`desktop-shares-logic-not-ui`): share the **logic** (`useMarkets` hook,
  formatters, lookalike/launcher helpers exported from the renderer module)
  and nothing presentational. Loading, empty, rate-limited (`partial`) and
  error states are drawn. At the BOOK's minimum width (300 px) the tab
  icons keep their size (the bar scrolls or wraps, icons never shrink),
  the price-in-token cell wraps rather than truncating to "0.0…", and the
  premium never overlaps the Age column; pair text that must truncate does
  so with an ellipsis, never mid-glyph; the tab strip shows it scrolls (an
  edge fade) when tabs are hidden.
- `useMarkets(params, enabled)` in `stores/trading.ts` → `rpc.call('trading.markets', …)`,
  `staleTime` 60 s, keyed by every param.
- **Swap from anywhere**: `stores/trading-ui.ts` gains
  `swapRequest: { chainId, tokenIn: Token, tokenOut: Token, seq } | null` and
  `requestSwap(req)`. `Book` and `TradingView` subscribe and turn it into
  their `SwapPrefill` (+ `setTab('swap')` / `setBookTab('swap')`). The
  Markets row's Swap button, the chat card's `onSwap` (wired in the
  desktop's transcript deps), and the existing Holdings Swap button all go
  through `requestSwap`; the Holdings path keeps its behaviour.
- A Holdings row of a Stock Token gets a **Markets** action next to Swap
  that opens the tab with that token.
- i18n: `trading.markets.*` keys in `i18n/en/trading.ts` (and the other
  locales the file set has, English fallback is fine).
- Desk agent (`desk/agent.ts`): the allowlist admits `agentos trade
  markets`; TOOLS.md teaches: *"pairs of X", "what trades against X",
  "markets for X", "tokens priced in X", "pools of X on every DEX"* →
  `agentos trade markets X --chain robinhood --json`; the card is the
  answer; a liquidity question about **one** pool stays `lp pool` — but a
  launchpad pool (Bankr, Pons: `lp pool` answers
  `trading.lp.pool_key_unknown`) is answered with the markets card of its
  quote token, never with a different pool's card.
  `TRADING_AGENT_VERSION` bumps.
- Desktop skin for `.mk-*` in `views/chat/chat.css` next to `.lp-card`,
  distinct from the web look.

## Docs to update in the same change

`docs/cli.md` (the `trade markets` command, and the Stock Token paragraph:
they now route through Uniswap), `src/agentos/skills/bundled/agentos/SKILL.md`
(the `trade` command list **and** its `trade` row that said the 29 stocks
"cannot be routed at all"), `src/agentos/skills/bundled/wallet-trading/SKILL.md`
and `src/agentos/skills/bundled/robinhood-agentic-trading/SKILL.md` (same
sentence: Stock Tokens route through Uniswap when a key is configured; the
aggregator alone refuses them), `docs/features/trading.md` (the aggregator
paragraph quoted above), `CHANGELOG.md` under *Unreleased / Added*.

## Verified facts the tests pin

- Router sets above; `trusted_targets` on 4663 contains `0x204f…`.
- `is_stock_token_name`: `"NVIDIA • Robinhood Token"` → true,
  `"International Business Machines • Robinhood Toke"` → true,
  `" NVIDIA Robinhood Token "` → false, `"memestock TSLA"` → false.
- Routing: a Stock Token on either side → the quote is asked with
  `source=uniswap` and carries `provider: "aggregator"`; no Uniswap key
  exists or is needed.
- Markets: a recorded GeckoTerminal page set for NVDA (fixture) yields
  `AI/NVDA` in `sections.quote` with `dex.label = "Bankr"`, `NVDA/USDG` in
  `sections.base` with `feePct = 0.01`, `viaUniswap = true`; a 429 on page
  3 gives `partial: true` and the warning; lookalike filtering hides a
  counterparty named `NVDA` whose address differs from the Stock Token.
