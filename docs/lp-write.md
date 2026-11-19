# LP writes

Phase 1 (`docs/lp-cards.md`) reads Uniswap V4 liquidity. Phase 2 lets the
desk act on it — collect fees, remove liquidity, add liquidity — through the
**same order pipeline as swaps and sends**: an order row, guardrails,
`awaiting_approval`, the operator approves (desk ApprovalCard or
`agentos trade approve`), the engine signs with the vault, broadcasts, waits
for the receipt and settles into the ledger. Nothing in this phase signs with
a private key from the environment; the skill's `lp_write.py` stays as it is
for the main agent, the desk never uses it.

Chains: Base (8453) and Robinhood Chain (4663). Scope: `collect`, `remove`,
`add` (mint a new position or increase an existing one). Not in scope:
creating pools, ratchets/auto-rebalancing, swapping to obtain the other side.

## Policy

- **Every LP write parks for approval**, whatever the initiator and whatever
  the amount (like a send from the agent). `guardrails.evaluate_lp_write`
  returns `needs_approval` for `collect`/`remove`; for `add` it also returns
  `blocked_daily_cap` when the deposit's USD value would exceed the daily
  cap (an `add` counts toward `daily_spend`; `collect`/`remove` do not).
- Approval TTL = the existing `approval_ttl_seconds`. On approve the plan is
  **re-validated** against the current pool state: for `add`, the required
  amounts are recomputed and if they exceed the approved maxima the order
  fails `trading.price_moved`; for `remove`, the expected amounts are
  recomputed and if the minimums can no longer be met it fails
  `trading.price_moved`. Deadline = latest block timestamp + 600 s, set at
  execution, never at creation.
- Slippage default 1 % (`--slippage`), applied as maxima on `add` and minima
  on `remove`; `collect` has no slippage (zero-liquidity decrease).
- The daily cap applies to agent-initiated `add` only (manual adds park but
  are not capped, as for swaps). A native deposit keeps a gas reserve back.
  Approvals are re-read live at execution; a top-up of a position with
  uncollected fees uses INCREASE + CLOSE_CURRENCY (the SETTLE_PAIR form
  reverts when fees exceed what a side owes).
- No auto-swap: if the wallet lacks a side for `add`, the order is refused
  with `trading.insufficient_balance` naming the missing amount; one-sided
  ranges (entirely above or below the current price) need only one token and
  are the cheap way to test.
- Wallets: the position's owner must be a vault wallet (`collect`/`remove`);
  `add` uses the primary wallet unless `--wallet` names another vault wallet.

## Order model

New `kind` values in the `orders` table (free TEXT today; add them to
`ORDER_KINDS`): `lp_collect`, `lp_remove`, `lp_add`. Columns map as:

| column | lp_collect | lp_remove | lp_add |
|---|---|---|---|
| `token_in` / `token_out` | base / quote of the pool | same | same |
| `amount_raw`, `amount_human` | 0 | liquidity removed (raw L) | liquidity added (raw L) |
| `value_usd` | fees expected (USD) | principal + fees expected | USD deposited |
| `slippage_pct` | null | pct | pct |
| `quote_json` | `LpPlan` (below) | `LpPlan` | `LpPlan` |
| `recipient` | owner wallet | owner wallet | null |
| `note`, `client_order_id`, `initiator`, `session_key`, `batch_id` | as for swap/send | | |

```jsonc
LpPlan = {
  "op": "collect" | "remove" | "add",
  "chain": Chain,                         // as in lp-cards.md
  "tokenId": "48213" | null,              // null for a fresh mint until confirmed
  "increase": boolean,                    // add: true when --to-position was given
  "pool": { "poolId", "poolKey": {currency0, currency1, fee, tickSpacing, hooks}, "tick", "sqrtPriceX96", "feePct" },
  "token": Token, "quote": Token,         // base = the token the user named / currency that is not the known quote
  "range": Range,                         // tickLower/Upper + price + mcap bounds (lp-cards.md)
  "liquidity": "raw L delta",
  "pct": 100,                             // remove only
  "expected": { "base": Amount, "quote": Amount, "usd": number | null },   // what moves
  "bounds":   { "base": "raw", "quote": "raw" },   // add: maxima; remove: minima; collect: 0/0
  "fees":     { "base": Amount, "quote": Amount, "usd": number | null },   // uncollected fees at plan time (collect/remove)
  "positionValueUsd": number | null,      // remove/collect: the position's value before
  "approvals": [ { "token": "0x…", "symbol", "step": "erc20->permit2" | "permit2->posm", "amountRaw": "…", "needed": boolean } ],  // add only
  "oneSided": "base" | "quote" | null,    // add: range entirely on one side
  "simulation": { "ok": true, "gasUsed": 210000, "method": "eth_simulateV1" | "eth_call", "revert": null },
  "gasUsd": number | null,
  "slippagePct": 1.0,
  "planHash": "0x1a2b3c4d",               // keccak of the canonical plan, first 4 bytes (as the skill does)
  "createdAtBlock": 21044901
}
```

