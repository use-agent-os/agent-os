# DCA mandates (`application/vnd.agentos.dca+json`)

A **DCA mandate** is a recurring buy the trading engine owns and runs by
itself: *buy $10 of ETH with USDC on Base every day until $300 is spent*.
The user approves the mandate once; every buy after that is an ordinary
swap order through the existing pipeline (guardrails, approval threshold,
daily cap, vault signing, ledger). No LLM turn is spent on a buy, no prompt
is asked to count a budget: the cap, the schedule and the stop rules are
rows in the ledger and are enforced there.

The mandate is shown in the chat as a **card** (same mechanism as the LP
cards in `docs/lp-cards.md`): the agent runs `agentos trade dca … --json`,
the CLI publishes the payload as an inline artifact, the transcript renders
it. On the desktop the card carries the controls (approve, pause, resume,
buy now, stop); on the web console the card is read-only (the web UI is
agent-bound and cannot approve anything).

This file is the contract between the engine, the gateway, the CLI, the
shared renderer and the desktop. Change it before changing any of them.

Chains: Base (8453) and Robinhood Chain (4663). Not in scope: selling
schedules, ladders/grids, cross-chain, more than one token per mandate.

## Why an engine object

Before this, a "DCA" on the desk was a cron job whose prompt asked the
agent to swap once per run and to sum the notes of previous orders to stay
under a budget (`desk-logic.ts` `composeMissionPrompt`). That has three
problems: the budget is a request, not a limit; the state (spent, runs,
next buy) is not readable anywhere without re-deriving it from prose; and
every buy costs an agent turn. A mandate fixes all three. Old cron-based DCA
missions keep working unchanged; the desk's DCA presets now create
mandates.

## Lifecycle

```
create ──(operator)──────────────► active ──► completed   (cap reached / runs reached)
   └────(agent)──► awaiting_approval ─┤   ├─► stopped     (user)
                        │             │   └─► paused ⇄ active  (user, or auto-pause after 3 bad runs)
                        ├─► rejected  │
                        └─► expired   │  (24 h without a decision)
```

- **Who may create.** Any connection. An **agent-bound** connection (see
  `gateway/agent_surface.py`) always gets `awaiting_approval`; the operator
  (desktop, CLI without agent markers) gets `active` at once. The `initiator`
  param is never trusted, exactly as for orders.
- **Approval** of a pending mandate is operator-only (`trading.dca.approve`,
  desktop card button, `agentos trade dca approve`). A pending mandate
  expires **24 h** after creation (`expires_at`), not after the order TTL:
  a proposal should survive the user being away.
- **Runs.** A run is due when `now >= next_run_at`. The engine's
  housekeeping pass (`TradingService.housekeep()`, every
  `sync_interval_seconds`, 30 s, from the moment the gateway starts) calls
  `dca.run_due()`; the wallet sync runs beside it, never ahead of it. First run: at activation when `startNow` is true
  (default), otherwise one interval after activation. `next_run_at` is
  drift-free: `anchor_at + k * every_seconds` for the smallest `k` that is
  in the future. If the gateway was down and more than one interval passed,
  **one** run fires and the schedule skips forward; never a burst.
- **A run** = one swap order with `initiator="agent"` (so the guardrails
  apply: a buy at or under `approval_threshold_usd` executes on its own, a
  larger one parks for approval; the daily cap counts it), `session_key` =
  the mandate's, `note` = `"<name> · buy <n>/<max|∞>"`, `mandate_id` set,
  `amount_usd = min(usd_per_run, remaining)`. Before placing the order the
  engine advances `next_run_at` (compare-and-set on the previous value) and
  inserts the run row, so a crash mid-run cannot buy twice.
- **Run outcomes** (`mandate_runs.status`): `pending` (order placed, not
  yet settled — buys run with `wait=False`), `filled` (order confirmed),
  `parked` (awaiting approval; becomes `filled` / `expired` / `rejected`
  when the order settles), `skipped` (with `reasonCode`: `max_price`,
  `daily_cap`, `insufficient_balance`, `cap_reached`), `failed` (quote or
  execution failed, `reasonCode` = the error code), `expired`, `rejected`.
  Only `filled` runs count toward `runs.done`; skipped and failed runs are
  counted separately and do not shift the cap. A `pending` run whose order
  never appeared is written off as `failed trading.interrupted` after 1 h;
  each tick re-checks unsettled runs against their orders (restart safety).
  The balance is checked before an order is placed, so an
  `insufficient_balance` skip leaves no failed order in the Book. With a
  max price set and **no price known**, the run is skipped as `max_price`
  (never buy blind) and does not count toward auto-pause.
