// Chat transcript — DCA mandate cards.
//
// `agentos trade dca … --json` publishes a JSON artifact with the
// `application/vnd.agentos.dca+json` mime; the artifact renderer emits a mount
// placeholder for it and this module fetches the payload and draws one of two
// layouts into it, keyed by the payload's `kind`: `mandate` (one recurring buy,
// read like an instrument) and `mandates` (the list). docs/dca.md is the
// contract.
//
// Same two surfaces as lp.ts:
//   1. Pure helpers (top-level exports) — mime match, payload normalization,
//      schedule / countdown / progress text, chart geometry. No network.
//   2. `createDcaMounter(deps)` — the imperative mounter the transcript
//      composes next to the LP mounter.
//
// Styling: the markup carries `dca-*` classes plus `data-dca-kind`,
// `data-dca-status`, `data-dca-chain`, `data-dca-layout` hooks. Only the desktop
// mounts this markup and skins the hooks in its own chat.css; the web console
// renders its own React cards (web/features/chat/cards/) from the pure helpers.
//
// SECURITY: every payload-derived string reaches the DOM through `textContent`
// or an attribute setter, never `innerHTML` — token symbols are attacker-chosen
// on a permissionless chain. Explorer links are built from an http(s) explorer
// origin and a hex hash, or accepted only as an http(s) URL.

import { t, tPlural } from '@/i18n'
import '@/i18n/en/chat'

import type { Artifact } from './artifacts'
import {
  NO_VALUE,
  copyLpText,
  explorerUrl,
  formatSmall,
  formatTokenAmount,
  formatUsd,
  relativeTime,
  safeExplorerBase,
  shortAddress,
  type LpAmount,
  type LpChain,
  type LpToken,
  type LpWallet,
} from './lp'

/** The mime the engine publishes a DCA mandate read-out under. */
export const DCA_ARTIFACT_MIME = 'application/vnd.agentos.dca+json'

/** The countdown's cadence under an hour to go. */
export const DCA_SECOND_MS = 1_000

/** The countdown's cadence above an hour, and the "as of 2m ago" refresh. */
export const DCA_MINUTE_MS = 60_000

/** How long the copy button reads "copied". */
export const DCA_COPIED_MS = 1_500

/** How long Stop waits for its confirming second click. */
export const DCA_CONFIRM_MS = 4_000

/** How long a failed ↻ says why under the footer. */
export const DCA_ERROR_MS = 4_000

/** Card width (border box, px) from which the stats sit 4 across. */
export const DCA_WIDE_MIN_PX = 520

/** At most this many attempts are drawn in the buys chart. */
export const DCA_CHART_MAX = 50

/** The chart's price axis reaches at least this far (a fraction of the average) past the data. */
export const DCA_CHART_MIN_PAD = 0.005

/** Rows in "Recent buys" / "Recent runs". */
export const DCA_RECENT_RUNS = 5

const HOUR_MS = 3_600_000

/* ── Payload shape (docs/dca.md) ────────────────────────────────────────── */

export type DcaKind = 'mandate' | 'mandates'
export type DcaStatus =
  | 'awaiting_approval'
  | 'active'
  | 'paused'
  | 'completed'
  | 'stopped'
  | 'rejected'
  | 'expired'
  | 'unknown'
export type DcaRunStatus =
  'filled' | 'pending' | 'parked' | 'skipped' | 'failed' | 'expired' | 'rejected' | 'unknown'
export type DcaAction = 'approve' | 'reject' | 'pause' | 'resume' | 'run' | 'stop'

export interface DcaRun {
  n: number
  at: string
  manual: boolean
  status: DcaRunStatus
  /** Human-readable detail ("ETH at $3,120 above $3,000"). */
  reason: string | null
  /** Machine code: max_price | daily_cap | insufficient_balance | cap_reached | trading.<code>. */
  reasonCode: string | null
  usd: number | null
  amount: LpAmount | null
  priceUsd: number | null
  orderId: string | null
  txHash: string | null
  /** A safe http(s) link to the transaction, or ''. */
  explorerUrl: string
  gasUsd: number | null
}

export interface DcaSchedule {
  everySeconds: number
  label: string
  startNow: boolean
  anchorAt: string | null
  nextRunAt: string | null
  lastRunAt: string | null
}

export interface DcaBudget {
  usdPerRun: number
  capUsd: number
  spentUsd: number
  reservedUsd: number
  remainingUsd: number
  /** spent / cap, 0–1. */
  progress: number
}

export interface DcaMandate {
  id: string
  name: string
  status: DcaStatus
  statusReason: string | null
  chain: LpChain | null
  wallet: LpWallet | null
  token: LpToken
  quote: LpToken
  schedule: DcaSchedule
  budget: DcaBudget
  runs: { done: number; max: number | null; skipped: number; failed: number; attempts: number }
  guards: {
    maxPriceUsd: number | null
    approvalThresholdUsd: number | null
    dailyCapUsd: number | null
    slippagePct: number | null
    buysNeedApproval: boolean
  }
  acquired: {
    amount: LpAmount
    avgPriceUsd: number | null
    currentPriceUsd: number | null
    vsAvgPct: number | null
    unrealizedUsd: number | null
    gasUsd: number | null
  }
  /** Newest first, ≤ 50. */
  history: DcaRun[]
  initiator: string
  sessionKey: string | null
  createdAt: string
  updatedAt: string
  approvedAt: string | null
  expiresAt: string | null
}

/** What ↻ re-runs: `trading.dca.<kind>` with `params` verbatim. */
export interface DcaRequest {
  kind: 'get' | 'list'
  params: Record<string, unknown>
}

interface DcaEnvelope {
  version: number
  fetchedAt: string
  warnings: string[]
  request: DcaRequest | null
}

export interface DcaMandatePayload extends DcaEnvelope {
  kind: 'mandate'
  mandate: DcaMandate
  /** Only in the answer of `trading.dca.run`. */
  run: DcaRun | null
}

export interface DcaMandatesPayload extends DcaEnvelope {
  kind: 'mandates'
  mandates: DcaMandate[]
  totals: {
    count: number
    active: number
    spentUsd: number | null
    capUsd: number | null
    acquiredUsd: number | null
  }
}

export type DcaPayload = DcaMandatePayload | DcaMandatesPayload

/* ── Pure helpers: mime + normalization ─────────────────────────────────── */

