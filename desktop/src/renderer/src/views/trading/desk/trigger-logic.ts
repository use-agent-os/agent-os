import { t, type MessageKey } from '~/i18n'
import { formatAmount } from '../logic'
import type { Bracket, Trigger, TriggerFire, TriggerStatus } from '../types'
import { FINISHED_ROWS, FINISHED_VISIBLE_MS } from './mandate-logic'

/**
 * Price triggers on the desk (docs/triggers.md): the engine watches the
 * price, confirms the condition and fires, so nothing here evaluates one.
 * These helpers only turn the engine's figures into words and decide which
 * triggers a desk lists.
 */

/** An armed trigger this close to its line (in %) is "near". */
export const TRIGGER_NEAR_PCT = 1

const LIVE: ReadonlySet<TriggerStatus> = new Set([
  'awaiting_approval',
  'armed',
  'triggered',
  'paused',
])
const TERMINAL: ReadonlySet<TriggerStatus> = new Set(['done', 'stopped', 'rejected', 'expired'])

export function isTerminalTrigger(tr: Pick<Trigger, 'status'>): boolean {
  return TERMINAL.has(tr.status)
}

/** Pause, Resume, Fire now and Stop apply; a pending one is decided on its card. */
export function isSteerableTrigger(tr: Pick<Trigger, 'status'>): boolean {
  return tr.status === 'armed' || tr.status === 'paused'
}

const usdWhole = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})
const usdCents = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})
const usdSmall = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumSignificantDigits: 4,
})

/**
 * A price as a trader reads it: "$3,790" from a thousand up, "$1.25" from a
 * dollar, "$0.9998" / "$0.00001234" under one; "—" when unknown (USD null
 * means unknown, never zero).
 */
export function priceText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs >= 1000) return usdWhole.format(value)
  if (abs >= 1) return usdCents.format(value)
  return usdSmall.format(value)
}

const usdTiny = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumSignificantDigits: 2,
})

/**
 * "$189" for whole dollars, "$12.40" otherwise, "$0.0028" under a cent, "—"
 * when unknown. A non-zero amount never reads "$0": a dust-sized bracket once
 * said "≈ $0" on the desk while its chat card said "≈ $0.00275".
 */
export function usdText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value !== 0 && Math.abs(value) < 0.01) return usdTiny.format(value)
  return Math.abs(value - Math.round(value)) < 0.005 || Math.abs(value) >= 100
    ? usdWhole.format(Math.round(value))
    : usdCents.format(value)
}

/** "10 %": a percent the way the contract writes one (a thin space, no trailing zeros). */
export function pctText(value: number): string {
  return `${Number(value.toFixed(2))} %`
}

/**
 * The signed move the price still has to make: "−2.1 %" (must fall), "+4.0 %"
 * (must rise), "0 %" once met; '' when unknown.
 */
export function distanceText(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return ''
  if (pct === 0) return '0 %'
  const abs = Math.abs(pct)
  const body = abs < 0.1 ? abs.toFixed(2) : abs.toFixed(1)
  return `${pct < 0 ? '−' : '+'}${body} %`
}

/** An armed trigger within 1 % of its line (or past it, waiting to confirm). */
export function isNear(tr: Pick<Trigger, 'status' | 'market'>): boolean {
  const d = tr.market?.distancePct
  return (
    tr.status === 'armed' &&
    typeof d === 'number' &&
    Number.isFinite(d) &&
    Math.abs(d) <= TRIGGER_NEAR_PCT
  )
}

/**
 * An armed bracket within 1 % of either line (docs/brackets.md): the move left
 * to the take-profit or to the stop. Lives here so the trigger chip can count
 * a bracket without importing the bracket helpers back.
 */
export function isRangeNear(b: Pick<Bracket, 'status' | 'market'>): boolean {
  if (b.status !== 'armed') return false
  const close = (v: number | null | undefined) =>
    typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= TRIGGER_NEAR_PCT
  return close(b.market?.upsidePct) || close(b.market?.downsidePct)
}

/** "under $3,800", "over $5,000", "10 % below peak": the engine's label, else built. */
export function conditionText(tr: Pick<Trigger, 'condition'>): string {
  const c = tr.condition
  if (c.label) return c.label
  if (c.direction === 'trail') return c.trailPct !== null ? `${pctText(c.trailPct)} below peak` : ''
  return `${c.direction === 'below' ? 'under' : 'over'} ${priceText(c.priceUsd)}`
}

/** The size alone: "50 %", "0.05 ETH", "$50"; '' for an alert. */
export function sizeOnly(tr: Pick<Trigger, 'action' | 'token'>): string {
  const a = tr.action
  if (a.kind === 'alert') return ''
  if (a.amountPct !== null) return pctText(a.amountPct)
  if (a.amount) return `${formatAmount(a.amount.human)} ${tr.token.symbol}`
  if (a.amountUsd !== null) return usdText(a.amountUsd)
  return ''
}

