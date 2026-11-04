import type { RawJob, RawRun } from '@/views/cron/logic'
import {
  chainName,
  chainShort,
  clampSymbol,
  compareAmounts,
  formatAmount,
  formatPct,
  formatPrice,
  formatUsd,
  fromRaw,
  sameAddress,
  shortAddress,
  toRaw,
} from '../logic'
import {
  CHAINS,
  isLpKind,
  NATIVE_ADDRESS,
  providerLabel,
  type LpPlan,
  type LpPlanAmount,
  type Order,
  type OrderKind,
  type Wallet,
} from '../types'
import { TRADING_AGENT_ID } from './agent'

/**
 * Pure decisions for the chat-centric desk: the strip's status word, the
 * approval card's facts, mission contracts and their cron payloads, the
 * BOOK's concession chain. Nothing here touches the DOM or the gateway, so
 * every rule the screen shows can be checked in a unit test.
 */

/* ── Status word ─────────────────────────────────────────────────────────── */

export type StatusWord = 'awaiting' | 'running' | 'idle'

/**
 * AWAITING beats RUNNING beats IDLE: a pending decision is the one thing to
 * see. A streaming reply earns no word: the chat itself shows it.
 */
export function statusWord(input: {
  pendingApprovals: number
  missionRunning: boolean
}): StatusWord {
  if (input.pendingApprovals > 0) return 'awaiting'
  if (input.missionRunning) return 'running'
  return 'idle'
}

/* ── Approval card ───────────────────────────────────────────────────────── */

export const HIGH_RISK_USD = 500
export const HIGH_RISK_IMPACT_PCT = 3

export type Risk = 'high' | 'normal'

/** Older engines send no `kind`; every order they know is a swap. */
export function orderKind(order: Pick<Order, 'kind'>): OrderKind {
  return order.kind ?? 'swap'
}

export function riskStamp(order: Pick<Order, 'valueUsd' | 'priceImpactPct' | 'kind'>): Risk {
  if (order.valueUsd !== null && order.valueUsd >= HIGH_RISK_USD) return 'high'
  if (order.priceImpactPct !== null && order.priceImpactPct >= HIGH_RISK_IMPACT_PCT) return 'high'
  // A send nobody could price is not "small": it is unknown, and gone once it mines.
  if (orderKind(order) === 'send' && order.valueUsd === null) return 'high'
  // Nor is a deposit into a pool nobody could price.
  if (orderKind(order) === 'lp_add' && order.valueUsd === null) return 'high'
  return 'normal'
}

/* ── Asks: one card per decision ─────────────────────────────────────────── */

/**
 * What one card decides: a swap, a send, a revoke — or every leg of a
 * multisend, which the engine approves and rejects as one. `lead` is the
 * first leg; its status, expiry and session stand for the batch.
 */
export interface Ask {
  key: string
  kind: OrderKind
  lead: Order
  orders: Order[]
  batch: boolean
  /** Sum of the legs' USD values; null when any leg is unpriced. */
  totalUsd: number | null
  /** Sum of the legs' amounts in the token, as a decimal string. */
  totalAmount: string
}

function sumUsd(orders: readonly Order[]): number | null {
  let total = 0
  for (const o of orders) {
    if (o.valueUsd === null) return null
    total += o.valueUsd
  }
  return total
}

/** Decimal-string sum of the legs' `amountIn`, in the token's own decimals. */
export function sumAmounts(orders: readonly Pick<Order, 'amountIn' | 'tokenIn'>[]): string {
  const decimals = orders[0]?.tokenIn.decimals ?? 18
  let total = 0n
  // A malformed amount parses to 0n and adds nothing.
  for (const o of orders) total += toRaw(o.amountIn, decimals)
  return fromRaw(total, decimals)
}

/** Group orders into asks, keeping first-seen order; legs of a batch fold into one. */
export function groupAsks(orders: readonly Order[]): Ask[] {
  const asks: Ask[] = []
  const byKey = new Map<string, Ask>()
  for (const order of orders) {
    const key = order.batchId || order.orderId
    const existing = byKey.get(key)
    if (existing) {
      existing.orders.push(order)
      continue
    }
    const ask: Ask = {
      key,
      kind: orderKind(order),
      lead: order,
      orders: [order],
      batch: Boolean(order.batchId),
      totalUsd: order.valueUsd,
      totalAmount: order.amountIn,
    }
    byKey.set(key, ask)
    asks.push(ask)
  }
  for (const ask of asks) {
    if (ask.orders.length > 1) {
      ask.totalUsd = sumUsd(ask.orders)
      ask.totalAmount = sumAmounts(ask.orders)
    }
  }
  return asks
}

export function askRisk(ask: Pick<Ask, 'kind' | 'lead' | 'totalUsd'>): Risk {
  if (ask.totalUsd !== null && ask.totalUsd >= HIGH_RISK_USD) return 'high'
  return riskStamp({ ...ask.lead, valueUsd: ask.totalUsd })
}

export function recipientDisplay(order: Pick<Order, 'recipient' | 'recipientLabel'>): string {
  const address = order.recipient ?? ''
  if (!address) return ''
  return order.recipientLabel ? `${order.recipientLabel} · ${shortAddress(address)}` : address
}

export interface Fact {
  key: string
  label: string
  value: string
  tone?: 'warn' | 'danger'
  /** An address: rendered on its own line, in full, never truncated. */
  wide?: boolean
  /** The value with every symbol unclamped, for the row's `title`; set only when a clamp cut something. */
  full?: string
}

/**
 * "0.2 ETH", the symbol clamped: `[shown, full]`, the second only when the
 * clamp changed something. Symbols are on-chain data; a fact row must not
 * grow to fit a 200-character one, and the whole belongs in a tooltip.
 */
function amountWithSymbol(amount: unknown, symbol: string): [string, string | undefined] {
  const short = clampSymbol(symbol)
  const shown = `${formatAmount(amount)} ${short}`
  return [shown, short === symbol ? undefined : `${formatAmount(amount)} ${symbol}`]
}

function utcOffset(d: Date): string {
  const offsetMin = -d.getTimezoneOffset()
  const sign = offsetMin >= 0 ? '+' : '−'
  const abs = Math.abs(offsetMin)
  const hh = Math.floor(abs / 60)
  const mm = abs % 60
  return mm ? `UTC${sign}${hh}:${String(mm).padStart(2, '0')}` : `UTC${sign}${hh}`
}

/** The engine owns expiry: print the moment whole, never a renderer countdown. */
export function formatExpiryWhole(ts: number, locale?: string): string {
  const d = new Date(ts)
  const when = new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(
    d,
  )
  return `${when} (${utcOffset(d)})`
}

/**
 * The expiry as the card prints it: the time alone when it falls today, the
 * short date and time otherwise — the year never, the offset always. The
 * whole form did not fit a card fact and was cut mid-word.
 */
export function formatExpiryShort(ts: number, now: number, locale?: string): string {
  const d = new Date(ts)
  const today = new Date(now)
  const sameDay =
    d.getFullYear() === today.getFullYear() &&
    d.getMonth() === today.getMonth() &&
    d.getDate() === today.getDate()
  const when = sameDay
    ? new Intl.DateTimeFormat(locale, { timeStyle: 'short' }).format(d)
    : new Intl.DateTimeFormat(locale, {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      }).format(d)
  return `${when} (${utcOffset(d)})`
}

/** The recipient (send) or spender (revoke) fact: the label, then the address in full. */
function addressFact(order: Pick<Order, 'recipient' | 'recipientLabel'>): string {
  const address = order.recipient ?? ''
  if (!address) return ''
  return order.recipientLabel ? `${order.recipientLabel} · ${address}` : address
}

export function walletDisplay(address: string, wallets: readonly Wallet[]): string {
  const w = wallets.find((x) => sameAddress(x.address, address))
  return w ? `${w.label} · ${shortAddress(w.address)}` : shortAddress(address)
}

/**
 * Bound facts for an approval card. Absent facts render no row: a "–" would
 * invite the reader to trust a value nobody measured.
 */
