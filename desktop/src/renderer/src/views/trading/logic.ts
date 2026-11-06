import { keccak_256 } from '@noble/hashes/sha3.js'
import type { ChainId, Entry, Holding, Initiator, Order, OrderStatus, Token, Totals } from './types'
import { CHAINS } from './types'

/**
 * Every figure the trading terminal shows, as pure functions over the RPC
 * shapes: decimal-string arithmetic (never floats for token amounts),
 * formatting, sorting, tones, the quote countdown, the confirm rule.
 * Components render; this file decides.
 */

// ── Decimal strings ─────────────────────────────────────────────────────────

const AMOUNT_RE = /^(\d+)(?:\.(\d*))?$/

/** A typed amount, normalised: "1,5" → "1.5", ".5" → "0.5", "" → null, junk → null. */
/**
 * A decimal string in, a normalised decimal string out.
 *
 * Takes `unknown` on purpose. Every amount in the trading API is documented as
 * a decimal string, but this runs on whatever the engine actually sent, and a
 * single field arriving as a number once white-screened the whole desk mid-
 * swap. A wrong shape now renders as "—"; it does not take the app with it.
 */
export function parseAmount(input: unknown): string | null {
  if (typeof input === 'number') return Number.isFinite(input) ? parseAmount(String(input)) : null
  if (typeof input !== 'string') return null
  let s = input.trim().replace(/,/g, '.').replace(/\s+/g, '')
  if (s === '' || s === '.') return null
  if (s.startsWith('.')) s = `0${s}`
  const m = AMOUNT_RE.exec(s)
  if (!m) return null
  const whole = (m[1] ?? '0').replace(/^0+(?=\d)/, '')
  const frac = (m[2] ?? '').replace(/0+$/, '')
  if (whole === '0' && frac === '') return '0'
  return frac ? `${whole}.${frac}` : whole
}

export function isPositiveAmount(amount: string | null): boolean {
  return amount !== null && amount !== '0' && /[1-9]/.test(amount)
}

/** "1.5" with 6 decimals → 1500000n. Extra precision is truncated, never rounded up. */
export function toRaw(amount: string, decimals: number): bigint {
  const parsed = parseAmount(amount)
  if (parsed === null) return 0n
  const [whole, frac = ''] = parsed.split('.')
  const digits = (frac + '0'.repeat(decimals)).slice(0, decimals)
  return BigInt(whole + digits)
}

/** 1500000n with 6 decimals → "1.5". */
export function fromRaw(raw: bigint | string, decimals: number): string {
  const value = typeof raw === 'bigint' ? raw : BigInt(raw || '0')
  const negative = value < 0n
  const abs = negative ? -value : value
  const s = abs.toString().padStart(decimals + 1, '0')
  const whole = s.slice(0, s.length - decimals) || '0'
  const frac = decimals ? s.slice(s.length - decimals).replace(/0+$/, '') : ''
  const out = frac ? `${whole}.${frac}` : whole
  return negative ? `-${out}` : out
}

/** The share of a balance, as an amount string: pct 50 of "1.5" → "0.75". */
export function amountFromPct(balance: string, decimals: number, pct: number): string {
  const share = Math.max(0, Math.min(100, Math.round(pct)))
  const raw = (toRaw(balance, decimals) * BigInt(share)) / 100n
  return fromRaw(raw, decimals)
}

export function compareAmounts(a: string, b: string, decimals: number): number {
  const ra = toRaw(a, decimals)
  const rb = toRaw(b, decimals)
  return ra < rb ? -1 : ra > rb ? 1 : 0
}

// ── Formatting ──────────────────────────────────────────────────────────────

const usd2 = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})
const usdSmall = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumSignificantDigits: 4,
})
const usdCompact = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  // Pinned: V8 in Node 22 defaults the minimum to 1 here ("$131.0K"),
  // Node 24 to 0 ("$131K").
  minimumFractionDigits: 0,
  maximumFractionDigits: 1,
})

/** "$12,480.22"; tiny prices keep 4 significant digits; null → "—". */
export function formatUsd(
  value: number | null | undefined,
  opts: { signed?: boolean; compact?: boolean } = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  const body =
    opts.compact && abs >= 100_000
      ? usdCompact.format(abs)
      : abs > 0 && abs < 0.01
        ? usdSmall.format(abs)
        : usd2.format(abs)
  if (value < 0) return `−${body}`
  return opts.signed && value > 0 ? `+${body}` : body
}

