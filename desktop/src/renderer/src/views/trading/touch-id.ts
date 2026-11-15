import type { GateKind } from '~/lib/biometric-gate'
import { t, type MessageKey } from '~/i18n'
import { askRisk, HIGH_RISK_USD, orderKind, orderKindWord, orderLine } from './desk/desk-logic'
import { bracketHeroText } from './desk/bracket-logic'
import { heroText } from './desk/trigger-logic'
import { chainName, formatUsd, shortAddress } from './logic'
import type {
  Bracket,
  BracketPayload,
  Mandate,
  MandatePayload,
  Order,
  Trigger,
  TriggerPayload,
} from './types'

/**
 * What the Touch ID sheet says, and which gate kind applies, for each action
 * the gate stands in front of. The reason names the action, so the person at
 * the sensor knows what the fingerprint approves.
 */
export interface TouchIdAsk {
  kind: Extract<GateKind, 'approve' | 'approve-high'>
  reason: string
}

function fill(text: string, values: Record<string, string>): string {
  return text.replace(/\{(\w+)\}/g, (whole, key: string) => values[key] ?? whole)
}

/**
 * An order's approval: the card's own risk (the batch total for a
 * multisend), and "approve 0.05 ETH → USDC on Base". A leg of a batch whose
 * other legs are not known is treated as high-risk: the engine approves
 * every leg at once, and the part in hand cannot say what the whole is worth.
 */
export function orderTouchId(order: Order, legs?: readonly Order[]): TouchIdAsk {
  const orders = legs && legs.length ? [...legs] : [order]
  const batchUnknown = Boolean(order.batchId) && !(legs && legs.length)
  let totalUsd: number | null = 0
  for (const o of orders) {
    if (o.valueUsd === null) {
      totalUsd = null
      break
    }
    totalUsd += o.valueUsd
  }
  const risk = askRisk({ kind: orderKind(order), lead: order, totalUsd })
  const word = orderKindWord(order, orders.length)
  const line = orderLine(order, orders, t('trading.send.count'))
  const what = word === 'swap' ? line : `${t(`trading.card.kind.${word}`).toLowerCase()} ${line}`
  return {
    kind: risk === 'high' || batchUnknown ? 'approve-high' : 'approve',
    reason: fill(t('trading.touchId.reason.order'), { what, chain: chainName(order.chainId) }),
  }
}

/**
 * What a mandate may spend at most: its USD cap, else its buys × USD per buy;
 * null when neither is known.
 */
export function mandateCeilingUsd(mandate: Pick<Mandate, 'budget' | 'runs'>): number | null {
  const positive = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n > 0
  const cap = mandate.budget?.capUsd
  if (positive(cap)) return cap
  const per = mandate.budget?.usdPerRun
  const max = mandate.runs?.max
  return positive(per) && positive(max) ? per * max : null
}

/** The writes on a mandate the fingerprint stands in front of. */
export type MandateTouchOp = 'approve' | 'run' | 'resume' | 'create' | 'update'

const MANDATE_REASON: Record<MandateTouchOp, MessageKey> = {
  approve: 'trading.touchId.reason.mandate',
  run: 'trading.touchId.reason.mandateRun',
  resume: 'trading.touchId.reason.mandateResume',
  create: 'trading.touchId.reason.mandateCreate',
  update: 'trading.touchId.reason.mandateUpdate',
}

/**
 * A DCA mandate's approval: a standing permission to spend, high-risk when
 * what it may spend reaches the card's high-risk line or is unknown. A
 * resume, an operator's own start (active at once) and an edit grant the
 * same permission again, so they are asked the same way. `run` is "Buy now":
 * one buy at once, sized by what one buy spends.
 */
