import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LP_CLOCK_MS, LP_ERROR_MS } from './lp'
import {
  MARKETS_ARTIFACT_MIME,
  MARKETS_ROW_CAP,
  buildMarketsCard,
  countsText,
  createMarketsMounter,
  deeperOffered,
  dexLabel,
  dexVersion,
  formatAge,
  formatMarketsPct,
  formatMarketsUsd,
  isMarketsArtifact,
  launcherLabel,
  normalizeMarketsPayload,
  normalizeMarketsRequest,
  pairLabel,
  readAgainOffered,
  showsCounterpartyName,
  visibleLauncher,
  type MarketsPayload,
  type MarketsRenderContext,
} from './markets'

const NVDA = '0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec'
const AI = '0x2e8c00000000000000000000000000000000e18a'
const ORBIO = '0x0b1b000000000000000000000000000000000001'
const USDG = '0x5d6a000000000000000000000000000000000002'
const FAKE_NVDA = '0x7777000000000000000000000000000000000003'

const FETCHED_AT = Date.parse('2026-10-06T04:12:00Z')

const PARAMS = {
  target: NVDA,
  chainId: 4663,
  side: 'all',
  minTvlUsd: 10000,
  limit: 50,
  lookalikes: false,
  deep: false,
}

function token(address: string, symbol: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    address,
    symbol,
    name,
    decimals: 18,
    logoUrl: null,
    verified: false,
    stockToken: false,
    lookalike: false,
    ...extra,
  }
}

function quotePool(cp: ReturnType<typeof token>, extra: Record<string, unknown> = {}) {
  return {
    poolAddress: `0xpool${cp.symbol}`,
    pair: `${cp.symbol}/NVDA`,
    side: 'quote',
    dex: { id: 'bankr-robinhood', label: 'Bankr', version: 'v4' },
    launcher: 'Bankr',
    viaUniswap: false,
    feePct: 1.0,
    counterparty: cp,
    tvlUsd: 4732293.0,
    volume24hUsd: 806473.0,
    txns24h: { buys: 812, sells: 790 },
    priceUsd: 0.1131,
    priceInToken: 0.000471,
    change24hPct: -3.2,
    premiumPct: null,
    createdAt: '2026-07-25T00:52:36Z',
    url: `https://www.geckoterminal.com/robinhood/pools/0xpool${cp.symbol}`,
    swap: { chainId: 4663, tokenIn: NVDA, tokenOut: cp.address },
    ...extra,
  }
}

/** A payload in the contract's shape: two quote rows and one base row. */
function raw(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    version: 1,
    kind: 'markets',
    chain: {
      id: 4663,
      key: 'robinhood',
      name: 'Robinhood Chain',
      explorer: 'https://robin.etherscan.io',
    },
    fetchedAt: '2026-10-06T04:12:00Z',
    partial: false,
    warnings: [],
    token: {
      ...token(NVDA, 'NVDA', 'NVIDIA • Robinhood Token', { verified: true, stockToken: true }),
      priceUsd: 240.3,
      oracle: {
        usd: 239.74,
        updatedAt: '2026-10-05T20:38:49Z',
        ageSeconds: 47269,
        stale: false,
        paused: false,
      },
    },
    counts: {
      scanned: 100,
      shown: 3,
      belowMinTvl: 61,
      hiddenLookalikes: 5,
      limited: 0,
      pages: 5,
      pageCap: 5,
      pageCapHit: true,
    },
    sections: {
      quote: [
        quotePool(token(ORBIO, 'ORBIO', 'Orbio'), {
          tvlUsd: 64210,
          dex: { id: 'pons-v2-dex', label: 'Pons', version: 'v2' },
          launcher: 'Pons',
          change24hPct: 12.5,
        }),
        quotePool(token(AI, 'AI', 'Artificial Inu')),
      ],
      base: [
        {
          poolAddress: '0xpoolusdg',
          pair: 'NVDA/USDG',
          side: 'base',
          dex: { id: 'uniswap-v3-robinhood', label: 'Uniswap', version: 'v3' },
          launcher: null,
          viaUniswap: true,
          feePct: 0.01,
          counterparty: token(USDG, 'USDG', 'Global Dollar', { verified: true }),
          tvlUsd: 1200000,
          volume24hUsd: 350000,
          txns24h: null,
          priceUsd: 240.71,
          priceInToken: 240.6,
          change24hPct: 0.01,
          premiumPct: 0.41,
          createdAt: '2026-08-06T00:00:00Z',
          url: 'https://www.geckoterminal.com/robinhood/pools/0xpoolusdg',
          swap: { chainId: 4663, tokenIn: NVDA, tokenOut: USDG },
        },
      ],
    },
    request: { kind: 'markets', params: { ...PARAMS } },
    ...overrides,
  }
}

function payload(overrides: Record<string, unknown> = {}): MarketsPayload {
  const p = normalizeMarketsPayload(raw(overrides))
  if (!p) throw new Error('payload did not normalize')
  return p
}

function ctx(overrides: Partial<MarketsRenderContext> = {}): MarketsRenderContext {
  return { now: () => FETCHED_AT + 2 * 60_000, ...overrides }
}

function render(p: MarketsPayload, context = ctx()): HTMLElement {
  const card = buildMarketsCard(p, context)
  document.body.append(card)
  return card
}

