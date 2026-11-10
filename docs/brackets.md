# Brackets: take-profit + stop-loss on one position (`application/vnd.agentos.trigger+json`, kind `bracket`)

A **bracket** protects a position with two price triggers at once: *sell my
ETH when it is up 20 %, or when it is down 10 %, whichever comes first*. The
two **legs** are ordinary price triggers (`docs/triggers.md`) that know
about each other: when one fires and fills, the other is stopped (one
cancels the other, "OCO"). Without a bracket the user has to create a
take-profit and a stop-loss separately and remember to cancel the survivor;
if they forget, the survivor fires into an empty wallet and pauses with
*nothing to sell*.

A bracket is **not** a new kind of engine object. It is a group of two
trigger rows that share a `group_id`; everything a trigger already does
(confirmation on two ticks, the fire rows, the guardrails, the approval of
large orders, the ledger, the cards) applies to each leg unchanged. What is
new is the group: it is created, approved, paused, resumed, stopped and
shown **as one thing**, and the engine keeps its two legs consistent.

This file is the contract between the engine, the gateway, the CLI, the
shared renderer and the desktop. Change it before changing any of them. It
extends `docs/triggers.md`; whatever it does not say, that file says.

Chains: Base (8453) and Robinhood Chain (4663). Kinds: `sell` (the bracket
sells the position) and `alert` (a range alert: *tell me when ETH leaves
$3,420–$4,560*). Not in scope: buy brackets (OCO entries), more than two
legs, ladders, editing a live bracket (stop and create again), moving the
stop to break-even by itself.

## Vocabulary

| word | meaning |
|---|---|
| **bracket** | the group: one name, one status, one card, two legs |
| **leg** | one of the two triggers: the **take-profit** leg (`tp`, fires `above`) and the **stop-loss** leg (`sl`, fires `below` or `trail`). A range alert's legs are its **ceiling** (`tp`) and **floor** (`sl`): every leg word below reads so for `kind = alert` (`triggers.leg_word(leg, kind)`) |
| **sibling** | the other leg |
| **on hold** | a leg paused by the engine because its sibling is firing; it is released or stopped when the sibling's fire settles |
| **line** | a leg's price: the take-profit line and the stop line (a trail's moves with the peak) |

Default names (`--name` renames the bracket only; the legs keep theirs):

| kind | bracket | take-profit leg | stop-loss leg |
|---|---|---|---|
| sell | `Protect ETH` | `Take-profit ETH` | `Stop-loss ETH` (`Trailing stop ETH` with `--trail`) |
| alert | `Watch ETH` | `Alert ETH over $4,560` | `Alert ETH under $3,420` (`Alert ETH −10 % from peak`) |

## Lifecycle

A bracket has no row of its own; its **status is derived from its legs**,
by precedence:

```
triggered > awaiting_approval > armed > paused > done > stopped > rejected > expired
```

The first status in that order that any leg has is the bracket's.
`statusReason` is the deciding leg's `statusReason`, prefixed with the leg
word when the two legs are in different states (`"take-profit: sold 0.05
ETH for 230 USDC at $4,560"`, `"stop-loss paused: nothing to sell"`,
`"take-profit filled · stop-loss guards the rest"`). The engine helper is
`triggers.bracket_status(legs)`; it is pure and tested.

```
create ──(operator)────────────► armed ──(a leg fires)──► triggered ──► done      (leg filled / alerted; sibling stopped)
   └────(agent)──► awaiting_approval ┤                        │
                        │            │                        ├─► armed     (fire failed / order rejected or expired: sibling released)
                        ├─► rejected │                        └─► paused    (nothing to sell: both legs paused)
                        └─► expired  ├─► paused ⇄ armed   (user: both legs)
                                     ├─► stopped          (user: both legs; a parked order is rejected)
                                     └─► expired          (validUntil: both legs)
```

- **Creation** writes both legs in **one ledger transaction**
  (`insert_triggers`), both with the same `group_id` (`"brk_" + 8 hex`),
  `group_name`, `wallet`, `token`, `quote`, `slippage_pct`, `valid_until`,
  `initiator`, `session_key`; `leg` is `"tp"` / `"sl"`. An agent-bound
  connection gets both legs `awaiting_approval` (one proposal, one card, one
  **Approve & arm**); the operator gets both `armed`. `initiator` is never
  trusted from params.
