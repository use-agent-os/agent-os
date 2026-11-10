// Chat transcript — price trigger cards.
//
// `agentos trade trigger … --json` publishes a JSON artifact with the
// `application/vnd.agentos.trigger+json` mime; the artifact renderer emits a
// mount placeholder for it and this module fetches the payload and draws one
// of four layouts into it, keyed by the payload's `kind`: `trigger` (one
// conditional order, read like an instrument: the sentence, the live price
// against the line, a gauge), `triggers` (the list), `bracket` (a take-profit
// and a stop-loss on one position, one cancelling the other: the two lines
// with the price between them on a range gauge, the two legs) and `brackets`
// (their list). docs/triggers.md and docs/brackets.md are the contract.
//
// Same two surfaces as dca.ts:
//   1. Pure helpers (top-level exports) — mime match, payload normalization,
//      hero / live-line / distance text, gauge geometry. No network.
//   2. `createTriggerMounter(deps)` — the imperative mounter the transcript
//      composes next to the DCA mounter.
//
// Styling: the markup carries `trigger-*` classes plus `data-trigger-kind`,
// `data-trigger-action`, `data-trigger-status`, `data-trigger-chain`,
// `data-trigger-layout` and `data-trigger-stale` hooks. Only the desktop mounts
// this markup and skins the hooks in its own chat.css; the web console renders
// its own React cards (web/features/chat/cards/) from the pure helpers.
//
// SECURITY: every payload-derived string reaches the DOM through `textContent`
// or an attribute setter, never `innerHTML` — token symbols are attacker-chosen
// on a permissionless chain. Explorer links are built from an http(s) explorer
// origin and a hex hash, or accepted only as an http(s) URL.

import { t, tPlural } from '@/i18n'
import '@/i18n/en/chat'

import type { Artifact } from './artifacts'
import {
  DCA_MINUTE_MS,
  DCA_SECOND_MS,
  dcaTickDelay,
  formatCountdown,
  formatDcaPrice,
  formatDcaUsd,
  statusReasonText,
} from './dca'
import {
  NO_VALUE,
  copyLpText,
  explorerUrl,
  formatTokenAmount,
  relativeTime,
  safeExplorerBase,
  shortAddress,
  type LpAmount,
  type LpChain,
  type LpToken,
  type LpWallet,
} from './lp'

/**
 * Every price a trigger card says — the hero, the gauge's labels, the live
 * line, a fire — in one voice, the engine's (`price_text`): under a dollar the
 * shared formatter trims "$0.50" to "$0.5", so the gauge read `line $0.5`
 * under a hero that said `over $0.50`. Whole cents keep two decimals.
 */
export function formatTriggerPrice(value: number | null): string {
  const text = formatDcaPrice(value)
  return /^\$0\.\d$/.test(text) ? `${text}0` : text
}

/** The mime the engine publishes a price trigger read-out under. */
export const TRIGGER_ARTIFACT_MIME = 'application/vnd.agentos.trigger+json'

/** How long the copy button reads "copied". */
export const TRIGGER_COPIED_MS = 1_500

/** How long Stop / Fire now wait for their confirming second click. */
export const TRIGGER_CONFIRM_MS = 4_000

/** How long a failed ↻ says why under the footer. */
export const TRIGGER_ERROR_MS = 4_000

/** Card width (border box, px) from which a list row keeps its controls on the right. */
export const TRIGGER_WIDE_MIN_PX = 520

/** Rows in "Recent fires". */
export const TRIGGER_RECENT_FIRES = 5

/** An armed trigger within this many percent of its line reads "near". */
export const TRIGGER_NEAR_PCT = 1

/** The gauge's price axis reaches at least this far (a fraction of the line) past the data. */
export const TRIGGER_GAUGE_MIN_PAD = 0.02
/**
 * A fact value longer than this many characters asks for two grid columns
 * (`data-trigger-fact-span="2"` on its `.trigger-fact`): four facts in a row
 * leave ~18 mono characters a column.
 */
export const TRIGGER_FACT_WIDE_CHARS = 18

const HOUR_MS = 3_600_000

/* ── Payload shape (docs/triggers.md) ───────────────────────────────────── */

export type TriggerKind = 'trigger' | 'triggers' | 'bracket' | 'brackets'
export type TriggerActionKind = 'sell' | 'buy' | 'alert' | 'unknown'
export type TriggerDirection = 'below' | 'above' | 'trail' | 'unknown'
export type TriggerStatus =
  | 'awaiting_approval'
  | 'armed'
  | 'triggered'
  | 'paused'
  | 'done'
  | 'stopped'
  | 'rejected'
  | 'expired'
  | 'unknown'
export type TriggerFireStatus =
  | 'pending'
  | 'filled'
  | 'parked'
  | 'alerted'
  | 'skipped'
  | 'failed'
  | 'expired'
  | 'rejected'
  | 'unknown'
/** A control on a trigger card; each calls `trading.trigger.<action>`. */
export type TriggerAction = 'approve' | 'reject' | 'pause' | 'resume' | 'fire' | 'stop'

export interface TriggerFire {
  n: number
  at: string
  manual: boolean
  status: TriggerFireStatus
  /** Machine code: insufficient_balance | needs_approval | trading.<code>. */
  reasonCode: string | null
  /** Human-readable detail. */
  reason: string | null
  priceUsd: number | null
  orderId: string | null
  txHash: string | null
  /** A safe http(s) link to the transaction, or ''. */
  explorerUrl: string
}

export interface TriggerCondition {
  direction: TriggerDirection
  priceUsd: number | null
  trailPct: number | null
  fromPriceUsd: number | null
  peakPriceUsd: number | null
  stopPriceUsd: number | null
  confirmTicks: number
  hits: number
  /** The engine's words ("under $3,800"), or '' (see `conditionLabel`). */
  label: string
}

export interface TriggerActionSpec {
  kind: TriggerActionKind
  amountUsd: number | null
  amountPct: number | null
  amount: LpAmount | null
  estimatedUsd: number | null
  slippagePct: number | null
  needsApproval: boolean
  approvalThresholdUsd: number | null
  dailyCapUsd: number | null
  label: string
}

export interface TriggerMarket {
  priceUsd: number | null
  armedPriceUsd: number | null
  /** Signed % move from the price needed to fire; 0 once met. */
  distancePct: number | null
  checkedAt: string | null
  balance: LpAmount | null
}

export interface TriggerResult {
  orderId: string
  txHash: string | null
  explorerUrl: string
  amountIn: LpAmount | null
  amountOut: LpAmount | null
  priceUsd: number | null
  gasUsd: number | null
}

export interface Trigger {
  id: string
  name: string
  kind: TriggerActionKind
  status: TriggerStatus
  statusReason: string | null
  chain: LpChain | null
  wallet: LpWallet | null
  token: LpToken
  quote: LpToken
  condition: TriggerCondition
  action: TriggerActionSpec
  market: TriggerMarket
  /** Newest first, ≤ 20. */
  fires: TriggerFire[]
  result: TriggerResult | null
  validUntil: string | null
  initiator: string
  sessionKey: string | null
  createdAt: string
  updatedAt: string
  approvedAt: string | null
  armedAt: string | null
  triggeredAt: string | null
  expiresAt: string | null
  /** Set on a leg of a bracket (docs/brackets.md): its writes go through the bracket. */
  bracket: TriggerBracketRef | null
}

/** One of a bracket's two legs: the take-profit (`tp`) or the stop-loss (`sl`). */
export type BracketLeg = 'tp' | 'sl'

/** The bracket a trigger is a leg of. */
export interface TriggerBracketRef {
  id: string
  name: string
  leg: BracketLeg
}

export type BracketKind = 'sell' | 'alert' | 'unknown'

export interface BracketLines {
  /** The take-profit line. */
  takeProfitUsd: number | null
  /** The stop line now: the threshold, or a trail's peak × (1 − trailPct/100). */
  stopLossUsd: number | null
  trailPct: number | null
  /** The price at creation when either line was given as a percent. */
  fromPriceUsd: number | null
  /** The engine's words ("over $4,560"), or ''. */
  takeProfitLabel: string
  /** The engine's words ("under $3,420", "10 % below peak"), or ''. */
  stopLossLabel: string
}

export interface BracketActionSpec {
  kind: BracketKind
  amountPct: number | null
  amount: LpAmount | null
  amountUsd: number | null
  /** The take-profit leg's smaller share, when under `amountPct`. */
  tpPct: number | null
  estimatedUsd: number | null
  slippagePct: number | null
  needsApproval: boolean
  approvalThresholdUsd: number | null
  dailyCapUsd: number | null
  label: string
}

export interface BracketMarket {
  priceUsd: number | null
  armedPriceUsd: number | null
  checkedAt: string | null
  balance: LpAmount | null
  /** % rise to the take-profit line; 0 when met. */
  upsidePct: number | null
  /** % fall to the stop line (negative); 0 when met. */
  downsidePct: number | null
  /** Where the price sits between the lines: 0 at the stop, 100 at the take-profit. */
  positionPct: number | null
  rewardRisk: number | null
  /** The leg closer to firing. */
  nearest: BracketLeg | null
}

export interface Bracket {
  id: string
  name: string
  kind: BracketKind
  status: TriggerStatus
  statusReason: string | null
  chain: LpChain | null
  wallet: LpWallet | null
  token: LpToken
  quote: LpToken
  /** The take-profit leg, a full trigger; null when the payload lacks it. */
  takeProfit: Trigger | null
  /** The stop-loss leg. */
  stopLoss: Trigger | null
  lines: BracketLines
  action: BracketActionSpec
  market: BracketMarket
  /** The leg whose fire ended (or, partial take-profit, advanced) the bracket. */
  fired: BracketLeg | null
  result: TriggerResult | null
  validUntil: string | null
  initiator: string
  sessionKey: string | null
  createdAt: string
  updatedAt: string
  approvedAt: string | null
  armedAt: string | null
  expiresAt: string | null
}

/**
 * What ↻ re-runs: `trading.<scope>.<kind>` with `params` verbatim. `scope` is
 * `bracket` for a bracket card (absent: a trigger's read).
 */
export interface TriggerRequest {
  kind: 'get' | 'list'
  params: Record<string, unknown>
  scope?: 'trigger' | 'bracket'
}

interface TriggerEnvelope {
  version: number
  fetchedAt: string
  warnings: string[]
  request: TriggerRequest | null
}

export interface TriggerOnePayload extends TriggerEnvelope {
  kind: 'trigger'
  trigger: Trigger
  /** Only in the answer of `trading.trigger.fire`. */
  fire: TriggerFire | null
}

export interface TriggerListPayload extends TriggerEnvelope {
  kind: 'triggers'
  triggers: Trigger[]
  totals: { count: number; armed: number; awaiting: number; triggered: number }
}

export interface BracketOnePayload extends TriggerEnvelope {
  kind: 'bracket'
  bracket: Bracket
  /** Only in the answer of `trading.bracket.fire`. */
  fire: TriggerFire | null
}

export interface BracketListPayload extends TriggerEnvelope {
  kind: 'brackets'
  brackets: Bracket[]
  totals: { count: number; armed: number; awaiting: number; triggered: number }
}

export type TriggerPayload =
  TriggerOnePayload | TriggerListPayload | BracketOnePayload | BracketListPayload

/* ── Pure helpers: mime + normalization ─────────────────────────────────── */

/** True when the artifact should render as a trigger card. */
export function isTriggerArtifact(artifact: Artifact | null | undefined): boolean {
  if (!artifact || !artifact.mime) return false
  return String(artifact.mime).toLowerCase().split(';')[0]?.trim() === TRIGGER_ARTIFACT_MIME
}

type Obj = Record<string, unknown>

function obj(value: unknown): Obj | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Obj) : null
}

function text(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

function textOrNull(value: unknown): string | null {
  return text(value) || null
}

/** A finite number, or null. Numeric strings count. */
function num(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

/** A positive finite number, or null: a price of 0 is no price. */
function price(value: unknown): number | null {
  const n = num(value)
  return n !== null && n > 0 ? n : null
}

function bool(value: unknown): boolean {
  return value === true || value === 'true' || value === 1
}

function list<T>(value: unknown, fn: (item: unknown) => T | null): T[] {
  return Array.isArray(value) ? value.map(fn).filter((item): item is T => item !== null) : []
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value))
}

function normChain(value: unknown): LpChain | null {
  const row = obj(value)
  if (!row) return null
  const key = text(row.key)
  const name = text(row.name) || key
  if (!name) return null
  return { id: num(row.id) ?? 0, key, name, explorer: safeExplorerBase(row.explorer) }
}

function normToken(value: unknown): LpToken | null {
  const row = obj(value)
  if (!row) return null
  const symbol = text(row.symbol)
  const address = text(row.address)
  if (!symbol && !address) return null
  return {
    address,
    symbol: symbol || shortAddress(address),
    decimals: num(row.decimals) ?? 0,
    priceUsd: num(row.priceUsd),
  }
}

function normAmount(value: unknown): LpAmount | null {
  const row = obj(value)
  if (!row) return null
  const human = text(row.human)
  const raw = text(row.raw)
  if (!human && !raw) return null
  return { raw, human, usd: num(row.usd) }
}

function normWallet(value: unknown): LpWallet | null {
  const row = obj(value)
  if (!row) return null
  const address = text(row.address)
  if (!address) return null
  return { address, label: textOrNull(row.label), inApp: row.inApp === true }
}

/** An http(s) URL as given, or ''. */
function safeHttpUrl(value: unknown): string {
  const raw = text(value)
  if (!raw) return ''
  try {
    const url = new URL(raw)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : ''
  } catch {
    return ''
  }
}

/** A tx link from the chain's explorer, else the payload's when it is plain http(s). */
function txLink(chain: LpChain | null, txHash: string | null, given: unknown): string {
  return (txHash ? explorerUrl(chain, 'tx', txHash) : '') || safeHttpUrl(given)
}

const STATUSES = new Set<TriggerStatus>([
  'awaiting_approval',
  'armed',
  'triggered',
  'paused',
  'done',
  'stopped',
  'rejected',
  'expired',
])

function normStatus(value: unknown): TriggerStatus {
  const raw = text(value).toLowerCase() as TriggerStatus
  return STATUSES.has(raw) ? raw : 'unknown'
}

const FIRE_STATUSES = new Set<TriggerFireStatus>([
  'pending',
  'filled',
  'parked',
  'alerted',
  'skipped',
  'failed',
  'expired',
  'rejected',
])

const ACTION_KINDS = new Set<TriggerActionKind>(['sell', 'buy', 'alert'])
const DIRECTIONS = new Set<TriggerDirection>(['below', 'above', 'trail'])

function normKind(...values: unknown[]): TriggerActionKind {
  for (const value of values) {
    const raw = text(value).toLowerCase() as TriggerActionKind
    if (ACTION_KINDS.has(raw)) return raw
  }
  return 'unknown'
}

function normDirection(value: unknown): TriggerDirection {
  const raw = text(value).toLowerCase() as TriggerDirection
  return DIRECTIONS.has(raw) ? raw : 'unknown'
}

function normFire(value: unknown, chain: LpChain | null): TriggerFire | null {
  const row = obj(value)
  if (!row) return null
  const status = text(row.status).toLowerCase() as TriggerFireStatus
  const txHash = textOrNull(row.txHash)
  return {
    n: Math.trunc(num(row.n) ?? 0),
    at: text(row.at),
    manual: bool(row.manual),
    status: FIRE_STATUSES.has(status) ? status : 'unknown',
    reasonCode: textOrNull(row.reasonCode),
    reason: textOrNull(row.reason),
    priceUsd: price(row.priceUsd),
    orderId: textOrNull(row.orderId),
    txHash,
    // Built from the chain's explorer when possible; the payload's link only
    // when it is plain http(s) — never a `javascript:` smuggled in a payload.
    explorerUrl: txLink(chain, txHash, row.explorerUrl),
  }
}

function normResult(value: unknown, chain: LpChain | null): TriggerResult | null {
  const row = obj(value)
  if (!row) return null
  const orderId = text(row.orderId)
  if (!orderId) return null
  const txHash = textOrNull(row.txHash)
  return {
    orderId,
    txHash,
    explorerUrl: txLink(chain, txHash, row.explorerUrl),
    amountIn: normAmount(row.amountIn),
    amountOut: normAmount(row.amountOut),
    priceUsd: price(row.priceUsd),
    gasUsd: num(row.gasUsd),
  }
}

/** Normalize one trigger, or null when it has no id or no token to name. */
export function normalizeTrigger(value: unknown): Trigger | null {
  const row = obj(value)
  if (!row) return null
  const id = text(row.id)
  const token = normToken(row.token)
  if (!id || !token) return null
  const quote = normToken(row.quote) ?? { address: '', symbol: '?', decimals: 0, priceUsd: null }
  const chain = normChain(row.chain)
  const condition = obj(row.condition) ?? {}
  const action = obj(row.action) ?? {}
  const market = obj(row.market) ?? {}
  const kind = normKind(row.kind, action.kind)
  const confirmTicks = Math.max(1, Math.trunc(num(condition.confirmTicks) ?? 2))
  const name = text(row.name)

  return {
    id,
    name: name || t('chat.triggerDefaultName', { token: token.symbol }),
    kind,
    status: normStatus(row.status),
    statusReason: textOrNull(row.statusReason),
    chain,
    wallet: normWallet(row.wallet),
    token,
    quote,
    condition: {
      direction: normDirection(condition.direction),
      priceUsd: price(condition.priceUsd),
      trailPct: price(condition.trailPct),
      fromPriceUsd: price(condition.fromPriceUsd),
      peakPriceUsd: price(condition.peakPriceUsd),
      stopPriceUsd: price(condition.stopPriceUsd),
      confirmTicks,
      hits: Math.min(confirmTicks, Math.max(0, Math.trunc(num(condition.hits) ?? 0))),
      label: text(condition.label),
    },
    action: {
      kind: normKind(action.kind, row.kind),
      amountUsd: price(action.amountUsd),
      amountPct: price(action.amountPct),
      amount: normAmount(action.amount),
      estimatedUsd: num(action.estimatedUsd),
      slippagePct: num(action.slippagePct),
      needsApproval: bool(action.needsApproval),
      approvalThresholdUsd: num(action.approvalThresholdUsd),
      dailyCapUsd: num(action.dailyCapUsd),
      label: text(action.label),
    },
    market: {
      priceUsd: price(market.priceUsd) ?? token.priceUsd,
      armedPriceUsd: price(market.armedPriceUsd),
      distancePct: num(market.distancePct),
      checkedAt: textOrNull(market.checkedAt),
      balance: normAmount(market.balance),
    },
    fires: list(row.fires, (item) => normFire(item, chain)),
    result: normResult(row.result, chain),
    validUntil: textOrNull(row.validUntil),
    initiator: text(row.initiator),
    sessionKey: textOrNull(row.sessionKey),
    createdAt: text(row.createdAt),
    updatedAt: text(row.updatedAt),
    approvedAt: textOrNull(row.approvedAt),
    armedAt: textOrNull(row.armedAt),
    triggeredAt: textOrNull(row.triggeredAt),
    expiresAt: textOrNull(row.expiresAt),
    bracket: normBracketRef(row.bracket),
  }
}

