import { formatUsd } from './logic'
import type { LpPosition, LpStatus } from './types'

/**
 * Pure helpers for the Liquidity tab: what a row says about its range, and
 * how the table is narrowed. No React, no gateway.
 */

export type LpTone = 'ok' | 'warn' | 'dim'

export function lpStatusTone(status: LpStatus): LpTone {
  if (status === 'in-range' || status === 'full-range') return 'ok'
  return status === 'closed' ? 'dim' : 'warn'
}

/**
 * Where the pool's price sits inside the position's range, 0–1 on a log
 * scale (ticks are log-spaced, so the middle of the bar is the geometric
 * middle of the range). Out of range clamps to the nearer end. `null` when
 * the position has no range or the price is not known.
 */
export function rangeMarker(position: LpPosition): number | null {
  const lower = position.range?.priceLower
  const upper = position.range?.priceUpper
  const base = position.token.priceUsd
  const quote = position.quote.priceUsd
  if (!isPositive(lower) || !isPositive(upper) || !isPositive(base) || !isPositive(quote)) {
    return position.status === 'above-range' ? 1 : position.status === 'below-range' ? 0 : null
  }
  if (position.status === 'above-range') return 1
  if (position.status === 'below-range') return 0
  if (upper <= lower) return null
  const at = (Math.log(base / quote) - Math.log(lower)) / (Math.log(upper) - Math.log(lower))
  return Number.isFinite(at) ? Math.min(1, Math.max(0, at)) : null
}

function isPositive(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

/**
 * A rate (quote per base) at a glance: four significant figures, grouped
 * above a thousand, never an exponent a trader has to decode.
 */
export function formatRate(value: number | null | undefined): string {
  if (!isPositive(value)) return '—'
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`
  if (value >= 1000) return Math.round(value).toLocaleString('en-US')
  if (value >= 1) return String(Number(value.toPrecision(5)))
  if (value >= 0.0001) return String(Number(value.toPrecision(4)))
  // 0.0₅1234: the count of zeros, then the significant digits.
  const digits = value.toFixed(24).slice(2)
  const zeros = digits.search(/[1-9]/)
  if (zeros < 0) return '0'
  return `0.0${subscript(zeros)}${digits.slice(zeros, zeros + 4).replace(/0+$/, '')}`
}

// Three significant figures, not the one decimal of a compact balance: the
// two ends of a narrow range ($5.12M – $5.34M) must not print as one figure.
const mcapCompact = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  minimumFractionDigits: 0,
  maximumSignificantDigits: 3,
})

/**
 * A market cap at a glance: `$3.83M` from a hundred thousand up, whole
 * dollars under it (the cents of a market cap are noise).
 */
export function formatMcap(value: number | null | undefined): string {
  if (!isPositive(value)) return '—'
  if (value >= 100_000) return mcapCompact.format(value)
  if (value < 1) return formatUsd(value)
  return `$${Math.round(value).toLocaleString('en-US')}`
}

/**
 * The market caps (USD) of the base token between which a position provides
 * liquidity: `$2.1M – $9.8M`. `null` when the position has no range (a pair)
 * or the engine could not price a bound (no supply, no quote price).
 */
export function mcapRange(position: LpPosition): string | null {
  const lower = position.range?.mcapLower
  const upper = position.range?.mcapUpper
  if (!isPositive(lower) || !isPositive(upper)) return null
  return `${formatMcap(Math.min(lower, upper))} – ${formatMcap(Math.max(lower, upper))}`
}

const SUBSCRIPTS = '₀₁₂₃₄₅₆₇₈₉'

function subscript(n: number): string {
  return String(n)
    .split('')
    .map((d) => SUBSCRIPTS[Number(d)] ?? d)
    .join('')
}

/** Rows of one protocol (`null`: every row), in the order the engine sent. */
export function filterByProtocol(
  positions: readonly LpPosition[],
  protocol: string | null,
): LpPosition[] {
  return protocol === null
    ? [...positions]
    : positions.filter((position) => position.protocol.key === protocol)
}

/** `#48213` for an NFT position; `null` for a pair, which has no id to show. */
export function positionTag(position: LpPosition): string | null {
  return position.tokenId ? `#${position.tokenId}` : null
}

/** A pool share as a percentage: `12.4%`, `<0.01%`; `null` when not a pair. */
export function formatShare(share: number | null | undefined): string | null {
  if (typeof share !== 'number' || !Number.isFinite(share) || share <= 0) return null
  const pct = share * 100
  if (pct < 0.01) return '<0.01%'
  return `${pct >= 10 ? pct.toFixed(1) : pct.toFixed(2)}%`
}

/** What the desk can do to a position from the table. */
export type LpAction = 'collect' | 'remove'

/**
 * The actions a row offers. Only Uniswap V4 positions have engine writes
 * (`trading.lp.collect`, `trading.lp.remove`); every other protocol is
 * listed, not managed. Collect needs fees to collect; remove takes the whole
 * position out (and its fees with it), so it is offered for any V4 row.
 */
export function lpActions(position: LpPosition): LpAction[] {
  if (position.protocol.key !== 'uniswap_v4' || !position.tokenId) return []
  const nonZero = (raw: string | undefined) => Boolean(raw && raw !== '0')
  const fees = nonZero(position.fees?.base.raw) || nonZero(position.fees?.quote.raw)
  return fees ? ['collect', 'remove'] : ['remove']
}

/** Whether a position has anything waiting to be claimed (fees or gauge rewards). */
export function hasClaimable(position: LpPosition): boolean {
  const nonZero = (raw: string | undefined) => Boolean(raw && raw !== '0')
  return (
    nonZero(position.fees?.base.raw) ||
    nonZero(position.fees?.quote.raw) ||
    nonZero(position.rewards?.amount.raw)
  )
}
