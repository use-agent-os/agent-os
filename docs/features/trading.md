# Wallets and Trading

AgentOS can hold EVM wallets of its own and swap tokens on **Base** and
**Robinhood Chain** through the **AgentOS DEX**, which needs no API key.
The engine owns everything:
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
  A wallet made or imported without a label is named after its address:
  its first five characters and its last four (`0xAbC_1234`).
- **Bulk wallets** (desktop: *Create new › Bulk create* and *Import Wallets ›
  Bulk import*) — `wallet.createBatch` makes 1–30 wallets at once, each from
  its own 12-word BIP-39 phrase (the key is the phrase's first account,
  `m/44'/60'/0'/0/0`, so any wallet app that restores the phrase lands on the
  same address). The vault keeps the keys and never the phrases: the
  batch's keys and phrases wait in gateway memory for 15 minutes (forgotten
  on `wallet.lock`) and `wallet.exportBatch` hands them over once the
  operator confirms with the vault password, or with Touch ID in the
  desktop, which downloads them as `no,address,private_key,seed_phrase` CSV.
  `wallet.importBatch` imports up to 100 raw private keys (one per row in
  the app) and answers each row by its index — `imported`, `exists`,
  `invalid` (with why) or `skipped` — without echoing a key back.
  `wallet.removeBatch` (desktop: *Manage wallets › Select*) removes up to
  100 wallets under one vault-password check, all or none: an address the
  vault does not hold refuses the whole list. Each removed wallet's DCA
  mandates and triggers end with it, as with `wallet.remove`.
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
  A token that has never answered is not asked again on every pass, only on
  the hourly full read, a resync, or when it moves; the read stays `partial`
  until it answers.
  `trading.status` and `trading.portfolio` carry `syncing` and `syncKind`,
  why the pass in flight runs: `background` (the loop's own clock),
  `requested` (a resync, a wallet import, a `--refresh`) or `initial` (a
  wallet/chain swept for the first time, or rebuilt); `null` while none
  runs. The desktop shows "Syncing" for the last two only: with many
  wallets the background pass runs most of the time.
- **Junk stays out of sight** — anyone can airdrop a token to any address.
  A token is hidden automatically when nobody lists it (not in the chain's
  token lists), nobody trades it (no DexScreener pool with at least
  $1,000 behind it; an unreachable price source is not a verdict) and the
  wallet never acted on it (never sold, sent, swapped or quoted it). Hidden
  tokens keep their ledger rows — the balance is real — but are left out of
  balances, portfolio, history, the totals, the agent's view and the sync's
  every-60-seconds scan (they are re-read hourly and re-judged daily, so a
  real launch that gains a pool resurfaces on its own). The desktop shows
  "N junk tokens hidden" with a Show toggle and a per-token hide/keep; the
  CLI has `--hidden` and `agentos trade hide`/`unhide`. A user's choice is
  final, and quoting or swapping a hidden token shows it again.
- **History** — deposits, withdrawals, swaps (yours, the agent's, or
  external), approvals and gas, rebuilt from the chain itself: ERC-20
  `Transfer` logs plus native balance reconciliation. After downtime the
  sweep catches up window by window (100,000 blocks on Base, 30,000 on
  Robinhood Chain) and keeps its place after each one, so a catch-up that is
  interrupted resumes where it stopped instead of starting over. No indexer
  is needed for this; where one exists it only widens the set of tokens the
  sweep reads. A full resync drops and rebuilds the ledger.
- **Swaps** — quote, approve (a plain ERC-20 approval to the provider's
  spender: the AgentOS Router or 0x's AllowanceHolder, no Permit2
  signatures: the engine signs transactions only, never typed data for a
  swap), sign and broadcast, then confirm from the receipt.
  A swap can target one wallet, several, or all of them as a batch; one
  wallet failing never blocks the others.
- **Agent trading** — the agent signs on its own within code-enforced limits
  (below). Orders above the per-order threshold wait for a human in the
  desktop app; the agent is told the outcome and decides what to do next.
- **Liquidity book** — every liquidity position the vault's wallets hold,
  across Uniswap V2/V3/V4, Aero and Aerodrome on Base and Robinhood Chain,
  staked positions and their rewards included. Cached by the engine and read
  only while someone looks (`docs/lp-book.md`); the desktop's Liquidity tab
  shows it.
- **Uniswap V4 liquidity** — read a token's pools, their liquidity ranges
  and the vault's positions (cards in the chat, `docs/lp-cards.md`), and
  change them: collect fees, remove liquidity (100 % burns the NFT), add
  liquidity (mint, or top up a position) on Base and Robinhood Chain. Every
  LP write is an order that **always waits for approval**, whoever asked;
  on approval the engine re-checks the pool against the approved bounds,
  approves exactly what the call may pull (ERC-20 → Permit2 → PositionManager,
  30-minute Permit2 expiry, never unlimited), sends `modifyLiquidities` and
  books the receipt as `lp_collect` / `lp_remove` / `lp_add` entries. Nothing
  is swapped to make up a missing side. Contract: [`../lp-write.md`](../lp-write.md).
- **DCA mandates** — a recurring buy the engine owns: `agentos trade dca
  create ETH --usd 10 --every 1d --cap 300`. The cap, schedule, `--runs` and
  `--max-price` guards are ledger rows, enforced by the engine on every tick;
  each buy is an ordinary swap order under the agent guardrails (a buy under
  the threshold executes on its own, a larger one parks). An agent may only
  *propose* a mandate (`awaiting_approval`); the user approves it once from
  the chat card, the Missions panel or `agentos trade dca approve`. The card
  in the chat shows the next buy countdown, spent of cap, average buy price
  vs now and a buys chart, with Pause / Buy now / Stop on the desktop.
  Contract: [`../dca.md`](../dca.md).

## Chains

| Chain | Id | Explorer | Notes |
| --- | --- | --- | --- |
| Base | 8453 | basescan.org | Uniswap Universal Router 2.0 default |
| Robinhood Chain | 4663 | robin.etherscan.io | Router 2.1.1 only; public RPC needs a `User-Agent` and has no archive data |

Only these two chains are enabled. Funds move between them only through
`agentos trade bridge` (the AgentOS DEX, over Relay or Across): ETH↔ETH and
USDC↔USDG, both directions; a
bridge from an agent always waits for approval, and the order is
`confirmed` only once the fill has landed on the destination chain
([`../bridge.md`](../bridge.md)). Native ETH is `0x0000…0000` in the API.
A swap into native ETH on an L2 may deliver **WETH** instead; the order then
reports `deliveredToken` and the app offers a one-click unwrap
(`trading.unwrap`, a `withdraw(uint256)` on the WETH contract).

## Swap provider

There is one: the AgentOS DEX (`aggregator`), at
`https://agg.useagentos.dev`. It needs no API key. One `GET /v1/quote`
returns the price *and* the unsigned calldata, including the ERC-20 approval
when one is needed. It quotes two liquidity sources, 0x Swap v2 (the default)
and the Uniswap Trading API (`source=uniswap`), both executed through the
same Router. A 20 bps integrator fee is reported in the quote and already
deducted from the output. Quotes expire in 30 s upstream; the engine stops
trusting them at 20 s.

The Uniswap Trading API used to be a second provider, selected with
`trading.provider = "uniswap"` and a `trading.uniswap_api_key`. It was
removed in 2026-11: `trading.provider`, `trading.uniswap_api_key` and
`trading.uniswap_api_key_env` are dropped from an existing config at boot
(a backup is written first), `agentos trade provider` and the
`trading.setProvider` RPC are gone, and an order row that still records
`provider: "uniswap"` is priced and built by the AgentOS DEX.

The aggregator holds no key of its own and neither signs nor broadcasts:
reading it cannot move funds. Before anything is signed the engine checks
the one relation its calldata cannot hide — the approval's `spender` must be
the same contract the swap transaction is sent `to`, and that contract must be
one the engine pins itself: the AgentOS Router
(`0x4cf93bac8920b1efc92b0d2c84dcbe56802e530c`, the same immutable contract on
Base and Robinhood Chain, which the gateway quotes through in contract mode)
or 0x's AllowanceHolder (`0x0000000000001ff3684f28c67538d4d072c22734`, when
it quotes 0x directly), or one the operator added with
`trading.aggregator_trusted_spenders` — and refuses the quote outright
otherwise. The approval is always for exactly
the order's amount, never unlimited.