/** "+3.2%" / "−0.4%" / "—". A cost basis of nearly zero makes the rate on it
 *  astronomical (and `toFixed` turns it into "1.19e+22%"), so past four
 *  digits the figure stops being a number and becomes ">9,999%". */
export function formatPct(
  value: number | null | undefined,
  opts: { signed?: boolean } = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : 2
  const body = abs >= 10_000 ? '>9,999%' : `${abs.toFixed(digits)}%`
  if (value < 0) return `−${body}`
  return opts.signed && value > 0 ? `+${body}` : body
}

/**
 * A token amount for a table cell: grouped, at most 6 decimals (8 under
 * 0.001), trailing zeros dropped, "0" for nothing. Works on the decimal
 * string directly so 18-decimal balances never pass through a float.
 */
export function formatAmount(amount: unknown, maxDecimals = 6): string {
  if (amount === null || amount === undefined) return '—'
  const parsed = parseAmount(amount)
  if (parsed === null) return '—'
  const [whole = '0', frac = ''] = parsed.split('.')
  const small = whole === '0'
  // Dust (≤ 1e-6) as scientific with three significant digits: "1.23e-7".
  if (small && frac !== '' && /^0{5}/.test(frac)) {
    const first = frac.search(/[1-9]/)
    if (first >= 5) {
      const digits = frac.slice(first, first + 3).padEnd(3, '0')
      const mantissa = `${digits[0]}.${digits.slice(1)}`.replace(/\.?0+$/, '')
      return `${mantissa}e-${first + 1}`
    }
  }
  const keep = small && frac.length > maxDecimals ? Math.max(maxDecimals, 8) : maxDecimals
  let cut = frac.slice(0, keep).replace(/0+$/, '')
  if (small && cut === '' && frac !== '') cut = '0'.repeat(keep - 1) + '1'
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return cut ? `${grouped}.${cut}` : grouped
}

const SUBSCRIPT_DIGITS = '₀₁₂₃₄₅₆₇₈₉'

function subscript(n: number): string {
  return String(n)
    .split('')
    .map((d) => SUBSCRIPT_DIGITS[Number(d)] ?? d)
    .join('')
}

/**
 * A token price for a cell. Under $0.001 it uses DexScreener's notation:
 * "$0.0₅1727" is 0.00000 1727 — the subscript counts the zeros after "0.",
 * then four significant digits. Everything else is `formatUsd`.
 */
export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs === 0 || abs >= 0.001) return formatUsd(value)
  // Exact decimal expansion, no exponent, enough digits for the significant part.
  const fixed = abs.toFixed(20)
  const frac = fixed.slice(2)
  const first = frac.search(/[1-9]/)
  if (first < 0) return formatUsd(value)
  const sig = frac.slice(first, first + 4).replace(/0+$/, '')
  const body = `$0.0${subscript(first)}${sig}`
  return value < 0 ? `−${body}` : body
}

/**
 * A USD figure for a table cell. Anything under a cent collapses to "<$0.01"
 * (the exact figure belongs in the cell's `title`), so dust never widens the
 * column. Zero and larger values format as `formatUsd`.
 */
export function formatUsdCell(
  value: number | null | undefined,
  opts: { signed?: boolean } = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs > 0 && abs < 0.01) {
    if (value < 0) return '−<$0.01'
    return opts.signed ? '+<$0.01' : '<$0.01'
  }
  return formatUsd(value, opts)
}

/**
 * A token amount for a narrow column: four significant digits under 1
 * ("0.0002064"), four decimals above it, dust in scientific notation.
 */
export function formatAmountCompact(amount: unknown): string {
  if (amount === null || amount === undefined) return '—'
  const parsed = parseAmount(amount)
  if (parsed === null) return '—'
  const [whole = '0', frac = ''] = parsed.split('.')
  if (whole !== '0') return formatAmount(parsed, 4)
  const first = frac.search(/[1-9]/)
  if (first < 0) return '0'
  if (first >= 5) return formatAmount(parsed)
  const kept = frac.slice(0, first + 4).replace(/0+$/, '')
  return `0.${kept}`
}

/** "0x1234…abcd". */
export function shortAddress(address: string, head = 6, tail = 4): string {
  if (!address) return ''
  if (address.length <= head + tail + 1) return address
  return `${address.slice(0, head)}…${address.slice(-tail)}`
}

export function shortHash(hash: string | null): string {
  return hash ? shortAddress(hash, 8, 6) : ''
}

/** The most of a token symbol a row shows; the rest belongs in a `title`. */
export const SYMBOL_MAX = 12