/** "sell 50 % of ETH", "sell 0.05 ETH", "buy $50 of ETH", "notify". */
export function actionText(tr: Pick<Trigger, 'action' | 'token'>): string {
  const a = tr.action
  const symbol = tr.token.symbol
  if (a.kind === 'alert') return 'notify'
  const verb = a.kind === 'sell' ? 'sell' : 'buy'
  if (a.amount && a.amountPct === null) return `${verb} ${sizeOnly(tr)}`
  const size = sizeOnly(tr)
  return size ? `${verb} ${size} of ${symbol}` : `${verb} ${symbol}`
}

/**
 * The card's one sentence: "sell 50 % of ETH when under $3,800", "buy $50 of
 * ETH when under $3,500", "notify when ETH is over $5,000".
 */
export function heroText(tr: Pick<Trigger, 'action' | 'token' | 'condition'>): string {
  const when = conditionText(tr)
  if (tr.action.kind === 'alert') return `notify when ${tr.token.symbol} is ${when}`
  return `${actionText(tr)} when ${when}`
}

/** The fire as it happens, in the present: "selling 50 % of ETH", "buying $50 of ETH". */
export function actionProgressive(tr: Pick<Trigger, 'action' | 'token'>): string {
  const text = actionText(tr)
  if (text.startsWith('sell')) return `selling${text.slice(4)}`
  if (text.startsWith('buy')) return `buying${text.slice(3)}`
  return text
}

/** "sell 50 % · under $3,800": a Missions row's plan, short. */
export function planText(tr: Pick<Trigger, 'action' | 'token' | 'condition'>): string {
  const when = conditionText(tr)
  if (tr.action.kind === 'alert') return `${tr.token.symbol} ${when}`
  const size = sizeOnly(tr)
  return [`${tr.action.kind}${size ? ` ${size}` : ''} ${tr.token.symbol}`, when].join(' · ')
}

/**
 * What a done trigger's order actually moved: "0.02 ETH → 42.4 USDC · ≈ $42.40"
 * (the in side alone when the out side is unknown); '' without a result.
 */
export function movedText(tr: Pick<Trigger, 'action' | 'token' | 'quote' | 'result'>): string {
  const r = tr.result
  if (!r?.amountIn?.human) return ''
  const [inSym, outSym] =
    tr.action.kind === 'buy'
      ? [tr.quote.symbol, tr.token.symbol]
      : [tr.token.symbol, tr.quote.symbol]
  const moved = r.amountOut?.human
    ? `${formatAmount(r.amountIn.human)} ${inSym} → ${formatAmount(r.amountOut.human)} ${outSym}`
    : `${formatAmount(r.amountIn.human)} ${inSym}`
  const usd = r.amountIn.usd ?? tr.action.estimatedUsd
  return usd !== null && Number.isFinite(usd) ? `${moved} · ≈ ${usdText(usd)}` : moved
}

/**
 * "50 % · ≈ $189", "$50", "0.05 ETH · ≈ $189", "—": the card's size fact. A
 * done trigger with a result says what its order moved instead of the plan.
 */
export function sizeText(
  tr: Pick<Trigger, 'action' | 'token'> & Partial<Pick<Trigger, 'quote' | 'result' | 'status'>>,
): string {
  if (tr.status === 'done' && tr.result && tr.quote) {
    const moved = movedText({ ...tr, quote: tr.quote, result: tr.result })
    if (moved) return moved
  }
  const size = sizeOnly(tr)
  if (!size) return '—'
  const est = tr.action.estimatedUsd
  if (tr.action.amountUsd !== null && tr.action.amountPct === null && !tr.action.amount) return size
  return est !== null && Number.isFinite(est) ? `${size} · ≈ ${usdText(est)}` : size
}

const STATE_KEYS: Record<TriggerStatus, MessageKey> = {
  awaiting_approval: 'trading.trigger.state.awaiting',
  armed: 'trading.trigger.state.armed',
  triggered: 'trading.trigger.state.triggered',
  paused: 'trading.trigger.state.paused',
  done: 'trading.trigger.state.done',
  stopped: 'trading.trigger.state.stopped',
  rejected: 'trading.trigger.state.rejected',
  expired: 'trading.trigger.state.expired',
}

export function triggerStateKey(tr: Pick<Trigger, 'status'>): MessageKey {
  return STATE_KEYS[tr.status] ?? 'trading.trigger.state.done'
}