export function approvalFacts(
  order: Order,
  wallets: readonly Wallet[],
  labels: Record<string, string>,
  locale?: string,
  now: number = Date.now(),
): Fact[] {
  const facts: Fact[] = []
  const push = (
    key: string,
    value: string | null | undefined,
    tone?: Fact['tone'],
    wide?: boolean,
    full?: string,
  ) => {
    if (value === null || value === undefined || value === '') return
    facts.push({
      key,
      label: labels[key] ?? key,
      value,
      ...(tone ? { tone } : {}),
      ...(wide ? { wide } : {}),
      ...(full ? { full } : {}),
    })
  }
  const symIn = clampSymbol(order.tokenIn.symbol)
  const symOut = clampSymbol(order.tokenOut.symbol)
  push('wallet', walletDisplay(order.wallet, wallets))
  push('chain', chainName(order.chainId))
  const kind = orderKind(order)
  if (isLpKind(kind)) {
    for (const f of lpFacts(order, labels)) facts.push(f)
    const gas = order.gasUsd ?? order.plan?.gasUsd ?? null
    if (gas !== null) push('gas', formatUsd(gas))
    push('order', order.orderId)
    if (order.expiresAt) push('expires', formatExpiryShort(order.expiresAt, now, locale))
    return facts
  }
  if (kind === 'bridge') {
    for (const f of bridgeFacts(order, labels)) facts.push(f)
    if (order.valueUsd !== null) push('value', formatUsd(order.valueUsd))
    if (order.gasUsd !== null) push('gas', formatUsd(order.gasUsd))
    push('order', order.orderId)
    if (order.expiresAt) push('expires', formatExpiryShort(order.expiresAt, now, locale))
    return facts
  }
  if (kind === 'send') {
    // Where the money goes is the one fact that may never be shortened.
    push('to', addressFact(order), undefined, true)
    const [send, sendFull] = amountWithSymbol(order.amountIn, order.tokenIn.symbol)
    push('send', send, undefined, undefined, sendFull)
    if (order.valueUsd !== null) push('value', formatUsd(order.valueUsd))
    if (order.gasUsd !== null) push('gas', formatUsd(order.gasUsd))
    push('order', order.orderId)
    if (order.expiresAt) push('expires', formatExpiryShort(order.expiresAt, now, locale))
    return facts
  }
  if (kind === 'revoke') {
    push(
      'token',
      symIn || shortAddress(order.tokenIn.address),
      undefined,
      undefined,
      symIn === order.tokenIn.symbol ? undefined : order.tokenIn.symbol,
    )
    push('spender', addressFact(order), undefined, true)
    const [allowance, allowanceFull] = amountWithSymbol(order.amountIn, order.tokenIn.symbol)
    push(
      'allowance',
      order.amountIn === 'unlimited' ? (labels.unlimited ?? 'unlimited') : allowance,
      order.amountIn === 'unlimited' ? 'danger' : undefined,
      undefined,
      order.amountIn === 'unlimited' ? undefined : allowanceFull,
    )
    if (order.gasUsd !== null) push('gas', formatUsd(order.gasUsd))
    push('order', order.orderId)
    if (order.expiresAt) push('expires', formatExpiryShort(order.expiresAt, now, locale))
    return facts
  }
  const [pay, payFull] = amountWithSymbol(order.amountIn, order.tokenIn.symbol)
  push('pay', pay, undefined, undefined, payFull)
  if (order.expectedOut) {
    const [receive, receiveFull] = amountWithSymbol(order.expectedOut, order.tokenOut.symbol)
    push('receive', receive, undefined, undefined, receiveFull)
  }
  if (order.minOut) {
    const [minimum, minimumFull] = amountWithSymbol(order.minOut, order.tokenOut.symbol)
    push('minimum', minimum, undefined, undefined, minimumFull)
  }
  if (order.expectedOut) {
    const rate = Number(order.expectedOut) / Number(order.amountIn)
    if (Number.isFinite(rate) && rate > 0) {
      const shown = `1 ${symIn} ≈ ${formatAmount(String(rate), 6)} ${symOut}`
      const whole = `1 ${order.tokenIn.symbol} ≈ ${formatAmount(String(rate), 6)} ${order.tokenOut.symbol}`
      push('rate', shown, undefined, undefined, shown === whole ? undefined : whole)
    }
  }
  if (order.valueUsd !== null) push('value', formatUsd(order.valueUsd))
  if (order.priceImpactPct !== null) {
    push(
      'impact',
      formatPct(order.priceImpactPct),
      order.priceImpactPct >= HIGH_RISK_IMPACT_PCT
        ? 'danger'
        : order.priceImpactPct >= 1
          ? 'warn'
          : undefined,
    )
  }
  if (order.gasUsd !== null) push('gas', formatUsd(order.gasUsd))
  if (order.provider) push('provider', providerLabel(order.provider))
  push('order', order.orderId)
  if (order.expiresAt) push('expires', formatExpiryShort(order.expiresAt, now, locale))
  return facts
}

/**
 * Bound facts for a multisend card: the batch as a whole, not any one leg.
 * The recipients themselves are listed by the card, one line each.
 */
export function batchFacts(
  ask: Ask,
  wallets: readonly Wallet[],
  labels: Record<string, string>,
  locale?: string,
  now: number = Date.now(),
): Fact[] {
  const facts: Fact[] = []
  const push = (
    key: string,
    value: string | null | undefined,
    tone?: Fact['tone'],
    full?: string,
  ) => {
    if (value === null || value === undefined || value === '') return
    facts.push({
      key,
      label: labels[key] ?? key,
      value,
      ...(tone ? { tone } : {}),
      ...(full ? { full } : {}),
    })
  }
  const lead = ask.lead
  push('wallet', walletDisplay(lead.wallet, wallets))
  push('chain', chainName(lead.chainId))
  push('recipients', String(ask.orders.length))
  const [total, totalFull] = amountWithSymbol(ask.totalAmount, lead.tokenIn.symbol)
  push('total', total, undefined, totalFull)
  if (ask.totalUsd !== null) push('value', formatUsd(ask.totalUsd))
  const gas = ask.orders.reduce((s, o) => s + (o.gasUsd ?? 0), 0)
  if (gas > 0) push('gas', formatUsd(gas))
  push('batch', ask.key)
  if (lead.expiresAt) push('expires', formatExpiryShort(lead.expiresAt, now, locale))
  return facts
}

export function ordersForSession<T extends Pick<Order, 'sessionKey'>>(
  orders: readonly T[],
  sessionKey: string,
): T[] {
  return orders.filter((o) => o.sessionKey === sessionKey)
}

/**
 * The page of awaiting orders, with every batch replaced by its full set of
 * awaiting legs. The engine approves and rejects every awaiting leg of a
 * batch as one, so a card built from the legs that happened to land in the
 * page would show fewer recipients than Approve executes. A batch the
 * lookup has not answered yet keeps the page's legs meanwhile.
 */
export function withBatchLegs(
  orders: readonly Order[],
  batches: ReadonlyMap<string, readonly Order[]>,
): Order[] {
  const out: Order[] = []
  const done = new Set<string>()
  for (const order of orders) {
    const id = order.batchId
    if (!id) {
      out.push(order)
      continue
    }
    if (done.has(id)) continue
    done.add(id)
    const legs = batches.get(id)
    if (!legs) {
      out.push(...orders.filter((o) => o.batchId === id))
      continue
    }
    const awaiting = legs.filter((o) => o.status === 'awaiting_approval')
    out.push(...(awaiting.length ? awaiting : orders.filter((o) => o.batchId === id)))
  }
  return out
}

/** Every distinct batch id among these orders, in first-seen order. */
export function batchIdsOf(orders: readonly Pick<Order, 'batchId'>[]): string[] {
  const ids: string[] = []
  for (const o of orders) if (o.batchId && !ids.includes(o.batchId)) ids.push(o.batchId)
  return ids
}

/**
 * One line naming what an order moves, for a toast or a notification:
 * "0.2 ETH → USDC", "0.00001 ETH → 0x6c83…8312", "USDC ⛨ Permit2",
 * "0.25 ETH → 3 recipients", "0.01 ETH → Robinhood Chain". `legs` is every leg of a multisend.
 */
