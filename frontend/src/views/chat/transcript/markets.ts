// Chat transcript — markets cards: every pool a token trades in.
//
// `agentos trade markets <token> --json` publishes a JSON artifact with the
// `application/vnd.agentos.markets+json` mime; the artifact renderer emits a
// mount placeholder for it and this module fetches the payload and draws one
// card: the token's head, then two sections — pools where the token is the
// quote asset ("Priced in NVDA") and pools where it is the base ("NVDA priced
// in") — then a footer with the counts, ↻, *Show lookalikes*, *Deeper* and
// *Read again*.
// docs/markets.md is the contract.
//
// Same two surfaces as lp.ts:
//   1. Pure helpers (top-level exports) — mime match, payload normalization,
//      labels and formatting. No network; the desktop reuses them for its own
//      Markets tab (it shares logic, never markup).
//   2. `createMarketsMounter(deps)` — the imperative mounter the transcript
//      composes next to the LP mounter.
//
// Styling: the markup carries the `.mk-*` classes the contract lists verbatim
// ("CSS hooks"). Only the desktop mounts this markup and skins the hooks in its
// own chat.css; the web console renders its own React cards
// (web/features/chat/cards/) from the pure helpers.
//
// SECURITY: every payload-derived string reaches the DOM through `textContent`
// or an attribute setter, never `innerHTML` — token symbols and names are
// attacker-chosen on a permissionless chain. Links and logos are used only when
// they parse as http(s) URLs, so a payload cannot smuggle `javascript:`.

import { t, tPlural } from '@/i18n'
import '@/i18n/en/chat'

import type { Artifact } from './artifacts'
import {
  LP_CLOCK_MS,
  LP_ERROR_MS,
  NO_VALUE,
  formatPrice,
  formatSignedPct,
  formatUsd,
  formatUsdCompact,
  relativeTime,
  safeExplorerBase,
  shortAddress,
} from './lp'

/** The mime the engine publishes a markets read-out under. */
export const MARKETS_ARTIFACT_MIME = 'application/vnd.agentos.markets+json'

/** The gateway read a card's ↻, *Show lookalikes* and *Deeper* re-run. */
export const MARKETS_METHOD = 'trading.markets'

/** Rows drawn per section before the "+N more" control. */
export const MARKETS_ROW_CAP = 40

/** USD figures from here up print compact ($4.7M); below, whole dollars. */
export const MARKETS_COMPACT_FROM = 100_000

/**
 * The card's DOM, as `buildMarketsCard` writes it — the nesting the desktop
 * skin matches. `?` marks a node drawn only sometimes (the note says when);
 * `|` separates alternatives. Every class and data attribute here is a stable
 * hook (docs/markets.md "CSS hooks").
 */
export const MARKETS_DOM = `
div.msg-artifact-markets[data-markets-src][data-markets-host=rendered]   (the transcript placeholder)
  div.msg-artifact-markets__body
    article.mk-card[data-chain]  [data-partial=true]?  [data-refreshing=true][aria-busy=true]? (re-run in flight)
      header.mk-head
        img.mk-logo | span.mk-logo[aria-hidden]           (letter fallback)
        span.mk-symbol
        span.mk-name?                                     (token has a name)
        span.mk-chain[data-chain-id]?                     (chain known)
        span.mk-price
        span.mk-oracle?                                   (Stock Token with an oracle price)
          span.mk-oracle-badge[data-tone=warn|danger]?    (stale | paused)
      section.mk-section[data-side=quote|base]            (quote first; one only on a one-sided read)
        h3.mk-section-title
          span.mk-section-count
        p.mk-empty?                                       (no rows: nothing else follows in the section)
        div.mk-cols[aria-hidden]
          span.mk-col.mk-col--pair  .mk-col--tvl  .mk-col--vol  .mk-col--px  .mk-col--change  .mk-col--age
        ul.mk-rows
          li.mk-row[data-side=quote|base]  [data-lookalike=true]?  [data-pool]?
            div.mk-market
              a.mk-pair[href][target=_blank] | span.mk-pair  (no pool URL)
              span.mk-cp-name?                            (lookalike, or counterparty symbol == token symbol)
              div.mk-venue
                span.mk-dex[data-dex]
                  span.mk-version?
                span.mk-launcher?                         (launcher set and not the DEX label)
                span.mk-flags?                            (at least one flag)
                  span.mk-flag[data-kind=uni|stock|lookalike]
            span.mk-tvl
            span.mk-vol
            div.mk-pricing
              span.mk-px
              span.mk-px-in?                              (priceInToken known)
              span.mk-premium[data-tone=up|down|flat]?    (base rows of a Stock Token)
            span.mk-change[data-tone=up|down|flat]
            span.mk-age
            button.mk-swap[data-action=swap][data-chain-id][data-token-in][data-token-out]?  (onSwap given)
        button.mk-more[data-action=more]?                 (more than MARKETS_ROW_CAP rows)
      ul.mk-warnings?
        li.mk-warning
      footer.mk-foot
        span.mk-meta
          span.mk-counts
          span.mk-partial?
          time.mk-ago[datetime][data-mk-fetched-at]?
        span.mk-actions?                                  (the mounter has a gateway call)
          button.mk-link[data-action=lookalikes]?         (lookalikesOffered)
          button.mk-link[data-action=deep]?               (deeperOffered)
          button.mk-link[data-action=again]?              (readAgainOffered: a deep read cut short by a 429)
          button.mk-refresh[data-action=refresh]
        span.mk-error[role=alert]?                        (a failed re-run, for a few seconds)
  p.msg-artifact-markets__status                          (loading / error line; hidden once rendered)
`

/* ── Payload shape (docs/markets.md "Payload") ──────────────────────────── */

export type MarketsSide = 'quote' | 'base'

export interface MarketsChain {
  id: number
  key: string
  name: string
  /** An http(s) explorer origin, or ''. */
  explorer: string
}