- **Cap accounting.** `spentUsd` = Σ `value_usd` of **confirmed** orders
  with this `mandate_id`; `reservedUsd` = Σ of open ones (awaiting /
  approved / submitted). A run is sized from `capUsd − spent − reserved`.
  When that remainder is under **min($0.50, half a buy)** (rounded down to
  the cent, never under $0.01), or `runs.done == runs.max`, the
  mandate is `completed` (`statusReason` = `"cap reached"` /
  `"runs reached"`) and emits `trading.dca.changed`. `spent_usd` is also
  cached on the mandate row (updated at settle) so the list is one query.
- **Max price guard.** With `max_price_usd` set, a run where the engine's
  spot price of `token` is above it is `skipped` (`max_price`, the price
  seen goes into `reason` detail). No buy, schedule advances normally.
- **Auto-pause.** Three consecutive runs that are `skipped`
  (`insufficient_balance` / `daily_cap`) or `failed` pause the mandate with
  `statusReason` = e.g. `"paused after 3 runs: insufficient USDC"`. A
  `max_price` skip does **not** count (waiting for a price is the point).
  Resume is manual.
- **Buy now** (`trading.dca.run`, operator-only) fires a run immediately on
  an `active` or `paused` mandate; it counts like any run and does **not**
  move the next scheduled buy.
- **Stop** also rejects the mandate's buys still waiting for approval.
  **Resume** resets the bad-run streak. A user pause sets `statusReason`
  `"user"`; an expired proposal `"no decision within 24 h"`. A mandate is
  never completed while one of its buys is still open; it completes when
  that buy settles. The order note counts **buys**, not attempts
  (`buy 3/30` even after skipped runs).
- **Update** (operator-only): `usdPerRun`, `capUsd`, `runsMax`,
  `everySeconds`, `maxPriceUsd`, `name` (`null` = not given; `runsMax: 0`
  removes the run limit, `maxPriceUsd: 0` removes the price guard; a cap
  below `usdPerRun` is allowed). Lowering the cap below what is
  already spent completes the mandate. Changing `everySeconds` re-anchors
  the schedule at now (next buy = now + every).
- **Stop** is terminal; **pause/resume** flip `active`/`paused`. Resuming
  after a long pause does not run the missed buys: `next_run_at` is
  recomputed from the anchor, so at most one buy fires when it is due.
- Nothing is ever deleted. `list` shows live mandates
  (`awaiting_approval`, `active`, `paused`) unless `--all`.

## Ledger (schema v7)

```sql
CREATE TABLE IF NOT EXISTS mandates (
  mandate_id      TEXT PRIMARY KEY,        -- "dca_" + 8 hex
  kind            TEXT NOT NULL DEFAULT 'dca',
  name            TEXT NOT NULL,
  status          TEXT NOT NULL,           -- awaiting_approval|active|paused|completed|stopped|rejected|expired
  status_reason   TEXT,
  chain_id        INTEGER NOT NULL,
  wallet          TEXT NOT NULL,
  token_out       TEXT NOT NULL,           -- what is bought (address)
  token_in        TEXT NOT NULL,           -- what is spent (address), default the chain's USDC
  usd_per_run     REAL NOT NULL,
  cap_usd         REAL NOT NULL,
  runs_max        INTEGER,
  every_seconds   INTEGER NOT NULL,
  max_price_usd   REAL,
  slippage_pct    REAL,
  start_now       INTEGER NOT NULL DEFAULT 1,
  anchor_at       REAL,                    -- epoch s; set at activation
  next_run_at     REAL,
  last_run_at     REAL,
  spent_usd       REAL NOT NULL DEFAULT 0, -- cache of Σ confirmed value_usd
  runs_done       INTEGER NOT NULL DEFAULT 0,
  runs_skipped    INTEGER NOT NULL DEFAULT 0,
  runs_failed     INTEGER NOT NULL DEFAULT 0,
  bad_streak      INTEGER NOT NULL DEFAULT 0,
  initiator       TEXT NOT NULL,           -- 'agent' | 'manual' (who created it)
  session_key     TEXT,
  created_at      REAL NOT NULL, updated_at REAL NOT NULL,
  approved_at     REAL, expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_mandates_status ON mandates(status, created_at DESC);

CREATE TABLE IF NOT EXISTS mandate_runs (
  run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  mandate_id   TEXT NOT NULL,
  n            INTEGER NOT NULL,           -- 1-based, counts every attempt
  at           REAL NOT NULL,
  status       TEXT NOT NULL,              -- filled|parked|skipped|failed|expired|rejected
  reason       TEXT,
  usd          REAL,                       -- what the run tried to spend
  price_usd    REAL,                       -- spot price of token_out seen at the run
  order_id     TEXT,
  manual       INTEGER NOT NULL DEFAULT 0  -- 1 = "buy now"
);
CREATE INDEX IF NOT EXISTS idx_mandate_runs ON mandate_runs(mandate_id, n DESC);

ALTER TABLE orders ADD COLUMN mandate_id TEXT;      -- migration step, v6 → v7
CREATE INDEX IF NOT EXISTS idx_orders_mandate ON orders(mandate_id) WHERE mandate_id IS NOT NULL;
```