/**
 * A token symbol for a row: at most `max` characters, the tail replaced by
 * an ellipsis. Symbols are on-chain data anyone can mint, so a 200-character
 * one must not widen a leg out of its row, and an ellipsis marks the cut so
 * "USDC…" is never read as "USDC". Bidi isolation is the `.trd-sym` class's
 * job (see trading.css); this only bounds the length.
 */
export function clampSymbol(symbol: string, max = SYMBOL_MAX): string {
  const chars = Array.from(symbol)
  if (chars.length <= max) return symbol
  return `${chars.slice(0, Math.max(1, max - 1)).join('')}…`
}

export function chainName(chainId: number): string {
  return CHAINS.find((c) => c.id === chainId)?.name ?? `Chain ${chainId}`
}

export function chainShort(chainId: number): string {
  return CHAINS.find((c) => c.id === chainId)?.short ?? String(chainId)
}

/** Two tokens are the same asset: same chain, same address (case-insensitive). */
export function sameToken(
  a: Pick<Token, 'chainId' | 'address'> | null,
  b: Pick<Token, 'chainId' | 'address'> | null,
): boolean {
  if (!a || !b) return false
  return a.chainId === b.chainId && a.address.toLowerCase() === b.address.toLowerCase()
}

// ── Tones ───────────────────────────────────────────────────────────────────

export type PnlTone = 'up' | 'down' | 'flat'

export function pnlTone(value: number | null | undefined): PnlTone {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'flat'
  if (Math.abs(value) < 0.005) return 'flat'
  return value > 0 ? 'up' : 'down'
}

export type OrderTone = 'ok' | 'warn' | 'danger' | 'dim' | 'live'

export function orderTone(status: OrderStatus): OrderTone {
  switch (status) {
    case 'confirmed':
      return 'ok'
    case 'awaiting_approval':
      return 'warn'
    case 'failed':
    case 'rejected':
    case 'expired':
      return 'danger'
    case 'submitted':
    case 'approved':
      return 'live'
    case 'quoted':
      return 'dim'
  }
}

export function isAwaitingApproval(order: Pick<Order, 'status'>): boolean {
  return order.status === 'awaiting_approval'
}

export function isOrderLive(order: Pick<Order, 'status'>): boolean {
  return order.status === 'submitted' || order.status === 'approved'
}

// ── Sorting and grouping ────────────────────────────────────────────────────

export type HoldingSort = 'value' | 'pnl' | 'change' | 'symbol' | 'allocation'

/** Positions worth less than this are hidden by default, so airdrop dust and
 *  worthless tokens do not bury the book. The figure in the "n hidden" line is
 *  rendered from this constant, never written out, so the two cannot drift. */
export const DUST_USD = 0.1

export function splitDust(holdings: readonly Holding[]): { kept: Holding[]; dust: Holding[] } {
  const kept: Holding[] = []
  const dust: Holding[] = []
  for (const h of holdings) {
    // An unpriced position is not dust: we simply do not know.
    if (h.valueUsd !== null && h.valueUsd < DUST_USD) dust.push(h)
    else kept.push(h)
  }
  return { kept, dust }
}

export function sortHoldings(
  holdings: readonly Holding[],
  key: HoldingSort,
  dir: 'asc' | 'desc' = 'desc',
): Holding[] {
  const sign = dir === 'desc' ? -1 : 1
  const num = (v: number | null) => (v === null || !Number.isFinite(v) ? -Infinity : v)
  return [...holdings].sort((a, b) => {
    let d: number
    switch (key) {
      case 'symbol':
        d = a.token.symbol.localeCompare(b.token.symbol)
        break
      case 'pnl':
        d = num(a.unrealizedUsd) - num(b.unrealizedUsd)
        break
      case 'change':
        d = num(a.change24hPct) - num(b.change24hPct)
        break
      case 'allocation':
        d = a.allocationPct - b.allocationPct
        break
      case 'value':
      default:
        d = num(a.valueUsd) - num(b.valueUsd)
    }
    if (d === 0) d = a.token.symbol.localeCompare(b.token.symbol) * -sign
    return d * sign
  })
}

export interface AllocationSegment {
  /**
   * Unique per holding (chain, token address, wallet): a symbol is not — ETH
   * on Base in two wallets, or a junk token calling itself ETH, gave two
   * slices the same React key.
   */
  key: string
  symbol: string
  pct: number
}

