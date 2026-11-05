# Bridge: Base ⇄ Robinhood Chain through Relay

The trading engine moves ETH and the USD stable between its two chains
through [Relay](https://docs.relay.link) — a public, keyless REST API that
prices a cross-chain transfer and answers with the unsigned origin-chain
transactions that fund it. Relay holds no key and never broadcasts: funds
move only when the vault signs the deposit. A bridge runs through the
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
  slipping past the cap. A manual bridge runs at once.
- **Re-quote before signing.** The quote the user approved is a promise
  about the price only. At execution Relay is asked again; a fill below the
  approved floor (`BRIDGE_TOLERANCE_PCT`, 1 %) sends an agent's order back to
  `awaiting_approval` with the new numbers, and fails a manual one with
  `trading.price_moved`.
- **What is signed is pinned, not read from the response.** The deposit
  must go `to` a Relay solver in `RELAY_SOLVERS` and carry calldata (that is
  what ties it to the request — a bare transfer to the solver would be
  lost); a native deposit carries exactly the order's value, an ERC-20 one
  none; the approval, when one is needed, is on the bridged token, to that
  same solver, and is re-encoded by the engine for exactly the order's
  amount (`_check_approval_tx`, then `_check_bridge_tx`). A `signature`
  step, an unknown step, two deposits — refused unsigned
  (`trading.bridge.unsupported_step`, `trading.tx_failed`).
- **Gas is the desk's decision**, as for swaps: Relay's `gas` is taken only
  under the chain's cap, fees come from the node.

## Order model

The row is the **origin side** of the transfer, shaped like a send:
`chain_id` = origin, `token_in` = `token_out` = the origin token,
`recipient` = the Relay solver, `provider = "relay"`, `kind = "bridge"`,
`expected_out_raw` / `min_out_raw` in destination units (the same decimals
on every route), `tx_hash` = the deposit. The destination side lives in
`quote_json` (the *BridgePlan*):

```json
{
  "kind": "bridge", "provider": "relay",
  "route": {"asset": "USD", "originChainId": 8453, "destinationChainId": 4663,
            "tokenIn": {...}, "tokenOut": {...}},
  "amountRaw": "25000000",
  "quote": {"requestId": "0x…", "solver": "0x4cd0…bc31", "expectedOutRaw": "…",
            "rate": 0.997, "feesUsd": 0.03, "fees": {"gasUsd": …, "relayerUsd": …, "appUsd": …},
            "timeEstimateSeconds": 1, "fetchedAt": …, "steps": [...]},
  "approved": {"requestId": "0x…", "expectedOutRaw": "…", "minOutRaw": "…"},
  "execution": {"requestId": "0x…", "approvalTxHash": null, "depositTxHash": "0x…",
                "depositBookedAt": …, "depositBlock": …, "depositGasWei": "…"},
  "fill": {"status": "waiting|pending|success|failure|refund|unknown",
           "destinationTxHash": "0x…", "checkedAt": …, "attempts": 3,
           "receivedRaw": "…", "bookedAt": …, "lastError": null}
}
```

The order JSON (`trading.order`, `trade order --json`) shows `tokenOut` as
the **destination** token, `expectedOut` / `minOut` / `receivedOut` in its
units, `recipientLabel: "Relay solver"`, `provider: "relay"`, and a
`bridge` block: `originChainId`, `destinationChainId`, `destinationChain`,
`asset`, `requestId`, `fillStatus`, `feesUsd`, `fees`, `rate`,
`timeEstimateSeconds`, `approvalTxHash`, `depositTxHash`,
`depositExplorerUrl`, `destinationTxHash`, `destinationExplorerUrl`,
`checkedAt`.

## Lifecycle

```
quoted ──(manual)─────────────────────────────► approved ─┐
quoted ──(agent)──────────────────────────────► awaiting_approval ──approve──► approved ─┤
quoted ──(agent, over the cap)────────────────► rejected                                  │
                                                                                          ▼
approved ── re-quote below the floor ────────► (agent) awaiting_approval · (manual) failed
approved ── approve tx (if the allowance is short) → deposit signed ──► submitted
submitted ── deposit reverted ───────────────► failed (gas entry)
submitted ── deposit mined: `withdraw` booked on the origin; fill.status = waiting
submitted ── Relay success: `deposit` booked on the destination ──► confirmed (spend counted)
submitted ── Relay failure / refund ─────────► failed
submitted ── no receipt or no fill after 6 h ─► failed
```

`confirmed` means the funds are on the destination chain. The waits
(`trade order --wait`, `trading.orders.wait`), the in-flight cap and
`recover_submitted` all work unchanged: the order simply stays `submitted`
longer.

**Fill tracking.** After the deposit is booked, a tracker polls
`/intents/status/v3` with a short backoff (2 → 30 s) for up to 30 minutes,
then leaves the order `submitted` with its reason; every housekeeping
`tick` re-enters the settlement through `recover_submitted`, which starts a
fresh tracker (the deposit half is skipped: `execution.depositBookedAt` is
set), until 6 h after creation, when the order fails naming the request
id. A gateway restart resumes the same way. `trading.bridge.status` reads
Relay at most every 2 s per order; the tracker and a status call never
book the arrival twice (one lock per order, and the arrival entry is keyed
by the fill transaction and log index, so a sweep that saw the transfer
first already booked it).

**Ledger.** The origin leg is a `withdraw` entry under the order (FIFO lots
consumed, gas on it); the arrival is a `deposit` entry on the destination
under the same order (a new lot at the fill-time price). The sync treats
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
- Config: `trading.relay_base_url` (https, or http on loopback).
- Errors: `trading.bridge.unsupported_route` (exit 2, `details.supportedRoutes`),
  `trading.bridge.provider` (Relay unreachable or refusing; exit 1),
  `trading.bridge.unsupported_step`, `trading.bridge.not_found`, plus the shared
  `trading.insufficient_balance`, `trading.unpriced`, `trading.price_moved`,
  `trading.tx_failed`, `wallet.locked`.
