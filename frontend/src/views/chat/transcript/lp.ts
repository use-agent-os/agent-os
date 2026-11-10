// Chat transcript — Uniswap V4 liquidity cards.
//
// `agentos trade lp …` publishes a JSON artifact with the
// `application/vnd.agentos.lp+json` mime; the artifact renderer emits a mount
// placeholder for it and this module fetches the payload and draws one of four
// layouts into it, keyed by the payload's `kind`: pool, ranges (the liquidity
// distribution chart), position, positions. docs/lp-cards.md is the contract.
//
// Same two surfaces as cards.ts / chart.ts:
//   1. Pure helpers (top-level exports) — mime match, payload normalization,
//      number formatting, chart geometry. No DOM, no network.
//   2. `createLpMounter(deps)` — the imperative mounter the transcript composes
//      next to the chart and cards mounters.
//
// Styling: the markup carries `lp-card*` classes plus `data-lp-kind`,
// `data-lp-status`, `data-lp-chain` hooks. Only the desktop mounts this markup
// and skins the hooks in its own chat.css; the web console renders its own
// React cards (web/features/chat/cards/) from the pure helpers.
//
// SECURITY: every payload-derived string reaches the DOM through `textContent`
// or an attribute setter, never `innerHTML` — token symbols are attacker-chosen
// on a permissionless chain. Explorer links are built only from an http(s)
// explorer origin and a hex address, so a payload cannot smuggle `javascript:`.

import { t, tPlural } from '@/i18n'
import '@/i18n/en/chat'

import type { Artifact } from './artifacts'

/** The mime the engine publishes an LP read-out under. */
export const LP_ARTIFACT_MIME = 'application/vnd.agentos.lp+json'

/** How often the "2m ago" stamps are refreshed. */
export const LP_CLOCK_MS = 60_000

/** How long the copy button reads "copied". */
export const LP_COPIED_MS = 1_500

/** How long a failed write or refresh says why, under its button. */
export const LP_ERROR_MS = 4_000

/* ── Payload shape (docs/lp-cards.md) ───────────────────────────────────── */

export type LpKind = 'pool' | 'ranges' | 'position' | 'positions'
export type LpStatus = 'in-range' | 'above-range' | 'below-range' | 'closed' | 'unknown'

export interface LpChain {
  id: number
  key: string
  name: string
  /** http(s) origin without a trailing slash, or '' when the payload's is unusable. */
  explorer: string
}

export interface LpToken {
  address: string
  symbol: string
  decimals: number
  priceUsd: number | null
}

export interface LpAmount {
  raw: string
  human: string
  usd: number | null
}

export interface LpRange {
  tickLower: number
  tickUpper: number
  priceLower: number | null
  priceUpper: number | null
  mcapLower: number | null
  mcapUpper: number | null
}

export interface LpPool {
  poolId: string
  hook: string | null
  tickSpacing: number | null
  feePct: string
  tick: number | null
  liquidity: string
  priceUsd: number | null
  mcapUsd: number | null
  tvlUsd: number | null
}

export interface LpWallet {
  address: string
  label: string | null
  inApp: boolean
}

export interface LpPosition {
  chain: LpChain | null
  tokenId: string
  owner: LpWallet
  token: LpToken
  quote: LpToken
  pool: LpPool
  range: LpRange
  status: LpStatus
  liquidity: string
  principal: { base: LpAmount; quote: LpAmount; usd: number | null }
  fees: { base: LpAmount; quote: LpAmount; usd: number | null }
  valueUsd: number | null
  band: string | null
  distancePct: number | null
}

export interface LpTopRange extends LpRange {
  liquidity: string
  share: number
  owner: string | null
}

export interface LpSegment extends LpRange {
  liquidity: string
  share: number
  base: LpAmount
  quote: LpAmount
  active: boolean
}

/** The block one chain's reads were made against (multi-chain `positions`). */
export interface LpChainBlock {
  key: string
  block: number
}

/**
 * What produced the card, echoed by the engine so the card can re-run itself
 * (phase 2): `kind` names the read RPC (`trading.lp.<kind>`), `params` are its
 * params verbatim.
 */
export interface LpRequest {
  kind: LpKind
  params: Record<string, unknown>
}

interface LpEnvelope {
  version: number
  chain: LpChain | null
  asOfBlock: number
  /** Every scanned chain's block, by chain key; empty when the payload has none. */
  asOfBlocks: LpChainBlock[]
  fetchedAt: string
  partialScan: boolean
  warnings: string[]
  /** How to re-run this read; null when the payload does not say (no ↻ then). */
  request: LpRequest | null
}

export interface LpPoolPayload extends LpEnvelope {
  kind: 'pool'
  token: LpToken
  quote: LpToken
  pool: LpPool
  reserves: { base: LpAmount; quote: LpAmount }
  safety: {
    launcher: { name: string | null; address: string | null }
    locked: boolean | null
    note: string | null
  }
  topRanges: LpTopRange[]
}

export interface LpRangesPayload extends LpEnvelope {
  kind: 'ranges'
  token: LpToken
  quote: LpToken
  pool: LpPool
  current: { tick: number | null; priceUsd: number | null; mcapUsd: number | null }
  segments: LpSegment[]
  scan: { mode: string; scannedWords: number | null; fullWords: number | null; truncated: boolean }
}

export interface LpPositionPayload extends LpEnvelope {
  kind: 'position'
  position: LpPosition
  /**
   * Client-side only: the block the position was last read open at, set when
   * a later read found it burned. The card then says "last seen at block N".
   */
  lastSeenBlock?: number
}

export interface LpPositionsPayload extends LpEnvelope {
  kind: 'positions'
  wallets: LpWallet[]
  chains: LpChain[]
  positions: LpPosition[]
  totals: { valueUsd: number | null; feesUsd: number | null; count: number; outOfRange: number }
}

export type LpPayload = LpPoolPayload | LpRangesPayload | LpPositionPayload | LpPositionsPayload

/* ── Pure helpers: mime + normalization ─────────────────────────────────── */