export function orderLine(
  order: Pick<
    Order,
    'kind' | 'amountIn' | 'tokenIn' | 'tokenOut' | 'recipient' | 'recipientLabel' | 'batchId'
  > &
    Partial<Pick<Order, 'plan' | 'valueUsd' | 'tokenId' | 'bridge'>>,
  legs?: readonly Pick<Order, 'amountIn' | 'tokenIn'>[],
  recipientsWord = 'recipients',
): string {
  const kind = orderKind(order)
  const symbol = order.tokenIn?.symbol ?? ''
  if (isLpKind(kind)) return lpOrderLine(order)
  if (kind === 'revoke') {
    const spender = order.recipientLabel || shortAddress(order.recipient ?? '')
    return `${symbol || shortAddress(order.tokenIn?.address ?? '')} ⛨ ${spender}`.trim()
  }
  if (kind === 'bridge') {
    // "0.01 ETH → Robinhood Chain": the asset keeps its name, the chain changes.
    const to = bridgeDestinationName(order) || order.tokenOut?.symbol || ''
    return `${formatAmount(order.amountIn)} ${symbol} → ${to}`.trim()
  }
  if (kind === 'send') {
    if (order.batchId && legs && legs.length > 1) {
      const total = sumAmounts(legs)
      return `${formatAmount(total)} ${symbol} → ${legs.length} ${recipientsWord}`
    }
    const to = order.recipientLabel || shortAddress(order.recipient ?? '')
    return `${formatAmount(order.amountIn)} ${symbol} → ${to}`.trim()
  }
  return `${formatAmount(order.amountIn)} ${symbol} → ${order.tokenOut?.symbol ?? ''}`.trim()
}

/** The word a toast or notification leads with: Swap, Send, Multisend, Revoke, Collect fees… */
export function orderKindWord(
  order: Pick<Order, 'kind' | 'batchId'>,
  legs = 1,
): OrderKind | 'multisend' {
  const kind = orderKind(order)
  if (kind === 'send' && order.batchId && legs > 1) return 'multisend'
  return kind
}

/** The engine's answer to a second decision on an order already decided. */
export function alreadyDecided(text: string): boolean {
  return /no longer awaiting approval/i.test(text)
}

/** The chat message a rejection leaves for the agent, so it can re-plan. */
export function rejectionMessage(
  order: Pick<Order, 'orderId' | 'batchId' | 'kind'>,
  reason: string,
  legs = 1,
): string {
  const clean = reason.trim()
  const what =
    order.batchId && legs > 1
      ? `batch ${order.batchId} (${legs} sends)`
      : isLpKind(orderKind(order))
        ? `${orderKind(order).replace('_', ' ')} order ${order.orderId}`
        : `${orderKind(order) === 'swap' ? 'order' : orderKind(order)} ${order.orderId}`
  return clean
    ? `Rejected ${what}: ${clean}`
    : `Rejected ${what}. Do not retry it without asking first.`
}

/* ── LP writes (docs/lp-write.md) ────────────────────────────────────────── */

/** Where the pool's price sits against the plan's range; null when the plan does not say. */
export type LpRangeStatus = 'in-range' | 'above-range' | 'below-range'

/**
 * In range when `tickLower <= tick < tickUpper` (V4's own rule). Outside it,
 * "above" means the price in quote per base is past the upper bound — which
 * is the high-tick side when the base token is currency0, the low-tick side
 * when it is currency1.
 */
export function lpRangeStatus(plan: LpPlan): LpRangeStatus | null {
  if (plan.status === 'in-range' || plan.status === 'above-range' || plan.status === 'below-range')
    return plan.status
  const tick = plan.pool?.tick
  const { tickLower, tickUpper } = plan.range ?? {}
  if (typeof tick !== 'number' || typeof tickLower !== 'number' || typeof tickUpper !== 'number')
    return null
  if (tick >= tickLower && tick < tickUpper) return 'in-range'
  const currency0 = plan.pool.poolKey?.currency0
  const baseIsCurrency1 = Boolean(
    currency0 && plan.token?.address && !sameAddress(currency0, plan.token.address),
  )
  const highTick = tick >= tickUpper
  return highTick !== baseIsCurrency1 ? 'above-range' : 'below-range'
}

/** A quote-per-base price without the dollar sign formatPrice gives it. */
function formatRatio(value: number | null): string {
  return formatPrice(value).replace(/^(−?)\$/, '$1')
}

/**
 * The range as the card prints it: market cap when both bounds are known
 * ("$2.1M – $9.8M mcap"), else the price in quote per base
 * ("0.0₈19 – 0.0₇154 WETH per PEPE").
 */
export function lpRangeText(plan: LpPlan, words: { mcap: string; per: string }): string {
  const r = plan.range
  if (!r) return ''
  if (typeof r.mcapLower === 'number' && typeof r.mcapUpper === 'number') {
    return `${formatUsd(r.mcapLower, { compact: true })} – ${formatUsd(r.mcapUpper, {
      compact: true,
    })} ${words.mcap}`
  }
  if (typeof r.priceLower !== 'number' || typeof r.priceUpper !== 'number') return ''
  return `${formatRatio(r.priceLower)} – ${formatRatio(r.priceUpper)} ${clampSymbol(
    plan.quote?.symbol ?? '',
  )} ${words.per} ${clampSymbol(plan.token?.symbol ?? '')}`.trim()
}

/** The position the order acts on: `#48213`, or null for a fresh mint. */
export function lpTokenId(order: Pick<Order, 'plan' | 'tokenId'>): string | null {
  return order.tokenId || order.plan?.tokenId || null
}

/** "PEPE / WETH": the plan's pair, else the order's two tokens. */
function lpPair(order: Pick<Order, 'plan' | 'tokenIn' | 'tokenOut'>): string {
  const base = order.plan?.token?.symbol ?? order.tokenIn?.symbol ?? ''
  const quote = order.plan?.quote?.symbol ?? order.tokenOut?.symbol ?? ''
  return `${clampSymbol(base)} / ${clampSymbol(quote)}`
}

/**
 * The card's position line: `#48213 · PEPE / WETH · Base · 1%`. A mint has
 * no id yet and leads with the pair.
 */
export function lpPositionLine(
  order: Pick<Order, 'plan' | 'tokenId' | 'tokenIn' | 'tokenOut' | 'chainId'>,
  newPosition: string,
): string {
  const id = lpTokenId(order)
  return [
    id ? `#${id}` : newPosition,
    lpPair(order),
    chainShort(order.chainId),
    order.plan?.pool?.feePct ?? '',
  ]
    .filter(Boolean)
    .join(' · ')
}

/**
 * One line for a toast or a notification: "#48213 · PEPE / WETH",
 * "#48213 · 100% · PEPE / WETH", "PEPE / WETH · $50.00".
 */
export function lpOrderLine(
  order: Partial<Pick<Order, 'kind' | 'plan' | 'tokenId' | 'valueUsd' | 'tokenIn' | 'tokenOut'>>,
  /** Leave the add's dollar figure out (a row that shows the deposit says it there). */
  opts: { usd?: boolean } = {},
): string {
  const kind = order.kind
  const plan = order.plan ?? null
  const pair = lpPair({
    plan,
    tokenIn: order.tokenIn ?? ({ symbol: '' } as Order['tokenIn']),
    tokenOut: order.tokenOut ?? ({ symbol: '' } as Order['tokenOut']),
  })
  const id = order.tokenId || plan?.tokenId || null
  if (kind === 'lp_add') {
    const usd = opts.usd === false ? null : (order.valueUsd ?? plan?.expected?.usd ?? null)
    return [pair, usd !== null ? formatUsd(usd) : '', id ? `→ #${id}` : '']
      .filter(Boolean)
      .join(' · ')
  }
  const pct = kind === 'lp_remove' && typeof plan?.pct === 'number' ? `${plan.pct}%` : ''
  return [id ? `#${id}` : '', pct, pair].filter(Boolean).join(' · ')
}

/** "1,240,000 PEPE + 0.00184 WETH · $38.11": a zero side is left out unless both are. */
function lpAmounts(
  base: LpPlanAmount | null | undefined,
  baseSymbol: string,
  quote: LpPlanAmount | null | undefined,
  quoteSymbol: string,
  usd: number | null | undefined,
): [string, string | undefined] {
  const sides: [string, string][] = []
  const zero = (a: LpPlanAmount | null | undefined) => !a || !(Number(a.human) > 0)
  if (!zero(base) || (zero(base) && zero(quote)))
    sides.push([formatAmount(base?.human ?? '0'), baseSymbol])
  if (!zero(quote) || (zero(base) && zero(quote)))
    sides.push([formatAmount(quote?.human ?? '0'), quoteSymbol])
  const tail = usd !== null && usd !== undefined ? ` · ${formatUsd(usd)}` : ''
  const shown = sides.map(([a, sym]) => `${a} ${clampSymbol(sym)}`).join(' + ') + tail
  const whole = sides.map(([a, sym]) => `${a} ${sym}`).join(' + ') + tail
  return [shown, shown === whole ? undefined : whole]
}