/**
 * What the trigger is doing, as the Missions rows say it: "Awaiting
 * approval", "Armed · ETH $3,790 · −0.3 %", "Armed · 1 of 2 checks",
 * "Armed · waiting for a price", "Triggered · order open", "Paused", "Done".
 */
export function triggerWord(tr: Trigger): string {
  const state = t(triggerStateKey(tr))
  if (tr.status === 'triggered') return `${state} · ${t('trading.trigger.word.orderOpen')}`
  // Done says how it ended ("Done · sold 0.05 ETH … at $3,788"); never a
  // distance — a finished trigger has no line left to reach.
  if (tr.status === 'done') {
    const reason = triggerReason(tr)
    return reason && reason !== 'user' ? `${state} · ${reason}` : state
  }
  if (tr.status !== 'armed') return state
  const hits = tr.condition?.hits ?? 0
  const need = tr.condition?.confirmTicks ?? 2
  if (hits >= 1) {
    return `${state} · ${t('trading.trigger.word.checks')
      .replace('{hits}', String(hits))
      .replace('{need}', String(need))}`
  }
  const price = tr.market?.priceUsd ?? null
  if (price === null) return `${state} · ${t('trading.trigger.word.noPrice')}`
  const dist = distanceText(tr.market.distancePct)
  return [state, `${tr.token.symbol} ${priceText(price)}`, dist].filter(Boolean).join(' · ')
}

/**
 * The live line under the card's sentence: "ETH $3,790 · 0.3 % above the
 * line", "fires after 1 more check", "armed, waiting for a price", "awaiting
 * approval".
 */
export function nowText(tr: Trigger): string {
  // Finished: how it ended, never a stale distance to a line it no longer watches.
  if (TERMINAL.has(tr.status)) {
    const state = t(triggerStateKey(tr)).toLowerCase()
    const reason = triggerReason(tr)
    return reason && reason !== 'user' ? `${state} · ${reason}` : state
  }
  if (tr.status === 'awaiting_approval') {
    const price = tr.market?.priceUsd ?? tr.token.priceUsd
    return price !== null
      ? `${tr.token.symbol} ${priceText(price)} · ${t('trading.trigger.state.awaiting').toLowerCase()}`
      : t('trading.trigger.state.awaiting').toLowerCase()
  }
  const price = tr.market?.priceUsd ?? null
  if (price === null) return t('trading.trigger.now.noPrice')
  const head = `${tr.token.symbol} ${priceText(price)}`
  const hits = tr.condition?.hits ?? 0
  const need = tr.condition?.confirmTicks ?? 2
  if (tr.status === 'armed' && hits >= 1 && need > hits) {
    const left = need - hits
    return `${head} · ${t(left === 1 ? 'trading.trigger.now.oneMore' : 'trading.trigger.now.more').replace('{n}', String(left))}`
  }
  const d = tr.market?.distancePct
  if (typeof d !== 'number' || !Number.isFinite(d)) return head
  if (d === 0) return `${head} · ${t('trading.trigger.now.met')}`
  // Must fall → the price sits above the line; must rise → below it.
  const side = d < 0 ? t('trading.trigger.now.above') : t('trading.trigger.now.below')
  return `${head} · ${side.replace('{pct}', pctText(Number(Math.abs(d).toFixed(1))))}`
}

/**
 * Why a finished or paused trigger is where it is, for the row's tooltip: the
 * engine's `statusReason`, a "user: …" note reduced to the note. Null when
 * there is none.
 */
export function triggerReason(tr: Pick<Trigger, 'statusReason'>): string | null {
  const reason = tr.statusReason?.trim()
  if (!reason) return null
  if (reason.startsWith('user:')) return reason.slice(5).trim() || 'user'
  return reason
}

/**
 * The triggers a desk lists beside its missions. Every live one, in the
 * engine's order, whichever chat filed it (a stop-loss guards the wallet,
 * not a chat; the row says where it is filed); then the finished ones for
 * an hour after they ended, newest first — this session's and the unfiled
 * ones (the CLI's, the operator's and so every desk's).
 */
export function deskTriggers(all: readonly Trigger[], sessionKey: string, now: number): Trigger[] {
  const mine = all.filter((tr) => !tr.sessionKey || tr.sessionKey === sessionKey)
  // Another chat's proposal is decided on its card (the approvals region
  // lists every pending one); only what runs is listed here.
  const live = all.filter(
    (tr) => LIVE.has(tr.status) && (mine.includes(tr) || tr.status !== 'awaiting_approval'),
  )
  const finished = mine
    .filter((tr) => {
      if (!TERMINAL.has(tr.status)) return false
      const at = Date.parse(tr.updatedAt)
      return Number.isFinite(at) && now - at < FINISHED_VISIBLE_MS
    })
    .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
  return [...live, ...finished]
}