/** True when the artifact should render as an LP card. */
export function isLpArtifact(artifact: Artifact | null | undefined): boolean {
  if (!artifact || !artifact.mime) return false
  return String(artifact.mime).toLowerCase().split(';')[0]?.trim() === LP_ARTIFACT_MIME
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

/** A finite number, or null. Numeric strings count: chain values travel as strings. */
function num(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

const ADDRESS = /^0x[0-9a-fA-F]{40}$/

/** An http(s) explorer origin without a trailing slash, or ''. */
export function safeExplorerBase(value: unknown): string {
  const raw = text(value)
  if (!raw) return ''
  try {
    const url = new URL(raw)
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return ''
    return `${url.origin}${url.pathname.replace(/\/+$/, '')}`
  } catch {
    return ''
  }
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

function normAmount(value: unknown): LpAmount {
  const row = obj(value) ?? {}
  return { raw: text(row.raw), human: text(row.human), usd: num(row.usd) }
}

function normRange(row: Obj): LpRange {
  return {
    tickLower: num(row.tickLower) ?? 0,
    tickUpper: num(row.tickUpper) ?? 0,
    priceLower: num(row.priceLower),
    priceUpper: num(row.priceUpper),
    mcapLower: num(row.mcapLower),
    mcapUpper: num(row.mcapUpper),
  }
}

function normPool(value: unknown): LpPool | null {
  const row = obj(value)
  if (!row) return null
  return {
    poolId: text(row.poolId),
    hook: textOrNull(row.hook),
    tickSpacing: num(row.tickSpacing),
    feePct: text(row.feePct),
    tick: num(row.tick),
    liquidity: text(row.liquidity),
    priceUsd: num(row.priceUsd),
    mcapUsd: num(row.mcapUsd),
    tvlUsd: num(row.tvlUsd),
  }
}

function normWallet(value: unknown): LpWallet | null {
  const row = obj(value)
  if (!row) return null
  const address = text(row.address)
  if (!address) return null
  return { address, label: textOrNull(row.label), inApp: row.inApp === true }
}

const STATUSES = new Set<LpStatus>(['in-range', 'above-range', 'below-range', 'closed'])

function normStatus(value: unknown): LpStatus {
  const raw = text(value).toLowerCase() as LpStatus
  return STATUSES.has(raw) ? raw : 'unknown'
}

function share(value: unknown): number {
  const n = num(value)
  if (n === null) return 0
  return Math.min(1, Math.max(0, n))
}

function normPosition(value: unknown, fallbackChain: LpChain | null): LpPosition | null {
  const row = obj(value)
  if (!row) return null
  const token = normToken(row.token)
  const quote = normToken(row.quote)
  const pool = normPool(row.pool)
  const owner = normWallet(row.owner) ?? { address: '', label: null, inApp: false }
  if (!token || !quote || !pool) return null
  const principal = obj(row.principal) ?? {}
  const fees = obj(row.fees) ?? {}
  return {
    chain: normChain(row.chain) ?? fallbackChain,
    tokenId: text(row.tokenId),
    owner,
    token,
    quote,
    pool,
    range: normRange(obj(row.range) ?? {}),
    status: normStatus(row.status),
    liquidity: text(row.liquidity),
    principal: {
      base: normAmount(principal.base),
      quote: normAmount(principal.quote),
      usd: num(principal.usd),
    },
    fees: { base: normAmount(fees.base), quote: normAmount(fees.quote), usd: num(fees.usd) },
    valueUsd: num(row.valueUsd),
    band: textOrNull(row.band),
    distancePct: num(row.distancePct),
  }
}

const KINDS = new Set<LpKind>(['pool', 'ranges', 'position', 'positions'])

/** The read to re-run, or null. Only the four read kinds pass: the method is built from it. */
export function normalizeLpRequest(value: unknown): LpRequest | null {
  const row = obj(value)
  if (!row) return null
  const kind = text(row.kind) as LpKind
  if (!KINDS.has(kind)) return null
  return { kind, params: { ...(obj(row.params) ?? {}) } }
}

function list<T>(value: unknown, fn: (item: unknown) => T | null): T[] {
  return Array.isArray(value) ? value.map(fn).filter((item): item is T => item !== null) : []
}

/**
 * Validate and normalize an artifact body into a renderable payload, or return
 * null when there is nothing to draw (unknown kind, missing core fields).
 */
export function normalizeLpPayload(raw: unknown): LpPayload | null {
  const body = obj(raw)
  if (!body) return null
  const kind = text(body.kind) as LpKind
  const chain = normChain(body.chain)
  const envelope: LpEnvelope = {
    version: num(body.version) ?? 1,
    chain,
    asOfBlock: Math.max(0, Math.trunc(num(body.asOfBlock) ?? 0)),
    asOfBlocks: Object.entries(obj(body.asOfBlocks) ?? {})
      .map(([key, value]) => ({ key: key.trim(), block: Math.trunc(num(value) ?? 0) }))
      .filter((row) => row.key !== '' && row.block > 0),
    fetchedAt: text(body.fetchedAt),
    partialScan: body.partialScan === true,
    warnings: list(body.warnings, (w) => text(w) || null),
    request: normalizeLpRequest(body.request),
  }

  if (kind === 'pool' || kind === 'ranges') {
    const token = normToken(body.token)
    const quote = normToken(body.quote)
    const pool = normPool(body.pool)
    if (!token || !quote || !pool) return null
    if (kind === 'pool') {
      const reserves = obj(body.reserves) ?? {}
      const safety = obj(body.safety) ?? {}
      const launcher = obj(safety.launcher) ?? {}
      return {
        ...envelope,
        kind,
        token,
        quote,
        pool,
        reserves: { base: normAmount(reserves.base), quote: normAmount(reserves.quote) },
        safety: {
          launcher: { name: textOrNull(launcher.name), address: textOrNull(launcher.address) },
          locked: typeof safety.locked === 'boolean' ? safety.locked : null,
          note: textOrNull(safety.note),
        },
        topRanges: list(body.topRanges, (item) => {
          const row = obj(item)
          if (!row) return null
          return {
            ...normRange(row),
            liquidity: text(row.liquidity),
            share: share(row.share),
            owner: textOrNull(row.owner),
          }
        }),
      }
    }
    const current = obj(body.current) ?? {}
    const scan = obj(body.scan) ?? {}
    return {
      ...envelope,
      kind,
      token,
      quote,
      pool,
      current: {
        tick: num(current.tick),
        priceUsd: num(current.priceUsd),
        mcapUsd: num(current.mcapUsd),
      },
      segments: list(body.segments, (item) => {
        const row = obj(item)
        if (!row) return null
        return {
          ...normRange(row),
          liquidity: text(row.liquidity),
          share: share(row.share),
          base: normAmount(row.base),
          quote: normAmount(row.quote),
          active: row.active === true,
        }
      }),
      scan: {
        mode: text(scan.mode),
        scannedWords: num(scan.scannedWords),
        fullWords: num(scan.fullWords),
        truncated: scan.truncated === true,
      },
    }
  }

  if (kind === 'position') {
    const position = normPosition(body.position, chain)
    if (!position) return null
    return { ...envelope, kind, chain: chain ?? position.chain, position }
  }

  if (kind === 'positions') {
    if (!Array.isArray(body.positions)) return null
    const positions = list(body.positions, (item) => normPosition(item, null))
    const totals = obj(body.totals) ?? {}
    return {
      ...envelope,
      kind,
      chain: null,
      wallets: list(body.wallets, normWallet),
      chains: list(body.chains, normChain),
      positions,
      totals: {
        valueUsd: num(totals.valueUsd),
        feesUsd: num(totals.feesUsd),
        count: num(totals.count) ?? positions.length,
        outOfRange:
          num(totals.outOfRange) ??
          positions.filter((p) => p.status === 'above-range' || p.status === 'below-range').length,
      },
    }
  }
  return null
}

/* ── Pure helpers: formatting ───────────────────────────────────────────── */

/** What an unknown USD value renders as — never "$0". */
export const NO_VALUE = '—'

const UNITS: Array<[number, string]> = [
  [1e12, 'T'],
  [1e9, 'B'],
  [1e6, 'M'],
  [1e3, 'K'],
]

/** Group with commas, `digits` fraction digits. */
function grouped(value: number, min: number, max: number): string {
  return value.toLocaleString('en-US', { minimumFractionDigits: min, maximumFractionDigits: max })
}

/** `n` to three significant digits, trailing zeros dropped: 1.2842 → "1.28", 2.1 → "2.1". */
function sig3(value: number): string {
  return String(Number(value.toPrecision(3)))
}

/** 1284.2 → "1.28K", 2_100_000 → "2.1M". Below 1,000 → null (caller decides). */
function compactMagnitude(abs: number): string | null {
  for (let i = 0; i < UNITS.length; i++) {
    const [size, suffix] = UNITS[i] as [number, string]
    if (abs < size) continue
    const scaled = Number((abs / size).toPrecision(3))
    // 999,950 rounds to "1000K": promote it to the next unit instead.
    if (scaled >= 1000 && i > 0) {
      const [bigger, biggerSuffix] = UNITS[i - 1] as [number, string]
      return `${sig3(abs / bigger)}${biggerSuffix}`
    }
    return `${sig3(abs / size)}${suffix}`
  }
  return null
}

/**
 * Compact USD for axes, TVL and market caps: $1.28K, $2.1M, $11.4M.
 * Null → "—"; the caller adds the "no price" hint.
 */
export function formatUsdCompact(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return NO_VALUE
  const sign = value < 0 ? '-' : ''
  const abs = Math.abs(value)
  const compact = compactMagnitude(abs)
  if (compact) return `${sign}$${compact}`
  return `${sign}${formatUsdSmall(abs)}`
}

function formatUsdSmall(abs: number): string {
  if (abs === 0) return '$0.00'
  if (abs < 0.01) return `$${formatSmall(abs)}`
  if (abs < 1) return `$${grouped(abs, 2, 4)}`
  return `$${grouped(abs, 2, 2)}`
}

/**
 * USD for a holding: exact cents below $10K ($1,284.20), compact above.
 * Sub-cent prices keep their significant digits ($0.0₅921).
 */
export function formatUsd(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return NO_VALUE
  const abs = Math.abs(value)
  if (abs >= 10_000) return formatUsdCompact(value)
  return `${value < 0 ? '-' : ''}${formatUsdSmall(abs)}`
}

const SUBSCRIPT = '₀₁₂₃₄₅₆₇₈₉'

/**
 * A tiny positive number with its leading zeros counted in a subscript, the way
 * DEX screens print meme-coin prices: 0.00000000123 → "0.0₈123".
 * Values from 0.0001 up print plainly with three significant digits.
 */
export function formatSmall(value: number, digits = 3): string {
  const abs = Math.abs(value)
  if (abs === 0) return '0'
  if (abs >= 1e-4) return grouped(Number(abs.toPrecision(digits)), 0, 10)
  const zeros = -Math.floor(Math.log10(abs)) - 1
  const mantissa = abs
    .toExponential(digits - 1)
    .replace(/e.*$/, '')
    .replace('.', '')
    .replace(/0+$/, '')
  const sub = String(zeros)
    .split('')
    .map((d) => SUBSCRIPT[Number(d)])
    .join('')
  return `0.0${sub}${mantissa || '0'}`
}

/** A quote-per-base price: plain with grouping above 1, subscript zeros below. */
export function formatPrice(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return NO_VALUE
  const abs = Math.abs(value)
  if (abs >= 1_000_000) return compactMagnitude(abs) ?? grouped(abs, 0, 0)
  if (abs >= 1) return grouped(Number(abs.toPrecision(4)), 0, 4)
  return formatSmall(abs)
}

/**
 * A token amount from its decimal string: 1240000 → "1,240,000", 0.212 → "0.212",
 * 12.3456 → "12.35", 22,367,000,000 → "22.4B". The string stays authoritative:
 * an unparsable one renders as given.
 */
export function formatTokenAmount(human: string): string {
  const value = Number(human)
  if (!human || !Number.isFinite(value)) return human || NO_VALUE
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  if (abs === 0) return '0'
  if (abs >= 1e9) return `${sign}${compactMagnitude(abs)}`
  if (abs >= 1000) return `${sign}${grouped(Math.round(abs), 0, 0)}`
  if (abs >= 1) return `${sign}${grouped(Number(abs.toPrecision(4)), 0, 4)}`
  return `${sign}${formatSmall(abs, 4)}`
}

/** A 0–1 fraction as a percentage with one decimal: 0.42 → "42.0%". */
export function formatShare(fraction: number): string {
  return `${(fraction * 100).toFixed(1)}%`
}

/** An already-percent number, signed, one decimal: 16.3 → "+16.3%". */
export function formatSignedPct(pct: number): string {
  const sign = pct > 0 ? '+' : pct < 0 ? '−' : ''
  return `${sign}${Math.abs(pct).toFixed(1)}%`
}

/** 0x7a3f…9e3f. Anything that is not an address is returned as-is. */
export function shortAddress(address: string): string {
  if (!address) return ''
  if (address.length <= 12) return address
  return `${address.slice(0, 6)}…${address.slice(-4)}`
}

/** "2m ago" from an ISO stamp, or '' when the stamp is unusable. */
export function relativeTime(fetchedAt: string, nowMs: number): string {
  const at = Date.parse(fetchedAt)
  if (!fetchedAt || Number.isNaN(at)) return ''
  const seconds = Math.max(0, (nowMs - at) / 1000)
  if (seconds < 60) return t('chat.lpAgoNow')
  if (seconds < 3600) return t('chat.lpAgoMinutes', { count: Math.floor(seconds / 60) })
  if (seconds < 86_400) return t('chat.lpAgoHours', { count: Math.floor(seconds / 3600) })
  return t('chat.lpAgoDays', { count: Math.floor(seconds / 86_400) })
}

/** `<explorer>/address/<addr>` or `/tx/<hash>`, or '' when either half is unusable. */
export function explorerUrl(
  chain: LpChain | null | undefined,
  kind: 'address' | 'tx',
  value: string,
): string {
  if (!chain || !chain.explorer) return ''
  const ok = kind === 'address' ? ADDRESS.test(value) : /^0x[0-9a-fA-F]{64}$/.test(value)
  return ok ? `${chain.explorer}/${kind}/${value}` : ''
}

/* ── Pure helpers: axis + chart geometry ────────────────────────────────── */

export type LpAxis = 'mcap' | 'price'

/**
 * Market cap when every range carries both bounds, else price. The contract
 * falls back when every mcap is null; a mixed payload also falls back, because
 * an mcap axis would have to drop the segments it cannot place.
 */
export function chooseAxis(ranges: LpRange[]): LpAxis {
  if (ranges.length === 0) return 'mcap'
  return ranges.every((r) => r.mcapLower !== null && r.mcapUpper !== null) ? 'mcap' : 'price'
}

export function rangeBounds(range: LpRange, axis: LpAxis): [number | null, number | null] {
  const a = axis === 'mcap' ? range.mcapLower : range.priceLower
  const b = axis === 'mcap' ? range.mcapUpper : range.priceUpper
  if (a !== null && b !== null && a > b) return [b, a]
  return [a, b]
}

export function formatAxisValue(value: number | null, axis: LpAxis): string {
  return axis === 'mcap' ? formatUsdCompact(value) : formatPrice(value)
}

export function formatRangeLabel(range: LpRange, axis: LpAxis): string {
  const [lo, hi] = rangeBounds(range, axis)
  return `${formatAxisValue(lo, axis)} – ${formatAxisValue(hi, axis)}`
}

/** Where `value` sits between `lo` and `hi` on a log scale (ticks are log-price). */
export function logFraction(value: number, lo: number, hi: number): number | null {
  if (!(value > 0) || !(lo > 0) || !(hi > 0) || lo === hi) return null
  return Math.log(value / lo) / Math.log(hi / lo)
}

/** Quote-per-base price from the two USD prices, when both are known. */
function quotePerBase(token: LpToken, quote: LpToken, baseUsd: number | null): number | null {
  const b = baseUsd ?? token.priceUsd
  if (b === null || quote.priceUsd === null || quote.priceUsd <= 0) return null
  return b / quote.priceUsd
}

export interface ChartBar {
  index: number
  segment: LpSegment
  /** Left edge and width in 0–100 of the plot width. */
  x: number
  width: number
  /** Bar height in 0–100 of the plot height (tallest segment = 100). */
  height: number
}

export interface ChartModel {
  axis: LpAxis
  bars: ChartBar[]
  /** Boundary labels: position (0–100) and formatted value. */
  ticks: Array<{ x: number; label: string }>
  /** The current-value marker, or null when it cannot be placed. */
  marker: { x: number; label: string; edge: 'left' | 'right' | null } | null
}

/**
 * Lay the distribution out on an ordinal axis: one equal-width slot per
 * segment, ordered by value. Equal slots rather than log-proportional widths
 * because V4 ranges span anything from one tick to the full curve — a single
 * full-range position would otherwise flatten every other bar to a sliver. The
 * boundary labels carry the real values.
 */
export function buildChartModel(payload: LpRangesPayload): ChartModel {
  const axis = chooseAxis(payload.segments)
  const ordered = payload.segments
    .map((segment, i) => ({ segment, i, lo: rangeBounds(segment, axis)[0] }))
    .sort((a, b) => {
      if (a.lo === null || b.lo === null) return a.i - b.i
      return a.lo - b.lo || a.i - b.i
    })
    .map((row) => row.segment)
  const n = ordered.length
  const slot = n > 0 ? 100 / n : 100
  const tallest = Math.max(0, ...ordered.map((s) => s.share))
  const bars: ChartBar[] = ordered.map((segment, index) => ({
    index,
    segment,
    x: index * slot,
    width: slot,
    height: tallest > 0 ? (segment.share / tallest) * 100 : 0,
  }))

  // Boundary labels, thinned to at most five so they cannot collide.
  const ticks: ChartModel['ticks'] = []
  if (n > 0) {
    const boundaries = ordered.map((s) => rangeBounds(s, axis)[0])
    boundaries.push(rangeBounds(ordered[n - 1] as LpSegment, axis)[1])
    const picks = new Set<number>()
    for (let k = 0; k <= 4; k++) picks.add(Math.round((k * n) / 4))
    for (const i of [...picks].sort((a, b) => a - b)) {
      ticks.push({ x: i * slot, label: formatAxisValue(boundaries[i] ?? null, axis) })
    }
  }

  return { axis, bars, ticks, marker: chartMarker(payload, ordered, axis, slot) }
}

function chartMarker(
  payload: LpRangesPayload,
  ordered: LpSegment[],
  axis: LpAxis,
  slot: number,
): ChartModel['marker'] {
  if (ordered.length === 0) return null
  const current =
    axis === 'mcap'
      ? payload.current.mcapUsd
      : quotePerBase(payload.token, payload.quote, payload.current.priceUsd)
  const label = `${t('chat.lpNow')} ${formatAxisValue(current, axis)}`

  const activeIndex = ordered.findIndex((s) => s.active)
  if (activeIndex >= 0) {
    const seg = ordered[activeIndex] as LpSegment
    const [lo, hi] = rangeBounds(seg, axis)
    let f = current !== null && lo !== null && hi !== null ? logFraction(current, lo, hi) : null
    if (f === null && payload.current.tick !== null && seg.tickUpper !== seg.tickLower) {
      f = (payload.current.tick - seg.tickLower) / (seg.tickUpper - seg.tickLower)
      // Price falls as the tick rises when the base token is currency1.
      if (descendsWithTick(ordered)) f = 1 - f
    }
    const x = (activeIndex + clamp01(f ?? 0.5)) * slot
    return { x, label, edge: null }
  }
  if (current === null) return null
  const first = rangeBounds(ordered[0] as LpSegment, axis)[0]
  const last = rangeBounds(ordered[ordered.length - 1] as LpSegment, axis)[1]
  if (first !== null && current < first) return { x: 0, label, edge: 'left' }
  if (last !== null && current > last) return { x: 100, label, edge: 'right' }
  // Inside the span but in no segment (a gap in a truncated scan): place by value.
  for (let i = 0; i < ordered.length; i++) {
    const [lo, hi] = rangeBounds(ordered[i] as LpSegment, axis)
    if (lo !== null && hi !== null && current >= lo && current <= hi) {
      return { x: (i + clamp01(logFraction(current, lo, hi) ?? 0.5)) * slot, label, edge: null }
    }
  }
  return null
}

function descendsWithTick(ordered: LpSegment[]): boolean {
  if (ordered.length < 2) return false
  return (ordered[0] as LpSegment).tickLower > (ordered[ordered.length - 1] as LpSegment).tickLower
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value))
}

/**
 * Where a position's range sits on its range bar, in % of the track. The
 * filled band IS the range; the track runs past it on both sides by half the
 * range's own log width, so a "now" a little outside still lands on the track.
 */
export const RANGE_BAND: [number, number] = [25, 75]

export interface RangeGeometry {
  axis: LpAxis
  /** The now marker, in % of the track (0–100). */
  x: number
  value: number | null
  /** Which side of the range "now" is on; null when inside (or unknown). */
  outside: 'above' | 'below' | null
  /** True when "now" lies past the track's end and the marker is pinned there. */
  pinned: boolean
}

/**
 * Where "now" sits on a position's range bar. Placed proportionally in
 * log-value space (ticks are log-price, so equal ratios get equal widths) while
 * it is within the track; past either end it is pinned to that end and flagged
 * `pinned`, so the label can say "below range" instead of pretending to scale.
 */
export function positionMarker(position: LpPosition): RangeGeometry {
  const axis = chooseAxis([position.range])
  const [lo, hi] = rangeBounds(position.range, axis)
  const value =
    axis === 'mcap'
      ? position.pool.mcapUsd
      : quotePerBase(position.token, position.quote, position.pool.priceUsd)
  const [left, right] = RANGE_BAND
  const span = right - left

  // 0 = the range's lower bound, 1 = its upper bound.
  let f: number | null = null
  if (value !== null && lo !== null && hi !== null) f = logFraction(value, lo, hi)
  if (f === null && position.pool.tick !== null) {
    const { tickLower, tickUpper } = position.range
    if (tickUpper !== tickLower) f = (position.pool.tick - tickLower) / (tickUpper - tickLower)
  }

  // The status is the engine's verdict; the geometry must agree with it.
  const verdict =
    position.status === 'above-range'
      ? 'above'
      : position.status === 'below-range'
        ? 'below'
        : position.status === 'in-range'
          ? 'inside'
          : null
  if (verdict === 'above' && (f === null || f <= 1)) f = 1.15
  else if (verdict === 'below' && (f === null || f >= 0)) f = -0.15
  else if (verdict === 'inside' || f === null) f = clamp01(f ?? 0.5)

  const outside = f > 1 ? 'above' : f < 0 ? 'below' : null
  const raw = left + f * span
  const pinned = raw < 0 || raw > 100
  return { axis, x: Math.min(100, Math.max(0, raw)), value, outside, pinned }
}

/** A position's out-of-range distance, signed by side: −72.6 below, +16.3 above. */
export function signedDistance(position: LpPosition): number | null {
  if (position.distancePct === null) return null
  const abs = Math.abs(position.distancePct)
  if (position.status === 'above-range') return abs
  if (position.status === 'below-range') return -abs
  return null
}

/* ── Pure helpers: measured label layout ────────────────────────────────── */
//
// The builders place every label in % so a card renders sensibly before it is
// measured (and in jsdom, which cannot measure). Once the card is in the
// document the mounter measures it and these decide, in px, which labels would
// touch and how to move them apart.

/** Card width (border box, px) from which a positions row fits on one line. */
export const LP_DENSE_MIN_PX = 760

/**
 * Card width (border box, px) below which a positions row needs three lines:
 * under it the status, pair and value cannot share one without the pair being
 * cut (the desk with its Book panel open is ~360px).
 */
export const LP_NARROW_MAX_PX = 440

/** Room kept between two labels on the same line. */
export const LP_LABEL_GAP_PX = 8

/**
 * `dense`: one line per position (status · pair · chain · owner · fees · value).
 * `stacked`: two lines — status · pair · value over chain · owner · fees. The
 * default (what an unmeasured card renders as).
 * `narrow`: three lines — status · value, pair · fees, chain · owner — and a
 * tighter stats strip, so nothing overlaps or is cut in a ~360px column.
 * The mounter stamps it as `.lp-card[data-lp-layout]`.
 */
export type LpLayout = 'dense' | 'stacked' | 'narrow'

export function lpLayoutFor(cardPx: number): LpLayout {
  if (cardPx >= LP_DENSE_MIN_PX) return 'dense'
  return cardPx < LP_NARROW_MAX_PX ? 'narrow' : 'stacked'
}

export type LabelAlign = 'start' | 'center' | 'end'

/** The [left, right] a label covers when hung from `anchor` with `align`. */
export function labelExtent(anchor: number, width: number, align: LabelAlign): [number, number] {
  if (align === 'start') return [anchor, anchor + width]
  if (align === 'end') return [anchor - width, anchor]
  return [anchor - width / 2, anchor + width / 2]
}

/**
 * How to hang a label from `anchor` so it stays inside [0, box]: centred when
 * it can be (it then points straight at its mark), else toward whichever side
 * has room. `fits` is false when no alignment keeps it inside; `align` is then
 * the one that overflows least.
 */
export function fitLabel(
  anchor: number,
  width: number,
  box: number,
): { align: LabelAlign; fits: boolean } {
  let best: { align: LabelAlign; over: number } | null = null
  for (const align of ['center', 'start', 'end'] as const) {
    const [a, b] = labelExtent(anchor, width, align)
    const over = Math.max(0, -a) + Math.max(0, b - box)
    if (over === 0) return { align, fits: true }
    if (!best || over < best.over) best = { align, over }
  }
  return { align: (best as { align: LabelAlign }).align, fits: false }
}

export interface RangeLabelInput {
  /** The track's width in px. */
  trackPx: number
  /** The now marker, in % of the track (RangeGeometry.x). */
  x: number
  lowerPx: number
  upperPx: number
  /** The whole "now" reading on one line. */
  nowPx: number
  /** The wider of its two lines once wrapped ("▲ now $2.51M" / "(+4.6%) above range"). */
  nowWrapPx: number
}

export interface RangeLabelLayout {
  /** `stacked` lifts the upper bound one line so the two bounds cannot touch. */
  bounds: 'inline' | 'stacked'
  nowAlign: LabelAlign
  /** Put the distance and side on a second line under the reading. */
  nowWrap: boolean
}

/**
 * Lay out a position range bar's labels for a measured track. The bounds hang
 * inward from the band's edges above the rule and "now" hangs from the pin
 * below it, so a bound and "now" never share a line; what can still collide
 * is the two bounds with each other (a narrow band) and "now" with the track's
 * ends (a long reading). The first lifts the upper bound a line; the second
 * re-aligns "now", and when even that is not enough stacks its distance and
 * side on a second line.
 */
export function layoutRangeLabels(input: RangeLabelInput): RangeLabelLayout {
  const { trackPx, x, lowerPx, upperPx, nowPx, nowWrapPx } = input
  const [left, right] = RANGE_BAND
  const lowerEnd = (trackPx * left) / 100 + lowerPx
  const upperStart = (trackPx * right) / 100 - upperPx
  const bounds = lowerEnd + LP_LABEL_GAP_PX > upperStart ? 'stacked' : 'inline'
  const anchor = (trackPx * x) / 100
  const one = fitLabel(anchor, nowPx, trackPx)
  if (one.fits) return { bounds, nowAlign: one.align, nowWrap: false }
  return { bounds, nowAlign: fitLabel(anchor, nowWrapPx, trackPx).align, nowWrap: true }
}

/** The alignment the chart gives a boundary tick at `x` (0–100). */
export function tickAlign(x: number): LabelAlign {
  return x <= 0 ? 'start' : x >= 100 ? 'end' : 'center'
}

/**
 * Which chart ticks to show on a measured plot: the two ends always, an inner
 * tick only when it clears its kept neighbour and the last tick by
 * LP_LABEL_GAP_PX. Returns one flag per tick, in order.
 */
export function visibleTicks(
  ticks: Array<{ x: number; width: number }>,
  plotPx: number,
): boolean[] {
  const spans = ticks.map((tick) =>
    labelExtent((plotPx * tick.x) / 100, tick.width, tickAlign(tick.x)),
  )
  const keep = ticks.map(() => false)
  if (spans.length === 0) return keep
  const clear = (a: [number, number], b: [number, number]): boolean =>
    a[1] + LP_LABEL_GAP_PX <= b[0] || b[1] + LP_LABEL_GAP_PX <= a[0]
  const lastIndex = spans.length - 1
  const last = spans[lastIndex] as [number, number]
  keep[0] = true
  let prev = spans[0] as [number, number]
  for (let i = 1; i < lastIndex; i++) {
    const span = spans[i] as [number, number]
    if (clear(prev, span) && clear(span, last)) {
      keep[i] = true
      prev = span
    }
  }
  if (lastIndex > 0 && clear(spans[0] as [number, number], last)) keep[lastIndex] = true
  return keep
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

/** A USD figure; null renders "—" with a "no price" hint, never "$0". */
function usdNode(className: string, value: number | null, format = formatUsd): HTMLElement {
  const node = el('span', className, format(value))
  if (value === null) {
    node.dataset.lpNoPrice = 'true'
    node.title = t('chat.lpNoPriceTitle')
  }
  return node
}

function statusLabel(status: LpStatus): string {
  switch (status) {
    case 'in-range':
      return t('chat.lpStatusInRange')
    case 'above-range':
      return t('chat.lpStatusAboveRange')
    case 'below-range':
      return t('chat.lpStatusBelowRange')
    case 'closed':
      return t('chat.lpStatusClosed')
    default:
      return t('chat.lpStatusUnknown')
  }
}

/** Status pill: a dot plus the word, so the state never rests on colour alone. */
function statusPill(status: LpStatus): HTMLElement {
  const pill = el('span', 'lp-pill')
  pill.dataset.lpStatus = status
  const dot = el('span', 'lp-pill__dot', '●')
  dot.setAttribute('aria-hidden', 'true')
  pill.append(dot, el('span', 'lp-pill__text', statusLabel(status)))
  return pill
}

function pairNode(token: LpToken, quote: LpToken): HTMLElement {
  const pair = el('span', 'lp-card__pair')
  pair.append(
    el('span', 'lp-card__base', token.symbol),
    el('span', 'lp-card__slash', ' / '),
    el('span', 'lp-card__quote', quote.symbol),
  )
  return pair
}

function header(
  token: LpToken,
  quote: LpToken,
  chain: LpChain | null,
  pool: LpPool,
  trailing: HTMLElement[],
): HTMLElement {
  const head = el('header', 'lp-card__head')
  const title = el('div', 'lp-card__title')
  title.append(pairNode(token, quote))
  const meta = [chain?.name ?? '', pool.feePct].filter(Boolean).join(' · ')
  if (meta) title.append(el('span', 'lp-card__meta', meta))
  head.append(title)
  if (trailing.length) {
    const end = el('div', 'lp-card__head-end')
    end.append(...trailing)
    head.append(end)
  }
  return head
}

function partialBadge(payload: LpPayload): HTMLElement | null {
  const truncated = payload.kind === 'ranges' && payload.scan.truncated
  if (!payload.partialScan && !truncated) return null
  const badge = el('span', 'lp-badge', t('chat.lpPartialScan'))
  badge.dataset.lpBadge = 'partial-scan'
  const scan = payload.kind === 'ranges' ? payload.scan : null
  badge.title =
    scan && scan.scannedWords !== null && scan.fullWords !== null
      ? t('chat.lpPartialScanTitle', {
          scanned: String(scan.scannedWords),
          full: String(scan.fullWords),
        })
      : t('chat.lpPartialScanTitleShort')
  return badge
}

/**
 * `data-lp-fees` for an unclaimed-fees figure: only `positive` earns the
 * green "+$x"; `zero` reads a muted "$0.00"; `none` is unpriced ("—").
 */
function feesTone(usd: number | null): 'positive' | 'zero' | 'none' {
  if (usd === null || !Number.isFinite(usd)) return 'none'
  return usd > 0 ? 'positive' : 'zero'
}

function stat(label: string, value: HTMLElement, key: string): HTMLElement {
  const cell = el('div', 'lp-stat')
  cell.dataset.lpStat = key
  cell.append(el('span', 'lp-stat__label', label), value)
  return cell
}

function amountNode(amount: LpAmount, symbol: string): HTMLElement {
  const node = el('span', 'lp-amount')
  node.append(
    el('span', 'lp-amount__value', formatTokenAmount(amount.human)),
    el('span', 'lp-amount__symbol', ` ${symbol}`),
  )
  node.title = `${amount.human || '0'} ${symbol}`
  return node
}

function amountsRow(
  className: string,
  base: LpAmount,
  baseSymbol: string,
  quote: LpAmount,
  quoteSymbol: string,
): HTMLElement {
  const row = el('div', className)
  row.append(
    amountNode(base, baseSymbol),
    el('span', 'lp-sep', ' · '),
    amountNode(quote, quoteSymbol),
  )
  return row
}

function warningsNode(warnings: string[]): HTMLElement | null {
  if (warnings.length === 0) return null
  const listNode = el('ul', 'lp-card__warnings')
  warnings.forEach((w) => listNode.append(el('li', 'lp-card__warning', w)))
  return listNode
}

/** Everything the DOM builders need from the mounter. */
export interface LpRenderContext {
  now: () => number
  copyText: (value: string) => void | Promise<void>
  /** A cancellable timer the mounter clears on unmount. */
  setTimer: (fn: () => void, ms: number) => void
  /** Offer ↻ on a card whose payload says how to re-run it. */
  readonly canRefresh?: boolean
  /**
   * Offer Collect fees / Remove… on the user's own open positions. Only an
   * operator connection (the desktop) can have its orders approved, so the
   * web console never sets it.
   */
  readonly canWrite?: boolean
}

/** Something the footer offers to copy: shown short, copied whole. */
interface CopyTarget {
  /** What the value is, shown before it: "token", "pool", "owner", "wallet". */
  label: string
  value: string
  /** An in-app name for the value ("Main"), shown between the label and the address. */
  name?: string | null
}

interface FooterSpec {
  parts: string[]
  /** Copy chips; the first is the card's subject, and the explorer link opens the same. */
  copies: CopyTarget[]
  explorer: string
  explorerName: string
  /**
   * A closed card: no "as of block" (the note says which block), and the
   * stamp reads "checked 2m ago" — when the card last looked, not what it shows.
   */
  checked?: boolean
}

function chainLabel(payload: LpPayload, key: string): string {
  const chains = payload.kind === 'positions' ? payload.chains : []
  const known =
    chains.find((c) => c.key === key) ??
    (payload.kind === 'positions'
      ? payload.positions.map((p) => p.chain).find((c) => c?.key === key)
      : payload.chain?.key === key
        ? payload.chain
        : null)
  return known?.name || key
}

/** "as of block N", or "as of Base #N · Robinhood #M" when several chains were read. */
function asOfText(payload: LpPayload): string {
  const blocks = payload.asOfBlocks
  if (blocks.length > 1) {
    return t('chat.lpAsOfBlocks', {
      blocks: blocks
        .map((b) =>
          t('chat.lpChainBlock', {
            chain: chainLabel(payload, b.key),
            block: b.block.toLocaleString('en-US'),
          }),
        )
        .join(' · '),
    })
  }
  const block = payload.asOfBlock > 0 ? payload.asOfBlock : (blocks[0]?.block ?? 0)
  return block > 0 ? t('chat.lpAsOfBlock', { block }) : ''
}

/**
 * A copy chip: the label and the short value it copies, then a brief
 * "copied" — or "copy failed" when the clipboard refuses — once the write
 * settles. `data-lp-copied` carries the same state for styling and tests.
 */
function copyChip(target: CopyTarget, ctx: LpRenderContext): HTMLElement {
  const button = el('button', 'lp-card__action lp-card__copy') as HTMLButtonElement
  button.type = 'button'
  button.dataset.lpAction = 'copy'
  button.dataset.lpCopy = target.label
  button.title = t('chat.lpCopyTitle', { address: target.value })
  button.setAttribute('aria-label', t('chat.lpCopyTitle', { address: target.value }))
  const glyph = el('span', 'lp-card__action-glyph', '⧉')
  glyph.setAttribute('aria-hidden', 'true')
  const label = el('span', 'lp-card__action-label', target.label)
  const value = el('span', 'lp-card__action-value', shortAddress(target.value))
  button.append(glyph, label)
  if (target.name) button.append(el('span', 'lp-card__action-name', target.name))
  button.append(value)

  let generation = 0
  const settle = (ok: boolean): void => {
    const mine = ++generation
    button.dataset.lpCopied = ok ? 'true' : 'failed'
    glyph.textContent = ok ? '✓' : '✕'
    value.textContent = ok ? t('chat.lpCopied') : t('chat.lpCopyFailed')
    ctx.setTimer(() => {
      if (mine !== generation) return
      delete button.dataset.lpCopied
      glyph.textContent = '⧉'
      value.textContent = shortAddress(target.value)
    }, LP_COPIED_MS)
  }
  button.addEventListener('click', () => {
    void Promise.resolve()
      .then(() => ctx.copyText(target.value))
      .then(
        () => settle(true),
        () => settle(false),
      )
  })
  return button
}

function footer(payload: LpPayload, spec: FooterSpec, ctx: LpRenderContext): HTMLElement {
  const foot = el('footer', 'lp-card__foot')
  const meta = el('span', 'lp-card__foot-meta')
  const parts = [...spec.parts]
  const asOf = spec.checked ? '' : asOfText(payload)
  if (asOf) parts.push(asOf)
  meta.textContent = parts.join(' · ')
  const ago = relativeTime(payload.fetchedAt, ctx.now())
  if (ago) {
    const time = el('time', 'lp-card__ago', ago)
    time.setAttribute('datetime', payload.fetchedAt)
    time.dataset.lpFetchedAt = payload.fetchedAt
    time.title = payload.fetchedAt
    if (parts.length) meta.append(el('span', 'lp-sep', ' · '))
    if (spec.checked) meta.append(el('span', 'lp-card__checked', `${t('chat.lpChecked')} `))
    meta.append(time)
  }
  foot.append(meta)

  const actions = el('span', 'lp-card__actions')
  for (const target of spec.copies) {
    if (target.value) actions.append(copyChip(target, ctx))
  }
  if (spec.explorer) {
    const link = el('a', 'lp-card__action lp-card__explorer') as HTMLAnchorElement
    link.href = spec.explorer
    link.target = '_blank'
    link.rel = 'noopener noreferrer'
    link.dataset.lpAction = 'explorer'
    link.title = t('chat.lpExplorerTitle', { explorer: spec.explorerName })
    const glyph = el('span', 'lp-card__action-glyph', '↗')
    glyph.setAttribute('aria-hidden', 'true')
    link.append(glyph, el('span', 'lp-card__action-label', t('chat.lpExplorer')))
    actions.append(link)
  }
  if (ctx.canRefresh && payload.request) actions.append(refreshButton())
  if (actions.childElementCount) foot.append(actions)
  return foot
}

/** ↻: re-run the card's read. The mounter's click handler does the work. */
function refreshButton(): HTMLElement {
  const button = el('button', 'lp-card__action lp-card__refresh') as HTMLButtonElement
  button.type = 'button'
  button.dataset.lpAction = 'refresh'
  button.title = t('chat.lpRefreshTitle')
  button.setAttribute('aria-label', t('chat.lpRefreshTitle'))
  const glyph = el('span', 'lp-card__action-glyph', '↻')
  glyph.setAttribute('aria-hidden', 'true')
  button.append(glyph, el('span', 'lp-card__action-label', t('chat.lpRefresh')))
  return button
}

/** A position the user can act on: in the vault, still open, and addressable. */
function writable(position: LpPosition): boolean {
  return (
    position.owner.inApp &&
    position.status !== 'closed' &&
    /^\d+$/.test(position.tokenId) &&
    (position.chain?.id ?? 0) > 0
  )
}

/** True only when the position reports zero owed on both sides — never for unknown fees. */
function noFeesOwed(position: Pick<LpPosition, 'fees'>): boolean {
  return position.fees.base.raw === '0' && position.fees.quote.raw === '0'
}

/**
 * Collect fees / Remove… (50 % or 100 %) for one position. Plain markup keyed
 * by `data-lp-token-id` + `data-lp-chain-id`; the mounter's click handler
 * calls the write RPC and drives `data-lp-write` (pending → awaiting).
 */
function writeActions(position: LpPosition, className: string): HTMLElement {
  const group = el('div', `lp-writes ${className}`)
  group.dataset.lpWrites = 'true'
  group.dataset.lpTokenId = position.tokenId
  group.dataset.lpChainId = String(position.chain?.id ?? 0)
  const button = (action: string, label: string, title: string): HTMLButtonElement => {
    const b = el('button', 'lp-card__action lp-writes__button', label) as HTMLButtonElement
    b.type = 'button'
    b.dataset.lpAction = action
    b.title = title
    return b
  }
  const collect = button(
    'collect',
    t('chat.lpCollectFees'),
    t('chat.lpCollectTitle', { id: position.tokenId }),
  )
  // Both sides owe exactly zero: a collect would only pay gas. Unknown fees
  // (an empty raw) are not zero — the button stays.
  if (noFeesOwed(position)) {
    collect.dataset.lpNoFees = 'true'
    collect.disabled = true
    collect.title = t('chat.lpNoFeesYet')
  }
  const remove = button('remove', t('chat.lpRemove'), t('chat.lpRemoveTitle'))
  remove.setAttribute('aria-expanded', 'false')
  const choice = el('span', 'lp-writes__choice')
  choice.dataset.lpChoice = 'remove'
  choice.hidden = true
  choice.setAttribute('role', 'group')
  choice.setAttribute('aria-label', t('chat.lpRemoveChoice'))
  for (const pct of [50, 100]) {
    const b = button(
      'remove-pct',
      `${pct}%`,
      t('chat.lpRemovePctTitle', { pct: String(pct), id: position.tokenId }),
    )
    b.dataset.lpPct = String(pct)
    if (pct === 100) b.dataset.lpTone = 'warn'
    choice.append(b)
  }
  const state = el('span', 'lp-writes__state')
  state.dataset.lpState = 'true'
  state.setAttribute('role', 'status')
  state.setAttribute('aria-live', 'polite')
  state.hidden = true
  const error = el('p', 'lp-writes__error')
  error.dataset.lpError = 'true'
  error.setAttribute('role', 'alert')
  error.hidden = true
  group.append(collect, remove, choice, state, error)
  return group
}

function explorerName(chain: LpChain | null): string {
  if (!chain?.explorer) return ''
  try {
    return new URL(chain.explorer).host
  } catch {
    return chain.name
  }
}

function shell(payload: LpPayload, status?: LpStatus): HTMLElement {
  const card = el('article', 'lp-card')
  card.dataset.lpKind = payload.kind
  if (status) card.dataset.lpStatus = status
  if (payload.chain?.key) card.dataset.lpChain = payload.chain.key
  if (payload.partialScan) card.dataset.lpPartial = 'true'
  return card
}

/* ── kind = pool ── */

/**
 * Pool and ranges footers: the token is the subject — its address is the
 * first chip and what the explorer opens — and the pool id is its own chip,
 * so what is shown is exactly what is copied.
 */
function tokenFooter(payload: LpPoolPayload | LpRangesPayload): FooterSpec {
  return {
    parts: [],
    copies: [
      { label: t('chat.lpCopyToken'), value: payload.token.address },
      { label: t('chat.lpCopyPool'), value: payload.pool.poolId },
    ],
    explorer: explorerUrl(payload.chain, 'address', payload.token.address),
    explorerName: explorerName(payload.chain),
  }
}

function buildPool(payload: LpPoolPayload, ctx: LpRenderContext): HTMLElement {
  const card = shell(payload)
  const badge = partialBadge(payload)
  card.append(
    header(payload.token, payload.quote, payload.chain, payload.pool, badge ? [badge] : []),
  )

  const stats = el('div', 'lp-card__stats')
  stats.append(
    stat(t('chat.lpTvl'), usdNode('lp-stat__value', payload.pool.tvlUsd, formatUsdCompact), 'tvl'),
    stat(t('chat.lpPrice'), usdNode('lp-stat__value', payload.pool.priceUsd), 'price'),
    stat(
      t('chat.lpMcap'),
      usdNode('lp-stat__value', payload.pool.mcapUsd, formatUsdCompact),
      'mcap',
    ),
  )
  card.append(stats)

  const reserves = el('div', 'lp-card__row')
  reserves.dataset.lpRow = 'reserves'
  reserves.append(el('span', 'lp-card__row-label', t('chat.lpReserves')))
  const reserveValues = el('span', 'lp-card__row-value')
  for (const [amount, symbol] of [
    [payload.reserves.base, payload.token.symbol],
    [payload.reserves.quote, payload.quote.symbol],
  ] as Array<[LpAmount, string]>) {
    if (reserveValues.childElementCount) reserveValues.append(el('span', 'lp-sep', ' · '))
    reserveValues.append(amountNode(amount, symbol))
    const usd = usdNode('lp-amount__usd', amount.usd, formatUsdCompact)
    usd.textContent = ` (${usd.textContent})`
    reserveValues.append(usd)
  }
  reserves.append(reserveValues)
  card.append(reserves)

  const safety = el('div', 'lp-card__row lp-safety')
  safety.dataset.lpRow = 'safety'
  safety.append(el('span', 'lp-card__row-label', t('chat.lpLauncher')))
  const launcher = el(
    'span',
    'lp-safety__launcher',
    payload.safety.launcher.name ?? t('chat.lpLauncherUnknown'),
  )
  if (payload.safety.launcher.address) launcher.title = payload.safety.launcher.address
  const locked = payload.safety.locked
  const lock = el(
    'span',
    'lp-safety__lock',
    locked === true
      ? t('chat.lpLocked')
      : locked === false
        ? t('chat.lpNotLocked')
        : t('chat.lpLockUnknown'),
  )
  lock.dataset.lpLocked = locked === null ? 'unknown' : String(locked)
  safety.append(launcher, lock)
  if (payload.safety.note) safety.append(el('span', 'lp-safety__note', payload.safety.note))
  card.append(safety)

  const axis = chooseAxis(payload.topRanges)
  const ranges = el('section', 'lp-card__ranges')
  ranges.append(el('h4', 'lp-card__section-title', t('chat.lpTopRanges')))
  if (payload.topRanges.length === 0) {
    ranges.append(el('p', 'lp-card__empty-line', t('chat.lpTopRangesEmpty')))
  } else {
    const rows = el('ol', 'lp-ranges')
    for (const range of payload.topRanges) {
      const row = el('li', 'lp-ranges__row')
      const meter = el('span', 'lp-ranges__meter')
      meter.setAttribute('aria-hidden', 'true')
      const fill = el('span', 'lp-ranges__fill')
      fill.style.width = `${(range.share * 100).toFixed(1)}%`
      meter.append(fill)
      row.append(
        el('span', 'lp-ranges__band', formatRangeLabel(range, axis)),
        meter,
        el('span', 'lp-ranges__share', formatShare(range.share)),
        el('span', 'lp-ranges__owner', range.owner ? shortAddress(range.owner) : ''),
      )
      if (range.owner) (row.lastElementChild as HTMLElement).title = range.owner
      rows.append(row)
    }
    ranges.append(rows)
  }
  card.append(ranges)

  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  card.append(footer(payload, tokenFooter(payload), ctx))
  return card
}

/* ── kind = ranges ── */

let tooltipSeq = 0

function tooltipLines(
  segment: LpSegment,
  axis: LpAxis,
  payload: LpRangesPayload,
): Array<[string, string]> {
  const lines: Array<[string, string]> = [['range', formatRangeLabel(segment, axis)]]
  if (axis === 'mcap' && segment.priceLower !== null && segment.priceUpper !== null) {
    lines.push([
      'price',
      `${formatRangeLabel(segment, 'price')} ${t('chat.lpPriceUnit', {
        quote: payload.quote.symbol,
        base: payload.token.symbol,
      })}`,
    ])
  }
  const shareText = t('chat.lpShareOfLiquidity', { share: formatShare(segment.share) })
  lines.push(['share', segment.active ? `${shareText} · ${t('chat.lpActive')}` : shareText])
  lines.push([
    'amounts',
    `${formatTokenAmount(segment.base.human)} ${payload.token.symbol} · ${formatTokenAmount(
      segment.quote.human,
    )} ${payload.quote.symbol}`,
  ])
  return lines
}

function buildChart(payload: LpRangesPayload): HTMLElement {
  const model = buildChartModel(payload)
  const figure = el('figure', 'lp-chart')
  figure.dataset.lpAxis = model.axis
  const axisName = model.axis === 'mcap' ? t('chat.lpAxisMcap') : t('chat.lpAxisPrice')

  const plot = el('div', 'lp-chart__plot')
  const tooltipId = `lp-tip-${++tooltipSeq}`
  const tooltip = el('div', 'lp-chart__tooltip')
  tooltip.id = tooltipId
  tooltip.setAttribute('role', 'tooltip')
  tooltip.hidden = true

  const chart = svg('svg', {
    class: 'lp-chart__svg',
    viewBox: '0 0 1000 100',
    preserveAspectRatio: 'none',
    role: 'group',
    'aria-label': t('chat.lpChartLabel', { axis: axisName }),
  })
  chart.append(svg('line', { class: 'lp-chart__baseline', x1: 0, x2: 1000, y1: 100, y2: 100 }))

  const groups: SVGElement[] = []
  const show = (bar: ChartBar, group: SVGElement): void => {
    tooltip.replaceChildren(
      ...tooltipLines(bar.segment, model.axis, payload).map(([key, line]) => {
        const row = el('span', 'lp-chart__tooltip-line', line)
        row.dataset.lpLine = key
        return row
      }),
    )
    const center = bar.x + bar.width / 2
    tooltip.style.left = `${center}%`
    tooltip.dataset.align = center < 20 ? 'start' : center > 80 ? 'end' : 'center'
    tooltip.hidden = false
    groups.forEach((g) => g.removeAttribute('data-hover'))
    group.setAttribute('data-hover', 'true')
  }
  const hide = (): void => {
    tooltip.hidden = true
    groups.forEach((g) => g.removeAttribute('data-hover'))
  }

  for (const bar of model.bars) {
    const group = svg('g', {
      class: 'lp-chart__bar',
      tabindex: 0,
      role: 'img',
      'aria-label': t('chat.lpChartBar', {
        range: formatRangeLabel(bar.segment, model.axis),
        share: formatShare(bar.segment.share),
      }),
      'aria-describedby': tooltipId,
      'data-index': bar.index,
    })
    if (bar.segment.active) group.setAttribute('data-active', 'true')
    const gap = model.bars.length > 1 ? Math.min(4, bar.width * 2) : 0
    const x = bar.x * 10 + gap / 2
    const width = Math.max(1, bar.width * 10 - gap)
    group.append(
      svg('rect', {
        class: 'lp-chart__hit',
        x: bar.x * 10,
        y: 0,
        width: bar.width * 10,
        height: 100,
      }),
      svg('rect', {
        class: 'lp-chart__fill',
        x,
        y: 100 - bar.height,
        width,
        height: bar.height,
      }),
    )
    group.addEventListener('mouseenter', () => show(bar, group))
    group.addEventListener('mouseleave', hide)
    group.addEventListener('focus', () => show(bar, group))
    group.addEventListener('blur', hide)
    group.addEventListener('keydown', (event) => {
      const key = (event as KeyboardEvent).key
      if (key === 'Escape') {
        hide()
        return
      }
      const target =
        key === 'ArrowRight'
          ? bar.index + 1
          : key === 'ArrowLeft'
            ? bar.index - 1
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

  if (model.marker) {
    chart.append(
      svg('line', {
        class: 'lp-chart__now-line',
        x1: model.marker.x * 10,
        x2: model.marker.x * 10,
        y1: 0,
        y2: 100,
        'vector-effect': 'non-scaling-stroke',
      }),
    )
  }
  plot.append(chart)

  if (model.marker) {
    const glyph = model.marker.edge === 'left' ? '◀ ' : model.marker.edge === 'right' ? '' : '▼ '
    const tail = model.marker.edge === 'right' ? ' ▶' : ''
    const label = el('span', 'lp-chart__now', `${glyph}${model.marker.label}${tail}`)
    label.style.left = `${model.marker.x}%`
    label.dataset.align = model.marker.x < 15 ? 'start' : model.marker.x > 85 ? 'end' : 'center'
    if (model.marker.edge) label.dataset.edge = model.marker.edge
    plot.append(label)
  }
  plot.append(tooltip)
  figure.append(plot)

  const axis = el('div', 'lp-chart__axis')
  axis.setAttribute('aria-hidden', 'true')
  for (const tick of model.ticks) {
    const label = el('span', 'lp-chart__tick', tick.label)
    label.style.left = `${tick.x}%`
    label.dataset.align = tick.x <= 0 ? 'start' : tick.x >= 100 ? 'end' : 'center'
    axis.append(label)
  }
  figure.append(axis)

  const caption = el('figcaption', 'lp-chart__caption')
  caption.textContent =
    model.axis === 'mcap'
      ? t('chat.lpAxisMcap')
      : `${t('chat.lpAxisPrice')} (${t('chat.lpPriceUnit', {
          quote: payload.quote.symbol,
          base: payload.token.symbol,
        })})`
  figure.append(caption)
  return figure
}

function buildRanges(payload: LpRangesPayload, ctx: LpRenderContext): HTMLElement {
  const card = shell(payload)
  if (payload.scan.truncated) card.dataset.lpPartial = 'true'
  const badge = partialBadge(payload)
  card.append(
    header(payload.token, payload.quote, payload.chain, payload.pool, badge ? [badge] : []),
  )

  const stats = el('div', 'lp-card__stats')
  stats.append(
    stat(t('chat.lpTvl'), usdNode('lp-stat__value', payload.pool.tvlUsd, formatUsdCompact), 'tvl'),
    stat(
      t('chat.lpMcap'),
      usdNode('lp-stat__value', payload.current.mcapUsd ?? payload.pool.mcapUsd, formatUsdCompact),
      'mcap',
    ),
    stat(
      t('chat.lpPrice'),
      usdNode('lp-stat__value', payload.current.priceUsd ?? payload.pool.priceUsd),
      'price',
    ),
  )
  card.append(stats)
  if (payload.segments.length > 0) card.append(buildChart(payload))
  else card.append(el('p', 'lp-card__empty-line', t('chat.lpTopRangesEmpty')))

  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  card.append(footer(payload, tokenFooter(payload), ctx))
  return card
}

/* ── kind = position ── */

function rangeBar(position: LpPosition): HTMLElement {
  const marker = positionMarker(position)
  const [lo, hi] = rangeBounds(position.range, marker.axis)
  const [left, right] = RANGE_BAND
  const wrap = el('div', 'lp-range')
  wrap.dataset.lpAxis = marker.axis
  wrap.dataset.lpNow = marker.outside ?? 'inside'
  if (marker.pinned) wrap.dataset.lpPinned = 'true'
  if (position.band) wrap.title = position.band

  const axisLabel = marker.axis === 'mcap' ? t('chat.lpAxisMcap') : t('chat.lpAxisPrice')

  // Everything positioned is a child of the track, so every % offset is
  // measured against the same box: the band is the range, its two bounds sit
  // on the band's own edges, and the track runs past it on both sides.
  const track = el('span', 'lp-range__track')
  const band = el('span', 'lp-range__band')
  band.setAttribute('aria-hidden', 'true')
  band.style.left = `${left}%`
  band.style.width = `${right - left}%`

  const lower = el('span', 'lp-range__lower', formatAxisValue(lo, marker.axis))
  lower.style.left = `${left}%`
  lower.dataset.align = 'start'
  const upper = el('span', 'lp-range__upper', formatAxisValue(hi, marker.axis))
  upper.style.left = `${right}%`
  upper.dataset.align = 'end'

  const pin = el('span', 'lp-range__pin')
  pin.setAttribute('aria-hidden', 'true')
  pin.style.left = `${marker.x}%`
  if (marker.pinned) pin.dataset.pinned = marker.outside ?? 'true'

  const distancePct = signedDistance(position)
  const distance =
    marker.outside && distancePct !== null ? ` (${formatSignedPct(distancePct)})` : ''
  const side =
    marker.outside === 'above'
      ? ` ${t('chat.lpAboveRangeShort')}`
      : marker.outside === 'below'
        ? ` ${t('chat.lpBelowRangeShort')}`
        : ''
  const reading = `${t('chat.lpNow')} ${formatAxisValue(marker.value, marker.axis)}`
  // A pinned marker is not to scale: it points off the track instead of up at it.
  const head =
    marker.pinned && marker.outside === 'below'
      ? `◀ ${reading}`
      : marker.pinned && marker.outside === 'above'
        ? reading
        : `▲ ${reading}`
  const tail = `${distance}${side}${marker.pinned && marker.outside === 'above' ? ' ▶' : ''}`
  // Two parts so a narrow track can stack the distance and side under the
  // reading (`.lp-range[data-lp-now-wrap]`, set by layoutLpCard).
  const now = el('span', 'lp-range__now')
  now.append(el('span', 'lp-range__now-value', head))
  if (tail) now.append(el('span', 'lp-range__now-side', tail))
  now.style.left = `${marker.x}%`
  now.dataset.align = marker.x < 20 ? 'start' : marker.x > 80 ? 'end' : 'center'
  if (marker.outside) now.dataset.outside = marker.outside
  if (marker.pinned) now.dataset.pinned = 'true'

  track.append(band, lower, upper, pin, now)
  const row = el('div', 'lp-range__row')
  row.append(el('span', 'lp-range__axis', axisLabel), track)
  wrap.append(row)
  return wrap
}

function walletName(wallet: LpWallet): string {
  return wallet.label ?? shortAddress(wallet.address)
}

/** The one line a closed position card says instead of its figures. */
function closedNoteText(payload: LpPositionPayload): string {
  const lastSeen = payload.lastSeenBlock ?? 0
  if (lastSeen > 0) return t('chat.lpClosedBurned', { block: lastSeen })
  if (payload.asOfBlock > 0) return t('chat.lpClosedEmpty', { block: payload.asOfBlock })
  return t('chat.lpClosedNote')
}

function positionFooter(
  payload: LpPositionPayload,
  ctx: LpRenderContext,
  checked = false,
): HTMLElement {
  const position = payload.position
  return footer(
    payload,
    {
      // The owner is the copy chip (copy + explorer), never repeated as text.
      parts: position.tokenId ? [`#${position.tokenId}`] : [],
      copies: [
        {
          label: t('chat.lpCopyOwner'),
          value: position.owner.address,
          name: position.owner.label,
        },
      ],
      explorer: explorerUrl(position.chain, 'address', position.owner.address),
      explorerName: explorerName(position.chain),
      checked,
    },
    ctx,
  )
}

/**
 * A position the chain no longer holds open — burned, or emptied to zero
 * liquidity. Its value, fees, amounts and range belong to a position that is
 * gone, so the card keeps only who it was (pair, chain, fee, `#id`) and says
 * when it was last seen.
 */
function buildClosedPosition(payload: LpPositionPayload, ctx: LpRenderContext): HTMLElement {
  const position = payload.position
  const card = shell(payload, 'closed')
  card.append(
    header(position.token, position.quote, position.chain, position.pool, [statusPill('closed')]),
  )
  const note = el('p', 'lp-card__closed-note', closedNoteText(payload))
  note.dataset.lpClosedNote = 'true'
  card.append(note, positionFooter(payload, ctx, true))
  return card
}

/**
 * The closed version of an open position card, for when a later read (or a
 * write) found it gone: last seen at the block it was drawn from, checked now.
 */
export function closedPositionPayload(
  payload: LpPositionPayload,
  checkedAt: string,
): LpPositionPayload {
  return {
    ...payload,
    position: { ...payload.position, status: 'closed', distancePct: null },
    lastSeenBlock: payload.lastSeenBlock ?? payload.asOfBlock,
    fetchedAt: checkedAt,
    partialScan: false,
    warnings: [],
  }
}

function buildPosition(payload: LpPositionPayload, ctx: LpRenderContext): HTMLElement {
  const position = payload.position
  if (position.status === 'closed') return buildClosedPosition(payload, ctx)
  const card = shell(payload, position.status)
  const badge = partialBadge(payload)
  const trailing = [statusPill(position.status)]
  if (badge) trailing.unshift(badge)
  card.append(header(position.token, position.quote, position.chain, position.pool, trailing))

  const hero = el('div', 'lp-card__hero')
  const value = el('div', 'lp-hero')
  value.dataset.lpHero = 'value'
  value.append(
    usdNode('lp-hero__value', position.valueUsd),
    el('span', 'lp-hero__label', t('chat.lpValue')),
  )
  const fees = el('div', 'lp-hero')
  fees.dataset.lpHero = 'fees'
  const feesValue = usdNode('lp-hero__value', position.fees.usd)
  feesValue.dataset.lpFees = feesTone(position.fees.usd)
  if (position.fees.usd !== null && position.fees.usd > 0) {
    feesValue.textContent = `+${feesValue.textContent}`
  }
  fees.append(feesValue, el('span', 'lp-hero__label', t('chat.lpUnclaimedFees')))
  hero.append(value, fees)
  card.append(hero)

  card.append(rangeBar(position))

  const principal = amountsRow(
    'lp-card__amounts',
    position.principal.base,
    position.token.symbol,
    position.principal.quote,
    position.quote.symbol,
  )
  principal.dataset.lpRow = 'principal'
  principal.title = `${t('chat.lpPrincipal')}: ${formatUsd(position.principal.usd)}`
  card.append(principal)

  const feeAmounts = amountsRow(
    'lp-card__amounts lp-card__amounts--fees',
    position.fees.base,
    position.token.symbol,
    position.fees.quote,
    position.quote.symbol,
  )
  feeAmounts.dataset.lpRow = 'fees'
  feeAmounts.prepend(el('span', 'lp-card__row-label', `${t('chat.lpFees')} `))
  card.append(feeAmounts)
  if (ctx.canWrite && writable(position)) card.append(writeActions(position, 'lp-card__writes'))

  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  card.append(positionFooter(payload, ctx))
  return card
}

/* ── kind = positions ── */

function positionRow(position: LpPosition, ctx: LpRenderContext): HTMLElement {
  const row = el('li', 'lp-row')
  row.dataset.lpStatus = position.status
  if (position.chain?.key) row.dataset.lpChain = position.chain.key
  if (position.tokenId) row.dataset.lpTokenId = position.tokenId

  const pair = el('span', 'lp-row__pair')
  pair.append(pairNode(position.token, position.quote))
  if (position.pool.feePct) pair.append(el('span', 'lp-row__fee', position.pool.feePct))
  const chain = el('span', 'lp-row__chain', position.chain?.name ?? '')

  // The owner opens on the explorer when the chain has one; otherwise it is
  // plain text and carries no link glyph.
  const ownerUrl = explorerUrl(position.chain, 'address', position.owner.address)
  let wallet: HTMLElement
  if (ownerUrl) {
    const link = el('a', 'lp-row__wallet', walletName(position.owner)) as HTMLAnchorElement
    link.href = ownerUrl
    link.target = '_blank'
    link.rel = 'noopener noreferrer'
    link.dataset.lpLink = 'true'
    link.title = `${position.owner.address} · ${t('chat.lpExplorerTitle', {
      explorer: explorerName(position.chain),
    })}`
    wallet = link
  } else {
    wallet = el('span', 'lp-row__wallet', walletName(position.owner))
    wallet.title = position.owner.address
  }
  if (!position.owner.inApp) wallet.dataset.lpExternal = 'true'

  // Status, then how far out of range: the number a trader acts on.
  const status = el('span', 'lp-row__status')
  status.append(statusPill(position.status))
  const distance = signedDistance(position)
  if (distance !== null) {
    const pct = el('span', 'lp-row__distance', formatSignedPct(distance))
    const signed = formatSignedPct(distance)
    pct.title =
      distance > 0
        ? t('chat.lpDistanceAboveTitle', { pct: signed })
        : t('chat.lpDistanceBelowTitle', { pct: signed })
    status.append(pct)
  }

  const fees = el('span', 'lp-row__fees')
  const feesUsd = position.fees.usd
  fees.textContent = feesUsd === null ? NO_VALUE : `${feesUsd > 0 ? '+' : ''}${formatUsd(feesUsd)}`
  fees.title =
    feesUsd === null
      ? `${t('chat.lpUnclaimedFees')}: ${t('chat.lpNoPriceTitle')}`
      : `${t('chat.lpUnclaimedFees')}: ${formatUsd(feesUsd)}`
  if (feesUsd === null) fees.dataset.lpNoPrice = 'true'
  fees.dataset.lpFees = feesTone(feesUsd)

  const value = el('span', 'lp-row__value')
  if (position.valueUsd === null) {
    value.dataset.lpNoPrice = 'true'
    value.title = t('chat.lpNoPriceTitle')
    value.append(
      el('span', 'lp-row__value-num', NO_VALUE),
      el('span', 'lp-row__no-price', ` ${t('chat.lpNoPrice')}`),
    )
  } else {
    value.append(el('span', 'lp-row__value-num', formatUsd(position.valueUsd)))
  }
  row.append(status, pair, chain, wallet, fees, value)
  if (ctx.canWrite && writable(position)) row.append(writeActions(position, 'lp-row__writes'))
  if (position.band) row.title = position.band
  return row
}

function joinNames(names: string[]): string {
  return names.filter(Boolean).join(', ')
}

function buildPositions(payload: LpPositionsPayload, ctx: LpRenderContext): HTMLElement {
  const card = shell(payload)
  if (payload.positions.length === 0) card.dataset.lpEmpty = 'true'
  const head = el('header', 'lp-card__head')
  const title = el('div', 'lp-card__title')
  title.append(el('span', 'lp-card__pair', t('chat.lpPositionsTitle')))
  const scope = [
    payload.wallets.length ? tPlural('chat.lpWallets', payload.wallets.length) : '',
    payload.chains.length ? tPlural('chat.lpChains', payload.chains.length) : '',
  ].filter(Boolean)
  if (scope.length) title.append(el('span', 'lp-card__meta', scope.join(' · ')))
  head.append(title)
  const badge = partialBadge(payload)
  if (badge) {
    const end = el('div', 'lp-card__head-end')
    end.append(badge)
    head.append(end)
  }
  card.append(head)

  if (payload.positions.length === 0) {
    card.append(
      el(
        'p',
        'lp-card__empty',
        t('chat.lpEmpty', {
          wallets: joinNames(payload.wallets.map(walletName)) || '—',
          chains: joinNames(payload.chains.map((c) => c.name)) || '—',
        }),
      ),
    )
  } else {
    const totals = el('div', 'lp-card__stats lp-totals')
    const outOfRange = el('span', 'lp-stat__value', String(payload.totals.outOfRange))
    if (payload.totals.outOfRange > 0) outOfRange.dataset.lpTone = 'warn'
    totals.append(
      stat(
        t('chat.lpPositions'),
        el('span', 'lp-stat__value', String(payload.totals.count)),
        'count',
      ),
      stat(t('chat.lpOutOfRange'), outOfRange, 'out-of-range'),
      stat(t('chat.lpTotalValue'), usdNode('lp-stat__value', payload.totals.valueUsd), 'value'),
      stat(t('chat.lpTotalFees'), usdNode('lp-stat__value', payload.totals.feesUsd), 'fees'),
    )
    card.append(totals)
    const rows = el('ul', 'lp-rows')
    // The engine's order is the contract (out-of-range first, then by value);
    // never re-sort here.
    payload.positions.forEach((position) => rows.append(positionRow(position, ctx)))
    card.append(rows)
  }

  const warnings = warningsNode(payload.warnings)
  if (warnings) card.append(warnings)
  const single = payload.wallets.length === 1 ? (payload.wallets[0] as LpWallet) : null
  const singleChain = payload.chains.length === 1 ? (payload.chains[0] as LpChain) : null
  card.append(
    footer(
      payload,
      {
        // The wallet is the copy chip, never repeated as text.
        parts: [],
        copies: single
          ? [{ label: t('chat.lpCopyWallet'), value: single.address, name: single.label }]
          : [],
        explorer: single && singleChain ? explorerUrl(singleChain, 'address', single.address) : '',
        explorerName: explorerName(singleChain),
      },
      ctx,
    ),
  )
  return card
}

/**
 * Build the card for a normalized payload. Exported for the unit tests, which
 * assert on the built DOM without standing up a mounter or a fetch.
 */
export function buildLpCard(payload: LpPayload, ctx: LpRenderContext): HTMLElement {
  switch (payload.kind) {
    case 'pool':
      return buildPool(payload, ctx)
    case 'ranges':
      return buildRanges(payload, ctx)
    case 'position':
      return buildPosition(payload, ctx)
    case 'positions':
      return buildPositions(payload, ctx)
  }
}

/**
 * Replace `host`'s card slot with the payload's card. The kind lives on the
 * card (`.lp-card[data-lp-kind]`) only; the host just says it holds one.
 */
export function renderLp(host: HTMLElement, payload: LpPayload, ctx: LpRenderContext): void {
  const slot = host.querySelector<HTMLElement>('.msg-artifact-lp__body')
  if (!slot) return
  slot.replaceChildren(buildLpCard(payload, ctx))
  host.dataset.lpHost = 'rendered'
}

/**
 * Copy through the Clipboard API, falling back to a hidden-textarea
 * `execCommand('copy')` when the API is missing or refuses — an Electron
 * window without focus, or a page outside a secure context, rejects
 * `writeText`, and a copy button that silently does nothing is the bug.
 */
export function copyLpText(value: string): Promise<void> {
  const fallback = (): Promise<void> => {
    const area = document.createElement('textarea')
    area.value = value
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.left = '-9999px'
    document.body.appendChild(area)
    area.select()
    let ok = false
    try {
      ok = typeof document.execCommand === 'function' && document.execCommand('copy')
    } catch {
      ok = false
    }
    area.remove()
    return ok ? Promise.resolve() : Promise.reject(new Error('copy command failed'))
  }
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    return navigator.clipboard.writeText(value).catch(fallback)
  }
  return fallback()
}

/** Re-render every "2m ago" stamp inside `root`. */
export function refreshLpTimes(root: ParentNode, nowMs: number): void {
  root.querySelectorAll<HTMLElement>('[data-lp-fetched-at]').forEach((node) => {
    const next = relativeTime(node.dataset.lpFetchedAt || '', nowMs)
    if (next && node.textContent !== next) node.textContent = next
  })
}

/* ── Measured layout ────────────────────────────────────────────────────── */

/** Set (or, for null, remove) a data attribute, writing only on a change. */
function setData(node: HTMLElement, key: string, value: string | null): void {
  if (value === null) {
    if (key in node.dataset) delete node.dataset[key]
  } else if (node.dataset[key] !== value) {
    node.dataset[key] = value
  }
}

function layoutRangeBar(range: HTMLElement): void {
  const track = range.querySelector<HTMLElement>('.lp-range__track')
  const now = range.querySelector<HTMLElement>('.lp-range__now')
  const trackPx = track?.clientWidth ?? 0
  if (!track || !now || !(trackPx > 0)) return
  // Measure the reading on one line: drop the wrap first (it is re-applied
  // below, before the frame paints, when it is still needed).
  setData(range, 'lpNowWrap', null)
  const width = (selector: string): number =>
    range.querySelector<HTMLElement>(selector)?.offsetWidth ?? 0
  const layout = layoutRangeLabels({
    trackPx,
    x: parseFloat(now.style.left) || 0,
    lowerPx: width('.lp-range__lower'),
    upperPx: width('.lp-range__upper'),
    nowPx: now.offsetWidth,
    nowWrapPx: Math.max(width('.lp-range__now-value'), width('.lp-range__now-side')),
  })
  setData(range, 'lpBounds', layout.bounds === 'stacked' ? 'stacked' : null)
  setData(range, 'lpNowWrap', layout.nowWrap ? 'true' : null)
  setData(now, 'align', layout.nowAlign)
}

function layoutChart(chart: HTMLElement): void {
  const plot = chart.querySelector<HTMLElement>('.lp-chart__plot')
  const plotPx = plot?.clientWidth ?? 0
  if (!plot || !(plotPx > 0)) return
  const ticks = [...chart.querySelectorAll<HTMLElement>('.lp-chart__tick')]
  // A hidden tick keeps its box (visibility, not display), so it still measures.
  const keep = visibleTicks(
    ticks.map((tick) => ({ x: parseFloat(tick.style.left) || 0, width: tick.offsetWidth })),
    plotPx,
  )
  ticks.forEach((tick, i) => setData(tick, 'lpHidden', keep[i] ? null : 'true'))
  const now = chart.querySelector<HTMLElement>('.lp-chart__now')
  if (now) {
    const anchor = (plotPx * (parseFloat(now.style.left) || 0)) / 100
    setData(now, 'align', fitLabel(anchor, now.offsetWidth, plotPx).align)
  }
}

/**
 * Fit a rendered card to its measured width: stamp `data-lp-layout` (dense
 * position rows from LP_DENSE_MIN_PX, narrow under LP_NARROW_MAX_PX, stacked
 * between) and move apart any range
 * or chart labels that would touch. A card that measures 0 (detached, hidden,
 * jsdom) is left as rendered — stacked rows and %-placed labels.
 */
export function layoutLpCard(card: HTMLElement): void {
  const width = card.offsetWidth
  if (!(width > 0)) return
  setData(card, 'lpLayout', lpLayoutFor(width))
  card.querySelectorAll<HTMLElement>('.lp-range').forEach(layoutRangeBar)
  card.querySelectorAll<HTMLElement>('.lp-chart').forEach(layoutChart)
}

/* ── Mounter ────────────────────────────────────────────────────────────── */

/** A gateway RPC call: `rpc.call(method, params)`. */
export type LpCall = (method: string, params: Record<string, unknown>) => Promise<unknown>

/**
 * The write capability the desktop hands the mounter for its operator
 * connection. Absent (the web console) → no Collect/Remove buttons at all.
 */
export interface LpActions {
  /** Calls `trading.lp.collect` / `trading.lp.remove` on the operator connection. */
  call: LpCall
  /** An order was parked for approval (the desk can show its card). */
  onOrder?: (orderId: string) => void
}

/** What a write button is doing for one position (`<chainId>:<tokenId>`). */
interface WriteState {
  phase: 'pending' | 'awaiting'
  action: 'collect' | 'remove'
  pct?: number
  orderId?: string
  /** The card the click came from: it re-reads itself once the order settles. */
  host: HTMLElement
}

/** The read RPC a request re-runs: only the four phase-1 reads. */
export function lpReadMethod(request: LpRequest): string {
  return `trading.lp.${request.kind}`
}

/** An RPC error's code and message, whatever shape it arrived in. */
function rpcError(error: unknown): { code: string; message: string } {
  const e = error as { code?: unknown; message?: unknown } | null
  return {
    code: typeof e?.code === 'string' ? e.code : '',
    message: typeof e?.message === 'string' ? e.message : String(error),
  }
}

/** What a failed write says under its button. */
export function lpWriteErrorText(error: unknown): string {
  const { code, message } = rpcError(error)
  if (code === 'trading.lp.not_owner') return t('chat.lpErrNotOwner')
  if (code === 'trading.operator_required') return t('chat.lpErrOperator')
  if (code === 'trading.lp.position_closed') return t('chat.lpErrClosed')
  return t('chat.lpErrFailed', { message: message || code || 'error' })
}

/** The order an LP write RPC answered with: bare, or under `order`. */
function orderOf(value: unknown): { orderId: string; status: string; reason: string } | null {
  const row = obj(value)
  const order = obj(row?.order) ?? row
  const orderId = text(order?.orderId)
  if (!order || !orderId) return null
  return { orderId, status: text(order.status), reason: text(order.reason) }
}

/** `lpo_c41a9e…`: an order id short enough for a chip; the title carries it whole. */
function shortOrder(orderId: string): string {
  return orderId.length > 12 ? `${orderId.slice(0, 10)}…` : orderId
}

/** `<chainId>:<tokenId>` for a position, the key the write groups carry. */
function positionKey(position: LpPosition): string {
  return `${position.chain?.id ?? 0}:${position.tokenId}`
}

/**
 * The positions a re-read shows closed: those it says are closed, and — for a
 * complete wallet scan — those the earlier read listed that are gone now
 * (a burned position no longer shows up at all).
 */
function closedBy(before: LpPayload, after: LpPayload): string[] {
  if (after.kind === 'position') {
    return after.position.status === 'closed' ? [positionKey(after.position)] : []
  }
  if (after.kind !== 'positions') return []
  const out = after.positions.filter((p) => p.status === 'closed').map(positionKey)
  if (before.kind === 'positions' && !after.partialScan) {
    const now = new Set(after.positions.map(positionKey))
    before.positions
      .filter((p) => p.tokenId && !now.has(positionKey(p)))
      .forEach((p) => out.push(positionKey(p)))
  }
  return out
}

export interface LpMounterDeps {
  /** Fetch an LP artifact body from its (authenticated) URL. */
  fetchPayload: (url: string) => Promise<unknown>
  /**
   * The gateway call ↻ re-runs a card's read through (`trading.lp.*` reads
   * are agent-callable, so the web console passes it too). Default: none, no ↻
   * — unless `actions` is given, whose call then serves.
   */
  call?: LpCall
  /**
   * Collect/Remove on the user's own positions. An object, or a getter read
   * at render and click time (the desktop's chat becomes the desk without
   * remounting the transcript). Absent → no write buttons.
   */
  actions?: LpActions | (() => LpActions | null | undefined) | null
  /** Copy an address. Default: `copyLpText` (Clipboard API, then execCommand). */
  copyText?: (value: string) => void | Promise<void>
  /** Clock. Default: Date.now. */
  now?: () => number
  /** chat.js `_chatDiag` — the diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/**
 * Create the LP card mounter bound to the transcript's fetch surface.
 *
 * `mountLp(root)` is idempotent: it only picks up placeholders it has not
 * claimed yet, so the streaming path may call it after every artifact append
 * and the history path after a bulk replay without double-rendering.
 *
 * It owns two kinds of timer — the once-a-minute clock that refreshes the
 * relative stamps and the short "copied" resets — and `destroyAll` clears both.
 * The clock only runs while at least one rendered card is in the document.
 *
 * It also keeps every rendered card fitted to its width: one ResizeObserver
 * watches the cards and re-runs `layoutLpCard` on the next frame (never inside
 * the observer callback, which would re-trigger it).
 */
export function createLpMounter(deps: LpMounterDeps) {
  const diag = deps.diag ?? ((): void => {})
  const now = deps.now ?? ((): number => Date.now())
  const copyText = deps.copyText ?? copyLpText
  const getActions = (): LpActions | null => {
    const value = typeof deps.actions === 'function' ? deps.actions() : deps.actions
    return value && typeof value.call === 'function' ? value : null
  }
  const readCall = (): LpCall | null => deps.call ?? getActions()?.call ?? null
  const claimed = new Set<HTMLElement>()
  const rendered = new Set<HTMLElement>()
  /** The payload each rendered host shows, so ↻ knows what to re-run. */
  const payloads = new Map<HTMLElement, LpPayload>()
  const refreshing = new Set<HTMLElement>()
  const wired = new WeakSet<HTMLElement>()
  /** In-flight writes by `<chainId>:<tokenId>`; they outlive a re-render. */
  const writes = new Map<string, WriteState>()
  /** Orders that finished before their write RPC answered. */
  const finishedEarly = new Set<string>()
  /**
   * Positions (`<chainId>:<tokenId>`) a read or a write found closed or gone.
   * Every card still drawing one of them — an older position card, a positions
   * row — keeps its buttons disabled and wears the closed pill.
   */
  const closed = new Set<string>()
  const timers = new Set<ReturnType<typeof setTimeout>>()
  let clock: ReturnType<typeof setInterval> | null = null
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
        if (card.isConnected) layoutLpCard(card)
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
    const card = host.querySelector<HTMLElement>('.lp-card')
    if (!card) return
    const previous = cards.get(host)
    if (previous && previous !== card) observer?.unobserve(previous)
    cards.set(host, card)
    layoutLpCard(card)
    observer?.observe(card)
  }

  function unwatch(host: HTMLElement): void {
    const card = cards.get(host)
    if (!card) return
    observer?.unobserve(card)
    dirty.delete(card)
    cards.delete(host)
  }

  const ctx: LpRenderContext = {
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

  /* ── ↻ refresh ── */

  /** Show a line under the footer (a failed refresh) for LP_ERROR_MS. */
  function flashFootError(host: HTMLElement, message: string): void {
    const foot = host.querySelector<HTMLElement>('.lp-card__foot')
    if (!foot) return
    let line = foot.querySelector<HTMLElement>('[data-lp-refresh-error]')
    if (!line) {
      line = el('span', 'lp-card__refresh-error')
      line.dataset.lpRefreshError = 'true'
      line.setAttribute('role', 'alert')
      foot.append(line)
    }
    line.textContent = message
    const shown = line
    ctx.setTimer(() => shown.remove(), LP_ERROR_MS)
  }

  async function refreshHost(host: HTMLElement): Promise<void> {
    const current = payloads.get(host)
    const call = readCall()
    const request = current?.request
    if (!current || !request || !call || refreshing.has(host)) return
    refreshing.add(host)
    const card = host.querySelector<HTMLElement>('.lp-card')
    card?.setAttribute('data-lp-refreshing', 'true')
    card?.setAttribute('aria-busy', 'true')
    const button = host.querySelector<HTMLButtonElement>('[data-lp-action="refresh"]')
    if (button) button.disabled = true
    diag('lp.refresh.start', { kind: request.kind })
    try {
      const raw = await call(lpReadMethod(request), request.params)
      const next = normalizeLpPayload(raw)
      if (!next) throw new Error(t('chat.lpUnreadable'))
      if (!host.isConnected || !rendered.has(host)) return
      // The engine echoes the request; an older one does not, and ↻ must keep working.
      if (!next.request) next.request = request
      if (!next.fetchedAt) next.fetchedAt = new Date(now()).toISOString()
      closedBy(current, next).forEach((key) => closed.add(key))
      show(host, next)
      paintAll()
      diag('lp.refresh.done', { kind: next.kind })
    } catch (error) {
      card?.removeAttribute('data-lp-refreshing')
      card?.removeAttribute('aria-busy')
      if (button) button.disabled = false
      const { code, message } = rpcError(error)
      // A position read that finds nothing: burned (or never there). The card
      // says so and its buttons stop offering a write that cannot happen.
      const gone =
        request.kind === 'position' &&
        (code === 'trading.lp.not_found' || code === 'trading.lp.position_closed')
      if (gone) {
        closed.add(
          `${String(request.params.chainId ?? '')}:${String(request.params.tokenId ?? '')}`,
        )
        paintAll()
      }
      // A card redrawn closed says so itself; anything else explains under its footer.
      const shown = payloads.get(host)
      const redrawn = shown?.kind === 'position' && shown.position.status === 'closed'
      if (!redrawn) {
        flashFootError(host, gone ? t('chat.lpErrClosed') : t('chat.lpRefreshFailed', { message }))
      }
      diag('lp.refresh.error', { error: String(error) })
    } finally {
      refreshing.delete(host)
    }
  }

  /* ── Collect / Remove ── */

  function writeKey(group: HTMLElement): string {
    return `${group.dataset.lpChainId ?? ''}:${group.dataset.lpTokenId ?? ''}`
  }

  /** Draw one write group from the state its position is in. */
  function paintWrites(group: HTMLElement): void {
    const key = writeKey(group)
    const state = writes.get(key)
    const buttons = group.querySelectorAll<HTMLButtonElement>('button[data-lp-action]')
    const label = group.querySelector<HTMLElement>('[data-lp-state]')
    const choice = group.querySelector<HTMLElement>('[data-lp-choice]')
    if (closed.has(key) && !state) {
      paintClosed(group, buttons)
      if (choice) choice.hidden = true
      if (label) {
        label.hidden = true
        label.textContent = ''
        label.removeAttribute('title')
      }
      return
    }
    // Pending or awaiting approval: no second write on the same position.
    // A collect with nothing owed stays off whatever the write state says.
    buttons.forEach((b) => (b.disabled = Boolean(state) || b.dataset.lpNoFees === 'true'))
    if (!state) {
      delete group.dataset.lpWrite
      delete group.dataset.lpWriteAction
      if (label) {
        label.hidden = true
        label.textContent = ''
        label.removeAttribute('title')
      }
      return
    }
    group.dataset.lpWrite = state.phase
    group.dataset.lpWriteAction = state.action
    if (choice) choice.hidden = true
    group
      .querySelector<HTMLElement>('[data-lp-action="remove"]')
      ?.setAttribute('aria-expanded', 'false')
    if (label) {
      label.hidden = false
      if (state.phase === 'pending') {
        label.textContent = t('chat.lpWritePending')
        label.removeAttribute('title')
      } else {
        label.textContent = t('chat.lpAwaiting', { order: shortOrder(state.orderId ?? '') })
        label.title = t('chat.lpAwaitingTitle', { order: state.orderId ?? '' })
      }
    }
  }

  /** A closed position: every button off with the reason, the closed pill on its card or row. */
  function paintClosed(group: HTMLElement, buttons: NodeListOf<HTMLButtonElement>): void {
    group.dataset.lpWrite = 'closed'
    delete group.dataset.lpWriteAction
    buttons.forEach((b) => {
      b.disabled = true
      b.title = t('chat.lpPositionClosed')
    })
    group
      .querySelector<HTMLElement>('[data-lp-action="remove"]')
      ?.setAttribute('aria-expanded', 'false')
    // The row of a positions card, else the position card itself.
    const owner =
      group.closest<HTMLElement>('.lp-row') ?? group.closest<HTMLElement>('.lp-card') ?? null
    if (!owner || owner.dataset.lpStatus === 'closed') return
    owner.dataset.lpStatus = 'closed'
    const pill = owner.querySelector<HTMLElement>('.lp-pill')
    pill?.replaceWith(statusPill('closed'))
    owner.querySelector('.lp-row__distance')?.remove()
  }

  /**
   * Redraw every open position card whose position is now known closed (and
   * has no write in flight) as a closed card: the figures it shows are gone.
   */
  function closeCards(): void {
    rendered.forEach((host) => {
      const shown = payloads.get(host)
      if (shown?.kind !== 'position' || shown.position.status === 'closed') return
      const key = positionKey(shown.position)
      if (!closed.has(key) || writes.has(key)) return
      show(host, closedPositionPayload(shown, new Date(now()).toISOString()))
    })
  }

  function paintAll(): void {
    closeCards()
    rendered.forEach((host) =>
      host.querySelectorAll<HTMLElement>('[data-lp-writes]').forEach(paintWrites),
    )
  }

  function flashWriteError(key: string, message: string): void {
    rendered.forEach((host) =>
      host.querySelectorAll<HTMLElement>('[data-lp-writes]').forEach((group) => {
        if (writeKey(group) !== key) return
        const line = group.querySelector<HTMLElement>('[data-lp-error]')
        if (!line) return
        line.textContent = message
        line.hidden = false
        ctx.setTimer(() => {
          if (line.textContent === message) {
            line.hidden = true
            line.textContent = ''
          }
        }, LP_ERROR_MS)
      }),
    )
  }

  async function write(
    host: HTMLElement,
    group: HTMLElement,
    action: 'collect' | 'remove',
    pct?: number,
  ): Promise<void> {
    const actions = getActions()
    const key = writeKey(group)
    if (!actions || writes.has(key)) return
    const tokenId = group.dataset.lpTokenId ?? ''
    const chainId = Number(group.dataset.lpChainId)
    const params: Record<string, unknown> = { tokenId, chainId }
    if (action === 'remove') params.pct = pct ?? 100
    writes.set(key, { phase: 'pending', action, pct, host })
    paintAll()
    diag('lp.write.start', { action, tokenId, chainId, pct })
    try {
      const order = orderOf(await actions.call(`trading.lp.${action}`, params))
      if (!order) throw new Error(t('chat.lpErrNoOrder'))
      const parked =
        order.status === '' ||
        order.status === 'awaiting_approval' ||
        order.status === 'approved' ||
        order.status === 'submitted'
      if (!parked || finishedEarly.delete(order.orderId)) {
        // Settled (or refused) without waiting: say how, and free the buttons.
        writes.delete(key)
        paintAll()
        if (!parked) {
          flashWriteError(
            key,
            t('chat.lpWriteEnded', {
              order: order.orderId,
              status: order.reason || order.status,
            }),
          )
        }
        return
      }
      writes.set(key, { phase: 'awaiting', action, pct, host, orderId: order.orderId })
      paintAll()
      actions.onOrder?.(order.orderId)
      diag('lp.write.parked', { action, orderId: order.orderId })
    } catch (error) {
      writes.delete(key)
      if (rpcError(error).code === 'trading.lp.position_closed') closed.add(key)
      paintAll()
      flashWriteError(key, lpWriteErrorText(error))
      diag('lp.write.error', { action, error: String(error) })
    }
  }

  /** One click handler per host: refresh, collect, the remove choice. */
  function wire(host: HTMLElement): void {
    if (wired.has(host)) return
    wired.add(host)
    host.addEventListener('click', (event) => {
      const target = (event.target as Element | null)?.closest<HTMLElement>('[data-lp-action]')
      if (!target || !host.contains(target) || (target as HTMLButtonElement).disabled) return
      const action = target.dataset.lpAction
      if (action === 'refresh') {
        void refreshHost(host)
        return
      }
      const group = target.closest<HTMLElement>('[data-lp-writes]')
      if (!group) return
      if (action === 'collect') {
        void write(host, group, 'collect')
      } else if (action === 'remove') {
        const choice = group.querySelector<HTMLElement>('[data-lp-choice]')
        if (!choice) return
        choice.hidden = !choice.hidden
        target.setAttribute('aria-expanded', choice.hidden ? 'false' : 'true')
      } else if (action === 'remove-pct') {
        void write(host, group, 'remove', Number(target.dataset.lpPct) || 100)
      }
    })
  }

  /** Render (or re-render) a payload into its host and keep it fitted. */
  function show(host: HTMLElement, payload: LpPayload): void {
    renderLp(host, payload, ctx)
    payloads.set(host, payload)
    host.querySelectorAll<HTMLElement>('[data-lp-writes]').forEach(paintWrites)
    watch(host)
  }

  /**
   * `trading.order.finished` for an order a write button parked: the buttons
   * come back and the card it was clicked on reads itself again.
   */
  function orderFinished(orderId: string): void {
    if (!orderId) return
    let matched = false
    for (const [key, state] of [...writes]) {
      if (state.orderId !== orderId) continue
      matched = true
      writes.delete(key)
      paintAll()
      if (state.host.isConnected && rendered.has(state.host)) void refreshHost(state.host)
    }
    if (!matched && [...writes.values()].some((w) => w.phase === 'pending')) {
      finishedEarly.add(orderId)
    }
  }

  function stopClock(): void {
    if (clock !== null) clearInterval(clock)
    clock = null
  }

  function tick(): void {
    pruneDetached()
    if (rendered.size === 0) {
      stopClock()
      return
    }
    const at = now()
    rendered.forEach((host) => refreshLpTimes(host, at))
  }

  function startClock(): void {
    if (clock === null) clock = setInterval(tick, LP_CLOCK_MS)
  }

  function pruneDetached(): void {
    for (const host of [...claimed]) {
      if (!host.isConnected) {
        claimed.delete(host)
        rendered.delete(host)
        payloads.delete(host)
        unwatch(host)
      }
    }
  }

  function setStatus(host: HTMLElement, message: string): void {
    const status = host.querySelector<HTMLElement>('.msg-artifact-lp__status')
    if (!status) return
    status.textContent = message
    status.hidden = message === ''
  }

  async function mountOne(host: HTMLElement): Promise<void> {
    const url = host.dataset.lpSrc || ''
    if (!url) {
      setStatus(host, t('chat.lpUnavailable'))
      return
    }
    try {
      diag('lp.mount.start', { url })
      const raw = await deps.fetchPayload(url)
      const payload = normalizeLpPayload(raw)
      if (!payload) {
        setStatus(host, t('chat.lpUnreadable'))
        diag('lp.mount.empty', { url })
        return
      }
      // The row can be rebuilt while the payload is in flight.
      if (!host.isConnected || !claimed.has(host)) {
        claimed.delete(host)
        return
      }
      wire(host)
      rendered.add(host)
      show(host, payload)
      setStatus(host, '')
      startClock()
      diag('lp.mount.done', { url, kind: payload.kind })
    } catch (error) {
      setStatus(host, t('chat.lpFailed'))
      diag('lp.mount.error', { url, error: String(error) })
    }
  }

  /** Mount every not-yet-mounted LP placeholder inside `root`. */
  function mountLp(root: HTMLElement | null | undefined): void {
    if (!root) return
    pruneDetached()
    root.querySelectorAll<HTMLElement>('[data-lp-src]').forEach((host) => {
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
    writes.clear()
    finishedEarly.clear()
    closed.clear()
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

  return { mountLp, destroyAll, pruneDetached, orderFinished, refresh: refreshHost }
}

export type LpMounter = ReturnType<typeof createLpMounter>