function placeholder(src = '/api/v1/artifacts/mk-1'): HTMLElement {
  const node = document.createElement('div')
  node.className = 'msg-artifact-markets'
  node.dataset.marketsSrc = src
  node.innerHTML = `<div class="msg-artifact-markets__body"></div>
    <p class="msg-artifact-markets__status">Loading markets…</p>`
  document.body.append(node)
  return node
}

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve()
}

function section(root: ParentNode, side: 'quote' | 'base'): HTMLElement {
  return root.querySelector<HTMLElement>(`.mk-section[data-side="${side}"]`)!
}

function rowOf(root: ParentNode, pair: string): HTMLElement {
  const row = [...root.querySelectorAll<HTMLElement>('.mk-row')].find(
    (r) => r.querySelector('.mk-pair')?.textContent === pair,
  )
  if (!row) throw new Error(`no row ${pair}`)
  return row
}

beforeEach(() => {
  document.body.replaceChildren()
})

afterEach(() => {
  vi.useRealTimers()
})

/* ── pure helpers ──────────────────────────────────────────────────────── */

describe('isMarketsArtifact', () => {
  it('matches the markets mime only', () => {
    expect(isMarketsArtifact({ mime: MARKETS_ARTIFACT_MIME })).toBe(true)
    expect(isMarketsArtifact({ mime: `${MARKETS_ARTIFACT_MIME}; charset=utf-8` })).toBe(true)
    expect(isMarketsArtifact({ mime: 'application/vnd.agentos.lp+json' })).toBe(false)
    expect(isMarketsArtifact(null)).toBe(false)
  })
})

describe('normalizeMarketsPayload', () => {
  it('refuses anything that is not a markets payload with a token', () => {
    expect(normalizeMarketsPayload(null)).toBeNull()
    expect(normalizeMarketsPayload({ kind: 'pool' })).toBeNull()
    expect(normalizeMarketsPayload({ kind: 'markets' })).toBeNull()
  })

  it('sorts each section by TVL and keeps unknown numbers null, never 0', () => {
    const p = payload({
      sections: {
        quote: [
          quotePool(token(ORBIO, 'ORBIO', 'Orbio'), { tvlUsd: null, priceUsd: null }),
          quotePool(token(AI, 'AI', 'Artificial Inu'), { tvlUsd: '4732293.0' }),
        ],
        base: [],
      },
    })
    expect(p.sections.quote.map((r) => r.pair)).toEqual(['AI/NVDA', 'ORBIO/NVDA'])
    expect(p.sections.quote[0]!.tvlUsd).toBe(4732293)
    expect(p.sections.quote[1]!.tvlUsd).toBeNull()
    expect(p.sections.quote[1]!.priceUsd).toBeNull()
  })

  it('fills the launcher, pair and DEX label from the DEX id when the engine left them out', () => {
    const p = payload({
      sections: {
        quote: [
          quotePool(token(AI, 'AI', 'Artificial Inu'), {
            pair: '',
            launcher: null,
            dex: { id: 'bankr-robinhood' },
          }),
        ],
        base: [],
      },
    })
    const row = p.sections.quote[0]!
    expect(row.pair).toBe('AI/NVDA')
    expect(row.launcher).toBe('Bankr')
    expect(row.dex).toEqual({ id: 'bankr-robinhood', label: 'Bankr', version: null })
  })

  it('drops a link or a logo that is not http(s), and a swap without addresses', () => {
    const p = payload({
      sections: {
        quote: [
          quotePool(token(AI, 'AI', 'Artificial Inu', { logoUrl: 'javascript:alert(1)' }), {
            url: 'javascript:alert(1)',
            swap: { chainId: 4663, tokenIn: 'NVDA', tokenOut: AI },
          }),
        ],
        base: [],
      },
    })
    const row = p.sections.quote[0]!
    expect(row.url).toBe('')
    expect(row.counterparty.logoUrl).toBeNull()
    expect(row.swap).toBeNull()
  })

  it('reads the request echo, and only a markets one', () => {
    expect(payload().request).toEqual({ kind: 'markets', params: PARAMS })
    expect(normalizeMarketsRequest({ kind: 'orders.approve', params: {} })).toBeNull()
    expect(normalizeMarketsRequest({ kind: 'markets' })).toEqual({ kind: 'markets', params: {} })
  })
})

describe('formatAge', () => {
  const now = Date.parse('2026-10-06T00:00:00Z')
  it.each([
    ['2026-10-05T23:15:00Z', '45m'],
    ['2026-10-05T19:00:00Z', '5h'],
    ['2026-10-03T00:00:00Z', '3d'],
    ['2026-08-06T00:00:00Z', '2mo'],
    ['2025-09-01T00:00:00Z', '1y'],
    ['2026-10-06T00:05:00Z', '0m'],
  ])('%s → %s', (iso, expected) => {
    expect(formatAge(iso, now)).toBe(expected)
  })

  it('renders an unknown or unusable stamp as a dash', () => {
    expect(formatAge(null, now)).toBe('—')
    expect(formatAge('', now)).toBe('—')
    expect(formatAge('not a date', now)).toBe('—')
  })
})