export interface MarketsOracle {
  usd: number | null
  updatedAt: string | null
  ageSeconds: number | null
  stale: boolean
  paused: boolean
}

/** What a token and a counterparty both carry. */
interface MarketsTokenBase {
  address: string
  symbol: string
  name: string
  decimals: number | null
  /** An http(s) URL, or null. */
  logoUrl: string | null
  verified: boolean
  stockToken: boolean
}

/** The token the card is about (`NVDA`). */
export interface MarketsToken extends MarketsTokenBase {
  priceUsd: number | null
  /** The Chainlink feed of a Stock Token on Robinhood Chain; null otherwise. */
  oracle: MarketsOracle | null
}

/** The other token in a pool (`AI` in `AI/NVDA`). */
export interface MarketsCounterparty extends MarketsTokenBase {
  lookalike: boolean
  /** The chain's native coin (ETH at the zero address), never "WETH". */
  native: boolean
}

export interface MarketsDex {
  id: string
  label: string
  version: string | null
}

/** What a row's Swap button prefills: sell the token, buy the counterparty. */
export interface MarketsSwap {
  chainId: number
  tokenIn: string
  tokenOut: string
}

export interface MarketsPool {
  poolAddress: string
  pair: string
  side: MarketsSide
  dex: MarketsDex
  launcher: string | null
  viaUniswap: boolean
  feePct: number | null
  counterparty: MarketsCounterparty
  tvlUsd: number | null
  volume24hUsd: number | null
  txns24h: { buys: number; sells: number } | null
  priceUsd: number | null
  priceInToken: number | null
  change24hPct: number | null
  premiumPct: number | null
  createdAt: string | null
  /** An http(s) URL (GeckoTerminal), or ''. */
  url: string
  swap: MarketsSwap | null
}

export interface MarketsCounts {
  scanned: number
  shown: number
  belowMinTvl: number
  hiddenLookalikes: number
  /** Rows above the floor that `limit` cut (0 on payloads that predate it). */
  limited: number
  pages: number
  pageCap: number
  /**
   * The page cap, not `limit`, ended a read that had more pools — the only
   * case *Deeper* is offered (false on payloads that predate it).
   */
  pageCapHit: boolean
  /**
   * A 429 cut the read short — *Deeper* (or *Read again* on a deep read) is
   * offered so the user can retry after a minute (false when absent).
   */
  rateLimited: boolean
}

export interface MarketsRequest {
  kind: 'markets'
  params: Record<string, unknown>
}

export interface MarketsPayload {
  version: number
  kind: 'markets'
  chain: MarketsChain | null
  fetchedAt: string
  partial: boolean
  warnings: string[]
  token: MarketsToken
  counts: MarketsCounts
  sections: { quote: MarketsPool[]; base: MarketsPool[] }
  request: MarketsRequest | null
}

/* ── Pure helpers: mime + normalization ─────────────────────────────────── */

