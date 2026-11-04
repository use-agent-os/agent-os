import type { RawJob, RawRun } from '@/views/cron/logic'
import type { MessageKey } from '~/i18n'
import type { Bracket, Mandate, MandateRun, Trigger, TriggerFire } from '../types'
import { bracketFires, isTerminalBracket, legWord } from './bracket-logic'
import { TRADING_AGENT_ID } from './agent'
import { jobSessionKey, missionStatus } from './desk-logic'
import { isTerminalMandate } from './mandate-logic'
import { isTerminalTrigger } from './trigger-logic'

/**
 * The BOOK's Automations tab: every mission, DCA mandate, price trigger and
 * bracket the engine knows, in one list, grouped by what the user has to do
 * about it rather than by kind. The groups answer, in order: what is
 * running, what waits on me, what stopped on its own and why, what is over.
 *
 * Pure: the component hands the four lists in and draws what comes back.
 */

export type AutomationGroup = 'running' | 'needs' | 'paused' | 'finished'

/** The order the groups are drawn in — and the filter chips. */
export const AUTOMATION_GROUPS: readonly AutomationGroup[] = [
  'running',
  'needs',
  'paused',
  'finished',
]

export type AutomationFilter = 'all' | AutomationGroup

export type AutomationItem =
  | { kind: 'mission'; id: string; job: RawJob; group: AutomationGroup; at: number }
  | { kind: 'mandate'; id: string; mandate: Mandate; group: AutomationGroup; at: number }
  | { kind: 'trigger'; id: string; trigger: Trigger; group: AutomationGroup; at: number }
  | { kind: 'bracket'; id: string; bracket: Bracket; group: AutomationGroup; at: number }

/** A cron job the trading desk owns: run by the trading agent, or posting into a trading chat. */
export function isTradingJob(job: RawJob): boolean {
  if (job.agentId === TRADING_AGENT_ID) return true
  return jobSessionKey(job).startsWith(`agent:${TRADING_AGENT_ID}:`)
}

function stamp(iso: string | number | null | undefined): number {
  if (iso === null || iso === undefined || iso === '') return 0
  const n = typeof iso === 'number' ? iso * (iso < 1e12 ? 1000 : 1) : Date.parse(iso)
  return Number.isFinite(n) ? n : 0
}

/**
 * A mission's group. The scheduler has no "done" word: a mission the desk
 * turned off (it reported completion, hit its stop rule, or the user stopped
 * it) is simply disabled — that is "paused" here, since Continue brings it
 * back; one whose last run failed is still running (the next run is
 * scheduled) and says so in its state word.
 */
export function missionGroup(
  job: RawJob,
  ctx: { running: boolean; pendingApprovals: number },
): AutomationGroup {
  if (job.enabled === false) return 'paused'
  return missionStatus(job, ctx).state === 'awaiting' ? 'needs' : 'running'
}

export function mandateGroup(m: Mandate): AutomationGroup {
  if (m.status === 'awaiting_approval') return 'needs'
  if (m.status === 'paused') return 'paused'
  return isTerminalMandate(m) ? 'finished' : 'running'
}

export function triggerGroup(tr: Trigger): AutomationGroup {
  if (tr.status === 'awaiting_approval') return 'needs'
  if (tr.status === 'paused') return 'paused'
  return isTerminalTrigger(tr) ? 'finished' : 'running'
}

export function bracketGroup(b: Bracket): AutomationGroup {
  if (b.status === 'awaiting_approval') return 'needs'
  if (b.status === 'paused') return 'paused'
  return isTerminalBracket(b) ? 'finished' : 'running'
}

/**
 * Every automation as one list, newest movement first within a group. A
 * bracket's legs never appear on their own: `trading.trigger.list` leaves
 * them out, and the bracket row speaks for both.
 */
