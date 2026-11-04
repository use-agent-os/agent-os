import { t, type MessageKey } from '~/i18n'
import { formatAmount } from '../logic'
import type {
  Bracket,
  BracketKind,
  BracketLeg,
  Trigger,
  TriggerFire,
  TriggerStatus,
} from '../types'
import { FINISHED_ROWS, FINISHED_VISIBLE_MS } from './mandate-logic'
import {
  isRangeNear,
  pctText,
  priceText,
  triggerNotes,
  triggerReason,
  triggerStateKey,
  usdText,
} from './trigger-logic'

/**
 * Brackets on the desk (docs/brackets.md): a take-profit and a stop-loss on
 * one position, shown, approved and steered as one thing. The engine derives
 * the bracket's status from its legs and keeps them consistent (one cancels
 * the other); these helpers only put its figures into words and decide which
 * brackets a desk lists. A leg is never a row of its own.
 */

const LIVE: ReadonlySet<TriggerStatus> = new Set([
  'awaiting_approval',
  'armed',
  'triggered',
  'paused',
])
const TERMINAL: ReadonlySet<TriggerStatus> = new Set(['done', 'stopped', 'rejected', 'expired'])

export function isTerminalBracket(b: Pick<Bracket, 'status'>): boolean {
  return TERMINAL.has(b.status)
}

/** Pause, Resume, Sell now and Stop apply; a pending one is decided on its card. */
export function isSteerableBracket(b: Pick<Bracket, 'status'>): boolean {
  return b.status === 'armed' || b.status === 'paused'
}

/** An armed bracket within 1 % of either line (or past one, waiting to confirm). */
export function bracketNear(b: Pick<Bracket, 'status' | 'market'>): boolean {
  return isRangeNear(b)
}

const LEG_KEYS: Record<BracketKind, Record<BracketLeg, MessageKey>> = {
  sell: { tp: 'trading.bracket.leg.tp', sl: 'trading.bracket.leg.sl' },
  // A range alert sells nothing: its legs are the range's two edges, as the
  // engine's reasons say them ("ceiling: alerted at $1", "range left over the top").
  alert: { tp: 'trading.bracket.leg.ceiling', sl: 'trading.bracket.leg.floor' },
}

/**
 * "take-profit" / "stop-loss" (a sell bracket), "ceiling" / "floor" (a range
 * alert): the leg word, lowercase, as the engine's `leg_word`.
 */
export function legWord(leg: BracketLeg, kind: BracketKind = 'sell'): string {
  return t((LEG_KEYS[kind] ?? LEG_KEYS.sell)[leg])
}

/** "Take-profit" / "Stop-loss" / "Ceiling" / "Floor": the leg word at the head of a phrase. */
export function legTitle(leg: BracketLeg, kind: BracketKind = 'sell'): string {
  const word = legWord(leg, kind)
  return word.charAt(0).toUpperCase() + word.slice(1)
}

/** The leg object for its word. */
export function bracketLeg(b: Pick<Bracket, 'takeProfit' | 'stopLoss'>, leg: BracketLeg): Trigger {
  return leg === 'tp' ? b.takeProfit : b.stopLoss
}

/** "+20 %" / "−9.8 %" / "0 %": one move left to a line; '' when unknown. */
function moveText(value: number | null | undefined, sign: '+' | '−'): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ''
  if (value === 0) return '0 %'
  return `${sign}${Number(Math.abs(value).toFixed(1))} %`
}

/**
 * "+20 % / −10 %": the moves left to the take-profit and to the stop, as the
 * Missions row says them; '' when neither is known.
 */
export function movesText(b: Pick<Bracket, 'market'>): string {
  const up = moveText(b.market?.upsidePct, '+')
  const down = moveText(b.market?.downsidePct, '−')
  if (!up && !down) return ''
  return `${up || '—'} / ${down || '—'}`
}

/** "over $4,560": the take-profit line, the engine's label else built. */
export function takeProfitText(b: Pick<Bracket, 'lines'>): string {
  return b.lines.takeProfitLabel || `over ${priceText(b.lines.takeProfitUsd)}`
}

/** "under $3,420" / "10 % below peak": the stop line, the engine's label else built. */
export function stopLossText(b: Pick<Bracket, 'lines'>): string {
  if (b.lines.stopLossLabel) return b.lines.stopLossLabel
  if (b.lines.trailPct !== null) return `${pctText(b.lines.trailPct)} below peak`
  return `under ${priceText(b.lines.stopLossUsd)}`
}

/** "$3,420 – $4,560": the range between the two lines. */
export function rangeText(b: Pick<Bracket, 'lines'>): string {
  return `${priceText(b.lines.stopLossUsd)} – ${priceText(b.lines.takeProfitUsd)}`
}