/** A raw bound in the token's units, or null when it cannot be read. */
function rawHuman(raw: string | null | undefined, decimals: number): string | null {
  if (raw === null || raw === undefined || raw === '') return null
  try {
    return fromRaw(BigInt(raw), decimals)
  } catch {
    return null
  }
}

/** The slippage bounds as a line: both sides, or only the side that is not zero. */
function lpBounds(plan: LpPlan): [string, string | undefined] | null {
  const base = rawHuman(plan.bounds?.base, plan.token?.decimals ?? 18)
  const quote = rawHuman(plan.bounds?.quote, plan.quote?.decimals ?? 18)
  if (base === null && quote === null) return null
  const amount = (human: string | null): LpPlanAmount => ({
    raw: '',
    human: human ?? '0',
    usd: null,
  })
  return lpAmounts(amount(base), plan.token.symbol, amount(quote), plan.quote.symbol, null)
}

/** The destination chain's name on a bridge: the engine's, else our registry's, else the id. */
export function bridgeDestinationName(order: Pick<Order, 'bridge'>): string {
  const bridge = order.bridge
  if (!bridge) return ''
  return bridge.destinationChain?.name || chainName(bridge.destinationChainId)
}

/**
 * The bridge facts, in the order the card lists them: the two chains, what
 * leaves the origin, what Relay expects to land (what landed, once
 * confirmed), and what the route keeps. `chain` above them is the origin.
 */
function bridgeFacts(order: Order, labels: Record<string, string>): Fact[] {
  const bridge = order.bridge ?? null
  const facts: Fact[] = []
  const push = (key: string, pair: [string, string | undefined] | null) => {
    if (!pair || !pair[0]) return
    facts.push({
      key,
      label: labels[key] ?? key,
      value: pair[0],
      ...(pair[1] ? { full: pair[1] } : {}),
    })
  }
  push('fromChain', [chainName(bridge?.originChainId ?? order.chainId), undefined])
  push('toChain', [bridgeDestinationName(order), undefined])
  push('send', amountWithSymbol(order.amountIn, order.tokenIn.symbol))
  const confirmed = order.status === 'confirmed'
  const out = confirmed ? (order.receivedOut ?? order.expectedOut) : order.expectedOut
  if (out) {
    const [shown, full] = amountWithSymbol(out, order.tokenOut.symbol)
    push('receive', [
      confirmed ? shown : `≈ ${shown}`,
      full ? (confirmed ? full : `≈ ${full}`) : undefined,
    ])
  }
  if (bridge && bridge.feesUsd !== null && bridge.feesUsd !== undefined) {
    const parts = bridge.fees
      ? [
          ['gas', bridge.fees.gasUsd],
          ['relayer', bridge.fees.relayerUsd],
          ['app', bridge.fees.appUsd],
        ]
          .filter((p): p is [string, number] => typeof p[1] === 'number' && p[1] > 0)
          .map(([k, v]) => `${k} ${formatUsd(v)}`)
          .join(' · ')
      : ''
    push('bridgeFees', [formatUsd(bridge.feesUsd), parts || undefined])
  }
  return facts
}

/**
 * The LP facts, in the order the card lists them: what comes back (collect,
 * remove) or goes in (add), the fees a remove includes, the slippage bound,
 * the approvals an add will set first.
 */
function lpFacts(order: Order, labels: Record<string, string>): Fact[] {
  const plan = order.plan
  const facts: Fact[] = []
  const push = (key: string, pair: [string, string | undefined] | null, tone?: Fact['tone']) => {
    if (!pair || !pair[0]) return
    facts.push({
      key,
      label: labels[key] ?? key,
      value: pair[0],
      ...(tone ? { tone } : {}),
      ...(pair[1] ? { full: pair[1] } : {}),
    })
  }
  if (!plan) {
    if (order.valueUsd !== null) push('value', [formatUsd(order.valueUsd), undefined])
    return facts
  }
  const baseSym = plan.token?.symbol ?? order.tokenIn.symbol
  const quoteSym = plan.quote?.symbol ?? order.tokenOut.symbol
  const kind = orderKind(order)
  const confirmed = order.status === 'confirmed'
  if (kind === 'lp_add') {
    const spent = confirmed ? order.spent : null
    push(
      'lpDeposit',
      spent
        ? lpAmounts(spent.base, baseSym, spent.quote, quoteSym, null)
        : lpAmounts(
            plan.expected?.base,
            baseSym,
            plan.expected?.quote,
            quoteSym,
            plan.expected?.usd ?? order.valueUsd,
          ),
    )
    if (!confirmed) push('lpMaximum', lpBounds(plan))
    const needed = (plan.approvals ?? []).filter((a) => a.needed)
    if (!confirmed) {
      push('lpApprovals', [
        needed.length
          ? needed
              .map(
                (a) =>
                  `${clampSymbol(a.symbol)} ${a.step === 'erc20->permit2' ? '→ Permit2' : '→ PositionManager'}`,
              )
              .join(', ')
          : (labels.none ?? 'none'),
        undefined,
      ])
    }
  } else {
    const received = confirmed ? order.received : null
    push(
      'lpReceive',
      received
        ? lpAmounts(received.base, baseSym, received.quote, quoteSym, null)
        : lpAmounts(
            plan.expected?.base,
            baseSym,
            plan.expected?.quote,
            quoteSym,
            plan.expected?.usd ?? order.valueUsd,
          ),
    )
    if (kind === 'lp_remove' && plan.fees) {
      push('lpFees', lpAmounts(plan.fees.base, baseSym, plan.fees.quote, quoteSym, plan.fees.usd))
    }
    if (kind === 'lp_remove' && !confirmed) push('lpMinimum', lpBounds(plan))
  }
  if (kind !== 'lp_collect') {
    const slippage = order.slippagePct ?? plan.slippagePct
    if (typeof slippage === 'number') push('lpSlippage', [formatPct(slippage), undefined])
  }
  return facts
}

/**
 * What an LP order moves, for a list row — never `amountIn`, which on an LP
 * order is the liquidity (raw L), not a token amount. An add's deposit, a
 * collect's or remove's take: the receipt once confirmed, else the plan's
 * expectation with its USD. Null without a plan (an older engine's order).
 */
export function lpOrderAmounts(
  order: Pick<Order, 'kind' | 'plan' | 'status' | 'valueUsd' | 'tokenIn' | 'tokenOut'> &
    Partial<Pick<Order, 'spent' | 'received'>>,
): { side: 'deposit' | 'receive'; text: string; full?: string } | null {
  const plan = order.plan
  if (!plan) return null
  const baseSym = plan.token?.symbol ?? order.tokenIn.symbol
  const quoteSym = plan.quote?.symbol ?? order.tokenOut.symbol
  const side = orderKind(order) === 'lp_add' ? 'deposit' : 'receive'
  const moved =
    order.status === 'confirmed' ? (side === 'deposit' ? order.spent : order.received) : null
  const [text, full] = moved
    ? lpAmounts(moved.base, baseSym, moved.quote, quoteSym, null)
    : lpAmounts(
        plan.expected?.base,
        baseSym,
        plan.expected?.quote,
        quoteSym,
        plan.expected?.usd ?? order.valueUsd,
      )
  return full ? { side, text, full } : { side, text }
}

/** The hook the pool calls, or null for a hook-less pool. */
export function lpHook(plan: LpPlan | null | undefined): string | null {
  const hooks = plan?.pool?.hook || plan?.pool?.poolKey?.hooks
  if (!hooks || sameAddress(hooks, NATIVE_ADDRESS)) return null
  return hooks
}

export type LpStampKey = 'burns' | 'oneSided' | 'hook' | 'noFees'

/**
 * The warnings an LP card wears: a full remove burns the position NFT, a
 * one-sided add deposits one token only, a pool with a hook runs someone
 * else's code on every change of the position, and a collect with nothing
 * owed only pays gas.
 */
