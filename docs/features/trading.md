# Wallets and Trading

AgentOS can hold EVM wallets of its own and swap tokens on **Base** and
**Robinhood Chain** through a pluggable swap provider: the **AgentOS
Aggregator** (default, no key) or the **Uniswap Trading API** (fallback,
needs a free API key). The engine owns everything:
the encrypted wallet vault, the ledger with cost basis and PnL, quote and
swap execution, and the guardrails that bound what an agent may do on its
own. The desktop app's Trading page, the `agentos wallet` / `agentos trade`
commands and the bundled `wallet-trading` skill are all clients of the same
gateway RPC surface (`wallet.*`, `trading.*`).

> Swaps move real money and cannot be undone. Start with small amounts, keep
> the daily cap low, and treat the vault directory like an SSH key.

## What it does

- **Wallets** — create new keys or import a private key / keystore JSON;
  export either form after re-entering the vault password; label, rename,
  remove; one wallet is *primary* and is the default for every command.
- **Balances and portfolio** — native ETH and every ERC-20 the wallet
  holds, priced through DexScreener (CoinGecko as fallback), with cost
  basis, realized (FIFO) and unrealized PnL, 24h change, gas spent, and
  allocation. Per wallet and across all wallets. Every number is read from
  the RPC; on Base, Blockscout is only asked *which* tokens a wallet holds,
  so a token received before the wallet was imported shows up without a
  full resync (Robinhood Chain's Blockscout refuses non-browser clients, so
  there the sweep and the token registry are the only sources). Clients
  read the ledger, never the chain: the sync
  loop (every `sync_interval_seconds`), every settled swap and an explicit
  `--refresh` (throttled) are what read the chain. A `balanceOf` the node
  fails to answer leaves the stored amount alone and marks that wallet/chain
  read `partial` (`failed` when the node was unreachable), which
  `wallet.balances` reports per chain and the CLI prints under the table.
- **Junk stays out of sight** — anyone can airdrop a token to any address.
  A token is hidden automatically when nobody lists it (not in the chain's
  CoinGecko registry), nobody trades it (no DexScreener pool with at least
  $1,000 behind it; an unreachable price source is not a verdict) and the
  wallet never acted on it (never sold, sent, swapped or quoted it). Hidden
  tokens keep their ledger rows — the balance is real — but are left out of
  balances, portfolio, history, the totals, the agent's view and the sync's
  every-30-seconds scan (they are re-read hourly and re-judged daily, so a
  real launch that gains a pool resurfaces on its own). The desktop shows
  "N junk tokens hidden" with a Show toggle and a per-token hide/keep; the
  CLI has `--hidden` and `agentos trade hide`/`unhide`. A user's choice is
  final, and quoting or swapping a hidden token shows it again.
- **History** — deposits, withdrawals, swaps (yours, the agent's, or
  external), approvals and gas, rebuilt from the chain itself: ERC-20
  `Transfer` logs plus native balance reconciliation. No indexer is needed
  for this; where one exists it only widens the set of tokens the sweep
  reads. A full resync drops and rebuilds the ledger.
- **Swaps** — quote, approve (a plain ERC-20 approval to the provider's
  spender: 0x's AllowanceHolder or Uniswap's proxy, no Permit2 signatures),
  sign and broadcast, then confirm from the receipt.
  A swap can target one wallet, several, or all of them as a batch; one
  wallet failing never blocks the others.
- **Agent trading** — the agent signs on its own within code-enforced limits
  (below). Orders above the per-order threshold wait for a human in the
  desktop app; the agent is told the outcome and decides what to do next.

## Chains

| Chain | Id | Explorer | Notes |
| --- | --- | --- | --- |
| Base | 8453 | basescan.org | Uniswap Universal Router 2.0 default |
| Robinhood Chain | 4663 | robinhoodchain.blockscout.com | Router 2.1.1 only; public RPC needs a `User-Agent` and has no archive data |

Only these two chains are enabled. Nothing moves funds between them: there
is no bridge, and an agent asked to bridge says so and runs nothing. Native
ETH is `0x0000…0000` in the API.
A swap into native ETH on an L2 may deliver **WETH** instead; the order then
reports `deliveredToken` and the app offers a one-click unwrap
(`trading.unwrap`, a `withdraw(uint256)` on the WETH contract).