describe('labels and money', () => {
  it('names a DEX from its GeckoTerminal id', () => {
    expect(dexLabel('uniswap-v4-robinhood')).toBe('Uniswap')
    expect(dexLabel('bankr-robinhood')).toBe('Bankr')
    expect(dexLabel('pons-v2-dex')).toBe('Pons')
    expect(dexLabel('ramses-v3-robinhood')).toBe('Ramses')
    expect(dexLabel('quickswap-base')).toBe('Quickswap')
    expect(dexLabel('')).toBe('—')
    expect(dexVersion('ramses-v3-robinhood')).toBe('v3')
    expect(dexVersion('bankr-robinhood')).toBeNull()
  })

  it('labels a launcher from the engine, else the DEX id prefix', () => {
    expect(launcherLabel({ launcher: 'Bankr', dex: { id: 'uniswap-v4-base' } })).toBe('Bankr')
    expect(launcherLabel({ launcher: null, dex: { id: 'pons-v2-dex' } })).toBe('Pons')
    expect(launcherLabel({ launcher: null, dex: { id: 'clanker-base' } })).toBe('Clanker')
    expect(launcherLabel({ launcher: null, dex: { id: 'uniswap-v3-robinhood' } })).toBeNull()
  })

  it('puts the counterparty first on quote rows and the token first on base rows', () => {
    const cp = { symbol: 'AI' }
    expect(pairLabel({ side: 'quote', counterparty: cp }, 'NVDA')).toBe('AI/NVDA')
    expect(pairLabel({ side: 'base', counterparty: { symbol: 'USDG' } }, 'NVDA')).toBe('NVDA/USDG')
    expect(pairLabel({ pair: 'X/Y', side: 'quote', counterparty: cp }, 'NVDA')).toBe('X/Y')
  })

  it('prints USD compact from $100K and whole dollars below', () => {
    expect(formatMarketsUsd(4732293)).toBe('$4.73M')
    expect(formatMarketsUsd(806473)).toBe('$806K')
    expect(formatMarketsUsd(64210.4)).toBe('$64,210')
    expect(formatMarketsUsd(0.1131)).toBe('$0.1131')
    expect(formatMarketsUsd(null)).toBe('—')
  })

  it('writes the counts line, leaving zero parts out', () => {
    expect(countsText(payload())).toBe('3 of 100 pools shown · 61 under $10K · 5 lookalikes hidden')
    expect(
      countsText(
        payload({
          counts: {
            scanned: 20,
            shown: 20,
            belowMinTvl: 0,
            hiddenLookalikes: 1,
            pages: 1,
            pageCap: 5,
          },
        }),
      ),
    ).toBe('20 of 20 pools shown · 1 lookalike hidden')
  })

  it('adds the rows the limit cut, and reads an older payload without the new counts', () => {
    const limited = payload({
      counts: { scanned: 100, shown: 50, belowMinTvl: 0, hiddenLookalikes: 0, limited: 12 },
    })
    expect(limited.counts.limited).toBe(12)
    expect(countsText(limited)).toBe('50 of 100 pools shown · 12 more over the limit')
    const old = payload({
      counts: {
        scanned: 100,
        shown: 3,
        belowMinTvl: 61,
        hiddenLookalikes: 5,
        pages: 5,
        pageCap: 5,
      },
    })
    expect(old.counts.limited).toBe(0)
    expect(old.counts.pageCapHit).toBe(false)
    expect(old.counts.rateLimited).toBe(false)
    expect(countsText(old)).toBe('3 of 100 pools shown · 61 under $10K · 5 lookalikes hidden')
  })

  it('offers Deeper only when the page cap ended the read and it was not deep', () => {
    expect(deeperOffered(payload())).toBe(true)
    // `limit`, not the page cap, ended a "50 of 100 shown" read: Deeper would do nothing.
    const byLimit = payload({
      counts: {
        scanned: 100,
        shown: 50,
        belowMinTvl: 0,
        hiddenLookalikes: 0,
        limited: 50,
        pages: 5,
        pageCap: 5,
        pageCapHit: false,
      },
    })
    expect(deeperOffered(byLimit)).toBe(false)
    // An older payload without pageCapHit never offers it, even at the cap.
    expect(
      deeperOffered(payload({ counts: { scanned: 100, shown: 3, pages: 5, pageCap: 5 } })),
    ).toBe(false)
    expect(
      deeperOffered(payload({ request: { kind: 'markets', params: { ...PARAMS, deep: true } } })),
    ).toBe(false)
  })

  it('offers a retry after a 429: Deeper on a shallow read, Read again on a deep one', () => {
    const COUNTS = {
      scanned: 40,
      shown: 3,
      belowMinTvl: 0,
      hiddenLookalikes: 0,
      limited: 0,
      pages: 2,
      pageCap: 5,
      pageCapHit: false,
    }
    const DEEP = { kind: 'markets', params: { ...PARAMS, deep: true } }
    const shallow = payload({ counts: { ...COUNTS, rateLimited: true } })
    expect(shallow.counts.rateLimited).toBe(true)
    expect(deeperOffered(shallow)).toBe(true)
    expect(readAgainOffered(shallow)).toBe(false)
    const deep = payload({ counts: { ...COUNTS, rateLimited: true }, request: DEEP })
    expect(deeperOffered(deep)).toBe(false)
    expect(readAgainOffered(deep)).toBe(true)
    // Not rate-limited: neither (a deep read that finished has nothing to retry).
    expect(readAgainOffered(payload({ counts: COUNTS, request: DEEP }))).toBe(false)
    expect(deeperOffered(payload({ counts: COUNTS }))).toBe(false)
    // Anything but `true` is not rate-limited.
    expect(payload({ counts: { ...COUNTS, rateLimited: 'yes' } }).counts.rateLimited).toBe(false)
  })

  it('never prints a signed zero percent', () => {
    expect(formatMarketsPct(0.41)).toBe('+0.4%')
    expect(formatMarketsPct(-3.2)).toBe('−3.2%')
    expect(formatMarketsPct(-0.004)).toBe('0.0%')
    expect(formatMarketsPct(0.04)).toBe('0.0%')
    expect(formatMarketsPct(-0.049)).toBe('0.0%')
    expect(formatMarketsPct(0)).toBe('0.0%')
    expect(formatMarketsPct(-0.05)).toBe('−0.1%')
    expect(formatMarketsPct(null)).toBe('—')
  })

  it('hides a launcher that only repeats the DEX label', () => {
    expect(visibleLauncher({ launcher: 'Bankr', dex: { label: 'Bankr' } })).toBeNull()
    expect(visibleLauncher({ launcher: 'Pons', dex: { label: 'pons' } })).toBeNull()
    expect(visibleLauncher({ launcher: 'Bankr', dex: { label: 'Uniswap' } })).toBe('Bankr')
    expect(visibleLauncher({ launcher: null, dex: { label: 'Uniswap' } })).toBeNull()
  })

  it('names the counterparty of a lookalike or a same-symbol row', () => {
    const cp = (symbol: string, lookalike = false, name = 'Some Name') => ({
      counterparty: { symbol, name, lookalike },
    })
    expect(showsCounterpartyName(cp('GME'), 'GME')).toBe(true)
    expect(showsCounterpartyName(cp('gme'), 'GME')).toBe(true)
    expect(showsCounterpartyName(cp('TSLA', true), 'GME')).toBe(true)
    expect(showsCounterpartyName(cp('AI'), 'GME')).toBe(false)
    expect(showsCounterpartyName(cp('GME', false, ''), 'GME')).toBe(false)
  })

  it('names a native counterparty by its symbol, never the zero address', () => {
    const ZERO = '0x0000000000000000000000000000000000000000'
    const p = payload({
      sections: {
        quote: [],
        base: [
          {
            ...quotePool(token(ZERO, '', '', { native: true }), {
              pair: '0x0000…0000/NVDA',
              swap: { chainId: 4663, tokenIn: NVDA, tokenOut: ZERO },
            }),
            side: 'base',
          },
        ],
      },
    })
    const row = p.sections.base[0]!
    expect(row.counterparty.native).toBe(true)
    expect(row.counterparty.symbol).toBe('ETH')
    expect(row.pair).toBe('NVDA/ETH')
    const card = render(p, ctx({ canSwap: true }))
    expect(card.textContent).not.toContain('0x0000')
    expect(card.querySelector('.mk-swap')!.getAttribute('title')).toBe('Swap NVDA for ETH')
  })
})