function normLeg(value: unknown): BracketLeg | null {
  const raw = text(value).toLowerCase()
  return raw === 'tp' || raw === 'sl' ? raw : null
}

/** A leg's `bracket` field, or null when it names no bracket or no leg. */
function normBracketRef(value: unknown): TriggerBracketRef | null {
  const row = obj(value)
  if (!row) return null
  const id = text(row.id)
  const leg = normLeg(row.leg)
  if (!id || !leg) return null
  return { id, name: text(row.name) || id, leg }
}

function normBracketKind(...values: unknown[]): BracketKind {
  for (const value of values) {
    const raw = text(value).toLowerCase()
    if (raw === 'sell' || raw === 'alert') return raw
  }
  return 'unknown'
}

/**
 * Normalize one bracket, or null when it has no id or no token to name. A
 * line the payload leaves out is read off its leg (a trail's stop price for a
 * trailing stop), so an older engine still draws its gauge.
 */
export function normalizeBracket(value: unknown): Bracket | null {
  const row = obj(value)
  if (!row) return null
  const id = text(row.id)
  const takeProfit = normalizeTrigger(row.takeProfit)
  const stopLoss = normalizeTrigger(row.stopLoss)
  const token = normToken(row.token) ?? takeProfit?.token ?? stopLoss?.token ?? null
  if (!id || !token) return null
  const quote = normToken(row.quote) ??
    takeProfit?.quote ?? { address: '', symbol: '?', decimals: 0, priceUsd: null }
  const chain = normChain(row.chain) ?? takeProfit?.chain ?? stopLoss?.chain ?? null
  const lines = obj(row.lines) ?? {}
  const action = obj(row.action) ?? {}
  const market = obj(row.market) ?? {}
  const kind = normBracketKind(row.kind, action.kind)
  const slCondition = stopLoss?.condition
  const trailPct =
    price(lines.trailPct) ?? (slCondition?.direction === 'trail' ? slCondition.trailPct : null)
  const slLine =
    price(lines.stopLossUsd) ??
    (slCondition
      ? slCondition.direction === 'trail'
        ? slCondition.stopPriceUsd
        : slCondition.priceUsd
      : null)
  const name = text(row.name)
  return {
    id,
    name:
      name ||
      t(kind === 'alert' ? 'chat.bracketDefaultNameAlert' : 'chat.bracketDefaultName', {
        token: token.symbol,
      }),
    kind,
    status: normStatus(row.status),
    statusReason: textOrNull(row.statusReason),
    chain,
    wallet: normWallet(row.wallet) ?? takeProfit?.wallet ?? null,
    token,
    quote,
    takeProfit,
    stopLoss,
    lines: {
      takeProfitUsd: price(lines.takeProfitUsd) ?? takeProfit?.condition.priceUsd ?? null,
      stopLossUsd: slLine,
      trailPct,
      fromPriceUsd: price(lines.fromPriceUsd),
      takeProfitLabel: text(lines.takeProfitLabel),
      stopLossLabel: text(lines.stopLossLabel),
    },
    action: {
      kind: normBracketKind(action.kind, row.kind),
      amountPct: price(action.amountPct),
      amount: normAmount(action.amount),
      amountUsd: price(action.amountUsd),
      tpPct: price(action.tpPct),
      estimatedUsd: num(action.estimatedUsd),
      slippagePct: num(action.slippagePct),
      needsApproval: bool(action.needsApproval),
      approvalThresholdUsd: num(action.approvalThresholdUsd),
      dailyCapUsd: num(action.dailyCapUsd),
      label: text(action.label),
    },
    market: {
      priceUsd: price(market.priceUsd) ?? token.priceUsd,
      armedPriceUsd: price(market.armedPriceUsd),
      checkedAt: textOrNull(market.checkedAt),
      balance: normAmount(market.balance),
      upsidePct: num(market.upsidePct),
      downsidePct: num(market.downsidePct),
      positionPct: num(market.positionPct),
      rewardRisk: price(market.rewardRisk),
      nearest: normLeg(market.nearest),
    },
    fired: normLeg(row.fired),
    result: normResult(row.result, chain),
    validUntil: textOrNull(row.validUntil),
    initiator: text(row.initiator),
    sessionKey: textOrNull(row.sessionKey),
    createdAt: text(row.createdAt),
    updatedAt: text(row.updatedAt),
    approvedAt: textOrNull(row.approvedAt),
    armedAt: textOrNull(row.armedAt),
    expiresAt: textOrNull(row.expiresAt),
  }
}

/** The read to re-run, or null. Only `get` and `list` pass: the method is built from it. */
export function normalizeTriggerRequest(value: unknown): TriggerRequest | null {
  const row = obj(value)
  if (!row) return null
  const kind = text(row.kind)
  if (kind !== 'get' && kind !== 'list') return null
  return { kind, params: { ...(obj(row.params) ?? {}) } }
}

/**
 * Validate and normalize an artifact body into a renderable payload, or return
 * null when there is nothing to draw (unknown kind, no trigger). Never throws.
 */
export function normalizeTriggerPayload(raw: unknown): TriggerPayload | null {
  const body = obj(raw)
  if (!body) return null
  const kind = text(body.kind)
  const envelope: TriggerEnvelope = {
    version: num(body.version) ?? 1,
    fetchedAt: text(body.fetchedAt),
    warnings: list(body.warnings, (w) => text(w) || null),
    request: normalizeTriggerRequest(body.request),
  }
  if (kind === 'trigger') {
    const trigger = normalizeTrigger(body.trigger)
    if (!trigger) return null
    return { ...envelope, kind, trigger, fire: normFire(body.fire, trigger.chain) }
  }
  if (kind === 'triggers') {
    if (!Array.isArray(body.triggers)) return null
    const triggers = list(body.triggers, normalizeTrigger)
    const totals = obj(body.totals) ?? {}
    const count = (status: TriggerStatus): number =>
      triggers.filter((tr) => tr.status === status).length
    return {
      ...envelope,
      kind,
      triggers,
      totals: {
        count: Math.max(0, Math.trunc(num(totals.count) ?? triggers.length)),
        armed: Math.max(0, Math.trunc(num(totals.armed) ?? count('armed'))),
        awaiting: Math.max(0, Math.trunc(num(totals.awaiting) ?? count('awaiting_approval'))),
        triggered: Math.max(0, Math.trunc(num(totals.triggered) ?? count('triggered'))),
      },
    }
  }
  if (kind === 'bracket' || kind === 'brackets') {
    // ↻ on a bracket card re-runs `trading.bracket.<kind>`.
    if (envelope.request) envelope.request.scope = 'bracket'
  }
  if (kind === 'bracket') {
    const bracket = normalizeBracket(body.bracket)
    if (!bracket) return null
    return { ...envelope, kind, bracket, fire: normFire(body.fire, bracket.chain) }
  }
  if (kind === 'brackets') {
    if (!Array.isArray(body.brackets)) return null
    const brackets = list(body.brackets, normalizeBracket)
    const totals = obj(body.totals) ?? {}
    const count = (status: TriggerStatus): number =>
      brackets.filter((b) => b.status === status).length
    return {
      ...envelope,
      kind,
      brackets,
      totals: {
        count: Math.max(0, Math.trunc(num(totals.count) ?? brackets.length)),
        armed: Math.max(0, Math.trunc(num(totals.armed) ?? count('armed'))),
        awaiting: Math.max(0, Math.trunc(num(totals.awaiting) ?? count('awaiting_approval'))),
        triggered: Math.max(0, Math.trunc(num(totals.triggered) ?? count('triggered'))),
      },
    }
  }
  return null
}

/* ── Pure helpers: text ─────────────────────────────────────────────────── */

/** A percent figure without trailing zeros: 50 → "50", 12.5 → "12.5". */
function pctNumber(value: number): string {
  return String(Number(value.toFixed(2)))
}

/**
 * The condition in words: the engine's label, else one derived from the
 * direction ("under $3,800", "over $5,000", "10 % below peak").
 */
export function conditionLabel(trigger: Trigger): string {
  const c = trigger.condition
  if (c.label) return c.label
  switch (c.direction) {
    case 'below':
      return t('chat.triggerCondBelow', { price: formatTriggerPrice(c.priceUsd) })
    case 'above':
      return t('chat.triggerCondAbove', { price: formatTriggerPrice(c.priceUsd) })
    case 'trail':
      return t('chat.triggerCondTrail', {
        pct: c.trailPct !== null ? pctNumber(c.trailPct) : NO_VALUE,
      })
    default:
      return t('chat.triggerCondUnknown')
  }
}

/** What a fire does, in a phrase: "sell 50 % of ETH", "buy $50 of ETH". */
function actionPhrase(trigger: Trigger): string {
  const { action, token } = trigger
  const symbol = token.symbol
  if (trigger.kind === 'sell') {
    if (action.amountPct !== null) {
      return t('chat.triggerSellPct', { pct: pctNumber(action.amountPct), token: symbol })
    }
    if (action.amount) {
      return t('chat.triggerSellAmount', {
        amount: formatTokenAmount(action.amount.human),
        token: symbol,
      })
    }
    return t('chat.triggerSellUsd', { usd: formatDcaUsd(action.amountUsd), token: symbol })
  }
  if (trigger.kind === 'buy') {
    return t('chat.triggerBuyUsd', { usd: formatDcaUsd(action.amountUsd), token: symbol })
  }
  return t('chat.triggerActUnknown', { token: symbol })
}

/** The card's sentence: "sell 50 % of ETH when under $3,800". */
export function heroText(trigger: Trigger): string {
  const condition = conditionLabel(trigger)
  if (trigger.kind === 'alert') {
    return t('chat.triggerHeroAlert', { token: trigger.token.symbol, condition })
  }
  return t('chat.triggerHero', { action: actionPhrase(trigger), condition })
}

/**
 * The signed distance to the line in percent of the price: the engine's
 * `distancePct`, else derived from the price and the line. 0 once met; null
 * when either price is unknown.
 */
export function distancePctOf(trigger: Trigger): number | null {
  if (trigger.market.distancePct !== null) return trigger.market.distancePct
  const now = trigger.market.priceUsd
  const c = trigger.condition
  const line = c.direction === 'trail' ? c.stopPriceUsd : c.priceUsd
  if (now === null || line === null) return null
  if (c.direction === 'above') return now >= line ? 0 : ((line - now) / now) * 100
  if (c.direction === 'below' || c.direction === 'trail') {
    return now <= line ? 0 : ((line - now) / now) * 100
  }
  return null
}

/** "0.3 % above the line", "4.0 % below the line", "at the line"; '' when unknown. */
export function distanceText(trigger: Trigger): string {
  const d = distancePctOf(trigger)
  if (d === null) return ''
  const trail = trigger.condition.direction === 'trail'
  if (d === 0) return trail ? t('chat.triggerTrailMet') : t('chat.triggerDistMet')
  const pct = Math.abs(d).toFixed(1)
  if (d < 0) {
    return trail ? t('chat.triggerTrailAbove', { pct }) : t('chat.triggerDistAbove', { pct })
  }
  return t('chat.triggerDistBelow', { pct })
}

/** A signed distance for a list row: "−0.3 %", "+4.0 %", "at the line". */
export function formatDistance(pct: number | null): string {
  if (pct === null || !Number.isFinite(pct)) return ''
  if (pct === 0) return t('chat.triggerDistMet')
  return `${pct > 0 ? '+' : '−'}${Math.abs(pct).toFixed(1)} %`
}

/** "12 s ago", "4 m ago", "3 h ago", "2 d ago" from an ISO stamp; '' when unusable. */
export function agoText(iso: string | null, nowMs: number): string {
  if (!iso) return ''
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return ''
  const seconds = Math.max(0, Math.floor((nowMs - at) / 1000))
  if (seconds < 60) return t('chat.triggerAgoSeconds', { count: String(seconds) })
  if (seconds < 3600)
    return t('chat.triggerAgoMinutes', { count: String(Math.floor(seconds / 60)) })
  if (seconds < 86_400) {
    return t('chat.triggerAgoHours', { count: String(Math.floor(seconds / 3600)) })
  }
  return t('chat.triggerAgoDays', { count: String(Math.floor(seconds / 86_400)) })
}

function msUntil(iso: string | null, nowMs: number): number | null {
  if (!iso) return null
  const at = Date.parse(iso)
  return Number.isNaN(at) ? null : at - nowMs
}

/** "done · sold 0.05 ETH at $3,788" from the order that finished the trigger. */
function doneText(trigger: Trigger): string {
  const { result } = trigger
  if (result && trigger.kind === 'sell' && result.amountIn) {
    return t('chat.triggerNowDoneSold', {
      amount: formatTokenAmount(result.amountIn.human),
      token: trigger.token.symbol,
      price: formatTriggerPrice(result.priceUsd),
    })
  }
  if (result && trigger.kind === 'buy' && result.amountOut) {
    return t('chat.triggerNowDoneBought', {
      amount: formatTokenAmount(result.amountOut.human),
      token: trigger.token.symbol,
      price: formatTriggerPrice(result.priceUsd),
    })
  }
  const alerted = trigger.fires.find((f) => f.status === 'alerted')
  if (trigger.kind === 'alert' && alerted?.priceUsd) {
    return t('chat.triggerNowDoneAlerted', { price: formatTriggerPrice(alerted.priceUsd) })
  }
  return trigger.statusReason
    ? `${t('chat.triggerNowDone')} · ${trigger.statusReason}`
    : t('chat.triggerNowDone')
}

/**
 * The live line under the hero: "ETH $3,790 · 0.3 % above the line · checked
 * 12 s ago", or what the trigger is doing instead. Re-derived by the mounter's
 * clock, so it must stay pure in `(trigger, now)`.
 */
export function nowText(trigger: Trigger, nowMs: number): string {
  switch (trigger.status) {
    case 'awaiting_approval': {
      const left = msUntil(trigger.expiresAt, nowMs)
      const head = t('chat.triggerNowAwaiting')
      if (left === null) return head
      if (left <= 0) return `${head} · ${t('chat.triggerProposalLapsing')}`
      return `${head} · ${t('chat.triggerExpiresIn', { time: unbreakable(formatCountdown(left)) })}`
    }
    case 'armed': {
      const ago = agoText(trigger.market.checkedAt, nowMs)
      const checked = ago ? t('chat.triggerChecked', { ago }) : ''
      const now = trigger.market.priceUsd
      if (now === null) {
        return [t('chat.triggerWaitingPrice'), checked].filter(Boolean).join(' · ')
      }
      const { hits, confirmTicks } = trigger.condition
      const parts = [`${trigger.token.symbol} ${formatTriggerPrice(now)}`]
      if (hits > 0 && hits < confirmTicks) {
        parts.push(tPlural('chat.triggerConfirming', confirmTicks - hits))
      } else {
        parts.push(distanceText(trigger))
      }
      parts.push(checked)
      return parts.filter(Boolean).join(' · ')
    }
    case 'triggered':
      return t('chat.triggerNowTriggered')
    case 'paused':
      return t('chat.triggerNowPaused')
    case 'done':
      return doneText(trigger)
    case 'stopped':
      return t('chat.triggerNowStopped')
    case 'rejected':
      return t('chat.triggerNowRejected')
    case 'expired':
      return t('chat.triggerNowExpired')
    default:
      return ''
  }
}

/**
 * When the clock should next wake for `trigger`'s live line: every second
 * while "checked N s ago" counts seconds or a proposal's expiry is under an
 * hour away, on the next minute boundary of the stamp above that, once a
 * minute when nothing ticks.
 */
export function triggerTickDelay(trigger: Trigger, nowMs: number): number {
  return liveTickDelay(trigger.status, trigger.market.checkedAt, trigger.expiresAt, nowMs)
}

/** `triggerTickDelay` for anything with a status, a "checked" stamp and a proposal expiry. */
function liveTickDelay(
  status: TriggerStatus,
  checkedAt: string | null,
  expiresAt: string | null,
  nowMs: number,
): number {
  if (status === 'awaiting_approval') {
    return dcaTickDelay(msUntil(expiresAt, nowMs))
  }
  if (status !== 'armed' || !checkedAt) return DCA_MINUTE_MS
  const at = Date.parse(checkedAt)
  if (Number.isNaN(at)) return DCA_MINUTE_MS
  const age = Math.max(0, nowMs - at)
  if (age < DCA_MINUTE_MS) return DCA_SECOND_MS
  if (age < HOUR_MS) return Math.max(DCA_SECOND_MS, DCA_MINUTE_MS - (age % DCA_MINUTE_MS))
  return DCA_MINUTE_MS
}

/** The controls a trigger in `status` offers, primary first. Terminal → none. */
export function triggerActionsFor(status: TriggerStatus): TriggerAction[] {
  switch (status) {
    case 'awaiting_approval':
      return ['approve', 'reject']
    case 'armed':
      return ['pause', 'fire', 'stop']
    case 'paused':
      return ['resume', 'fire', 'stop']
    case 'triggered':
      return ['stop']
    default:
      return []
  }
}

const ACTIONS = new Set<TriggerAction>(['approve', 'reject', 'pause', 'resume', 'fire', 'stop'])

/** Actions that take a confirming second click. */
const CONFIRMED = new Set<TriggerAction>(['fire', 'stop'])