## Swap providers

| Provider | Key | Notes |
| --- | --- | --- |
| `aggregator` (default) | none | AgentOS Aggregator at `https://agg.useagentos.dev`, an HTTP API in front of 0x Swap v2. One `GET /v1/quote` returns the price *and* the unsigned calldata, including the ERC-20 approval when one is needed. 20 bps integrator fee, reported in the quote and already deducted from the output. Quotes expire in 30 s upstream; the engine stops trusting them at 20 s. |
| `uniswap` | Uniswap Trading API key | Universal Router; `x-agent-info` attribution on every call. Quotes fresh for 30 s. |

The aggregator holds no key of its own and neither signs nor broadcasts:
reading it cannot move funds. Before anything is signed the engine checks
the one relation its calldata cannot hide — the approval's `spender` must be
the same contract the swap transaction is sent `to` (0x's AllowanceHolder) —
and refuses the quote outright otherwise. The approval is always for exactly
the order's amount, never unlimited.

The tokenised stocks on Robinhood Chain (29 of the 34 listed: AAPL, TSLA,
NVDA, SPY, …) cannot be routed in either direction, at any size, at any time
— 0x refuses them for legal reasons. That surfaces as
`trading.token_not_tradeable`, and retrying does not help — not by address
either. ETH, WETH and USDG trade normally there. Two more Robinhood Chain
facts: the engine has no native USD price there yet, so `--usd` may be
refused with `trading.unpriced` (size with `--amount`), and the bare symbol
`USDC` resolves to unverified lookalikes (`TOKEN_UNVERIFIED`) — use an
address the token search marks `verified: true`.

The provider is read from `trading.provider` on **every** call, so switching
takes effect immediately, no gateway restart. There is one canonical way to
change it: write the config key. The desktop does `config.patch
{trading: {provider: "uniswap"}}`; the CLI does `agentos trade provider
uniswap` (`config.set trading.provider`); `trading.setProvider {provider}`
is a thin RPC wrapper over the same `config.set` path.

Prices, balances and history never depend on the provider (DexScreener /
CoinGecko / your RPC node); only quoting and calldata do.

## Configuration

```toml
[trading]
enabled = true
provider = "aggregator"              # or "uniswap"
aggregator_base_url = "https://agg.useagentos.dev"
uniswap_api_key = ""                 # only for provider = "uniswap"; or set UNISWAP_API_KEY
uniswap_api_key_env = "UNISWAP_API_KEY"
approval_threshold_usd = 100.0       # agent orders above this wait for approval
daily_cap_usd = 1000.0               # per wallet, agent-initiated swaps only; 0 = agent swaps off
approval_ttl_seconds = 900           # unanswered approvals expire
agent_max_price_impact_pct = 5.0     # agent orders above this wait for approval even under the threshold
agent_max_slippage_pct = 5.0         # an agent asking for more slippage is refused
default_slippage_pct = 0.5           # omit to let the provider pick (auto slippage)
unlock_mode = "auto"                 # or "manual"
sync_interval_seconds = 30
price_ttl_seconds = 20

[trading.rpc_urls]
"8453" = "https://mainnet.base.org"  # optional overrides, by chain id or key
"4663" = "https://rpc.mainnet.chain.robinhood.com"
```

RPC resolution order per chain: `trading.rpc_urls`, then the environment
variables `RPC_BASE_URL` (8453) / `RPC_ROBINHOOD_URL` (4663), then the
public default. Every `[trading]` key is also an environment variable with
the `AGENTOS_TRADING_` prefix (`AGENTOS_TRADING_DAILY_CAP_USD=250`,
`AGENTOS_TRADING_PROVIDER=uniswap`). The API key is redacted in
`config.snapshot` and every public config view like any other `*_api_key`;
`rpc_urls` values are shown as scheme and host only (`https://base.drpc.org/…`)
because dRPC, Alchemy and Infura keys live in the path. Writing that
redacted form back through `config.apply` / `config.patch` keeps the stored
URL. `aggregator_base_url` must be `https`; `http` is accepted only for
`127.0.0.1`, `localhost` or `::1`.
Get a key from the Uniswap developer dashboard; the desktop's **Settings →
Trading** pane has a *Test key* button (`trading.probe`).

## Security model

- Keystores are standard Ethereum **keystore v3** JSON files under
  `~/.agentos/wallets/` (directory `0700`, files `0600`), all encrypted with
  one vault password. A random *verifier* keystore in `index.json` lets the
  engine check a password without touching any wallet key.
- **Unlock modes**, both OS-independent (no Keychain, no Electron store):
  - `auto` — the password sits in `~/.agentos/wallets/unlock.key` (`0600`)
    and the gateway unlocks at first use. This is what unattended agent
    missions need. Anyone who can read the directory can spend: same trust
    level as an SSH key without a passphrase.
  - `manual` — nothing on disk; unlock per gateway session from the app or
    `agentos wallet unlock`. Keys live only in the gateway's memory.
- Export (keystore or raw private key) and wallet removal always ask for
  the vault password, whatever the mode. Private keys never appear in logs,
  RPC responses (other than `wallet.export`), or the ledger.
- Before signing, the engine checks balances, simulates the transaction
  with `eth_call`, and validates what the provider returned: an approval
  must be a plain `approve(spender, amount)` on the token being sold, to a
  spender the provider is known to use, for no more than the order (or the
  conventional unlimited allowance); a swap must come from the signing
  wallet, on the order's chain, carrying exactly the order's native value
  (zero for an ERC-20 sale), and not be addressed to the wallet or the sold
  token itself. Anything else is refused unsigned.
- `~/.agentos/wallets/` (and any `unlock.key`) is a sandbox *sensitive
  path*: the agent's file tools cannot read it. The agent trades through
  the gateway, which is the only thing that needs the keys.
- Token metadata from third-party APIs is data, not instructions: the skill
  tells the agent to ignore anything in a token name that reads like a
  command, and genuine Robinhood Stock Tokens are preferred over lookalikes
  that reuse the same ticker.

## Who is the agent?

Guardrails are only worth anything if the gateway, not the client, decides
who is asking. `agentos.gateway.agent_surface` computes an *agent binding*
for every admitted connection from three signals, strongest first:

1. **An operator secret.** Two are accepted. The desktop app spawns the
   gateway and hands it a random secret (`AGENTOS_OPERATOR_SECRET_FILE`, a
   `0600` file the gateway deletes after reading; `AGENTOS_OPERATOR_SECRET`
   as a fallback, scrubbed from the environment). The gateway also writes
   its own secret to `~/.agentos/wallets/operator.secret` (`0600`, rotated
   at every boot); the CLI reads it automatically when `AGENTOS_AGENT_TOKEN`
   is not set and the gateway is local, and an agent's shell cannot read it
   (`~/.agentos/wallets` is a denied path). A connection presenting either
   at the handshake is the operator's, never an agent's. Against a remote
   gateway the CLI presents nothing and gets the agent's rules.
2. **An agent token.** When an agent turn spawns a shell — or the scheduler
   runs a `cron --script` job — a token is minted and passed to the child
   as `AGENTOS_AGENT_TOKEN`; the CLI presents it back and the connection is
   bound to that session and agent. A script job therefore trades under
   the agent's rules.
3. **An exec window.** While any agent shell is running (background
   processes included), a new connection that presents nothing is treated
   as the agent's. Unsetting variables gains nothing; a person who opens
   the CLI in that window gets the agent's rules, which fail safe (their
   order waits for approval instead of executing).