/** True when the artifact should render as a DCA card. */
export function isDcaArtifact(artifact: Artifact | null | undefined): boolean {
  if (!artifact || !artifact.mime) return false
  return String(artifact.mime).toLowerCase().split(';')[0]?.trim() === DCA_ARTIFACT_MIME
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
  return { raw: text(row.raw), human: text(row.human), usd: num(row.usd) }
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

const STATUSES = new Set<DcaStatus>([
  'awaiting_approval',
  'active',
  'paused',
  'completed',
  'stopped',
  'rejected',
  'expired',
])

function normStatus(value: unknown): DcaStatus {
  const raw = text(value).toLowerCase() as DcaStatus
  return STATUSES.has(raw) ? raw : 'unknown'
}

const RUN_STATUSES = new Set<DcaRunStatus>([
  'filled',
  'pending',
  'parked',
  'skipped',
  'failed',
  'expired',
  'rejected',
])

function normRun(value: unknown, chain: LpChain | null): DcaRun | null {
  const row = obj(value)
  if (!row) return null
  const status = text(row.status).toLowerCase() as DcaRunStatus
  const txHash = textOrNull(row.txHash)
  return {
    n: Math.trunc(num(row.n) ?? 0),
    at: text(row.at),
    manual: bool(row.manual),
    status: RUN_STATUSES.has(status) ? status : 'unknown',
    reason: textOrNull(row.reason),
    reasonCode: textOrNull(row.reasonCode),
    usd: num(row.usd),
    amount: normAmount(row.amount),
    priceUsd: num(row.priceUsd),
    orderId: textOrNull(row.orderId),
    txHash,
    // Built from the chain's explorer when possible; the payload's link only
    // when it is plain http(s) — never a `javascript:` smuggled in a payload.
    explorerUrl: (txHash ? explorerUrl(chain, 'tx', txHash) : '') || safeHttpUrl(row.explorerUrl),
    gasUsd: num(row.gasUsd),
  }
}

/** Normalize one mandate, or null when it has no id or no token to name. */
export function normalizeDcaMandate(value: unknown): DcaMandate | null {
  const row = obj(value)
  if (!row) return null
  const id = text(row.id)
  const token = normToken(row.token)
  if (!id || !token) return null
  const quote = normToken(row.quote) ?? { address: '', symbol: '?', decimals: 0, priceUsd: null }
  const chain = normChain(row.chain)
  const schedule = obj(row.schedule) ?? {}
  const budget = obj(row.budget) ?? {}
  const runs = obj(row.runs) ?? {}
  const guards = obj(row.guards) ?? {}
  const acquired = obj(row.acquired) ?? {}

  const usdPerRun = num(budget.usdPerRun) ?? 0
  const spentUsd = Math.max(0, num(budget.spentUsd) ?? 0)
  const reservedUsd = Math.max(0, num(budget.reservedUsd) ?? 0)
  const runsMax = num(runs.max)
  const capUsd = num(budget.capUsd) ?? (runsMax !== null && usdPerRun > 0 ? usdPerRun * runsMax : 0)
  const progress = num(budget.progress) ?? (capUsd > 0 ? spentUsd / capUsd : 0)
  const history = list(row.history, (item) => normRun(item, chain))
  const done = num(runs.done) ?? history.filter((r) => r.status === 'filled').length

  return {
    id,
    name: text(row.name) || `DCA ${token.symbol}`,
    status: normStatus(row.status),
    statusReason: textOrNull(row.statusReason),
    chain,
    wallet: normWallet(row.wallet),
    token,
    quote,
    schedule: {
      everySeconds: Math.max(0, num(schedule.everySeconds) ?? 0),
      label: text(schedule.label),
      startNow: schedule.startNow === undefined ? true : bool(schedule.startNow),
      anchorAt: textOrNull(schedule.anchorAt),
      nextRunAt: textOrNull(schedule.nextRunAt),
      lastRunAt: textOrNull(schedule.lastRunAt),
    },
    budget: {
      usdPerRun,
      capUsd,
      spentUsd,
      reservedUsd,
      remainingUsd: num(budget.remainingUsd) ?? Math.max(0, capUsd - spentUsd - reservedUsd),
      progress: clamp01(progress),
    },
    runs: {
      done: Math.max(0, Math.trunc(done)),
      max: runsMax !== null && runsMax > 0 ? Math.trunc(runsMax) : null,
      skipped: Math.max(0, Math.trunc(num(runs.skipped) ?? 0)),
      failed: Math.max(0, Math.trunc(num(runs.failed) ?? 0)),
      attempts: Math.max(0, Math.trunc(num(runs.attempts) ?? history.length)),
    },
    guards: {
      maxPriceUsd: num(guards.maxPriceUsd),
      approvalThresholdUsd: num(guards.approvalThresholdUsd),
      dailyCapUsd: num(guards.dailyCapUsd),
      slippagePct: num(guards.slippagePct),
      buysNeedApproval: bool(guards.buysNeedApproval),
    },
    acquired: {
      amount: normAmount(acquired.amount) ?? { raw: '', human: '', usd: null },
      avgPriceUsd: num(acquired.avgPriceUsd),
      currentPriceUsd: num(acquired.currentPriceUsd) ?? token.priceUsd,
      vsAvgPct: num(acquired.vsAvgPct),
      unrealizedUsd: num(acquired.unrealizedUsd),
      gasUsd: num(acquired.gasUsd),
    },
    history,
    initiator: text(row.initiator),
    sessionKey: textOrNull(row.sessionKey),
    createdAt: text(row.createdAt),
    updatedAt: text(row.updatedAt),
    approvedAt: textOrNull(row.approvedAt),
    expiresAt: textOrNull(row.expiresAt),
  }
}

/** The read to re-run, or null. Only `get` and `list` pass: the method is built from it. */
export function normalizeDcaRequest(value: unknown): DcaRequest | null {
  const row = obj(value)
  if (!row) return null
  const kind = text(row.kind)
  if (kind !== 'get' && kind !== 'list') return null
  return { kind, params: { ...(obj(row.params) ?? {}) } }
}

/**
 * Validate and normalize an artifact body into a renderable payload, or return
 * null when there is nothing to draw (unknown kind, no mandate).
 */
export function normalizeDcaPayload(raw: unknown): DcaPayload | null {
  const body = obj(raw)
  if (!body) return null
  const kind = text(body.kind)
  const envelope: DcaEnvelope = {
    version: num(body.version) ?? 1,
    fetchedAt: text(body.fetchedAt),
    warnings: list(body.warnings, (w) => text(w) || null),
    request: normalizeDcaRequest(body.request),
  }
  if (kind === 'mandate') {
    const mandate = normalizeDcaMandate(body.mandate)
    if (!mandate) return null
    return { ...envelope, kind, mandate, run: normRun(body.run, mandate.chain) }
  }
  if (kind === 'mandates') {
    if (!Array.isArray(body.mandates)) return null
    const mandates = list(body.mandates, normalizeDcaMandate)
    const totals = obj(body.totals) ?? {}
    return {
      ...envelope,
      kind,
      mandates,
      totals: {
        count: num(totals.count) ?? mandates.length,
        active: num(totals.active) ?? mandates.filter((m) => m.status === 'active').length,
        spentUsd: num(totals.spentUsd),
        capUsd: num(totals.capUsd),
        acquiredUsd: num(totals.acquiredUsd),
      },
    }
  }
  return null
}

/* ── Pure helpers: formatting ───────────────────────────────────────────── */

function grouped(value: number, min: number, max: number): string {
  return value.toLocaleString('en-US', { minimumFractionDigits: min, maximumFractionDigits: max })
}

/** USD for a budget figure: whole dollars without cents ($300), else cents ($120.40). */
export function formatDcaUsd(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return NO_VALUE
  const abs = Math.abs(value)
  if (abs === 0) return '$0'
  if (abs < 10_000 && Number.isInteger(Math.round(abs * 100) / 100) && abs >= 1) {
    return `${value < 0 ? '-' : ''}$${grouped(Math.round(abs), 0, 0)}`
  }
  return formatUsd(value)
}

/** A USD price: $2,860 from 1,000 up, $1.24 above 1, subscript zeros below. */
export function formatDcaPrice(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return NO_VALUE
  const abs = Math.abs(value)
  if (abs >= 1_000_000) return formatUsd(value)
  if (abs >= 1000) return `$${grouped(Math.round(abs), 0, 0)}`
  if (abs >= 1) return `$${grouped(abs, 2, 2)}`
  return `$${formatSmall(abs)}`
}

/** A signed USD delta: +$10.80, −$4.20, $0.00. */
export function formatSignedUsd(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return NO_VALUE
  const body = formatUsd(Math.abs(value))
  if (value > 0) return `+${body}`
  if (value < 0) return `−${body}`
  return body
}

/** A percent with one decimal and a direction arrow: ▲ 4.2 %, ▼ 1.1 %. */
export function formatVsPct(pct: number): string {
  const arrow = pct > 0 ? '▲' : pct < 0 ? '▼' : '='
  return `${arrow} ${Math.abs(pct).toFixed(1)} %`
}

export type DcaTone = 'up' | 'down' | 'flat'

function toneOf(value: number | null): DcaTone | null {
  if (value === null || !Number.isFinite(value)) return null
  return value > 0 ? 'up' : value < 0 ? 'down' : 'flat'
}

/** "every day", "every 6 hours", "every 30 minutes" from a number of seconds. */
export function everyLabel(seconds: number): string {
  const s = Math.round(seconds)
  if (!(s > 0)) return ''
  const week = 7 * 86_400
  if (s % week === 0)
    return s === week
      ? t('chat.dcaEveryWeek')
      : t('chat.dcaEveryWeeks', { count: String(s / week) })
  if (s % 86_400 === 0) {
    return s === 86_400
      ? t('chat.dcaEveryDay')
      : t('chat.dcaEveryDays', { count: String(s / 86_400) })
  }
  if (s % 3600 === 0) {
    return s === 3600
      ? t('chat.dcaEveryHour')
      : t('chat.dcaEveryHours', { count: String(s / 3600) })
  }
  if (s % 60 === 0) {
    return s === 60
      ? t('chat.dcaEveryMinute')
      : t('chat.dcaEveryMinutes', { count: String(s / 60) })
  }
  return t('chat.dcaEverySeconds', { count: String(s) })
}

/** The schedule in words: the engine's label, else one derived from the interval. */
export function scheduleLabel(schedule: DcaSchedule): string {
  return schedule.label || everyLabel(schedule.everySeconds)
}

/**
 * "3 h 12 m", "12 m 5 s", "42 s", "1 d 4 h" for a positive span. `trim` drops a
 * zero second part for a whole span: "1 h", "2 m", "1 d" (not "1 h 0 m").
 */
export function formatCountdown(ms: number, trim = false): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const days = Math.floor(total / 86_400)
  const hours = Math.floor((total % 86_400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (days > 0) {
    if (trim && hours === 0) return t('chat.dcaDurationDaysOnly', { days: String(days) })
    return t('chat.dcaDurationDays', { days: String(days), hours: String(hours) })
  }
  if (hours > 0) {
    if (trim && minutes === 0) return t('chat.dcaDurationHoursOnly', { hours: String(hours) })
    return t('chat.dcaDurationHours', { hours: String(hours), minutes: String(minutes) })
  }
  if (minutes > 0) {
    if (trim && seconds === 0) return t('chat.dcaDurationMinutesOnly', { minutes: String(minutes) })
    return t('chat.dcaDurationMinutes', { minutes: String(minutes), seconds: String(seconds) })
  }
  return t('chat.dcaDurationSeconds', { seconds: String(seconds) })
}

function msUntil(iso: string | null, nowMs: number): number | null {
  if (!iso) return null
  const at = Date.parse(iso)
  return Number.isNaN(at) ? null : at - nowMs
}

/**
 * The live line under the hero: when the next buy happens, or why none will.
 * Re-derived by the mounter's clock, so it must stay pure in `(mandate, now)`.
 */
export function nextBuyText(mandate: DcaMandate, nowMs: number): string {
  switch (mandate.status) {
    case 'awaiting_approval': {
      const every = mandate.schedule.everySeconds
      const first = mandate.schedule.startNow
        ? t('chat.dcaFirstOnApproval')
        : every > 0
          ? t('chat.dcaFirstAfter', { time: formatCountdown(every * 1000, true) })
          : t('chat.dcaFirstAfterApproval')
      const left = msUntil(mandate.expiresAt, nowMs)
      if (left === null) return first
      if (left <= 0) return `${first} · ${t('chat.dcaProposalLapsing')}`
      return `${first} · ${t('chat.dcaExpiresIn', { time: formatCountdown(left) })}`
    }
    case 'active': {
      const left = msUntil(mandate.schedule.nextRunAt, nowMs)
      if (left === null) return t('chat.dcaNextUnknown')
      if (left <= 0) return t('chat.dcaNextDue')
      return t('chat.dcaNextIn', { time: formatCountdown(left) })
    }
    case 'paused':
      return t('chat.dcaStatePaused')
    case 'completed':
      return t('chat.dcaStateCompleted')
    case 'stopped':
      return t('chat.dcaStateStopped')
    case 'rejected':
      return t('chat.dcaStateRejected')
    case 'expired':
      return t('chat.dcaStateExpired')
    default:
      return ''
  }
}

/**
 * The engine's `statusReason` for the hero: "user" (the owner acted) reads
 * "by you", "user: <note>" reads "by you: <note>"; anything else as given.
 */
export function statusReasonText(reason: string): string {
  if (reason === 'user') return t('chat.dcaReasonByYou')
  const note = /^user:\s*(.*)$/s.exec(reason)
  if (note) return t('chat.dcaReasonByYouNote', { note: note[1]!.trim() })
  return reason
}

/** Milliseconds the countdown line of `mandate` has left, or null when it does not tick. */
export function countdownMs(mandate: DcaMandate, nowMs: number): number | null {
  if (mandate.status === 'active') return msUntil(mandate.schedule.nextRunAt, nowMs)
  if (mandate.status === 'awaiting_approval') return msUntil(mandate.expiresAt, nowMs)
  return null
}

/**
 * When the clock should next wake for a countdown `msLeft` away: every second
 * under an hour, every minute above (waking early enough to catch the switch
 * into seconds). Nothing to count → the once-a-minute stamp refresh.
 */
export function dcaTickDelay(msLeft: number | null): number {
  if (msLeft === null || msLeft <= 0) return DCA_MINUTE_MS
  if (msLeft <= HOUR_MS) return DCA_SECOND_MS
  return Math.max(DCA_SECOND_MS, Math.min(DCA_MINUTE_MS, msLeft - HOUR_MS))
}

export interface DcaProgress {
  /** Bar widths in % of the track; spent + reserved never exceeds 100. */
  spentPct: number
  reservedPct: number
  /** "$120.40 of $300 · 40 %". */
  amountText: string
  /** "12 of 30 buys" or "12 buys". */
  runsText: string
}

/** The progress bar's geometry and labels. */
export function dcaProgress(mandate: DcaMandate): DcaProgress {
  const { budget, runs } = mandate
  const cap = budget.capUsd
  const spentPct = cap > 0 ? clamp01(budget.spentUsd / cap) * 100 : budget.progress * 100
  const reservedPct = cap > 0 ? Math.min(100 - spentPct, (budget.reservedUsd / cap) * 100) : 0
  const pct = Math.round((cap > 0 ? budget.spentUsd / cap : budget.progress) * 100)
  return {
    spentPct,
    reservedPct: Math.max(0, reservedPct),
    amountText: t('chat.dcaProgressAmount', {
      spent: formatDcaUsd(budget.spentUsd),
      cap: formatDcaUsd(cap),
      pct: String(Math.min(100, Math.max(0, pct))),
    }),
    runsText:
      runs.max !== null
        ? t('chat.dcaBuysOfMax', { done: String(runs.done), max: String(runs.max) })
        : tPlural('chat.dcaBuys', runs.done),
  }
}

/** The controls a mandate in `status` offers, primary first. Terminal → none. */
export function dcaActionsFor(status: DcaStatus): DcaAction[] {
  switch (status) {
    case 'awaiting_approval':
      return ['approve', 'reject']
    case 'active':
      return ['pause', 'run', 'stop']
    case 'paused':
      return ['resume', 'run', 'stop']
    default:
      return []
  }
}

const ACTIONS = new Set<DcaAction>(['approve', 'reject', 'pause', 'resume', 'run', 'stop'])

function actionLabel(action: DcaAction): string {
  switch (action) {
    case 'approve':
      return t('chat.dcaApprove')
    case 'reject':
      return t('chat.dcaReject')
    case 'pause':
      return t('chat.dcaPause')
    case 'resume':
      return t('chat.dcaResume')
    case 'run':
      return t('chat.dcaRun')
    case 'stop':
      return t('chat.dcaStop')
  }
}

function actionTitle(action: DcaAction): string {
  switch (action) {
    case 'approve':
      return t('chat.dcaApproveTitle')
    case 'reject':
      return t('chat.dcaRejectTitle')
    case 'pause':
      return t('chat.dcaPauseTitle')
    case 'resume':
      return t('chat.dcaResumeTitle')
    case 'run':
      return t('chat.dcaRunTitle')
    case 'stop':
      return t('chat.dcaStopTitle')
  }
}

export function statusLabel(status: DcaStatus): string {
  switch (status) {
    case 'awaiting_approval':
      return t('chat.dcaStatusAwaiting')
    case 'active':
      return t('chat.dcaStatusActive')
    case 'paused':
      return t('chat.dcaStatusPaused')
    case 'completed':
      return t('chat.dcaStatusCompleted')
    case 'stopped':
      return t('chat.dcaStatusStopped')
    case 'rejected':
      return t('chat.dcaStatusRejected')
    case 'expired':
      return t('chat.dcaStatusExpired')
    default:
      return t('chat.dcaStatusUnknown')
  }
}

function runStatusLabel(status: DcaRunStatus): string {
  switch (status) {
    case 'filled':
      return t('chat.dcaRunFilled')
    case 'pending':
      return t('chat.dcaRunPending')
    case 'parked':
      return t('chat.dcaRunParked')
    case 'skipped':
      return t('chat.dcaRunSkipped')
    case 'failed':
      return t('chat.dcaRunFailed')
    case 'expired':
      return t('chat.dcaRunExpired')
    case 'rejected':
      return t('chat.dcaRunRejected')
    default:
      return t('chat.dcaRunUnknown')
  }
}

/** `ord_7f3a91…`: an order id short enough for a row; the title carries it whole. */
function shortOrder(orderId: string): string {
  return orderId.length > 12 ? `${orderId.slice(0, 10)}…` : orderId
}

/** "$10 → 0.0035 ETH @ $2,860" for a filled run. */
function fillText(run: DcaRun, token: LpToken): string {
  return t('chat.dcaFill', {
    usd: formatDcaUsd(run.usd),
    amount: run.amount ? formatTokenAmount(run.amount.human) : NO_VALUE,
    symbol: token.symbol,
    price: formatDcaPrice(run.priceUsd),
  })
}

/** Words for a reason code, when the engine sent no human reason. */
function reasonCodeText(code: string | null): string {
  switch (code) {
    case 'max_price':
      return t('chat.dcaReasonMaxPrice')
    case 'daily_cap':
      return t('chat.dcaReasonDailyCap')
    case 'insufficient_balance':
      return t('chat.dcaReasonBalance')
    case 'cap_reached':
      return t('chat.dcaReasonCapReached')
    default:
      return code ?? ''
  }
}

/** What a run did, in one phrase (the part after its status word). */
export function runDetail(run: DcaRun, token: LpToken): string {
  switch (run.status) {
    case 'filled':
      return fillText(run, token)
    case 'pending':
      return run.usd !== null ? formatDcaUsd(run.usd) : ''
    case 'parked':
      return run.orderId ? `#${shortOrder(run.orderId)}` : ''
    case 'skipped':
    case 'failed':
      return (
        run.reason ??
        (reasonCodeText(run.reasonCode) ||
          (run.priceUsd !== null
            ? t('chat.dcaSeenAt', { price: formatDcaPrice(run.priceUsd) })
            : ''))
      )
    default:
      return run.reason ?? (run.usd !== null ? formatDcaUsd(run.usd) : '')
  }
}

/** A local date + time for a tooltip or a title; '' when the stamp is unusable. */
function formatWhen(iso: string, withTime: boolean): string {
  const at = Date.parse(iso)
  if (!iso || Number.isNaN(at)) return ''
  const options: Intl.DateTimeFormatOptions = withTime
    ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }
    : { month: 'short', day: 'numeric' }
  return new Date(at).toLocaleString('en-US', options)
}

/* ── Pure helpers: the buys chart ───────────────────────────────────────── */

export type DcaColumnKind = 'filled' | 'pending' | 'parked' | 'skipped' | 'failed' | 'void'

export interface DcaChartColumn {
  run: DcaRun
  index: number
  kind: DcaColumnKind
  /** Left edge and width in 0–100 of the plot width. */
  x: number
  width: number
  /** The price's height, 0–100 from the top of the plot; null when unpriced. */
  y: number | null
}

export interface DcaChartModel {
  columns: DcaChartColumn[]
  /** The price domain the plot spans (bottom, top). */
  lo: number
  hi: number
  filled: number
  minPrice: number
  maxPrice: number
  avg: number | null
  avgY: number | null
  now: number | null
  nowY: number | null
}

function columnKind(status: DcaRunStatus): DcaColumnKind {
  if (status === 'filled' || status === 'pending' || status === 'parked') return status
  if (status === 'skipped') return status
  if (status === 'failed') return 'failed'
  return 'void'
}

/**
 * Lay the attempts out in time order, one equal slot each, on a price axis that
 * spans the buys (padded, not from zero: a DCA's buys sit within a few percent
 * of each other and a zero baseline would draw them all the same height).
 * Fewer than two priced buys → null: the card says so in a line instead.
 */
export function buildDcaChartModel(mandate: DcaMandate): DcaChartModel | null {
  const attempts = [...mandate.history]
    .sort((a, b) => a.n - b.n || Date.parse(a.at) - Date.parse(b.at))
    .slice(-DCA_CHART_MAX)
  const fills = attempts.filter((r) => r.status === 'filled' && r.priceUsd !== null)
  if (fills.length < 2) return null
  const avg = mandate.acquired.avgPriceUsd
  const now = mandate.acquired.currentPriceUsd
  const prices = attempts.map((r) => r.priceUsd).filter((p): p is number => p !== null && p > 0)
  const fillPrices = fills.map((r) => r.priceUsd as number)
  const domain = [...prices, ...[avg, now].filter((p): p is number => p !== null && p > 0)]
  if (domain.length === 0) return null
  const min = Math.min(...domain)
  const max = Math.max(...domain)
  const span = max - min
  // Pad by at least ±0.5 % of the average: near-identical prices (a 0.02 %
  // spread) then draw as bars of nearly equal height, and the y labels show
  // how narrow the band is, instead of one full bar next to an empty one.
  const ref = avg !== null && avg > 0 ? avg : domain.reduce((a, b) => a + b, 0) / domain.length
  const floor = ref * DCA_CHART_MIN_PAD
  const lo = Math.max(0, min - Math.max(span * 0.35, floor))
  const hi = max + Math.max(span * 0.12, floor)
  const yOf = (p: number | null): number | null =>
    p === null || !(p > 0) ? null : 100 - clamp01((p - lo) / (hi - lo)) * 100
  const slot = 100 / attempts.length
  return {
    columns: attempts.map((run, index) => ({
      run,
      index,
      kind: columnKind(run.status),
      x: index * slot,
      width: slot,
      y: yOf(run.priceUsd),
    })),
    lo,
    hi,
    filled: fills.length,
    minPrice: Math.min(...fillPrices),
    maxPrice: Math.max(...fillPrices),
    avg,
    avgY: yOf(avg),
    now,
    nowY: yOf(now),
  }
}

/** A local "HH:MM"; '' when the stamp is unusable. */
function formatClock(iso: string): string {
  const at = Date.parse(iso)
  if (!iso || Number.isNaN(at)) return ''
  return new Date(at).toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function localDay(iso: string): string {
  const at = Date.parse(iso)
  return Number.isNaN(at) ? '' : new Date(at).toDateString()
}

/** Decimals formatDcaPrice gives `price`: $2,860 → 0, $1.24 → 2, $0.00123 → 5. */
function priceDecimals(price: number): number {
  if (!(price > 0) || price >= 1000) return 0
  if (price >= 1) return 2
  return -Math.floor(Math.log10(price)) + 2
}

/**
 * The y axis's two labels (top, bottom), both at one precision: the average
 * label's, plus as many decimals (up to 6) as it takes for the two to differ
 * visibly — distinct, and a last-digit step no wider than a fifth of the band,
 * so rounding cannot misstate it: "$1.005" / "$0.995", never "$1.01" / "$1.00".
 */
export function dcaYAxisLabels(model: DcaChartModel): [string, string] {
  const ref = model.avg !== null && model.avg > 0 ? model.avg : (model.hi + model.lo) / 2
  if (ref < 1e-4) {
    // Subscript-zero prices: widen by significant digits instead ($0.0₅921).
    let sig = 3
    while (sig < 6 && formatSmall(model.hi, sig) === formatSmall(model.lo, sig)) sig += 1
    return [`$${formatSmall(model.hi, sig)}`, `$${formatSmall(model.lo, sig)}`]
  }
  const base = priceDecimals(ref)
  const fmt = (value: number, digits: number) => `$${grouped(value, digits, digits)}`
  let digits = base
  const span = model.hi - model.lo
  while (
    digits < 6 &&
    (fmt(model.hi, digits) === fmt(model.lo, digits) || 10 ** -digits > span / 5)
  ) {
    digits += 1
  }
  return [fmt(model.hi, digits), fmt(model.lo, digits)]
}

/**
 * The x axis's two end labels (first, last; last '' when there is one
 * column): dates, or local times when every plotted run fell on the same
 * calendar day — the same date twice says nothing.
 */
export function dcaAxisLabels(model: DcaChartModel): [string, string] {
  const first = model.columns[0]
  const last = model.columns[model.columns.length - 1]
  if (!first || !last) return ['', '']
  const day = localDay(first.run.at)
  const oneDay = day !== '' && model.columns.every((c) => localDay(c.run.at) === day)
  const label = (iso: string): string => (oneDay ? formatClock(iso) : formatWhen(iso, false))
  return [label(first.run.at), last === first ? '' : label(last.run.at)]
}

/** The chart's accessible summary. */
export function dcaChartSummary(model: DcaChartModel): string {
  return t('chat.dcaChartLabel', {
    count: String(model.filled),
    low: formatDcaPrice(model.minPrice),
    high: formatDcaPrice(model.maxPrice),
    avg: formatDcaPrice(model.avg),
    now: formatDcaPrice(model.now),
  })
}

/* ── Pure helpers: layout ───────────────────────────────────────────────── */

export type DcaLayout = 'wide' | 'narrow'

export function dcaLayoutFor(cardPx: number): DcaLayout {
  return cardPx >= DCA_WIDE_MIN_PX ? 'wide' : 'narrow'
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

/** A USD figure; null renders "—" with a "no price" hint, never "$0". */
function usdNode(className: string, value: number | null, format = formatUsd): HTMLElement {
  const node = el('span', className, format(value))
  if (value === null) {
    node.dataset.dcaNoPrice = 'true'
    node.title = t('chat.dcaNoPriceTitle')
  }
  return node
}

/** Everything the DOM builders need from the mounter. */
export interface DcaRenderContext {
  now: () => number
  copyText: (value: string) => void | Promise<void>
  /** A cancellable timer the mounter clears on unmount. */
  setTimer: (fn: () => void, ms: number) => void
  /** Offer ↻ on a card whose payload says how to re-run it. */
  readonly canRefresh?: boolean
  /**
   * Offer the mandate controls (approve, pause, buy now, stop…). Only an
   * operator connection (the desktop desk) can use them; the web never sets it.
   */
  readonly canWrite?: boolean
}

function statusPill(status: DcaStatus): HTMLElement {
  const pill = el('span', 'dca-pill')
  pill.dataset.status = status
  pill.append(
    hidden(el('span', 'dca-pill__dot')),
    el('span', 'dca-pill__text', statusLabel(status)),
  )
  return pill
}

/** "ETH ← USDC": what is bought, from what is spent. */
function pairNode(mandate: DcaMandate, className: string): HTMLElement {
  const pair = el('span', className)
  pair.title = t('chat.dcaPairTitle', { token: mandate.token.symbol, quote: mandate.quote.symbol })
  pair.append(
    el('span', 'dca-pair__token', mandate.token.symbol),
    hidden(el('span', 'dca-pair__arrow', ' ← ')),
    el('span', 'dca-pair__quote', mandate.quote.symbol),
  )
  return pair
}

function actionButton(action: DcaAction, id: string, compact: boolean): HTMLButtonElement {
  const button = el('button', 'dca-action', actionLabel(action)) as HTMLButtonElement
  button.type = 'button'
  button.dataset.dcaAction = action
  button.dataset.dcaId = id
  button.title = actionTitle(action)
  if (action === 'approve') button.dataset.dcaTone = 'primary'
  else if (action === 'stop' || action === 'reject') button.dataset.dcaTone = 'danger'
  if (compact) button.dataset.dcaCompact = 'true'
  return button
}

/**
 * The controls row for one mandate, keyed by `data-dca-id`. The mounter's
 * click handler calls the RPC and drives `data-dca-busy` and the error line.
 * Null for a terminal mandate (nothing left to control).
 */
function actionsRow(mandate: DcaMandate, compact: boolean): HTMLElement | null {
  const actions = dcaActionsFor(mandate.status)
  if (actions.length === 0) return null
  const row = el('div', compact ? 'dca-actions dca-actions--compact' : 'dca-actions')
  row.dataset.dcaId = mandate.id
  row.setAttribute('role', 'group')
  row.setAttribute('aria-label', t('chat.dcaActionsLabel', { name: mandate.name }))
  const buttons = el('div', 'dca-actions__buttons')
  actions.forEach((action) => buttons.append(actionButton(action, mandate.id, compact)))
  const error = el('p', 'dca-actions__error')
  error.setAttribute('role', 'alert')
  error.hidden = true
  row.append(buttons, error)
  return row
}

function nextNode(mandate: DcaMandate, className: string, nowMs: number): HTMLElement {
  const node = el('span', className, nextBuyText(mandate, nowMs))
  node.setAttribute('aria-live', 'off')
  if (mandate.status === 'active' && mandate.schedule.nextRunAt) {
    node.dataset.dcaNextAt = mandate.schedule.nextRunAt
    const left = msUntil(mandate.schedule.nextRunAt, nowMs)
    if (left !== null && left <= 0) node.dataset.dcaDue = 'true'
  }
  return node
}

function progressBar(mandate: DcaMandate, mini: boolean): HTMLElement {
  const model = dcaProgress(mandate)
  const bar = el('div', mini ? 'dca-progress dca-progress--mini' : 'dca-progress')
  bar.setAttribute('role', 'progressbar')
  bar.setAttribute('aria-valuemin', '0')
  bar.setAttribute('aria-valuemax', '100')
  bar.setAttribute('aria-valuenow', String(Math.round(model.spentPct)))
  bar.setAttribute('aria-valuetext', model.amountText)
  const spent = el('span', 'dca-progress__spent')
  spent.style.width = `${model.spentPct}%`
  bar.append(spent)
  if (model.reservedPct > 0) {
    const reserved = el('span', 'dca-progress__reserved')
    reserved.style.left = `${model.spentPct}%`
    reserved.style.width = `${model.reservedPct}%`
    reserved.title = t('chat.dcaReservedTitle', {
      usd: formatDcaUsd(mandate.budget.reservedUsd),
    })
    bar.append(reserved)
  }
  return bar
}

function progressSection(mandate: DcaMandate): HTMLElement {
  const model = dcaProgress(mandate)
  const section = el('section', 'dca-card__progress')
  section.append(progressBar(mandate, false))
  const labels = el('div', 'dca-progress__labels')
  labels.append(el('span', 'dca-progress__amount', model.amountText))
  const runs = el('span', 'dca-progress__runs', model.runsText)
  const extra: string[] = []
  if (mandate.runs.skipped > 0) {
    extra.push(t('chat.dcaSkippedCount', { count: String(mandate.runs.skipped) }))
  }
  if (mandate.runs.failed > 0) {
    extra.push(t('chat.dcaFailedCount', { count: String(mandate.runs.failed) }))
  }
  if (extra.length) runs.append(el('span', 'dca-progress__extra', ` · ${extra.join(' · ')}`))
  labels.append(runs)
  section.append(labels)
  if (mandate.budget.reservedUsd > 0) {
    const reserved = el(
      'span',
      'dca-progress__reserved-note',
      t('chat.dcaReserved', { usd: formatDcaUsd(mandate.budget.reservedUsd) }),
    )
    section.append(reserved)
  }
  return section
}

function guardsLine(mandate: DcaMandate): HTMLElement | null {
  const parts: string[] = []
  const { guards } = mandate
  if (guards.maxPriceUsd !== null) {
    parts.push(t('chat.dcaGuardMaxPrice', { price: formatDcaPrice(guards.maxPriceUsd) }))
  }
  if (guards.buysNeedApproval) parts.push(t('chat.dcaGuardApproval'))
  if (guards.slippagePct !== null) {
    parts.push(t('chat.dcaGuardSlippage', { pct: String(guards.slippagePct) }))
  }
  if (parts.length === 0) return null
  return el('p', 'dca-card__guards', parts.join(' · '))
}

function stat(
  key: string,
  label: string,
  value: HTMLElement,
  sub?: HTMLElement | null,
): HTMLElement {
  const cell = el('div', 'dca-stat')
  cell.dataset.dcaStat = key
  cell.append(el('span', 'dca-stat__label', label), value)
  if (sub) cell.append(sub)
  return cell
}

function toned(node: HTMLElement, value: number | null): HTMLElement {
  const tone = toneOf(value)
  if (tone) node.dataset.dcaTone = tone
  return node
}

function statsSection(mandate: DcaMandate): HTMLElement {
  const { acquired, token } = mandate
  const stats = el('section', 'dca-card__stats')

  // Before the first filled buy nothing is acquired, there is no average and
  // nothing to be up or down on: "—" in every cell, as for an unknown price —
  // never "0 ETH / $0.00" or "$0.00 on $0 spent".
  const bought = mandate.runs.done > 0
  if (bought) {
    const amount = el('span', 'dca-stat__value')
    const human = acquired.amount.human
    amount.append(
      el('span', 'dca-stat__amount', human ? formatTokenAmount(human) : '0'),
      el('span', 'dca-stat__symbol', ` ${token.symbol}`),
    )
    amount.title = `${human || '0'} ${token.symbol}`
    stats.append(
      stat(
        'acquired',
        t('chat.dcaStatAcquired'),
        amount,
        usdNode('dca-stat__sub', acquired.amount.usd, formatUsd),
      ),
    )
  } else {
    stats.append(
      stat('acquired', t('chat.dcaStatAcquired'), usdNode('dca-stat__value', null, formatUsd)),
    )
  }

  const avg = usdNode('dca-stat__value', bought ? acquired.avgPriceUsd : null, formatDcaPrice)
  let vs: HTMLElement | null = null
  if (bought && acquired.vsAvgPct !== null) {
    vs = toned(
      el('span', 'dca-stat__sub', t('chat.dcaVsNow', { pct: formatVsPct(acquired.vsAvgPct) })),
      acquired.vsAvgPct,
    )
    vs.title = t('chat.dcaVsNowTitle', { price: formatDcaPrice(acquired.currentPriceUsd) })
  } else if (bought && acquired.avgPriceUsd !== null) {
    vs = el('span', 'dca-stat__sub', t('chat.dcaNoPrice'))
  }
  stats.append(stat('avg', t('chat.dcaStatAvg'), avg, vs))

  const unrealizedUsd = bought ? acquired.unrealizedUsd : null
  const unrealized = toned(
    usdNode('dca-stat__value', unrealizedUsd, formatSignedUsd),
    unrealizedUsd,
  )
  const unrealizedSub = !bought
    ? null
    : unrealizedUsd === null
      ? el('span', 'dca-stat__sub', t('chat.dcaNoPrice'))
      : el(
          'span',
          'dca-stat__sub',
          t('chat.dcaOnSpent', { usd: formatDcaUsd(mandate.budget.spentUsd) }),
        )
  stats.append(stat('unrealized', t('chat.dcaStatUnrealized'), unrealized, unrealizedSub))

  // No buy yet and no gas spent: "—" like the other cells, not "$0.00".
  const gasUsd = !bought && acquired.gasUsd === 0 ? null : acquired.gasUsd
  const gas = usdNode('dca-stat__value', gasUsd, formatUsd)
  const buys = mandate.runs.done
  const gasSub =
    gasUsd !== null && buys > 0
      ? el('span', 'dca-stat__sub', t('chat.dcaGasPerBuy', { usd: formatUsd(gasUsd / buys) }))
      : null
  stats.append(stat('gas', t('chat.dcaStatGas'), gas, gasSub))
  return stats
}

/* ── the buys chart ── */

let tooltipSeq = 0

/**
 * A CSS `top` for a height `y` (0–100 of the SVG): the plot box also holds
 * its top padding, so the offset is built from the two lengths the stylesheet
 * sets on `.dca-chart__plot` rather than a bare percentage of the box.
 */
function plotTop(y: number): string {
  return `calc(var(--dca-plot-pad) + var(--dca-plot-h) * ${(y / 100).toFixed(4)})`
}

/** The top of a column's mark, 0–100 of the SVG height: where its tooltip anchors. */
function columnMarkTop(col: DcaChartColumn): number {
  switch (col.kind) {
    case 'filled':
      return col.y ?? 100
    case 'parked':
    case 'pending':
      return col.y ?? 60
    case 'skipped':
      return Math.max(0, (col.y ?? 94) - 1.5)
    case 'failed':
      return 86 // the HTML × sits on the baseline
    default:
      return 97
  }
}

/** Gap between a column's mark and its tooltip, in px. */
const TIP_GAP = 4

function columnLabel(col: DcaChartColumn, token: LpToken): string {
  const detail = runDetail(col.run, token)
  return [`#${col.run.n}`, formatWhen(col.run.at, true), runStatusLabel(col.run.status), detail]
    .filter(Boolean)
    .join(' · ')
}

function buildChart(mandate: DcaMandate, ctx: DcaRenderContext): HTMLElement {
  const model = buildDcaChartModel(mandate)
  if (!model) {
    const hint = el('p', 'dca-chart dca-chart--empty', t('chat.dcaChartHint'))
    hint.dataset.dcaChart = 'empty'
    return hint
  }
  const figure = el('figure', 'dca-chart')
  figure.dataset.dcaChart = 'buys'
  const plot = el('div', 'dca-chart__plot')
  const tooltipId = `dca-tip-${++tooltipSeq}`
  const tooltip = el('div', 'dca-chart__tooltip')
  tooltip.id = tooltipId
  tooltip.setAttribute('role', 'tooltip')
  tooltip.hidden = true
  // Placed inside the plot on every show (see `place`), so it never covers the
  // stats above the chart nor the legend and runs list below it.
  tooltip.dataset.dcaPlace = 'above'

  const chart = svg('svg', {
    class: 'dca-chart__svg',
    viewBox: '0 0 1000 100',
    preserveAspectRatio: 'none',
    role: 'group',
    'aria-label': dcaChartSummary(model),
  })
  chart.append(
    svg('line', {
      class: 'dca-chart__baseline',
      x1: 0,
      x2: 1000,
      y1: 100,
      y2: 100,
      'vector-effect': 'non-scaling-stroke',
    }),
  )

  const groups: SVGElement[] = []
  let generation = 0
  let current: DcaChartColumn | null = null
  const hide = (): void => {
    generation++
    current = null
    tooltip.hidden = true
    groups.forEach((g) => g.removeAttribute('data-hover'))
  }
  // Leaving a column waits a beat so the pointer can reach the tooltip's link.
  const hideSoon = (): void => {
    const mine = ++generation
    ctx.setTimer(() => {
      if (mine === generation) hide()
    }, 160)
  }
  const show = (col: DcaChartColumn, group: SVGElement): void => {
    generation++
    current = col
    const run = col.run
    const lines: HTMLElement[] = []
    const head = el('span', 'dca-chart__tooltip-line', `#${run.n} · ${formatWhen(run.at, true)}`)
    head.dataset.dcaLine = 'when'
    lines.push(head)
    const detail = runDetail(run, mandate.token)
    if (detail) {
      const line = el('span', 'dca-chart__tooltip-line', detail)
      line.dataset.dcaLine = 'detail'
      lines.push(line)
    }
    const status = el(
      'span',
      'dca-chart__tooltip-line',
      run.manual
        ? `${runStatusLabel(run.status)} · ${t('chat.dcaManual')}`
        : runStatusLabel(run.status),
    )
    status.dataset.dcaLine = 'status'
    status.dataset.dcaRunStatus = run.status
    lines.push(status)
    if (run.explorerUrl) {
      const link = el(
        'a',
        'dca-chart__tx',
        t('chat.dcaTx', { hash: shortAddress(run.txHash ?? '') || t('chat.dcaExplorer') }),
      ) as HTMLAnchorElement
      link.href = run.explorerUrl
      link.target = '_blank'
      link.rel = 'noopener noreferrer'
      link.tabIndex = -1
      lines.push(link)
    }
    tooltip.replaceChildren(...lines)
    tooltip.hidden = false
    place(col)
    groups.forEach((g) => g.removeAttribute('data-hover'))
    group.setAttribute('data-hover', 'true')
  }
  /**
   * Anchor the tooltip on the column's mark, inside the plot: a few px above
   * the mark's top, or just below it when there is no room above; centred on
   * the column, flipped to `start` / `end` near the side edges, and clamped
   * to the plot's top and bottom. `top`, `bottom` and `left` are inline, so
   * every skin places it the same; `data-dca-place` / `data-align` are the
   * hooks. Without a layout (detached, or a test DOM) it falls back to the
   * same anchor in CSS lengths.
   */
  const place = (col: DcaChartColumn): void => {
    const center = col.x + col.width / 2
    const y = columnMarkTop(col)
    const box = plot.getBoundingClientRect()
    const area = chart.getBoundingClientRect()
    const tipW = tooltip.offsetWidth
    const tipH = tooltip.offsetHeight
    let align: 'start' | 'center' | 'end' = center < 22 ? 'start' : center > 78 ? 'end' : 'center'
    tooltip.style.left = `${center}%`
    if (box.width > 0 && box.height > 0 && area.height > 0 && tipH > 0) {
      const cx = (box.width * center) / 100
      align = cx - tipW / 2 < 0 ? 'start' : cx + tipW / 2 > box.width ? 'end' : 'center'
      const mark = area.top - box.top + (area.height * y) / 100
      const above = mark - TIP_GAP - tipH >= 0
      const top = above ? mark - TIP_GAP - tipH : mark + TIP_GAP
      tooltip.dataset.dcaPlace = above ? 'above' : 'below'
      tooltip.style.top = `${Math.round(Math.max(0, Math.min(top, box.height - tipH)))}px`
      tooltip.style.bottom = 'auto'
    } else if (y >= 50) {
      tooltip.dataset.dcaPlace = 'above'
      tooltip.style.top = 'auto'
      tooltip.style.bottom = `calc(var(--dca-plot-h) * ${(1 - y / 100).toFixed(4)} + ${TIP_GAP}px)`
    } else {
      tooltip.dataset.dcaPlace = 'below'
      tooltip.style.top = `calc(${plotTop(y)} + ${TIP_GAP}px)`
      tooltip.style.bottom = 'auto'
    }
    tooltip.dataset.align = align
  }
  tooltip.addEventListener('mouseenter', () => generation++)
  tooltip.addEventListener('mouseleave', hideSoon)

  for (const col of model.columns) {
    const group = svg('g', {
      class: 'dca-chart__col',
      tabindex: 0,
      role: 'img',
      'aria-label': columnLabel(col, mandate.token),
      'aria-describedby': tooltipId,
      'data-index': col.index,
      'data-kind': col.kind,
    })
    const x0 = col.x * 10
    const w = col.width * 10
    const barW = Math.max(2, Math.min(w * 0.64, 42))
    const bx = x0 + (w - barW) / 2
    group.append(svg('rect', { class: 'dca-chart__hit', x: x0, y: 0, width: w, height: 100 }))
    if (col.kind === 'filled' && col.y !== null) {
      group.append(
        svg('rect', {
          class: 'dca-chart__bar',
          x: bx,
          y: col.y,
          width: barW,
          height: Math.max(1.5, 100 - col.y),
        }),
      )
    } else if (col.kind === 'parked' || col.kind === 'pending') {
      // In flight or waiting for approval: an outline, never a bar.
      const y = col.y ?? 60
      group.append(
        svg('rect', {
          class: col.kind === 'pending' ? 'dca-chart__pending' : 'dca-chart__parked',
          x: bx,
          y,
          width: barW,
          height: Math.max(1.5, 100 - y),
          'vector-effect': 'non-scaling-stroke',
        }),
      )
    } else if (col.kind === 'skipped') {
      group.append(
        svg('rect', {
          class: 'dca-chart__skip',
          x: bx,
          y: Math.max(0, (col.y ?? 94) - 1.5),
          width: barW,
          height: 3,
          'vector-effect': 'non-scaling-stroke',
        }),
      )
    } else if (col.kind === 'void') {
      group.append(
        svg('line', {
          class: 'dca-chart__void',
          x1: bx,
          x2: bx + barW,
          y1: 97,
          y2: 97,
          'vector-effect': 'non-scaling-stroke',
        }),
      )
    }
    group.addEventListener('mouseenter', () => show(col, group))
    group.addEventListener('mouseleave', hideSoon)
    group.addEventListener('focus', () => show(col, group))
    group.addEventListener('blur', hideSoon)
    group.addEventListener('keydown', (event) => {
      const key = (event as KeyboardEvent).key
      if (key === 'Escape') {
        hide()
        return
      }
      if (key === 'Enter' && current === col) {
        tooltip.querySelector<HTMLAnchorElement>('.dca-chart__tx')?.click()
        return
      }
      const target =
        key === 'ArrowRight'
          ? col.index + 1
          : key === 'ArrowLeft'
            ? col.index - 1
            : key === 'Home'
              ? 0
              : key === 'End'
                ? groups.length - 1
                : null
      if (target === null) return
      event.preventDefault()
      const next = groups[Math.max(0, Math.min(groups.length - 1, target))]
      ;(next as unknown as HTMLElement | undefined)?.focus?.()
    })
    groups.push(group)
    chart.append(group)
  }

  if (model.avgY !== null) {
    chart.append(
      svg('line', {
        class: 'dca-chart__avg-line',
        x1: 0,
        x2: 1000,
        y1: model.avgY,
        y2: model.avgY,
        'vector-effect': 'non-scaling-stroke',
      }),
    )
  }
  if (model.nowY !== null) {
    const line = svg('line', {
      class: 'dca-chart__now-line',
      x1: 0,
      x2: 1000,
      y1: model.nowY,
      y2: model.nowY,
      'vector-effect': 'non-scaling-stroke',
    })
    const tone = toneOf(mandate.acquired.vsAvgPct)
    if (tone) line.setAttribute('data-dca-tone', tone)
    chart.append(line)
  }
  plot.append(chart)

  // Failed runs: an HTML ×, so the glyph keeps its shape in the stretched plot.
  for (const col of model.columns) {
    if (col.kind !== 'failed') continue
    const mark = hidden(el('span', 'dca-chart__fail', '×'))
    mark.style.left = `${col.x + col.width / 2}%`
    plot.append(mark)
  }

  if (model.avgY !== null) {
    const label = hidden(
      el('span', 'dca-chart__avg', t('chat.dcaChartAvg', { price: formatDcaPrice(model.avg) })),
    )
    label.style.top = plotTop(model.avgY)
    // Both labels hang above their lines; when the average sits just under the
    // current price, its label hangs below instead so the two stay apart.
    // Near the top it hangs below too, clear of the y axis's top label.
    if (
      model.avgY < 16 ||
      (model.nowY !== null && model.avgY >= model.nowY && model.avgY - model.nowY < 14)
    ) {
      label.dataset.dcaFlip = 'true'
    }
    plot.append(label)
  }
  if (model.nowY !== null) {
    const label = hidden(
      el('span', 'dca-chart__now', t('chat.dcaChartNow', { price: formatDcaPrice(model.now) })),
    )
    label.style.top = plotTop(model.nowY)
    const tone = toneOf(mandate.acquired.vsAvgPct)
    if (tone) label.dataset.dcaTone = tone
    plot.append(label)
  }
  // The y axis: the top and bottom of the price range, at the left edge, so
  // near-equal bars read as "within a hair of each other", not as a trend.
  const yAxis = hidden(el('div', 'dca-chart__y'))
  const [yTop, yBottom] = dcaYAxisLabels(model)
  for (const [edge, price, y] of [
    ['top', yTop, 0],
    ['bottom', yBottom, 100],
  ] as const) {
    const label = el('span', 'dca-chart__ylabel', price)
    label.dataset.edge = edge
    label.style.top = plotTop(y)
    yAxis.append(label)
  }
  plot.append(yAxis)
  plot.append(tooltip)
  figure.append(plot)

  const axis = hidden(el('div', 'dca-chart__axis'))
  const [firstLabel, lastLabel] = dcaAxisLabels(model)
  if (model.columns.length > 0) axis.append(el('span', 'dca-chart__date', firstLabel))
  if (model.columns.length > 1) {
    const end = el('span', 'dca-chart__date', lastLabel)
    end.dataset.align = 'end'
    axis.append(end)
  }
  figure.append(axis)

  const legend = hidden(el('figcaption', 'dca-chart__legend'))
  const kinds = new Set(model.columns.map((c) => c.kind))
  const key = (name: string, label: string): void => {
    const item = el('span', 'dca-chart__key', label)
    item.dataset.key = name
    legend.append(item)
  }
  key('bar', t('chat.dcaLegendBuy'))
  if (model.avgY !== null) key('avg', t('chat.dcaLegendAvg'))
  if (model.nowY !== null) key('now', t('chat.dcaLegendNow'))
  if (kinds.has('pending')) key('pending', t('chat.dcaRunPending'))
  if (kinds.has('parked')) key('parked', t('chat.dcaRunParked'))
  if (kinds.has('skipped')) key('skipped', t('chat.dcaRunSkipped'))
  if (kinds.has('failed')) key('failed', t('chat.dcaRunFailed'))
  figure.append(legend)
  return figure
}

/* ── recent runs ── */

function runRow(run: DcaRun, mandate: DcaMandate, nowMs: number): HTMLElement {
  const row = el('li', 'dca-run')
  row.dataset.dcaRunStatus = run.status
  if (run.reasonCode) row.dataset.dcaReason = run.reasonCode
  row.append(el('span', 'dca-run__n', `#${run.n}`))
  const ago = relativeTime(run.at, nowMs)
  const time = el('time', 'dca-run__ago', ago || NO_VALUE)
  if (ago) {
    time.setAttribute('datetime', run.at)
    time.dataset.dcaAt = run.at
    time.title = formatWhen(run.at, true)
  }
  row.append(time)
  if (run.status === 'parked') {
    const status = el(
      'span',
      'dca-run__status',
      run.orderId
        ? t('chat.dcaRunParkedOrder', { order: shortOrder(run.orderId) })
        : runStatusLabel(run.status),
    )
    if (run.orderId) status.title = run.orderId
    row.append(status)
  } else {
    row.append(el('span', 'dca-run__status', runStatusLabel(run.status)))
    const detail = runDetail(run, mandate.token)
    if (detail) row.append(el('span', 'dca-run__detail', detail))
  }
  if (run.manual) row.append(el('span', 'dca-run__manual', t('chat.dcaManual')))
  if (run.explorerUrl) {
    const link = el('a', 'dca-run__link', '↗') as HTMLAnchorElement
    link.href = run.explorerUrl
    link.target = '_blank'
    link.rel = 'noopener noreferrer'
    const host = explorerHost(run.explorerUrl)
    link.title = t('chat.dcaExplorerTitle', { explorer: host })
    link.setAttribute('aria-label', t('chat.dcaExplorerTitle', { explorer: host }))
    row.append(link)
  }
  return row
}

function explorerHost(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

function runsSection(mandate: DcaMandate, nowMs: number): HTMLElement | null {
  if (mandate.history.length === 0) return null
  const section = el('section', 'dca-runs')
  const recent = mandate.history.slice(0, DCA_RECENT_RUNS)
  // "Recent buys" only while every row is one; a skip or a failure is a run
  // that bought nothing, and listing it under "buys" misreads it.
  const allFilled = recent.every((run) => run.status === 'filled')
  section.append(
    el('h4', 'dca-runs__title', allFilled ? t('chat.dcaRecent') : t('chat.dcaRecentRuns')),
  )
  const listNode = el('ol', 'dca-runs__list')
  recent.forEach((run) => listNode.append(runRow(run, mandate, nowMs)))
  section.append(listNode)
  return section
}

/* ── footer ── */

function copyButton(id: string, ctx: DcaRenderContext): HTMLElement {
  const button = el('button', 'dca-card__action dca-card__copy') as HTMLButtonElement
  button.type = 'button'
  button.dataset.dcaFoot = 'copy'
  button.title = t('chat.dcaCopyTitle', { id })
  button.setAttribute('aria-label', t('chat.dcaCopyTitle', { id }))
  const glyph = hidden(el('span', 'dca-card__action-glyph', '⧉'))
  const label = el('span', 'dca-card__action-label', t('chat.dcaCopyId'))
  button.append(glyph, label)
  let generation = 0
  const settle = (ok: boolean): void => {
    const mine = ++generation
    button.dataset.dcaCopied = ok ? 'true' : 'failed'
    glyph.textContent = ok ? '✓' : '✕'
    label.textContent = ok ? t('chat.dcaCopied') : t('chat.dcaCopyFailed')
    ctx.setTimer(() => {
      if (mine !== generation) return
      delete button.dataset.dcaCopied
      glyph.textContent = '⧉'
      label.textContent = t('chat.dcaCopyId')
    }, DCA_COPIED_MS)
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
  const button = el('button', 'dca-card__action dca-card__refresh') as HTMLButtonElement
  button.type = 'button'
  button.dataset.dcaFoot = 'refresh'
  button.title = t('chat.dcaRefreshTitle')
  button.setAttribute('aria-label', t('chat.dcaRefreshTitle'))
  button.append(
    hidden(el('span', 'dca-card__action-glyph', '↻')),
    el('span', 'dca-card__action-label', t('chat.dcaRefresh')),
  )
  return button
}

/**
 * Why a card's controls are off: its live read is in flight (`checking`) or
 * failed (`failed`). Stamped as `data-dca-stale` on the `.dca-card`.
 */
export type DcaStale = 'checking' | 'failed'

/** "state may be stale · ↻" — shown while a card's live read has failed. */
function staleHint(): HTMLElement {
  const hint = el('p', 'dca-card__stale')
  hint.setAttribute('role', 'status')
  hint.title = t('chat.dcaStaleTitle')
  const button = el('button', 'dca-card__stale-refresh', '↻') as HTMLButtonElement
  button.type = 'button'
  button.dataset.dcaFoot = 'refresh'
  button.title = t('chat.dcaRefreshTitle')
  button.setAttribute('aria-label', t('chat.dcaRefreshTitle'))
  hint.append(
    el('span', 'dca-card__stale-text', t('chat.dcaStale')),
    hidden(el('span', 'dca-sep', ' · ')),
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
  payload: DcaPayload,
  parts: string[],
  copyId: string,
  ctx: DcaRenderContext,
): HTMLElement {
  const foot = el('footer', 'dca-card__foot')
  const meta = el('span', 'dca-card__foot-meta')
  meta.textContent = parts.filter(Boolean).join(' · ')
  const ago = relativeTime(payload.fetchedAt, ctx.now())
  if (ago) {
    if (meta.textContent) meta.append(el('span', 'dca-sep', ' · '))
    meta.append(el('span', 'dca-card__as-of', `${t('chat.dcaAsOf')} `))
    const time = el('time', 'dca-card__ago', ago)
    time.setAttribute('datetime', payload.fetchedAt)
    time.dataset.dcaFetchedAt = payload.fetchedAt
    time.title = payload.fetchedAt
    meta.append(time)
  }
  foot.append(meta)
  const actions = el('span', 'dca-card__actions')
  if (ctx.canRefresh && payload.request) actions.append(refreshButton())
  if (copyId) actions.append(copyButton(copyId, ctx))
  if (actions.childElementCount) foot.append(actions)
  return foot
}

function warningsNode(warnings: string[]): HTMLElement | null {
  if (warnings.length === 0) return null
  const listNode = el('ul', 'dca-card__warnings')
  warnings.forEach((w) => listNode.append(el('li', 'dca-card__warning', w)))
  return listNode
}

function shell(kind: DcaKind): HTMLElement {
  const card = el('article', 'dca-card')
  card.dataset.dcaKind = kind
  // The unmeasured default; the mounter re-stamps it from the card's width.
  card.dataset.dcaLayout = 'narrow'
  return card
}

/* ── kind = mandate ── */

function buildMandate(payload: DcaMandatePayload, ctx: DcaRenderContext): HTMLElement {
  const { mandate } = payload
  const nowMs = ctx.now()
  const card = shell('mandate')
  card.dataset.dcaStatus = mandate.status
  card.dataset.dcaId = mandate.id
  if (mandate.chain?.key) card.dataset.dcaChain = mandate.chain.key

  const head = el('header', 'dca-card__head')
  const title = el('div', 'dca-card__title')
  title.append(pairNode(mandate, 'dca-card__pair'))
  if (mandate.chain) title.append(el('span', 'dca-chain', mandate.chain.name))
  title.append(statusPill(mandate.status))
  head.append(title, el('span', 'dca-card__name', mandate.name))
  card.append(head)

  const hero = el('section', 'dca-card__hero')
  const line = el('p', 'dca-card__hero-line')
  line.append(
    el('span', 'dca-card__usd', formatDcaUsd(mandate.budget.usdPerRun)),
    el('span', 'dca-card__every', ` ${scheduleLabel(mandate.schedule)}`),
  )
  hero.append(line, nextNode(mandate, 'dca-card__next', nowMs))
  if (mandate.statusReason) {
    hero.append(el('p', 'dca-card__reason', statusReasonText(mandate.statusReason)))
  }
  card.append(hero)

  card.append(progressSection(mandate))
  const guards = guardsLine(mandate)
  if (guards) card.append(guards)
  card.append(statsSection(mandate))
  card.append(buildChart(mandate, ctx))
  const runs = runsSection(mandate, nowMs)
  if (runs) card.append(runs)
  if (ctx.canWrite) {
    const actions = actionsRow(mandate, false)
    if (actions) card.append(actions)
  }
  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  card.append(footer(payload, [mandate.id, walletText(mandate.wallet)], mandate.id, ctx))
  return card
}

/* ── kind = mandates ── */

function mandateRow(mandate: DcaMandate, ctx: DcaRenderContext, nowMs: number): HTMLElement {
  const row = el('li', 'dca-row')
  row.dataset.dcaId = mandate.id
  row.dataset.dcaStatus = mandate.status
  if (mandate.chain?.key) row.dataset.dcaChain = mandate.chain.key
  const dot = hidden(el('span', 'dca-row__dot'))
  const main = el('div', 'dca-row__main')
  const top = el('div', 'dca-row__top')
  top.append(
    pairNode(mandate, 'dca-row__pair'),
    el(
      'span',
      'dca-row__plan',
      `${scheduleLabel(mandate.schedule)} · ${formatDcaUsd(mandate.budget.usdPerRun)}`,
    ),
    el('span', 'dca-row__state', statusLabel(mandate.status)),
  )
  const progress = el('div', 'dca-row__progress')
  progress.append(
    progressBar(mandate, true),
    el(
      'span',
      'dca-row__spent',
      `${formatDcaUsd(mandate.budget.spentUsd)} / ${formatDcaUsd(mandate.budget.capUsd)}`,
    ),
  )
  // The name leads: seven "USDC ← ETH · every 2 minutes · $0.04" rows are
  // otherwise indistinguishable. Pair · plan · state is the second line.
  const name = el('span', 'dca-row__name', mandate.name)
  name.title = mandate.name
  main.append(name, top, progress, nextNode(mandate, 'dca-row__next', nowMs))
  row.append(dot, main)
  if (ctx.canWrite) {
    const actions = actionsRow(mandate, true)
    if (actions) row.append(actions)
  }
  return row
}

function buildMandates(payload: DcaMandatesPayload, ctx: DcaRenderContext): HTMLElement {
  const nowMs = ctx.now()
  const card = shell('mandates')
  const head = el('header', 'dca-card__head')
  const title = el('div', 'dca-card__title')
  title.append(
    el('span', 'dca-card__list-title', t('chat.dcaListTitle')),
    hidden(el('span', 'dca-sep', ' · ')),
    el('span', 'dca-card__count', tPlural('chat.dcaMandates', payload.totals.count)),
  )
  head.append(title)
  if (payload.totals.active > 0) {
    head.append(
      el(
        'span',
        'dca-card__name',
        t('chat.dcaActiveCount', { count: String(payload.totals.active) }),
      ),
    )
  }
  card.append(head)

  if (payload.mandates.length === 0) {
    card.append(el('p', 'dca-card__empty', t('chat.dcaEmpty')))
  } else {
    const { totals } = payload
    if (totals.spentUsd !== null || totals.capUsd !== null) {
      card.append(
        el(
          'p',
          'dca-totals',
          t('chat.dcaTotals', {
            spent: formatDcaUsd(totals.spentUsd),
            cap: formatDcaUsd(totals.capUsd),
            acquired: formatDcaUsd(totals.acquiredUsd),
          }),
        ),
      )
    }
    const rows = el('ul', 'dca-rows')
    payload.mandates.forEach((m) => rows.append(mandateRow(m, ctx, nowMs)))
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
export function buildDcaCard(payload: DcaPayload, ctx: DcaRenderContext): HTMLElement {
  return payload.kind === 'mandate' ? buildMandate(payload, ctx) : buildMandates(payload, ctx)
}

/** Replace `host`'s card slot with the payload's card. */
export function renderDca(host: HTMLElement, payload: DcaPayload, ctx: DcaRenderContext): void {
  const slot = host.querySelector<HTMLElement>('.msg-artifact-dca__body')
  if (!slot) return
  slot.replaceChildren(buildDcaCard(payload, ctx))
  host.dataset.dcaHost = 'rendered'
}

/** Stamp `data-dca-layout` from the card's measured width; an unmeasured card keeps its default. */
export function layoutDcaCard(card: HTMLElement): void {
  const width = card.offsetWidth
  if (!(width > 0)) return
  const layout = dcaLayoutFor(width)
  if (card.dataset.dcaLayout !== layout) card.dataset.dcaLayout = layout
}

/* ── Mounter ────────────────────────────────────────────────────────────── */

/** A gateway RPC call: `rpc.call(method, params)`. */
export type DcaCall = (method: string, params: Record<string, unknown>) => Promise<unknown>

/**
 * The write capability the desktop desk hands the mounter for its operator
 * connection; the same shape as `LpActions`. Absent (the web) → no controls.
 */
export interface DcaActions {
  /** Calls `trading.dca.<action>` on the operator connection. */
  call: DcaCall
  /** A "Buy now" placed an order (the desk focuses it in the Book). */
  onOrder?: (orderId: string) => void
}

/** The read RPC a request re-runs. */
export function dcaReadMethod(request: DcaRequest): string {
  return `trading.dca.${request.kind}`
}

function rpcError(error: unknown): { code: string; message: string } {
  const e = error as { code?: unknown; message?: unknown } | null
  return {
    code: typeof e?.code === 'string' ? e.code : '',
    message: typeof e?.message === 'string' ? e.message : String(error),
  }
}

/** What a failed control says in `.dca-actions__error`. */
export function dcaErrorText(error: unknown): string {
  const { code, message } = rpcError(error)
  if (code === 'trading.operator_required') return t('chat.dcaErrOperator')
  return message || code || t('chat.dcaErrUnknown')
}

/**
 * A `trading.dca.changed` body as a mandate payload: the full payload, the
 * payload under `mandate`, or a bare mandate under `mandate`. Null otherwise.
 */
export function dcaChangedPayload(raw: unknown): {
  mandate: DcaMandate
  payload: DcaMandatePayload | null
} | null {
  const body = obj(raw)
  if (!body) return null
  const direct = body.kind === 'mandate' ? normalizeDcaPayload(body) : null
  if (direct?.kind === 'mandate') return { mandate: direct.mandate, payload: direct }
  const inner = obj(body.mandate)
  if (!inner) return null
  if (inner.kind === 'mandate') {
    const nested = normalizeDcaPayload(inner)
    return nested?.kind === 'mandate' ? { mandate: nested.mandate, payload: nested } : null
  }
  const mandate = normalizeDcaMandate(inner)
  return mandate ? { mandate, payload: null } : null
}

export interface DcaMounterDeps {
  /** Fetch a DCA artifact body from its (authenticated) URL. */
  fetchPayload: (url: string) => Promise<unknown>
  /**
   * The gateway call ↻ and the settle refresh re-run a card's read through
   * (`trading.dca.get` / `list` are agent-callable, so the web passes it too).
   * Default: none, no ↻ — unless `actions` is given, whose call then serves.
   */
  call?: DcaCall
  /**
   * The mandate controls. An object, or a getter read at render and click
   * time (the desktop's chat becomes the desk without remounting). Absent → no
   * `.dca-actions` at all.
   */
  actions?: DcaActions | (() => DcaActions | null | undefined) | null
  /** Copy the mandate id. Default: Clipboard API, then execCommand. */
  copyText?: (value: string) => void | Promise<void>
  /** Clock. Default: Date.now. */
  now?: () => number
  /** The diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/**
 * Create the DCA card mounter bound to the transcript's fetch surface.
 *
 * `mountDca(root)` is idempotent: it only picks up placeholders it has not
 * claimed yet. It owns one clock — every second while some mounted countdown
 * is under an hour, every minute otherwise — that re-derives the "next buy in"
 * lines and the "as of" stamps, plus the short copy / confirm resets;
 * `destroyAll` clears them all. One ResizeObserver keeps `data-dca-layout`
 * current.
 */
export function createDcaMounter(deps: DcaMounterDeps) {
  const diag = deps.diag ?? ((): void => {})
  const now = deps.now ?? ((): number => Date.now())
  const copyText = deps.copyText ?? copyLpText
  const getActions = (): DcaActions | null => {
    const value = typeof deps.actions === 'function' ? deps.actions() : deps.actions
    return value && typeof value.call === 'function' ? value : null
  }
  const readCall = (): DcaCall | null => deps.call ?? getActions()?.call ?? null
  const claimed = new Set<HTMLElement>()
  const rendered = new Set<HTMLElement>()
  const payloads = new Map<HTMLElement, DcaPayload>()
  const refreshing = new Set<HTMLElement>()
  const wired = new WeakSet<HTMLElement>()
  /** Controls in flight, by mandate id; they outlive a re-render. */
  const busy = new Map<string, DcaAction>()
  /** The last failed control's message, by mandate id, until the next click. */
  const errors = new Map<string, string>()
  /** Orders a "Buy now" placed, by order id → mandate id (refresh on settle). */
  const orders = new Map<string, string>()
  /**
   * Hosts whose card may not show the mandate as it is now: the artifact
   * snapshot while its live read is in flight (`checking`), or after that
   * read failed (`failed`). Their controls stay off (`data-dca-stale`).
   */
  const stale = new Map<HTMLElement, DcaStale>()
  /**
   * The newest live state of every mandate this mounter has seen (reads,
   * control answers, `trading.dca.changed`), by id. A transcript re-render
   * re-mounts a card from its artifact file — the snapshot taken when the
   * mandate was created — so a re-mounted card draws this instead. It holds
   * no DOM and outlives `destroyAll`.
   */
  const latestByMandateId = new Map<string, DcaMandatePayload>()
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
        if (card.isConnected) layoutDcaCard(card)
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
    const card = host.querySelector<HTMLElement>('.dca-card')
    if (!card) return
    const previous = cards.get(host)
    if (previous && previous !== card) observer?.unobserve(previous)
    cards.set(host, card)
    layoutDcaCard(card)
    observer?.observe(card)
  }

  function unwatch(host: HTMLElement): void {
    const card = cards.get(host)
    if (!card) return
    observer?.unobserve(card)
    dirty.delete(card)
    cards.delete(host)
  }

  const ctx: DcaRenderContext = {
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

  function mandatesOf(payload: DcaPayload): DcaMandate[] {
    return payload.kind === 'mandate' ? [payload.mandate] : payload.mandates
  }

  /* ── the live-state cache ── */

  function stampOf(mandate: DcaMandate): number {
    const at = Date.parse(mandate.updatedAt)
    return Number.isNaN(at) ? -Infinity : at
  }

  /** A mandate payload for a mandate that arrived without its envelope. */
  function envelopeFor(mandate: DcaMandate, fetchedAt: string): DcaMandatePayload {
    return {
      version: 1,
      fetchedAt,
      warnings: [],
      request: { kind: 'get', params: { mandateId: mandate.id } },
      kind: 'mandate',
      mandate,
      run: null,
    }
  }

  /** Keep `fresh` unless the cache already holds a strictly newer state. */
  function rememberMandate(fresh: DcaMandatePayload): void {
    const known = latestByMandateId.get(fresh.mandate.id)
    if (known && stampOf(known.mandate) > stampOf(fresh.mandate)) return
    latestByMandateId.set(fresh.mandate.id, { ...fresh, run: null })
  }

  /** Feed a live payload into the cache. */
  function remember(payload: DcaPayload): void {
    if (payload.kind === 'mandate') {
      rememberMandate(payload)
      return
    }
    const at = payload.fetchedAt || new Date(now()).toISOString()
    payload.mandates.forEach((m) => rememberMandate(envelopeFor(m, at)))
  }

  /**
   * A snapshot with every mandate the cache knows at least as fresh swapped
   * in. `live` is true when nothing of the snapshot's own state is left (a
   * mandate card with a cache hit, a list whose every row hit).
   */
  function withCache(snapshot: DcaPayload): { payload: DcaPayload; live: boolean } {
    const hit = (m: DcaMandate): DcaMandatePayload | null => {
      const known = latestByMandateId.get(m.id)
      return known && stampOf(known.mandate) >= stampOf(m) ? known : null
    }
    if (snapshot.kind === 'mandate') {
      const known = hit(snapshot.mandate)
      if (!known) return { payload: snapshot, live: false }
      return {
        payload: { ...known, request: snapshot.request ?? known.request, run: null },
        live: true,
      }
    }
    let live = snapshot.mandates.length > 0
    const mandates = snapshot.mandates.map((m) => {
      const known = hit(m)
      if (!known) live = false
      return known ? known.mandate : m
    })
    return { payload: { ...snapshot, mandates }, live }
  }

  /* ── the clock ── */

  function stopClock(): void {
    if (clock !== null) clearTimeout(clock)
    clock = null
  }

  function paintTimes(host: HTMLElement, payload: DcaPayload, at: number): void {
    const byId = new Map(mandatesOf(payload).map((m) => [m.id, m]))
    host.querySelectorAll<HTMLElement>('.dca-card__next, .dca-row__next').forEach((node) => {
      const id = node.closest<HTMLElement>('[data-dca-id]')?.dataset.dcaId ?? ''
      const mandate = byId.get(id)
      if (!mandate) return
      const next = nextBuyText(mandate, at)
      if (node.textContent !== next) node.textContent = next
      const left = mandate.status === 'active' ? msUntil(mandate.schedule.nextRunAt, at) : null
      if (left !== null && left <= 0) node.dataset.dcaDue = 'true'
    })
    host.querySelectorAll<HTMLElement>('[data-dca-fetched-at], [data-dca-at]').forEach((node) => {
      const stamp = node.dataset.dcaFetchedAt ?? node.dataset.dcaAt ?? ''
      const next = relativeTime(stamp, at)
      if (next && node.textContent !== next) node.textContent = next
    })
  }

  function nextDelay(at: number): number {
    let delay = DCA_MINUTE_MS
    rendered.forEach((host) => {
      const payload = payloads.get(host)
      if (!payload) return
      mandatesOf(payload).forEach((m) => {
        delay = Math.min(delay, dcaTickDelay(countdownMs(m, at)))
      })
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
    const card = host.querySelector<HTMLElement>('.dca-card')
    const staleState = stale.get(host)
    let anyBusy = false
    host.querySelectorAll<HTMLElement>('.dca-actions').forEach((row) => {
      const id = row.dataset.dcaId ?? ''
      const action = busy.get(id)
      if (action) {
        anyBusy = true
        row.dataset.dcaBusy = action
        row.setAttribute('aria-busy', 'true')
      } else {
        delete row.dataset.dcaBusy
        row.removeAttribute('aria-busy')
      }
      row.querySelectorAll<HTMLButtonElement>('button[data-dca-action]').forEach((b) => {
        b.disabled = Boolean(action) || staleState !== undefined
        if (action && b.dataset.dcaAction === action) b.dataset.dcaPending = 'true'
        else delete b.dataset.dcaPending
      })
      const line = row.querySelector<HTMLElement>('.dca-actions__error')
      if (line) {
        const message = action ? '' : (errors.get(id) ?? '')
        line.textContent = message
        line.hidden = message === ''
      }
    })
    if (card) {
      if (anyBusy && card.dataset.dcaKind === 'mandate') card.dataset.dcaBusy = 'true'
      else delete card.dataset.dcaBusy
      paintStale(card, staleState)
    }
  }

  /** `data-dca-stale` on the card, and the "may be stale · ↻" hint once the live read failed. */
  function paintStale(card: HTMLElement, state: DcaStale | undefined): void {
    if (state) card.dataset.dcaStale = state
    else delete card.dataset.dcaStale
    const hint = card.querySelector<HTMLElement>(':scope > .dca-card__stale')
    if (state !== 'failed') {
      hint?.remove()
      return
    }
    if (hint) return
    const foot = card.querySelector<HTMLElement>(':scope > .dca-card__foot')
    card.insertBefore(staleHint(), foot)
  }

  function paintAll(): void {
    rendered.forEach(paintControls)
  }

  /** Render (or re-render) a payload into its host and keep it fitted. */
  function show(host: HTMLElement, payload: DcaPayload): void {
    renderDca(host, payload, ctx)
    payloads.set(host, payload)
    paintControls(host)
    watch(host)
  }

  /**
   * Put a fresh mandate on every card that draws it: a mandate card of the
   * same id swaps to `fresh` (keeping its own ↻ request when `fresh` has
   * none), a list card replaces that row.
   */
  function applyMandate(mandate: DcaMandate, fresh: DcaMandatePayload | null): void {
    const stamp = new Date(now()).toISOString()
    remember(fresh ?? envelopeFor(mandate, stamp))
    rendered.forEach((host) => {
      const shown = payloads.get(host)
      if (!shown || !host.isConnected) return
      if (shown.kind === 'mandate') {
        if (shown.mandate.id !== mandate.id) return
        // A live state: whatever snapshot the card held is gone.
        stale.delete(host)
        const next: DcaMandatePayload = fresh
          ? {
              ...fresh,
              request: fresh.request ?? shown.request,
              fetchedAt: fresh.fetchedAt || stamp,
            }
          : { ...shown, mandate, run: null, fetchedAt: stamp }
        show(host, next)
      } else if (shown.mandates.some((m) => m.id === mandate.id)) {
        show(host, {
          ...shown,
          mandates: shown.mandates.map((m) => (m.id === mandate.id ? mandate : m)),
        })
      }
    })
    scheduleClock()
  }

  function armStop(button: HTMLButtonElement): void {
    button.dataset.dcaConfirm = 'true'
    button.textContent = t('chat.dcaStopConfirm')
    button.title = t('chat.dcaStopConfirmTitle')
    ctx.setTimer(() => {
      if (!button.isConnected || button.dataset.dcaConfirm !== 'true') return
      delete button.dataset.dcaConfirm
      button.textContent = t('chat.dcaStop')
      button.title = t('chat.dcaStopTitle')
    }, DCA_CONFIRM_MS)
  }

  async function act(button: HTMLButtonElement): Promise<void> {
    const actions = getActions()
    const id = button.dataset.dcaId ?? ''
    const action = button.dataset.dcaAction as DcaAction
    if (!actions || !id || !ACTIONS.has(action) || busy.has(id)) return
    if (action === 'stop' && button.dataset.dcaConfirm !== 'true') {
      armStop(button)
      return
    }
    busy.set(id, action)
    errors.delete(id)
    paintAll()
    diag('dca.action.start', { action, mandateId: id })
    try {
      const raw = await actions.call(`trading.dca.${action}`, { mandateId: id })
      const next = normalizeDcaPayload(raw)
      if (!next || next.kind !== 'mandate') throw new Error(t('chat.dcaUnreadable'))
      busy.delete(id)
      applyMandate(next.mandate, next)
      paintAll()
      const orderId = action === 'run' ? next.run?.orderId : null
      if (orderId) {
        orders.set(orderId, id)
        actions.onOrder?.(orderId)
      }
      diag('dca.action.done', { action, mandateId: id, status: next.mandate.status })
    } catch (error) {
      busy.delete(id)
      errors.set(id, dcaErrorText(error))
      paintAll()
      diag('dca.action.error', { action, error: String(error) })
      // "cannot approve …: it is completed": the card is out of date. Re-read
      // every card that draws this mandate, controls off until it lands.
      const { code } = rpcError(error)
      if (code === 'trading.dca.bad_state' || code === 'trading.dca.not_found') {
        rendered.forEach((host) => {
          const payload = payloads.get(host)
          if (!payload || !host.isConnected) return
          if (!mandatesOf(payload).some((m) => m.id === id)) return
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
    const foot = host.querySelector<HTMLElement>('.dca-card__foot')
    if (!foot) return
    let line = foot.querySelector<HTMLElement>('.dca-card__refresh-error')
    if (!line) {
      line = el('span', 'dca-card__refresh-error')
      line.setAttribute('role', 'alert')
      foot.append(line)
    }
    line.textContent = message
    const shown = line
    ctx.setTimer(() => shown.remove(), DCA_ERROR_MS)
  }

  /** The read that re-draws `payload`: its echoed request, else `get` by id. */
  function requestFor(payload: DcaPayload): DcaRequest | null {
    if (payload.request) return payload.request
    if (payload.kind === 'mandate')
      return { kind: 'get', params: { mandateId: payload.mandate.id } }
    return null
  }

  /**
   * Re-run a card's read and redraw it. `quiet` (the read a mount fires on
   * its own) neither dims the card nor flashes a failure under the footer: a
   * stale card says so with its hint instead. `call` overrides the read call.
   */
  async function refreshHost(
    host: HTMLElement,
    opts: { quiet?: boolean; call?: DcaCall | null } = {},
  ): Promise<void> {
    const current = payloads.get(host)
    const call = opts.call ?? readCall()
    const request = current ? requestFor(current) : null
    if (!current || !request || !call || refreshing.has(host)) return
    refreshing.add(host)
    const card = host.querySelector<HTMLElement>('.dca-card')
    const buttons = [...host.querySelectorAll<HTMLButtonElement>('[data-dca-foot="refresh"]')]
    if (!opts.quiet) {
      card?.setAttribute('data-dca-refreshing', 'true')
      card?.setAttribute('aria-busy', 'true')
      buttons.forEach((b) => (b.disabled = true))
    }
    diag('dca.refresh.start', { kind: request.kind, quiet: Boolean(opts.quiet) })
    try {
      const raw = await call(dcaReadMethod(request), request.params)
      const next = normalizeDcaPayload(raw)
      if (!next) throw new Error(t('chat.dcaUnreadable'))
      if (!next.request) next.request = current.request
      if (!next.fetchedAt) next.fetchedAt = new Date(now()).toISOString()
      remember(next)
      if (!host.isConnected || !rendered.has(host)) return
      stale.delete(host)
      show(host, next)
      scheduleClock()
      diag('dca.refresh.done', { kind: next.kind })
    } catch (error) {
      if (!opts.quiet) {
        card?.removeAttribute('data-dca-refreshing')
        card?.removeAttribute('aria-busy')
        buttons.forEach((b) => (b.disabled = false))
        flashFootError(host, t('chat.dcaRefreshFailed', { message: rpcError(error).message }))
      }
      if (stale.has(host) && rendered.has(host)) {
        stale.set(host, 'failed')
        paintControls(host)
      }
      diag('dca.refresh.error', { error: String(error) })
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
        '[data-dca-action], [data-dca-foot]',
      )
      if (!target || !host.contains(target) || (target as HTMLButtonElement).disabled) return
      if (target.dataset.dcaFoot === 'refresh') {
        void refreshHost(host)
        return
      }
      if (target.dataset.dcaAction) void act(target as HTMLButtonElement)
    })
  }

  /* ── events ── */

  /**
   * `trading.order.finished`: re-read every card that shows a run of that
   * order (a parked buy settling, a "Buy now" landing).
   */
  function orderFinished(orderId: string): void {
    if (!orderId) return
    const mandateId = orders.get(orderId)
    orders.delete(orderId)
    rendered.forEach((host) => {
      const payload = payloads.get(host)
      if (!payload || !host.isConnected) return
      const hit = mandatesOf(payload).some(
        (m) => m.id === mandateId || m.history.some((r) => r.orderId === orderId),
      )
      if (hit) void refreshHost(host)
    })
  }

  /** `trading.dca.changed`: swap the mandate into every card that draws it. */
  function mandateChanged(raw: unknown): void {
    const changed = dcaChangedPayload(raw)
    if (!changed) return
    diag('dca.changed', { mandateId: changed.mandate.id, status: changed.mandate.status })
    applyMandate(changed.mandate, changed.payload)
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
    const status = host.querySelector<HTMLElement>('.msg-artifact-dca__status')
    if (!status) return
    status.textContent = message
    status.hidden = message === ''
  }

  async function mountOne(host: HTMLElement): Promise<void> {
    const url = host.dataset.dcaSrc || ''
    if (!url) {
      setStatus(host, t('chat.dcaUnavailable'))
      return
    }
    try {
      diag('dca.mount.start', { url })
      const raw = await deps.fetchPayload(url)
      const payload = normalizeDcaPayload(raw)
      if (!payload) {
        setStatus(host, t('chat.dcaUnreadable'))
        diag('dca.mount.empty', { url })
        return
      }
      if (!host.isConnected || !claimed.has(host)) {
        claimed.delete(host)
        return
      }
      // The artifact is a snapshot from when it was published: draw the
      // newest state this mounter knows instead, and re-read the live one.
      // Until that read lands, a card still holding snapshot state keeps its
      // controls off — an "Approve & start" on a completed mandate must not
      // be clickable.
      const { payload: shown, live } = withCache(payload)
      const call = deps.call ?? null
      const reread = call !== null && requestFor(shown) !== null
      if (reread && !live) stale.set(host, 'checking')
      wire(host)
      rendered.add(host)
      show(host, shown)
      setStatus(host, '')
      scheduleClock()
      diag('dca.mount.done', { url, kind: payload.kind, cached: live })
      if (reread) void refreshHost(host, { quiet: true, call })
    } catch (error) {
      setStatus(host, t('chat.dcaFailed'))
      diag('dca.mount.error', { url, error: String(error) })
    }
  }

  /** Mount every not-yet-mounted DCA placeholder inside `root`. */
  function mountDca(root: HTMLElement | null | undefined): void {
    if (!root) return
    pruneDetached()
    root.querySelectorAll<HTMLElement>('[data-dca-src]').forEach((host) => {
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
    mountDca,
    destroyAll,
    pruneDetached,
    orderFinished,
    mandateChanged,
    refresh: refreshHost,
  }
}

export type DcaMounter = ReturnType<typeof createDcaMounter>