/** Top slices of the pie for the stacked bar; the tail folds into "other". */
export function allocationSegments(holdings: readonly Holding[], max = 6): AllocationSegment[] {
  const sorted = [...holdings]
    .filter((h) => h.allocationPct > 0)
    .sort((a, b) => b.allocationPct - a.allocationPct)
  const head = sorted.slice(0, max)
  const rest = sorted.slice(max).reduce((sum, h) => sum + h.allocationPct, 0)
  const out = head.map((h) => ({
    key: `${h.chainId}:${h.token.address.toLowerCase()}:${h.wallet ?? ''}`,
    symbol: h.token.symbol,
    pct: h.allocationPct,
  }))
  if (rest > 0) out.push({ key: 'other', symbol: 'other', pct: rest })
  return out
}

export function filterHoldings(holdings: readonly Holding[], chainId: ChainId | null): Holding[] {
  return chainId === null ? [...holdings] : holdings.filter((h) => h.chainId === chainId)
}

/** Entries by local day, newest day first; entries within a day newest first. */
export function groupEntriesByDay(entries: readonly Entry[]): { day: string; entries: Entry[] }[] {
  const byDay = new Map<string, Entry[]>()
  for (const e of [...entries].sort((a, b) => b.ts - a.ts)) {
    const d = new Date(e.ts)
    const day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    const list = byDay.get(day)
    if (list) list.push(e)
    else byDay.set(day, [e])
  }
  return [...byDay.entries()].map(([day, list]) => ({ day, entries: list }))
}

export function initiatorKey(initiator: Initiator): 'you' | 'agent' | 'external' {
  return initiator === 'manual' ? 'you' : initiator
}

// ── Quote freshness and the confirm rule ───────────────────────────────────

export const QUOTE_REFRESH_MS = 15_000

export interface Countdown {
  /** Whole seconds left, never below 0. */
  seconds: number
  /** 1 fresh → 0 expired, for the ring. */
  fraction: number
  expired: boolean
}

/**
 * How long a price is still good for. The engine names the moment it stops
 * honouring a quote (`expiresAt`, ms); when it does, that is the expiry and
 * the ring runs from the fetch to it. Without one — an older engine, or a
 * stamp that is not after the fetch (clock skew) — the fetch plus the TTL
 * stands in. A fetch time of 0 is a placeholder and is already expired.
 */
export function quoteCountdown(
  fetchedAt: number,
  now: number,
  expiresAt: number | null | undefined = undefined,
  ttlMs = QUOTE_REFRESH_MS,
): Countdown {
  const engineExpiry =
    typeof expiresAt === 'number' && Number.isFinite(expiresAt) && expiresAt > fetchedAt
  const expiry = engineExpiry ? expiresAt : fetchedAt + ttlMs
  const span = engineExpiry ? expiresAt - fetchedAt : ttlMs
  const left = fetchedAt > 0 ? expiry - now : 0
  if (left <= 0) return { seconds: 0, fraction: 0, expired: true }
  return { seconds: Math.ceil(left / 1000), fraction: Math.min(1, left / span), expired: false }
}

/** Above this the confirm sheet asks the amount to be typed again. */
export const RETYPE_ABOVE_USD = 1000

export function needsRetype(valueUsd: number | null): boolean {
  return valueUsd !== null && Number.isFinite(valueUsd) && valueUsd > RETYPE_ABOVE_USD
}

/** The retyped amount matches when it parses to the same decimal string. */
export function retypeMatches(typed: string, amount: string): boolean {
  const a = parseAmount(typed)
  const b = parseAmount(amount)
  return a !== null && b !== null && a === b
}

/** Price impact above this is shown as a warning, above the second as danger. */
export function impactTone(pct: number | null): 'ok' | 'warn' | 'danger' {
  if (pct === null || !Number.isFinite(pct)) return 'ok'
  if (pct >= 5) return 'danger'
  if (pct >= 1) return 'warn'
  return 'ok'
}

/** Seconds until an approval lapses, for the countdown on its row. */
export function approvalSecondsLeft(order: Pick<Order, 'expiresAt'>, now: number): number | null {
  if (order.expiresAt === null) return null
  return Math.max(0, Math.ceil((order.expiresAt - now) / 1000))
}