/** The size alone: "100 %", "0.05 ETH", "$100"; '' for an alert. */
export function bracketSizeOnly(b: Pick<Bracket, 'action' | 'token'>): string {
  const a = b.action
  if (a.kind === 'alert') return ''
  if (a.amountPct !== null) return pctText(a.amountPct)
  if (a.amount) return `${formatAmount(a.amount.human)} ${b.token.symbol}`
  if (a.amountUsd !== null) return usdText(a.amountUsd)
  return ''
}

/**
 * "sell 100 % of ETH", "sell 50 % of ETH at take-profit, 100 % at stop",
 * "sell 0.05 ETH", "notify".
 */
export function bracketActionText(b: Pick<Bracket, 'action' | 'token'>): string {
  const a = b.action
  if (a.kind === 'alert') return 'notify'
  const size = bracketSizeOnly(b)
  const symbol = b.token.symbol
  if (a.tpPct !== null && a.amountPct !== null && a.tpPct < a.amountPct) {
    return `sell ${pctText(a.tpPct)} of ${symbol} at take-profit, ${pctText(a.amountPct)} at stop`
  }
  if (a.amount && a.amountPct === null) return `sell ${size}`
  return size ? `sell ${size} of ${symbol}` : `sell ${symbol}`
}

/**
 * The card's one sentence: "sell 100 % of ETH · take profit over $4,560 ·
 * stop under $3,420"; an alert: "notify when ETH is over $4,560 or under $3,420".
 */
export function bracketHeroText(b: Pick<Bracket, 'action' | 'token' | 'lines'>): string {
  if (b.action.kind === 'alert') {
    return `notify when ${b.token.symbol} is ${takeProfitText(b)} or ${stopLossText(b)}`
  }
  return `${bracketActionText(b)} · take profit ${takeProfitText(b)} · stop ${stopLossText(b)}`
}

/** "sell 100 % ETH · $3,420 – $4,560": a Missions row's plan, short; an alert: "ETH $3,420 – $4,560". */
export function bracketPlanText(b: Pick<Bracket, 'action' | 'token' | 'lines'>): string {
  if (b.action.kind === 'alert') return `${b.token.symbol} ${rangeText(b)}`
  const size = bracketSizeOnly(b)
  return [`sell${size ? ` ${size}` : ''} ${b.token.symbol}`, rangeText(b)].join(' · ')
}

/**
 * "100 % · ≈ $189", "50 % at take-profit, 100 % at stop", "—": the card's size
 * fact. The estimate is what the stop would move now (the whole size).
 */
export function bracketSizeText(b: Pick<Bracket, 'action' | 'token'>): string {
  const a = b.action
  if (a.kind === 'alert') return '—'
  if (a.tpPct !== null && a.amountPct !== null && a.tpPct < a.amountPct) {
    return `${pctText(a.tpPct)} at take-profit, ${pctText(a.amountPct)} at stop`
  }
  const size = bracketSizeOnly(b)
  if (!size) return '—'
  if (a.amountUsd !== null && a.amountPct === null && !a.amount) return size
  const est = a.estimatedUsd
  return est !== null && Number.isFinite(est) ? `${size} · ≈ ${usdText(est)}` : size
}

/** "2.1 : 1", "2.0 : 1": reward against risk, one decimal as the chat card says it; '' when unknown. */
export function rewardRiskText(b: Pick<Bracket, 'market'>): string {
  const rr = b.market?.rewardRisk
  return typeof rr === 'number' && Number.isFinite(rr) ? `${rr.toFixed(1)} : 1` : ''
}

/** The leg whose order is open (status `triggered`), or null. */
export function firingLeg(b: Pick<Bracket, 'takeProfit' | 'stopLoss'>): BracketLeg | null {
  if (b.takeProfit?.status === 'triggered') return 'tp'
  if (b.stopLoss?.status === 'triggered') return 'sl'
  return null
}

/** The armed leg furthest into its two checks, with its hits; null when none has a hit. */
function checkingLeg(
  b: Pick<Bracket, 'takeProfit' | 'stopLoss'>,
): { leg: BracketLeg; hits: number; need: number } | null {
  let best: { leg: BracketLeg; hits: number; need: number } | null = null
  for (const leg of ['tp', 'sl'] as const) {
    const tr = bracketLeg(b, leg)
    const hits = tr?.condition?.hits ?? 0
    if (tr?.status !== 'armed' || hits < 1) continue
    if (!best || hits > best.hits) best = { leg, hits, need: tr.condition?.confirmTicks ?? 2 }
  }
  return best
}

/**
 * What the bracket is doing, as its Missions row says it: "Awaiting
 * approval", "Armed · ETH $3,790 · +20 % / −10 %", "Take-profit · 1 of 2
 * checks", "Triggered · stop-loss order open", "Paused", "Done · take-profit".
 */