export function triggerActionLabel(action: TriggerAction, kind: TriggerActionKind): string {
  switch (action) {
    case 'approve':
      return t('chat.triggerApprove')
    case 'reject':
      return t('chat.triggerReject')
    case 'pause':
      return t('chat.triggerPause')
    case 'resume':
      return t('chat.triggerResume')
    case 'fire':
      return kind === 'sell'
        ? t('chat.triggerSellNow')
        : kind === 'buy'
          ? t('chat.triggerBuyNow')
          : t('chat.triggerFire')
    case 'stop':
      return t('chat.triggerStop')
  }
}

function actionTitle(action: TriggerAction): string {
  switch (action) {
    case 'approve':
      return t('chat.triggerApproveTitle')
    case 'reject':
      return t('chat.triggerRejectTitle')
    case 'pause':
      return t('chat.triggerPauseTitle')
    case 'resume':
      return t('chat.triggerResumeTitle')
    case 'fire':
      return t('chat.triggerFireTitle')
    case 'stop':
      return t('chat.triggerStopTitle')
  }
}

export function triggerStatusLabel(status: TriggerStatus): string {
  switch (status) {
    case 'awaiting_approval':
      return t('chat.triggerStatusAwaiting')
    case 'armed':
      return t('chat.triggerStatusArmed')
    case 'triggered':
      return t('chat.triggerStatusTriggered')
    case 'paused':
      return t('chat.triggerStatusPaused')
    case 'done':
      return t('chat.triggerStatusDone')
    case 'stopped':
      return t('chat.triggerStatusStopped')
    case 'rejected':
      return t('chat.triggerStatusRejected')
    case 'expired':
      return t('chat.triggerStatusExpired')
    default:
      return t('chat.triggerStatusUnknown')
  }
}

function fireStatusLabel(status: TriggerFireStatus): string {
  switch (status) {
    case 'pending':
      return t('chat.triggerFirePending')
    case 'filled':
      return t('chat.triggerFireFilled')
    case 'parked':
      return t('chat.triggerFireParked')
    case 'alerted':
      return t('chat.triggerFireAlerted')
    case 'skipped':
      return t('chat.triggerFireSkipped')
    case 'failed':
      return t('chat.triggerFireFailed')
    case 'expired':
      return t('chat.triggerFireExpired')
    case 'rejected':
      return t('chat.triggerFireRejected')
    default:
      return t('chat.triggerFireUnknown')
  }
}

/** Words for a reason code, when the engine sent no human reason. */
function reasonCodeText(code: string | null): string {
  switch (code) {
    case 'insufficient_balance':
      return t('chat.triggerReasonBalance')
    case 'trading.interrupted':
      return t('chat.triggerReasonInterrupted')
    default:
      return code ?? ''
  }
}

/** What a fire did, in one phrase (the part after its status word). */
export function fireDetail(fire: TriggerFire, trigger: Trigger): string {
  const at =
    fire.priceUsd !== null
      ? t('chat.triggerFireAt', { price: formatTriggerPrice(fire.priceUsd) })
      : ''
  switch (fire.status) {
    case 'filled': {
      const result = trigger.result
      if (result && fire.orderId && result.orderId === fire.orderId && result.amountIn) {
        const sell = trigger.kind !== 'buy'
        return t('chat.triggerFireSwap', {
          amountIn: formatTokenAmount(result.amountIn.human),
          symbolIn: sell ? trigger.token.symbol : trigger.quote.symbol,
          amountOut: result.amountOut ? formatTokenAmount(result.amountOut.human) : NO_VALUE,
          symbolOut: sell ? trigger.quote.symbol : trigger.token.symbol,
          price: formatTriggerPrice(result.priceUsd ?? fire.priceUsd),
        })
      }
      return at
    }
    case 'skipped':
    case 'failed':
      return fire.reason ?? (reasonCodeText(fire.reasonCode) || at)
    case 'parked':
    case 'pending':
    case 'alerted':
      return at
    default:
      return fire.reason ?? at
  }
}

/** A local date + time for a tooltip or a fact; '' when the stamp is unusable. */
function formatWhen(iso: string, withTime: boolean): string {
  const at = Date.parse(iso)
  if (!iso || Number.isNaN(at)) return ''
  const options: Intl.DateTimeFormatOptions = withTime
    ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }
    : { month: 'short', day: 'numeric', year: 'numeric' }
  return new Date(at).toLocaleString('en-US', options)
}

/* ── Pure helpers: the gauge ────────────────────────────────────────────── */

export type TriggerProximity = 'met' | 'near' | 'far'

export interface TriggerGauge {
  /** The price domain the rail spans (left, right). */
  lo: number
  hi: number
  /** Positions 0–1 along the rail; null when that price is unknown or not drawn. */
  current: number | null
  /** The below/above line. */
  trigger: number | null
  /** trail: the highest price since arming. */
  peak: number | null
  /** trail: the price it would fire at now. */
  stop: number | null
  /** The span of the rail where the condition holds, 0–1, or null. */
  zone: [number, number] | null
  distancePct: number | null
  proximity: TriggerProximity | null
}

/** "met" at 0, "near" within TRIGGER_NEAR_PCT, else "far"; null when unknown. */
export function proximityOf(distancePct: number | null): TriggerProximity | null {
  if (distancePct === null || !Number.isFinite(distancePct)) return null
  if (distancePct === 0) return 'met'
  return Math.abs(distancePct) <= TRIGGER_NEAR_PCT ? 'near' : 'far'
}

/**
 * Lay the prices out on one rail: the current price, the line (below/above)
 * or the peak and the stop (trail), on an axis padded past the data (by at
 * least TRIGGER_GAUGE_MIN_PAD of the line) so the marks never sit on the
 * ends. Null when there is no line to draw.
 */
export function triggerGauge(trigger: Trigger): TriggerGauge | null {
  const c = trigger.condition
  const trail = c.direction === 'trail'
  if (c.direction === 'unknown') return null
  const line = trail ? c.stopPriceUsd : c.priceUsd
  if (line === null) return null
  const now = trigger.market.priceUsd
  const peak = trail ? c.peakPriceUsd : null
  const known = [now, line, peak].filter((p): p is number => p !== null)
  const min = Math.min(...known)
  const max = Math.max(...known)
  const pad = Math.max((max - min) * 0.25, line * TRIGGER_GAUGE_MIN_PAD)
  const lo = Math.max(0, min - pad)
  const hi = max + pad
  const at = (p: number | null): number | null =>
    p === null || !(hi > lo) ? null : clamp01((p - lo) / (hi - lo))
  const linePos = at(line) as number
  const distancePct = distancePctOf(trigger)
  return {
    lo,
    hi,
    current: at(now),
    trigger: trail ? null : linePos,
    peak: at(peak),
    stop: trail ? linePos : null,
    zone: c.direction === 'above' ? [linePos, 1] : [0, linePos],
    distancePct,
    proximity: proximityOf(distancePct),
  }
}

/* ── Pure helpers: brackets (docs/brackets.md) ──────────────────────────── */

/**
 * The leg as a bracket names it in a phrase: "take-profit", "stop-loss"; a
 * range alert's (`kind` = `alert`, the bracket's or the leg's) "ceiling",
 * "floor": nothing is sold, so there is no profit to take nor loss to stop.
 */
export function bracketLegWord(leg: BracketLeg, kind: string = 'sell'): string {
  if (kind === 'alert')
    return leg === 'tp' ? t('chat.bracketLegCeiling') : t('chat.bracketLegFloor')
  return leg === 'tp' ? t('chat.bracketLegTp') : t('chat.bracketLegSl')
}

/** A leg's trigger, or null when the payload left it out. */
export function bracketLegOf(bracket: Bracket, leg: BracketLeg): Trigger | null {
  return leg === 'tp' ? bracket.takeProfit : bracket.stopLoss
}

function bracketTrails(bracket: Bracket): boolean {
  return bracket.lines.trailPct !== null || bracket.stopLoss?.condition.direction === 'trail'
}

/** A leg row's title: "Take-profit", "Stop-loss", "Trailing stop"; an alert's "Ceiling", "Floor". */
export function bracketLegTitle(bracket: Bracket, leg: BracketLeg): string {
  if (bracket.kind === 'alert') {
    if (leg === 'tp') return t('chat.bracketLegTitleCeiling')
    return bracketTrails(bracket)
      ? t('chat.bracketLegTitleTrailFloor')
      : t('chat.bracketLegTitleFloor')
  }
  if (leg === 'tp') return t('chat.bracketLegTitleTp')
  return bracketTrails(bracket) ? t('chat.bracketLegTitleTrail') : t('chat.bracketLegTitleSl')
}

/**
 * Whether a leg can still fire: not done, stopped, rejected or expired. A
 * leg the payload left out counts as live (the bracket's status speaks for
 * it). After a partial take-profit the take-profit leg is done while the
 * bracket stays armed: it has no distance, it is never the nearest.
 */
export function isBracketLegLive(bracket: Bracket, leg: BracketLeg): boolean {
  const trigger = bracketLegOf(bracket, leg)
  return !trigger || !isTriggerTerminal(trigger.status)
}

/**
 * What became of a leg that will not fire again, for the live line of a
 * bracket still armed: "take-profit filled", "ceiling alerted", "stop-loss
 * stopped"; '' for a live leg.
 */
export function bracketLegOverText(bracket: Bracket, leg: BracketLeg): string {
  const trigger = bracketLegOf(bracket, leg)
  if (!trigger || !isTriggerTerminal(trigger.status)) return ''
  const word = bracketLegWord(leg, bracket.kind)
  if (trigger.status === 'done') {
    const alerted = bracket.kind === 'alert' || trigger.kind === 'alert'
    return t(alerted ? 'chat.bracketLegOverAlerted' : 'chat.bracketLegOverFilled', { leg: word })
  }
  const state =
    trigger.status === 'stopped'
      ? t('chat.bracketLegStopped')
      : trigger.status === 'rejected'
        ? t('chat.bracketLegRejected')
        : t('chat.bracketLegExpired')
  return t('chat.bracketLegOver', { leg: word, state })
}

/** A leg's line in words: the engine's label, else "over $4,560" / "under $3,420" / "10 % below peak". */
export function bracketLineLabel(bracket: Bracket, leg: BracketLeg): string {
  const { lines } = bracket
  if (leg === 'tp') {
    return (
      lines.takeProfitLabel ||
      bracket.takeProfit?.condition.label ||
      t('chat.triggerCondAbove', { price: formatTriggerPrice(lines.takeProfitUsd) })
    )
  }
  if (lines.stopLossLabel) return lines.stopLossLabel
  if (bracket.stopLoss?.condition.label) return bracket.stopLoss.condition.label
  if (lines.trailPct !== null) {
    return t('chat.triggerCondTrail', { pct: pctNumber(lines.trailPct) })
  }
  return t('chat.triggerCondBelow', { price: formatTriggerPrice(lines.stopLossUsd) })
}

/** What the bracket sells, in a phrase: "sell 100 % of ETH", "sell 0.05 ETH", "sell $100 of ETH". */
function bracketSellPhrase(bracket: Bracket): string {
  const { action, token } = bracket
  const symbol = token.symbol
  if (action.amountPct !== null) {
    return t('chat.triggerSellPct', { pct: pctNumber(action.amountPct), token: symbol })
  }
  if (action.amount) {
    return t('chat.triggerSellAmount', {
      amount: formatTokenAmount(action.amount.human),
      token: symbol,
    })
  }
  if (action.amountUsd !== null) {
    return t('chat.triggerSellUsd', { usd: formatDcaUsd(action.amountUsd), token: symbol })
  }
  return bracket.kind === 'sell'
    ? t('chat.triggerSellPct', { pct: '100', token: symbol })
    : t('chat.triggerActUnknown', { token: symbol })
}

/**
 * The card's sentence: "sell 100 % of ETH · take profit over $4,560 · stop
 * under $3,420"; an alert: "notify when ETH is over $4,560 or under $3,420".
 */
export function bracketHeroText(bracket: Bracket): string {
  const tp = bracketLineLabel(bracket, 'tp')
  const sl = bracketLineLabel(bracket, 'sl')
  if (bracket.kind === 'alert') {
    return t('chat.bracketHeroAlert', { token: bracket.token.symbol, tp, sl })
  }
  return t('chat.bracketHero', { action: bracketSellPhrase(bracket), tp, sl })
}

/** A signed percent: "+20.3 %", "−9.8 %", "0 %"; '' when unknown. */
export function formatSignedPct(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return ''
  if (value === 0) return '0 %'
  return `${value > 0 ? '+' : '−'}${Math.abs(value).toFixed(1)} %`
}

/**
 * % rise to the take-profit line: the engine's, else derived. 0 once met;
 * null when unknown, or when the take-profit leg is over (a partial
 * take-profit filled it: it will not fire again).
 */
export function bracketUpsidePct(bracket: Bracket): number | null {
  if (!isBracketLegLive(bracket, 'tp')) return null
  if (bracket.market.upsidePct !== null) return bracket.market.upsidePct
  const now = bracket.market.priceUsd
  const line = bracket.lines.takeProfitUsd
  if (now === null || line === null) return null
  return now >= line ? 0 : ((line - now) / now) * 100
}

/** % fall to the stop line (negative): the engine's, else derived. 0 once met; null when unknown or over. */
export function bracketDownsidePct(bracket: Bracket): number | null {
  if (!isBracketLegLive(bracket, 'sl')) return null
  if (bracket.market.downsidePct !== null) return bracket.market.downsidePct
  const now = bracket.market.priceUsd
  const line = bracket.lines.stopLossUsd
  if (now === null || line === null) return null
  return now <= line ? 0 : ((line - now) / now) * 100
}

/**
 * The leg closer to firing: the only live one when its twin is over, else
 * the engine's, else the smaller distance; null when unknown or neither leg
 * can fire.
 */
export function bracketNearest(bracket: Bracket): BracketLeg | null {
  const tpLive = isBracketLegLive(bracket, 'tp')
  const slLive = isBracketLegLive(bracket, 'sl')
  if (!tpLive && !slLive) return null
  if (!tpLive) return 'sl'
  if (!slLive) return 'tp'
  if (bracket.market.nearest) return bracket.market.nearest
  const up = bracketUpsidePct(bracket)
  const down = bracketDownsidePct(bracket)
  if (up === null || down === null) return null
  return Math.abs(up) < Math.abs(down) ? 'tp' : 'sl'
}

/** The signed distance to the nearest leg's line. */
export function bracketDistancePct(bracket: Bracket): number | null {
  const nearest = bracketNearest(bracket)
  if (nearest === null) return null
  return nearest === 'tp' ? bracketUpsidePct(bracket) : bracketDownsidePct(bracket)
}

/** Reward over risk: the engine's, else upside / |downside|; null when either is 0, unknown or over. */
export function bracketRewardRisk(bracket: Bracket): number | null {
  if (!isBracketLegLive(bracket, 'tp') || !isBracketLegLive(bracket, 'sl')) return null
  if (bracket.market.rewardRisk !== null) return bracket.market.rewardRisk
  const up = bracketUpsidePct(bracket)
  const down = bracketDownsidePct(bracket)
  if (up === null || down === null || up === 0 || down === 0) return null
  return Math.round((up / Math.abs(down)) * 10) / 10
}

/** "2.0 : 1", or — when unknown. */
export function bracketRewardRiskText(bracket: Bracket): string {
  const rr = bracketRewardRisk(bracket)
  return rr === null ? NO_VALUE : t('chat.bracketRewardRisk', { ratio: rr.toFixed(1) })
}

/** The status word on a bracket's pill (the trigger's words: one status, one voice). */
export function bracketStatusLabel(status: TriggerStatus): string {
  return triggerStatusLabel(status)
}

/** The armed leg still confirming its condition (hits under its ticks), nearest first. */
function confirmingLeg(bracket: Bracket): BracketLeg | null {
  const nearest = bracketNearest(bracket) ?? 'sl'
  const order: BracketLeg[] = nearest === 'tp' ? ['tp', 'sl'] : ['sl', 'tp']
  return (
    order.find((leg) => {
      const tr = bracketLegOf(bracket, leg)
      if (!tr || tr.status !== 'armed') return false
      const { hits, confirmTicks } = tr.condition
      return hits > 0 && hits < confirmTicks
    }) ?? null
  )
}

/** The leg whose fire is in flight: the `triggered` one, else the fired, else the nearest. */
function firingLeg(bracket: Bracket): BracketLeg {
  if (bracket.takeProfit?.status === 'triggered') return 'tp'
  if (bracket.stopLoss?.status === 'triggered') return 'sl'
  return bracket.fired ?? bracketNearest(bracket) ?? 'sl'
}

/** "done · take-profit: sold 0.05 ETH at $4,560", from the leg that filled. */
function bracketDoneText(bracket: Bracket): string {
  const leg = bracket.fired
  const word = leg ? bracketLegWord(leg, bracket.kind) : ''
  const { result } = bracket
  const head = t('chat.triggerNowDone')
  const tagged = (what: string): string =>
    word ? t('chat.bracketNowDoneLeg', { leg: word, what }) : `${head} · ${what}`
  if (result?.amountIn && bracket.kind !== 'alert') {
    return tagged(
      t('chat.bracketSold', {
        amount: formatTokenAmount(result.amountIn.human),
        token: bracket.token.symbol,
        price: formatTriggerPrice(result.priceUsd),
      }),
    )
  }
  const firedTrigger = leg ? bracketLegOf(bracket, leg) : null
  const alerted = firedTrigger?.fires.find((f) => f.status === 'alerted')
  if (alerted?.priceUsd) {
    return tagged(t('chat.bracketAlerted', { price: formatTriggerPrice(alerted.priceUsd) }))
  }
  return bracket.statusReason ? `${head} · ${bracket.statusReason}` : head
}

/**
 * The live line under a bracket's hero: "ETH $3,790 · +20.3 % to take-profit
 * · −9.8 % to stop · checked 12 s ago", or what the bracket is doing instead.
 * Pure in `(bracket, now)`: the mounter's clock re-derives it.
 */