export function lpStamps(
  order: Pick<Order, 'kind' | 'plan'>,
): { key: LpStampKey; token?: string }[] {
  const plan = order.plan
  if (!plan) return []
  const kind = orderKind(order)
  const out: { key: LpStampKey; token?: string }[] = []
  if (kind === 'lp_remove' && (plan.burn === true || plan.pct === 100)) out.push({ key: 'burns' })
  if (kind === 'lp_add' && plan.oneSided) {
    const token = plan.oneSided === 'base' ? plan.token?.symbol : plan.quote?.symbol
    out.push({ key: 'oneSided', token: clampSymbol(token ?? '') })
  }
  if (kind === 'lp_collect' && lpNothingOwed(plan)) out.push({ key: 'noFees' })
  if (lpHook(plan)) out.push({ key: 'hook' })
  return out
}

/**
 * A collect's plan owes nothing: the fees are worth exactly $0, or both raw
 * sides are "0". Unknown fees (no figure at all) are not nothing.
 */
function lpNothingOwed(plan: LpPlan): boolean {
  const fees = plan.fees ?? plan.expected
  if (!fees) return false
  if (fees.usd === 0) return true
  return fees.base?.raw === '0' && fees.quote?.raw === '0'
}

/* ── Send prompt ─────────────────────────────────────────────────────────── */

export interface SendRecipientForm {
  address: string
  /** Own amount; empty means "the shared amount". */
  amount: string
}

export interface SendForm {
  chainId: number
  wallet: string | null
  /** Symbol or address, as typed or picked. */
  token: string
  recipients: SendRecipientForm[]
  /** Shared sizing for recipients without their own amount. */
  amount: string
  usd: string
  note: string
}

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/
const AMOUNT_RE = /^\d+(\.\d+)?$/

export type SendError = 'token' | 'recipients' | 'address' | 'amount' | 'duplicate'

export function validateSend(form: SendForm): { ok: boolean; error?: SendError; index?: number } {
  if (!form.token.trim()) return { ok: false, error: 'token' }
  const rows = form.recipients.filter((r) => r.address.trim() || r.amount.trim())
  if (rows.length === 0) return { ok: false, error: 'recipients' }
  const shared = form.amount.trim() || form.usd.trim()
  const seen = new Set<string>()
  for (const [index, row] of rows.entries()) {
    const address = row.address.trim()
    if (!ADDRESS_RE.test(address)) return { ok: false, error: 'address', index }
    if (seen.has(address.toLowerCase())) return { ok: false, error: 'duplicate', index }
    seen.add(address.toLowerCase())
    const own = row.amount.trim()
    if (own && !(AMOUNT_RE.test(own) && Number(own) > 0))
      return { ok: false, error: 'amount', index }
    if (!own && !shared) return { ok: false, error: 'amount', index }
  }
  if (form.amount.trim() && !(AMOUNT_RE.test(form.amount.trim()) && Number(form.amount) > 0))
    return { ok: false, error: 'amount' }
  // The same strict decimal as the amount: "1e4" and "0x10" are numbers to
  // Number(), and ten thousand dollars is not what someone who typed them meant.
  if (form.usd.trim() && !(AMOUNT_RE.test(form.usd.trim()) && Number(form.usd.trim()) > 0))
    return { ok: false, error: 'amount' }
  return { ok: true }
}

/** Lines pasted into the recipients box: "ADDR", "ADDR=AMOUNT", "ADDR,AMOUNT" or "ADDR AMOUNT". */
export function parseRecipientLines(text: string): SendRecipientForm[] {
  const out: SendRecipientForm[] = []
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.split('#', 1)[0]!.trim()
    if (!line) continue
    const m = /^(0x[0-9a-fA-F]{40})\s*(?:[=,\s]\s*(\S+))?$/.exec(line)
    if (m) out.push({ address: m[1]!, amount: m[2] ?? '' })
    else out.push({ address: line, amount: '' })
  }
  return out
}

export const SEND_TAG = '[Trading desk send]'

/**
 * The prompt a Send sheet posts into the chat when the desk is asked to do
 * it: exact addresses, exact amounts, the chain, and the rule that the
 * engine will park it for the user. Deterministic, so the preview is what
 * the agent reads.
 */
export function composeSendPrompt(form: SendForm, ctx: { wallets: readonly Wallet[] }): string {
  const rows = form.recipients.filter((r) => r.address.trim())
  const shared = form.amount.trim()
    ? `${form.amount.trim()} ${form.token.trim()}`
    : form.usd.trim()
      ? `${formatUsd(Number(form.usd))} worth of ${form.token.trim()}`
      : ''
  const lines: string[] = []
  lines.push(`${SEND_TAG} ${rows.length === 1 ? 'Send' : `Multisend to ${rows.length} recipients`}`)
  lines.push(`Chain: ${chainName(form.chainId)} · Token: ${form.token.trim()}`)
  lines.push(
    `From: ${form.wallet ? walletDisplay(form.wallet, ctx.wallets) : 'the primary wallet'}`,
  )
  lines.push('Recipients:')
  for (const row of rows) {
    const own = row.amount.trim()
    lines.push(`- ${row.address.trim()} → ${own ? `${own} ${form.token.trim()}` : shared}`)
  }
  if (form.note.trim()) lines.push(`Note: ${form.note.trim()}`)
  lines.push(
    'Rules: use `agentos trade send` once, with every recipient in that one command (it is one batch). ' +
      'Use exactly these addresses and amounts; do not resolve names or change anything. ' +
      'It will wait for my approval here; wait for it with `--wait --wait-seconds 600` and report each leg with its tx link.',
  )
  return lines.join('\n')
}

/* ── Burn ────────────────────────────────────────────────────────────────── */

/**
 * Where a burnt token goes: an address with no known key, so the supply is
 * stranded there for good. The zero address is **not** an alternative — the
 * engine refuses it (`trading.invalid`) — and no other address is used.
 */
export const BURN_ADDRESS = '0x000000000000000000000000000000000000dEaD'

export interface BurnForm {
  chainId: number
  wallet: string | null
  /** The token's address, picked from what the wallet actually holds. */
  token: string
  amount: string
  /** The symbol typed back by hand: the second half of the confirmation. */
  confirm: string
  note: string
}

/** What the paying wallet holds of the picked token, as the sheet knows it. */
export interface BurnHolding {
  symbol: string
  decimals: number
  /** Human units, as the balance reads. */
  amount: string
  native: boolean
  valueUsd: number | null
}

export type BurnError = 'token' | 'native' | 'amount' | 'balance' | 'confirm'

/**
 * A burn is refused for five reasons, in the order a person hits them: no
 * token picked, a native asset (ETH burnt is money destroyed, and no UI of
 * ours offers that), an unusable amount, more than the wallet holds, and
 * finally the symbol not typed back. The confirmation is checked last so
 * the box only turns red once everything else is right.
 */
export function validateBurn(
  form: BurnForm,
  held: BurnHolding | null,
): { ok: boolean; error?: BurnError } {
  if (!form.token.trim() || !held) return { ok: false, error: 'token' }
  if (held.native) return { ok: false, error: 'native' }
  const amount = form.amount.trim()
  if (!(AMOUNT_RE.test(amount) && Number(amount) > 0)) return { ok: false, error: 'amount' }
  if (compareAmounts(amount, held.amount, held.decimals) > 0) return { ok: false, error: 'balance' }
  // Case-insensitive, whitespace-trimmed: the point is that they read the
  // symbol and typed it, not that they matched its capitalisation.
  if (form.confirm.trim().toUpperCase() !== held.symbol.trim().toUpperCase())
    return { ok: false, error: 'confirm' }
  return { ok: true }
}

export const BURN_TAG = '[Trading desk burn]'

/**
 * The prompt the Burn sheet posts into the chat. It names the burn address
 * in full rather than leaving the agent to recall it, and it says the two
 * things an agent must not improvise around: one command, and no
 * substitution if the amount or the token turns out to be awkward.
 */