4. **Nothing.** A connection with no secret, no token and no open window
   is unbound and gets the agent's rules as well: a process an agent
   detached (`setsid`, `nohup`, `&`) that connects later gains nothing.

An agent-bound connection is `initiator: agent` whatever it declares, and
its orders are filed under the session the binding names. These RPCs are
**operator-only** and answer an agent with `trading.operator_required`:
`wallet.setup`, `unlock`, `lock`, `setUnlockMode`, `changePassword`,
`create`, `import`, `export`, `rename`, `remove`, `setPrimary`;
`trading.orders.approve`, `trading.orders.reject`, `trading.tokens.hide`,
`trading.unwrap`, `trading.lot.setCost`, `trading.probe` with an `apiKey`
(testing a key that is not in config) and `trading.sync` with `full`
(dropping and rebuilding the ledger). `config.set` / `config.patch` of
any `trading.*` key is refused from an agent as well (as an invalid request,
not with a `trading.*` code). So `agentos trade approve`,
`agentos wallet export` or `agentos config set trading.daily_cap_usd`
simply fail inside an agent turn. `wallet.status` and `trading.status`
answer an agent without `vaultPath`. A `note` on a swap, send or revoke is
stored as the approver will see it: control and bidi characters are
dropped, whitespace is collapsed, and it is cut at 240 characters.
`trading.swap` and `trading.send` take a `clientOrderId`: a repeat call with
the same id returns the existing order instead of placing a second one
(`agentos trade swap|send --client-id`). Swap targets and approval spenders
are pinned to the provider's known contracts (a quote naming another address
is refused, not signed); the gas limit is the provider's or the estimate
plus 20 %, and an order the wallet cannot fund (value plus gas at the quoted
fee) is refused before signing. `trading.rpc_urls` are shown host-only in
every public config view (`config.snapshot`, `agentos config get`) because
provider keys live in the URL path; `trading.aggregator_base_url` must be
`https` (plain `http` only for a loopback host). This is not a full sandbox (a same-user process can still read
files); it is the difference between a guardrail a prompt can talk its way
around and one it cannot.