export function bracketWord(b: Bracket): string {
  const state = t(triggerStateKey(b))
  if (b.status === 'triggered') {
    const leg = firingLeg(b) ?? b.fired
    return leg
      ? `${state} · ${t('trading.bracket.word.legOrderOpen').replace('{leg}', legWord(leg, b.kind))}`
      : `${state} · ${t('trading.trigger.word.orderOpen')}`
  }
  if (b.status === 'done') return b.fired ? `${state} · ${legWord(b.fired, b.kind)}` : state
  if (b.status !== 'armed') return state
  const checking = checkingLeg(b)
  if (checking) {
    return `${legTitle(checking.leg, b.kind)} · ${t('trading.trigger.word.checks')
      .replace('{hits}', String(checking.hits))
      .replace('{need}', String(checking.need))}`
  }
  const price = b.market?.priceUsd ?? null
  if (price === null) return `${state} · ${t('trading.trigger.word.noPrice')}`
  return [state, `${b.token.symbol} ${priceText(price)}`, movesText(b)].filter(Boolean).join(' · ')
}

/** Why a bracket is where it is, for the row's tooltip; null when the engine says nothing. */
export function bracketReason(b: Pick<Bracket, 'statusReason'>): string | null {
  return triggerReason(b)
}

/** "Sell now" for a sell bracket, "Notify now" for a range alert. */
export function bracketFireKey(b: Pick<Bracket, 'kind'>): MessageKey {
  return b.kind === 'alert' ? 'trading.bracket.notifyNow' : 'trading.trigger.sellNow'
}

/**
 * The brackets a desk lists beside its missions. Every live one, in the
 * engine's order, whichever chat filed it (the row says where); then the
 * finished ones for an hour after they ended, newest first — this session's
 * and the unfiled ones (the operator's, every desk's).
 */
export function deskBrackets(all: readonly Bracket[], sessionKey: string, now: number): Bracket[] {
  const mine = all.filter((b) => !b.sessionKey || b.sessionKey === sessionKey)
  // Another chat's proposal is decided on its card; only what runs is listed here.
  const live = all.filter(
    (b) => LIVE.has(b.status) && (mine.includes(b) || b.status !== 'awaiting_approval'),
  )
  const finished = mine
    .filter((b) => {
      if (!TERMINAL.has(b.status)) return false
      const at = Date.parse(b.updatedAt)
      return Number.isFinite(at) && now - at < FINISHED_VISIBLE_MS
    })
    .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
  return [...live, ...finished]
}

/** Every live bracket, then at most FINISHED_ROWS finished ones; `more` counts the rest. */
export function bracketRows(
  brackets: readonly Bracket[],
  all = false,
): { rows: Bracket[]; more: number } {
  const live = brackets.filter((b) => !TERMINAL.has(b.status))
  const finished = brackets.filter((b) => TERMINAL.has(b.status))
  return {
    rows: [...live, ...(all ? finished : finished.slice(0, FINISHED_ROWS))],
    more: Math.max(0, finished.length - FINISHED_ROWS),
  }
}

/**
 * Both legs' warnings as the bracket's: a note both legs give (no price,
 * over the approval threshold, nothing to sell) once; a note only one leg
 * gives (its line already crossed) prefixed with that leg's word.
 */
export function bracketNotes(b: Bracket, walletName: string): string[] {
  const tp = b.takeProfit ? triggerNotes(b.takeProfit, walletName) : []
  const sl = b.stopLoss ? triggerNotes(b.stopLoss, walletName) : []
  const notes: string[] = []
  for (const note of tp) notes.push(sl.includes(note) ? note : `${legWord('tp', b.kind)}: ${note}`)
  for (const note of sl) if (!tp.includes(note)) notes.push(`${legWord('sl', b.kind)}: ${note}`)
  return notes
}

/** Both legs' fires, newest first, each tagged with its leg. */
export function bracketFires(
  b: Pick<Bracket, 'takeProfit' | 'stopLoss'>,
): (TriggerFire & { leg: BracketLeg })[] {
  const tagged = (['tp', 'sl'] as const).flatMap((leg) =>
    (bracketLeg(b, leg)?.fires ?? []).map((f) => ({ ...f, leg })),
  )
  return tagged.sort((x, y) => Date.parse(y.at) - Date.parse(x.at))
}

/** The fire a desk control just made: the answer's `fire`, else the newest manual one of either leg. */
export function answeredBracketFire(
  fire: TriggerFire | undefined,
  b: Pick<Bracket, 'takeProfit' | 'stopLoss'> | undefined,
): TriggerFire | undefined {
  if (fire) return fire
  if (!b) return undefined
  const newest = bracketFires(b)[0]
  return newest?.manual ? newest : undefined
}