export function formatClock(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

// ── Limits ─────────────────────────────────────────────────────────────────

/** How much of the day's agent budget is used, 0..1, with the remainder. */
export function capUsage(spent: number, cap: number): { fraction: number; leftUsd: number } {
  if (!Number.isFinite(cap) || cap <= 0) return { fraction: 0, leftUsd: 0 }
  const used = Math.max(0, spent)
  return { fraction: Math.min(1, used / cap), leftUsd: Math.max(0, cap - used) }
}

export const EMPTY_TOTALS: Totals = {
  valueUsd: 0,
  costUsd: 0,
  unrealizedUsd: 0,
  realizedUsd: 0,
  gasUsd: 0,
  change24hUsd: null,
  change24hPct: null,
}

/** Pending-approval count for a badge; capped so the pill never widens. */
export function badgeText(count: number): string {
  if (count <= 0) return ''
  return count > 9 ? '9+' : String(count)
}

/** Is this the wallet's own primary key? (Matched case-insensitively.) */
export function sameAddress(a: string | null | undefined, b: string | null | undefined): boolean {
  return Boolean(a && b) && String(a).toLowerCase() === String(b).toLowerCase()
}

/**
 * The active (primary) wallet first, the others in the vault's order. A copy,
 * because the input is React Query's cached list; Array.sort is stable.
 */
export function activeFirst<T extends { primary: boolean }>(wallets: readonly T[]): T[] {
  return [...wallets].sort((a, b) => Number(b.primary) - Number(a.primary))
}

/** A sane label for a wallet with none. */
export function walletLabel(wallet: { label: string; address: string }): string {
  return wallet.label.trim() || shortAddress(wallet.address)
}

/** The error text an RPC failure carries, for a toast. */
export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** The engine's error code ("trading.price_moved"), when the failure carries one. */
export function errorCode(err: unknown): string | null {
  if (err && typeof err === 'object' && 'code' in err) {
    const code = (err as { code?: unknown }).code
    return typeof code === 'string' && code ? code : null
  }
  return null
}

/** The engine refused to fill at the price the person confirmed. */
export const PRICE_MOVED = 'trading.price_moved'

// ── Addresses ───────────────────────────────────────────────────────────────

const HEX_ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

/**
 * EIP-55: the address with its letters cased by the keccak of its lowercase
 * hex. Null when the input is not 20 bytes of hex at all.
 */
export function checksumAddress(address: string): string | null {
  const trimmed = address.trim()
  if (!HEX_ADDRESS_RE.test(trimmed)) return null
  const lower = trimmed.slice(2).toLowerCase()
  const hash = keccak_256(new TextEncoder().encode(lower))
  let out = '0x'
  for (let i = 0; i < lower.length; i++) {
    const ch = lower[i] as string
    // One hex digit of the hash per character: the high nibble for even
    // positions, the low one for odd.
    const nibble = i % 2 === 0 ? (hash[i >> 1] as number) >> 4 : (hash[i >> 1] as number) & 0x0f
    out += /[a-f]/.test(ch) && nibble >= 8 ? ch.toUpperCase() : ch
  }
  return out
}

/**
 * A mixed-case address whose casing is not its checksum: almost always a
 * character mangled in copying, which a lowercase-only compare would let
 * through. All-lower and all-upper carry no checksum and pass.
 */
export function checksumMismatch(address: string): boolean {
  const trimmed = address.trim()
  if (!HEX_ADDRESS_RE.test(trimmed)) return false
  const body = trimmed.slice(2)
  if (body === body.toLowerCase() || body === body.toUpperCase()) return false
  return checksumAddress(trimmed) !== trimmed
}

// ── Gas reserve ─────────────────────────────────────────────────────────────

/**
 * What "Max" keeps back when the pay token is the chain's gas coin: a swap
 * that spends every wei cannot pay for itself. A fixed floor per chain; a
 * live quote's own fee estimate wins when it is larger.
 */
export const GAS_RESERVE_ETH: Record<number, string> = {
  8453: '0.0003',
  4663: '0.0003',
}

export function gasReserveEth(chainId: number, quoteGasEth?: string | null): string {
  const floor = GAS_RESERVE_ETH[chainId] ?? '0.0003'
  if (!quoteGasEth) return floor
  const parsed = parseAmount(quoteGasEth)
  if (parsed === null) return floor
  // Twice the quoted fee: gas prices move between the quote and the send.
  const doubled = fromRaw(toRaw(parsed, 18) * 2n, 18)
  return compareAmounts(doubled, floor, 18) > 0 ? doubled : floor
}

/** The most of a native balance a swap may spend: the balance less the reserve, never below zero. */
export function maxSpendable(balance: string, decimals: number, reserve: string | null): string {
  if (reserve === null) return parseAmount(balance) ?? '0'
  const raw = toRaw(balance, decimals) - toRaw(reserve, decimals)
  return fromRaw(raw < 0n ? 0n : raw, decimals)
}
