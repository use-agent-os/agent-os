# Bridge

The trading engine moves ETH and the USD stable between its two chains
through the **AgentOS DEX** (`https://agg.useagentos.dev`, the same service
the swaps use). Its `GET /v1/bridge/quote` asks two bridges at once,
[Relay](https://docs.relay.link) and [Across](https://docs.across.to), and
answers with the offer that delivers more; `GET /v1/bridge/status` says how
far a transfer has got. A quote for a wallet carries one unsigned
transaction: a call to the `AgentOSBridge` contract
(`0xe7b13ae215b1aa7b5f42ac0cafaffdd54441bc20`, the same address on both
chains), which pulls the amount from the wallet, keeps the AgentOS fee
(5 bps) and deposits the rest into the chosen bridge on the wallet's behalf.
The service holds no key and never broadcasts: funds move only when the
vault signs that call. A bridge runs through the
**same order pipeline as swaps and sends**: an order row, guardrails,
`awaiting_approval`, the operator approves (desk ApprovalCard or
`agentos trade approve`), the engine signs with the vault, broadcasts, waits
for the receipt, follows the fill on the other chain and settles both legs
into the ledger.

Routes (`agentos.trading.bridge.CHAIN_BRIDGE_TOKENS`): a token bridges to
the token on the other chain with the same asset, so the table is
per-chain, not per-pair.

| Direction | Asset | Origin token | Destination token |
| --- | --- | --- | --- |
| Base → Robinhood Chain | ETH | ETH | ETH |
| Base → Robinhood Chain | USD | USDC `0x8335…2913` | USDG `0x5fc5…d168` |
| Robinhood Chain → Base | ETH | ETH | ETH |
| Robinhood Chain → Base | USD | USDG `0x5fc5…d168` | USDC `0x8335…2913` |

Anything else — another token, another chain, the same chain twice — is
`trading.bridge.unsupported_route`, with `details.supportedRoutes`.

## Policy

- **An agent's bridge always parks for approval**, whatever the amount
  (`guardrails.evaluate_bridge` = `evaluate_transfer`: a bridge leaves the
  chain for good, like a send). It counts toward the agent's daily cap
  (origin notional in USD, in flight and once confirmed); over the cap, or
  with the cap at 0, it is `rejected`; unpriced, it waits rather than
  slipping past the cap. The notional comes from the engine's own price
  feed: the origin chain's price of the asset, else the destination chain's
  price of the same asset. Nothing the bridge says sizes the order. A manual bridge runs at once.
- **Which bridge.** The gateway picks whichever of Relay and Across expects
  to deliver more (Relay on a tie); the quote names it (`provider`,
  `providerLabel`) and lists both under `routes`. Across does not promise a
  refund on some routes once a transfer has started: the quote then carries
  `refundGuaranteed: false`, and the desktop, the CLI and the agent say so
  before the user confirms.
- **Two floors.** A bridge expects one amount (`expectedOut`) and commits to
  a little less (the gateway's `minBuyAmount`, 2–3 % under on Relay). The
  order's `minOut` is that commitment less the re-quote tolerance
  (`BRIDGE_TOLERANCE_PCT`, 1 %); a quote whose commitment sits more than
  `BRIDGE_MAX_COMMIT_GAP_PCT` (5 %) under what it expects is refused.
- **Re-quote before signing.** The quote the user approved is a promise
  about the price only. At execution the gateway is asked again, pinned to
  the bridge that was shown (`source`); an answer that expects less than the
  approved floor (1 % under the approved expectation) or commits to less than
  `minOut` sends an agent's order back to `awaiting_approval` with the new
  numbers, and fails a manual one with `trading.price_moved`. A pinned bridge
  that no longer has a route fails the order (`trading.bridge.no_route`)
  rather than switching to the other one. Once signed, the order's `minOut`
  is the commitment of the quote that was executed.
- **What is signed is pinned and decoded, not read from the response.** The
  call must go `to` a bridge contract in `TRUSTED_BRIDGES` (plus any the
  operator added with `trading.aggregator_trusted_spenders`), on the origin
  chain, with exactly the order's value when native and none otherwise. Its
  calldata must be `bridge(BridgeParams)` and is decoded field by field: the
  bridge named is the quote's, the token and gross amount are the order's,
  the net amount is the order less a fee of at most `BRIDGE_MAX_FEE_BPS`
  (1 %), the destination chain is the route's, the recipient is the wallet,
  the deadline is ahead and no more than an hour out (`bridge_transaction`,
  then `_check_bridge_tx`). Anything else is refused unsigned
  (`trading.bridge.unsupported_step`, `trading.tx_failed`).
- **The approval is the engine's own.** The gateway hands out no approval
  calldata. When the allowance to the bridge contract is short, the engine
  encodes `approve(bridge, amount)` for exactly the order's amount.
- **Gas is the desk's decision**, as for swaps: the gateway sends no gas
  figure, the engine estimates the call, and fees come from the node.

## Order model

The row is the **origin side** of the transfer, shaped like a send:
`chain_id` = origin, `token_in` = `token_out` = the origin token,
`recipient` = the bridge contract, `provider` = the bridge chosen (`relay`
or `across`), `kind = "bridge"`, `expected_out_raw` / `min_out_raw` in
destination units (the same decimals on every route), `amount_raw` = what
leaves the wallet, fee included, `tx_hash` = the deposit. The destination side lives in
`quote_json` (the *BridgePlan*):

```json
{
  "kind": "bridge", "via": "agentos-dex", "provider": "relay", "source": "relay",
  "route": {"asset": "USD", "originChainId": 8453, "destinationChainId": 4663,
            "tokenIn": {...}, "tokenOut": {...}},
  "amountRaw": "25000000",
  "quote": {"requestId": "0x…", "orderId": "0x…", "source": "relay", "sourceLabel": "Relay",
            "bridge": "0xe7b1…bc20", "expectedOutRaw": "…", "minBuyRaw": "…", "rate": 0.998,
            "fee": {"bps": 5, "amountRaw": "12500"}, "netAmountRaw": "24987500",
            "feesUsd": 0.05, "fees": {"gasUsd": null, "relayerUsd": 0.03, "appUsd": 0.0125},
            "timeEstimateSeconds": 1, "refundGuaranteed": true, "expiresAt": …,
            "routes": [...], "fetchedAt": …},
  "approved": {"requestId": "0x…", "source": "relay", "expectedOutRaw": "…",
               "floorRaw": "…", "minOutRaw": "…"},
  "execution": {"requestId": "0x…", "orderId": "0x…", "source": "relay", "bridge": "0xe7b1…bc20",
                "minBuyRaw": "…", "approvalTxHash": null, "depositTxHash": "0x…",
                "depositBookedAt": …, "depositBlock": …, "depositGasWei": "…"},
  "fill": {"status": "waiting|pending|success|failure|refund|unknown",
           "destinationTxHash": "0x…", "checkedAt": …, "attempts": 3,
           "receivedRaw": "…", "bookedAt": …, "lastError": null}
}
```

`requestId` is the gateway's `trackingId` (what a fill is followed by);
`orderId` is the bridge's own id, the one in the calldata. `feesUsd` is the
whole cost of the transfer, priced by the engine: `appUsd` is the AgentOS
fee and `relayerUsd` what the bridge keeps (its relayer and the destination
gas together). The gateway's five status words map onto the fill statuses:
`pending` → `pending`, `filled` → `success`, `refunded` → `refund`, `failed`
→ `failure`, `unknown` → `unknown`.

A plan without `"via": "agentos-dex"` was written before the bridge moved
behind the gateway, when the engine called Relay itself and deposited to its
solver. Such an order still runs: it is re-quoted through the gateway like
any other (unpinned, since it never chose a bridge), and its fill is read
with `source=relay`.

The order JSON (`trading.order`, `trade order --json`) shows `tokenOut` as
the **destination** token, `expectedOut` / `minOut` / `receivedOut` in its
units, `recipientLabel: "AgentOS Bridge"`, `provider` / `providerLabel` (the
bridge), and a `bridge` block: `originChainId`, `destinationChainId`,
`destinationChain`, `asset`, `source`, `sourceLabel`, `refundGuaranteed`,
`feeBps`, `requestId`, `fillStatus`, `feesUsd`, `fees`, `rate`,
`timeEstimateSeconds`, `approvalTxHash`, `depositTxHash`,
`depositExplorerUrl`, `destinationTxHash`, `destinationExplorerUrl`,
`checkedAt`.

The quote JSON (`trading.bridge.quote`) is a preview: it is asked without
the wallet as taker, so it has the amounts and no `requestId`. It carries
`expectedOut`, `minOut`, `rate`, `feesUsd`, `fees`, `fee` (`bps`, `amount`),
`timeEstimateSeconds`, `refundGuaranteed`, `provider`, `providerLabel`,
`source`, `sourceLabel`, `routes` (both bridges' answers) and `guard`.

## Lifecycle

```
quoted ──(manual)─────────────────────────────► approved ─┐
quoted ──(agent)──────────────────────────────► awaiting_approval ──approve──► approved ─┤
quoted ──(agent, over the cap)────────────────► rejected                                  │
                                                                                          ▼
approved ── re-quote below the floor ────────► (agent) awaiting_approval · (manual) failed
approved ── approve tx (if the allowance is short) → bridge call signed ──► submitted
submitted ── deposit reverted ───────────────► failed (gas entry)
submitted ── deposit mined: `withdraw` booked on the origin; fill.status = waiting
submitted ── fill `success`: `deposit` booked on the destination ──► confirmed (spend counted)
submitted ── fill `refund`, or `failure` for 2 min ─► failed
submitted ── no receipt or no fill after 6 h ─► failed
```

`confirmed` means the funds are on the destination chain. The waits
(`trade order --wait`, `trading.orders.wait`), the in-flight cap and
`recover_submitted` all work unchanged: the order simply stays `submitted`
longer.

**Fill tracking.** After the deposit is booked, a tracker polls
`/v1/bridge/status` (with the order's `source` and `requestId`) with a short backoff (2 → 30 s) for up to 30 minutes,
then leaves the order `submitted` with its reason; every housekeeping
`tick` re-enters the settlement through `recover_submitted`, which starts a
fresh tracker (the deposit half is skipped: `execution.depositBookedAt` is
set), until 6 h after creation, when the order fails naming the request
id. A `failure` is often not the last word (Relay may still retry or refund
the transfer): the order only fails once the bridge has said so for
`BRIDGE_FAILURE_GRACE_S` (2 minutes), and a fill or a refund that comes in
the meantime settles it as usual. A gateway restart resumes the same way.
`trading.bridge.status` reads the fill at most every 2 s per order; the tracker and a status call never
book the arrival twice (one lock per order, and the arrival entry is keyed
by the fill transaction and log index, so a sweep that saw the transfer
first already booked it).

**Ledger.** The origin leg is a `withdraw` entry under the order (FIFO lots
consumed, gas on it); the arrival is a `deposit` entry on the destination
under the same order (a new lot at the fill-time price). Its amount is read
from the fill transaction (the `Transfer` to the wallet, or the value when
the coin is paid straight to the wallet), else taken from the quote: a fill
that hands the coin over from inside a contract call carries no value of
its own. The sync treats
the destination chain as busy while a bridge is `submitted`, so the arriving
ETH is not booked twice as an anonymous deposit; a rebuild replays the
origin leg like a send's. A refund books the withdrawal and, when the funds
come back, an external deposit: net balance right, small realized-PnL noise.

## Surface

- CLI:
  - `agentos trade bridge quote --chain base --to-chain robinhood --token ETH (--amount 0.01 | --usd 20 | --pct 50) [--wallet A] --json`
  - `agentos trade bridge create … [--note …] [--client-id …] [--wait] [--wait-seconds 1..900] [--as-agent] --json` → `{"order": …}`
  - `agentos trade bridge status ORDER_ID|REQUEST_ID --json` → `{"order": …, "bridge": …}`
  - `agentos trade orders --kind bridge`
- RPC: `trading.bridge.quote`, `trading.bridge.create` (`chainId|chain`,
  `toChainId|toChain`, `token`, exactly one of `amount` / `amountUsd` /
  `amountPct`, `wallet`, `note`, `clientOrderId`, `wait`),
  `trading.bridge.status` (`orderId` or `requestId`), `trading.bridge.routes`.
- Desktop: the full desk's ticket switches **Swap | Bridge**, and the BOOK
  beside the chat has a **Bridge** tab. Both are the same form over
  `trading.bridge.quote` and `trading.bridge.create` (`initiator: "manual"`,
  so it runs at once after the confirm sheet, behind Touch ID when on); the
  order is then followed on Orders like any other bridge.
- Config: `trading.aggregator_base_url` (https, or http on loopback) is where
  the bridge is asked, the same deployment as the swaps;
  `trading.aggregator_trusted_spenders` adds bridge contracts to the pin.
  `trading.relay_base_url` is no longer used; a config file that still sets
  it keeps loading.
- Errors: `trading.bridge.unsupported_route` (exit 2, `details.supportedRoutes`),
  `trading.bridge.no_route` (exit 2: neither bridge carries this amount,
  usually because it is too small; `details.routes` says why for each),
  `trading.bridge.provider` (the gateway or a bridge unreachable or refusing; exit 1),
  `trading.bridge.unsupported_step`, `trading.bridge.not_found`, plus the shared
  `trading.insufficient_balance`, `trading.unpriced`, `trading.price_moved`,
  `trading.tx_failed`, `wallet.locked`.