Times are epoch seconds as floats like `orders.created_at`; the JSON
payload carries ISO-8601 UTC strings. `ORDER_KINDS` is unchanged: a
mandate buy is `kind='swap'`. `_order_dict` gains `"mandateId": str | null`
for every order.

Engine module: `src/agentos/trading/dca.py` — pure-ish planning (`next_run_at`,
sizing, guards, payload building) plus the runner entry points the service
calls. The service exposes:

```python
async def dca_create(self, *, chain, token, quote, usd_per_run, cap_usd, runs_max,
                     every_seconds, max_price_usd, wallet, slippage_pct, name, start_now,
                     initiator, session_key) -> dict          # mandate payload (kind="mandate")
async def dca_get(self, mandate_id) -> dict                   # payload
async def dca_list(self, *, all=False, wallet=None) -> dict   # payload (kind="mandates")
async def dca_approve(self, mandate_id) -> dict
async def dca_reject(self, mandate_id, reason=None) -> dict
async def dca_pause(self, mandate_id) -> dict
async def dca_resume(self, mandate_id) -> dict
async def dca_stop(self, mandate_id, reason=None) -> dict
async def dca_update(self, mandate_id, **fields) -> dict
async def dca_run_now(self, mandate_id, *, wait=False) -> dict   # payload + "run": Run
async def dca_run_due(self) -> None                               # called from tick()
```

Errors are `TradingError` codes: `trading.dca.not_found`,
`trading.dca.bad_state` (e.g. approve on an active one, resume on a stopped
one), `trading.dca.invalid` (usd ≤ 0 or under $0.01, cap < usd, every < 60 s, unknown
quote on Robinhood Chain without `--quote`), `trading.operator_required`
(from the RPC layer), plus the usual `trading.disabled`, `wallet.locked`,
`trading.unsupported_chain`, `trading.token_not_found` (unknown symbol;
an ambiguous one stays `trading.invalid`). At create, `slippagePct` may
not exceed the agent slippage ceiling and the wallet may not be `all`.