/* ── the card ──────────────────────────────────────────────────────────── */

describe('markets card', () => {
  it('draws both sections in order with their titles and counts', () => {
    const card = render(payload())
    expect(card.classList.contains('mk-card')).toBe(true)
    expect(card.dataset.chain).toBe('4663')
    expect(card.dataset.partial).toBeUndefined()
    const sections = [...card.querySelectorAll<HTMLElement>('.mk-section')]
    expect(sections.map((s) => s.dataset.side)).toEqual(['quote', 'base'])
    expect(section(card, 'quote').querySelector('.mk-section-title')).toHaveTextContent(
      'Priced in NVDA',
    )
    expect(section(card, 'quote').querySelector('.mk-section-count')).toHaveTextContent('2')
    expect(section(card, 'base').querySelector('.mk-section-title')).toHaveTextContent(
      'NVDA priced in',
    )
    expect(section(card, 'base').querySelector('.mk-section-count')).toHaveTextContent('1')
  })

  it('heads the card with the token, its chain, price and oracle', () => {
    const card = render(payload())
    const head = card.querySelector('.mk-head')!
    expect(head.querySelector('.mk-symbol')).toHaveTextContent('NVDA')
    expect(head.querySelector('.mk-name')).toHaveTextContent('NVIDIA • Robinhood Token')
    expect(head.querySelector('.mk-chain')).toHaveTextContent('Robinhood Chain')
    expect(head.querySelector('.mk-price')).toHaveTextContent('$240.30')
    expect(head.querySelector('.mk-oracle')).toHaveTextContent('oracle $239.74')
    expect(head.querySelector('.mk-oracle-badge')).toBeNull()
  })

  it('badges a stale or paused oracle', () => {
    const base = raw()
    const tokenRow = base.token as Record<string, unknown>
    const stale = payload({
      token: { ...tokenRow, oracle: { ...(tokenRow.oracle as object), stale: true } },
    })
    const badge = render(stale).querySelector<HTMLElement>('.mk-oracle-badge')!
    expect(badge).toHaveTextContent('stale')
    expect(badge.dataset.tone).toBe('warn')
    const paused = payload({
      token: { ...tokenRow, oracle: { ...(tokenRow.oracle as object), stale: true, paused: true } },
    })
    const pausedBadge = render(paused).querySelector<HTMLElement>(
      '.mk-card:last-of-type .mk-oracle-badge',
    )!
    expect(pausedBadge).toHaveTextContent('paused')
    expect(pausedBadge.dataset.tone).toBe('danger')
  })

  it('lists AI/NVDA first in the quote section, on Bankr with no repeated launcher pill', () => {
    const card = render(payload())
    const pairs = [...section(card, 'quote').querySelectorAll('.mk-pair')].map((n) => n.textContent)
    expect(pairs).toEqual(['AI/NVDA', 'ORBIO/NVDA'])
    const row = rowOf(card, 'AI/NVDA')
    expect(row.dataset.side).toBe('quote')
    expect(row.querySelector('.mk-dex')).toHaveTextContent('Bankr v4')
    expect(row.querySelector('.mk-version')).toHaveTextContent('v4')
    // "Bankr Bankr" and "Pons v2 Pons" repeat themselves: no launcher pill.
    expect(row.querySelector('.mk-launcher')).toBeNull()
    expect(rowOf(card, 'ORBIO/NVDA').querySelector('.mk-launcher')).toBeNull()
    expect(row.querySelector('.mk-cp-name')).toBeNull()
    expect(row.querySelector('.mk-tvl')).toHaveTextContent('$4.73M')
    expect(row.querySelector('.mk-vol')).toHaveTextContent('$806K')
    expect(row.querySelector('.mk-px')).toHaveTextContent('$0.1131')
    expect(row.querySelector('.mk-px-in')).toHaveTextContent('0.000471 NVDA')
    expect(row.querySelector('.mk-age')).toHaveTextContent('2mo')
    const change = row.querySelector<HTMLElement>('.mk-change')!
    expect(change).toHaveTextContent('−3.2%')
    expect(change.dataset.tone).toBe('down')
    expect(rowOf(card, 'ORBIO/NVDA').querySelector<HTMLElement>('.mk-change')!.dataset.tone).toBe(
      'up',
    )
    expect(rowOf(card, 'ORBIO/NVDA').querySelector('.mk-tvl')).toHaveTextContent('$64,210')
    // The pair links to the pool page, opened outside the app.
    const link = row.querySelector<HTMLAnchorElement>('a.mk-pair')!
    expect(link.href).toBe('https://www.geckoterminal.com/robinhood/pools/0xpoolAI')
    expect(link.rel).toBe('noopener noreferrer')
  })

  it('wears a launcher pill when the launcher differs from the DEX', () => {
    const p = payload({
      sections: {
        quote: [
          quotePool(token(AI, 'AI', 'Artificial Inu'), {
            dex: { id: 'uniswap-v4-robinhood', label: 'Uniswap', version: 'v4' },
            launcher: 'Bankr',
          }),
        ],
        base: [],
      },
    })
    const row = rowOf(render(p), 'AI/NVDA')
    expect(row.querySelector('.mk-dex')).toHaveTextContent('Uniswap v4')
    expect(row.querySelector('.mk-launcher')).toHaveTextContent('Bankr')
  })

  it('names the counterparty after the pair so two GME/GME rows can be told apart', () => {
    const GME = '0x6e00000000000000000000000000000000000004'
    const p = payload({
      token: { ...token(GME, 'GME', 'GameStop • Robinhood Token', { stockToken: true }) },
      sections: {
        quote: [],
        base: [
          {
            ...quotePool(token(FAKE_NVDA, 'GME', 'memestock GME'), { pair: 'GME/GME' }),
            side: 'base',
            tvlUsd: 50_000,
          },
          {
            ...quotePool(token(AI, 'gme', 'Gamer Meme'), { pair: 'GME/gme' }),
            side: 'base',
            tvlUsd: 40_000,
          },
          {
            ...quotePool(token(USDG, 'USDG', 'Global Dollar'), { pair: 'GME/USDG' }),
            side: 'base',
            tvlUsd: 30_000,
          },
        ],
      },
    })
    const card = render(p)
    const names = [...card.querySelectorAll('.mk-row')].map(
      (r) => r.querySelector('.mk-cp-name')?.textContent ?? null,
    )
    expect(names).toEqual(['memestock GME', 'Gamer Meme', null])
    // The name sits after the pair, outside it, and before the venue line.
    const market = rowOf(card, 'GME/GME').querySelector('.mk-market')!
    expect([...market.children].map((c) => c.className)).toEqual([
      'mk-pair',
      'mk-cp-name',
      'mk-venue',
    ])
  })

  it('shows the base row with its uni flag, price in the counterparty and premium', () => {
    const row = rowOf(render(payload()), 'NVDA/USDG')
    expect(row.querySelector('.mk-launcher')).toBeNull()
    expect(row.querySelector<HTMLElement>('.mk-flag[data-kind="uni"]')).toHaveTextContent('uni')
    expect(row.querySelector('.mk-px-in')).toHaveTextContent('240.6 USDG')
    const premium = row.querySelector<HTMLElement>('.mk-premium')!
    expect(premium).toHaveTextContent('+0.4% vs oracle')
    expect(premium.dataset.tone).toBe('up')
    expect(row.querySelector<HTMLElement>('.mk-change')!.dataset.tone).toBe('flat')
  })

  it('shows a premium that rounds to zero as a flat 0.0%, never −0.0%', () => {
    const p = payload()
    p.sections.base[0]!.premiumPct = -0.004
    p.sections.base[0]!.change24hPct = -0.004
    const row = rowOf(render(p), 'NVDA/USDG')
    const premium = row.querySelector<HTMLElement>('.mk-premium')!
    expect(premium.textContent).toBe('0.0% vs oracle')
    expect(premium.dataset.tone).toBe('flat')
    expect(premium.title).toBe('This pool prices NVDA 0.0% against its Chainlink feed')
    const change = row.querySelector<HTMLElement>('.mk-change')!
    expect(change.textContent).toBe('0.0%')
    expect(change.dataset.tone).toBe('flat')
  })

  it('marks a lookalike row and flags a Stock Token counterparty', () => {
    const p = payload({
      sections: {
        quote: [
          quotePool(token(FAKE_NVDA, 'NVDA', 'NVIDIA Robinhood Token', { lookalike: true })),
          quotePool(token(AI, 'AAPL', 'Apple • Robinhood Token', { stockToken: true }), {
            pair: 'AAPL/NVDA',
          }),
        ],
        base: [],
      },
    })
    const card = render(p)
    const fake = rowOf(card, 'NVDA/NVDA')
    expect(fake.dataset.lookalike).toBe('true')
    expect(fake.querySelector('.mk-cp-name')).toHaveTextContent('NVIDIA Robinhood Token')
    expect(fake.querySelector('.mk-flag[data-kind="lookalike"]')).toHaveTextContent('lookalike')
    const stock = rowOf(card, 'AAPL/NVDA')
    expect(stock.dataset.lookalike).toBeUndefined()
    expect(stock.querySelector('.mk-cp-name')).toBeNull()
    expect(stock.querySelector('.mk-flag[data-kind="stock"]')).not.toBeNull()
  })

  it('draws no Swap button unless the context can swap', () => {
    expect(render(payload()).querySelector('.mk-swap')).toBeNull()
    document.body.replaceChildren()
    expect(render(payload(), ctx({ canSwap: true })).querySelectorAll('.mk-swap')).toHaveLength(3)
  })

  it('says so in one line for an empty section', () => {
    const card = render(payload({ sections: { quote: [], base: [] } }))
    expect(section(card, 'quote').querySelector('.mk-empty')).toHaveTextContent(
      'No pools against NVDA above $10K',
    )
    expect(section(card, 'quote').querySelector('.mk-section-count')).toHaveTextContent('0')
    expect(section(card, 'quote').querySelector('.mk-rows')).toBeNull()
    expect(section(card, 'base').querySelector('.mk-empty')).not.toBeNull()
  })

  it('draws only the section a one-sided read asked for', () => {
    const card = render(
      payload({ request: { kind: 'markets', params: { ...PARAMS, side: 'quote' } } }),
    )
    expect(section(card, 'quote')).not.toBeNull()
    expect(section(card, 'base')).toBeNull()
  })

  it('wears the partial badge with the warning as its title', () => {
    const warning = 'GeckoTerminal rate limit: showing the first 40 pools'
    const card = render(payload({ partial: true, warnings: [warning] }))
    expect(card.dataset.partial).toBe('true')
    const badge = card.querySelector<HTMLElement>('.mk-foot .mk-partial')!
    expect(badge).toHaveTextContent('partial')
    expect(badge.title).toBe(warning)
    expect(card.querySelector('.mk-warnings')).toHaveTextContent(warning)
    expect(render(payload()).querySelector('.mk-partial')).toBeNull()
  })

  it('caps a section at 40 rows and expands the rest in place', () => {
    const many = Array.from({ length: MARKETS_ROW_CAP + 5 }, (_, i) =>
      quotePool(token(`0x${String(i + 1).padStart(40, '0')}`, `T${i}`, `Token ${i}`), {
        pair: `T${i}/NVDA`,
        tvlUsd: 1_000_000 - i,
      }),
    )
    const card = render(payload({ sections: { quote: many, base: [] } }))
    const quote = section(card, 'quote')
    expect(quote.querySelectorAll('.mk-row')).toHaveLength(MARKETS_ROW_CAP)
    expect(quote.querySelector('.mk-section-count')).toHaveTextContent(String(MARKETS_ROW_CAP + 5))
    const more = quote.querySelector<HTMLButtonElement>('.mk-more')!
    expect(more).toHaveTextContent('+5 more')
    more.click()
    expect(quote.querySelectorAll('.mk-row')).toHaveLength(MARKETS_ROW_CAP + 5)
    expect(quote.querySelector('.mk-more')).toBeNull()
    // The extra rows land in the same list, in TVL order.
    expect(quote.querySelectorAll('.mk-rows')).toHaveLength(1)
    expect(quote.querySelector('.mk-row:last-child .mk-pair')).toHaveTextContent('T44/NVDA')
  })

  it('offers refresh, lookalikes and deeper only when it can re-run', () => {
    const quiet = render(payload())
    expect(quiet.querySelector('.mk-refresh')).toBeNull()
    expect(quiet.querySelector('.mk-link')).toBeNull()
    const card = render(payload(), ctx({ canRefresh: true }))
    expect(card.querySelector('.mk-refresh')).not.toBeNull()
    expect(card.querySelector('.mk-link[data-action="lookalikes"]')).toHaveTextContent(
      'Show lookalikes',
    )
    expect(card.querySelector('.mk-link[data-action="deep"]')).toHaveTextContent('Deeper')
    // Already showing lookalikes, already deep: neither link again.
    const done = render(
      payload({
        request: { kind: 'markets', params: { ...PARAMS, lookalikes: true, deep: true } },
      }),
      ctx({ canRefresh: true }),
    )
    expect(done.querySelector('.mk-link')).toBeNull()
    // `limit` ended the read, not the page cap: no Deeper, lookalikes still offered.
    const byLimit = render(
      payload({
        counts: {
          scanned: 100,
          shown: 50,
          belowMinTvl: 0,
          hiddenLookalikes: 2,
          limited: 50,
          pages: 5,
          pageCap: 5,
          pageCapHit: false,
        },
      }),
      ctx({ canRefresh: true }),
    )
    expect(byLimit.querySelector('.mk-link[data-action="deep"]')).toBeNull()
    expect(byLimit.querySelector('.mk-link[data-action="lookalikes"]')).not.toBeNull()
    expect(byLimit.querySelector('.mk-counts')).toHaveTextContent('50 more over the limit')
    // A deep read a 429 cut short: Read again (no Deeper), beside ↻.
    const limited = render(
      payload({
        counts: { ...payload().counts, pageCapHit: false, rateLimited: true },
        request: { kind: 'markets', params: { ...PARAMS, lookalikes: true, deep: true } },
      }),
      ctx({ canRefresh: true }),
    )
    expect(limited.querySelector('.mk-link[data-action="deep"]')).toBeNull()
    expect(limited.querySelector('.mk-link[data-action="again"]')).toHaveTextContent('Read again')
    expect(limited.querySelectorAll('[data-action="refresh"]')).toHaveLength(1)
  })

  it('never interprets a symbol as markup', () => {
    const p = payload({
      sections: {
        quote: [quotePool(token(AI, '<img src=x onerror=alert(1)>', 'x'), { pair: '' })],
        base: [],
      },
    })
    const card = render(p)
    expect(card.querySelector('.mk-rows img')).toBeNull()
    expect(card.querySelector('.mk-pair')!.textContent).toContain('<img')
  })
})