export function mandateTouchId(mandate: Mandate, op: MandateTouchOp = 'approve'): TouchIdAsk {
  const ceiling =
    op === 'run' ? positiveOrNull(mandate.budget?.usdPerRun) : mandateCeilingUsd(mandate)
  return {
    kind: ceiling === null || ceiling >= HIGH_RISK_USD ? 'approve-high' : 'approve',
    reason: fill(t(MANDATE_REASON[op]), {
      name: `“${mandate.name}”`,
      cap: formatUsd(ceiling),
      per: formatUsd(ceiling),
      chain: mandate.chain?.name || chainName(mandate.chain?.id ?? 0),
    }),
  }
}

function positiveOrNull(n: unknown): number | null {
  return typeof n === 'number' && Number.isFinite(n) && n > 0 ? n : null
}

/** The writes on a trigger the fingerprint stands in front of. */
export type TriggerTouchOp = 'approve' | 'fire' | 'resume'

const TRIGGER_REASON: Record<TriggerTouchOp, MessageKey> = {
  approve: 'trading.touchId.reason.trigger',
  fire: 'trading.touchId.reason.triggerFire',
  resume: 'trading.touchId.reason.triggerResume',
}

/**
 * A price trigger's approval: a standing permission to trade when the
 * market says so. High-risk when what one fire moves reaches the card's
 * high-risk line or is unknown; an alert moves nothing. `fire` is "Fire
 * now", which trades at once; `resume` re-arms what was approved.
 */
export function triggerTouchId(trigger: Trigger, op: TriggerTouchOp = 'approve'): TouchIdAsk {
  const moves = trigger.kind !== 'alert'
  const est = trigger.action?.estimatedUsd ?? trigger.action?.amountUsd ?? null
  return {
    kind: moves && (est === null || est >= HIGH_RISK_USD) ? 'approve-high' : 'approve',
    reason: fill(t(TRIGGER_REASON[op]), {
      name: `“${trigger.name}”`,
      what: heroText(trigger),
      chain: trigger.chain?.name || chainName(trigger.chain?.id ?? 0),
    }),
  }
}

/** Reads one object for the sheet; null when it cannot be read. */
type ReadCall = (method: string, params: Record<string, unknown>) => Promise<unknown>

const TRIGGER_ID_REASON: Record<TriggerTouchOp, MessageKey> = {
  approve: 'trading.touchId.reason.triggerId',
  fire: 'trading.touchId.reason.triggerFireId',
  resume: 'trading.touchId.reason.triggerResumeId',
}

/**
 * The ask for a write on a trigger known only by its id (a chat card): the
 * trigger is read first so the sheet can name it. Unreadable → high-risk.
 */
export async function triggerAskById(
  call: ReadCall,
  triggerId: string,
  op: TriggerTouchOp = 'approve',
): Promise<TouchIdAsk> {
  let trigger: Trigger | null = null
  try {
    const res = (await call('trading.trigger.get', { triggerId })) as TriggerPayload | null
    trigger = res?.trigger ?? null
  } catch {
    /* unreadable: the high-risk ask below */
  }
  return trigger
    ? triggerTouchId(trigger, op)
    : { kind: 'approve-high', reason: fill(t(TRIGGER_ID_REASON[op]), { id: triggerId }) }
}

/** The writes on a bracket the fingerprint stands in front of. */
export type BracketTouchOp = 'approve' | 'fire' | 'resume'

const BRACKET_REASON: Record<BracketTouchOp, MessageKey> = {
  approve: 'trading.touchId.reason.bracket',
  fire: 'trading.touchId.reason.bracketFire',
  resume: 'trading.touchId.reason.bracketResume',
}

const BRACKET_ID_REASON: Record<BracketTouchOp, MessageKey> = {
  approve: 'trading.touchId.reason.bracketId',
  fire: 'trading.touchId.reason.bracketFireId',
  resume: 'trading.touchId.reason.bracketResumeId',
}

/**
 * A bracket's approval (docs/brackets.md): one standing permission for both
 * legs, so the ask names both lines and the size ("arm the bracket “Protect
 * ETH”: sell 100 % of ETH · take profit over $4,560 · stop under $3,420, on
 * Base"). High-risk when what the stop would move reaches the card's
 * high-risk line or is unknown; a range alert moves nothing. `fire` is the
 * desk's Sell now, which trades at once.
 */