- **Approve / reject** act on both legs (`approve` arms both at the price
  then; the pending TTL is the trigger's 24 h). **Pause** pauses every
  `armed` leg (`"user"`); it is `bad_state` while a leg is `triggered`.
  **Resume** re-arms every `paused` leg (hits, streak and a trail's peak
  reset as for a trigger). **Stop** stops every live leg and rejects a
  parked order of either, as `trigger_stop` does.
- **Leg-level writes are refused.** `trading.trigger.approve|reject|pause|
  resume|stop|fire` on a leg answer `trading.trigger.bad_state` (*"trg_… is
  the take-profit leg of bracket brk_…: use trading.bracket.*"*). Reads
  (`trading.trigger.get`) work and show the leg with its `bracket` field.
- **`trading.trigger.list` does not list legs** (nor count them in
  `totals`); a bracket is listed by `trading.bracket.list`. The desk's
  Missions therefore see one row per bracket, never three.
- **Checking** is the trigger's: each armed leg is evaluated every tick with
  the same two-tick confirmation. Nothing is added to `trigger_check()`.

### One cancels the other

All of this happens inside the trigger runner (`_trigger_fire`,
`_trigger_after_fire`), under the trigger lock, for a leg that has a
`group_id`:

1. **When a leg is claimed** (`armed` → `triggered`, manual or not), its
   sibling, if `armed`, is put **on hold**: `paused` with `status_reason =
   "on hold: take-profit fired"` (`triggers.OCO_HOLD` prefix, naming the
   fired leg). A sibling that is already `paused` (by the user or by a
   failure) is left alone. The sibling never fires while its twin's order is
   open: there is one position and one order on it at a time.
2. **When the fire settles:**
   - `filled` / `alerted` → the leg is `done`; the sibling is **stopped**
     from any live status (`"take-profit filled"` / `"stop-loss filled"` /
     `"range left over the top"` / `"range left under the floor"`,
     `triggers.oco_stopped_reason`). The bracket is `done`.
   - **Partial take-profit** (`tp_pct` under the position's `amount_pct`, see
     *Sizing*): a `filled` take-profit leg is `done`, but the stop-loss leg
     is **released** instead of stopped (it guards the rest: `amount_pct`
     of what is left). The bracket stays `armed` with reason
     `"take-profit filled · stop-loss guards the rest"`. A filled stop-loss
     still stops the take-profit (the stop sells the whole size).
   - `failed`, or a parked order `rejected` by the user or `expired` waiting:
     the leg goes where a trigger goes (`armed` / `paused` / `stopped`); a
     sibling **on hold is released**: `armed` again, `hits = 0`, `bad_streak
     = 0`, a trail's peak reset to the price now (`triggers.release`). A
     sibling the user had paused stays paused.
   - `skipped` (insufficient balance): the leg pauses as a trigger does; a
     sibling on hold is **paused the same way** (`"paused: nothing to
     sell"`): it would find the same empty wallet. The bracket is `paused`;
     resume re-arms both.
3. **Fire now** on a bracket (`trading.bracket.fire`) fires one leg by hand:
   the `leg` given, else the **nearest** leg (the smaller `|distancePct|`
   at the last price; `sl` when no price is known). The sibling goes on hold
   as in 1. The desk words it *Sell now* (`sell`) / *Notify now* (`alert`).
4. A release or a stop of the sibling is a trigger state change: it emits
   `trading.trigger.changed` for the sibling and `trading.bracket.changed`
   once for the group.

Restart safety is the trigger's: the hold is a row (`paused` + reason), the
fire row is written before the order, and `_trigger_reconcile` settles the
stray fire, which runs step 2.

### Sizing

Both legs sell the same position: exactly one of `amountPct` (default
`100` when no size is given), `amount` (token units) or `amountUsd`, copied
to both legs. `tpPct` (sell, with `amountPct` only) makes the take-profit
leg sell a smaller share: `tpPct` ≤ `amountPct`, `0 < tpPct`; equal to
`amountPct` it is dropped. An alert bracket has no size.

### Lines

`takeProfit` is a price or a percent **over** the price now (`4560`,
`"+20%"`, `"20%"`), resolved at creation like a trigger's `--above`.
`stopLoss` is a price or a percent **under** it (`3420`, `"-10%"`), like
`--below`; or `trailPct` makes the stop leg a trailing stop (exactly one
of `stopLoss` / `trailPct`). After resolution the take-profit line must be
**above** the stop line (`trading.bracket.invalid`: *"take-profit $3,000
must be above stop-loss $3,400"*); a trail's initial stop (`price × (1 −
trailPct/100)`) is compared when the price is known. A percent with no
price known is refused as for a trigger. The price now **between** the
lines is the normal case; outside, the creation warnings of the leg that is
already met say so (*"ETH is already at $4,700, over $4,560: the
take-profit fires after the next two checks"*). Both legs' warnings are the
bracket's, prefixed with the leg word.

Quote and wallet rules are the trigger's: `quote` defaults to the chain's
USDC (the native coin when the token is USDC itself), is required on a
chain without a canonical USDC for a `sell` bracket, never refused for an
`alert`; `wallet` defaults to the primary wallet and may not be `all`.

## Ledger (schema v9)

```sql
ALTER TABLE triggers ADD COLUMN group_id   TEXT;   -- "brk_" + 8 hex; NULL for a plain trigger
ALTER TABLE triggers ADD COLUMN group_name TEXT;   -- the bracket's name, on both legs
ALTER TABLE triggers ADD COLUMN leg        TEXT;   -- 'tp' | 'sl' | NULL
CREATE INDEX IF NOT EXISTS idx_triggers_group ON triggers(group_id) WHERE group_id IS NOT NULL;
```

Migration v8 → v9 adds the three columns (`_add_trigger_group_columns`);
existing triggers are untouched (`group_id IS NULL`). Ledger API:

```python
def insert_triggers(self, rows: list[dict]) -> None                 # one transaction
def list_triggers(self, *, statuses=None, wallet=None, legs="exclude")   # "exclude" | "only" | "all"
def group_triggers(self, group_id: str) -> list[dict]               # the legs, tp first
def list_groups(self, *, statuses=None, wallet=None) -> list[str]    # group ids, newest first; statuses: any leg in
```

`list_triggers(legs="exclude")` is the default so every existing caller
(the checker, `trigger_list`, the reconciler) keeps working; the checker
and the expirers call it with `legs="all"` so legs are evaluated and
expired like any trigger.

`triggers.py` gains the pure helpers: `BRACKET_ID_PREFIX = "brk_"`,
`new_bracket_id()`, `LEGS = ("tp", "sl")`, `OCO_HOLD = "on hold: "`,
`hold_reason(leg_name)`, `oco_stopped_reason(kind, fired_leg)`,
`bracket_default_name(kind, symbol)`, `leg_word(leg)` (`"take-profit"` /
`"stop-loss"`), `bracket_status(legs) -> (status, status_reason)`,
`validate_bracket_terms(...)`, `bracket_json(...)`, `bracket_payload(...)`,
`brackets_payload(...)`, `nearest_leg(legs)`. The runner on
`TradingService`:

```python
async def bracket_create(self, *, chain, kind, token, quote, take_profit, stop_loss, trail_pct,
                         amount_usd, amount_pct, amount, tp_pct, wallet, slippage_pct, name,
                         valid_for_seconds, initiator, session_key) -> dict     # payload kind="bracket"
async def bracket_get(self, bracket_id) -> dict
async def bracket_list(self, *, all=False, wallet=None) -> dict                 # kind="brackets"
async def bracket_approve(self, bracket_id) -> dict
async def bracket_reject(self, bracket_id, reason=None) -> dict
async def bracket_pause(self, bracket_id) -> dict
async def bracket_resume(self, bracket_id) -> dict
async def bracket_stop(self, bracket_id, reason=None) -> dict
async def bracket_fire_now(self, bracket_id, *, leg=None, wait=False) -> dict  # payload + "fire": Fire
```

Errors: `trading.bracket.not_found`, `trading.bracket.bad_state`,
`trading.bracket.invalid` (unknown kind; `takeProfit` missing; neither or
both of `stopLoss` / `trailPct`; a size on `alert`; more than one size;
`tpPct` without `amountPct`, over it, or ≤ 0; take-profit not above the
stop; the trigger's price/percent/trail/validFor/quote/wallet refusals,
same wording), `trading.operator_required` (RPC), plus `trading.disabled`,
`wallet.locked`, `trading.unsupported_chain`, `trading.token_not_found`.

Events: every leg change emits the trigger's events **and**
`trading.changed {reason: "bracket", bracketId}` +
`trading.bracket.changed {bracket}` (the full `Bracket`). A fire emits
`trading.trigger.fired {triggerId, trigger, fire}` as before; the `trigger`
carries its `bracket` field so a notification can say *Protect ETH ·
take-profit fired*.

## Payload

The trigger mime carries two more kinds:

```jsonc
Envelope = { "version": 1, "kind": "trigger" | "triggers" | "bracket" | "brackets",
             "fetchedAt": iso, "warnings": [string],
             "request": { "kind": "get" | "list", "params": {…} } }   // ↻ re-runs trading.bracket.<kind>

{ ...Envelope, "bracket": Bracket, "fire": Fire | undefined }          // kind = "bracket"
{ ...Envelope, "brackets": [Bracket],                                  // kind = "brackets": live first, then newest
  "totals": { "count": 2, "armed": 1, "awaiting": 1, "triggered": 0 } }

Bracket = {
  "id": "brk_1a2b3c4d", "name": "Protect ETH",
  "kind": "sell" | "alert",
  "status": "awaiting_approval" | "armed" | "triggered" | "paused" | "done" | "stopped" | "rejected" | "expired",
  "statusReason": string | null,
  "chain": Chain, "wallet": Wallet, "token": Token, "quote": Token,
  "takeProfit": Trigger,                  // the tp leg, full trigger object (docs/triggers.md), with "bracket" set
  "stopLoss": Trigger,                    // the sl leg
  "lines": {
    "takeProfitUsd": number | null,       // the tp line
    "stopLossUsd": number | null,         // the sl line now: the threshold, or a trail's peak × (1 − trailPct/100)
    "trailPct": number | null,
    "fromPriceUsd": number | null,        // the price at creation when a percent was given on either leg
    "takeProfitLabel": "over $4,560",
    "stopLossLabel": "under $3,420" | "10 % below peak"
  },
  "action": {
    "kind": "sell" | "alert",
    "amountPct": number | null, "amount": Amount | null, "amountUsd": number | null,
    "tpPct": number | null,               // partial take-profit share, when under amountPct
    "estimatedUsd": number | null,        // what the stop-loss leg would move now (the whole size); terminal: what the filled leg moved
    "slippagePct": number | null,
    "needsApproval": boolean, "approvalThresholdUsd": number, "dailyCapUsd": number,
    "label": "sell 100 % of ETH → USDC" | "sell 50 % of ETH at take-profit, 100 % at stop → USDC" | "notify"
  },
  "market": {
    "priceUsd": number | null, "armedPriceUsd": number | null, "checkedAt": iso | null,
    "balance": Amount | null,             // the wallet's token balance now (sell); null for alert / terminal
    "upsidePct": number | null,           // +20.3: % rise to the tp line; 0 when met
    "downsidePct": number | null,         // −9.8: % fall to the sl line; 0 when met
    "positionPct": number | null,         // where the price sits between the lines: 0 = at the stop, 100 = at the take-profit, clamped
    "rewardRisk": number | null,          // upsidePct / |downsidePct|, one decimal; null when either is 0 or unknown
    "nearest": "tp" | "sl" | null         // the leg closer to firing
  },
  "fired": "tp" | "sl" | null,            // the leg whose fire ended (or, partial tp, advanced) the bracket
  "result": Result | null,                // the filled leg's result (docs/triggers.md), else null
  "validUntil": iso | null, "initiator": "agent" | "manual", "sessionKey": string | null,
  "createdAt": iso, "updatedAt": iso, "approvedAt": iso | null, "armedAt": iso | null, "expiresAt": iso | null
}
```

`Trigger` (every trigger payload, legs or not) gains one field:

```jsonc
"bracket": { "id": "brk_1a2b3c4d", "name": "Protect ETH", "leg": "tp" | "sl" } | null
```

USD `null` means unknown, never `0`. The `fires` of a bracket are read from
its legs (`takeProfit.fires`, `stopLoss.fires`); the card merges them,
newest first, tagged with the leg.

## RPC

Agent-callable (create only proposes; reads are harmless):

| method | params | returns |
|---|---|---|
| `trading.bracket.create` | `chainId`/`chain`, `token`, `takeProfit` (string or number), `stopLoss?` (string or number), `trailPct?`, `kind?` (`sell` default, `alert`), `quote?`, `amountPct?`, `amount?`, `amountUsd?`, `tpPct?`, `wallet?`, `slippagePct?`, `name?`, `validForSeconds?`, `sessionKey?` (operator only) | `bracket` payload |
| `trading.bracket.get` | `bracketId` | `bracket` payload |
| `trading.bracket.list` | `all?`, `wallet?` | `brackets` payload |

Operator-only (`@_operator_only`):

| method | params | returns |
|---|---|---|
| `trading.bracket.approve` | `bracketId` | payload (now `armed`) |
| `trading.bracket.reject` | `bracketId`, `reason?` | payload |
| `trading.bracket.pause` / `resume` / `stop` | `bracketId`, `reason?` (stop) | payload |
| `trading.bracket.fire` | `bracketId`, `leg?` (`tp` / `sl`), `wait?` | payload + `fire` |

Validation errors are `trading.bracket.invalid` naming the field; the
helpers mirror `_trigger_*` in `rpc_trading.py`. The structural rules
(`takeProfit` required; exactly one of `stopLoss` / `trailPct`; one size at
most; `tpPct` only with `amountPct`; no size on `alert`) are checked in
the RPC layer, the values against the market in the engine.

## CLI

```
agentos trade protect <token> --tp <price|pct%> (--sl <price|pct%> | --trail <pct>)
                      [--pct 100 | --amount 0.05 | --usd 100] [--tp-pct 50] [--alert]
                      [--quote USDC] [--chain base|robinhood] [--wallet ADDR|label] [--slippage 1]
                      [--name "…"] [--for 7d] [--json] [--no-card]
agentos trade bracket list    [--all] [--wallet …] [--json] [--no-card]
agentos trade bracket show    <id> [--json] [--no-card]
agentos trade bracket approve <id> [--json] [--no-card]
agentos trade bracket reject  <id> [--reason "…"] [--json] [--no-card]
agentos trade bracket pause | resume <id> [--json] [--no-card]
agentos trade bracket stop    <id> [--reason "…"] [--json] [--no-card]
agentos trade bracket fire    <id> [--leg tp|sl] [--wait --wait-seconds N] [--json] [--no-card]
```

- `trade protect` is the creating verb (it reads as the user speaks: *protect
  my ETH*); `trade bracket …` manages one. `--tp` is required; exactly one
  of `--sl` / `--trail`; at most one of `--pct` / `--amount` / `--usd`
  (none → `--pct 100`); `--tp-pct` needs `--pct` (or the default) and must
  not exceed it; `--alert` takes no size. Checked in the CLI (exit 2)
  before the RPC. `--tp` accepts `4560`, `+20%`, `20%`; `--sl` accepts
  `3420`, `-10%`, `10%` (`parse_trigger_price` with `--above` / `--below`
  rules).
- `--for` as for a trigger (`parse_every`, ≥ 60 s).
- `--json` prints the payload and, unless `--no-card`, writes
  `trigger-cards/<bracket|brackets>-<slug>-<utc stamp>.json` (same folder,
  same 20-newest pruning, `_TRIGGER_CARD_FILE` accepts the two new prefixes)
  and prints `publish_artifact path=<file>
  mime=application/vnd.agentos.trigger+json` as the last line.
- Human output: a Rich panel (name, kind, status, the two lines with the
  price now between them, upside / downside / reward:risk, size, balance,
  each leg's checks, recent fires of both legs, result) or a table for
  `list` (name, status, token, price now, `+20 % / −10 %`, size).
- Exit codes as `trade trigger`: 2 for input / `trading.bracket.invalid` /
  `bad_state` / `not_found` / `trading.invalid` / `trading.token_not_found`;
  1 for gateway errors. JSON errors on stderr under `--json`, never a card.
- From an agent shell `protect` always answers `status: "awaiting_approval"`;
  the agent says the card has one **Approve & arm** button for both legs and
  never approves itself.

## Rendering (shared renderer, `frontend/src/views/chat/transcript/trigger.ts`)

`trigger.ts` handles the two new kinds in the same module (one mime, one
mounter, one registration): `TriggerKind` becomes `'trigger' | 'triggers'
| 'bracket' | 'brackets'`, `normalizeTriggerPayload` returns a
`BracketOnePayload` / `BracketListPayload`, `triggerReadMethod` maps a
bracket request to `trading.bracket.<kind>`, and `trading.bracket.changed`
refreshes mounted cards of the affected bracket (the mounter gains
`bracketChanged(payload)`, wired in `useTranscript.ts` next to
`triggerChanged`). Controls reuse `TriggerActions.call` with the
`trading.bracket.<op>` methods; no new option on the transcript hook.

### `kind = "bracket"` — the card

Root `article.trigger-card[data-trigger-kind=bracket][data-trigger-action=sell|alert][data-trigger-status=…][data-trigger-chain=…][data-trigger-layout=wide|narrow][data-trigger-nearest=tp|sl]`.
Every hook below is **exact**: the desktop skins these selectors blind.

1. **Header** `.trigger-card__head`: name, chain pill, status pill
   `.trigger-pill[data-status]` as a trigger's. Kind glyph via
   `data-trigger-action` (▼▲ pair for sell, 🔔 for alert, CSS only).
2. **Hero** `.trigger-card__hero`: `sell 100 % of ETH · take profit over
   $4,560 · stop under $3,420` (alert: `notify when ETH is over $4,560 or
   under $3,420`); under it `.trigger-card__now`: `ETH $3,790 · +20.3 % to
   take-profit · −9.8 % to stop · checked 12 s ago`, or `armed, waiting for
   a price`, `take-profit fires after 1 more check`, `awaiting approval`,
   `done · take-profit: sold 0.05 ETH at $4,560`, `on hold · stop-loss
   order open`.
3. **Range gauge** `.trigger-gauge[data-trigger-gauge=range]`: one rail
   `.trigger-gauge__line`, the stop line as a tick at the left
   `.trigger-gauge__tick[data-leg=sl]`, the take-profit line as a tick at
   the right `.trigger-gauge__tick[data-leg=tp]`, the price as the dot
   `.trigger-gauge__dot`, the span between the ticks as
   `.trigger-gauge__zone[data-zone=bracket]`; labels
   `.trigger-gauge__label[data-leg=sl|tp]` and `.trigger-gauge__label[data-leg=now]`.
   Pure SVG, `data-trigger-dist` on the root carries the nearest leg's
   signed distance for the skin to tint (near = amber).
4. **Legs** `.bracket-legs`: two rows `.bracket-leg[data-leg=tp|sl][data-status=…]`:
   leg word `.bracket-leg__word` (*Take-profit* / *Stop-loss* / *Trailing
   stop*; an alert's *Ceiling* / *Floor*), line `.bracket-leg__line` (`over
   $4,560`), state `.bracket-leg__state` (`armed · 1 of 2 checks`, `on hold`,
   `stopped · take-profit filled`, `done · sold …`). A leg that is over
   (`done` / `stopped` / `rejected` / `expired`) has no distance: the
   now-line says what happened (`take-profit filled · −9.8 % to stop`), the
   list row prints `—` for it, it is never `data-trigger-nearest`, and
   reward : risk is `—`.
5. **Facts** `.trigger-card__facts` (2×2): *size* (`100 % · ≈ $189` / `50 %
   at take-profit, 100 % at stop`; the `≈ $…` part sits on its own line in
   `.trigger-fact__sub`, and a fact whose value is over 18 characters carries
   `data-trigger-fact-span="2"` so the skin gives it two columns and nothing
   ever overflows a column), *balance*, *reward : risk* (`2.1 : 1` with
   `.trigger-fact__rr`), *on fire* (`trades at once` / `waits for you · over
   $100`; absent on an alert, whose size already says `notify only`); *valid
   until* joins when set.
6. **Fires** `.trigger-fires`: both legs' fires merged, newest first, ≤ 5,
   each row prefixed with the leg word (`take-profit #1 · 2 m ago · filled …`).
   Row actions on the `brackets` (and `triggers`) list card recount the
   totals line from the rows after every swap.
7. **Actions** `.trigger-actions` (only with `canWrite`): `awaiting_approval`:
   **Approve & arm** · Reject; `armed`: Pause · Sell now (alert: Notify now)
   · Stop; `paused`: Resume · Sell now · Stop; `triggered`: Stop; terminal:
   none. Buttons carry `data-trigger-op=approve|reject|pause|resume|fire|stop`
   and `data-bracket-id`; a click calls `trading.bracket.<op>`, disables the
   row in flight, swaps in the returned payload; `fire` passes the fire's
   `orderId` to `actions.onOrder`. Sell now and Stop take a confirming
   second click (`data-trigger-confirm`). Errors in `.trigger-actions__error`.
8. **Warnings** `.trigger-card__warnings`, **Footer** `.trigger-card__foot`:
   `brk_1a2b3c4d · Key main (0x89e0…da97) · as of <relative>` · ↻ · copy id.

Live state as for a trigger (`data-trigger-stale="checking"`, re-read
through `trading.bracket.get`, per-id cache fed by reads, action responses
and `trading.bracket.changed`).

### `kind = "brackets"` — the list

`article.trigger-card[data-trigger-kind=brackets]`: header `Brackets · 2`,
totals, one row per bracket `.trigger-row[data-bracket-id]`: status dot,
name, `sell 100 % ETH · $3,420 – $4,560`, `ETH $3,790 · +20 % / −10 %`,
compact actions. Empty → `No brackets yet.`

### A leg's own card

A trigger card whose `trigger.bracket` is set shows a line in the header,
`.trigger-card__group` (`take-profit leg of Protect ETH · brk_1a2b3c4d`),
and **no** `.trigger-actions` (leg writes are refused).

### Web vs desktop

As for triggers: the web console shows a read-only card
(`frontend/src/web/features/chat/cards/TradingCards.tsx`); the desktop renders
the full card and styles it in `desktop/src/renderer/src/views/chat/chat.css`
(range gauge: stop tick red-tinted, take-profit tick green-tinted, zone
faint; legs strip as two mono rows). `canWrite` is true only on the desk.

## Desktop desk

- **Types** (`views/trading/types.ts`): `Bracket`, `BracketPayload`,
  `BracketListPayload`, `BracketLeg = 'tp' | 'sl'`; `Trigger.bracket`.
- **Store** (`stores/trading.ts`): `useBrackets(all, enabled)` on
  `trading.bracket.list`, key `['trading', 'bracket', all ? 'all' :
  'live']`, refreshed by `trading.changed` and `trading.bracket.changed`
  (add to `TRADING_EVENTS`); `useBracketActions()` → approve / reject /
  pause / resume / stop / fire(bracket, leg?) with toasts
  (`trading.bracket.toast.*`), Touch ID through `bracketTouchId(bracket)`
  in `touch-id.ts` (the ask names both lines and the size).
- **Approvals region**: pending brackets above pending triggers as a
  `BracketCard` (name, the two lines, size, wallet, expiry, both legs'
  warnings) with **Approve & arm** / Reject (one decision for both legs).
- **Missions panel** (`missions.ts`, `MissionControls.tsx`): a bracket is one
  row: name, state word (`Awaiting approval`, `Armed · ETH $3,790 · +20 % /
  −10 %`, `Take-profit · 1 of 2 checks`, `Triggered · stop-loss order open`,
  `Paused`, `Done · take-profit`), controls Pause / Resume, Sell now
  (two-click; alert: Notify now), Stop (two-click). Finished brackets stay
  1 h. Legs never appear as rows of their own.
- **Status strip**: brackets count into the trigger chip (`Triggers ×3`
  counts a bracket once); `Trigger · near` when either line is within 1 %;
  `Trigger · fired` while a leg is `triggered`.
- **Notifications** (`notifications/logic.ts`): a `trading.trigger.fired`
  whose `trigger.bracket` is set is titled `Protect ETH · take-profit fired`
  (`notify.bracket.fired.title`), subtitle as a trigger's; an alert leg:
  `Watch ETH · over $4,560`.
- **Desk ledger** (`ledger.ts`): `agentos trade protect …` → row kind
  `bracket_create`, title `Protect · ETH · +20 % / −10 %` (or the prices);
  `trade bracket <sub>` → `bracket_list|bracket_approve|…`; the
  `trigger-cards/bracket-…` marker is a bracket card.
- **Chat wiring** (`ChatView.tsx`): the Touch ID gate before a bracket
  `approve` / `fire` (`requireBracketTouchId`), beside the trigger one.
- **Desk prompt** (`agent.ts`, bump `TRADING_AGENT_VERSION`): a
  **Brackets** section. A request for both an exit above and an exit below
  on a position — "protect my ETH", "protect the position", "take profit
  20 %, stop loss 10 %", "take profit at 4,500 and stop at 3,400", "sell half
  at +20 %, stop at −10 %", "tell me if ETH leaves 3,400–4,500" — is **one bracket**,
  never two triggers: `agentos trade protect ETH --tp +20% --sl -10% --json`
  (default `--pct 100`); "take profit on half" → `--tp-pct 50`; "trailing" on
  the stop → `--trail 10`; a range alert → `--alert`. From an agent the
  result is `awaiting_approval`: report the card has one **Approve & arm**
  for both legs, give the id (`brk_…`), stop. A single-sided request stays a
  trigger. "how are my brackets" → `bracket list --json`, one line.

## Docs and skills

`docs/cli.md` (`trade protect` / `trade bracket` beside `trade trigger`),
the bundled `agentos` skill `SKILL.md` (CLI surface lines), the
`wallet-trading` skill `SKILL.md` (the reading rules above, the command
lines), and `docs/triggers.md` (scope line: OCO pairs now live in
`brackets.md`; `Trigger.bracket`; `list` excludes legs; leg writes refused).

## Tests

- **Engine** (`tests/test_trading/test_brackets.py`, offline, the `World`
  of `test_triggers.py`): create as operator arms both / as agent parks
  both in one card; default and given names; percent lines resolve; tp not
  above sl refused; `tpPct` rules; alert bracket; `trigger.list` hides
  legs; leg writes refused; status precedence (`bracket_status`); tp claim
  holds sl; tp filled → sl stopped, bracket done, result from the tp leg;
  sl filled → tp stopped; partial tp → sl released, bracket armed with the
  reason; failed fire releases the hold; parked order rejected → leg
  stopped, sibling released; expired waiting → leg paused, sibling
  released; skipped → both paused; a user-paused sibling is not released;
  fire now nearest / given leg; pause / resume / stop / approve / reject act
  on both; validUntil expires both; reconcile after restart; migration v8 →
  v9 keeps triggers; fixtures `tests/fixtures/trigger_cards/{bracket-armed,
  bracket-awaiting,bracket-done,bracket-alert,brackets,brackets-empty}.json`
  (regenerate with `AGENTOS_REGEN_TRIGGER_FIXTURES=1`).
- **RPC** (`tests/test_gateway/test_rpc_trading_brackets.py`): registered;
  agent create/get/list, `awaiting_approval`; every write
  `trading.operator_required` for an agent; validation codes; approve → armed.
- **CLI** (`tests/test_cli/test_trade_protect_cmd.py`): flag rules, default
  size, `--tp-pct`, `--for`, marker last line, pruning accepts the new
  prefixes, JSON errors on stderr, exit codes, params sent.
- **Frontend** (`trigger.test.ts` or `bracket.test.ts`): category and
  normalisation of the two kinds, each status layout, hero/now text, range
  gauge geometry (dot between the ticks, clamped outside), legs strip,
  merged fires, actions hidden without `canWrite`, action → RPC → payload
  swap, `bracketChanged` refresh, a leg's card shows the group line and no
  actions, CSS contract entries.
- **Desktop**: `BracketCard` approve/reject, Missions rows, `ledger.ts`
  parsing, notification title, store hooks, status chip, CSS skin test.
- **Live** (dust wallet, Base, ≈ $0.10 + gas): `trade protect USDC --tp 0.5
  --sl 0.1 --usd 0.04 --quote ETH --json` → the take-profit leg fires
  within two ticks and fills, the stop-loss leg is `stopped · take-profit
  filled`, the bracket `done`; `trade protect ETH --tp +20% --sl -10% --pct
  1` stays armed with `upsidePct ≈ 20`, `downsidePct ≈ −10`, `positionPct ≈
  33`, survives pause / resume, and is stopped; `trade protect USDC --tp
  0.5 --sl 0.1 --alert` fires the alert leg and is `done`; `trade protect
  USDC --tp 0.5 --sl 0.1 --pct 1 --tp-pct 0.5 --quote ETH` fills the
  take-profit and leaves the stop-loss armed for the rest.
