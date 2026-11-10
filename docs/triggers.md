# Price triggers (`application/vnd.agentos.trigger+json`)

A **trigger** is a conditional order the trading engine watches and fires by
itself: *sell half my ETH if it drops under $3,800*, *buy $50 of ETH when it
is back under $3,500*, *sell when ETH falls 10 % from its peak*, or just
*tell me when ETH crosses $5,000*. The user approves the trigger once; the
engine polls the price on every tick and, when the condition holds, places
one ordinary swap order through the existing pipeline (guardrails, approval
threshold, daily cap, vault signing, ledger) or sends one notification. No
LLM turn is spent watching the market; the condition, the size and the stop
rules are rows in the ledger and are enforced there.

A trigger is the third way the engine acts: *now* (`trade swap`), *on a
schedule* (DCA mandates, `docs/dca.md`) and now *when the market does X*.
It reuses the mandate machinery wherever it can: the same lifecycle, the
same "an agent only proposes" rule, the same cards-in-chat mechanism, the
same desk controls.

This file is the contract between the engine, the gateway, the CLI, the
shared renderer and the desktop. Change it before changing any of them.

Chains: Base (8453) and Robinhood Chain (4663). OCO pairs (a take-profit and
a stop-loss on one position, one cancelling the other) are **brackets** and
live in [`brackets.md`](brackets.md): two triggers of this file sharing a
group. Not in scope here: ladders, time-of-day conditions, conditions on anything but one token's USD
price, repeating alerts, LP-range alerts (follow-up).

## Vocabulary

| word | meaning |
|---|---|
| **kind** | what fires: `sell` (token → quote), `buy` (quote → token) or `alert` (a notification, no order) |
| **token** | the token whose USD price is watched **and** traded |
| **quote** | the counter token: what a sell receives / a buy spends; defaults to the chain's USDC, or the native coin when USDC itself is the token |
| **condition** | `below <price>`, `above <price>`, or `trail <pct>` (price falls `pct` % from the highest price seen since arming) |
| **armed** | the trigger is live and being checked every tick |
| **fire** | one attempt to act when the condition held (or a manual *Fire now*) |

Common names, derived by the engine when no `--name` is given:

| kind · condition | name |
|---|---|
| sell · below | `Stop-loss ETH` |
| sell · above | `Take-profit ETH` |
| sell · trail | `Trailing stop ETH` |
| buy · below | `Buy ETH under $3,500` |
| buy · above | `Buy ETH over $4,200` |
| buy · trail | not allowed (`trading.trigger.invalid`) |
| alert · any | `Alert ETH under $3,800` / `Alert ETH over $5,000` / `Alert ETH −10 % from peak` |

## Lifecycle

```
create ──(operator)────────────► armed ──► triggered ──► done
   └────(agent)──► awaiting_approval ┤        │           (filled / alerted)
                        │            │        ├─► armed   (fire failed, retry next tick; 3 in a row → paused)
                        ├─► rejected │        ├─► paused  (parked order expired, or nothing to sell)
                        └─► expired  │        └─► stopped (parked order rejected by the user)
                                     ├─► paused ⇄ armed   (user)
                                     ├─► stopped          (user)
                                     └─► expired          (validUntil passed)
```

- **Who may create.** Any connection. An **agent-bound** connection
  (`gateway/agent_surface.py`) always gets `awaiting_approval`; the operator
  (desktop, CLI without agent markers) gets `armed` at once. `initiator` is
  never trusted from params, exactly as for orders and mandates.
- **Approval** of a pending trigger is operator-only. A pending trigger
  expires **24 h** after creation (`expires_at`).
- **Arming** records `armed_at` and `armed_price_usd` (the price seen then,
  or `null`). For a `trail` condition the peak starts at the armed price and
  is raised on every later check where the price is higher.