export function bracketNowText(bracket: Bracket, nowMs: number): string {
  switch (bracket.status) {
    case 'awaiting_approval': {
      const left = msUntil(bracket.expiresAt, nowMs)
      const head = t('chat.triggerNowAwaiting')
      if (left === null) return head
      if (left <= 0) return `${head} · ${t('chat.triggerProposalLapsing')}`
      return `${head} · ${t('chat.triggerExpiresIn', { time: unbreakable(formatCountdown(left)) })}`
    }
    case 'armed': {
      const ago = agoText(bracket.market.checkedAt, nowMs)
      const checked = ago ? t('chat.triggerChecked', { ago }) : ''
      const now = bracket.market.priceUsd
      if (now === null) {
        return [t('chat.triggerWaitingPrice'), checked].filter(Boolean).join(' · ')
      }
      const parts = [`${bracket.token.symbol} ${formatTriggerPrice(now)}`]
      const confirming = confirmingLeg(bracket)
      if (confirming) {
        const leg = bracketLegOf(bracket, confirming)!
        parts.push(
          tPlural('chat.bracketConfirming', leg.condition.confirmTicks - leg.condition.hits, {
            leg: bracketLegWord(confirming, bracket.kind),
          }),
        )
      } else {
        const alert = bracket.kind === 'alert'
        const up = bracketUpsidePct(bracket)
        const down = bracketDownsidePct(bracket)
        // A leg that is over says what became of it, never a distance.
        const tpOver = bracketLegOverText(bracket, 'tp')
        if (tpOver) parts.push(tpOver)
        else if (up !== null) {
          const pct = formatSignedPct(up)
          parts.push(
            up === 0
              ? t(alert ? 'chat.bracketAtCeiling' : 'chat.bracketAtTp')
              : t(alert ? 'chat.bracketToCeiling' : 'chat.bracketToTp', { pct }),
          )
        }
        const slOver = bracketLegOverText(bracket, 'sl')
        if (slOver) parts.push(slOver)
        else if (down !== null) {
          const pct = formatSignedPct(down)
          parts.push(
            down === 0
              ? t(alert ? 'chat.bracketAtFloor' : 'chat.bracketAtSl')
              : t(alert ? 'chat.bracketToFloor' : 'chat.bracketToSl', { pct }),
          )
        }
      }
      parts.push(checked)
      return parts.filter(Boolean).join(' · ')
    }
    case 'triggered': {
      const leg = bracketLegWord(firingLeg(bracket), bracket.kind)
      return bracket.kind === 'alert'
        ? t('chat.bracketNowFiring', { leg })
        : t('chat.bracketNowOrderOpen', { leg })
    }
    case 'paused':
      return t('chat.triggerNowPaused')
    case 'done':
      return bracketDoneText(bracket)
    case 'stopped':
      return t('chat.triggerNowStopped')
    case 'rejected':
      return t('chat.triggerNowRejected')
    case 'expired':
      return t('chat.triggerNowExpired')
    default:
      return ''
  }
}

/** `triggerTickDelay` for a bracket card's live line. */
export function bracketTickDelay(bracket: Bracket, nowMs: number): number {
  return liveTickDelay(bracket.status, bracket.market.checkedAt, bracket.expiresAt, nowMs)
}

/** Whether a paused leg is on hold for its sibling's fire (`OCO_HOLD` prefix). */
export function isLegOnHold(leg: Trigger): boolean {
  return leg.status === 'paused' && /^on hold\b/i.test(leg.statusReason ?? '')
}

/**
 * A leg's state in the legs strip: "armed · 1 of 2 checks", "armed · −9.8 %",
 * "on hold", "stopped · take-profit filled", "done · sold 0.05 ETH at $4,560".
 */
export function bracketLegStateText(leg: Trigger | null): string {
  if (!leg) return NO_VALUE
  switch (leg.status) {
    case 'awaiting_approval':
      return t('chat.triggerNowAwaiting')
    case 'armed': {
      const { hits, confirmTicks } = leg.condition
      if (hits > 0 && hits < confirmTicks) {
        return t('chat.bracketLegChecks', { hits: String(hits), ticks: String(confirmTicks) })
      }
      const distance = formatDistance(distancePctOf(leg))
      return [t('chat.bracketLegArmed'), distance].filter(Boolean).join(' · ')
    }
    case 'paused':
      if (isLegOnHold(leg)) return t('chat.bracketLegOnHold')
      return leg.statusReason
        ? `${t('chat.bracketLegPaused')} · ${statusReasonText(leg.statusReason)}`
        : t('chat.bracketLegPaused')
    case 'triggered':
      return leg.kind === 'alert' ? t('chat.bracketLegFired') : t('chat.triggerNowTriggered')
    case 'done':
      return doneText(leg)
    case 'stopped':
      return leg.statusReason
        ? `${t('chat.bracketLegStopped')} · ${statusReasonText(leg.statusReason)}`
        : t('chat.bracketLegStopped')
    case 'rejected':
      return t('chat.bracketLegRejected')
    case 'expired':
      return t('chat.bracketLegExpired')
    default:
      return NO_VALUE
  }
}

/** One row of a bracket's merged fires: the fire, the leg it belongs to, that leg. */
export interface BracketFireRow {
  leg: BracketLeg
  fire: TriggerFire
  trigger: Trigger
}

/** Both legs' fires, newest first, at most TRIGGER_RECENT_FIRES. */
export function bracketFires(bracket: Bracket): BracketFireRow[] {
  const rows: BracketFireRow[] = []
  for (const leg of ['tp', 'sl'] as const) {
    const trigger = bracketLegOf(bracket, leg)
    trigger?.fires.forEach((fire) => rows.push({ leg, fire, trigger }))
  }
  const stamp = (row: BracketFireRow): number => {
    const at = Date.parse(row.fire.at)
    return Number.isNaN(at) ? -Infinity : at
  }
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => stamp(b.row) - stamp(a.row) || b.row.fire.n - a.row.fire.n || a.index - b.index)
    .slice(0, TRIGGER_RECENT_FIRES)
    .map(({ row }) => row)
}

/**
 * "100 % · ≈ $189", "50 % at take-profit, 100 % at stop", "0.05 ETH · ≈ $189",
 * "notify only"; once finished with a fill, what the fill moved.
 */
export function bracketSizeText(bracket: Bracket): string {
  return joinSize(bracketSizeParts(bracket))
}

/** `bracketSizeText` in its two parts: what moves, and its "≈ $…" (its own line in the cell). */
export function bracketSizeParts(bracket: Bracket): TriggerSizeParts {
  const { action } = bracket
  const only = (main: string): TriggerSizeParts => ({ main, approx: '' })
  if (bracket.kind === 'alert') return only(t('chat.triggerSizeNotify'))
  if (isTriggerTerminal(bracket.status) && bracket.result) {
    const moved = movedParts(bracket.result, true, bracket.token, bracket.quote)
    if (moved) return moved
  }
  const approx =
    action.estimatedUsd !== null
      ? t('chat.triggerApprox', { usd: formatDcaUsd(action.estimatedUsd) })
      : ''
  if (action.amountPct !== null) {
    if (action.tpPct !== null && action.tpPct < action.amountPct) {
      return only(
        t('chat.bracketSizeSplit', {
          tp: pctNumber(action.tpPct),
          all: pctNumber(action.amountPct),
        }),
      )
    }
    return { main: `${pctNumber(action.amountPct)} %`, approx }
  }
  if (action.amount) {
    return { main: `${formatTokenAmount(action.amount.human)} ${bracket.token.symbol}`, approx }
  }
  if (action.amountUsd !== null) return only(formatDcaUsd(action.amountUsd))
  return only(NO_VALUE)
}

/** A bracket's controls: the trigger's, per status. */
export function bracketActionsFor(status: TriggerStatus): TriggerAction[] {
  return triggerActionsFor(status)
}

/* ── Pure helpers: the range gauge ── */

export interface BracketGauge {
  /** The price domain the rail spans. */
  lo: number
  hi: number
  /** Positions 0–1 along the rail. */
  sl: number
  tp: number
  /** The price; null when unknown. Clamped into the rail. */
  current: number | null
  /** Which end the price ran off, when it is past the rail. */
  clamped: 'below' | 'above' | null
  /** The span between the lines. */
  zone: [number, number]
  nearest: BracketLeg | null
  /** The nearest leg's signed distance. */
  distancePct: number | null
  proximity: TriggerProximity | null
}

/**
 * Lay the two lines out on one rail, the stop at the left and the take-profit
 * at the right, padded past them by the trigger gauge's rule (a quarter of
 * the span, at least TRIGGER_GAUGE_MIN_PAD of the take-profit line) so the
 * ticks never sit on the ends; the price is the dot, clamped into the rail
 * when it is past either end. Null without both lines, or with the
 * take-profit not above the stop.
 */
export function bracketGauge(bracket: Bracket): BracketGauge | null {
  const sl = bracket.lines.stopLossUsd
  const tp = bracket.lines.takeProfitUsd
  if (sl === null || tp === null || !(tp > sl)) return null
  const pad = Math.max((tp - sl) * 0.25, tp * TRIGGER_GAUGE_MIN_PAD)
  const lo = Math.max(0, sl - pad)
  const hi = tp + pad
  const at = (p: number): number => clamp01((p - lo) / (hi - lo))
  const now = bracket.market.priceUsd
  const slAt = at(sl)
  const tpAt = at(tp)
  const distancePct = bracketDistancePct(bracket)
  return {
    lo,
    hi,
    sl: slAt,
    tp: tpAt,
    current: now === null ? null : at(now),
    clamped: now === null ? null : now < lo ? 'below' : now > hi ? 'above' : null,
    zone: [slAt, tpAt],
    nearest: bracketNearest(bracket),
    distancePct,
    proximity: proximityOf(distancePct),
  }
}

/* ── Pure helpers: layout ───────────────────────────────────────────────── */

export type TriggerLayout = 'wide' | 'narrow'

export function triggerLayoutFor(cardPx: number): TriggerLayout {
  return cardPx >= TRIGGER_WIDE_MIN_PX ? 'wide' : 'narrow'
}

/* ── DOM building ───────────────────────────────────────────────────────── */

const SVG_NS = 'http://www.w3.org/2000/svg'

function el(tag: string, className: string, content?: string): HTMLElement {
  const node = document.createElement(tag)
  if (className) node.className = className
  // textContent, never innerHTML — see the security note at the top.
  if (content !== undefined) node.textContent = content
  return node
}

function svg(tag: string, attrs: Record<string, string | number>): SVGElement {
  const node = document.createElementNS(SVG_NS, tag)
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value))
  return node
}

function hidden(node: HTMLElement): HTMLElement {
  node.setAttribute('aria-hidden', 'true')
  return node
}

/** Everything the DOM builders need from the mounter. */
export interface TriggerRenderContext {
  now: () => number
  copyText: (value: string) => void | Promise<void>
  /** A cancellable timer the mounter clears on unmount. */
  setTimer: (fn: () => void, ms: number) => void
  /** Offer ↻ on a card whose payload says how to re-run it. */
  readonly canRefresh?: boolean
  /**
   * Offer the trigger controls (approve, pause, fire now, stop…). Only an
   * operator connection (the desktop desk) can use them; the web never sets it.
   */
  readonly canWrite?: boolean
}

function statusPill(status: TriggerStatus): HTMLElement {
  const pill = el('span', 'trigger-pill')
  pill.dataset.status = status
  pill.append(
    hidden(el('span', 'trigger-pill__dot')),
    el('span', 'trigger-pill__text', triggerStatusLabel(status)),
  )
  return pill
}

/** What a controls row acts on: a trigger (`data-trigger-id`) or a bracket (`data-bracket-id`). */
interface ControlTarget {
  id: string
  name: string
  scope: 'trigger' | 'bracket'
  label: (action: TriggerAction) => string
  title: (action: TriggerAction) => string
}

function stampTarget(node: HTMLElement, target: ControlTarget): void {
  if (target.scope === 'bracket') node.dataset.bracketId = target.id
  else node.dataset.triggerId = target.id
}

function actionButton(
  action: TriggerAction,
  target: ControlTarget,
  compact: boolean,
): HTMLButtonElement {
  const button = el('button', 'trigger-action', target.label(action)) as HTMLButtonElement
  button.type = 'button'
  button.dataset.triggerOp = action
  stampTarget(button, target)
  button.title = target.title(action)
  if (action === 'approve') button.dataset.triggerTone = 'primary'
  else if (action === 'stop' || action === 'reject') button.dataset.triggerTone = 'danger'
  if (compact) button.dataset.triggerCompact = 'true'
  return button
}

/**
 * The controls row for one trigger or bracket, keyed by `data-trigger-id` /
 * `data-bracket-id`. The mounter's click handler calls the RPC and drives
 * `data-trigger-busy` and the error line. Null with nothing left to control.
 */
function controlsRow(
  actions: TriggerAction[],
  target: ControlTarget,
  compact: boolean,
): HTMLElement | null {
  if (actions.length === 0) return null
  const row = el('div', compact ? 'trigger-actions trigger-actions--compact' : 'trigger-actions')
  stampTarget(row, target)
  row.setAttribute('role', 'group')
  row.setAttribute('aria-label', t('chat.triggerActionsLabel', { name: target.name }))
  const buttons = el('div', 'trigger-actions__buttons')
  actions.forEach((action) => buttons.append(actionButton(action, target, compact)))
  const error = el('p', 'trigger-actions__error')
  error.setAttribute('role', 'alert')
  error.hidden = true
  row.append(buttons, error)
  return row
}

/** A trigger's controls; a bracket's leg has none (its writes go through the bracket). */
function actionsRow(trigger: Trigger, compact: boolean): HTMLElement | null {
  if (trigger.bracket) return null
  return controlsRow(
    triggerActionsFor(trigger.status),
    {
      id: trigger.id,
      name: trigger.name,
      scope: 'trigger',
      label: (action) => triggerActionLabel(action, trigger.kind),
      title: actionTitle,
    },
    compact,
  )
}

export function bracketActionLabel(action: TriggerAction, kind: BracketKind): string {
  if (action === 'fire') {
    return kind === 'alert' ? t('chat.bracketNotifyNow') : t('chat.triggerSellNow')
  }
  return triggerActionLabel(action, 'sell')
}

function bracketActionTitle(action: TriggerAction): string {
  switch (action) {
    case 'approve':
      return t('chat.bracketApproveTitle')
    case 'reject':
      return t('chat.bracketRejectTitle')
    case 'pause':
      return t('chat.bracketPauseTitle')
    case 'resume':
      return t('chat.bracketResumeTitle')
    case 'fire':
      return t('chat.bracketFireTitle')
    case 'stop':
      return t('chat.bracketStopTitle')
  }
}

function bracketActionsRow(bracket: Bracket, compact: boolean): HTMLElement | null {
  return controlsRow(
    bracketActionsFor(bracket.status),
    {
      id: bracket.id,
      name: bracket.name,
      scope: 'bracket',
      label: (action) => bracketActionLabel(action, bracket.kind),
      title: bracketActionTitle,
    },
    compact,
  )
}

function nowNode(trigger: Trigger, nowMs: number): HTMLElement {
  const node = el('p', 'trigger-card__now', nowText(trigger, nowMs))
  node.setAttribute('aria-live', 'off')
  if (trigger.condition.hits > 0) {
    node.dataset.triggerHits = `${trigger.condition.hits}/${trigger.condition.confirmTicks}`
  }
  return node
}

/** "line set at −10 % from $3,780" when the user gave a percent at creation. */
function fromNode(trigger: Trigger): HTMLElement | null {
  const { fromPriceUsd, priceUsd } = trigger.condition
  if (fromPriceUsd === null || priceUsd === null) return null
  const move = ((priceUsd - fromPriceUsd) / fromPriceUsd) * 100
  return el(
    'p',
    'trigger-card__from',
    t('chat.triggerFromPrice', {
      pct: `${move > 0 ? '+' : move < 0 ? '−' : ''}${Math.abs(move).toFixed(1)} %`,
      price: formatTriggerPrice(fromPriceUsd),
    }),
  )
}

/* ── the gauge ── */

function labelAlign(x: number): 'start' | 'center' | 'end' {
  return x < 0.15 ? 'start' : x > 0.85 ? 'end' : 'center'
}

function pct(x: number): string {
  return `${(x * 100).toFixed(2)}%`
}