export function composeBurnPrompt(
  form: BurnForm,
  ctx: { wallets: readonly Wallet[]; held: BurnHolding | null },
): string {
  const symbol = ctx.held?.symbol ?? form.token.trim()
  const lines: string[] = []
  // The raw amount, never `formatAmount`: the agent passes this straight to
  // `--amount`, and a thousands separator is not a number to the CLI.
  lines.push(`${BURN_TAG} Burn ${form.amount.trim()} ${symbol}`)
  lines.push(`Chain: ${chainName(form.chainId)} · Token: ${form.token.trim()} (${symbol})`)
  lines.push(
    `From: ${form.wallet ? walletDisplay(form.wallet, ctx.wallets) : 'the primary wallet'}`,
  )
  lines.push(`To: ${BURN_ADDRESS} (the burn address)`)
  if (ctx.held?.valueUsd != null)
    lines.push(`Worth about ${formatUsd(ctx.held.valueUsd)} at the last price the desk read.`)
  if (form.note.trim()) lines.push(`Note: ${form.note.trim()}`)
  lines.push(
    'Rules: I have already read the plan and typed the symbol back, so do not re-ask me to confirm. ' +
      `Run \`agentos trade send\` exactly once, to ${BURN_ADDRESS} and to no other address, ` +
      'with exactly this token, this amount and this wallet. Do not round it, do not switch to a ' +
      'percentage, do not sell it instead, and do not burn anything else. Pass a --client-id so a ' +
      'retry cannot burn twice. It will wait for my approval here; wait with `--wait --wait-seconds 600` ' +
      'and report the tx link. If the transfer reverts, say so and stop — that token cannot be burnt.',
  )
  return lines.join('\n')
}

/* ── Missions ────────────────────────────────────────────────────────────── */

export type MissionKind = 'swap' | 'dca' | 'dip' | 'rebalance' | 'custom'

export type StopRule =
  | { kind: 'none' }
  | { kind: 'until'; until: string }
  | { kind: 'runs'; runs: number }
  | { kind: 'goal' }

export type Interval = { kind: 'every'; seconds: number } | { kind: 'cron'; expr: string }

export interface MissionForm {
  kind: MissionKind
  name: string
  goal: string
  wallets: string[]
  chains: number[]
  budgetTotalUsd: string
  budgetPerOrderUsd: string
  stop: StopRule
  interval: Interval
  dryRun: boolean
}

export const INTERVALS: readonly { seconds: number; label: string }[] = [
  { seconds: 60, label: '1 min' },
  { seconds: 300, label: '5 min' },
  { seconds: 900, label: '15 min' },
  { seconds: 3600, label: '1 hour' },
  { seconds: 14_400, label: '4 hours' },
  { seconds: 86_400, label: '1 day' },
]

/** What the four chips prefill. `swap` is a one-shot prompt, not a job. */
export function missionPrefill(
  kind: MissionKind,
  ctx: { primary: string | null; chains?: number[] },
): MissionForm {
  const wallets = ctx.primary ? [ctx.primary] : []
  const chains = ctx.chains?.length ? ctx.chains : [8453]
  const base: MissionForm = {
    kind,
    name: '',
    goal: '',
    wallets,
    chains,
    budgetTotalUsd: '100',
    budgetPerOrderUsd: '50',
    stop: { kind: 'none' },
    interval: { kind: 'every', seconds: 3600 },
    dryRun: true,
  }
  switch (kind) {
    case 'swap':
      return { ...base, name: 'Swap', goal: 'Swap 10 USDC to ETH on Base.', dryRun: false }
    case 'dca':
      return {
        ...base,
        name: 'DCA ETH',
        goal: 'Buy 10 USD of ETH with USDC on Base each run.',
        budgetTotalUsd: '300',
        budgetPerOrderUsd: '10',
        interval: { kind: 'every', seconds: 86_400 },
        stop: { kind: 'runs', runs: 30 },
      }
    case 'dip':
      return {
        ...base,
        name: 'Buy the dip',
        goal: 'If ETH is at or below 2,300 USD, buy 25 USD of ETH with USDC on Base; otherwise do nothing.',
        budgetTotalUsd: '100',
        budgetPerOrderUsd: '25',
        interval: { kind: 'every', seconds: 300 },
        stop: { kind: 'goal' },
      }
    case 'rebalance':
      return {
        ...base,
        name: 'Rebalance',
        goal: 'Keep the wallet at 70% ETH / 30% USDC by value on Base; trade only when a side drifts more than 5 points.',
        budgetTotalUsd: '500',
        budgetPerOrderUsd: '100',
        interval: { kind: 'every', seconds: 3600 },
      }
    default:
      return base
  }
}

export const MISSION_COMPLETE_MARKER = 'MISSION COMPLETE'
export const MISSION_TAG = '[Trading desk mission]'
const DRY_RUN_LINE = 'Dry run: this run, only quote and report. Do not swap.'

function usd(value: string): string {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? formatUsd(n) : ''
}

/**
 * The prompt a mission posts into the chat on every run. Deterministic, so
 * the preview in the contract modal is exactly what the agent will read.
 */
export function composeMissionPrompt(
  form: MissionForm,
  ctx: {
    wallets: readonly Wallet[]
    limits: { thresholdUsd: number; dailyCapUsd: number } | null
  },
): string {
  const lines: string[] = []
  lines.push(`${MISSION_TAG} ${form.name.trim() || 'Untitled'}`)
  lines.push(`Goal: ${form.goal.trim()}`)
  const wallets = form.wallets.map((a) => walletDisplay(a, ctx.wallets)).join(', ')
  const chains = form.chains.map((c) => chainName(c)).join(', ')
  lines.push(`Wallets: ${wallets || 'the primary wallet'} · Chains: ${chains || 'Base'}`)
  const total = usd(form.budgetTotalUsd)
  const per = usd(form.budgetPerOrderUsd)
  const budget: string[] = []
  if (total) budget.push(`at most ${total} in total for this mission`)
  if (per) budget.push(`at most ${per} per order`)
  if (budget.length) lines.push(`Budget: ${budget.join(' and ')}. Never exceed it.`)
  const budgeted = budget.length > 0
  if (ctx.limits) {
    lines.push(
      `Engine limits (not yours to change): orders above ${formatUsd(ctx.limits.thresholdUsd)} wait for approval; ${formatUsd(ctx.limits.dailyCapUsd)} per wallet per day.`,
    )
  }
  if (form.kind !== 'swap') {
    lines.push(
      form.interval.kind === 'every'
        ? `Cadence: this message arrives every ${intervalLabel(form.interval.seconds)} from a scheduled job.`
        : `Cadence: this message arrives on the schedule "${form.interval.expr}" from a scheduled job.`,
    )
  }
  const name = form.name.trim() || 'Untitled'
  lines.push(
    'Rules: use the wallet-trading skill (`agentos trade … --json`). Quote before you swap. ' +
      'If a swap needs approval, wait for it with `agentos trade order <id> --wait --wait-seconds 600 --json` and report the outcome. ' +
      'If nothing should be done this run, say so in one line. Always report order ids and explorer links. ' +
      'A bridge between Base and Robinhood Chain goes through `agentos trade bridge create` and always waits for the user. ' +
      (budgeted
        ? ' Before any order in a mission with a budget: `agentos trade orders --json`, sum `valueUsd` of `confirmed` orders ' +
          `whose \`note\` starts with \`${name}:\`; if that sum plus this order would exceed the total budget, do nothing and reply \`${MISSION_COMPLETE_MARKER}\`. ` +
          `Every order's \`--note\` starts with \`${name}:\`.`
        : ''),
  )
  const stop = stopLine(form.stop)
  if (stop) lines.push(stop)
  if (form.dryRun) lines.push(DRY_RUN_LINE)
  return lines.join('\n')
}

function stopLine(stop: StopRule): string {
  switch (stop.kind) {
    case 'until':
      return `Stop: after ${stop.until}. When that moment has passed, reply exactly "${MISSION_COMPLETE_MARKER}" and do nothing else.`
    case 'runs':
      return `Stop: after ${stop.runs} runs. Count the previous runs in this conversation; on the last one, end your reply with "${MISSION_COMPLETE_MARKER}".`
    case 'goal':
      return `Stop: when the goal is reached, end your reply with "${MISSION_COMPLETE_MARKER}".`
    default:
      return ''
  }
}

export function intervalLabel(seconds: number): string {
  const known = INTERVALS.find((i) => i.seconds === seconds)
  if (known) return known.label
  if (seconds % 86_400 === 0) return `${seconds / 86_400} days`
  if (seconds % 3600 === 0) return `${seconds / 3600} hours`
  if (seconds % 60 === 0) return `${seconds / 60} min`
  return `${seconds} s`
}