- **Checking.** `TradingService.tick()` (every `sync_interval_seconds`, 60 s)
  calls `trigger_check()`. One pass: expire proposals and `validUntil`,
  reconcile open fires, then for every `armed` trigger read the token's spot
  price (`PriceService.price`, batched per chain), store `last_price_usd` /
  `last_checked_at`, raise the peak, and evaluate:
  - `below`: `price <= price_usd`
  - `above`: `price >= price_usd`
  - `trail`: `peak_price_usd` known and `price <= peak × (1 − trail_pct/100)`
  - An unknown price (`None`) is **not** a miss: `hits` is left alone and the
    trigger waits for the next tick. It never fires blind.
- **Confirmation.** The condition must hold on **2 consecutive checks**
  (`CONFIRM_TICKS = 2`, about a minute) before the trigger fires; one check
  where it does not hold resets `hits` to 0. A single bad print from the
  price feed must not sell a position. The card shows `hits / 2`.
- **A fire** (`trigger_fires` row, written **before** the order like a DCA
  run so a crash cannot act twice):
  - `alert`: emit `trading.trigger.fired` with the price; the fire is
    `alerted`, the trigger is `done` (`statusReason` = `"alerted at $3,790"`).
  - `sell`: one swap order `token → quote`, `initiator="agent"` (guardrails
    apply), sized from the trigger: `amount_pct` of the wallet's balance **at
    fire time**, or `amount` (token units), or `amount_usd`. `note` =
    `"<name> · fired at $3,790"`, `trigger_id` set, `wait=False`. The
    trigger is `triggered` while the order is open.
  - `buy`: one swap order `quote → token` for `amount_usd`.
  - Before placing: a sell with a zero token balance, or a buy whose quote
    balance cannot cover `amount_usd`, is a fire `skipped insufficient_balance`
    and the trigger is **paused** at once (`"paused: nothing to sell"` /
    `"paused: insufficient USDC"`). There is nothing to retry.
- **Fire outcomes** (`trigger_fires.status`): `pending` (order placed, not
  settled), `filled`, `parked` (awaiting approval), `alerted`, `skipped`
  (`insufficient_balance`), `failed` (quote/execution error; `reasonCode` =
  the error code), `expired`, `rejected`.
  - `filled` → trigger `done`, `statusReason` e.g. `"sold 0.05 ETH for 189.4 USDC at $3,788"`.
  - `failed` → trigger back to `armed` with `bad_streak + 1` and `hits = 0`
    (it must re-confirm). Three consecutive failures → `paused`
    (`"paused after 3 failed fires: <code>"`). Resume resets the streak.
  - `parked` → trigger stays `triggered`; when the order settles: confirmed
    → `done`; **rejected** → `stopped` (`"sell rejected by you"`); **expired**
    → `paused` (`"the sell waited for approval and expired"`). It does not
    re-arm by itself: re-arming would park a new order every minute.
  - A `pending` fire whose order never appeared is written off as `failed
    trading.interrupted` after 1 h; each tick re-checks open fires against
    their orders (restart safety).
- **Fire now** (`trading.trigger.fire`, operator-only) acts at once on an
  `armed` or `paused` trigger regardless of the condition; it is a fire with
  `manual = 1`. Exposed on the desk as *Fire now* (and *Sell now* / *Buy
  now* wording for the two kinds).
- **Stop** is terminal and rejects a parked order of the trigger. **Pause**
  (`"user"`) / **Resume** flip `armed`/`paused`; resume resets `hits`,
  `bad_streak` and, for `trail`, the peak to the current price (a stop that
  was paused through a rally must not fire on the old peak).
- **validUntil** (optional, GTC when absent): an `armed`/`paused` trigger
  past it becomes `expired` (`"not reached by <date>"`).
- Nothing is ever deleted. `list` shows live triggers
  (`awaiting_approval`, `armed`, `triggered`, `paused`) unless `--all`.
  It never lists a bracket's legs (nor counts them in `totals`): a bracket
  is listed by `trading.bracket.list` (`brackets.md`).