export function automationItems(input: {
  jobs: readonly RawJob[]
  running: ReadonlySet<string>
  pendingApprovals: number
  mandates: readonly Mandate[]
  triggers: readonly Trigger[]
  brackets: readonly Bracket[]
}): AutomationItem[] {
  const items: AutomationItem[] = []
  for (const job of input.jobs) {
    if (!job.id || !isTradingJob(job)) continue
    const ctx = { running: input.running.has(job.id), pendingApprovals: input.pendingApprovals }
    items.push({
      kind: 'mission',
      id: job.id,
      job,
      group: missionGroup(job, ctx),
      at: Math.max(stamp(job.last_run), stamp(job.created_at as string | number | undefined)),
    })
  }
  for (const m of input.mandates)
    items.push({
      kind: 'mandate',
      id: m.id,
      mandate: m,
      group: mandateGroup(m),
      at: stamp(m.updatedAt),
    })
  for (const b of input.brackets)
    items.push({
      kind: 'bracket',
      id: b.id,
      bracket: b,
      group: bracketGroup(b),
      at: stamp(b.updatedAt),
    })
  for (const tr of input.triggers)
    items.push({
      kind: 'trigger',
      id: tr.id,
      trigger: tr,
      group: triggerGroup(tr),
      at: stamp(tr.updatedAt),
    })
  return items.sort((x, y) => y.at - x.at)
}

/** How many items each chip would show; `all` counts everything. */
export function automationCounts(
  items: readonly AutomationItem[],
): Record<AutomationFilter, number> {
  const counts: Record<AutomationFilter, number> = {
    all: items.length,
    running: 0,
    needs: 0,
    paused: 0,
    finished: 0,
  }
  for (const it of items) counts[it.group] += 1
  return counts
}

/** Finished items drawn before "+N earlier" when the list is not filtered to them. */
export const FINISHED_SHOWN = 5

/** The groups to draw for a filter, each with its items in order; empty groups are left out. */
export function automationSections(
  items: readonly AutomationItem[],
  filter: AutomationFilter,
): { group: AutomationGroup; items: AutomationItem[] }[] {
  return AUTOMATION_GROUPS.filter((g) => filter === 'all' || filter === g)
    .map((group) => ({ group, items: items.filter((it) => it.group === group) }))
    .filter((s) => s.items.length > 0)
}

/** The filed-by chat of an item, when it is not the desk's own (null for unfiled or own). */
export function automationElsewhere(item: AutomationItem, sessionKey: string): string | null {
  const filed =
    item.kind === 'mission'
      ? jobSessionKey(item.job)
      : item.kind === 'mandate'
        ? item.mandate.sessionKey
        : item.kind === 'trigger'
          ? item.trigger.sessionKey
          : item.bracket.sessionKey
  return sessionKey && filed && filed !== sessionKey ? filed : null
}

// ── History rows ─────────────────────────────────────────────────────────────

export type HistoryTone = 'ok' | 'warn' | 'danger' | 'live' | 'muted'

/** One past run, fire or job run, in the words the row draws. */
export interface HistoryRow {
  key: string
  at: string
  tone: HistoryTone
  /** The state word: "Filled", "Skipped", "Alerted", "Failed", "OK". */
  wordKey: MessageKey
  /** The figures: "$10 → 0.0031 ETH @ $3,210". Empty when there are none. */
  figures: string
  /** Why, when the engine said: "ETH at $3,120 above $3,000". */
  reason: string | null
  /** For a bracket's fire, the leg that fired ("take-profit", "floor"). */
  leg: string | null
  /** By hand (Buy now / Fire now), not on the clock. */
  manual: boolean
  explorerUrl: string | null
  orderId: string | null
}

const RUN_TONE: Record<MandateRun['status'] | TriggerFire['status'], HistoryTone> = {
  pending: 'live',
  parked: 'live',
  filled: 'ok',
  alerted: 'ok',
  skipped: 'warn',
  expired: 'warn',
  rejected: 'warn',
  failed: 'danger',
}