## Guardrails (code-enforced, agent-initiated swaps only)

| Rule | Default | Outcome when hit |
| --- | --- | --- |
| Per-order threshold | 100 USD | order parks as `awaiting_approval`; desktop notifies you |
| Daily cap per wallet | 1,000 USD (local calendar day) | order is `rejected` outright, not queued; the cap is per wallet, so `--all-wallets` spends up to N caps; an order counts as max(in, out) in USD; `spentTodayUsd` counts confirmed spend plus orders still in flight, so a burst cannot race its own confirmations; **0 switches agent swaps off** (there is no "unlimited") |
| Price-impact ceiling | 5 % (`agent_max_price_impact_pct`) | order parks as `awaiting_approval` even under the USD threshold. "Price impact" here is the price vs reference (it includes the venue fee and feed skew), not pool depth alone |
| Slippage ceiling | 5 % (`agent_max_slippage_pct`) | quote or swap refused with `trading.slippage_too_high`; nothing is queued |
| Unpriced order | — | treated as above threshold (fails closed) |
| Approval TTL | 15 min | `expired`; agent is told |

Manual swaps from the app or CLI are your own decision: they never queue
and do not count toward the agent's cap; if the price moved more than twice
the slippage between the quote and the send they fail with
`trading.price_moved` so you re-quote with open eyes. `trading.swap` (and
`agentos trade swap --expected-out-raw … --min-out-raw …`) accepts the
quote's `expectedOutRaw` / `minOutRaw` so the fill is checked against the
quote you actually saw: it refuses with `trading.price_moved` when the fill
would be more than 2× the slippage worse. Approving an agent
order re-quotes it; if the market moved that much since you approved, it
goes back to `awaiting_approval` with reason `price moved since approval;
please re-approve` instead of executing. `trading.limits` and
`trading.status` report every limit (`approvalThresholdUsd`, `dailyCapUsd`,
`approvalTtlSeconds`, `agentMaxPriceImpactPct`, `agentMaxSlippagePct`) and
`spentTodayUsd`.

Every quote result and order carries `provider` (`aggregator` / `uniswap`),
`expiresAt` (about 20 s for an aggregator quote, 30 s for a Uniswap one) and
`warnings` (a clamped slippage, a route the venue could not fully simulate).

The aggregator publishes no price-impact figure and prices gas in ETH rather
than dollars, so the engine fills both in from its own price feed before the
guardrails read them — `agent_max_price_impact_pct` keeps biting on a thin
route whichever provider found it.

Order statuses: `quoted → awaiting_approval | submitted → confirmed |
failed`, with `approved`, `rejected` and `expired` in between (`expired` is
an approval nobody answered within the TTL). Rejection `reason`s: a text
starting `daily cap` from the cap, `user` or `user: <text>` from the
person; a failed order's reason is `<code>: <message>`. A `submitted` order
is never orphaned: after a gateway restart, or if its confirm task died,
the housekeeping loop picks it up and keeps waiting for the receipt
(`recover_submitted`); after 6 hours without one it is marked `failed`
("transaction never mined"). An approval transaction that is not mined
within the wait window fails the order with `trading.tx_pending` (retry
once it lands). Every change is broadcast on the gateway WebSocket
(`trading.changed`, `trading.approval.requested`,
`trading.order.finished`), so the desktop never polls for it.