The tokenised stocks (AAPL, TSLA, NVDA, SPY, … on Robinhood Chain; AAPLc,
TSLAc, … on Base) are refused by 0x in either direction, at any size, at any
time, for legal reasons (`trading.token_not_tradeable`). Uniswap trades
them, and the AgentOS DEX carries Uniswap as a second source, so the engine
names it per pair: when either side is a tokenised stock (a row of the
chain's stock list, or a name ending `• Robinhood Token`), the quote, the
swap, DCA runs and trigger fires ask the DEX with `source=uniswap`. No
Uniswap API key is needed. A `token_not_tradeable` the flag missed is asked
once more from the Uniswap source. If that source cannot take the pair, the
DEX's own error is returned. ETH, WETH and USDG trade
normally there. Two more Robinhood Chain
facts: the engine has no native USD price there yet, so `--usd` may be
refused with `trading.unpriced` (size with `--amount`), and the bare symbol
`USDC` resolves to unverified lookalikes (`TOKEN_UNVERIFIED`) — use an
address the token search marks `verified: true`.

Prices, balances and history never depend on the provider (DexScreener /
CoinGecko / your RPC node); only quoting and calldata do.

Which tokens are `verified`, and which are tokenised stocks, comes from
AgentOS's own
[token lists](https://github.com/use-agent-os/agent-os-token-list), the same
ones the AgentOS DEX uses: `base_tokens.json` and
`base_stock_tokens.json` on Base, `robinhood_tokens.json` and
`robinhood_stock_tokens.json` on Robinhood Chain. The gateway downloads them
on first use and keeps them in memory. Each list is re-checked every 15
minutes with its ETag, so an unchanged list costs a `304`, and a download
replaces the held copy only when its version is newer: a new release of a
list is picked up without a restart, and never rolled back. A list that
cannot be reached is asked again after 2, 5 and 15 seconds, then once a
minute, while the held copy keeps answering. A token outside the lists is
still found by address or through DexScreener, and is shown as unverified.

## Configuration

```toml
[trading]
enabled = true
aggregator_base_url = "https://agg.useagentos.dev"
aggregator_trusted_spenders = []     # extra aggregator spender/target contracts, on top of the built-in pins
approval_threshold_usd = 100.0       # agent orders above this wait for approval
daily_cap_usd = 1000.0               # per wallet, agent-initiated swaps only; 0 = agent swaps off
approval_ttl_seconds = 900           # unanswered approvals expire
agent_max_price_impact_pct = 5.0     # agent orders above this wait for approval even under the threshold
agent_max_slippage_pct = 5.0         # an agent asking for more slippage is refused
default_slippage_pct = 0.5           # omit to let the provider pick (auto slippage)
unlock_mode = "auto"                 # or "manual": the vault never stores its password, and auto is refused
sync_interval_seconds = 60
price_ttl_seconds = 20

[trading.rpc_urls]
"8453" = "https://mainnet.base.org"  # optional overrides, by chain id or key
"4663" = "https://rpc.mainnet.chain.robinhood.com"
```

RPC resolution order per chain: `trading.rpc_urls`, then the environment
variables `RPC_BASE_URL` (8453) / `RPC_ROBINHOOD_URL` (4663), then the
public default. A custom RPC serves everything that needs a fresh answer (orders,
nonces, receipts, fees, the sync's block height, logs and native balance). The
wallet sync's bulk ERC-20 balance read goes to the chain's public node first, as
one Multicall3 call per wallet, and falls back to the custom RPC when the public
node does not answer.

A change to `trading.rpc_urls` the gateway has applied (`agentos config set`,
the desktop's Settings, `agentos config reload` after a file edit) or to the
environment fallback takes effect on the engine's next request, with no restart;
work already in flight (an order waiting for its receipt, an allowance scan)
finishes on the old endpoint. `trading.status` reports per chain what the engine
has sent since the gateway started (`rpcUsage`: requests, JSON-RPC calls by
method, failed requests; `rpc` is the configured endpoint, `read` the public
node the bulk balance read tries first), and the gateway logs one
`trading.rpc_usage` line per chain about once an hour with what was sent since
the last. A request counts as failed when it got no answer (timeout,
connection error), an HTTP status of 400 or above, or a body that is not JSON;
a JSON-RPC error inside an HTTP 200 is an answer and is not counted. An
endpoint that refuses the credentials (HTTP 401/403) twice in a row is left
alone by the background sync for 1, 2, 5, then every 15 minutes: its
wallets read `failed` with the refusal as the reason, a resync still tries at
once, and the first answer or a new URL ends the pause. Timeouts, rate limits
(HTTP 429) and server errors never pause the sync.

Every `[trading]` key is also an environment variable with
the `AGENTOS_TRADING_` prefix (`AGENTOS_TRADING_DAILY_CAP_USD=250`). In
`config.snapshot` and every public config view,
`rpc_urls` values are shown as scheme and host only (`https://base.drpc.org/…`)
because dRPC, Alchemy and Infura keys live in the path. Writing that
redacted form back through `config.apply` / `config.patch` keeps the stored
URL. `aggregator_base_url` must be `https`; `http` is accepted only for
`127.0.0.1`, `localhost` or `::1`.

`aggregator_trusted_spenders` adds contracts an aggregator quote may name as
the approval spender and send the swap or the bridge call to, on top of the
ones the engine pins itself (the AgentOS Router, 0x's AllowanceHolder and the
AgentOS bridge contract). It is for when the
gateway moves to a contract this build does not know yet: rather than waiting
for a release, the operator verifies the new address and adds it:

```bash
agentos config set trading.aggregator_trusted_spenders '["0x…"]'
```

It only adds; the built-in pins always stay trusted, and the approval's
spender must still be the contract the swap is sent to. Each entry must be a
0x address (a mixed-case one must pass its EIP-55 checksum; the zero address
is refused), and is stored lower-cased. It takes effect on the next quote,
without a restart. Like every `trading.*` key it is operator-only: an agent
cannot add a spender. Trust only a contract you have checked — a spender the
wallet approves can move up to the approved amount of that token.
Get a key from the Uniswap developer dashboard; the desktop's **Settings →
Trading** pane has a *Test key* button (`trading.probe`).

## Security model

- Keystores are standard Ethereum **keystore v3** JSON files under
  `~/.agentos/wallets/` (directory `0700`, files `0600`), all encrypted with
  one vault password. A random *verifier* keystore in `index.json` lets the
  engine check a password without touching any wallet key.
- **Unlock modes**, both OS-independent (no Keychain, no Electron store):
  - `auto` — the password sits in `~/.agentos/wallets/unlock.key` (`0600`),
    sealed with AES-256-GCM under a key kept **outside** the state directory
    (`AGENTOS_VAULT_KEK`, else Windows DPAPI, else a `0600` file under the
    OS state dir; `AGENTOS_VAULT_KEK_DIR` relocates it, and a key already
    stored in the default directory is moved on the next auto unlock; the
    container's `compose.yaml` puts it on its own volume), and the gateway
    unlocks at first use. This is what unattended agent missions need. The
    file and its key are both needed, but a process running code as this OS
    user can read both, exactly as the gateway does — `agentos sandbox` or
    `manual` unlock is the real boundary against that. A legacy plain-text
    `unlock.key` still works and is sealed in place on first unlock. The key
    is injected with `[trading] unlock_mode = "auto"` the default.
  - `manual` — nothing on disk; unlock per gateway session from the app or
    `agentos wallet unlock`. Keys live only in the gateway's memory.
- **Lock** (`agentos wallet lock`, `wallet.lock`) holds in both modes, and
  across gateway restarts, until you unlock with the password: `unlock.key`
  is not read while it holds. Nothing signs: agent swaps fail
  `wallet.locked`, due DCA buys and armed triggers wait (no failed runs,
  no auto-pause; alerts still ring), and approving an order is refused with
  the order left parked. `wallet.status` says `lockedByOperator`.
- Export (keystore or raw private key) and wallet removal always ask for
  the vault password, whatever the mode. Private keys never appear in logs,
  RPC responses (other than `wallet.export` and `wallet.exportBatch`), or
  the ledger. `wallet.exportBatch` serves only a batch `wallet.createBatch`
  made in the last 15 minutes, only while the vault is unlocked, and only
  with the vault password or `touchId: true` — the desktop sends that after
  its own Touch ID prompt, the same renderer-side gate that stands in front
  of every vault write. An empty password is refused, not read as either.
- Removing a wallet stops every DCA mandate, trigger and bracket bound to it
  (`wallet removed`) and rejects what they parked. Importing the same key
  again is a new wallet as far as approvals go: nothing comes back.
- **Ledger seals.** The engine does not sign what it merely reads back from
  `state/trading.sqlite`. Every order is sealed when the engine writes it,
  and every mandate, trigger and bracket when you approve it (or create it
  yourself): an HMAC over the terms that decide what is signed (wallet,
  chain, tokens, amounts, recipient, slippage and floors, the bridge or LP
  plan, batch, expiry; for a mandate or trigger its size, price condition
  and validity), bound to the wallet's incarnation. The key is derived from
  the vault's password-encrypted secret and exists only in gateway memory
  while the vault is unlocked, so approving or creating a mandate, trigger
  or bracket needs an unlocked vault. The seal is checked before every
  signature and before every DCA buy or trigger fire; a row changed outside
  the engine is refused (`trading.integrity`; a mandate or trigger is
  stopped), and the gateway broadcasts `trading.integrity.alert`. Seals
  survive restarts and password changes; the first unlock after the upgrade
  that introduced them seals what was already live.
- Before signing, the engine checks balances, simulates the transaction
  with `eth_call`, and validates what the provider returned: an approval
  must be a plain `approve(spender, amount)` on the token being sold, to a
  spender the provider is known to use, for no more than the order (or the
  conventional unlimited allowance); a swap must come from the signing
  wallet, on the order's chain, carrying exactly the order's native value
  (zero for an ERC-20 sale), and not be addressed to the wallet or the sold
  token itself. Anything else is refused unsigned.
- `~/.agentos/wallets/` (and any `unlock.key`) is a sandbox *sensitive
  path*: the agent's file tools cannot read it, and a recursive read, copy
  or archive of a *parent* directory (`grep -r ~/.agentos`, `cp -r ~`,
  `tar` of the state root, the `execute_code` equivalents) is refused too,
  so the vault cannot be swept up from above. The agent trades through the
  gateway, which is the only thing that needs the keys. So is the
  ledger, `state/trading.sqlite` with its `-wal`/`-shm`/`-journal` files
  (file mode `0600`), any copy named `trading.sqlite`, and the pre-upgrade
  snapshots under `state/snapshots`: the agent reads its orders and history
  through the gateway, never the file.
- Token metadata from third-party APIs is data, not instructions: the skill
  tells the agent to ignore anything in a token name that reads like a
  command, and genuine Robinhood Stock Tokens are preferred over lookalikes
  that reuse the same ticker.

## Who is the agent?

Guardrails are only worth anything if the gateway, not the client, decides
who is asking. `agentos.gateway.agent_surface` computes an *agent binding*
for every admitted connection. One rule orders the signals: **a sign that an
agent is asking outranks any proof an agent could have read.** Strongest
first:

1. **An agent token.** When an agent turn spawns a shell — or the scheduler
   runs a `cron --script` job — a token is minted and passed to the child
   as `AGENTOS_AGENT_TOKEN`; the CLI presents it back and the connection is
   bound to that session and agent. A script job therefore trades under
   the agent's rules. A connection that presents *any* agent token — one
   this gateway does not know (minted before a restart, or made up)
   included — is an agent's, and an operator secret next to it is ignored.
2. **The peer process (Linux).** The gateway looks up which process holds
   the client end of a local connection. One the gateway spawned, directly
   or not — an agent's shell, a background process, `execute_code`, an ACP
   harness, an MCP server — is an agent's whatever it presents.
3. **The desktop's operator secret.** The desktop app spawns the gateway and
   hands it a random secret (`AGENTOS_OPERATOR_SECRET_FILE`, a `0600` file
   the gateway deletes after reading; `AGENTOS_OPERATOR_SECRET` as a
   fallback, scrubbed from the environment). It never sits on disk or in
   the environment afterwards, so the app stays the operator even while an
   agent shell runs.
4. **From another machine, the gateway token.** Neither secret reaches a
   gateway on another host (a container behind a reverse proxy), so a
   Control WebSocket that authenticated with the gateway's bearer token
   from another machine is the operator's too: the desktop app in External
   mode, the web console and a remote CLI can create, import and unlock
   wallets and approve orders. The peer is the socket's own address — an
   `X-Forwarded-For` header never counts — and it must be neither loopback
   nor one of the gateway host's own addresses: a process on that host
   which read the token (from `config.toml` or the environment) and dialled
   the LAN or container address is still on that host. HTTP RPC
   (`POST /api/v1/rpc`) does not apply this rule. The gateway also keeps
   its token out of every process an agent runs, whichever variable it came
   from (`AGENTOS_AUTH_TOKEN`, `AGENTOS_GATEWAY_AUTH__TOKEN`, the hooks
   token), and drops it from its own environment after boot.
5. **An exec window.** While any agent shell is running (background
   processes and `execute_code` runs included), a new connection is the
   agent's unless one of the signals above says otherwise — *including one
   that presents the operator file below*. A person who opens the CLI in
   that window gets the agent's rules, which fail safe: their order waits
   for approval instead of executing, and an approval they try to give is
   refused with a reason. Approve in the desktop app, or retry once the
   agent's shell has finished.
6. **The operator file.** The gateway writes its own secret to
   `<state dir>/wallets/operator.secret` (`0600`, rotated at every boot;
   the state dir is `~/.agentos` unless `AGENTOS_STATE_DIR` moves it). The
   CLI reads it only outside an agent's session: never when
   `AGENTOS_AGENT_TOKEN`, `AGENTOS_SESSION_KEY`, `AGENTOS_AGENT` or
   `AGENTOS_ACP_LEASE` is present (even empty), and never when a running
   gateway is among its ancestors, so `env -u` / `env -i` gain nothing. It
   counts on the WebSocket handshake only. The wallets directory, wherever
   the state dir is, is a sensitive path for an agent's shell and file
   tools.
7. **Nothing.** A connection with no secret, no token and no open window
   is unbound and gets the agent's rules as well: a process an agent
   detached (`setsid`, `nohup`, `&`) that connects later gains nothing.

This is a guardrail, not a sandbox: on a host without the OS sandbox, a
process an agent detached and that was re-parented away from the gateway can
still read the operator file once no agent shell is running. Turn the
sandbox on (`agentos sandbox`) where that matters.

An agent-bound connection is `initiator: agent` whatever it declares, and
its orders are filed under the session the binding names. These RPCs are
**operator-only** and answer an agent with `trading.operator_required`:
`wallet.setup`, `unlock`, `lock`, `setUnlockMode`, `changePassword`,
`create`, `createBatch`, `import`, `importBatch`, `export`, `exportBatch`,
`rename`, `remove`, `setPrimary`;
`trading.orders.approve`, `trading.orders.reject`, `trading.tokens.hide`,
`trading.unwrap`, `trading.lot.setCost`, `trading.probe` with an `apiKey`
(testing a key that is not in config) and `trading.sync` with `full`
(dropping and rebuilding the ledger). Every config write (`config.set`,
`config.patch`, `config.apply`, onboarding) of any `trading.*` key is refused
from an agent as well (as an invalid request, not with a `trading.*` code),
and an edit of the config file itself never changes the trading settings of
the running gateway: the watcher holds it until the user runs
`agentos config reload` or restarts. So `agentos trade approve`,
`agentos wallet export` or `agentos config set trading.daily_cap_usd`
simply fail inside an agent turn. `wallet.status` and `trading.status`
answer an agent without `vaultPath`. A `note` on a swap, send or revoke is
stored as the approver will see it: control and bidi characters are
dropped, whitespace is collapsed, and it is cut at 240 characters. Token
symbols and names (whatever a contract's `symbol()`/`name()` or a feed
returns) are stored the same way, without control, bidi or zero-width
characters, and a failed order never keeps the caller's unparsed amount.
`agentos trade` and `agentos wallet` show any such character left in what
they print as a visible escape (`\x1b`, `\u202e`), never send it to your
terminal; `--json` output `\u`-escapes C1, bidi and invisible characters
(the parsed data is unchanged).
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

## Guardrails

Code-enforced, and applied to agent-initiated swaps only.

| Rule | Default | Outcome when hit |
| --- | --- | --- |
| Per-order threshold | 100 USD | order parks as `awaiting_approval`; desktop notifies you |
| Daily cap per wallet | 1,000 USD (local calendar day) | order is `rejected` outright, not queued; the cap is per wallet, so `--all-wallets` spends up to N caps; an order counts as max(in, out) in USD — but only while the sell side has a feed price; `spentTodayUsd` counts confirmed spend plus orders still in flight, so a burst cannot race its own confirmations; **0 switches agent swaps off** (there is no "unlimited") |
| Price-impact ceiling | 5 % (`agent_max_price_impact_pct`) | order parks as `awaiting_approval` even under the USD threshold. "Price impact" here is the price vs reference (it includes the venue fee and feed skew), not pool depth alone. A provider's own figure (Uniswap's `priceImpact`) is cross-checked against the engine's feed and the larger one counts; when the feed cannot price a side the impact is unknown and the order parks, whichever provider quoted it |
| Slippage ceiling | 5 % (`agent_max_slippage_pct`) | quote or swap refused with `trading.slippage_too_high`; nothing is queued |
| Unpriced order | — | treated as above threshold (fails closed). An agent swap whose **sell** side has no usable feed price (none, zero or non-finite) is unpriced even when the buy side has one: the token you receive never sizes the order on its own. The order shows its value as unknown, not $0 |
| Approval TTL | 15 min | `expired`; agent is told |
| LP writes (`trade lp collect/remove/add`) | — | **always** park as `awaiting_approval`, from the agent and from you; an agent's `add` counts toward the daily cap (over it: `rejected`), `collect`/`remove` never do; on approval a pool that moved past the approved bounds fails the order with `trading.price_moved` |
| Bridges (`trade bridge create`) | — | judged like a send: an agent's **always** parks as `awaiting_approval` and counts toward the daily cap (over it, or with the cap at 0: `rejected`; unpriced: waits); on approval the same bridge is quoted again, and an answer below the approved floors (1 %) sends the order back for approval (a manual one fails `trading.price_moved`) |

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
`trading.order.finished`), so the desktop never polls for it. A row that
failed its seal is announced as `trading.integrity.alert` (`kind` order /
mandate / trigger, `id`, `verdict` missing / mismatch, `wallet`, `message`).

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
| `trading.status`, `trading.limits`, `trading.probe`, `trading.network` | readiness (no `vaultPath` for an agent; per chain `rpcUsage`, what the engine sent that chain's RPC since start), the guardrails and today's spend, whether the AgentOS DEX is reachable, head block / gas / RPC latency per chain |
| `trading.tokens.search`, `trading.tokens.resolve`, `trading.tokens.hide` | search a chain's tokens; resolve one address to its metadata (symbol, decimals, verification, price); hide or show a token (operator-only) |
| `trading.quote`, `trading.swap`, `trading.send`, `trading.unwrap` | price a swap; place one (per wallet, several, or all); send a token to one or many recipients as one batch; unwrap WETH delivered by a swap (operator-only). `swap` and `send` take `clientOrderId` (idempotency: the same id returns the same order) and a `note` that is sanitised (control/bidi characters, 240 chars) |
| `trading.allowances.list`, `trading.allowances.revoke` | live ERC-20 allowances with exposure; `approve(spender, 0)`. Every listed amount is read live; the wallet's Approval logs, which find the pairs, are re-read at most once a minute per wallet and chain (`full` rescans at once, and an approval the engine sent shows at once). `trading.changed` (`reason: "allowances"`) goes out when a pair was added or a catch-up ended |
| `trading.orders.list`, `trading.orders.get`, `trading.orders.wait`, `trading.orders.batch` | orders (filter by status, wallet, `kind`); one order; block until it settles; every leg of a multisend by `batchId`. An order awaiting approval carries `approval`: what one approve releases (`orderIds`, `legCount`; for a batch leg also `totalRaw`/`total`/`totalUsd` and `unpriced`). A single order's `approval.digest` is on the order itself; a batch's only on `trading.orders.batch` (`approval`, with every leg's `recipient` and amount) |
| `trading.orders.approve`, `trading.orders.reject` | the user's decision on a parked order (operator-only). `approve` takes `approvalDigest`, the `approval.digest` the order (or its batch) showed: **required for a multisend** (without it: `trading.approval_binding_required`; one approve runs every awaiting leg) and checked when given for a single order; a digest that no longer matches — a leg changed, added or dropped since it was shown — is refused with `trading.approval_changed`. The digest covers chain, wallet, tokens, raw amounts, recipients, provider, slippage, the approved floor, the plan and the expiry of every leg; the engine holds each leg to it again where the transaction is built and signed, and a send or revoke to its exact `to`/`value`/`data`, so a parked order changed after the approval is failed, not signed |
| `trading.decode` | explain a transaction hash or raw calldata |
| `trading.lp.pool`, `trading.lp.ranges`, `trading.lp.position`, `trading.lp.positions` | Uniswap V4 read-outs (read-only, allowed for an agent): a token's deepest pool, its liquidity distribution, one position NFT, every position of the vault's (or given) wallets; each echoes `request: {kind, params}` so a card can re-read itself. Payloads: [`../lp-cards.md`](../lp-cards.md) |
| `trading.lp.book` | every LP position of the vault's wallets across Uniswap V2/V3/V4, Aero and Aerodrome, from the engine's cache (read-only, allowed for an agent). Params: `wallet`, `chainId`, `refresh`. Answers at once and refreshes behind the answer when the cache is stale; `trading.changed` (`reason: "lp"`) says when. Contract: [`../lp-book.md`](../lp-book.md) |
| `trading.lp.collect`, `trading.lp.remove`, `trading.lp.add` | Uniswap V4 writes (allowed for an agent: they only create an order, which always parks for approval). Params mirror the CLI in camelCase — `chainId`, `tokenId`, `pct`, `slippagePct`, `token`, `usd`, `amountBase`, `amountQuote`, `range`, `toPosition`, `wallet`, `note`, `clientOrderId`; the answer is `{"order": …}` with `plan`. Contract: [`../lp-write.md`](../lp-write.md) |
| `trading.bridge.quote`, `trading.bridge.create`, `trading.bridge.status`, `trading.bridge.routes` | bridge ETH or USDC↔USDG between Base and Robinhood Chain through the AgentOS DEX (allowed for an agent: `create` only creates an order, which always parks for approval). `chainId`/`chain` is the origin, `toChainId`/`toChain` the destination, `token`, exactly one of `amount` / `amountUsd` / `amountPct`, `wallet`, `note`, `clientOrderId`, `wait`; `status` takes `orderId` or the bridge's `requestId`. The order stays `submitted` until the fill lands, then `confirmed`. Contract: [`../bridge.md`](../bridge.md) |
| `trading.dca.create`, `trading.dca.get`, `trading.dca.list` | DCA mandates (agent-callable; an agent's `create` parks as `awaiting_approval`): propose a recurring buy, read one, list live ones (`all` for finished). Every answer is a full card payload with `request` echoed. Contract: [`../dca.md`](../dca.md) |
| `trading.dca.approve`, `trading.dca.reject`, `trading.dca.pause`, `trading.dca.resume`, `trading.dca.stop`, `trading.dca.run`, `trading.dca.update` | the user's decisions on a mandate (operator-only): start it, pause/resume, stop for good, buy now, change cap / size / cadence / max price |
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
separately in totals. A bridge is two entries under one order: a
`withdraw` on the origin chain when the deposit mines, and a `deposit` on
the destination chain when the bridge's fill lands.

Charts come from GeckoTerminal on Base; on Robinhood Chain the engine
serves its own price snapshots (one per held token per sync).

## Related

- [`agentic-trading.md`](agentic-trading.md) — the Robinhood MCP and partner skills.
- [`../cli.md`](../cli.md) — `agentos wallet` / `agentos trade`.
- [`../approvals-and-permissions.md`](../approvals-and-permissions.md).