export function bracketTouchId(bracket: Bracket, op: BracketTouchOp = 'approve'): TouchIdAsk {
  const moves = bracket.kind !== 'alert'
  const est = bracket.action?.estimatedUsd ?? bracket.action?.amountUsd ?? null
  return {
    kind: moves && (est === null || est >= HIGH_RISK_USD) ? 'approve-high' : 'approve',
    reason: fill(t(BRACKET_REASON[op]), {
      name: `“${bracket.name}”`,
      what: bracketHeroText(bracket),
      chain: bracket.chain?.name || chainName(bracket.chain?.id ?? 0),
    }),
  }
}

/**
 * The ask for a write on a bracket known only by its id (a chat card): the
 * bracket is read first so the sheet can name it. Unreadable → high-risk.
 */
export async function bracketAskById(
  call: ReadCall,
  bracketId: string,
  op: BracketTouchOp = 'approve',
): Promise<TouchIdAsk> {
  let bracket: Bracket | null = null
  try {
    const res = (await call('trading.bracket.get', { bracketId })) as BracketPayload | null
    bracket = res?.bracket ?? null
  } catch {
    /* unreadable: the high-risk ask below */
  }
  return bracket
    ? bracketTouchId(bracket, op)
    : { kind: 'approve-high', reason: fill(t(BRACKET_ID_REASON[op]), { id: bracketId }) }
}

/** "export the private key of Main (0x1234…abcd)" / "remove the wallet …". */
export function vaultReason(
  action: 'export' | 'exportKeystore' | 'remove',
  wallet: { address: string; label?: string | null },
): string {
  const short = shortAddress(wallet.address)
  const label = wallet.label?.trim()
  return fill(t(`trading.touchId.reason.${action}`), {
    wallet: label ? `${label} (${short})` : short,
  })
}

const MANDATE_ID_REASON: Record<Exclude<MandateTouchOp, 'create'>, MessageKey> = {
  approve: 'trading.touchId.reason.mandateId',
  run: 'trading.touchId.reason.mandateRunId',
  resume: 'trading.touchId.reason.mandateResumeId',
  update: 'trading.touchId.reason.mandateUpdateId',
}

/**
 * The ask for a write on a mandate known only by its id (a chat card): the
 * mandate is read first so the sheet can name it and the risk is the
 * card's. Unreadable → high-risk.
 */
export async function mandateAskById(
  call: ReadCall,
  mandateId: string,
  op: Exclude<MandateTouchOp, 'create'> = 'approve',
): Promise<TouchIdAsk> {
  let mandate: Mandate | null = null
  try {
    const res = (await call('trading.dca.get', { mandateId })) as MandatePayload | null
    mandate = res?.mandate ?? null
  } catch {
    /* unreadable: the high-risk ask below */
  }
  return mandate
    ? mandateTouchId(mandate, op)
    : { kind: 'approve-high', reason: fill(t(MANDATE_ID_REASON[op]), { id: mandateId }) }
}

/**
 * A swap or a send typed at the desk: it runs at once (a manual order never
 * parks), so with Touch ID on the fingerprint comes first. High-risk when
 * it moves the card's high-risk line or more, or its worth is unknown.
 */
export function manualTradeTouchId(
  op: 'swap' | 'send',
  trade: { what: string; chainId: number; valueUsd?: number | null },
): TouchIdAsk {
  const usd = trade.valueUsd
  const known = typeof usd === 'number' && Number.isFinite(usd)
  return {
    kind: !known || usd >= HIGH_RISK_USD ? 'approve-high' : 'approve',
    reason: fill(t(`trading.touchId.reason.${op}`), {
      what: trade.what,
      chain: chainName(trade.chainId),
    }),
  }
}