Events: every state change emits `trading.changed {reason: "dca",
mandateId}` (so the desktop's `['trading']` query prefix refreshes) and
`trading.dca.changed {mandate}` (full payload). Each run emits
`trading.dca.run {mandateId, run}` twice — when placed (`pending`) and
when settled; the order itself emits the normal
`trading.approval.requested` / `trading.order.finished`. When an order with
`mandate_id` settles (confirmed / failed / expired / rejected) the engine
updates the run row and the mandate cache in the same place it settles the
order.

## Payload

```jsonc
Envelope = {
  "version": 1,
  "kind": "mandate" | "mandates",
  "fetchedAt": "2026-09-28T09:30:00Z",
  "warnings": ["string"],
  "request": { "kind": "get" | "list", "params": {…} }   // what ↻ refresh re-runs: trading.dca.<kind>
}

// kind = "mandate"
{ ...Envelope, "mandate": Mandate, "run": Run | undefined }   // "run" only in the response of trading.dca.run

// kind = "mandates"
{ ...Envelope, "mandates": [ Mandate ],                      // live first, then by createdAt desc
  "totals": { "count": 3, "active": 2, "spentUsd": 240.5, "capUsd": 900, "acquiredUsd": 251.1 | null } }

Mandate = {
  "id": "dca_1a2b3c4d",
  "name": "DCA ETH",
  "status": "awaiting_approval" | "active" | "paused" | "completed" | "stopped" | "rejected" | "expired",
  "statusReason": string | null,                 // "cap reached", "runs reached", "paused after 3 runs: insufficient USDC", "user: …"
  "chain": Chain, "wallet": Wallet,              // as in lp-cards.md
  "token": Token, "quote": Token,                // token = bought, quote = spent (priceUsd on both)
  "schedule": { "everySeconds": 86400, "label": "every day",       // "every 6 hours", "every 30 minutes", "every week"
                "startNow": true, "anchorAt": iso | null, "nextRunAt": iso | null, "lastRunAt": iso | null },
  "budget":   { "usdPerRun": 10, "capUsd": 300, "spentUsd": 120.4, "reservedUsd": 10,
                "remainingUsd": 169.6, "progress": 0.40 },        // progress = spent / cap, 0–1
  "runs":     { "done": 12, "max": 30 | null, "skipped": 1, "failed": 0, "attempts": 13 },
  "guards":   { "maxPriceUsd": 3000 | null, "approvalThresholdUsd": 100, "dailyCapUsd": 1000,
                "slippagePct": 1.0, "buysNeedApproval": false },  // true when usdPerRun > threshold
  "acquired": { "amount": Amount,                                 // Σ received_out of confirmed buys; usd = at current price
                "avgPriceUsd": number | null,                     // spentUsd / amount.human
                "currentPriceUsd": number | null,
                "vsAvgPct": number | null,                        // (current − avg) / avg × 100
                "unrealizedUsd": number | null,                   // amount.usd − spentUsd
                "gasUsd": number },                               // Σ gas of the mandate's orders
  "history":  [ Run ],                                            // newest first, ≤ 50
  "initiator": "agent" | "manual", "sessionKey": string | null,
  "createdAt": iso, "updatedAt": iso, "approvedAt": iso | null, "expiresAt": iso | null
}

Run = { "n": 12, "at": iso, "manual": false,
        "status": "pending" | "filled" | "parked" | "skipped" | "failed" | "expired" | "rejected",
        "reasonCode": string | null,              // machine: max_price | daily_cap | insufficient_balance | cap_reached | trading.<code>
        "reason": string | null,                  // skipped/failed detail, human-readable ("ETH at $3,120 above $3,000")
        "usd": number | null,                     // spent (filled) or attempted
        "amount": Amount | null,                  // received (filled only)
        "priceUsd": number | null,                // effective buy price for filled, spot seen otherwise
        "orderId": string | null, "txHash": string | null, "explorerUrl": string | null, "gasUsd": number | null }
```

`Chain`, `Token`, `Amount`, `Wallet` are the LP card types. USD `null`
means unknown, never `0`. `warnings` carry things like *"each buy of $150
is above the $100 approval threshold and will wait for you"*, *"USDC balance
covers 4 more buys"* (when known), *"price unknown: max-price guard cannot
be checked"*.

## RPC

Agent-callable (create only proposes; reads are harmless):

| method | params | returns |
|---|---|---|
| `trading.dca.create` | `chainId`/`chain`, `token`, `quote?`, `usdPerRun`, `capUsd?`, `runsMax?` (at least one of cap/runs; cap defaults to `usdPerRun × runsMax`), `everySeconds`, `maxPriceUsd?`, `wallet?`, `slippagePct?`, `name?`, `startNow?` (default true), `sessionKey?` (operator only; agents are bound) | `mandate` payload |
| `trading.dca.get` | `mandateId` | `mandate` payload |
| `trading.dca.list` | `all?`, `wallet?` | `mandates` payload |

Operator-only (`@_operator_only`; an agent gets `trading.operator_required`):

| method | params | returns |
|---|---|---|
| `trading.dca.approve` | `mandateId` | `mandate` payload (now `active`; the first buy fires on the next tick when `startNow`) |
| `trading.dca.reject` | `mandateId`, `reason?` | payload |
| `trading.dca.pause` / `resume` / `stop` | `mandateId`, `reason?` (stop) | payload |
| `trading.dca.run` | `mandateId`, `wait?` | payload + `run` |
| `trading.dca.update` | `mandateId` + any of `usdPerRun`, `capUsd`, `runsMax`, `everySeconds`, `maxPriceUsd`, `name` | payload |

Every response is a full card payload with `request` echoed so the renderer
can swap it in place. `note` sanitising, `_chain`, `_number`, `_str` helpers
as for the other trading RPCs. Validation errors are `trading.dca.invalid`
with a message naming the field.

## CLI

```
agentos trade dca create <token> --usd 10 --every 1d (--cap 300 | --runs 30 | both)
                         [--max-price 3000] [--quote USDC] [--chain base|robinhood] [--wallet ADDR|label]
                         [--slippage 1] [--name "DCA ETH"] [--start now|next] [--json] [--no-card]
agentos trade dca list   [--all] [--wallet …] [--json] [--no-card]
agentos trade dca show   <id> [--json] [--no-card]
agentos trade dca approve <id> [--json] [--no-card]
agentos trade dca reject  <id> [--reason "…"] [--json] [--no-card]
agentos trade dca pause | resume <id> [--json] [--no-card]
agentos trade dca stop   <id> [--reason "…"] [--json] [--no-card]
agentos trade dca run    <id> [--wait --wait-seconds N] [--json] [--no-card]
agentos trade dca update <id> [--usd X] [--cap X] [--runs N] [--every 12h] [--max-price X] [--name …] [--json] [--no-card]
```

- `--every` accepts `30m`, `2h`, `1d`, `1w` or a plain number of seconds;
  minimum **60 s** (short intervals exist for testing; the desk offers
  hourly and up).
- `<token>` is an address or a ticker the engine resolves on that chain.
  `--quote` defaults to the chain's USDC; on Robinhood Chain (no canonical
  USDC) it is required.