Confirmed orders add to the order JSON: `txHash`, `explorerUrl`, `gasUsd`,
`received` / `spent` (`{base: Amount, quote: Amount}` from the receipt's
transfers), and for a mint the new `tokenId` (from the PositionManager
Transfer log). `approval_tx_hash` holds the last approval tx as for swaps;
the plan's `approvals[]` get `txHash` entries as they are mined.

## Execution (engine)

`service._execute` gains `_execute_lp` (all three ops) and `_confirm` gains
`_settle_lp`.

1. **Load** the position (`lp._load_position`) / pool (`lp.pool_states`)
   through a `ChainEnv` built by `lp._engine_env` (engine RPC, engine prices).
2. **Build the plan** with the skill's encoders, loaded through
   `lp.unilp()` (add `v4_actions`, `abi_codec`, `simulate` to `_MODULES`):
   - collect → `build_collect_plan(pool_key, token_id, recipient)`
   - remove → `pct == 100` → `build_burn_plan(...)` (decrease + burn +
     take), else `build_decrease_plan(pool_key, token_id, L, a0min, a1min,
     recipient)`; mins via `with_slippage_down`
   - add → mint: size `L` from the deposit with `v4_math.get_liquidity_for_amounts`,
     required amounts with `get_amounts_for_liquidity(round_up=True)`, maxima via
     `with_slippage_up`; `build_mint_plan(pool_key, lo, hi, L, a0max, a1max, recipient)`;
     increase: `build_increase_plan(pool_key, token_id, L, a0max, a1max, recipient)`.
     Native currency0 ⇒ `value = a0max` (the encoder already appends SWEEP).
   - calldata = `PositionManager.modifyLiquidities(encode_unlock_data(actions, params), deadline)`;
     the PositionManager address comes from the unilp chain registry.
3. **Simulate** (`unilp.simulate.simulate_call`, then `eth_call` fallback) before
   parking; a revert refuses the order with `trading.simulation_failed` and the
   decoded reason when available.
4. **Park** (`_decide_batch`) → `awaiting_approval`, emit
   `trading.approval.requested` as today.
5. **On approve**: re-validate (policy above), then for `add` run the
   approvals the plan marked `needed`, each through `_send` like swap
   approvals (gas recorded with `kind="approval"`):
   `ERC20.approve(Permit2, exact amount)` when the ERC-20→Permit2 allowance is
   short, then `Permit2.approve(token, PositionManager, amount160, expiration
   = now + 30 min)` when the Permit2 allowance or expiration is short.
   Exact amounts, never unlimited (the engine's rule; the skill's unlimited
   approve is not copied). Wait for each receipt before the next step.
6. **Send** the `modifyLiquidities` tx through `_send` (simulate, gas × 1.2,
   fee data, gas-USD ceiling, nonce, hash written before broadcast), then
   `_watch` → `_confirm`.
7. **Settle** (`_settle_lp`): parse the receipt's transfers for the wallet
   (`evm.receipt_transfers`), record ledger `entries` with new kinds
   `lp_collect` (tokens in), `lp_remove` (tokens in), `lp_add` (tokens out),
   plus `gas`; lots follow the deposit/withdraw convention (tokens received
   open lots at the settle-time price, tokens deposited consume lots FIFO);
   an agent-initiated `add` bumps `daily_spend` by its USD value (manual
   adds are not capped, as for swaps). Emit `trading.order.finished`
   and `trading.changed {reason:"order"}`. For a mint, read the new `tokenId`
   from the PositionManager `Transfer(0x0 → wallet)` log.
8. **Failure mapping** as for swaps: `trading.tx_failed` (reverted receipt),
   `trading.tx_pending`, `trading.gas_too_high`, `trading.insufficient_balance`,
   `trading.price_moved`, `trading.quote_expired` (approval TTL).

## RPC

Agent-callable (they only create parked orders): `trading.lp.collect`,
`trading.lp.remove`, `trading.lp.add`. Params mirror the CLI flags in
camelCase (`tokenId`, `chainId`, `pct`, `token`, `usd`, `amountBase`,
`amountQuote`, `range`, `toPosition`, `wallet`, `slippagePct`, `note`,
`clientOrderId`). Response = `{"order": …}` like revoke/approve/wait; the
order JSON (`_order_dict`) carries `plan` (= `LpPlan`, plus `wallet`,
`positionManager`, `status`, `burn`, `value`, `actions`, `rangeSpec`,
`rangeDefaulted`, `warnings`), `provider: "uniswap_v4"`. `trading.lp.add`
accepts `token` | `target` | `poolId`, and `token` may be omitted with
`toPosition`. Approval/rejection/wait stay on the existing operator-only
`trading.orders.approve|reject` and `trading.orders.wait`.

Read RPCs from phase 1 additionally echo `"request": {"kind", "params"}` in
every card payload so a card can re-run itself (refresh).

## CLI