/** True when the artifact should render as a markets card. */
export function isMarketsArtifact(artifact: Artifact | null | undefined): boolean {
  if (!artifact || !artifact.mime) return false
  return String(artifact.mime).toLowerCase().split(';')[0]?.trim() === MARKETS_ARTIFACT_MIME
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

/** A finite number, or null. Numeric strings count; unknown is never 0. */
function num(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function count(value: unknown): number {
  const n = num(value)
  return n !== null && n > 0 ? Math.floor(n) : 0
}

const ADDRESS = /^0x[0-9a-fA-F]{40}$/

/** The zero address: a counterparty there is the chain's native coin. */
const ZERO_ADDRESS = /^0x0{40}$/i

/** An http(s) URL as given, or ''. */
function safeUrl(value: unknown): string {
  const raw = text(value)
  if (!raw) return ''
  try {
    const url = new URL(raw)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : ''
  } catch {
    return ''
  }
}

/** Launchpads by DEX id prefix (docs/markets.md: the table is best-effort). */
const LAUNCHERS: Array<[string, string]> = [
  ['bankr', 'Bankr'],
  ['pons', 'Pons'],
  ['clanker', 'Clanker'],
  ['long', 'long.xyz'],
  ['virtuals', 'Virtuals'],
]

/** DEX names spelt the way the venues spell them. */
const DEX_NAMES: Record<string, string> = {
  uniswap: 'Uniswap',
  aerodrome: 'Aerodrome',
  pancakeswap: 'PancakeSwap',
  sushiswap: 'SushiSwap',
  baseswap: 'BaseSwap',
  ramses: 'Ramses',
  bankr: 'Bankr',
  pons: 'Pons',
  clanker: 'Clanker',
  long: 'long.xyz',
  virtuals: 'Virtuals',
}

/** Chain and filler words in a GeckoTerminal DEX id, never part of the name. */
const DEX_NOISE = new Set(['robinhood', 'base', 'dex', 'chain'])

function dexWords(dexId: string): string[] {
  return dexId
    .toLowerCase()
    .split(/[-_\s]+/)
    .filter(Boolean)
}

/**
 * A DEX id's display name: `uniswap-v4-robinhood` → "Uniswap",
 * `bankr-robinhood` → "Bankr", `pons-v2-dex` → "Pons". Unknown venues are
 * title-cased from the id; an empty id is "—".
 */
export function dexLabel(dexId: string): string {
  const words = dexWords(dexId).filter((w) => !DEX_NOISE.has(w) && !/^v\d+$/.test(w))
  if (words.length === 0) return dexId.trim() || NO_VALUE
  const known = DEX_NAMES[words[0] as string]
  if (known) return known
  return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

/** The protocol version a DEX id names (`ramses-v3-robinhood` → "v3"), or null. */
export function dexVersion(dexId: string): string | null {
  return dexWords(dexId).find((w) => /^v\d+$/.test(w)) ?? null
}

/**
 * The launchpad a pool came from: the engine's `launcher` when it set one,
 * else the DEX id's prefix (`bankr-*` → "Bankr"), else null.
 */
export function launcherLabel(pool: {
  launcher?: string | null
  dex?: { id?: string } | null
}): string | null {
  const given = text(pool.launcher)
  if (given) return given
  const first = dexWords(pool.dex?.id ?? '')[0] ?? ''
  return LAUNCHERS.find(([prefix]) => prefix === first)?.[1] ?? null
}

/**
 * A row's pair, counterparty first on quote rows (`AI/NVDA`) and the token
 * first on base rows (`NVDA/USDG`). The engine's `pair` wins when it sent one.
 */
export function pairLabel(
  pool: { pair?: string; side: MarketsSide; counterparty: { symbol: string } },
  tokenSymbol: string,
): string {
  const given = text(pool.pair)
  if (given) return given
  const other = pool.counterparty.symbol || '?'
  const self = tokenSymbol || '?'
  return pool.side === 'quote' ? `${other}/${self}` : `${self}/${other}`
}

function normChain(value: unknown): MarketsChain | null {
  const row = obj(value)
  if (!row) return null
  const key = text(row.key)
  const name = text(row.name) || key
  if (!name) return null
  return { id: num(row.id) ?? 0, key, name, explorer: safeExplorerBase(row.explorer) }
}

function normTokenBase(row: Obj): MarketsTokenBase {
  const address = text(row.address)
  return {
    address,
    symbol: text(row.symbol) || shortAddress(address) || '?',
    name: text(row.name),
    decimals: num(row.decimals),
    logoUrl: safeUrl(row.logoUrl) || null,
    verified: row.verified === true,
    stockToken: row.stockToken === true,
  }
}

function normOracle(value: unknown): MarketsOracle | null {
  const row = obj(value)
  if (!row) return null
  return {
    usd: num(row.usd),
    updatedAt: textOrNull(row.updatedAt),
    ageSeconds: num(row.ageSeconds),
    stale: row.stale === true,
    paused: row.paused === true,
  }
}

function normToken(value: unknown): MarketsToken | null {
  const row = obj(value)
  if (!row) return null
  const base = normTokenBase(row)
  if (!base.address && !text(row.symbol)) return null
  return { ...base, priceUsd: num(row.priceUsd), oracle: normOracle(row.oracle) }
}

function normSwap(value: unknown): MarketsSwap | null {
  const row = obj(value)
  if (!row) return null
  const chainId = num(row.chainId)
  const tokenIn = text(row.tokenIn)
  const tokenOut = text(row.tokenOut)
  if (chainId === null || !ADDRESS.test(tokenIn) || !ADDRESS.test(tokenOut)) return null
  return { chainId, tokenIn, tokenOut }
}

function normPool(value: unknown, side: MarketsSide, tokenSymbol: string): MarketsPool | null {
  const row = obj(value)
  if (!row) return null
  const cpRow = obj(row.counterparty)
  if (!cpRow) return null
  const native = cpRow.native === true || ZERO_ADDRESS.test(text(cpRow.address))
  const cpBase = normTokenBase(cpRow)
  const counterparty: MarketsCounterparty = {
    ...cpBase,
    // The native coin is named by its symbol, never by a "0x0000…0000" fallback.
    symbol: native && !text(cpRow.symbol) ? 'ETH' : cpBase.symbol,
    name: native && !cpBase.name ? 'Ether' : cpBase.name,
    lookalike: cpRow.lookalike === true,
    native,
  }
  const dexRow = obj(row.dex) ?? {}
  const dexId = text(dexRow.id)
  const dex: MarketsDex = {
    id: dexId,
    label: text(dexRow.label) || dexLabel(dexId),
    version: textOrNull(dexRow.version) ?? dexVersion(dexId),
  }
  const txns = obj(row.txns24h)
  const pool: MarketsPool = {
    poolAddress: text(row.poolAddress),
    pair: '',
    // The section a row sits in is its side; a row that disagrees is misfiled.
    side,
    dex,
    launcher: null,
    viaUniswap: typeof row.viaUniswap === 'boolean' ? row.viaUniswap : dexId.startsWith('uniswap-'),
    feePct: num(row.feePct),
    counterparty,
    tvlUsd: num(row.tvlUsd),
    volume24hUsd: num(row.volume24hUsd),
    txns24h: txns ? { buys: count(txns.buys), sells: count(txns.sells) } : null,
    priceUsd: num(row.priceUsd),
    priceInToken: num(row.priceInToken),
    change24hPct: num(row.change24hPct),
    premiumPct: side === 'base' ? num(row.premiumPct) : null,
    createdAt: textOrNull(row.createdAt),
    url: safeUrl(row.url),
    swap: normSwap(row.swap),
  }
  pool.launcher = launcherLabel({ launcher: textOrNull(row.launcher), dex })
  // A native counterparty's pair is always rebuilt from symbols, so an engine
  // pair spelt with the zero address never reaches the card.
  pool.pair = pairLabel({ pair: native ? '' : text(row.pair), side, counterparty }, tokenSymbol)
  return pool
}

/** Rows of one section, biggest TVL first (unknown TVL last). */
function normSection(value: unknown, side: MarketsSide, tokenSymbol: string): MarketsPool[] {
  if (!Array.isArray(value)) return []
  const rows = value
    .map((item) => normPool(item, side, tokenSymbol))
    .filter((p): p is MarketsPool => p !== null)
  return rows.sort((a, b) => (b.tvlUsd ?? -Infinity) - (a.tvlUsd ?? -Infinity))
}

/** The request a card re-runs; only `trading.markets` can be built from it. */
export function normalizeMarketsRequest(value: unknown): MarketsRequest | null {
  const row = obj(value)
  if (!row || text(row.kind) !== 'markets') return null
  return { kind: 'markets', params: { ...(obj(row.params) ?? {}) } }
}

/** A markets payload in the contract's shape, or null when it is not one. */
export function normalizeMarketsPayload(raw: unknown): MarketsPayload | null {
  const row = obj(raw)
  if (!row || text(row.kind) !== 'markets') return null
  const token = normToken(row.token)
  if (!token) return null
  const sections = obj(row.sections) ?? {}
  const counts = obj(row.counts) ?? {}
  const quote = normSection(sections.quote, 'quote', token.symbol)
  const base = normSection(sections.base, 'base', token.symbol)
  return {
    version: num(row.version) ?? 1,
    kind: 'markets',
    chain: normChain(row.chain),
    fetchedAt: text(row.fetchedAt),
    partial: row.partial === true,
    warnings: Array.isArray(row.warnings) ? row.warnings.map(text).filter(Boolean) : [],
    token,
    counts: {
      scanned: count(counts.scanned),
      shown: num(counts.shown) === null ? quote.length + base.length : count(counts.shown),
      belowMinTvl: count(counts.belowMinTvl),
      hiddenLookalikes: count(counts.hiddenLookalikes),
      limited: count(counts.limited),
      pages: count(counts.pages),
      pageCap: count(counts.pageCap),
      pageCapHit: counts.pageCapHit === true,
      rateLimited: counts.rateLimited === true,
    },
    sections: { quote, base },
    request: normalizeMarketsRequest(row.request),
  }
}

/* ── Pure helpers: formatting ───────────────────────────────────────────── */

/**
 * USD for the table: compact from $100K up ($4.73M), whole dollars with
 * grouping from $1,000 ($64,210), and `formatUsd` below that ($0.1131,
 * sub-cent subscripts). Null is "—", never "$0".
 */
export function formatMarketsUsd(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return NO_VALUE
  const abs = Math.abs(value)
  if (abs >= MARKETS_COMPACT_FROM) return formatUsdCompact(value)
  if (abs >= 1000) {
    return `${value < 0 ? '-' : ''}$${Math.round(abs).toLocaleString('en-US')}`
  }
  return formatUsd(value)
}

/**
 * A pool's age from its creation stamp: "45m", "5h", "3d", "2mo", "1y".
 * A stamp in the future (clock skew) is "0m"; an unusable one is "—".
 */
export function formatAge(iso: string | null | undefined, now: number): string {
  const at = iso ? Date.parse(iso) : NaN
  if (Number.isNaN(at)) return NO_VALUE
  const minutes = Math.max(0, Math.floor((now - at) / 60_000))
  if (minutes < 60) return t('chat.marketsAgeMinutes', { count: minutes })
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return t('chat.marketsAgeHours', { count: hours })
  const days = Math.floor(hours / 24)
  if (days < 30) return t('chat.marketsAgeDays', { count: days })
  if (days < 365) return t('chat.marketsAgeMonths', { count: Math.floor(days / 30) })
  return t('chat.marketsAgeYears', { count: Math.floor(days / 365) })
}

/** `up` / `down` / `flat` for a signed percent, matching what one decimal shows. */
export function changeTone(pct: number | null): 'up' | 'down' | 'flat' {
  if (pct === null || !Number.isFinite(pct) || Math.abs(pct) < 0.05) return 'flat'
  return pct > 0 ? 'up' : 'down'
}

/**
 * A signed percent at one decimal ("+0.4%", "−3.2%"), but never a signed
 * zero: anything that rounds to 0.0 (-0.004, +0.04) is plain "0.0%", the
 * text `changeTone` calls `flat`. Null is "—".
 */
export function formatMarketsPct(pct: number | null): string {
  if (pct === null || !Number.isFinite(pct)) return NO_VALUE
  return changeTone(pct) === 'flat' ? '0.0%' : formatSignedPct(pct)
}

/** The section titles: "Priced in NVDA" (quote) and "NVDA priced in" (base). */
export function sectionTitle(side: MarketsSide, symbol: string): string {
  return side === 'quote'
    ? t('chat.marketsQuoteTitle', { symbol })
    : t('chat.marketsBaseTitle', { symbol })
}

/** The `minTvlUsd` a payload was read with (the engine's default when absent). */
export function minTvlOf(payload: MarketsPayload): number {
  const value = num(payload.request?.params.minTvlUsd)
  return value !== null && value >= 0 ? value : 10_000
}

/**
 * The counts line: "34 of 100 pools shown · 61 under $10K · 12 more over the
 * limit · 5 lookalikes hidden". Zero parts are left out.
 */
export function countsText(payload: MarketsPayload): string {
  const { counts } = payload
  const parts = [t('chat.marketsCountsShown', { shown: counts.shown, scanned: counts.scanned })]
  if (counts.belowMinTvl > 0) {
    parts.push(
      t('chat.marketsCountsBelow', {
        count: counts.belowMinTvl,
        min: formatUsdCompact(minTvlOf(payload)),
      }),
    )
  }
  if (counts.limited > 0) {
    parts.push(t('chat.marketsCountsLimited', { count: counts.limited }))
  }
  if (counts.hiddenLookalikes > 0) {
    parts.push(tPlural('chat.marketsCountsLookalikes', counts.hiddenLookalikes))
  }
  return parts.join(' · ')
}

/**
 * The launcher pill a row wears: its launcher, unless that only repeats the
 * DEX label ("Bankr" on Bankr, "Pons" on Pons v2) — then null.
 */
export function visibleLauncher(pool: {
  launcher: string | null
  dex: { label: string }
}): string | null {
  const launcher = text(pool.launcher)
  if (!launcher) return null
  return launcher.toLowerCase() === text(pool.dex.label).toLowerCase() ? null : launcher
}

/**
 * True when a row should name its counterparty after the pair: a lookalike,
 * or a counterparty spelling the token's own symbol (case-insensitive), so
 * two "GME/GME" rows can be told apart.
 */
export function showsCounterpartyName(
  pool: { counterparty: { symbol: string; name: string; lookalike: boolean } },
  tokenSymbol: string,
): boolean {
  const cp = pool.counterparty
  if (!cp.name) return false
  return cp.lookalike || cp.symbol.trim().toLowerCase() === tokenSymbol.trim().toLowerCase()
}

/** *Show lookalikes* is offered: some were hidden and the read did not ask for them. */
export function lookalikesOffered(payload: MarketsPayload): boolean {
  return payload.request?.params.lookalikes !== true && payload.counts.hiddenLookalikes > 0
}

/**
 * *Deeper* is offered on a read that was not already deep when the page cap
 * (not `limit`) ended it with more pools left, or a 429 cut it short.
 */
export function deeperOffered(payload: MarketsPayload): boolean {
  const { counts } = payload
  return payload.request?.params.deep !== true && (counts.pageCapHit || counts.rateLimited)
}

/**
 * *Read again* is offered on a deep read a 429 cut short — *Deeper* has
 * nothing more to give there, but the same read a minute later may finish.
 */
export function readAgainOffered(payload: MarketsPayload): boolean {
  return payload.request?.params.deep === true && payload.counts.rateLimited
}

/** Which sections a payload draws: both, unless it was read for one side. */
export function visibleSides(payload: MarketsPayload): MarketsSide[] {
  const side = text(payload.request?.params.side)
  if (side === 'quote') return ['quote']
  if (side === 'base') return ['base']
  return ['quote', 'base']
}

/* ── DOM builders ───────────────────────────────────────────────────────── */

function el(tag: string, className: string, content?: string): HTMLElement {
  const node = document.createElement(tag)
  if (className) node.className = className
  // textContent, never innerHTML — see the security note at the top.
  if (content !== undefined) node.textContent = content
  return node
}

function button(className: string, label: string, title: string): HTMLButtonElement {
  const node = el('button', className, label) as HTMLButtonElement
  node.type = 'button'
  node.title = title
  return node
}

/** Everything the DOM builders need from the mounter. */
export interface MarketsRenderContext {
  now: () => number
  /** Offer ↻, *Show lookalikes*, *Deeper* and *Read again* (a call exists and the payload echoes its request). */
  readonly canRefresh?: boolean
  /** Draw a Swap button on each row (the mounter was given `onSwap`). */
  readonly canSwap?: boolean
}

function logoNode(token: MarketsTokenBase): HTMLElement {
  if (token.logoUrl) {
    const img = el('img', 'mk-logo') as HTMLImageElement
    img.alt = ''
    img.loading = 'lazy'
    img.decoding = 'async'
    img.referrerPolicy = 'no-referrer'
    img.src = token.logoUrl
    img.addEventListener('error', () => img.replaceWith(letterLogo(token)), { once: true })
    return img
  }
  return letterLogo(token)
}

function letterLogo(token: MarketsTokenBase): HTMLElement {
  const node = el('span', 'mk-logo', (token.symbol.charAt(0) || '?').toUpperCase())
  node.setAttribute('aria-hidden', 'true')
  return node
}

function head(payload: MarketsPayload): HTMLElement {
  const { token, chain } = payload
  const node = el('header', 'mk-head')
  node.append(logoNode(token), el('span', 'mk-symbol', token.symbol))
  if (token.name) node.append(el('span', 'mk-name', token.name))
  if (chain) {
    const pill = el('span', 'mk-chain', chain.name)
    if (chain.id) pill.dataset.chainId = String(chain.id)
    node.append(pill)
  }
  node.append(el('span', 'mk-price', formatUsd(token.priceUsd)))
  const oracle = token.oracle
  if (token.stockToken && oracle && oracle.usd !== null) {
    const line = el('span', 'mk-oracle', t('chat.marketsOracle', { price: formatUsd(oracle.usd) }))
    if (oracle.updatedAt) line.title = t('chat.marketsOracleTitle', { at: oracle.updatedAt })
    if (oracle.paused || oracle.stale) {
      const badge = el(
        'span',
        'mk-oracle-badge',
        oracle.paused ? t('chat.marketsOraclePaused') : t('chat.marketsOracleStale'),
      )
      badge.dataset.tone = oracle.paused ? 'danger' : 'warn'
      line.append(' ', badge)
    }
    node.append(line)
  }
  return node
}

function columns(): HTMLElement {
  const node = el('div', 'mk-cols')
  node.setAttribute('aria-hidden', 'true')
  node.append(
    el('span', 'mk-col mk-col--pair', t('chat.marketsColPair')),
    el('span', 'mk-col mk-col--tvl', t('chat.marketsColTvl')),
    el('span', 'mk-col mk-col--vol', t('chat.marketsColVol')),
    el('span', 'mk-col mk-col--px', t('chat.marketsColPrice')),
    el('span', 'mk-col mk-col--change', t('chat.marketsColChange')),
    el('span', 'mk-col mk-col--age', t('chat.marketsColAge')),
  )
  return node
}

function flag(kind: 'uni' | 'stock' | 'lookalike', label: string, title: string): HTMLElement {
  const node = el('span', 'mk-flag', label)
  node.dataset.kind = kind
  node.title = title
  return node
}

function rowNode(
  pool: MarketsPool,
  payload: MarketsPayload,
  ctx: MarketsRenderContext,
): HTMLElement {
  const { token } = payload
  const cp = pool.counterparty
  const row = el('li', 'mk-row')
  row.dataset.side = pool.side
  if (cp.lookalike) row.dataset.lookalike = 'true'
  if (pool.poolAddress) row.dataset.pool = pool.poolAddress

  // Market: the pair (a link to the pool when there is one), then where it trades.
  const market = el('div', 'mk-market')
  const pair = pool.url
    ? (el('a', 'mk-pair', pool.pair) as HTMLAnchorElement)
    : el('span', 'mk-pair', pool.pair)
  if (pair instanceof HTMLAnchorElement) {
    pair.href = pool.url
    pair.target = '_blank'
    pair.rel = 'noopener noreferrer'
  }
  pair.title = [cp.name, pool.poolAddress].filter(Boolean).join(' · ')
  market.append(pair)
  if (showsCounterpartyName(pool, token.symbol)) {
    const name = el('span', 'mk-cp-name', cp.name)
    if (!cp.native) name.title = cp.address
    market.append(name)
  }
  const venue = el('div', 'mk-venue')
  const dex = el('span', 'mk-dex', pool.dex.label)
  if (pool.dex.id) dex.dataset.dex = pool.dex.id
  if (pool.dex.version) dex.append(' ', el('span', 'mk-version', pool.dex.version))
  if (pool.feePct !== null) dex.title = t('chat.marketsFeeTitle', { fee: String(pool.feePct) })
  venue.append(dex)
  const launcherText = visibleLauncher(pool)
  if (launcherText) {
    const launcher = el('span', 'mk-launcher', launcherText)
    launcher.title = t('chat.marketsLauncherTitle', { launcher: launcherText })
    venue.append(launcher)
  }
  const flags = el('span', 'mk-flags')
  if (pool.viaUniswap)
    flags.append(flag('uni', t('chat.marketsFlagUni'), t('chat.marketsFlagUniTitle')))
  if (cp.stockToken) {
    flags.append(flag('stock', t('chat.marketsFlagStock'), t('chat.marketsFlagStockTitle')))
  }
  if (cp.lookalike) {
    flags.append(
      flag('lookalike', t('chat.marketsFlagLookalike'), t('chat.marketsFlagLookalikeTitle')),
    )
  }
  if (flags.childElementCount) venue.append(flags)
  market.append(venue)
  row.append(market)

  const tvl = el('span', 'mk-tvl', formatMarketsUsd(pool.tvlUsd))
  tvl.title = t('chat.marketsTvlTitle')
  const vol = el('span', 'mk-vol', formatMarketsUsd(pool.volume24hUsd))
  vol.title = pool.txns24h
    ? t('chat.marketsTxnsTitle', { buys: pool.txns24h.buys, sells: pool.txns24h.sells })
    : t('chat.marketsVolTitle')
  row.append(tvl, vol)

  // Price: USD, then in the other token — the counterparty priced in the token
  // on quote rows, the token priced in the counterparty on base rows.
  const price = el('div', 'mk-pricing')
  price.append(el('span', 'mk-px', formatUsd(pool.priceUsd)))
  if (pool.priceInToken !== null) {
    const unit = pool.side === 'quote' ? token.symbol : cp.symbol
    price.append(
      el(
        'span',
        'mk-px-in',
        t('chat.marketsPriceIn', { price: formatPrice(pool.priceInToken), symbol: unit }),
      ),
    )
  }
  if (pool.side === 'base' && pool.premiumPct !== null) {
    const pct = formatMarketsPct(pool.premiumPct)
    const premium = el('span', 'mk-premium', t('chat.marketsPremium', { pct }))
    premium.dataset.tone = changeTone(pool.premiumPct)
    premium.title = t('chat.marketsPremiumTitle', { symbol: token.symbol, pct })
    price.append(premium)
  }
  row.append(price)

  const change = el('span', 'mk-change', formatMarketsPct(pool.change24hPct))
  change.dataset.tone = changeTone(pool.change24hPct)
  change.title = t('chat.marketsChangeTitle')
  const age = el('span', 'mk-age', formatAge(pool.createdAt, ctx.now()))
  if (pool.createdAt) age.title = t('chat.marketsAgeTitle', { at: pool.createdAt })
  row.append(change, age)

  if (ctx.canSwap && pool.swap) {
    const swap = button(
      'mk-swap',
      t('chat.marketsSwap'),
      t('chat.marketsSwapTitle', {
        tokenIn:
          pool.swap.tokenIn.toLowerCase() === cp.address.toLowerCase() ? cp.symbol : token.symbol,
        tokenOut:
          pool.swap.tokenOut.toLowerCase() === cp.address.toLowerCase() ? cp.symbol : token.symbol,
      }),
    )
    swap.dataset.action = 'swap'
    swap.dataset.chainId = String(pool.swap.chainId)
    swap.dataset.tokenIn = pool.swap.tokenIn
    swap.dataset.tokenOut = pool.swap.tokenOut
    row.append(swap)
  }
  return row
}

function section(
  side: MarketsSide,
  payload: MarketsPayload,
  ctx: MarketsRenderContext,
): HTMLElement {
  const pools = payload.sections[side]
  const symbol = payload.token.symbol
  const node = el('section', 'mk-section')
  node.dataset.side = side
  const title = el('h3', 'mk-section-title', sectionTitle(side, symbol))
  title.append(' ', el('span', 'mk-section-count', String(pools.length)))
  node.append(title)
  if (pools.length === 0) {
    const min = minTvlOf(payload)
    node.append(
      el(
        'p',
        'mk-empty',
        min > 0
          ? t('chat.marketsEmpty', { symbol, min: formatUsdCompact(min) })
          : t('chat.marketsEmptyAny', { symbol }),
      ),
    )
    return node
  }
  node.append(columns())
  const rows = el('ul', 'mk-rows')
  pools.slice(0, MARKETS_ROW_CAP).forEach((pool) => rows.append(rowNode(pool, payload, ctx)))
  node.append(rows)
  const rest = pools.slice(MARKETS_ROW_CAP)
  if (rest.length) {
    // "+N more" expands in place: the remaining rows join the same list.
    const more = el('button', 'mk-more', t('chat.marketsMore', { count: rest.length }))
    ;(more as HTMLButtonElement).type = 'button'
    more.dataset.action = 'more'
    more.addEventListener('click', () => {
      rest.forEach((pool) => rows.append(rowNode(pool, payload, ctx)))
      more.remove()
    })
    node.append(more)
  }
  return node
}

function foot(payload: MarketsPayload, ctx: MarketsRenderContext): HTMLElement {
  const node = el('footer', 'mk-foot')
  const meta = el('span', 'mk-meta')
  meta.append(el('span', 'mk-counts', countsText(payload)))
  if (payload.partial) {
    const badge = el('span', 'mk-partial', t('chat.marketsPartial'))
    badge.title = payload.warnings.length
      ? payload.warnings.join('\n')
      : t('chat.marketsPartialTitle')
    meta.append(badge)
  }
  const ago = relativeTime(payload.fetchedAt, ctx.now())
  if (ago) {
    const time = el('time', 'mk-ago', ago)
    time.setAttribute('datetime', payload.fetchedAt)
    time.dataset.mkFetchedAt = payload.fetchedAt
    time.title = payload.fetchedAt
    meta.append(time)
  }
  node.append(meta)

  const actions = el('span', 'mk-actions')
  if (ctx.canRefresh && payload.request) {
    if (lookalikesOffered(payload)) {
      const link = button(
        'mk-link',
        t('chat.marketsShowLookalikes'),
        t('chat.marketsShowLookalikesTitle'),
      )
      link.dataset.action = 'lookalikes'
      actions.append(link)
    }
    if (deeperOffered(payload)) {
      const link = button('mk-link', t('chat.marketsDeeper'), t('chat.marketsDeeperTitle'))
      link.dataset.action = 'deep'
      actions.append(link)
    }
    if (readAgainOffered(payload)) {
      const link = button('mk-link', t('chat.marketsReadAgain'), t('chat.marketsReadAgainTitle'))
      link.dataset.action = 'again'
      actions.append(link)
    }
    const refresh = button(
      'mk-refresh',
      `↻ ${t('chat.marketsRefresh')}`,
      t('chat.marketsRefreshTitle'),
    )
    refresh.setAttribute('aria-label', t('chat.marketsRefreshTitle'))
    refresh.dataset.action = 'refresh'
    actions.append(refresh)
  }
  if (actions.childElementCount) node.append(actions)
  return node
}

/** The whole card for one payload. */
export function buildMarketsCard(payload: MarketsPayload, ctx: MarketsRenderContext): HTMLElement {
  const card = el('article', 'mk-card')
  if (payload.chain?.id) card.dataset.chain = String(payload.chain.id)
  if (payload.partial) card.dataset.partial = 'true'
  card.append(head(payload))
  visibleSides(payload).forEach((side) => card.append(section(side, payload, ctx)))
  if (payload.warnings.length) {
    const list = el('ul', 'mk-warnings')
    payload.warnings.forEach((w) => list.append(el('li', 'mk-warning', w)))
    card.append(list)
  }
  card.append(foot(payload, ctx))
  return card
}

/** Replace `host`'s card slot with the payload's card. */
export function renderMarkets(
  host: HTMLElement,
  payload: MarketsPayload,
  ctx: MarketsRenderContext,
): void {
  const slot = host.querySelector<HTMLElement>('.msg-artifact-markets__body')
  if (!slot) return
  slot.replaceChildren(buildMarketsCard(payload, ctx))
  host.dataset.marketsHost = 'rendered'
}

/** Re-render every "2m ago" stamp inside `root`. */
export function refreshMarketsTimes(root: ParentNode, nowMs: number): void {
  root.querySelectorAll<HTMLElement>('[data-mk-fetched-at]').forEach((node) => {
    const next = relativeTime(node.dataset.mkFetchedAt || '', nowMs)
    if (next && node.textContent !== next) node.textContent = next
  })
}

/* ── Mounter ────────────────────────────────────────────────────────────── */

/** A gateway RPC call: `rpc.call(method, params)`. */
export type MarketsCall = (method: string, params: Record<string, unknown>) => Promise<unknown>

/** What the host does with a row's Swap button (the desktop opens its swap panel). */
export type MarketsSwapHandler = (swap: MarketsSwap) => void

export interface MarketsMounterDeps {
  /** Fetch a markets artifact body from its (authenticated) URL. */
  fetchPayload: (url: string) => Promise<unknown>
  /**
   * The gateway call ↻, *Show lookalikes* and *Deeper* re-run
   * `trading.markets` through (agent-callable, so the web console passes it).
   * Absent → none of the three.
   */
  call?: MarketsCall | null
  /**
   * A Swap button per row, calling this with the row's `swap`. Absent (the
   * web console) → no button.
   */
  onSwap?: MarketsSwapHandler | null
  /**
   * `onSwap`, read live at render and click time — the host's handler may
   * come and go without remounting the transcript. Wins over `onSwap`.
   */
  getOnSwap?: () => MarketsSwapHandler | null | undefined
  /** Clock. Default: Date.now. */
  now?: () => number
  /** chat.js `_chatDiag` — the diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/** An RPC error's message, whatever shape it arrived in. */
function errorMessage(error: unknown): string {
  const e = error as { code?: unknown; message?: unknown } | null
  if (typeof e?.message === 'string' && e.message) return e.message
  if (typeof e?.code === 'string' && e.code) return e.code
  return String(error)
}

/**
 * Create the markets card mounter bound to the transcript's fetch surface.
 *
 * `mountMarkets(root)` is idempotent: it only picks up placeholders it has not
 * claimed yet, so the streaming path may call it after every artifact append
 * and the history path after a bulk replay without double-rendering.
 *
 * It owns the once-a-minute clock that refreshes the "2m ago" stamps (running
 * only while a rendered card is in the document) and the refresh-error resets;
 * `destroyAll` clears both.
 */
export function createMarketsMounter(deps: MarketsMounterDeps) {
  const diag = deps.diag ?? ((): void => {})
  const now = deps.now ?? ((): number => Date.now())
  const swapHandler = (): MarketsSwapHandler | null => {
    const value = deps.getOnSwap ? deps.getOnSwap() : deps.onSwap
    return typeof value === 'function' ? value : null
  }
  const claimed = new Set<HTMLElement>()
  const rendered = new Set<HTMLElement>()
  /** The payload each rendered host shows, so ↻ knows what to re-run. */
  const payloads = new Map<HTMLElement, MarketsPayload>()
  const refreshing = new Set<HTMLElement>()
  const wired = new WeakSet<HTMLElement>()
  const timers = new Set<ReturnType<typeof setTimeout>>()
  let clock: ReturnType<typeof setInterval> | null = null

  const ctx: MarketsRenderContext = {
    now,
    get canRefresh() {
      return typeof deps.call === 'function'
    },
    get canSwap() {
      return swapHandler() !== null
    },
  }

  function setTimer(fn: () => void, ms: number): void {
    const id = setTimeout(() => {
      timers.delete(id)
      fn()
    }, ms)
    timers.add(id)
  }

  function show(host: HTMLElement, payload: MarketsPayload): void {
    renderMarkets(host, payload, ctx)
    payloads.set(host, payload)
  }

  /** Show a line under the footer (a failed re-run) for LP_ERROR_MS. */
  function flashFootError(host: HTMLElement, message: string): void {
    const footNode = host.querySelector<HTMLElement>('.mk-foot')
    if (!footNode) return
    let line = footNode.querySelector<HTMLElement>('.mk-error')
    if (!line) {
      line = el('span', 'mk-error')
      line.setAttribute('role', 'alert')
      footNode.append(line)
    }
    line.textContent = message
    const shown = line
    setTimer(() => shown.remove(), LP_ERROR_MS)
  }

  /**
   * Re-run the card's read: ↻ and *Read again* with the same params, *Show lookalikes* and
   * *Deeper* with `lookalikes` / `deep` merged in. The new card replaces the
   * old one in place.
   */
  async function rerun(host: HTMLElement, extra: Record<string, unknown> = {}): Promise<void> {
    const current = payloads.get(host)
    const request = current?.request
    const call = deps.call
    if (!current || !request || typeof call !== 'function' || refreshing.has(host)) return
    refreshing.add(host)
    const params = { ...request.params, ...extra }
    const card = host.querySelector<HTMLElement>('.mk-card')
    card?.setAttribute('data-refreshing', 'true')
    card?.setAttribute('aria-busy', 'true')
    const controls = host.querySelectorAll<HTMLButtonElement>('.mk-foot button[data-action]')
    controls.forEach((b) => (b.disabled = true))
    diag('markets.refresh.start', { params })
    try {
      const next = normalizeMarketsPayload(await call(MARKETS_METHOD, params))
      if (!next) throw new Error(t('chat.marketsUnreadable'))
      if (!host.isConnected || !rendered.has(host)) return
      // The engine echoes the request; an older one does not, and ↻ must keep working.
      if (!next.request) next.request = { kind: 'markets', params }
      if (!next.fetchedAt) next.fetchedAt = new Date(now()).toISOString()
      show(host, next)
      diag('markets.refresh.done', {
        quote: next.sections.quote.length,
        base: next.sections.base.length,
        partial: next.partial,
      })
    } catch (error) {
      card?.removeAttribute('data-refreshing')
      card?.removeAttribute('aria-busy')
      controls.forEach((b) => (b.disabled = false))
      flashFootError(host, t('chat.marketsRefreshFailed', { message: errorMessage(error) }))
      diag('markets.refresh.error', { error: String(error) })
    } finally {
      refreshing.delete(host)
    }
  }

  /** One click handler per host: refresh, the two links, swap. */
  function wire(host: HTMLElement): void {
    if (wired.has(host)) return
    wired.add(host)
    host.addEventListener('click', (event) => {
      const target = (event.target as Element | null)?.closest<HTMLElement>(
        '.mk-card [data-action]',
      )
      if (!target || !host.contains(target) || (target as HTMLButtonElement).disabled) return
      const action = target.dataset.action
      if (action === 'refresh' || action === 'again') {
        void rerun(host)
      } else if (action === 'lookalikes') {
        void rerun(host, { lookalikes: true })
      } else if (action === 'deep') {
        void rerun(host, { deep: true })
      } else if (action === 'swap') {
        const handler = swapHandler()
        const chainId = Number(target.dataset.chainId)
        const tokenIn = target.dataset.tokenIn ?? ''
        const tokenOut = target.dataset.tokenOut ?? ''
        if (!handler || !Number.isFinite(chainId) || !tokenIn || !tokenOut) return
        diag('markets.swap', { chainId, tokenIn, tokenOut })
        handler({ chainId, tokenIn, tokenOut })
      }
    })
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
    rendered.forEach((host) => refreshMarketsTimes(host, at))
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
      }
    }
  }

  function setStatus(host: HTMLElement, message: string): void {
    const status = host.querySelector<HTMLElement>('.msg-artifact-markets__status')
    if (!status) return
    status.textContent = message
    status.hidden = message === ''
  }

  async function mountOne(host: HTMLElement): Promise<void> {
    const url = host.dataset.marketsSrc || ''
    if (!url) {
      setStatus(host, t('chat.marketsUnavailable'))
      return
    }
    try {
      diag('markets.mount.start', { url })
      const payload = normalizeMarketsPayload(await deps.fetchPayload(url))
      if (!payload) {
        setStatus(host, t('chat.marketsUnreadable'))
        diag('markets.mount.empty', { url })
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
      diag('markets.mount.done', {
        url,
        quote: payload.sections.quote.length,
        base: payload.sections.base.length,
      })
    } catch (error) {
      setStatus(host, t('chat.marketsFailed'))
      diag('markets.mount.error', { url, error: String(error) })
    }
  }

  /** Mount every not-yet-mounted markets placeholder inside `root`. */
  function mountMarkets(root: HTMLElement | null | undefined): void {
    if (!root) return
    pruneDetached()
    root.querySelectorAll<HTMLElement>('[data-markets-src]').forEach((host) => {
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
    stopClock()
    timers.forEach((id) => clearTimeout(id))
    timers.clear()
  }

  return { mountMarkets, destroyAll, pruneDetached, refresh: rerun }
}

export type MarketsMounter = ReturnType<typeof createMarketsMounter>