/* ── the mounter ───────────────────────────────────────────────────────── */

describe('createMarketsMounter', () => {
  it('fetches the payload, renders the card and clears the status', async () => {
    const host = placeholder()
    const fetchPayload = vi.fn().mockResolvedValue(raw())
    const diag = vi.fn()
    const mounter = createMarketsMounter({ fetchPayload, now: () => FETCHED_AT, diag })
    mounter.mountMarkets(document.body)
    mounter.mountMarkets(document.body)
    await flush()
    expect(fetchPayload).toHaveBeenCalledTimes(1)
    expect(fetchPayload).toHaveBeenCalledWith('/api/v1/artifacts/mk-1')
    expect(host.querySelectorAll('.mk-card')).toHaveLength(1)
    expect(host.dataset.marketsHost).toBe('rendered')
    expect(host.querySelector<HTMLElement>('.msg-artifact-markets__status')!.hidden).toBe(true)
    expect(diag).toHaveBeenCalledWith('markets.mount.done', expect.objectContaining({ quote: 2 }))
    mounter.destroyAll()
  })

  it('reports an unreadable payload, a failed fetch and a missing source', async () => {
    const bad = placeholder('/a')
    const broken = placeholder('/b')
    const empty = placeholder('')
    const fetchPayload = vi.fn((url: string) =>
      url === '/a' ? Promise.resolve({ kind: 'pool' }) : Promise.reject(new Error('HTTP 500')),
    )
    createMarketsMounter({ fetchPayload }).mountMarkets(document.body)
    await flush()
    expect(bad.querySelector('.msg-artifact-markets__status')).toHaveTextContent(
      'Markets data could not be read.',
    )
    expect(broken.querySelector('.msg-artifact-markets__status')).toHaveTextContent(
      'Markets card failed to load.',
    )
    expect(empty.querySelector('.msg-artifact-markets__status')).toHaveTextContent(
      'Markets data is unavailable.',
    )
  })

  it('draws no Swap button without onSwap', async () => {
    const host = placeholder()
    createMarketsMounter({ fetchPayload: () => Promise.resolve(raw()) }).mountMarkets(document.body)
    await flush()
    expect(rowOf(host, 'AI/NVDA')).not.toBeNull()
    expect(host.querySelector('.mk-swap')).toBeNull()
  })

  it("calls onSwap with the row's swap", async () => {
    const host = placeholder()
    const onSwap = vi.fn()
    createMarketsMounter({ fetchPayload: () => Promise.resolve(raw()), onSwap }).mountMarkets(
      document.body,
    )
    await flush()
    const button = rowOf(host, 'AI/NVDA').querySelector<HTMLButtonElement>('.mk-swap')!
    expect(button).toHaveTextContent('Swap')
    expect(button.title).toBe('Swap NVDA for AI')
    button.click()
    expect(onSwap).toHaveBeenCalledTimes(1)
    expect(onSwap).toHaveBeenCalledWith({ chainId: 4663, tokenIn: NVDA, tokenOut: AI })
  })

  it('reads a live onSwap getter at render time', async () => {
    const first = placeholder()
    let handler: ((swap: unknown) => void) | null = null
    const mounter = createMarketsMounter({
      fetchPayload: () => Promise.resolve(raw()),
      getOnSwap: () => handler,
    })
    mounter.mountMarkets(document.body)
    await flush()
    expect(first.querySelector('.mk-swap')).toBeNull()
    handler = vi.fn()
    const second = placeholder('/api/v1/artifacts/mk-2')
    mounter.mountMarkets(document.body)
    await flush()
    rowOf(second, 'NVDA/USDG').querySelector<HTMLButtonElement>('.mk-swap')!.click()
    expect(handler).toHaveBeenCalledWith({ chainId: 4663, tokenIn: NVDA, tokenOut: USDG })
    mounter.destroyAll()
  })

  it('↻ re-runs trading.markets with the request params and redraws in place', async () => {
    const host = placeholder()
    const call = vi
      .fn()
      .mockResolvedValue(
        raw({ sections: { quote: [quotePool(token(AI, 'AI', 'Artificial Inu'))], base: [] } }),
      )
    const mounter = createMarketsMounter({ fetchPayload: () => Promise.resolve(raw()), call })
    mounter.mountMarkets(document.body)
    await flush()
    expect(host.querySelectorAll('.mk-row')).toHaveLength(3)
    host.querySelector<HTMLButtonElement>('.mk-refresh')!.click()
    expect(host.querySelector('.mk-card')!.getAttribute('data-refreshing')).toBe('true')
    await flush()
    expect(call).toHaveBeenCalledWith('trading.markets', PARAMS)
    expect(host.querySelectorAll('.mk-card')).toHaveLength(1)
    expect(host.querySelectorAll('.mk-row')).toHaveLength(1)
    expect(host.querySelector('.mk-card')!.hasAttribute('data-refreshing')).toBe(false)
    mounter.destroyAll()
  })

  it('Show lookalikes and Deeper re-run with the flag merged into the params', async () => {
    const host = placeholder()
    const call = vi.fn((_method: string, params: Record<string, unknown>) =>
      Promise.resolve(raw({ request: { kind: 'markets', params } })),
    )
    const mounter = createMarketsMounter({ fetchPayload: () => Promise.resolve(raw()), call })
    mounter.mountMarkets(document.body)
    await flush()
    host.querySelector<HTMLButtonElement>('.mk-link[data-action="lookalikes"]')!.click()
    await flush()
    expect(call).toHaveBeenLastCalledWith('trading.markets', { ...PARAMS, lookalikes: true })
    // The new card was read with lookalikes: it does not offer them again.
    expect(host.querySelector('.mk-link[data-action="lookalikes"]')).toBeNull()
    host.querySelector<HTMLButtonElement>('.mk-link[data-action="deep"]')!.click()
    await flush()
    expect(call).toHaveBeenLastCalledWith('trading.markets', {
      ...PARAMS,
      lookalikes: true,
      deep: true,
    })
    expect(host.querySelector('.mk-link')).toBeNull()
    mounter.destroyAll()
  })

  it('Read again re-runs a rate-limited deep read with the same params', async () => {
    const host = placeholder()
    const DEEP = { ...PARAMS, deep: true }
    const limitedCounts = { ...payload().counts, pageCapHit: false, rateLimited: true }
    const call = vi.fn((_method: string, params: Record<string, unknown>) =>
      Promise.resolve(raw({ request: { kind: 'markets', params } })),
    )
    const mounter = createMarketsMounter({
      fetchPayload: () =>
        Promise.resolve(raw({ counts: limitedCounts, request: { kind: 'markets', params: DEEP } })),
      call,
    })
    mounter.mountMarkets(document.body)
    await flush()
    host.querySelector<HTMLButtonElement>('.mk-link[data-action="again"]')!.click()
    await flush()
    expect(call).toHaveBeenCalledWith('trading.markets', DEEP)
    // The new read finished: nothing to retry.
    expect(host.querySelector('.mk-link[data-action="again"]')).toBeNull()
    mounter.destroyAll()
  })

  it('keeps the card and says why when a re-run fails', async () => {
    vi.useFakeTimers()
    const host = placeholder()
    const call = vi.fn().mockRejectedValue({ code: 'trading.markets.unavailable', message: 'down' })
    const mounter = createMarketsMounter({ fetchPayload: () => Promise.resolve(raw()), call })
    mounter.mountMarkets(document.body)
    await flush()
    const refresh = host.querySelector<HTMLButtonElement>('.mk-refresh')!
    refresh.click()
    await flush()
    expect(host.querySelectorAll('.mk-row')).toHaveLength(3)
    expect(host.querySelector('.mk-error')).toHaveTextContent('refresh failed: down')
    expect(refresh.disabled).toBe(false)
    vi.advanceTimersByTime(LP_ERROR_MS)
    expect(host.querySelector('.mk-error')).toBeNull()
    mounter.destroyAll()
  })

  it('refreshes the relative time once a minute and stops on destroyAll', async () => {
    vi.useFakeTimers()
    let now = FETCHED_AT
    const host = placeholder()
    const mounter = createMarketsMounter({
      fetchPayload: () => Promise.resolve(raw()),
      now: () => now,
    })
    mounter.mountMarkets(document.body)
    await flush()
    const ago = host.querySelector('.mk-ago')!
    expect(ago).toHaveTextContent('just now')
    now += 3 * 60_000
    vi.advanceTimersByTime(LP_CLOCK_MS)
    expect(ago).toHaveTextContent('3m ago')
    expect(vi.getTimerCount()).toBe(1)
    mounter.destroyAll()
    expect(vi.getTimerCount()).toBe(0)
  })
})