/** Strip the dry-run line once the first run has happened. */
export function withoutDryRun(text: string): string {
  return text
    .split('\n')
    .filter((line) => line.trim() !== DRY_RUN_LINE)
    .join('\n')
}

export function isDryRunText(text: string | undefined): boolean {
  return Boolean(text && text.split('\n').some((line) => line.trim() === DRY_RUN_LINE))
}

export type MissionError = 'name' | 'goal' | 'interval' | 'budget' | 'stop'

export interface MissionValidation {
  ok: boolean
  error?: MissionError
}

export function validateMission(form: MissionForm): MissionValidation {
  if (!form.name.trim()) return { ok: false, error: 'name' }
  if (!form.goal.trim()) return { ok: false, error: 'goal' }
  if (form.kind !== 'swap' && form.interval.kind === 'every' && form.interval.seconds < 60)
    return { ok: false, error: 'interval' }
  if (form.kind !== 'swap' && form.interval.kind === 'cron' && !form.interval.expr.trim())
    return { ok: false, error: 'interval' }
  for (const v of [form.budgetTotalUsd, form.budgetPerOrderUsd]) {
    if (v.trim() && !(Number(v) > 0)) return { ok: false, error: 'budget' }
  }
  if (form.stop.kind === 'runs' && !(form.stop.runs >= 1)) return { ok: false, error: 'stop' }
  if (form.stop.kind === 'until' && !form.stop.until.trim()) return { ok: false, error: 'stop' }
  return { ok: true }
}

/** The `cron.add` params for a mission that posts into this chat, run by the desk's agent. */
export function missionCronPayload(
  form: MissionForm,
  prompt: string,
  sessionKey: string,
  tz?: string,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    name: form.name.trim(),
    enabled: true,
    payloadKind: 'agent_turn',
    agentId: TRADING_AGENT_ID,
    sessionTarget: 'current',
    targetSessionKey: sessionKey,
    text: prompt,
    schedule:
      form.interval.kind === 'every'
        ? { kind: 'every', every_seconds: form.interval.seconds }
        : { kind: 'cron', expr: form.interval.expr.trim(), ...(tz ? { tz } : {}) },
  }
  if (tz) payload.tz = tz
  return payload
}

export function jobSessionKey(job: RawJob): string {
  return String(
    job.targetSessionKey || job.target_session_key || job.sessionKey || job.session_key || '',
  )
}

export function isSessionMission(job: RawJob, sessionKey: string): boolean {
  return Boolean(sessionKey) && jobSessionKey(job) === sessionKey
}

/**
 * Whether a desk lists the job: every mission of this session, and the
 * enabled missions of every other trading chat — a mission that keeps
 * trading after its chat was left must stay in view, with its chat named.
 * A mission is a trading chat's when the trading agent runs it; a job of
 * another agent's, or one with no chat to post into, is the Jobs panel's.
 */
export function isDeskMission(job: RawJob, sessionKey: string): boolean {
  if (isSessionMission(job, sessionKey)) return true
  if (job.enabled === false) return false
  const target = jobSessionKey(job)
  return Boolean(target) && target.startsWith(`agent:${TRADING_AGENT_ID}:`)
}

export type MissionState = 'running' | 'awaiting' | 'sleeping' | 'paused' | 'failed' | 'done'

export interface MissionStatus {
  state: MissionState
  /** Next wake, when the mission sleeps. */
  until: number | null
}

function toEpochMs(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = typeof value === 'number' ? value : Date.parse(String(value))
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 1e12 ? n * 1000 : n
}

/**
 * Whether the last run failed. `cron.list` never sends `last_status` — that
 * field only ever existed on the TypeScript type — so the truth is in the
 * counters the scheduler keeps: a success clears `last_error` and resets
 * `consecutive_errors`, a failure sets both and can push `status` to
 * `failed`. Reading `last_status` made this state unreachable, so a mission
 * that errored every run still read as sleeping.
 */
export function missionFailing(job: RawJob): boolean {
  const consecutive = Number(job.consecutive_errors ?? 0)
  if (Number.isFinite(consecutive) && consecutive > 0) return true
  if (job.lastResult) return true
  return String(job.status ?? '') === 'failed'
}

export function missionStatus(
  job: RawJob,
  ctx: { running: boolean; pendingApprovals: number },
): MissionStatus {
  if (ctx.running) return { state: 'running', until: null }
  if (ctx.pendingApprovals > 0) return { state: 'awaiting', until: null }
  const failing = missionFailing(job)
  if (job.enabled === false) {
    return { state: failing ? 'failed' : 'paused', until: null }
  }
  const next = toEpochMs(job.next_run)
  if (failing) return { state: 'failed', until: next }
  if (next === null) return { state: 'done', until: null }
  return { state: 'sleeping', until: next }
}

/** Whether a run's recorded text declares the mission finished. */
export function runSaysComplete(text: string | null | undefined): boolean {
  return Boolean(text && text.includes(MISSION_COMPLETE_MARKER))
}

/**
 * Whether the preview alone cannot settle whether a run finished the
 * mission. `cron.runs` caps `summary` at its first 500 characters, while the
 * marker is instructed to be the *last* thing the agent says — so a talkative
 * run hides it, and only `cron.runOutput` can answer.
 */
export function needsFullOutput(run: RawRun): boolean {
  return !runSaysComplete(run.summary) && Boolean(run.summaryTruncated)
}

export function jobText(job: RawJob): string {
  return String(job.message || job.prompt || '')
}

/** "$1,234.50" as the form writes it: "1234.5". Absent → "". */
function usdField(text: string): string {
  const n = Number(text.replace(/,/g, ''))
  return Number.isFinite(n) && n > 0 ? String(n) : ''
}

/**
 * The wallets a `Wallets:` line names, in order, resolved against the list.
 * Each entry is what `walletDisplay` wrote: `label · 0x1111…1111`, or just the
 * short address for a wallet the list did not know at the time.
 */
function walletsFromLine(part: string, wallets: readonly Wallet[]): string[] {
  const out: string[] = []
  for (const token of part.split(', ')) {
    const entry = token.trim()
    if (!entry) continue
    const sep = entry.lastIndexOf(' · ')
    const label = sep >= 0 ? entry.slice(0, sep) : entry
    const short = sep >= 0 ? entry.slice(sep + 3) : entry
    const hit =
      wallets.find((w) => shortAddress(w.address) === short) ??
      wallets.find((w) => sep >= 0 && w.label === label) ??
      wallets.find((w) => sep < 0 && w.label === label)
    if (hit && !out.some((a) => sameAddress(a, hit.address))) out.push(hit.address)
  }
  return out
}

export function stopFromLine(line: string | undefined): StopRule {
  if (!line) return { kind: 'none' }
  const runs = /^Stop: after (\d+) runs\./.exec(line)
  if (runs) return { kind: 'runs', runs: Number(runs[1]) }
  const until = /^Stop: after (.+?)\. When that moment has passed/.exec(line)
  if (until?.[1]) return { kind: 'until', until: until[1] }
  if (line.startsWith('Stop: when the goal is reached')) return { kind: 'goal' }
  return { kind: 'none' }
}

/** The stop rule a job's prompt carries, as the contract wrote it. */
export function missionStopRule(job: RawJob): StopRule {
  return stopFromLine(
    jobText(job)
      .split('\n')
      .find((l) => l.startsWith('Stop: ')),
  )
}

/**
 * When an "until" date lapses. The contract writes a bare day
 * (`2026-09-30`), and "after 2026-09-30" includes that whole local day, so
 * the moment is the start of the next one. Any other form is read as-is.
 */
export function untilEpoch(until: string): number | null {
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(until.trim())
  if (day) return new Date(Number(day[1]), Number(day[2]) - 1, Number(day[3]) + 1).getTime()
  const ts = Date.parse(until)
  return Number.isFinite(ts) ? ts : null
}

/**
 * Whether the scheduler should stop this mission now, by its own stop rule.
 * The rule is prose to the agent — "count the previous runs", "when that
 * moment has passed" — and the agent can miscount or forget the marker, so
 * the desk enforces the two rules it can measure: `run_count` against
 * "after N runs", the clock against "after <date>". A goal is the agent's
 * to judge.
 */