- `--json` prints the payload and, unless `--no-card`, writes
  `dca-cards/<kind>-<slug>-<utc stamp>.json` (20 newest kept, same pruning
  as `lp-cards/`) and prints the last line
  `publish_artifact path=<file> mime=application/vnd.agentos.dca+json`.
  The gateway auto-publishes any `application/vnd.agentos.*` marker, no
  gateway change needed.
- Human output (no `--json`): a Rich panel per mandate (name, status,
  schedule, progress bar `spent / cap`, next buy, avg vs current price) and
  a table for `list`.
- Exit codes as `trade lp`: 2 for input / `trading.dca.invalid` /
  `trading.dca.bad_state` / `trading.dca.not_found` / `trading.invalid` /
  `trading.token_not_found`, 1 for gateway errors (including
  `trading.operator_required`).
  Errors are JSON on stderr under `--json`, never a card.
- `run --wait` waits for the fired order like `swap --wait`.
- From an agent shell, `create` always answers `status: "awaiting_approval"`;
  the agent tells the user the card has an **Approve & start** button and
  never approves itself (`trading.operator_required` otherwise).

## Rendering (shared renderer, `frontend/src/views/chat/transcript/dca.ts`)

Same skeleton as `lp.ts`: `isDcaArtifact`, `normalizeDcaPayload`,
`createDcaMounter({ fetchPayload, call, actions })`, registered in
`artifacts.ts` (`artifactCategory → 'dca'`, placeholder
`.msg-artifact-dca[data-dca-src]`) and mounted from `useTranscript.ts`
next to `lpMounter` (option `dcaActions`, same shape as `lpActions`:
`{ call, onOrder }`). `trading.order.finished` and `trading.dca.changed`
refresh mounted cards of the affected mandate.

### `kind = "mandate"` — the card

Root `article.dca-card[data-dca-kind=mandate][data-dca-status=…][data-dca-chain=…][data-dca-layout=wide|narrow]`.

1. **Header** `.dca-card__head`: `ETH ← USDC` (bought ← spent), chain pill,
   status pill `.dca-pill[data-status]` (`awaiting_approval` amber with a
   soft pulse, `active` green, `paused` grey, `completed` blue, `stopped` /
   `rejected` / `expired` muted). Name on the right, small.
2. **Hero** `.dca-card__hero`: `$10 every day` large; under it the live
   line `.dca-card__next[data-dca-next-at]`: `next buy in 3 h 12 m`
   (ticks every second while under an hour, every minute above; `first buy
   on approval` when pending; `buy now due` when overdue; `paused` etc.).
3. **Progress** `.dca-card__progress`: a bar `spent / cap` with the
   reserved slice hatched, labels `$120.40 of $300 · 40 %` and
   `12 of 30 buys` (or `12 buys`).