- **A bracket's legs refuse leg-level writes**: `approve`, `reject`,
  `pause`, `resume`, `stop` and `fire` on a leg answer
  `trading.trigger.bad_state` (*"trg_… is the take-profit leg of bracket
  brk_…: use trading.bracket.*"*); `get` works and shows the leg with its
  `bracket` field.
- **Update** is not in v1 (stop and create again).

### Relative prices

`--below`/`--above` accept an absolute USD price (`3800`) **or** a percent of
the price at creation (`-10%` / `10%` on `--below` means 10 % under the
current price; `+15%` / `15%` on `--above` means 15 % over it). The engine
resolves the percent to an absolute `price_usd` **at creation** (what the
user saw on the card) and records `from_price_usd`. With no price known the
percent form is refused (`trading.trigger.invalid`: *"ETH has no price right
now: give an absolute price"*). `--trail` is always a percent.

### Warnings at creation

- `below` when the price is already at or under it (`above` likewise): *"ETH
  is already at $3,700, under $3,800: this fires after the next two checks"*.
- A sell/buy whose estimated size is over the approval threshold: *"a sell
  of ≈$500 is above the $100 approval threshold and will wait for you when
  it fires"*.
- A sell from a wallet that holds none of the token: *"Key main holds no ETH"*.
- No price known: *"price unknown: the trigger waits until ETH has a price"*
  (the trigger still arms).

The quote of a **sell** or **buy** defaults to the chain's USDC, or the
native coin when USDC itself is the token: `trigger create USDC --sell --pct
100 --above 0.5` sells USDC for ETH (`sell 100 % of USDC → ETH`). Only a
`--quote` the user gives that equals the token is refused (*"token and quote
are the same token"*); a chain with no canonical USDC needs `--quote`.

An **alert** never trades, so its quote is only the counter the card shows:
the given `--quote`, else the chain's USDC, else (USDC itself being watched,
or a chain with no canonical USDC) the native coin. An alert is never
refused for its quote.

## Ledger (schema v8)

```sql
CREATE TABLE IF NOT EXISTS triggers (
  trigger_id       TEXT PRIMARY KEY,      -- "trg_" + 8 hex
  kind             TEXT NOT NULL,         -- sell | buy | alert
  name             TEXT NOT NULL,
  status           TEXT NOT NULL,         -- awaiting_approval|armed|triggered|paused|done|stopped|rejected|expired
  status_reason    TEXT,
  chain_id         INTEGER NOT NULL,
  wallet           TEXT NOT NULL,
  token            TEXT NOT NULL,         -- watched and traded (address)
  quote            TEXT NOT NULL,         -- counter token (address)
  direction        TEXT NOT NULL,         -- below | above | trail
  price_usd        REAL,                  -- below/above threshold (absolute, resolved)
  trail_pct        REAL,                  -- trail
  from_price_usd   REAL,                  -- price at creation when a percent was given
  amount_usd       REAL,                  -- buy: spend; sell: optional size
  amount_pct       REAL,                  -- sell: % of the token balance at fire time
  amount_raw       TEXT,                  -- sell: fixed token amount (raw string)
  slippage_pct     REAL,
  confirm_ticks    INTEGER NOT NULL DEFAULT 2,
  hits             INTEGER NOT NULL DEFAULT 0,
  peak_price_usd   REAL,                  -- trail state
  armed_at         REAL,
  armed_price_usd  REAL,
  last_price_usd   REAL,
  last_checked_at  REAL,
  triggered_at     REAL,
  fires_failed     INTEGER NOT NULL DEFAULT 0,
  bad_streak       INTEGER NOT NULL DEFAULT 0,
  valid_until      REAL,
  initiator        TEXT NOT NULL,         -- 'agent' | 'manual'
  session_key      TEXT,
  created_at       REAL NOT NULL, updated_at REAL NOT NULL,
  approved_at      REAL, expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_triggers_status ON triggers(status, created_at DESC);

CREATE TABLE IF NOT EXISTS trigger_fires (
  fire_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger_id   TEXT NOT NULL,
  n            INTEGER NOT NULL,          -- 1-based, every attempt
  at           REAL NOT NULL,
  status       TEXT NOT NULL,             -- pending|filled|parked|alerted|skipped|failed|expired|rejected
  reason       TEXT,                      -- "<code>: <detail>" like mandate_runs
  price_usd    REAL,                      -- spot price seen at the fire
  order_id     TEXT,
  manual       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trigger_fires ON trigger_fires(trigger_id, n DESC);

ALTER TABLE orders ADD COLUMN trigger_id TEXT;   -- migration v7 → v8
CREATE INDEX IF NOT EXISTS idx_orders_trigger ON orders(trigger_id) WHERE trigger_id IS NOT NULL;
```

`_order_dict` gains `"triggerId": str | null`. `ORDER_KINDS` unchanged (a
trigger's order is `kind='swap'`). When an order with `trigger_id` settles,
the engine settles the fire and the trigger in the same place it settles
the order (`_order_finished`), exactly like a DCA run.

Engine module: `src/agentos/trading/triggers.py` — pure helpers (condition
evaluation, relative-price resolution, default names, reasons, payload
building) with no service imports; the runner lives on `TradingService`:

```python
async def trigger_create(self, *, chain, kind, token, quote, direction, price, trail_pct,
                         amount_usd, amount_pct, amount, wallet, slippage_pct, name,
                         valid_for_seconds, initiator, session_key) -> dict   # payload kind="trigger"
async def trigger_get(self, trigger_id) -> dict
async def trigger_list(self, *, all=False, wallet=None) -> dict                # kind="triggers"
async def trigger_approve(self, trigger_id) -> dict
async def trigger_reject(self, trigger_id, reason=None) -> dict
async def trigger_pause(self, trigger_id) -> dict
async def trigger_resume(self, trigger_id) -> dict
async def trigger_stop(self, trigger_id, reason=None) -> dict
async def trigger_fire_now(self, trigger_id, *, wait=False) -> dict           # payload + "fire": Fire
async def trigger_check(self) -> None                                         # from tick()
```

`price` for `trigger_create` is a `str | float`: `"3800"`, `"-10%"`, `"+15%"`,
`"10%"`; the service resolves it (see *Relative prices*). `amount` is a
token amount in human units (`"0.05"`).

Errors are `TradingError` codes: `trading.trigger.not_found`,
`trading.trigger.bad_state`, `trading.trigger.invalid` (unknown kind or
direction; `buy` with `trail`; no size on sell/buy, more than one size, a
size on `alert`; `amount_pct` outside 0 < pct ≤ 100; price ≤ 0; trail
outside 0 < pct < 100; `valid_for` under 60 s; a percent price with no
current price; quote missing on Robinhood Chain; a given quote == token; wallet
`all`), `trading.operator_required` (RPC layer), plus `trading.disabled`,
`wallet.locked`, `trading.unsupported_chain`, `trading.token_not_found`.
At create, `slippagePct` may not exceed the agent slippage ceiling.

Events: every state change emits `trading.changed {reason: "trigger",
triggerId}` and `trading.trigger.changed {trigger}` (full `Trigger`
object). Each fire emits `trading.trigger.fired {triggerId, trigger, fire}`
once when it happens (alert sent / order placed / skipped / failed) and
`trading.trigger.changed` when it settles. The order itself emits the usual
`trading.approval.requested` / `trading.order.finished`.

## Payload

```jsonc
Envelope = {
  "version": 1,
  "kind": "trigger" | "triggers",
  "fetchedAt": iso, "warnings": [string],
  "request": { "kind": "get" | "list", "params": {…} }     // ↻ re-runs trading.trigger.<kind>
}

{ ...Envelope, "trigger": Trigger, "fire": Fire | undefined }          // kind = "trigger"
{ ...Envelope, "triggers": [Trigger],                                  // kind = "triggers": live first, then newest
  "totals": { "count": 3, "armed": 2, "awaiting": 1, "triggered": 0 } }

Trigger = {
  "id": "trg_1a2b3c4d", "name": "Stop-loss ETH",
  "kind": "sell" | "buy" | "alert",
  "status": "awaiting_approval" | "armed" | "triggered" | "paused" | "done" | "stopped" | "rejected" | "expired",
  "statusReason": string | null,
  "chain": Chain, "wallet": Wallet, "token": Token, "quote": Token,    // LP card types; priceUsd on both tokens
  "condition": {
    "direction": "below" | "above" | "trail",
    "priceUsd": number | null,            // below/above threshold
    "trailPct": number | null,            // trail
    "fromPriceUsd": number | null,        // when a percent was given at creation
    "peakPriceUsd": number | null,        // trail: highest price since arming
    "stopPriceUsd": number | null,        // trail: peak × (1 − trailPct/100), what it would fire at now
    "confirmTicks": 2, "hits": 0,
    "label": "under $3,800" | "over $5,000" | "10 % below peak"
  },
  "action": {
    "kind": "sell" | "buy" | "alert",
    "amountUsd": number | null,           // buy: spend; sell: size when given in USD
    "amountPct": number | null,           // sell: % of balance
    "amount": Amount | null,              // sell: fixed token amount
    "estimatedUsd": number | null,        // what the fire would move at the current price (sell pct/amount × price); once terminal with a result: result.amountIn.usd, what actually moved
    "slippagePct": number | null,
    "needsApproval": boolean,             // estimatedUsd > approvalThresholdUsd
    "approvalThresholdUsd": number, "dailyCapUsd": number,
    "label": "sell 50 % of ETH → USDC" | "buy $50 of ETH with USDC" | "notify"
  },
  "market": {
    "priceUsd": number | null,            // last price seen
    "armedPriceUsd": number | null,
    "distancePct": number | null,         // signed % move from priceUsd needed to fire: −2.1 (must fall), +4.0 (must rise); 0 when already met
    "checkedAt": iso | null,
    "balance": Amount | null              // the wallet's token (sell) / quote (buy) balance now; null for alert and for a terminal trigger
  },
  "fires": [Fire],                        // newest first, ≤ 20
  "result": {                             // when done by an order
    "orderId": string, "txHash": string | null, "explorerUrl": string | null,
    "amountIn": Amount, "amountOut": Amount | null, "priceUsd": number | null, "gasUsd": number | null
  } | null,
  "validUntil": iso | null, "initiator": "agent" | "manual", "sessionKey": string | null,
  "createdAt": iso, "updatedAt": iso, "approvedAt": iso | null, "armedAt": iso | null,
  "triggeredAt": iso | null, "expiresAt": iso | null,
  "bracket": { "id": "brk_1a2b3c4d", "name": "Protect ETH", "leg": "tp" | "sl" } | null   // set on a bracket's leg (brackets.md)
}

Fire = { "n": 1, "at": iso, "manual": false,
         "status": "pending" | "filled" | "parked" | "alerted" | "skipped" | "failed" | "expired" | "rejected",
         "reasonCode": string | null, "reason": string | null,
         "priceUsd": number | null, "orderId": string | null, "txHash": string | null, "explorerUrl": string | null }
```

USD `null` means unknown, never `0`. `Chain`, `Token`, `Amount`, `Wallet`
are the LP card types (`dca.py` already has the builders; import them).

## RPC

Agent-callable (create only proposes; reads are harmless):

| method | params | returns |
|---|---|---|
| `trading.trigger.create` | `chainId`/`chain`, `kind`, `token`, `quote?`, `direction`, `price?` (string or number; required for below/above), `trailPct?` (required for trail), `amountUsd?`, `amountPct?`, `amount?`, `wallet?`, `slippagePct?`, `name?`, `validForSeconds?`, `sessionKey?` (operator only) | `trigger` payload |
| `trading.trigger.get` | `triggerId` | `trigger` payload |
| `trading.trigger.list` | `all?`, `wallet?` | `triggers` payload |

Operator-only (`@_operator_only`):

| method | params | returns |
|---|---|---|
| `trading.trigger.approve` | `triggerId` | payload (now `armed`) |
| `trading.trigger.reject` | `triggerId`, `reason?` | payload |
| `trading.trigger.pause` / `resume` / `stop` | `triggerId`, `reason?` (stop) | payload |
| `trading.trigger.fire` | `triggerId`, `wait?` | payload + `fire` |

Validation errors are `trading.trigger.invalid` naming the field; helpers
mirror `_dca_*` in `rpc_trading.py`.

## CLI

```
agentos trade trigger create <token> (--below <price|pct%> | --above <price|pct%> | --trail <pct>)
                             (--sell (--pct 50 | --amount 0.05 | --usd 100) | --buy --usd 50 | --alert)
                             [--quote USDC] [--chain base|robinhood] [--wallet ADDR|label] [--slippage 1]
                             [--name "…"] [--for 7d] [--json] [--no-card]
agentos trade trigger list    [--all] [--wallet …] [--json] [--no-card]
agentos trade trigger show    <id> [--json] [--no-card]
agentos trade trigger approve <id> [--json] [--no-card]
agentos trade trigger reject  <id> [--reason "…"] [--json] [--no-card]
agentos trade trigger pause | resume <id> [--json] [--no-card]
agentos trade trigger stop    <id> [--reason "…"] [--json] [--no-card]
agentos trade trigger fire    <id> [--wait --wait-seconds N] [--json] [--no-card]
```

- Exactly one of `--below` / `--above` / `--trail`; exactly one of `--sell` /
  `--buy` / `--alert`; `--sell` takes exactly one size, `--buy` takes
  `--usd`, `--alert` takes none. Checked in the CLI (exit 2) before the RPC.
- `--for` accepts `30m`, `2h`, `1d`, `1w` or seconds (`parse_every`, ≥ 60 s).
- `--json` prints the payload and, unless `--no-card`, writes
  `trigger-cards/<kind>-<slug>-<utc stamp>.json` (20 newest kept) and prints
  the marker `publish_artifact path=<file> mime=application/vnd.agentos.trigger+json`
  as the last line. The gateway auto-publishes `application/vnd.agentos.*`.
- Human output: a Rich panel (name, kind, status, condition, price now and
  distance, size, hits, recent fires, result) or a table for `list`.
- Exit codes as `trade dca`: 2 for input / `trading.trigger.invalid` /
  `bad_state` / `not_found` / `trading.invalid` / `trading.token_not_found`;
  1 for gateway errors (including `trading.operator_required`). Errors are
  JSON on stderr under `--json`, never a card.
- From an agent shell, `create` always answers `status: "awaiting_approval"`;
  the agent says the card has an **Approve & arm** button and never approves
  itself.

## Rendering (shared renderer, `frontend/src/views/chat/transcript/trigger.ts`)

Same skeleton as `dca.ts`: `isTriggerArtifact`, `normalizeTriggerPayload`,
`createTriggerMounter({ fetchPayload, call, actions })`, registered in
`artifacts.ts` (category `'trigger'`, placeholder
`.msg-artifact-trigger[data-trigger-src]`) and mounted from
`useTranscript.ts` next to `dcaMounter` (option `triggerActions`, same shape
as `dcaActions`). `trading.order.finished` and `trading.trigger.changed`
refresh mounted cards of the affected trigger.

### `kind = "trigger"` — the card

Root `article.trigger-card[data-trigger-kind=trigger][data-trigger-action=sell|buy|alert][data-trigger-status=…][data-trigger-chain=…][data-trigger-layout=wide|narrow]`.

1. **Header** `.trigger-card__head`: name, chain pill, status pill
   `.trigger-pill[data-status]` (`awaiting_approval` amber pulse, `armed`
   green, `triggered` blue pulse, `paused` grey, `done` blue, `stopped` /
   `rejected` / `expired` muted). Kind glyph: ▼ sell, ▲ buy, 🔔 alert (CSS
   via `data-trigger-action`, no emoji in the DOM).
2. **Hero** `.trigger-card__hero`: the sentence `sell 50 % of ETH when under
   $3,800` (large), under it the live line `.trigger-card__now`: `ETH $3,790 ·
   0.3 % above the line · checked 12 s ago` (or `armed, waiting for a
   price`, `fires after 1 more check` when `hits ≥ 1`, `done · sold 0.05 ETH
   at $3,788`, `awaiting approval`).
3. **Gauge** `.trigger-gauge`: a horizontal rail with the current price as a
   dot and the trigger price as a tick, labelled with both; for `trail`,
   the peak and the stop price. Pure SVG, no dependency; `data-trigger-dist`
   carries the signed distance for the skin to tint (near = amber).
4. **Facts** `.trigger-card__facts` (2×2): *size* (`50 % · ≈ $189` / `$50` /
   `—`), *wallet balance* (`0.1 ETH`), *valid until* (`GTC` / date),
   *on fire* (`trades at once` / `waits for you · over $100`; absent on an
   alert).
5. **Fires** `.trigger-fires`: last 5 as rows `#1 · 2 m ago · filled · $189
   → 0.05 ETH @ $3,788 · ↗`, `parked · awaiting approval`, `failed · <reason>`.
6. **Actions** `.trigger-actions` (only when `ctx.canWrite`, desktop):
   `awaiting_approval`: **Approve & arm** · Reject; `armed`: Pause · Fire
   now · Stop; `paused`: Resume · Fire now · Stop; `triggered`: Stop;
   terminal: none. Buttons carry `data-trigger-op=approve|reject|pause|resume|fire|stop`
   and `data-trigger-id`; a click calls `trading.trigger.<op>`, disables the
   row in flight, swaps in the returned payload; `fire` passes the fire's
   `orderId` to `actions.onOrder`. Stop and Fire now take a confirming second
   click. Errors inline in `.trigger-actions__error`.
7. **Warnings** `.trigger-card__warnings`, **Footer** `.trigger-card__foot`:
   `trg_1a2b3c4d · Key main (0x89e0…da97) · as of <relative>` · ↻ · copy id.

Live state as for DCA: render the snapshot with actions disabled
(`data-trigger-stale="checking"`), re-read at once through
`trading.trigger.get`, keep a per-id cache fed by reads, action responses and
`trading.trigger.changed`; re-read after `bad_state` / `not_found`.

### `kind = "triggers"` — the list

`article.trigger-card[data-trigger-kind=triggers]`: header `Triggers · 3`,
totals (`2 armed · 1 awaiting`), one row per trigger: status dot, name,
`sell 50 % ETH · under $3,800`, `ETH $3,790 · −0.3 %`, compact actions.
Empty → `No triggers yet.`

### Web vs desktop

The payload normalizers live in `frontend/src/views/chat/transcript/`. The
web console draws its own read-only React card from them
(`frontend/src/web/features/chat/cards/TradingCards.tsx`): it refreshes on
`trading.*.changed` but offers no actions. The desktop renders the full card
with its actions and styles it in
`desktop/src/renderer/src/views/chat/chat.css` as a desk instrument (rail,
mono numerals, tinted status). `canWrite` is true only on the desktop desk.

## Desktop desk

- **Store** (`stores/trading.ts`): `useTriggers(all, enabled)` on
  `trading.trigger.list`, query key `['trading', 'trigger']`, refreshed by
  `trading.changed` (already) and `trading.trigger.changed` (add to
  `TRADING_EVENTS`); `useTriggerActions()` → approve / reject / pause /
  resume / stop / fire with toasts (`trading.trigger.toast.*`).
- **Approvals region** lists pending triggers above pending mandates as a
  compact `TriggerCard` (what, when, size, wallet, expiry, warnings) with
  **Approve & arm** / Reject (Touch ID gate like `MandateCard`).
- **Missions panel** shows triggers beside mandates: name, state word
  (`Awaiting approval`, `Armed · ETH $3,790 · −0.3 %`, `Armed · 1 of 2
  checks`, `Triggered · order open`, `Paused`, `Done`), controls Pause /
  Resume, Fire now (two-click), Stop (two-click). Finished triggers stay 1 h.
- **Status strip** adds one chip: `Triggers ×2`, `Trigger · near` when any
  armed trigger is within 1 % of its line, `Trigger · fired` while one is
  `triggered`.
- **Notifications** (`use-notifications.ts`): on `trading.trigger.fired`
  post a native notification, kind `trade` (the main-process whitelist
  already carries it), target `{ type: 'trading', orderId? }`:
  - alert: title `ETH under $3,800`, subtitle `Alert · $3,790 now`;
  - sell/buy: title `Stop-loss ETH fired`, subtitle `selling 50 % of ETH at
    $3,790`; the order's own filled/approval notifications follow as usual;
  - skipped/failed: kind `tradeFailed`, body = the reason.
- **Desk ledger** (`ledger.ts`) recognises `agentos trade trigger <sub> …`
  and the `trigger-cards/` marker: titles `Stop-loss · ETH · under $3,800`,
  `Take-profit · …`, `Buy the dip · …`, `Price alert · …`, `Trigger status`,
  `Pause trigger`, `Fire now`, …
- **Orders** show `Stop-loss ETH · fired at $3,790` from the note; an
  order's `triggerId` links to the trigger.
- **Desk prompt** (`agent.ts`, bump the version): a new **Triggers**
  section. A conditional request — "sell if it drops under", "cut my loss",
  "take profit at", "buy when it dips to", "stop loss 10 %",
  "trailing stop", "tell me when", "alert me when" — is a trigger the engine
  watches; never poll the price yourself, never schedule a cron for it.
  Reading rules: "sell all my ETH if it drops under 3800" → `trigger create ETH
  --sell --pct 100 --below 3800 --json`; "stop loss 10 %" → `--sell --pct 100
  --below -10%`; "take profit at +20 %" → `--sell --pct 50 --above +20%` (ask the
  size only if truly absent, default 100 %); "buy $50 of ETH when it is back at 3500" →
  `--buy --usd 50 --below 3500`; "trailing stop 10 %" → `--sell --pct 100
  --trail 10`; "tell me when ETH reaches 5000" → `--alert --above 5000`. From an
  agent the result is `awaiting_approval`: report the card has **Approve &
  arm**, give the id (`trg_…`), stop. "how are my triggers" → `trigger list
  --json`, one line. Pause/stop/fire are the user's controls
  (`trading.operator_required`).

## Tests

- **Engine** (`tests/test_trading/test_triggers.py`, offline, fake clock,
  `FakePrices`): create as agent parks / as operator arms; relative price
  resolution and refusal without a price; default names; below/above/trail
  evaluation incl. peak tracking; two-tick confirmation and reset on a miss;
  unknown price neither fires nor resets; alert fires once and is done;
  sell by pct sizes from the balance at fire time; buy by usd; insufficient
  balance pauses at once; failed fire re-arms, three pause; parked order →
  confirmed done / rejected stopped / expired paused; stop rejects a parked
  order; validUntil expiry; resume resets hits/streak/peak; fire now;
  reconcile after restart; migration v7 → v8 keeps orders and mandates and
  adds `orders.trigger_id`; payload shape pins
  `tests/fixtures/trigger_cards/{trigger-armed,trigger-awaiting,trigger-done,trigger-alert,triggers,triggers-empty}.json`
  (regenerate with `AGENTOS_REGEN_TRIGGER_FIXTURES=1`).
- **RPC** (`tests/test_gateway/test_rpc_trading_triggers.py`): methods
  registered; agent create/get/list, `awaiting_approval`, `initiator` and
  `sessionKey` not honoured; every write `trading.operator_required` for an
  agent; validation codes; approve → armed.
- **CLI** (`tests/test_cli/test_trade_trigger_cmd.py`): flag exclusivity,
  size rules, `--for` parsing, marker last line, `--no-card`, pruning, JSON
  errors on stderr, exit codes, params sent.
- **Frontend** (`trigger.test.ts`): category, normalisation, each status
  layout, hero/now text, gauge geometry, actions hidden without `canWrite`,
  action → RPC → payload swap, refresh, CSS contract entries.
- **Desktop**: `TriggerCard` approve/reject, Missions rows for triggers,
  `ledger.ts` parsing, notification mapping for `trading.trigger.fired`,
  store hooks, CSS skin test.
- **Live** (dust wallet, Base, ≈ $0.10 + gas): `trigger create USDC --alert
  --above 0.5 --json` fires within two ticks and posts a notification;
  `trigger create USDC --sell --usd 0.04 --above 0.5 --quote ETH` fires and
  fills; `trigger create USDC --buy --usd 0.04 --below 2 --quote ETH` fires
  and fills; `trigger create ETH --sell --pct 1 --below 100` stays armed with
  the right distance and is stopped; a trailing alert records the peak.