export function missionStopDue(job: RawJob, now: number): boolean {
  const stop = missionStopRule(job)
  if (stop.kind === 'runs') {
    const n = Number(job.run_count ?? Number.NaN)
    return Number.isFinite(n) && n >= stop.runs
  }
  if (stop.kind === 'until') {
    const ts = untilEpoch(stop.until)
    return ts !== null && now > ts
  }
  return false
}

/**
 * The form a job edits back into. Everything `composeMissionPrompt` wrote is
 * read back — goal, budget, wallets, chains, stop rule, dry run — so editing
 * a mission starts from what it is, not from the blank contract's defaults.
 */
export function missionFromJob(
  job: RawJob,
  primary: string | null,
  wallets: readonly Wallet[] = [],
): MissionForm {
  const form = missionPrefill('custom', { primary })
  form.name = String(job.name || '')
  const text = jobText(job)
  const lines = text.split('\n')
  const goal = lines.find((l) => l.startsWith('Goal: '))
  form.goal = goal ? goal.slice('Goal: '.length) : text
  form.dryRun = isDryRunText(text)

  const budget = lines.find((l) => l.startsWith('Budget: '))
  form.budgetTotalUsd = usdField(/at most \$([\d,.]+) in total/.exec(budget ?? '')?.[1] ?? '')
  form.budgetPerOrderUsd = usdField(/at most \$([\d,.]+) per order/.exec(budget ?? '')?.[1] ?? '')

  const scope = lines.find((l) => l.startsWith('Wallets: '))
  if (scope) {
    const cut = scope.lastIndexOf(' · Chains: ')
    const walletPart = (cut >= 0 ? scope.slice(0, cut) : scope).slice('Wallets: '.length)
    const chainPart = cut >= 0 ? scope.slice(cut + ' · Chains: '.length) : ''
    if (walletPart !== 'the primary wallet') {
      const named = walletsFromLine(walletPart, wallets)
      if (named.length) form.wallets = named
    }
    const chains: number[] = []
    for (const name of chainPart.split(', ')) {
      const id = CHAINS.find((c) => c.name === name.trim())?.id
      if (id !== undefined) chains.push(id)
    }
    if (chains.length) form.chains = chains
  }

  form.stop = stopFromLine(lines.find((l) => l.startsWith('Stop: ')))

  const kind = String(job.scheduleKind || job.schedule_kind || '')
  const raw = job.scheduleRaw ?? job.schedule_raw ?? job.expression ?? job.schedule
  if (kind === 'every' && Number(raw) > 0) form.interval = { kind: 'every', seconds: Number(raw) }
  else if (typeof raw === 'string' && raw.trim().split(/\s+/).length >= 5)
    form.interval = { kind: 'cron', expr: raw.trim() }
  return form
}

/* ── BOOK concession chain ───────────────────────────────────────────────── */

export const CHAT_MIN = 560
export const BOOK_MIN = 300
export const BOOK_MAX = 520
export const BOOK_DEFAULT = 360
export const BOOK_SPINE = 48

/**
 * Who yields when the frame narrows: the BOOK shrinks to its floor, then
 * collapses to the spine; the chat never drops below its floor. The stored
 * preference is never rewritten — widening restores it.
 *
 * `cramped` reports the case the caller cannot infer from `collapsed`: the
 * frame has no room for a split AT ALL, so flipping `open` changes nothing.
 * Without it the spine rendered an open button that ran its handler, set the
 * preference, and left the panel exactly where it was — a control that looked
 * broken because there was no way for it to say "not at this width".
 */
export function bookConcession(
  frameWidth: number,
  preferred: number,
  open: boolean,
): { book: number; collapsed: boolean; cramped: boolean } {
  const cramped = frameWidth - BOOK_MIN < CHAT_MIN
  if (!open) return { book: BOOK_SPINE, collapsed: true, cramped }
  const pref = Math.min(BOOK_MAX, Math.max(BOOK_MIN, preferred))
  if (frameWidth - pref >= CHAT_MIN) return { book: pref, collapsed: false, cramped }
  if (!cramped) return { book: BOOK_MIN, collapsed: false, cramped }
  return { book: BOOK_SPINE, collapsed: true, cramped }
}

/* ── Composer placeholder ────────────────────────────────────────────────── */

export const PLACEHOLDERS: readonly string[] = [
  'Swap 20 USDC to ETH on Base',
  'What is my portfolio worth right now?',
  'Sell 0.01 ETH for USDC on Base',
  'Sell half my ETH if it drops 5% today',
]

export function composerPlaceholder(input: {
  missionWord: string | null
  busy: boolean
  tick: number
  steering: string
}): string {
  if (input.busy) return input.steering
  if (input.missionWord) return input.missionWord
  return (
    PLACEHOLDERS[
      ((input.tick % PLACEHOLDERS.length) + PLACEHOLDERS.length) % PLACEHOLDERS.length
    ] ?? ''
  )
}

/* ── Project knowledge for the desk session ──────────────────────────────── */

export const TRADING_PROJECT_NAME = 'Trading desk'

export function tradingProjectKnowledge(ctx: {
  wallets: readonly Wallet[]
  chains: readonly number[]
  limits: { thresholdUsd: number; dailyCapUsd: number } | null
}): string {
  // The primary is the wallet orders come from; the others exist only so a
  // wallet the user names by label can be resolved. Listing them as peers
  // invited the agent to "check the other wallet too".
  const primary = ctx.wallets.find((w) => w.primary) ?? ctx.wallets[0] ?? null
  const others = ctx.wallets.filter((w) => w !== primary)
  const wallets = primary
    ? [
        `Primary wallet: ${primary.label} (${primary.address}). Every order comes from it unless the user names another wallet in this chat; do not read or report any other wallet's balance for an order.`,
        others.length
          ? `Other wallets, only when the user names them: ${others.map((w) => `${w.label} (${w.address})`).join('; ')}.`
          : '',
      ]
        .filter(Boolean)
        .join(' ')
    : 'Wallets: none yet.'
  const chains = (ctx.chains.length ? ctx.chains : CHAINS.map((c) => c.id))
    .map((c) => chainName(c))
    .join(', ')
  const limits = ctx.limits
    ? `Orders above ${formatUsd(ctx.limits.thresholdUsd)} wait for the user's approval; ${formatUsd(ctx.limits.dailyCapUsd)} per wallet per day.`
    : ''
  return [
    'You are at the trading desk of the AgentOS desktop app. The user can see wallets, holdings and orders beside this chat.',
    `${wallets} Chains: ${chains}.`,
    'Use the wallet-trading skill: `agentos wallet …` and `agentos trade … --json`. Prefer `--wait` on swaps and report the order id, status and explorer link. A bridge between the two chains goes through `agentos trade bridge create` and always waits for the user.',
    limits,
    'Never bypass an approval; if the user rejects an order they will say why in this chat.',
  ]
    .filter(Boolean)
    .join('\n')
}

/**
 * Keep a scrolling tab strip honest: `data-labels="off"` when the tabs with
 * their words do not fit (the icons stay, the title carries the word), and
 * `data-fade="start|end|both"` on the side(s) that hide tabs. Attributes are
 * set on the element directly (React renders neither), measured on resize
 * and scroll. Returns the cleanup.
 */
export function watchTabStrip(el: HTMLElement): () => void {
  const measure = () => {
    // With labels: do they fit? (A synchronous re-layout, no paint between.)
    const before = el.dataset.labels
    el.dataset.labels = 'on'
    const fits = el.scrollWidth <= el.clientWidth + 1
    el.dataset.labels = fits ? 'on' : 'off'
    // The strip changed shape, or it still overflows after a resize (520 →
    // 300 px with labels already off): the selected tab stays in view.
    if (before !== el.dataset.labels || el.scrollWidth > el.clientWidth + 1) {
      el.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView?.({
        block: 'nearest',
        inline: 'nearest',
      })
    }
    fade()
  }
  const fade = () => {
    const max = el.scrollWidth - el.clientWidth
    const start = el.scrollLeft > 1
    const end = max > 1 && el.scrollLeft < max - 1
    const side = start && end ? 'both' : start ? 'start' : end ? 'end' : ''
    if (side) el.dataset.fade = side
    else delete el.dataset.fade
  }
  measure()
  el.addEventListener('scroll', fade, { passive: true })
  const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
  ro?.observe(el)
  return () => {
    el.removeEventListener('scroll', fade)
    ro?.disconnect()
  }
}
