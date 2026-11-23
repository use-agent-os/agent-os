/**
 * The Markets tab's arithmetic (docs/markets.md): which token a typed query
 * means, the words a row wears, and the Token objects a row's Swap hands to
 * the ticket. Nothing here renders.
 */

import { formatPrice, formatUsd } from './logic'
import {
  NATIVE_ADDRESS,
  type MarketsCounterparty,
  type MarketsCounts,
  type MarketsPayload,
  type MarketsPool,
  type MarketsToken,
  type SearchToken,
  type Token,
} from './types'

export const ROBINHOOD_CHAIN = 4663

/** The Min TVL segments, in dollars; the engine's default is the middle one. */
export const MIN_TVL_STEPS = [1_000, 10_000, 100_000] as const
export const DEFAULT_MIN_TVL = 10_000

/** Rows a section shows before "+N more". */
export const SECTION_CAP = 40

export const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

/** "$1k", "$10k", "$100k": the Min TVL segment labels. */
export function tvlStepLabel(usd: number): string {
  if (usd >= 1_000_000) return `$${usd / 1_000_000}M`
  if (usd >= 1_000) return `$${usd / 1_000}k`
  return `$${usd}`
}

const usdShort = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  // Pinned: V8 in Node 22 defaults the minimum to 1 here ("$131.0K"),
  // Node 24 to 0 ("$131K").
  minimumFractionDigits: 0,
  maximumFractionDigits: 1,
})

/** TVL and volume for a narrow cell: "$4.7M", "$842K", "$12.3K"; under $1k as dollars. */
export function formatUsdShort(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs < 1_000) return formatUsd(value)
  const body = usdShort.format(abs)
  return value < 0 ? `−${body}` : body
}

/**
 * A pool's USD price: a memecoin at $0.1131 keeps its four significant
 * digits (the desk's `formatPrice` would round it to $0.11); under $0.001
 * the subscript notation, from $1 up ordinary dollars and cents.
 */
export function formatPoolPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs === 0 || abs >= 1 || abs < 0.001) return formatPrice(value)
  const body = `$${Number(abs.toPrecision(4))}`
  return value < 0 ? `−${body}` : body
}

/**
 * One token priced in another, no currency sign: "240.3", "0.1131",
 * "0.0₃471" (the subscript counts the zeros, as prices do). Four significant
 * digits under 1, four decimals above, two past a thousand.
 */
export function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs === 0) return '0'
  if (abs < 0.001) return formatPrice(value).replace('$', '')
  const body =
    abs >= 1
      ? abs.toLocaleString('en-US', { maximumFractionDigits: abs >= 1000 ? 2 : 4 })
      : String(Number(abs.toPrecision(4)))
  return value < 0 ? `−${body}` : body
}

const MIN = 60
const HOUR = 60 * MIN
const DAY = 24 * HOUR

/**
 * A pool's age the way the card says it: "45m", "5h", "3d", "2mo", "1y".
 * Days run to a month so a two-week-old launch reads "14d", not "2w".
 */
export function poolAge(createdAt: string | null | undefined, now = Date.now()): string {
  if (!createdAt) return '—'
  const at = Date.parse(createdAt)
  if (!Number.isFinite(at)) return '—'
  const s = Math.max(0, Math.floor((now - at) / 1000))
  if (s < HOUR) return `${Math.max(1, Math.floor(s / MIN))}m`
  if (s < DAY) return `${Math.floor(s / HOUR)}h`
  if (s < 30 * DAY) return `${Math.floor(s / DAY)}d`
  if (s < 365 * DAY) return `${Math.floor(s / (30 * DAY))}mo`
  return `${Math.floor(s / (365 * DAY))}y`
}

/** "Uniswap v4", "Bankr": the engine's label, with the version when it knows one. */
export function dexLabel(dex: MarketsPool['dex']): string {
  const label = dex.label || dexIdLabel(dex.id)
  return dex.version ? `${label} ${dex.version}` : label
}

/** A readable name from a GeckoTerminal dex id when the label is missing: "ramses-v3-robinhood" → "Ramses". */
function dexIdLabel(id: string): string {
  const word = id.split('-')[0] ?? id
  return word ? word[0]!.toUpperCase() + word.slice(1) : id
}

/** "0.05%" from `feePct`; null when the pool name carried no fee. */
export function feeLabel(feePct: number | null): string | null {
  if (feePct === null || !Number.isFinite(feePct)) return null
  return `${Number(feePct.toFixed(4))}%`
}

/**
 * A Stock Token's name ends in "• Robinhood Token"; CoinGecko caps names at
 * 60 characters, so a long one arrives as "… • Robinhood Toke". Any prefix of
 * the suffix after the bullet counts; a name without the bullet never does.
 */
export function isStockTokenName(name: string | null | undefined): boolean {
  if (!name) return false
  const at = name.lastIndexOf('•')
  if (at < 0) return false
  const rest = name
    .slice(at + 1)
    .trim()
    .replace(/\s+/g, ' ')
  return rest.length >= 1 && 'Robinhood Token'.startsWith(rest)
}