4. **Stats** `.dca-card__stats` (2×2, 4×1 when wide): *acquired*
   `0.0421 ETH · $131.20`; *avg buy* `$2,860` with `vs now ▲ 4.2 %` /
   `▼ 1.1 %` tinted; *unrealised* `+$10.80` signed and tinted; *gas*
   `$0.31`. `null` renders `—` with a "no price" hint.
5. **Buys chart** `.dca-chart` — plain SVG, no dependency: one column per
   attempt in time order (≤ 50): filled runs as bars whose height is the
   buy price, a dashed horizontal line at the average price, a dotted line
   at the current price with a right-edge label, skipped runs as hollow
   ticks at the price seen, failed as a small ×. Hover/focus shows a
   tooltip: date, `$10 → 0.0035 ETH @ $2,860`, status, tx link. Fewer
   than 2 filled runs → no chart, a one-line hint instead.
6. **Recent runs** `.dca-runs`: last 5 as rows `#12 · 2 h ago · filled ·
   $10 → 0.0035 ETH @ $2,860 · ↗` (explorer link). A `parked` run says
   `awaiting approval · #ord_…`.
7. **Actions** `.dca-actions` (only when `ctx.canWrite`, i.e. desktop):
   - `awaiting_approval`: **Approve & start** (primary) · Reject
   - `active`: Pause · Buy now · Stop
   - `paused`: Resume · Buy now · Stop
   - terminal: none.
   Buttons carry `data-dca-action=approve|reject|pause|resume|run|stop`
   and `data-dca-id`. A click calls `trading.dca.<action>`, disables the
   row while in flight, swaps in the returned payload, and for `run`
   passes the run's `orderId` to `actions.onOrder` so the desk focuses the
   Book. Errors show inline in `.dca-actions__error`. Stop asks for a
   second click ("Stop — click again") rather than a dialog.
8. **Warnings** `.dca-card__warnings` above the footer, one line each.
9. **Footer** `.dca-card__foot`: `dca_1a2b3c4d · Key main (0x89e0…da97) ·
   as of <relative time>` · ↻ refresh (when `request` present) · copy id.

### Live state, never a stale button

An artifact file is a snapshot from the moment the command ran; a mandate
moves on. The mounter therefore (a) renders the snapshot with every action
disabled (`data-dca-stale="checking"`) and at once re-reads the live state
through `trading.dca.<request.kind>`; a failed read keeps the snapshot,
sets `data-dca-stale="failed"` and shows a "state may be stale · ↻" line;
(b) keeps a per-mounter cache of the latest payload per mandate id (fed by
reads, action responses and `trading.dca.changed`) so a re-mounted card
draws the newest known state immediately; (c) after an action is refused
with `trading.dca.bad_state` / `not_found`, re-reads the card. The chart
pads its price range by at least ±0.5 % of the average and labels the top
and bottom of the range; same-day runs are labelled with times.

### `kind = "mandates"` — the list

`article.dca-card[data-dca-kind=mandates]`: header `DCA · 3 mandates`,
totals line (`$240 of $900 · $251 acquired`), then one row per mandate:
status dot, the mandate **name** first, `ETH ← USDC · every day · $10`, a
mini progress bar, `next buy in …`, and the same action buttons in compact
form. Empty → `No DCA
mandates yet.`

### Web vs desktop

The renderer lives in `frontend/`; the desktop restyles it in
`desktop/src/renderer/src/views/chat/chat.css` via the `data-dca-*` hooks
and must look like a desk instrument (rail, mono numerals, tinted status),
not like the web card. `canWrite` is true only on the desktop desk
(`ChatView` passes `dcaActions`), false on the web.

## Desktop desk

- **Presets** `dca` and `dca-capped` create a mandate (`trading.dca.create`)
  instead of a cron job. `MissionContract` for `kind === 'dca'` shows the
  mandate form (token, USD per buy, every, cap, buys, max price, wallet,
  chain, name, start now) and states the limits are **enforced by the
  engine** (drop the "goals, not limits" note there). Edit → `trading.dca.update`.