function gaugeNode(trigger: Trigger): HTMLElement | null {
  const model = triggerGauge(trigger)
  if (!model) return null
  const c = trigger.condition
  const trail = c.direction === 'trail'
  const linePrice = trail ? c.stopPriceUsd : c.priceUsd
  const gauge = el('div', 'trigger-gauge')
  gauge.dataset.triggerDirection = c.direction
  // A finished trigger has no distance left to tint: the dot is just today's price.
  if (!isTriggerTerminal(trigger.status)) {
    if (model.distancePct !== null) {
      gauge.dataset.triggerDist = String(Math.round(model.distancePct * 100) / 100)
    }
    if (model.proximity) gauge.dataset.triggerProximity = model.proximity
  }
  gauge.setAttribute('role', 'img')
  gauge.setAttribute(
    'aria-label',
    trail
      ? t('chat.triggerGaugeTrailLabel', {
          now: formatTriggerPrice(trigger.market.priceUsd),
          peak: formatTriggerPrice(c.peakPriceUsd),
          stop: formatTriggerPrice(c.stopPriceUsd),
        })
      : t('chat.triggerGaugeLabel', {
          now: formatTriggerPrice(trigger.market.priceUsd),
          line: formatTriggerPrice(c.priceUsd),
        }),
  )

  // Percent coordinates without a viewBox: the dot stays round at any width.
  const chart = svg('svg', {
    class: 'trigger-gauge__svg',
    'aria-hidden': 'true',
    focusable: 'false',
  })
  if (model.zone) {
    chart.append(
      svg('rect', {
        class: 'trigger-gauge__zone',
        x: pct(model.zone[0]),
        width: pct(Math.max(0, model.zone[1] - model.zone[0])),
        y: '30%',
        height: '40%',
      }),
    )
  }
  chart.append(
    svg('line', { class: 'trigger-gauge__rail', x1: '0%', x2: '100%', y1: '50%', y2: '50%' }),
  )
  const lineAt = trail ? model.stop : model.trigger
  if (lineAt !== null) {
    chart.append(
      svg('line', {
        class: 'trigger-gauge__line',
        'data-mark': trail ? 'stop' : 'line',
        x1: pct(lineAt),
        x2: pct(lineAt),
        y1: '10%',
        y2: '90%',
      }),
    )
  }
  if (model.peak !== null) {
    chart.append(
      svg('line', {
        class: 'trigger-gauge__peak',
        'data-mark': 'peak',
        x1: pct(model.peak),
        x2: pct(model.peak),
        y1: '25%',
        y2: '75%',
      }),
    )
  }
  if (model.current !== null) {
    chart.append(
      svg('circle', {
        class: 'trigger-gauge__now',
        'data-mark': 'now',
        cx: pct(model.current),
        cy: '50%',
        r: 5,
      }),
    )
  }
  gauge.append(chart)

  // Labels: the line (and peak) above the rail, the current price under it,
  // so a price sitting on the line never draws over its label.
  const label = (mark: string, x: number, content: string): HTMLElement => {
    const node = el('span', 'trigger-gauge__label', content)
    node.dataset.mark = mark
    node.dataset.align = labelAlign(x)
    node.style.left = pct(x)
    return node
  }
  const top = hidden(el('div', 'trigger-gauge__labels'))
  top.dataset.row = 'top'
  if (lineAt !== null) {
    top.append(
      label(
        trail ? 'stop' : 'line',
        lineAt,
        trail
          ? t('chat.triggerGaugeStop', { price: formatTriggerPrice(linePrice) })
          : t('chat.triggerGaugeLine', { price: formatTriggerPrice(linePrice) }),
      ),
    )
  }
  if (model.peak !== null) {
    top.append(
      label(
        'peak',
        model.peak,
        t('chat.triggerGaugePeak', { price: formatTriggerPrice(c.peakPriceUsd) }),
      ),
    )
  }
  const bottom = hidden(el('div', 'trigger-gauge__labels'))
  bottom.dataset.row = 'bottom'
  if (model.current !== null) {
    bottom.append(
      label(
        'now',
        model.current,
        t('chat.triggerGaugeNow', { price: formatTriggerPrice(trigger.market.priceUsd) }),
      ),
    )
  }
  gauge.prepend(top)
  gauge.append(bottom)
  return gauge
}

/* ── facts ── */

/** A size in two parts: what moves, and its dollar estimate ('' when none). */
export interface TriggerSizeParts {
  main: string
  approx: string
}

function joinSize(parts: TriggerSizeParts): string {
  return [parts.main, parts.approx].filter(Boolean).join(' · ')
}

/**
 * One fact cell: label over value. A value never overlaps its neighbour: it
 * wraps (CSS), and one longer than TRIGGER_FACT_WIDE_CHARS stamps
 * `data-trigger-fact-span="2"` so a skin can give it two columns. A size's
 * "≈ $…" is its own `.trigger-fact__sub` line inside the value (after a
 * hidden " · ", so the value still reads as one phrase).
 */
function fact(
  key: string,
  label: string,
  value: string | TriggerSizeParts,
  noValue = false,
): HTMLElement {
  const cell = el('div', 'trigger-fact')
  cell.dataset.triggerFact = key
  const parts = typeof value === 'string' ? { main: value, approx: '' } : value
  const node = el('span', 'trigger-fact__value', parts.main)
  if (parts.approx) {
    node.append(
      hidden(el('span', 'trigger-sep', ' · ')),
      el('span', 'trigger-fact__sub', parts.approx),
    )
  }
  if ([...parts.main].length > TRIGGER_FACT_WIDE_CHARS) cell.dataset.triggerFactSpan = '2'
  if (noValue) {
    node.dataset.triggerNoValue = 'true'
    node.title = t('chat.triggerNoPriceTitle')
  }
  cell.append(el('span', 'trigger-fact__label', label), node)
  return cell
}

/** A trigger that will not watch the price again: done, stopped, rejected, expired. */
export function isTriggerTerminal(status: TriggerStatus): boolean {
  return status === 'done' || status === 'stopped' || status === 'rejected' || status === 'expired'
}

/**
 * What the fill moved: "0.02 WETH → 42.4 USDC" and "≈ $42.4" (amountIn alone
 * when the out side is unknown); null when there is no result to read.
 */
function movedParts(
  result: TriggerResult | null,
  sell: boolean,
  token: LpToken,
  quote: LpToken,
): TriggerSizeParts | null {
  if (!result?.amountIn?.human) return null
  const symbolIn = sell ? token.symbol : quote.symbol
  const symbolOut = sell ? quote.symbol : token.symbol
  const amountIn = formatTokenAmount(result.amountIn.human)
  const moved = result.amountOut?.human
    ? t('chat.triggerSizeMoved', {
        amountIn,
        symbolIn,
        amountOut: formatTokenAmount(result.amountOut.human),
        symbolOut,
      })
    : `${amountIn} ${symbolIn}`
  const usd = result.amountIn.usd ?? result.amountOut?.usd ?? null
  return {
    main: moved,
    approx: usd !== null ? t('chat.triggerApprox', { usd: formatDcaUsd(usd) }) : '',
  }
}

/**
 * "50 % · ≈ $189", "$50", "0.05 ETH · ≈ $189", "notify only", "—"; once a
 * terminal trigger has filled, what the fill moved instead of what a fire
 * would move now.
 */
export function sizeText(trigger: Trigger): string {
  return joinSize(sizeParts(trigger))
}

/** `sizeText` in its two parts: what moves, and its "≈ $…". */
export function sizeParts(trigger: Trigger): TriggerSizeParts {
  const { action } = trigger
  const only = (main: string): TriggerSizeParts => ({ main, approx: '' })
  if (trigger.kind === 'alert') return only(t('chat.triggerSizeNotify'))
  if (isTriggerTerminal(trigger.status)) {
    const moved = movedParts(trigger.result, trigger.kind !== 'buy', trigger.token, trigger.quote)
    if (moved) return moved
  }
  const approx =
    action.estimatedUsd !== null
      ? t('chat.triggerApprox', { usd: formatDcaUsd(action.estimatedUsd) })
      : ''
  if (trigger.kind === 'sell') {
    if (action.amountPct !== null) return { main: `${pctNumber(action.amountPct)} %`, approx }
    if (action.amount) {
      return { main: `${formatTokenAmount(action.amount.human)} ${trigger.token.symbol}`, approx }
    }
    return only(formatDcaUsd(action.amountUsd))
  }
  if (trigger.kind === 'buy') return only(formatDcaUsd(action.amountUsd))
  return only(NO_VALUE)
}

/**
 * The "on fire" fact of a sell or a buy: what a fire does with its order (not
 * the proposal, which the status pill speaks for): "trades at once", "waits
 * for you · over $100". An alert places no order and has no such fact.
 */
export function onFireText(action: {
  needsApproval: boolean
  approvalThresholdUsd: number | null
}): string {
  return action.needsApproval
    ? t('chat.triggerApprovalWaits', { usd: formatDcaUsd(action.approvalThresholdUsd) })
    : t('chat.triggerOnFireTrades')
}

/**
 * size · wallet balance · valid until · on fire (hook `approval`). The
 * balance is the wallet now, so a finished trigger (or an alert, which spends
 * nothing) leaves it out rather than show a figure that is not part of its
 * story; an alert places no order, so it has no "on fire" either. An odd
 * count lets the size span the row (CSS).
 */
function factsSection(trigger: Trigger): HTMLElement {
  const facts = el('section', 'trigger-card__facts')
  const terminal = isTriggerTerminal(trigger.status)
  const size = sizeText(trigger)
  const sizeCell = fact('size', t('chat.triggerFactSize'), sizeParts(trigger), size === NO_VALUE)
  if (size !== NO_VALUE) sizeCell.title = size
  facts.append(sizeCell)

  if (trigger.kind !== 'alert' && !terminal) {
    const balance = trigger.market.balance
    const balanceSymbol = trigger.kind === 'buy' ? trigger.quote.symbol : trigger.token.symbol
    facts.append(
      fact(
        'balance',
        t('chat.triggerFactBalance'),
        balance && balance.human
          ? `${formatTokenAmount(balance.human)} ${balanceSymbol}`
          : NO_VALUE,
        !balance,
      ),
    )
  }

  const valid = trigger.validUntil ? formatWhen(trigger.validUntil, false) : ''
  const validCell = fact('valid', t('chat.triggerFactValid'), valid || t('chat.triggerGtc'))
  if (trigger.validUntil) validCell.title = formatWhen(trigger.validUntil, true)
  facts.append(validCell)

  if (trigger.kind !== 'alert') {
    // The hook stays `approval`; the words say what a fire does.
    const approvalCell = fact('approval', t('chat.triggerFactOnFire'), onFireText(trigger.action))
    // Only a trigger that can still fire has anything to wait for.
    if (trigger.action.needsApproval && !terminal) approvalCell.dataset.triggerWaits = 'true'
    facts.append(approvalCell)
  }
  return facts
}

/* ── fires ── */