/** A token the Markets tab is about: the engine's flag, or the Robinhood naming rule. */
export function isStockToken(token: Pick<Token, 'chainId' | 'name' | 'stockToken' | 'verified'>) {
  if (token.stockToken) return true
  return token.chainId === ROBINHOOD_CHAIN && token.verified && isStockTokenName(token.name)
}

export interface MarketsTarget {
  chainId: number
  /** An address when the lookup resolved one, else the query as typed (the engine resolves it). */
  target: string
  /** The resolved token, for the head line before the read lands. */
  token: SearchToken | null
  /**
   * The verified exact matches on the pick's chain, the pick first: the
   * "Showing … · 3 matches" list the user can choose another from. Deduped
   * by address and ranked by `rankMatches`, so the same search answers the
   * same list whatever order the chains replied in. Empty for an address or
   * when nothing verified matched.
   */
  candidates: SearchToken[]
}

/**
 * Verified exact matches, best first: a Stock Token, then the deepest
 * liquidity (unknown last), then the address — a total order, so the list
 * never depends on which chain's search answered first.
 */
export function rankMatches(rows: readonly SearchToken[]): SearchToken[] {
  return [...rows].sort(
    (a, b) =>
      Number(isStockToken(b)) - Number(isStockToken(a)) ||
      (b.liquidityUsd ?? -Infinity) - (a.liquidityUsd ?? -Infinity) ||
      a.chainId - b.chainId ||
      a.address.toLowerCase().localeCompare(b.address.toLowerCase()),
  )
}

/**
 * One row per token (chain + address, case-insensitive): a search can list
 * a token twice (two sources, or a refetch merged in); the row with the
 * deeper liquidity is kept.
 */
export function dedupeMatches(rows: readonly SearchToken[]): SearchToken[] {
  const byKey = new Map<string, SearchToken>()
  for (const row of rows) {
    const key = `${row.chainId}:${row.address.toLowerCase()}`
    const seen = byKey.get(key)
    if (!seen || (row.liquidityUsd ?? -Infinity) > (seen.liquidityUsd ?? -Infinity)) {
      byKey.set(key, row)
    }
  }
  return [...byKey.values()]
}

/**
 * Which token a typed query means. An address is taken as given on `chain`.
 * A symbol is looked up on both chains (`results` from `useTokenSearch`, read
 * only once that search has settled); only verified exact matches count,
 * deduped by address and ranked by `rankMatches` (a Stock Token, then the
 * highest liquidity) — never simply the first one the search returned: "AI"
 * on Robinhood Chain is several tokens, and the deep one is meant.
 *
 * - With no chain chosen in the tab, a verified Stock Token of that symbol
 *   wins wherever it lives (NVDA typed on Base means Robinhood Chain's
 *   NVDA), then the best match on the desk's chain, then on any chain.
 * - With a chain chosen, only that chain's matches are taken.
 *
 * The candidates are the pick's chain only: the list a user picks from is
 * the tokens of that symbol where the markets are being read, not a mix of
 * both chains (whose count changed with whichever chain answered last).
 *
 * Nothing verified → the symbol goes to the engine as typed, on `chain`; it
 * applies its own Stock-Token-first rule and answers ambiguity itself.
 */
export function pickMarketsTarget(
  query: string,
  results: readonly SearchToken[],
  chain: number,
  explicitChain: boolean,
): MarketsTarget {
  const q = query.trim()
  if (ADDRESS_RE.test(q)) return { chainId: chain, target: q, token: null, candidates: [] }
  const sym = q.toLowerCase()
  const exact = dedupeMatches(
    results.filter((r) => r.verified && r.symbol.trim().toLowerCase() === sym),
  )
  const onChain = rankMatches(exact.filter((r) => r.chainId === chain))
  const pick = explicitChain
    ? onChain[0]
    : (rankMatches(exact.filter((r) => isStockToken(r)))[0] ?? onChain[0] ?? rankMatches(exact)[0])
  if (!pick) return { chainId: chain, target: q, token: null, candidates: [] }
  const sameChain = rankMatches(exact.filter((r) => r.chainId === pick.chainId))
  return {
    chainId: pick.chainId,
    target: pick.address,
    token: pick,
    candidates: [pick, ...sameChain.filter((r) => r !== pick)],
  }
}

/** The pool's counterparty is the chain's native coin (the zero address). */
export function isNativeCounterparty(cp: Pick<MarketsCounterparty, 'address' | 'native'>): boolean {
  return cp.native === true || cp.address.toLowerCase() === NATIVE_ADDRESS
}

/** The counterparty's symbol as a row prints it: the native coin is ETH, never "0x000…". */
export function counterpartySymbol(
  cp: Pick<MarketsCounterparty, 'address' | 'native' | 'symbol'>,
): string {
  return isNativeCounterparty(cp) ? 'ETH' : cp.symbol
}