## How the agent uses it

The bundled `wallet-trading` skill drives the `agentos wallet` and
`agentos trade` commands with `--json`. Typical missions:

- "Swap 50 USDC to ETH on Base" — `agentos trade swap --chain base --in USDC --out ETH --amount 50 --wait --wait-seconds 600 --json`; a parked order is waited out with `agentos trade order <id> --wait --wait-seconds 600 --json` (`--wait-seconds` without `--wait` returns at once).
- "DCA 20 USDC into ETH every day" — a cron job running the swap; each
  run is a separate order under the same limits, with its own
  `--client-id` (minute resolution: `dca-eth-$(date +%Y%m%dT%H%M)`).
- "Buy WETH if it drops 5%" — quote/price checks on a schedule, swap when the
  condition holds, report the tx hash and explorer link.
- "Rebalance wallets A and B" — `--wallet A --wallet B` or
  `--all-wallets`; each wallet reports its own result.

Amounts are always human units (`0.5`, `1000`), never wei. See
[`../cli.md`](../cli.md) for the full command reference.

## RPC surface

Every client goes through the same gateway methods:

| Method | What it does |
| --- | --- |
| `trading.status`, `trading.limits`, `trading.probe`, `trading.network` | readiness (no `vaultPath` for an agent), the guardrails and today's spend, provider reachability (with `apiKey`: operator-only), head block / gas / RPC latency per chain |
| `trading.setProvider` | switch the swap provider (a wrapper over `config.set trading.provider`) |
| `trading.tokens.search`, `trading.tokens.resolve`, `trading.tokens.hide` | search a chain's tokens; resolve one address to its metadata (symbol, decimals, verification, price); hide or show a token (operator-only) |
| `trading.quote`, `trading.swap`, `trading.send`, `trading.unwrap` | price a swap; place one (per wallet, several, or all); send a token to one or many recipients as one batch; unwrap WETH delivered by a swap (operator-only). `swap` and `send` take `clientOrderId` (idempotency: the same id returns the same order) and a `note` that is sanitised (control/bidi characters, 240 chars) |
| `trading.allowances.list`, `trading.allowances.revoke` | live ERC-20 allowances with exposure; `approve(spender, 0)` |
| `trading.orders.list`, `trading.orders.get`, `trading.orders.wait`, `trading.orders.batch` | orders (filter by status, wallet, `kind`); one order; block until it settles; every leg of a multisend by `batchId` |
| `trading.orders.approve`, `trading.orders.reject` | the user's decision on a parked order (operator-only) |
| `trading.decode` | explain a transaction hash or raw calldata |
| `trading.history`, `trading.portfolio`, `trading.chart`, `trading.sync`, `trading.lot.setCost` | the ledger; holdings with PnL; price history for a token (GeckoTerminal on Base, the engine's own snapshots on Robinhood Chain); re-read the chain (`full`, a rebuild, is operator-only); correct a lot's cost basis (operator-only) |

## Ledger and PnL

`~/.agentos/state/trading.sqlite` holds tokens, entries (history), FIFO
lots, realized PnL, orders, daily spend, sync cursors, balance cache, the
outcome of each wallet/chain read (`chain_reads`) and price snapshots. A
release that ships a ledger repair migration flags it: `agentos trade
status --json` (and `trading.status`) then carries `ledgerRepair` ("full
sync required"); run `agentos trade sync --full` once after upgrading and
the flag clears. Cost basis for a deposit is the token's USD price at that
block (CoinGecko range; spot price with `cost_basis_source = "approx"` when
history is unavailable). A holding's cost can be corrected by hand with
`trading.lot.setCost`. Selling consumes the oldest lots first; the
difference to proceeds is realized PnL. Gas is booked per entry and shown
separately in totals.

Charts come from GeckoTerminal on Base; on Robinhood Chain the engine
serves its own price snapshots (one per held token per sync).

## Related

- [`agentic-trading.md`](agentic-trading.md) — the Robinhood MCP and partner skills.
- [`../cli.md`](../cli.md) — `agentos wallet` / `agentos trade`.
- [`../approvals-and-permissions.md`](../approvals-and-permissions.md).