```
agentos trade lp collect <tokenId> --chain base|robinhood [--note "…"] [--client-id ID] [--wait --wait-seconds N] [--json]
agentos trade lp remove  <tokenId> --chain base|robinhood [--pct 100] [--slippage 1] [--note …] [--client-id …] [--wait …] [--json]
agentos trade lp add     <token|poolId> --chain base|robinhood (--usd X | --amount-base A [--amount-quote B])
                         [--range mcap:2M-10M | pct:20 | full | ticks:LO:HI]   # default pct:20 around the current price
                         [--to-position <tokenId>] [--wallet ADDR|label] [--slippage 1] [--note …] [--client-id …] [--wait …] [--json]
```

- Output is the order JSON like `trade swap`/`trade send`: `status`
  (`awaiting_approval` → `approved` → `submitted` → `confirmed` | `failed` |
  `rejected` | `expired`), `plan`, and after confirmation `txHash`,
  `explorerUrl`, `received`/`spent`, `gasUsd`, `tokenId`.
- `--wait` blocks until a final status (same loop as swap).
- With `--json`, after a **confirmed** order the CLI also publishes the
  refreshed **position card** (`lp position <tokenId>` payload; for a burn,
  the wallet's positions card) with the phase-1 marker, so the chat shows the
  new state without another command.
- `--range mcap:LO-HI` accepts `2M`, `2.5m`, `750k`, `1e6`; `pct:N` = ±N %
  around the current price snapped to tick spacing; `full` = min/max ticks;
  `ticks:LO:HI` raw ticks (snapped, error if not aligned).
- `--usd X`: the deposit is split between the two sides as the range
  requires at the current price (all on one side when one-sided); the CLI
  refuses with `trading.insufficient_balance` naming the short side.
- Errors: JSON on stderr, exit 2 for input/`trading.invalid`, 1 for
  gateway/provider; new codes `trading.lp.not_owner` (position not in the
  vault), `trading.lp.position_closed`, `trading.simulation_failed`,
  `trading.lp.range_invalid`.

## Desktop and chat

- **ApprovalCard** renders the three kinds: title (`Collect fees`,
  `Remove liquidity`, `Add liquidity`), position (`#id · PEPE / WETH · Base · 1%`),
  range (lower – upper, mcap when known) with in/out-of-range pill, facts:
  *you receive* (collect/remove: base + quote + USD), *fees included*
  (remove), *you deposit* (add: base + quote + USD), *minimum* / *maximum*
  (slippage bounds), *approvals needed* (add), *gas*, *order*, *expires*;
  risk stamps: `remove 100%` → "burns the position NFT", `add` one-sided →
  "one-sided: all `<token>` until price enters the range", `add` with a hook
  address → "pool has a hook".
- **Desk ledger** labels: "Collect fees · #id", "Remove liquidity · #id · 100%",
  "Add liquidity · PEPE/WETH · $50", outcomes as for send.
- **Position card actions** (phase-1 card, `kind=position` and rows of
  `positions` whose owner is `inApp`): `Collect fees` and `Remove…` (a small
  choice: 50 % / 100 %). Clicking calls the write RPC directly from the
  desktop (operator connection) with the card's `tokenId`/`chainId`; the
  order parks and the ApprovalCard appears in the Book; the button shows
  "awaiting approval" until `trading.order.finished` for that order, then
  the card refreshes itself. On the web the buttons are hidden (the web UI is
  agent-bound and cannot approve).
- **Refresh** on every phase-1 card: re-runs `request` through the read RPC
  and swaps the payload in place; shows "as of block N · just now".
- **Desk prompt v14**: teaches the three commands; "From you an LP write
  **always** parks as `awaiting_approval`, whatever the amount; `--wait`
  then blocks until the user decides"; reading rules: "collect fee của X" /
  "claim fees on X" → find the tokenId from `lp positions` when the user
  names a pair, not an id; "rút hết / remove all / close" → `remove --pct 100`;
  "rút một nửa / take half out" → `--pct 50`; "add $50 to X between 2M and
  10M" → `add X --usd 50 --range mcap:2M-10M`; never pick a range the user
  did not state without saying which default was used (pct:20). Remove the
  phase-1 sentence that says writes are not available.

## Tests

- Engine: `lp_world` gains a PositionManager that decodes
  `modifyLiquidities` calldata (actions + params) and applies it to the fake
  pool (liquidity, fees, NFT transfer/burn); tests for plan building per op,
  one-sided sizing, mcap/pct/full/ticks ranges, slippage bounds, simulation
  failure refusal, guardrail parking for agent and operator, daily cap on
  `add`, approve → approvals sequence (exact amounts, Permit2 expiry) →
  modifyLiquidities → settle entries/lots/daily_spend, re-validation
  failures (`price_moved`, `quote_expired`), not-owner / closed position,
  RPC surface (agent may create, approve is operator-only), CLI flags,
  `--wait`, the post-confirm card marker.
- Frontend/desktop: ApprovalCard per kind (facts, stamps), ledger labels,
  position-card buttons (hidden on web, states on desktop), refresh
  (payload swap, "just now").
- Live (tester, real money, dust wallet): on Base, `add` a one-sided ~$0.05
  ETH position in a hookless ETH/USDC pool above the current price →
  approve with `agentos trade approve` → `collect` (0 fees, must still
  confirm) → `remove --pct 100` → the ETH is back minus gas; every step
  visible as an ApprovalCard in the desk and as ledger entries.