function explorerHost(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/**
 * One fire as a mono line. On a bracket card `leg` prefixes its number with
 * the leg word ("take-profit #1") and stamps `data-leg` on the row.
 */
function fireRow(
  fire: TriggerFire,
  trigger: Trigger,
  nowMs: number,
  leg: BracketLeg | null = null,
): HTMLElement {
  const row = el('li', 'trigger-fire')
  row.dataset.triggerFireStatus = fire.status
  if (fire.reasonCode) row.dataset.triggerReason = fire.reasonCode
  if (leg) row.dataset.leg = leg
  row.append(
    el(
      'span',
      'trigger-fire__n',
      leg ? `${bracketLegWord(leg, trigger.kind)} #${fire.n}` : `#${fire.n}`,
    ),
  )
  const ago = relativeTime(fire.at, nowMs)
  const time = el('time', 'trigger-fire__ago', ago || NO_VALUE)
  if (ago) {
    time.setAttribute('datetime', fire.at)
    time.dataset.triggerAt = fire.at
    time.title = formatWhen(fire.at, true)
  }
  row.append(time, el('span', 'trigger-fire__status', fireStatusLabel(fire.status)))
  const detail = fireDetail(fire, trigger)
  if (detail) row.append(el('span', 'trigger-fire__detail', detail))
  if (fire.manual) row.append(el('span', 'trigger-fire__manual', t('chat.triggerFireManual')))
  if (fire.explorerUrl) {
    const link = el('a', 'trigger-fire__link', '↗') as HTMLAnchorElement
    link.href = fire.explorerUrl
    link.target = '_blank'
    link.rel = 'noopener noreferrer'
    const host = explorerHost(fire.explorerUrl)
    link.title = t('chat.triggerExplorerTitle', { explorer: host })
    link.setAttribute('aria-label', t('chat.triggerExplorerTitle', { explorer: host }))
    row.append(link)
  }
  return row
}

function firesSection(trigger: Trigger, nowMs: number): HTMLElement | null {
  if (trigger.fires.length === 0) return null
  const section = el('section', 'trigger-fires')
  section.append(el('h4', 'trigger-fires__title', t('chat.triggerFiresTitle')))
  const listNode = el('ol', 'trigger-fires__list')
  trigger.fires
    .slice(0, TRIGGER_RECENT_FIRES)
    .forEach((fire) => listNode.append(fireRow(fire, trigger, nowMs)))
  section.append(listNode)
  return section
}

/* ── footer ── */

function copyButton(id: string, ctx: TriggerRenderContext): HTMLElement {
  const button = el('button', 'trigger-card__action trigger-card__copy') as HTMLButtonElement
  button.type = 'button'
  button.dataset.triggerFoot = 'copy'
  button.title = t('chat.triggerCopyTitle', { id })
  button.setAttribute('aria-label', t('chat.triggerCopyTitle', { id }))
  const glyph = hidden(el('span', 'trigger-card__action-glyph', '⧉'))
  const label = el('span', 'trigger-card__action-label', t('chat.triggerCopyId'))
  button.append(glyph, label)
  let generation = 0
  const settle = (ok: boolean): void => {
    const mine = ++generation
    button.dataset.triggerCopied = ok ? 'true' : 'failed'
    glyph.textContent = ok ? '✓' : '✕'
    label.textContent = ok ? t('chat.triggerCopied') : t('chat.triggerCopyFailed')
    ctx.setTimer(() => {
      if (mine !== generation) return
      delete button.dataset.triggerCopied
      glyph.textContent = '⧉'
      label.textContent = t('chat.triggerCopyId')
    }, TRIGGER_COPIED_MS)
  }
  button.addEventListener('click', () => {
    void Promise.resolve()
      .then(() => ctx.copyText(id))
      .then(
        () => settle(true),
        () => settle(false),
      )
  })
  return button
}

function refreshButton(): HTMLElement {
  const button = el('button', 'trigger-card__action trigger-card__refresh') as HTMLButtonElement
  button.type = 'button'
  button.dataset.triggerFoot = 'refresh'
  button.title = t('chat.triggerRefreshTitle')
  button.setAttribute('aria-label', t('chat.triggerRefreshTitle'))
  button.append(
    hidden(el('span', 'trigger-card__action-glyph', '↻')),
    el('span', 'trigger-card__action-label', t('chat.triggerRefresh')),
  )
  return button
}

/**
 * Why a card's controls are off: its live read is in flight (`checking`) or
 * failed (`failed`). Stamped as `data-trigger-stale` on the `.trigger-card`.
 */
export type TriggerStale = 'checking' | 'failed'

/** "state may be stale · ↻" — shown while a card's live read has failed. */
function staleHint(): HTMLElement {
  const hint = el('p', 'trigger-card__stale')
  hint.setAttribute('role', 'status')
  hint.title = t('chat.triggerStaleTitle')
  const button = el('button', 'trigger-card__stale-refresh', '↻') as HTMLButtonElement
  button.type = 'button'
  button.dataset.triggerFoot = 'refresh'
  button.title = t('chat.triggerRefreshTitle')
  button.setAttribute('aria-label', t('chat.triggerRefreshTitle'))
  hint.append(
    el('span', 'trigger-card__stale-text', t('chat.triggerStale')),
    hidden(el('span', 'trigger-sep', ' · ')),
    button,
  )
  return hint
}

function walletText(wallet: LpWallet | null): string {
  if (!wallet) return ''
  const short = shortAddress(wallet.address)
  return wallet.label ? `${wallet.label} (${short})` : short
}

function footer(
  payload: TriggerPayload,
  parts: string[],
  copyId: string,
  ctx: TriggerRenderContext,
): HTMLElement {
  const foot = el('footer', 'trigger-card__foot')
  const meta = el('span', 'trigger-card__foot-meta')
  meta.textContent = parts.filter(Boolean).join(' · ')
  const ago = relativeTime(payload.fetchedAt, ctx.now())
  if (ago) {
    if (meta.textContent) meta.append(el('span', 'trigger-sep', ' · '))
    meta.append(el('span', 'trigger-card__as-of', `${t('chat.triggerAsOf')} `))
    const time = el('time', 'trigger-card__ago', ago)
    time.setAttribute('datetime', payload.fetchedAt)
    time.dataset.triggerFetchedAt = payload.fetchedAt
    time.title = payload.fetchedAt
    meta.append(time)
  }
  foot.append(meta)
  const actions = el('span', 'trigger-card__actions')
  if (ctx.canRefresh && payload.request) actions.append(refreshButton())
  if (copyId) actions.append(copyButton(copyId, ctx))
  if (actions.childElementCount) foot.append(actions)
  return foot
}

function warningsNode(warnings: string[]): HTMLElement | null {
  if (warnings.length === 0) return null
  const listNode = el('ul', 'trigger-card__warnings')
  warnings.forEach((w) => listNode.append(el('li', 'trigger-card__warning', w)))
  return listNode
}

function shell(kind: TriggerKind): HTMLElement {
  const card = el('article', 'trigger-card')
  card.dataset.triggerKind = kind
  // The unmeasured default; the mounter re-stamps it from the card's width.
  card.dataset.triggerLayout = 'narrow'
  return card
}

/* ── kind = trigger ── */

function buildOne(payload: TriggerOnePayload, ctx: TriggerRenderContext): HTMLElement {
  const { trigger } = payload
  const nowMs = ctx.now()
  const card = shell('trigger')
  card.dataset.triggerId = trigger.id
  card.dataset.triggerAction = trigger.kind
  card.dataset.triggerStatus = trigger.status
  card.dataset.triggerDirection = trigger.condition.direction
  if (trigger.chain?.key) card.dataset.triggerChain = trigger.chain.key

  const head = el('header', 'trigger-card__head')
  const title = el('div', 'trigger-card__title')
  // The kind glyph (▼ sell, ▲ buy, bell for alert) is drawn by CSS from
  // `data-trigger-action`; the DOM carries no emoji.
  title.append(hidden(el('span', 'trigger-card__glyph')))
  const name = el('span', 'trigger-card__name', trigger.name)
  name.title = trigger.name
  title.append(name)
  if (trigger.chain) title.append(el('span', 'trigger-chain', trigger.chain.name))
  head.append(title, statusPill(trigger.status))
  // A bracket's leg says whose leg it is; its controls live on the bracket.
  if (trigger.bracket) {
    const group = el(
      'p',
      'trigger-card__group',
      t('chat.bracketLegOfGroup', {
        leg: bracketLegWord(trigger.bracket.leg, trigger.kind),
        name: trigger.bracket.name,
        id: trigger.bracket.id,
      }),
    )
    group.dataset.leg = trigger.bracket.leg
    head.append(group)
  }
  card.append(head)

  const hero = el('section', 'trigger-card__hero')
  const line = el('p', 'trigger-card__hero-line', heroText(trigger))
  if (trigger.action.label) line.title = trigger.action.label
  hero.append(line, nowNode(trigger, nowMs))
  const from = fromNode(trigger)
  if (from) hero.append(from)
  if (trigger.statusReason && trigger.status !== 'done') {
    hero.append(el('p', 'trigger-card__reason', statusReasonText(trigger.statusReason)))
  }
  card.append(hero)

  const gauge = gaugeNode(trigger)
  if (gauge) card.append(gauge)
  card.append(factsSection(trigger))
  const fires = firesSection(trigger, nowMs)
  if (fires) card.append(fires)
  if (ctx.canWrite) {
    const actions = actionsRow(trigger, false)
    if (actions) card.append(actions)
  }
  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  card.append(footer(payload, [trigger.id, walletText(trigger.wallet)], trigger.id, ctx))
  return card
}

/* ── kind = triggers ── */

/** "sell 50 % ETH · under $3,800" for a list row. */
export function rowPlanText(trigger: Trigger): string {
  const { action, token } = trigger
  const symbol = token.symbol
  let what: string
  if (trigger.kind === 'sell') {
    what =
      action.amountPct !== null
        ? t('chat.triggerRowSellPct', { pct: pctNumber(action.amountPct), token: symbol })
        : action.amount
          ? t('chat.triggerRowSellAmount', {
              amount: formatTokenAmount(action.amount.human),
              token: symbol,
            })
          : t('chat.triggerRowSellUsd', { usd: formatDcaUsd(action.amountUsd), token: symbol })
  } else if (trigger.kind === 'buy') {
    what = t('chat.triggerRowBuy', { usd: formatDcaUsd(action.amountUsd), token: symbol })
  } else {
    what = t('chat.triggerRowAlert', { token: symbol })
  }
  return `${what} · ${conditionLabel(trigger)}`
}

/** "ETH $3,790 · −0.3 %" for a list row; the state word stands in when there is no price. */
export function rowNowText(trigger: Trigger): string {
  const now = trigger.market.priceUsd
  const parts: string[] = []
  if (now !== null) parts.push(`${trigger.token.symbol} ${formatTriggerPrice(now)}`)
  if (trigger.status === 'armed' || trigger.status === 'paused') {
    const { hits, confirmTicks } = trigger.condition
    if (trigger.status === 'armed' && hits > 0 && hits < confirmTicks) {
      parts.push(tPlural('chat.triggerConfirming', confirmTicks - hits))
    } else {
      parts.push(formatDistance(distancePctOf(trigger)))
    }
  } else if (trigger.status === 'triggered') {
    parts.push(t('chat.triggerNowTriggered'))
  }
  return parts.filter(Boolean).join(' · ')
}

function triggerRow(trigger: Trigger, ctx: TriggerRenderContext): HTMLElement {
  const row = el('li', 'trigger-row')
  row.dataset.triggerId = trigger.id
  row.dataset.triggerStatus = trigger.status
  row.dataset.triggerAction = trigger.kind
  if (trigger.chain?.key) row.dataset.triggerChain = trigger.chain.key
  const proximity = proximityOf(distancePctOf(trigger))
  if (proximity && trigger.status === 'armed') row.dataset.triggerProximity = proximity
  const dot = hidden(el('span', 'trigger-row__dot'))
  const main = el('div', 'trigger-row__main')
  const top = el('div', 'trigger-row__top')
  const name = el('span', 'trigger-row__name', trigger.name)
  name.title = trigger.name
  top.append(name, el('span', 'trigger-row__state', triggerStatusLabel(trigger.status)))
  main.append(top, el('span', 'trigger-row__plan', rowPlanText(trigger)))
  const now = rowNowText(trigger)
  if (now) main.append(el('span', 'trigger-row__now', now))
  row.append(dot, main)
  if (ctx.canWrite) {
    const actions = actionsRow(trigger, true)
    if (actions) row.append(actions)
  }
  return row
}

/**
 * A list's totals recounted from its rows by status: a row swapped in by a
 * control's answer, an event or the live-state cache moves them, and the
 * header must not keep saying "2 armed". `count` (the rows the read
 * returned) stays the engine's.
 */
export function recountTotals(
  totals: TriggerListPayload['totals'],
  items: ReadonlyArray<{ status: TriggerStatus }>,
): TriggerListPayload['totals'] {
  const count = (status: TriggerStatus): number =>
    items.filter((item) => item.status === status).length
  return {
    count: totals.count,
    armed: count('armed'),
    awaiting: count('awaiting_approval'),
    triggered: count('triggered'),
  }
}

function totalsText(totals: TriggerListPayload['totals']): string {
  const parts: string[] = []
  if (totals.armed > 0) parts.push(t('chat.triggerTotalsArmed', { count: String(totals.armed) }))
  if (totals.awaiting > 0) {
    parts.push(t('chat.triggerTotalsAwaiting', { count: String(totals.awaiting) }))
  }
  if (totals.triggered > 0) {
    parts.push(t('chat.triggerTotalsTriggered', { count: String(totals.triggered) }))
  }
  return parts.join(' · ')
}

function buildList(payload: TriggerListPayload, ctx: TriggerRenderContext): HTMLElement {
  const card = shell('triggers')
  const head = el('header', 'trigger-card__head')
  const title = el('div', 'trigger-card__title')
  title.append(
    el('span', 'trigger-card__list-title', t('chat.triggerListTitle')),
    hidden(el('span', 'trigger-sep', ' · ')),
    el('span', 'trigger-card__count', String(payload.totals.count)),
  )
  head.append(title)
  card.append(head)

  if (payload.triggers.length === 0) {
    card.append(el('p', 'trigger-card__empty', t('chat.triggerEmpty')))
  } else {
    const totals = totalsText(payload.totals)
    if (totals) card.append(el('p', 'trigger-totals', totals))
    const rows = el('ul', 'trigger-rows')
    payload.triggers.forEach((tr) => rows.append(triggerRow(tr, ctx)))
    card.append(rows)
  }
  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  card.append(footer(payload, [], '', ctx))
  return card
}

/* ── kind = bracket ── */

/**
 * The range gauge: one rail (`.trigger-gauge__line`), the stop tick at the
 * left and the take-profit tick at the right (`.trigger-gauge__tick[data-leg]`),
 * the span between them (`.trigger-gauge__zone[data-zone=bracket]`), the
 * price as the dot (`.trigger-gauge__dot`); labels above (the lines) and
 * under (the price). The hooks are exact: the desktop skins them blind.
 */
function bracketGaugeNode(bracket: Bracket): HTMLElement | null {
  const model = bracketGauge(bracket)
  if (!model) return null
  const { lines } = bracket
  // A range alert's lines are its floor and its ceiling, not a stop and a take-profit.
  const alert = bracket.kind === 'alert'
  const gauge = el('div', 'trigger-gauge')
  gauge.dataset.triggerGauge = 'range'
  if (!isTriggerTerminal(bracket.status)) {
    if (model.distancePct !== null) {
      gauge.dataset.triggerDist = String(Math.round(model.distancePct * 100) / 100)
    }
    if (model.proximity) gauge.dataset.triggerProximity = model.proximity
  }
  gauge.setAttribute('role', 'img')
  gauge.setAttribute(
    'aria-label',
    t(alert ? 'chat.bracketGaugeLabelAlert' : 'chat.bracketGaugeLabel', {
      now: formatTriggerPrice(bracket.market.priceUsd),
      sl: formatTriggerPrice(lines.stopLossUsd),
      tp: formatTriggerPrice(lines.takeProfitUsd),
    }),
  )

  const chart = svg('svg', {
    class: 'trigger-gauge__svg',
    'aria-hidden': 'true',
    focusable: 'false',
  })
  chart.append(
    svg('rect', {
      class: 'trigger-gauge__zone',
      'data-zone': 'bracket',
      x: pct(model.zone[0]),
      width: pct(Math.max(0, model.zone[1] - model.zone[0])),
      y: '30%',
      height: '40%',
    }),
    svg('line', { class: 'trigger-gauge__line', x1: '0%', x2: '100%', y1: '50%', y2: '50%' }),
  )
  for (const leg of ['sl', 'tp'] as const) {
    const x = leg === 'sl' ? model.sl : model.tp
    chart.append(
      svg('line', {
        class: 'trigger-gauge__tick',
        'data-leg': leg,
        x1: pct(x),
        x2: pct(x),
        y1: '10%',
        y2: '90%',
      }),
    )
  }
  if (model.current !== null) {
    const dot = svg('circle', {
      class: 'trigger-gauge__dot',
      cx: pct(model.current),
      cy: '50%',
      r: 5,
    })
    if (model.clamped) dot.setAttribute('data-clamped', model.clamped)
    chart.append(dot)
  }
  gauge.append(chart)

  const label = (leg: BracketLeg | 'now', x: number, content: string): HTMLElement => {
    const node = el('span', 'trigger-gauge__label', content)
    node.dataset.leg = leg
    node.dataset.align = labelAlign(x)
    node.style.left = pct(x)
    return node
  }
  const top = hidden(el('div', 'trigger-gauge__labels'))
  top.dataset.row = 'top'
  top.append(
    label(
      'sl',
      model.sl,
      t(alert ? 'chat.bracketGaugeFloor' : 'chat.triggerGaugeStop', {
        price: formatTriggerPrice(lines.stopLossUsd),
      }),
    ),
    label(
      'tp',
      model.tp,
      t(alert ? 'chat.bracketGaugeCeiling' : 'chat.bracketGaugeTp', {
        price: formatTriggerPrice(lines.takeProfitUsd),
      }),
    ),
  )
  gauge.prepend(top)
  if (model.current !== null) {
    const bottom = hidden(el('div', 'trigger-gauge__labels'))
    bottom.dataset.row = 'bottom'
    bottom.append(
      label(
        'now',
        model.current,
        t('chat.triggerGaugeNow', { price: formatTriggerPrice(bracket.market.priceUsd) }),
      ),
    )
    gauge.append(bottom)
  }
  return gauge
}

/** The two legs as two rows: word, line, state. */
function bracketLegsNode(bracket: Bracket): HTMLElement {
  const legs = el('div', 'bracket-legs')
  legs.setAttribute('role', 'list')
  for (const leg of ['tp', 'sl'] as const) {
    const trigger = bracketLegOf(bracket, leg)
    const row = el('div', 'bracket-leg')
    row.setAttribute('role', 'listitem')
    row.dataset.leg = leg
    row.dataset.status = trigger?.status ?? 'unknown'
    if (trigger && isLegOnHold(trigger)) row.dataset.hold = 'true'
    if (bracket.fired === leg) row.dataset.fired = 'true'
    if (trigger) row.title = trigger.name
    row.append(
      el('span', 'bracket-leg__word', bracketLegTitle(bracket, leg)),
      el('span', 'bracket-leg__line', bracketLineLabel(bracket, leg)),
      el('span', 'bracket-leg__state', bracketLegStateText(trigger)),
    )
    legs.append(row)
  }
  return legs
}

/**
 * size · balance · reward : risk · on fire (hook `approval`), and valid
 * until when set. The balance and the reward : risk describe a bracket that
 * can still fire; an alert spends nothing, so it has neither balance nor
 * "on fire".
 */
function bracketFactsSection(bracket: Bracket): HTMLElement {
  const facts = el('section', 'trigger-card__facts')
  const terminal = isTriggerTerminal(bracket.status)
  const size = bracketSizeText(bracket)
  const sizeCell = fact(
    'size',
    t('chat.triggerFactSize'),
    bracketSizeParts(bracket),
    size === NO_VALUE,
  )
  if (size !== NO_VALUE) sizeCell.title = bracket.action.label || size
  facts.append(sizeCell)

  if (bracket.kind !== 'alert' && !terminal) {
    const balance = bracket.market.balance
    facts.append(
      fact(
        'balance',
        t('chat.triggerFactBalance'),
        balance && balance.human
          ? `${formatTokenAmount(balance.human)} ${bracket.token.symbol}`
          : NO_VALUE,
        !balance,
      ),
    )
  }

  if (!terminal) {
    const rr = bracketRewardRiskText(bracket)
    const cell = fact('rr', t('chat.bracketFactRewardRisk'), rr, rr === NO_VALUE)
    cell.querySelector('.trigger-fact__value')?.classList.add('trigger-fact__rr')
    facts.append(cell)
  }

  if (bracket.kind !== 'alert') {
    const onFire = fact('approval', t('chat.triggerFactOnFire'), onFireText(bracket.action))
    if (bracket.action.needsApproval && !terminal) onFire.dataset.triggerWaits = 'true'
    facts.append(onFire)
  }

  if (bracket.validUntil) {
    const valid = formatWhen(bracket.validUntil, false)
    if (valid) {
      const cell = fact('valid', t('chat.triggerFactValid'), valid)
      cell.title = formatWhen(bracket.validUntil, true)
      facts.append(cell)
    }
  }
  return facts
}

function bracketFiresSection(bracket: Bracket, nowMs: number): HTMLElement | null {
  const rows = bracketFires(bracket)
  if (rows.length === 0) return null
  const section = el('section', 'trigger-fires')
  section.append(el('h4', 'trigger-fires__title', t('chat.triggerFiresTitle')))
  const listNode = el('ol', 'trigger-fires__list')
  rows.forEach((row) => listNode.append(fireRow(row.fire, row.trigger, nowMs, row.leg)))
  section.append(listNode)
  return section
}

/** "lines set at +20.0 % / −10.0 % from $3,800" when a percent was given at creation. */
function bracketFromNode(bracket: Bracket): HTMLElement | null {
  const { fromPriceUsd, takeProfitUsd, stopLossUsd } = bracket.lines
  if (fromPriceUsd === null || takeProfitUsd === null || stopLossUsd === null) return null
  const move = (line: number): string =>
    formatSignedPct(((line - fromPriceUsd) / fromPriceUsd) * 100)
  return el(
    'p',
    'trigger-card__from',
    t('chat.bracketFromPrice', {
      tp: move(takeProfitUsd),
      sl: move(stopLossUsd),
      price: formatTriggerPrice(fromPriceUsd),
    }),
  )
}

function buildBracket(payload: BracketOnePayload, ctx: TriggerRenderContext): HTMLElement {
  const { bracket } = payload
  const nowMs = ctx.now()
  const card = shell('bracket')
  card.dataset.bracketId = bracket.id
  card.dataset.triggerAction = bracket.kind
  card.dataset.triggerStatus = bracket.status
  if (bracket.chain?.key) card.dataset.triggerChain = bracket.chain.key
  const nearest = bracketNearest(bracket)
  if (nearest && !isTriggerTerminal(bracket.status)) card.dataset.triggerNearest = nearest
  if (bracket.fired) card.dataset.triggerFired = bracket.fired

  const head = el('header', 'trigger-card__head')
  const title = el('div', 'trigger-card__title')
  // The glyph (▼▲ for a sell bracket, a bell for an alert) is CSS only.
  title.append(hidden(el('span', 'trigger-card__glyph')))
  const name = el('span', 'trigger-card__name', bracket.name)
  name.title = bracket.name
  title.append(name)
  if (bracket.chain) title.append(el('span', 'trigger-chain', bracket.chain.name))
  head.append(title, statusPill(bracket.status))
  card.append(head)

  const hero = el('section', 'trigger-card__hero')
  const line = el('p', 'trigger-card__hero-line', bracketHeroText(bracket))
  if (bracket.action.label) line.title = bracket.action.label
  const now = el('p', 'trigger-card__now', bracketNowText(bracket, nowMs))
  now.setAttribute('aria-live', 'off')
  hero.append(line, now)
  const from = bracketFromNode(bracket)
  if (from) hero.append(from)
  if (bracket.statusReason && bracket.status !== 'done') {
    hero.append(el('p', 'trigger-card__reason', statusReasonText(bracket.statusReason)))
  }
  card.append(hero)

  const gauge = bracketGaugeNode(bracket)
  if (gauge) card.append(gauge)
  card.append(bracketLegsNode(bracket), bracketFactsSection(bracket))
  const fires = bracketFiresSection(bracket, nowMs)
  if (fires) card.append(fires)
  if (ctx.canWrite) {
    const actions = bracketActionsRow(bracket, false)
    if (actions) card.append(actions)
  }
  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  card.append(footer(payload, [bracket.id, walletText(bracket.wallet)], bracket.id, ctx))
  return card
}

/* ── kind = brackets ── */

/** "sell 100 % WETH · $3,420 – $4,560" (alert: "alert WETH · …") for a list row. */
export function bracketRowPlanText(bracket: Bracket): string {
  const { action, token } = bracket
  const symbol = token.symbol
  let what: string
  if (bracket.kind === 'alert') {
    what = t('chat.triggerRowAlert', { token: symbol })
  } else if (action.amount) {
    what = t('chat.triggerRowSellAmount', {
      amount: formatTokenAmount(action.amount.human),
      token: symbol,
    })
  } else if (action.amountUsd !== null) {
    what = t('chat.triggerRowSellUsd', { usd: formatDcaUsd(action.amountUsd), token: symbol })
  } else {
    what = t('chat.triggerRowSellPct', { pct: pctNumber(action.amountPct ?? 100), token: symbol })
  }
  const { stopLossUsd, takeProfitUsd } = bracket.lines
  const range =
    stopLossUsd !== null && takeProfitUsd !== null
      ? t('chat.bracketRange', {
          sl: formatTriggerPrice(stopLossUsd),
          tp: formatTriggerPrice(takeProfitUsd),
        })
      : `${bracketLineLabel(bracket, 'sl')} / ${bracketLineLabel(bracket, 'tp')}`
  return `${what} · ${range}`
}

/**
 * "ETH $3,790 · +20.3 % / −9.8 %" for a list row; a leg that is over reads
 * "—" ("— / −90.0 %" after a partial take-profit); what it is doing instead
 * when firing.
 */
export function bracketRowNowText(bracket: Bracket): string {
  const now = bracket.market.priceUsd
  const parts: string[] = []
  if (now !== null) parts.push(`${bracket.token.symbol} ${formatTriggerPrice(now)}`)
  if (bracket.status === 'armed' || bracket.status === 'paused') {
    const confirming = bracket.status === 'armed' ? confirmingLeg(bracket) : null
    const leg = confirming ? bracketLegOf(bracket, confirming) : null
    if (confirming && leg) {
      parts.push(
        tPlural('chat.bracketConfirming', leg.condition.confirmTicks - leg.condition.hits, {
          leg: bracketLegWord(confirming, bracket.kind),
        }),
      )
    } else {
      const side = (leg: BracketLeg): string =>
        isBracketLegLive(bracket, leg)
          ? formatSignedPct(leg === 'tp' ? bracketUpsidePct(bracket) : bracketDownsidePct(bracket))
          : NO_VALUE
      const up = side('tp')
      const down = side('sl')
      if ((up && up !== NO_VALUE) || (down && down !== NO_VALUE)) {
        parts.push([up, down].filter(Boolean).join(' / '))
      }
    }
  } else if (bracket.status === 'triggered') {
    parts.push(bracketNowText(bracket, 0))
  }
  return parts.filter(Boolean).join(' · ')
}

function bracketRow(bracket: Bracket, ctx: TriggerRenderContext): HTMLElement {
  const row = el('li', 'trigger-row')
  row.dataset.bracketId = bracket.id
  row.dataset.triggerStatus = bracket.status
  row.dataset.triggerAction = bracket.kind
  if (bracket.chain?.key) row.dataset.triggerChain = bracket.chain.key
  const proximity = proximityOf(bracketDistancePct(bracket))
  if (proximity && bracket.status === 'armed') row.dataset.triggerProximity = proximity
  const nearest = bracketNearest(bracket)
  if (nearest && !isTriggerTerminal(bracket.status)) row.dataset.triggerNearest = nearest
  const dot = hidden(el('span', 'trigger-row__dot'))
  const main = el('div', 'trigger-row__main')
  const top = el('div', 'trigger-row__top')
  const name = el('span', 'trigger-row__name', bracket.name)
  name.title = bracket.name
  top.append(name, el('span', 'trigger-row__state', bracketStatusLabel(bracket.status)))
  main.append(top, el('span', 'trigger-row__plan', bracketRowPlanText(bracket)))
  const now = bracketRowNowText(bracket)
  if (now) main.append(el('span', 'trigger-row__now', now))
  row.append(dot, main)
  if (ctx.canWrite) {
    const actions = bracketActionsRow(bracket, true)
    if (actions) row.append(actions)
  }
  return row
}

function buildBracketList(payload: BracketListPayload, ctx: TriggerRenderContext): HTMLElement {
  const card = shell('brackets')
  const head = el('header', 'trigger-card__head')
  const title = el('div', 'trigger-card__title')
  title.append(
    el('span', 'trigger-card__list-title', t('chat.bracketListTitle')),
    hidden(el('span', 'trigger-sep', ' · ')),
    el('span', 'trigger-card__count', String(payload.totals.count)),
  )
  head.append(title)
  card.append(head)

  if (payload.brackets.length === 0) {
    card.append(el('p', 'trigger-card__empty', t('chat.bracketEmpty')))
  } else {
    const totals = totalsText(payload.totals)
    if (totals) card.append(el('p', 'trigger-totals', totals))
    const rows = el('ul', 'trigger-rows')
    payload.brackets.forEach((b) => rows.append(bracketRow(b, ctx)))
    card.append(rows)
  }
  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  card.append(footer(payload, [], '', ctx))
  return card
}

/**
 * Build the card for a normalized payload. Exported for the unit tests, which
 * assert on the built DOM without standing up a mounter or a fetch.
 */
export function buildTriggerCard(payload: TriggerPayload, ctx: TriggerRenderContext): HTMLElement {
  switch (payload.kind) {
    case 'trigger':
      return buildOne(payload, ctx)
    case 'triggers':
      return buildList(payload, ctx)
    case 'bracket':
      return buildBracket(payload, ctx)
    case 'brackets':
      return buildBracketList(payload, ctx)
  }
}

/** Replace `host`'s card slot with the payload's card. */
export function renderTrigger(
  host: HTMLElement,
  payload: TriggerPayload,
  ctx: TriggerRenderContext,
): void {
  const slot = host.querySelector<HTMLElement>('.msg-artifact-trigger__body')
  if (!slot) return
  slot.replaceChildren(buildTriggerCard(payload, ctx))
  host.dataset.triggerHost = 'rendered'
}

/** Stamp `data-trigger-layout` from the card's measured width; an unmeasured card keeps its default. */
export function layoutTriggerCard(card: HTMLElement): void {
  const width = card.offsetWidth
  if (!(width > 0)) return
  const layout = triggerLayoutFor(width)
  if (card.dataset.triggerLayout !== layout) card.dataset.triggerLayout = layout
}

/* ── Mounter ────────────────────────────────────────────────────────────── */

/** A gateway RPC call: `rpc.call(method, params)`. */
export type TriggerCall = (method: string, params: Record<string, unknown>) => Promise<unknown>

/**
 * The write capability the desktop desk hands the mounter for its operator
 * connection; the same shape as `DcaActions`. Absent (the web) → no controls.
 */
export interface TriggerActions {
  /** Calls `trading.trigger.<action>` / `trading.bracket.<action>` on the operator connection. */
  call: TriggerCall
  /** A "Fire now" placed an order (the desk focuses it in the Book). */
  onOrder?: (orderId: string) => void
}

/** "23 h 53 m" with non-breaking spaces: a countdown never leaves its unit alone on the next line. */
export function unbreakable(text: string): string {
  return text.replace(/ /g, '\u00a0')
}

/**
 * The read RPC a request re-runs: `trading.bracket.<kind>` for a bracket
 * card's request (or a `get` by `bracketId`), else `trading.trigger.<kind>`.
 */
export function triggerReadMethod(request: TriggerRequest): string {
  const bracket =
    request.scope === 'bracket' ||
    (request.scope === undefined && typeof request.params.bracketId === 'string')
  return `trading.${bracket ? 'bracket' : 'trigger'}.${request.kind}`
}

function rpcError(error: unknown): { code: string; message: string } {
  const e = error as { code?: unknown; message?: unknown } | null
  return {
    code: typeof e?.code === 'string' ? e.code : '',
    message: typeof e?.message === 'string' ? e.message : String(error),
  }
}

/** What a failed control says in `.trigger-actions__error`. */
export function triggerErrorText(error: unknown): string {
  const { code, message } = rpcError(error)
  if (code === 'trading.operator_required') return t('chat.triggerErrOperator')
  return message || code || t('chat.triggerErrUnknown')
}

/**
 * A `trading.trigger.changed` body as a trigger payload: the full payload, the
 * payload under `trigger`, or a bare trigger under `trigger`. Null otherwise.
 */
export function triggerChangedPayload(raw: unknown): {
  trigger: Trigger
  payload: TriggerOnePayload | null
} | null {
  const body = obj(raw)
  if (!body) return null
  const direct = body.kind === 'trigger' ? normalizeTriggerPayload(body) : null
  if (direct?.kind === 'trigger') return { trigger: direct.trigger, payload: direct }
  const inner = obj(body.trigger)
  if (!inner) return null
  if (inner.kind === 'trigger') {
    const nested = normalizeTriggerPayload(inner)
    return nested?.kind === 'trigger' ? { trigger: nested.trigger, payload: nested } : null
  }
  const trigger = normalizeTrigger(inner)
  return trigger ? { trigger, payload: null } : null
}

/**
 * A `trading.bracket.changed` body as a bracket payload: the full payload, the
 * payload under `bracket`, or a bare bracket under `bracket`. Null otherwise.
 */
export function bracketChangedPayload(raw: unknown): {
  bracket: Bracket
  payload: BracketOnePayload | null
} | null {
  const body = obj(raw)
  if (!body) return null
  const direct = body.kind === 'bracket' ? normalizeTriggerPayload(body) : null
  if (direct?.kind === 'bracket') return { bracket: direct.bracket, payload: direct }
  const inner = obj(body.bracket)
  if (!inner) return null
  if (inner.kind === 'bracket') {
    const nested = normalizeTriggerPayload(inner)
    return nested?.kind === 'bracket' ? { bracket: nested.bracket, payload: nested } : null
  }
  const bracket = normalizeBracket(inner)
  return bracket ? { bracket, payload: null } : null
}

export interface TriggerMounterDeps {
  /** Fetch a trigger artifact body from its (authenticated) URL. */
  fetchPayload: (url: string) => Promise<unknown>
  /**
   * The gateway call ↻ and the settle refresh re-run a card's read through
   * (`trading.trigger.get` / `list` are agent-callable, so the web passes it
   * too). Default: none, no ↻ — unless `actions` is given, whose call then serves.
   */
  call?: TriggerCall
  /**
   * The trigger controls. An object, or a getter read at render and click
   * time (the desktop's chat becomes the desk without remounting). Absent → no
   * `.trigger-actions` at all.
   */
  actions?: TriggerActions | (() => TriggerActions | null | undefined) | null
  /** Copy the trigger id. Default: Clipboard API, then execCommand. */
  copyText?: (value: string) => void | Promise<void>
  /** Clock. Default: Date.now. */
  now?: () => number
  /** The diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/**
 * Create the trigger card mounter bound to the transcript's fetch surface.
 *
 * `mountTrigger(root)` is idempotent: it only picks up placeholders it has not
 * claimed yet. It owns one clock — every second while a mounted card says
 * "checked N s ago" in seconds or a proposal expires within the hour, less
 * often otherwise — that re-derives the live lines and the "as of" stamps,
 * plus the short copy / confirm resets; `destroyAll` (alias `dispose`) clears
 * them all. One ResizeObserver keeps `data-trigger-layout` current.
 */
export function createTriggerMounter(deps: TriggerMounterDeps) {
  const diag = deps.diag ?? ((): void => {})
  const now = deps.now ?? ((): number => Date.now())
  const copyText = deps.copyText ?? copyLpText
  const getActions = (): TriggerActions | null => {
    const value = typeof deps.actions === 'function' ? deps.actions() : deps.actions
    return value && typeof value.call === 'function' ? value : null
  }
  const readCall = (): TriggerCall | null => deps.call ?? getActions()?.call ?? null
  const claimed = new Set<HTMLElement>()
  const rendered = new Set<HTMLElement>()
  const payloads = new Map<HTMLElement, TriggerPayload>()
  const refreshing = new Set<HTMLElement>()
  const wired = new WeakSet<HTMLElement>()
  /** Controls in flight, by trigger id; they outlive a re-render. */
  const busy = new Map<string, TriggerAction>()
  /** The last failed control's message, by trigger id, until the next click. */
  const errors = new Map<string, string>()
  /** Orders a "Fire now" placed, by order id → trigger id (refresh on settle). */
  const orders = new Map<string, string>()
  /**
   * Hosts whose card may not show the trigger as it is now: the artifact
   * snapshot while its live read is in flight (`checking`), or after that
   * read failed (`failed`). Their controls stay off (`data-trigger-stale`).
   */
  const stale = new Map<HTMLElement, TriggerStale>()
  /**
   * The newest live state of every trigger this mounter has seen (reads,
   * control answers, `trading.trigger.changed`), by id. A transcript
   * re-render re-mounts a card from its artifact file — the snapshot taken
   * when the trigger was created — so a re-mounted card draws this instead.
   * It holds no DOM and outlives `destroyAll`.
   */
  const latestByTriggerId = new Map<string, TriggerOnePayload>()
  /** The same for brackets (`trading.bracket.changed`), by bracket id. */
  const latestByBracketId = new Map<string, BracketOnePayload>()
  const timers = new Set<ReturnType<typeof setTimeout>>()
  let clock: ReturnType<typeof setTimeout> | null = null
  const cards = new Map<HTMLElement, HTMLElement>()
  const dirty = new Set<HTMLElement>()
  let pending = false
  let cancelFrame: (() => void) | null = null
  const observer =
    typeof ResizeObserver === 'function'
      ? new ResizeObserver((entries) => {
          entries.forEach((entry) => dirty.add(entry.target as HTMLElement))
          scheduleLayout()
        })
      : null

  function scheduleLayout(): void {
    if (pending) return
    pending = true
    const run = (): void => {
      pending = false
      cancelFrame = null
      const batch = [...dirty]
      dirty.clear()
      batch.forEach((card) => {
        if (card.isConnected) layoutTriggerCard(card)
      })
    }
    if (typeof requestAnimationFrame === 'function') {
      const id = requestAnimationFrame(run)
      if (pending) cancelFrame = () => cancelAnimationFrame(id)
    } else {
      const id = setTimeout(run, 16)
      if (pending) cancelFrame = () => clearTimeout(id)
    }
  }

  function watch(host: HTMLElement): void {
    const card = host.querySelector<HTMLElement>('.trigger-card')
    if (!card) return
    const previous = cards.get(host)
    if (previous && previous !== card) observer?.unobserve(previous)
    cards.set(host, card)
    layoutTriggerCard(card)
    observer?.observe(card)
  }

  function unwatch(host: HTMLElement): void {
    const card = cards.get(host)
    if (!card) return
    observer?.unobserve(card)
    dirty.delete(card)
    cards.delete(host)
  }

  const ctx: TriggerRenderContext = {
    now,
    copyText,
    setTimer(fn, ms) {
      const id = setTimeout(() => {
        timers.delete(id)
        fn()
      }, ms)
      timers.add(id)
    },
    get canRefresh() {
      return readCall() !== null
    },
    get canWrite() {
      return getActions() !== null
    },
  }

  /** The triggers a card draws (a bracket card draws none of its own). */
  function triggersOf(payload: TriggerPayload): Trigger[] {
    if (payload.kind === 'trigger') return [payload.trigger]
    if (payload.kind === 'triggers') return payload.triggers
    return []
  }

  /** The brackets a card draws. */
  function bracketsOf(payload: TriggerPayload): Bracket[] {
    if (payload.kind === 'bracket') return [payload.bracket]
    if (payload.kind === 'brackets') return payload.brackets
    return []
  }

  /** Every trigger and bracket id a card draws: what its controls are keyed by. */
  function idsOf(payload: TriggerPayload): string[] {
    return [...triggersOf(payload).map((tr) => tr.id), ...bracketsOf(payload).map((b) => b.id)]
  }

  /* ── the live-state cache ── */

  function stampOf(item: { updatedAt: string }): number {
    const at = Date.parse(item.updatedAt)
    return Number.isNaN(at) ? -Infinity : at
  }

  /** A trigger payload for a trigger that arrived without its envelope. */
  function envelopeFor(trigger: Trigger, fetchedAt: string): TriggerOnePayload {
    return {
      version: 1,
      fetchedAt,
      warnings: [],
      request: { kind: 'get', params: { triggerId: trigger.id } },
      kind: 'trigger',
      trigger,
      fire: null,
    }
  }

  /** A bracket payload for a bracket that arrived without its envelope. */
  function bracketEnvelopeFor(bracket: Bracket, fetchedAt: string): BracketOnePayload {
    return {
      version: 1,
      fetchedAt,
      warnings: [],
      request: { kind: 'get', params: { bracketId: bracket.id }, scope: 'bracket' },
      kind: 'bracket',
      bracket,
      fire: null,
    }
  }

  /** Keep `fresh` unless the cache already holds a strictly newer state. */
  function rememberTrigger(fresh: TriggerOnePayload): void {
    const known = latestByTriggerId.get(fresh.trigger.id)
    if (known && stampOf(known.trigger) > stampOf(fresh.trigger)) return
    latestByTriggerId.set(fresh.trigger.id, { ...fresh, fire: null })
  }

  function rememberBracket(fresh: BracketOnePayload): void {
    const known = latestByBracketId.get(fresh.bracket.id)
    if (known && stampOf(known.bracket) > stampOf(fresh.bracket)) return
    latestByBracketId.set(fresh.bracket.id, { ...fresh, fire: null })
  }

  /** Feed a live payload into the cache. */
  function remember(payload: TriggerPayload): void {
    const at = payload.fetchedAt || new Date(now()).toISOString()
    switch (payload.kind) {
      case 'trigger':
        rememberTrigger(payload)
        return
      case 'triggers':
        payload.triggers.forEach((tr) => rememberTrigger(envelopeFor(tr, at)))
        return
      case 'bracket':
        rememberBracket(payload)
        return
      case 'brackets':
        payload.brackets.forEach((b) => rememberBracket(bracketEnvelopeFor(b, at)))
    }
  }

  /**
   * A snapshot with every trigger (bracket) the cache knows at least as fresh
   * swapped in. `live` is true when nothing of the snapshot's own state is
   * left (a card with a cache hit, a list whose every row hit).
   */
  function withCache(snapshot: TriggerPayload): { payload: TriggerPayload; live: boolean } {
    const hit = (tr: Trigger): TriggerOnePayload | null => {
      const known = latestByTriggerId.get(tr.id)
      return known && stampOf(known.trigger) >= stampOf(tr) ? known : null
    }
    const hitBracket = (b: Bracket): BracketOnePayload | null => {
      const known = latestByBracketId.get(b.id)
      return known && stampOf(known.bracket) >= stampOf(b) ? known : null
    }
    switch (snapshot.kind) {
      case 'trigger': {
        const known = hit(snapshot.trigger)
        if (!known) return { payload: snapshot, live: false }
        return {
          payload: { ...known, request: snapshot.request ?? known.request, fire: null },
          live: true,
        }
      }
      case 'bracket': {
        const known = hitBracket(snapshot.bracket)
        if (!known) return { payload: snapshot, live: false }
        return {
          payload: { ...known, request: snapshot.request ?? known.request, fire: null },
          live: true,
        }
      }
      case 'triggers': {
        let live = snapshot.triggers.length > 0
        let swapped = false
        const triggers = snapshot.triggers.map((tr) => {
          const known = hit(tr)
          if (!known) live = false
          else swapped = true
          return known ? known.trigger : tr
        })
        const totals = swapped ? recountTotals(snapshot.totals, triggers) : snapshot.totals
        return { payload: { ...snapshot, triggers, totals }, live }
      }
      case 'brackets': {
        let live = snapshot.brackets.length > 0
        let swapped = false
        const brackets = snapshot.brackets.map((b) => {
          const known = hitBracket(b)
          if (!known) live = false
          else swapped = true
          return known ? known.bracket : b
        })
        const totals = swapped ? recountTotals(snapshot.totals, brackets) : snapshot.totals
        return { payload: { ...snapshot, brackets, totals }, live }
      }
    }
  }

  /* ── the clock ── */

  function stopClock(): void {
    if (clock !== null) clearTimeout(clock)
    clock = null
  }

  function paintTimes(host: HTMLElement, payload: TriggerPayload, at: number): void {
    if (payload.kind === 'trigger' || payload.kind === 'bracket') {
      const node = host.querySelector<HTMLElement>('.trigger-card__now')
      const next =
        payload.kind === 'trigger'
          ? nowText(payload.trigger, at)
          : bracketNowText(payload.bracket, at)
      if (node && node.textContent !== next) node.textContent = next
    }
    host
      .querySelectorAll<HTMLElement>('[data-trigger-fetched-at], [data-trigger-at]')
      .forEach((node) => {
        const stamp = node.dataset.triggerFetchedAt ?? node.dataset.triggerAt ?? ''
        const next = relativeTime(stamp, at)
        if (next && node.textContent !== next) node.textContent = next
      })
  }

  function nextDelay(at: number): number {
    let delay = DCA_MINUTE_MS
    rendered.forEach((host) => {
      const payload = payloads.get(host)
      // A list row's line does not tick; only a trigger or bracket card's live line does.
      if (payload?.kind === 'trigger') {
        delay = Math.min(delay, triggerTickDelay(payload.trigger, at))
      } else if (payload?.kind === 'bracket') {
        delay = Math.min(delay, bracketTickDelay(payload.bracket, at))
      }
    })
    return delay
  }

  function scheduleClock(): void {
    stopClock()
    if (rendered.size === 0) return
    clock = setTimeout(tick, nextDelay(now()))
  }

  function tick(): void {
    clock = null
    pruneDetached()
    if (rendered.size === 0) return
    const at = now()
    rendered.forEach((host) => {
      const payload = payloads.get(host)
      if (payload) paintTimes(host, payload, at)
    })
    scheduleClock()
  }

  /* ── controls ── */

  function paintControls(host: HTMLElement): void {
    const card = host.querySelector<HTMLElement>('.trigger-card')
    const staleState = stale.get(host)
    let anyBusy = false
    host.querySelectorAll<HTMLElement>('.trigger-actions').forEach((row) => {
      const id = row.dataset.triggerId ?? row.dataset.bracketId ?? ''
      const action = busy.get(id)
      if (action) {
        anyBusy = true
        row.dataset.triggerBusy = action
        row.setAttribute('aria-busy', 'true')
      } else {
        delete row.dataset.triggerBusy
        row.removeAttribute('aria-busy')
      }
      row.querySelectorAll<HTMLButtonElement>('button[data-trigger-op]').forEach((b) => {
        b.disabled = Boolean(action) || staleState !== undefined
        if (action && b.dataset.triggerOp === action) b.dataset.triggerPending = 'true'
        else delete b.dataset.triggerPending
      })
      const line = row.querySelector<HTMLElement>('.trigger-actions__error')
      if (line) {
        const message = action ? '' : (errors.get(id) ?? '')
        line.textContent = message
        line.hidden = message === ''
      }
    })
    if (card) {
      const single =
        card.dataset.triggerKind === 'trigger' || card.dataset.triggerKind === 'bracket'
      if (anyBusy && single) card.dataset.triggerBusy = 'true'
      else delete card.dataset.triggerBusy
      paintStale(card, staleState)
    }
  }

  /** `data-trigger-stale` on the card, and the "may be stale · ↻" hint once the live read failed. */
  function paintStale(card: HTMLElement, state: TriggerStale | undefined): void {
    if (state) card.dataset.triggerStale = state
    else delete card.dataset.triggerStale
    const hint = card.querySelector<HTMLElement>(':scope > .trigger-card__stale')
    if (state !== 'failed') {
      hint?.remove()
      return
    }
    if (hint) return
    const foot = card.querySelector<HTMLElement>(':scope > .trigger-card__foot')
    card.insertBefore(staleHint(), foot)
  }

  function paintAll(): void {
    rendered.forEach(paintControls)
  }

  /** Render (or re-render) a payload into its host and keep it fitted. */
  function show(host: HTMLElement, payload: TriggerPayload): void {
    renderTrigger(host, payload, ctx)
    payloads.set(host, payload)
    paintControls(host)
    watch(host)
  }

  /**
   * Put a fresh trigger on every card that draws it: a trigger card of the
   * same id swaps to `fresh` (keeping its own ↻ request when `fresh` has
   * none), a list card replaces that row.
   */
  function applyTrigger(trigger: Trigger, fresh: TriggerOnePayload | null): void {
    const stamp = new Date(now()).toISOString()
    remember(fresh ?? envelopeFor(trigger, stamp))
    rendered.forEach((host) => {
      const shown = payloads.get(host)
      if (!shown || !host.isConnected) return
      if (shown.kind === 'bracket' || shown.kind === 'brackets') return
      if (shown.kind === 'trigger') {
        if (shown.trigger.id !== trigger.id) return
        // A live state: whatever snapshot the card held is gone.
        stale.delete(host)
        const next: TriggerOnePayload = fresh
          ? {
              ...fresh,
              request: fresh.request ?? shown.request,
              fetchedAt: fresh.fetchedAt || stamp,
            }
          : { ...shown, trigger, fire: null, fetchedAt: stamp }
        show(host, next)
      } else if (shown.triggers.some((tr) => tr.id === trigger.id)) {
        const triggers = shown.triggers.map((tr) => (tr.id === trigger.id ? trigger : tr))
        show(host, { ...shown, triggers, totals: recountTotals(shown.totals, triggers) })
      }
    })
    scheduleClock()
  }

  /**
   * The bracket twin of `applyTrigger`: a bracket card of the same id swaps to
   * `fresh` (or the bare bracket), a brackets list replaces that row.
   */
  function applyBracket(bracket: Bracket, fresh: BracketOnePayload | null): void {
    const stamp = new Date(now()).toISOString()
    remember(fresh ?? bracketEnvelopeFor(bracket, stamp))
    rendered.forEach((host) => {
      const shown = payloads.get(host)
      if (!shown || !host.isConnected) return
      if (shown.kind === 'bracket') {
        if (shown.bracket.id !== bracket.id) return
        stale.delete(host)
        const next: BracketOnePayload = fresh
          ? {
              ...fresh,
              request: fresh.request ?? shown.request,
              fetchedAt: fresh.fetchedAt || stamp,
            }
          : { ...shown, bracket, fire: null, fetchedAt: stamp }
        show(host, next)
      } else if (shown.kind === 'brackets' && shown.brackets.some((b) => b.id === bracket.id)) {
        const brackets = shown.brackets.map((b) => (b.id === bracket.id ? bracket : b))
        show(host, { ...shown, brackets, totals: recountTotals(shown.totals, brackets) })
      }
    })
    scheduleClock()
  }

  /** First click on Stop / Fire now: say "click again" for a few seconds. */
  function armConfirm(button: HTMLButtonElement): void {
    const label = button.textContent ?? ''
    const title = button.title
    button.dataset.triggerConfirm = 'true'
    button.textContent = t('chat.triggerConfirm', { label })
    button.title =
      button.dataset.triggerOp === 'fire'
        ? t('chat.triggerFireConfirmTitle')
        : button.dataset.bracketId
          ? t('chat.bracketStopConfirmTitle')
          : t('chat.triggerStopConfirmTitle')
    ctx.setTimer(() => {
      if (!button.isConnected || button.dataset.triggerConfirm !== 'true') return
      delete button.dataset.triggerConfirm
      button.textContent = label
      button.title = title
    }, TRIGGER_CONFIRM_MS)
  }

  async function act(button: HTMLButtonElement): Promise<void> {
    const actions = getActions()
    // A bracket's controls carry `data-bracket-id` and call `trading.bracket.<op>`.
    const scope = button.dataset.bracketId ? 'bracket' : 'trigger'
    const id = (scope === 'bracket' ? button.dataset.bracketId : button.dataset.triggerId) ?? ''
    const action = button.dataset.triggerOp as TriggerAction
    if (!actions || !id || !ACTIONS.has(action) || busy.has(id)) return
    if (CONFIRMED.has(action) && button.dataset.triggerConfirm !== 'true') {
      armConfirm(button)
      return
    }
    busy.set(id, action)
    errors.delete(id)
    paintAll()
    diag('trigger.action.start', { action, scope, id })
    try {
      const raw = await actions.call(
        `trading.${scope}.${action}`,
        scope === 'bracket' ? { bracketId: id } : { triggerId: id },
      )
      const next = normalizeTriggerPayload(raw)
      if (!next || next.kind !== (scope === 'bracket' ? 'bracket' : 'trigger')) {
        throw new Error(t('chat.triggerUnreadable'))
      }
      busy.delete(id)
      let status: TriggerStatus = 'unknown'
      if (next.kind === 'trigger') {
        applyTrigger(next.trigger, next)
        status = next.trigger.status
      } else if (next.kind === 'bracket') {
        applyBracket(next.bracket, next)
        status = next.bracket.status
      }
      paintAll()
      const fire = next.kind === 'trigger' || next.kind === 'bracket' ? next.fire : null
      const orderId = action === 'fire' ? fire?.orderId : null
      if (orderId) {
        orders.set(orderId, id)
        actions.onOrder?.(orderId)
      }
      diag('trigger.action.done', { action, scope, id, status })
    } catch (error) {
      busy.delete(id)
      errors.set(id, triggerErrorText(error))
      paintAll()
      diag('trigger.action.error', { action, scope, error: String(error) })
      // "cannot approve …: it is done": the card is out of date. Re-read
      // every card that draws this trigger (bracket), controls off until it lands.
      const { code } = rpcError(error)
      if (code === `trading.${scope}.bad_state` || code === `trading.${scope}.not_found`) {
        rendered.forEach((host) => {
          const payload = payloads.get(host)
          if (!payload || !host.isConnected) return
          if (!idsOf(payload).includes(id)) return
          if (!readCall() || !requestFor(payload)) return
          stale.set(host, 'checking')
          paintControls(host)
          void refreshHost(host)
        })
      }
    }
  }

  /* ── ↻ refresh ── */

  function flashFootError(host: HTMLElement, message: string): void {
    const foot = host.querySelector<HTMLElement>('.trigger-card__foot')
    if (!foot) return
    let line = foot.querySelector<HTMLElement>('.trigger-card__refresh-error')
    if (!line) {
      line = el('span', 'trigger-card__refresh-error')
      line.setAttribute('role', 'alert')
      foot.append(line)
    }
    line.textContent = message
    const shown = line
    ctx.setTimer(() => shown.remove(), TRIGGER_ERROR_MS)
  }

  /** The read that re-draws `payload`: its echoed request, else `get` by id. */
  function requestFor(payload: TriggerPayload): TriggerRequest | null {
    if (payload.request) return payload.request
    if (payload.kind === 'trigger') {
      return { kind: 'get', params: { triggerId: payload.trigger.id } }
    }
    if (payload.kind === 'bracket') {
      return { kind: 'get', params: { bracketId: payload.bracket.id }, scope: 'bracket' }
    }
    return null
  }

  /**
   * Re-run a card's read and redraw it. `quiet` (the read a mount fires on
   * its own) neither dims the card nor flashes a failure under the footer: a
   * stale card says so with its hint instead. `call` overrides the read call.
   */
  async function refreshHost(
    host: HTMLElement,
    opts: { quiet?: boolean; call?: TriggerCall | null } = {},
  ): Promise<void> {
    const current = payloads.get(host)
    const call = opts.call ?? readCall()
    const request = current ? requestFor(current) : null
    if (!current || !request || !call || refreshing.has(host)) return
    refreshing.add(host)
    const card = host.querySelector<HTMLElement>('.trigger-card')
    const buttons = [...host.querySelectorAll<HTMLButtonElement>('[data-trigger-foot="refresh"]')]
    if (!opts.quiet) {
      card?.setAttribute('data-trigger-refreshing', 'true')
      card?.setAttribute('aria-busy', 'true')
      buttons.forEach((b) => (b.disabled = true))
    }
    diag('trigger.refresh.start', { kind: request.kind, quiet: Boolean(opts.quiet) })
    try {
      const raw = await call(triggerReadMethod(request), request.params)
      const next = normalizeTriggerPayload(raw)
      if (!next) throw new Error(t('chat.triggerUnreadable'))
      if (!next.request) next.request = current.request
      if (!next.fetchedAt) next.fetchedAt = new Date(now()).toISOString()
      remember(next)
      if (!host.isConnected || !rendered.has(host)) return
      stale.delete(host)
      show(host, next)
      scheduleClock()
      diag('trigger.refresh.done', { kind: next.kind })
    } catch (error) {
      if (!opts.quiet) {
        card?.removeAttribute('data-trigger-refreshing')
        card?.removeAttribute('aria-busy')
        buttons.forEach((b) => (b.disabled = false))
        flashFootError(host, t('chat.triggerRefreshFailed', { message: rpcError(error).message }))
      }
      if (stale.has(host) && rendered.has(host)) {
        stale.set(host, 'failed')
        paintControls(host)
      }
      diag('trigger.refresh.error', { error: String(error) })
    } finally {
      refreshing.delete(host)
    }
  }

  /** One click handler per host: the controls, ↻. (Copy wires itself.) */
  function wire(host: HTMLElement): void {
    if (wired.has(host)) return
    wired.add(host)
    host.addEventListener('click', (event) => {
      const target = (event.target as Element | null)?.closest<HTMLElement>(
        '[data-trigger-op], [data-trigger-foot]',
      )
      if (!target || !host.contains(target) || (target as HTMLButtonElement).disabled) return
      if (target.dataset.triggerFoot === 'refresh') {
        void refreshHost(host)
        return
      }
      if (target.dataset.triggerOp) void act(target as HTMLButtonElement)
    })
  }

  /* ── events ── */

  /**
   * `trading.order.finished`: re-read every card that shows that order (a
   * parked sell settling, a "Fire now" landing). `triggerId` is the order's
   * own `triggerId` when the event carries it.
   */
  function orderFinished(orderId: string, triggerId?: string | null): void {
    if (!orderId) return
    const placedBy = orders.get(orderId)
    orders.delete(orderId)
    rendered.forEach((host) => {
      const payload = payloads.get(host)
      if (!payload || !host.isConnected) return
      const owns = (tr: Trigger): boolean =>
        tr.id === placedBy ||
        (Boolean(triggerId) && tr.id === triggerId) ||
        tr.result?.orderId === orderId ||
        tr.fires.some((f) => f.orderId === orderId)
      // A bracket's order is a leg's: the order names the leg (`triggerId`),
      // mapped to its bracket through the legs the card or the cache knows.
      const ownsBracket = (b: Bracket): boolean => {
        if (b.id === placedBy || b.result?.orderId === orderId) return true
        const known = latestByBracketId.get(b.id)?.bracket
        return [b.takeProfit, b.stopLoss, known?.takeProfit, known?.stopLoss].some(
          (leg) => leg !== null && leg !== undefined && owns(leg),
        )
      }
      if (triggersOf(payload).some(owns) || bracketsOf(payload).some(ownsBracket)) {
        void refreshHost(host)
      }
    })
  }

  /** `trading.trigger.changed`: swap the trigger into every card that draws it. */
  function triggerChanged(raw: unknown): void {
    const changed = triggerChangedPayload(raw)
    if (!changed) return
    diag('trigger.changed', { triggerId: changed.trigger.id, status: changed.trigger.status })
    applyTrigger(changed.trigger, changed.payload)
  }

  /** `trading.bracket.changed`: swap the bracket into every card that draws it. */
  function bracketChanged(raw: unknown): void {
    const changed = bracketChangedPayload(raw)
    if (!changed) return
    diag('bracket.changed', { bracketId: changed.bracket.id, status: changed.bracket.status })
    applyBracket(changed.bracket, changed.payload)
  }

  /* ── mount ── */

  function pruneDetached(): void {
    for (const host of [...claimed]) {
      if (!host.isConnected) {
        claimed.delete(host)
        rendered.delete(host)
        payloads.delete(host)
        stale.delete(host)
        unwatch(host)
      }
    }
  }

  function setStatus(host: HTMLElement, message: string): void {
    const status = host.querySelector<HTMLElement>('.msg-artifact-trigger__status')
    if (!status) return
    status.textContent = message
    status.hidden = message === ''
  }

  async function mountOne(host: HTMLElement): Promise<void> {
    const url = host.dataset.triggerSrc || ''
    if (!url) {
      setStatus(host, t('chat.triggerUnavailable'))
      return
    }
    try {
      diag('trigger.mount.start', { url })
      const raw = await deps.fetchPayload(url)
      const payload = normalizeTriggerPayload(raw)
      if (!payload) {
        setStatus(host, t('chat.triggerUnreadable'))
        diag('trigger.mount.empty', { url })
        return
      }
      if (!host.isConnected || !claimed.has(host)) {
        claimed.delete(host)
        return
      }
      // The artifact is a snapshot from when it was published: draw the
      // newest state this mounter knows instead, and re-read the live one.
      // Until that read lands, a card still holding snapshot state keeps its
      // controls off — an "Approve & arm" on a done trigger must not be
      // clickable.
      const { payload: shown, live } = withCache(payload)
      const call = deps.call ?? null
      const reread = call !== null && requestFor(shown) !== null
      if (reread && !live) stale.set(host, 'checking')
      wire(host)
      rendered.add(host)
      show(host, shown)
      setStatus(host, '')
      scheduleClock()
      diag('trigger.mount.done', { url, kind: payload.kind, cached: live })
      if (reread) void refreshHost(host, { quiet: true, call })
    } catch (error) {
      setStatus(host, t('chat.triggerFailed'))
      diag('trigger.mount.error', { url, error: String(error) })
    }
  }

  /** Mount every not-yet-mounted trigger placeholder inside `root`. */
  function mountTrigger(root: HTMLElement | null | undefined): void {
    if (!root) return
    pruneDetached()
    root.querySelectorAll<HTMLElement>('[data-trigger-src]').forEach((host) => {
      if (claimed.has(host)) return
      claimed.add(host)
      void mountOne(host)
    })
  }

  /** Forget every host and clear every timer (route unmount). */
  function destroyAll(): void {
    claimed.clear()
    rendered.clear()
    payloads.clear()
    refreshing.clear()
    stale.clear()
    busy.clear()
    errors.clear()
    orders.clear()
    stopClock()
    observer?.disconnect()
    cards.clear()
    dirty.clear()
    cancelFrame?.()
    cancelFrame = null
    pending = false
    timers.forEach((id) => clearTimeout(id))
    timers.clear()
  }

  return {
    mountTrigger,
    destroyAll,
    dispose: destroyAll,
    pruneDetached,
    orderFinished,
    triggerChanged,
    bracketChanged,
    refresh: refreshHost,
  }
}

export type TriggerMounter = ReturnType<typeof createTriggerMounter>