/**
 * A row's two symbols, counterparty first on quote rows and the token first
 * on base rows. The engine's `pair` is used as sent, except when the
 * counterparty is the native coin: then the pair is rebuilt so it reads ETH
 * whatever an older engine put there.
 */
export function rowPair(
  pool: Pick<MarketsPool, 'pair' | 'side' | 'counterparty'>,
  tokenSymbol: string,
): [string, string] {
  if (!isNativeCounterparty(pool.counterparty) && pool.pair) return pairParts(pool.pair)
  const other = counterpartySymbol(pool.counterparty)
  return pool.side === 'quote' ? [other, tokenSymbol] : [tokenSymbol, other]
}

/**
 * Whether a row prints the counterparty's name after the pair: a lookalike
 * (two "GME/GME" rows must be told apart) or any counterparty wearing the
 * token's own symbol.
 */
export function showsCounterpartyName(
  pool: Pick<MarketsPool, 'counterparty'>,
  tokenSymbol: string,
): boolean {
  const cp = pool.counterparty
  if (!cp.name || isNativeCounterparty(cp)) return false
  return cp.lookalike || cp.symbol.trim().toLowerCase() === tokenSymbol.trim().toLowerCase()
}

/** The launcher pill would only repeat the DEX label (Bankr on Bankr). */
export function launcherRepeatsDex(pool: Pick<MarketsPool, 'launcher' | 'dex'>): boolean {
  if (!pool.launcher) return false
  const label = pool.dex.label || dexIdLabel(pool.dex.id)
  return pool.launcher.trim().toLowerCase() === label.trim().toLowerCase()
}

/**
 * Deeper is offered on a read that was not already deep when the page cap —
 * not `limit` — ended it with more pools left, or a 429 cut it short
 * (`rateLimited`). A limit-cut read has nothing more for a deeper read to find.
 */
export function offersDeeper(
  counts: Pick<MarketsCounts, 'pageCapHit' | 'rateLimited'>,
  deep: boolean,
): boolean {
  return !deep && (counts.pageCapHit === true || counts.rateLimited === true)
}

/**
 * *Read again* is offered on a deep read a 429 cut short: Deeper has nothing
 * more to give there, but the same read a minute later may finish.
 */
export function offersReadAgain(
  counts: Pick<MarketsCounts, 'rateLimited'>,
  deep: boolean,
): boolean {
  return deep && counts.rateLimited === true
}

/**
 * A premium (or any signed percent) the way the chat card prints it: one
 * decimal, signed, and never a signed zero — anything under 0.05 % either
 * way is a flat "0.0%", toned flat (docs/markets.md, shared with the card).
 */
export function formatPremiumPct(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return '—'
  if (premiumTone(pct) === 'flat') return '0.0%'
  return `${pct > 0 ? '+' : '−'}${Math.abs(pct).toFixed(1)}%`
}

/** The tone that goes with `formatPremiumPct`: flat under 0.05 % either way. */
export function premiumTone(pct: number | null | undefined): 'up' | 'down' | 'flat' {
  if (pct === null || pct === undefined || !Number.isFinite(pct) || Math.abs(pct) < 0.05) {
    return 'flat'
  }
  return pct > 0 ? 'up' : 'down'
}

function asToken(
  chainId: number,
  t: Pick<
    MarketsToken | MarketsCounterparty,
    'address' | 'symbol' | 'name' | 'decimals' | 'logoUrl' | 'verified' | 'stockToken'
  > & { native?: boolean },
): Token {
  const native = isNativeCounterparty(t)
  return {
    chainId,
    address: t.address,
    symbol: native ? 'ETH' : t.symbol,
    name: t.name,
    decimals: t.decimals,
    logoUrl: t.logoUrl,
    native,
    verified: native || t.verified,
    stockToken: t.stockToken,
  }
}

/**
 * The two legs a row's Swap fills the ticket with, as full Token objects
 * (the ticket shows symbols and decimals before any quote). `pool.swap`
 * names the direction by address; each side is whichever of the token and
 * the counterparty carries that address.
 */
export function poolSwapTokens(
  payload: Pick<MarketsPayload, 'token'>,
  pool: Pick<MarketsPool, 'swap' | 'counterparty'>,
): { chainId: number; tokenIn: Token; tokenOut: Token } {
  const chainId = pool.swap.chainId
  const token = asToken(chainId, payload.token)
  const other = asToken(chainId, pool.counterparty)
  const isToken = (address: string) => address.toLowerCase() === token.address.toLowerCase()
  return isToken(pool.swap.tokenOut)
    ? { chainId, tokenIn: other, tokenOut: token }
    : { chainId, tokenIn: token, tokenOut: other }
}

/** The pool's two symbols, split for the row: ["AI", "NVDA"]. */
export function pairParts(pair: string): [string, string] {
  const at = pair.indexOf('/')
  if (at < 0) return [pair, '']
  return [pair.slice(0, at).trim(), pair.slice(at + 1).trim()]
}