- **Missions list** shows mandates next to cron missions (the dot strip
  above it names cron missions only, so a mandate appears once): name,
  state word (`Awaiting approval`, `Active · next 3 h 12 m`, `Active · buy
  due`, `Paused`, `Done`, `Ended · no buys` when it finished with zero
  buys), `done/max buys` with a muted `· N skipped · N failed` suffix, a
  mini progress `$120 / $300`, `statusReason` as the row tooltip, and
  controls Pause / Resume, Buy now, Edit, Stop (two-click). Buy now toasts
  the run's outcome (bought / placed / awaiting approval / skipped: reason /
  failed: reason). Finished mandates (any terminal status) stay for 1 h
  after `updatedAt`, at most 2 rows newest first, then `+N more`. One
  formatter drives every countdown (`buy due`, `N m`, `H h M m`, `D d H h`,
  rounding down) on a shared one-second clock. Mandates never replace the
  composer placeholder (cron missions still do). The **header status
  strip** summarises mandates as one chip — `DCA · next 59 m`, `DCA ×2 ·
  next 12 m`, `DCA · awaiting` — with no names or progress. Query key
  `['trading', 'dca']`; the existing `trading.changed` invalidation covers it.
- **Approvals region** (the asks area above the composer, where order
  approval cards live) lists pending mandates above pending orders as a
  compact `MandateCard` (what, how much, how often, cap, first buy, expiry,
  warnings; values wrap, never truncate) with **Approve & start** / Reject.
- **Desk ledger** (`ledger.ts`) recognises `agentos trade dca <sub> …` and
  the `dca-cards/` marker, with titles `Start DCA · ETH · $10 / day`,
  `DCA status`, `Pause DCA`, `Buy now`, …
- **Orders** show `DCA ETH · buy 3/30` from the note; an order's `mandateId`
  links to the mandate.
- **Desk prompt v18** (`agent.ts`): a DCA is a mandate the engine runs;
  create it with `agentos trade dca create … --json` (never a cron job; the
  existing "never schedule a trade" rule now says "except a DCA mandate,
  which parks for the user's approval"); reading rules: "DCA $10 ETH every
  day, max $300" → `--usd 10 --every 1d --cap 300`; "30 buys" → `--runs 30`;
  "only under 3000" → `--max-price 3000`; "how is my DCA doing" → `dca list
  --json` / `dca show <id> --json` and answer in one line, the card shows
  the rest; "pause/stop my DCA" → tell the user to use the card or the
  Missions panel (operator-only), or run the command and report
  `trading.operator_required` plainly. Always report the mandate id.

## Tests

- **Engine** (`tests/test_trading/test_dca.py`, offline, fake clock): create
  as agent parks / as operator activates; approve → first run fires on
  tick with `startNow`; drift-free schedule and single catch-up after a
  gap; sizing to the remainder and completion at the cap / at `runs_max`;
  `max_price` skip without bad-streak; auto-pause after 3 insufficient
  balance skips; parked run when `usd_per_run > threshold` and its
  settlement path (approve → filled, expire → expired); daily-cap skip;
  update re-anchoring; stop/pause/resume state machine and `bad_state`
  errors; migration v6 → v7 on an existing ledger (orders keep their rows,
  `mandate_id` added); payload shape (regenerates
  `tests/fixtures/dca_cards/{mandate-active,mandate-awaiting,mandate-completed,mandates,mandates-empty}.json`,
  which the frontend tests read like the LP fixtures).
- **RPC** (`tests/test_gateway/test_rpc_trading_dca.py`): methods
  registered; agent may create/get/list and gets `awaiting_approval`; every
  write is `trading.operator_required` for an agent; validation codes.
- **CLI** (`tests/test_cli/test_trade_dca_cmd.py`): `--every` parsing, cap /
  runs rule, marker last line, `--no-card`, pruning, JSON errors on stderr,
  exit codes.
- **Frontend** (`dca.test.ts`): category, normalisation, each status
  layout, countdown text, progress math, chart with 0/1/N runs, actions
  hidden without `canWrite`, action → RPC → payload swap, refresh, CSS
  contract test entries.
- **Desktop**: `MandateCard` approve/reject, MissionControls rows for
  mandates, `ledger.ts` parsing of `dca` commands and the marker,
  `ChatView` passes `dcaActions` at the desk only, CSS skin test.
- **Live** (tester, dust wallet, Base): `agentos trade dca create ETH --usd
  0.05 --every 2m --runs 2 --json` from the desk chat as the agent → card
  with Approve & start → approve on the desktop → first buy fires within a
  tick, second after 2 min → mandate `completed`; `Buy now` on a paused
  mandate; the card and the Missions panel agree with `agentos trade dca
  show`. Cost ≈ $0.10 + gas.