const RUN_WORD: Record<MandateRun['status'] | TriggerFire['status'], MessageKey> = {
  pending: 'trading.auto.run.pending',
  parked: 'trading.auto.run.parked',
  filled: 'trading.auto.run.filled',
  alerted: 'trading.auto.run.alerted',
  skipped: 'trading.auto.run.skipped',
  expired: 'trading.auto.run.expired',
  rejected: 'trading.auto.run.rejected',
  failed: 'trading.auto.run.failed',
}

const usd = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v)
    ? null
    : `$${v.toLocaleString(undefined, { maximumFractionDigits: v < 1 ? 4 : 2 })}`

export function mandateRunRows(m: Pick<Mandate, 'id' | 'history' | 'token'>): HistoryRow[] {
  return m.history.map((run) => {
    const parts: string[] = []
    const spent = usd(run.usd)
    if (spent) parts.push(spent)
    if (run.amount?.human) parts.push(`→ ${run.amount.human} ${m.token.symbol}`)
    const price = usd(run.priceUsd)
    if (price) parts.push(`@ ${price}`)
    return {
      key: `${m.id}:${run.n}`,
      at: run.at,
      tone: RUN_TONE[run.status],
      wordKey: RUN_WORD[run.status],
      figures: parts.join(' '),
      reason: run.reason,
      leg: null,
      manual: run.manual,
      explorerUrl: run.explorerUrl,
      orderId: run.orderId,
    }
  })
}

function fireRow(id: string, f: TriggerFire, leg: string | null, suffix = ''): HistoryRow {
  const price = usd(f.priceUsd)
  return {
    key: `${id}:${suffix}${f.n}`,
    at: f.at,
    tone: RUN_TONE[f.status],
    wordKey: RUN_WORD[f.status],
    figures: price ? `@ ${price}` : '',
    reason: f.reason,
    leg,
    manual: f.manual,
    explorerUrl: f.explorerUrl,
    orderId: f.orderId,
  }
}

export function triggerFireRows(tr: Pick<Trigger, 'id' | 'fires'>): HistoryRow[] {
  return tr.fires.map((f) => fireRow(tr.id, f, null))
}

export function bracketFireRows(
  b: Pick<Bracket, 'id' | 'takeProfit' | 'stopLoss' | 'kind'>,
): HistoryRow[] {
  return bracketFires(b).map((f) => fireRow(b.id, f, legWord(f.leg, b.kind), `${f.leg}:`))
}

/** A mission's recorded runs (`cron.runs`), newest first, as history rows. */
export function missionRunRows(jobId: string, runs: readonly RawRun[]): HistoryRow[] {
  return runs.map((run, i) => {
    const ok = run.success !== false && run.status !== 'error'
    const ms = typeof run.duration_ms === 'number' ? run.duration_ms : null
    const error = typeof run.error === 'string' && run.error ? run.error : null
    return {
      key: `${jobId}:${run.id ?? i}`,
      at: String(run.started_at ?? run.finished_at ?? ''),
      tone: ok ? 'ok' : 'danger',
      wordKey: ok ? 'trading.auto.run.ok' : 'trading.auto.run.error',
      figures:
        ms !== null
          ? ms >= 60_000
            ? `${Math.round(ms / 60_000)} min`
            : `${Math.round(ms / 1000)} s`
          : '',
      reason: (ok ? run.summary : (error ?? run.summary)) ?? null,
      leg: null,
      manual: false,
      explorerUrl: null,
      orderId: null,
    }
  })
}

/** The history an item carries with it; a mission's is fetched (`cron.runs`) and passed in. */
export function itemHistory(item: AutomationItem, runs?: readonly RawRun[]): HistoryRow[] {
  switch (item.kind) {
    case 'mandate':
      return mandateRunRows(item.mandate)
    case 'trigger':
      return triggerFireRows(item.trigger)
    case 'bracket':
      return bracketFireRows(item.bracket)
    default:
      return missionRunRows(item.id, runs ?? [])
  }
}