/** Every live trigger, then at most FINISHED_ROWS finished ones; `more` counts the rest. */
export function triggerRows(
  triggers: readonly Trigger[],
  all = false,
): { rows: Trigger[]; more: number } {
  const live = triggers.filter((tr) => !TERMINAL.has(tr.status))
  const finished = triggers.filter((tr) => TERMINAL.has(tr.status))
  return {
    rows: [...live, ...(all ? finished : finished.slice(0, FINISHED_ROWS))],
    more: Math.max(0, finished.length - FINISHED_ROWS),
  }
}

export interface TriggerChip {
  /** Live triggers the chip stands for. */
  count: number
  /**
   * fired: one is `triggered` (its order is open); near: an armed one is
   * within 1 % of its line; awaiting: one waits for approval; armed: watching;
   * paused: none watches.
   */
  word: 'fired' | 'near' | 'awaiting' | 'armed' | 'paused'
}

/**
 * The status strip's one trigger chip; null when none is live. A bracket
 * (docs/brackets.md) counts once, never as its two legs: it is near when
 * either line is within 1 %, fired while a leg's order is open.
 */
export function triggerChip(
  triggers: readonly Trigger[],
  brackets: readonly Pick<Bracket, 'status' | 'market'>[] = [],
): TriggerChip | null {
  const live = triggers.filter((tr) => LIVE.has(tr.status))
  const liveBrackets = brackets.filter((b) => LIVE.has(b.status))
  const count = live.length + liveBrackets.length
  if (count === 0) return null
  const any = (status: TriggerStatus) =>
    live.some((tr) => tr.status === status) || liveBrackets.some((b) => b.status === status)
  if (any('triggered')) return { count, word: 'fired' }
  if (live.some(isNear) || liveBrackets.some(isRangeNear)) return { count, word: 'near' }
  if (any('awaiting_approval')) return { count, word: 'awaiting' }
  if (any('armed')) return { count, word: 'armed' }
  return { count, word: 'paused' }
}

/**
 * "Triggers ×2", "Trigger · near", "Trigger · fired", "Triggers ×2 · awaiting".
 * A lone trigger always says its state; several say only what needs an eye.
 */
export function triggerChipText(chip: TriggerChip): string {
  const head =
    chip.count > 1
      ? `${t('trading.trigger.chip.many')} ×${chip.count}`
      : t('trading.trigger.chip.one')
  if (chip.count > 1 && chip.word === 'armed') return head
  return `${head} · ${t(`trading.trigger.chip.${chip.word}`)}`
}

/** "Fire now", or "Sell now" / "Buy now" for the two kinds that trade. */
export function fireNowKey(tr: Pick<Trigger, 'kind'>): MessageKey {
  return tr.kind === 'sell'
    ? 'trading.trigger.sellNow'
    : tr.kind === 'buy'
      ? 'trading.trigger.buyNow'
      : 'trading.trigger.fireNow'
}

/**
 * What the engine would warn about at creation, read from the trigger
 * itself (a list payload carries warnings for the whole list, not per
 * trigger): already past the line, over the approval threshold, nothing to
 * sell, no price.
 */
export function triggerNotes(tr: Trigger, walletName: string): string[] {
  const notes: string[] = []
  const symbol = tr.token.symbol
  const price = tr.market?.priceUsd ?? null
  if (price === null) {
    notes.push(t('trading.trigger.warn.noPrice').replace('{token}', symbol))
  } else if (tr.market.distancePct === 0 && tr.condition.direction !== 'trail') {
    notes.push(
      t('trading.trigger.warn.already')
        .replace('{token}', symbol)
        .replace('{price}', priceText(price))
        .replace('{when}', conditionText(tr)),
    )
  }
  if (tr.kind !== 'alert' && tr.action.needsApproval) {
    notes.push(
      t('trading.trigger.warn.overThreshold')
        .replace('{kind}', tr.kind)
        .replace('{usd}', usdText(tr.action.estimatedUsd ?? tr.action.amountUsd))
        .replace('{threshold}', usdText(tr.action.approvalThresholdUsd)),
    )
  }
  const bal = tr.market?.balance
  if (tr.kind === 'sell' && bal && Number(bal.human) === 0) {
    notes.push(
      t('trading.trigger.warn.nothingToSell')
        .replace('{wallet}', walletName)
        .replace('{token}', symbol),
    )
  }
  return notes
}

/** The fire a desk control just made: the answer's `fire`, else the newest manual one. */
export function answeredFire(
  fire: TriggerFire | undefined,
  tr: Pick<Trigger, 'fires'> | undefined,
): TriggerFire | undefined {
  if (fire) return fire
  const newest = tr?.fires?.[0]
  return newest?.manual ? newest : undefined
}
