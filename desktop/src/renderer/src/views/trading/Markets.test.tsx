import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { invalidateTrading } from '~/stores/trading'
import { useTradingUi } from '~/stores/trading-ui'
import { Markets } from './Markets'
import { renderDesk } from './test-utils'
import type { MarketsPayload, MarketsPool, SearchToken } from './types'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
const openExternal = vi.fn(async () => {})
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal } }),
  isDesktop: () => true,
}))

const FIXTURE = '../frontend/src/views/trading/__fixtures__/markets/nvda.json'
const NVDA_MARKETS = JSON.parse(readFileSync(FIXTURE, 'utf8')) as MarketsPayload
const NVDA_ADDRESS = NVDA_MARKETS.token.address

const NVDA: SearchToken = {
  chainId: 4663,
  address: NVDA_ADDRESS,
  symbol: 'NVDA',
  name: 'NVIDIA • Robinhood Token',
  decimals: 18,
  logoUrl: null,
  native: false,
  verified: true,
  stockToken: true,
  priceUsd: 240.3,
  liquidityUsd: null,
}
const NVDA_LOOKALIKE: SearchToken = {
  ...NVDA,
  address: '0x1111111111111111111111111111111111111111',
  name: ' NVIDIA Robinhood Token ',
  verified: false,
  stockToken: false,
}

function answer(markets: () => MarketsPayload | Promise<MarketsPayload>) {
  rpcCall.mockImplementation(async (method: string, params: { chainId?: number }) => {
    if (method === 'trading.tokens.search')
      return { tokens: params.chainId === 4663 ? [NVDA_LOOKALIKE, NVDA] : [] }
    if (method === 'trading.markets') return markets()
    return {}
  })
}

function marketsCalls(): Record<string, unknown>[] {
  return rpcCall.mock.calls.filter((c) => c[0] === 'trading.markets').map((c) => c[1])
}

function search(query: string) {
  const field = screen.getByTestId('markets-search')
  fireEvent.change(field, { target: { value: query } })
  fireEvent.submit(field.closest('form')!)
}

beforeEach(() => {
  rpcCall.mockReset()
  openExternal.mockClear()
  useTradingUi.setState({
    bookTab: 'markets',
    deskMode: false,
    swapRequest: null,
    markets: {
      query: '',
      address: null,
      chainId: null,
      minTvlUsd: 10_000,
      lookalikes: false,
      deep: false,
    },
  })
  answer(() => NVDA_MARKETS)
})

describe('Markets · finding the token', () => {
  it('asks for a token before it reads anything', () => {
    renderDesk(<Markets />)
    expect(screen.getByTestId('markets-prompt')).toHaveTextContent('Every pool a token trades in')
    expect(rpcCall).not.toHaveBeenCalled()
  })

  it('takes NVDA typed on a Base desk to Robinhood Chain’s verified Stock Token', async () => {
    renderDesk(<Markets deskChain={8453} />)
    search('NVDA')
    await screen.findByTestId('markets-head')
    // Looked up on both chains, then read by address on the Stock Token's chain.
    const searched = rpcCall.mock.calls.filter((c) => c[0] === 'trading.tokens.search')
    expect(searched.map((c) => (c[1] as { chainId: number }).chainId).sort()).toEqual([4663, 8453])
    expect(marketsCalls()).toEqual([
      { target: NVDA_ADDRESS, chainId: 4663, minTvlUsd: 10_000, lookalikes: false, deep: false },
    ])
    expect(screen.getByTestId('markets-chain-4663')).toHaveAttribute('aria-checked', 'true')
  })

  it('reads a pasted address as given, with no symbol lookup', async () => {
    renderDesk(<Markets />)
    search(NVDA_ADDRESS)
    await screen.findByTestId('markets-head')
    expect(rpcCall.mock.calls.some((c) => c[0] === 'trading.tokens.search')).toBe(false)
    expect(marketsCalls()[0]).toMatchObject({ target: NVDA_ADDRESS, chainId: 4663 })
  })

  it('opens on the token a Holdings row named, without asking', async () => {
    useTradingUi.getState().openMarkets({ chainId: 4663, address: NVDA_ADDRESS, symbol: 'NVDA' })
    renderDesk(<Markets />)
    await screen.findByTestId('markets-head')
    expect(screen.getByTestId('markets-search')).toHaveValue('NVDA')
    expect(marketsCalls()[0]).toMatchObject({ target: NVDA_ADDRESS, chainId: 4663 })
  })
})

describe('Markets · staying put once the board is up', () => {
  function searchCalls() {
    return rpcCall.mock.calls.filter((c) => c[0] === 'trading.tokens.search').length
  }

  it('does not look the symbol up again on a trading event', async () => {
    const { client } = renderDesk(<Markets />)
    search('NVDA')
    await screen.findByTestId('markets-head')
    const before = searchCalls()
    // Every order, trigger and DCA tick invalidates the `trading` prefix.
    invalidateTrading(client)
    await new Promise((r) => setTimeout(r, 30))
    expect(searchCalls()).toBe(before)
    expect(screen.getByTestId('markets-head')).toBeInTheDocument()
  })

  it('keeps the board while the symbol lookup refreshes in the background', async () => {
    const { client } = renderDesk(<Markets />)
    search('NVDA')
    await screen.findByTestId('markets-head')
    let release = () => {}
    rpcCall.mockImplementation(async (method: string, params: { chainId?: number }) => {
      if (method === 'trading.tokens.search') {
        await new Promise<void>((r) => (release = r))
        return { tokens: params.chainId === 4663 ? [NVDA_LOOKALIKE, NVDA] : [] }
      }
      if (method === 'trading.markets') return NVDA_MARKETS
      return {}
    })
    // The lookup's own refresh (stale, refocused, or a key it shares): data in
    // hand, a fetch in flight.
    void client.refetchQueries({
      predicate: (q) => q.queryKey.some((k) => typeof k === 'string' && k.includes('tokens')),
    })
    await waitFor(() => expect(client.isFetching()).toBeGreaterThan(0))
    expect(screen.queryByTestId('markets-resolving')).toBeNull()
    expect(screen.getByTestId('markets-head')).toBeInTheDocument()
    release()
  })
})

describe('Markets · the board', () => {
  async function board() {
    renderDesk(<Markets />)
    search('NVDA')
    return screen.findByTestId('markets-head')
  }

  it('heads with the token, its price and the oracle', async () => {
    const head = await board()
    expect(head).toHaveTextContent('NVDA')
    expect(head).toHaveTextContent('Stock Token')
    expect(head).toHaveTextContent('$240.30')
    expect(screen.getByTestId('markets-oracle')).toHaveTextContent('Oracle$239.74')
    expect(screen.queryByTestId('markets-oracle-badge')).toBeNull()
  })

  it('lists the tokens priced in NVDA first, then what NVDA is priced in', async () => {
    await board()
    const sections = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)
    expect(sections).toEqual(['Priced in NVDA3', 'NVDA priced in2'])

    const quote = within(screen.getByTestId('markets-section-quote')).getAllByTestId('markets-row')
    expect(quote.map((r) => r.querySelector('.trd-mk__pairtext')?.textContent)).toEqual([
      'AI/NVDA',
      'ORBIO/NVDA',
      'SPY/NVDA',
    ])
    const ai = quote[0]!
    expect(ai).toHaveTextContent('Bankr')
    expect(ai.querySelector('.trd-mk__ver')).toHaveTextContent('v4')
    // Bankr launched it on Bankr's own DEX: one word, tinted, not two.
    expect(ai.querySelector('.trd-mk__dex')).toHaveAttribute('data-launcher', 'true')
    expect(ai.querySelector('.trd-mk__launcher')).toBeNull()
    expect(ai.querySelector('.trd-mk__tvl')).toHaveTextContent('$4.7M')
    expect(ai.querySelector('.trd-mk__vol')).toHaveTextContent('$806.5K')
    expect(ai.querySelector('.trd-mk__px')).toHaveTextContent('$0.1131')
    expect(ai.querySelector('.trd-mk__px small')).toHaveTextContent('0.0₃471 NVDA')
    expect(ai.querySelector('.trd-mk__chg')).toHaveAttribute('data-tone', 'down')
    expect(ai.querySelector('.trd-mk__chg')).toHaveTextContent('−3.20%')
    // A memecoin has no oracle: no premium on a quote row.
    expect(ai.querySelector('[data-testid="markets-premium"]')).toBeNull()

    const spy = quote[2]!
    expect(spy).toHaveTextContent('Uniswap')
    expect(spy.querySelector('.trd-mk__fee')).toHaveTextContent('0.025%')
    expect(spy.querySelector('[data-kind="uni"]')).not.toBeNull()
    expect(spy.querySelector('[data-kind="stock"]')).not.toBeNull()
  })

  it('shows the premium against the oracle on the rows NVDA is priced in', async () => {
    await board()
    const base = within(screen.getByTestId('markets-section-base')).getAllByTestId('markets-row')
    expect(base[0]!.querySelector('.trd-mk__pairtext')).toHaveTextContent('NVDA/USDG')
    expect(base[0]!.querySelector('.trd-mk__px small')).toHaveTextContent('240.85 USDG')
    expect(within(base[0]!).getByTestId('markets-premium')).toHaveTextContent('+0.5% vs oracle')
    // The premium is a line of the price cell, not squeezed into 24h beside Age.
    expect(base[0]!.querySelector('.trd-mk__px .trd-mk__premium')).not.toBeNull()
    expect(base[0]!.querySelector('.trd-mk__chg .trd-mk__premium')).toBeNull()
    expect(base[0]!.querySelector('.trd-mk__chg')).toHaveTextContent(/^[+−-]?[\d.]+%$/)
    expect(base[1]!.querySelector('.trd-mk__px small')).toHaveTextContent('0.0573 WETH')
  })

  it('counts what it read, what it left out, and why', async () => {
    await board()
    expect(screen.getByTestId('markets-foot')).toHaveTextContent(
      '5 of 100 pools shown · 61 under $10k · 5 lookalikes hidden',
    )
    expect(screen.queryByTestId('markets-partial')).toBeNull()
  })

  it('fills the ticket from a row: sell NVDA, buy the counterparty', async () => {
    await board()
    const ai = within(screen.getByTestId('markets-section-quote')).getAllByTestId('markets-row')[0]!
    const swap = within(ai).getByTestId('markets-swap')
    expect(swap).toHaveAccessibleName('Swap NVDA for AI')
    fireEvent.click(swap)
    const s = useTradingUi.getState()
    expect(s.bookTab).toBe('swap')
    expect(s.swapRequest).toMatchObject({
      chainId: 4663,
      tokenIn: { address: NVDA_ADDRESS, symbol: 'NVDA', decimals: 18, stockToken: true },
      tokenOut: { symbol: 'AI', name: 'Artificial Inu', verified: false },
    })
  })

  it('opens the pool on GeckoTerminal', async () => {
    await board()
    const ai = within(screen.getByTestId('markets-section-quote')).getAllByTestId('markets-row')[0]!
    fireEvent.click(within(ai).getByRole('button', { name: 'Open on GeckoTerminal' }))
    expect(openExternal).toHaveBeenCalledWith(NVDA_MARKETS.sections.quote[0]!.url)
  })
})

describe('Markets · filters', () => {
  it('re-reads with each filter, every param in the request', async () => {
    renderDesk(<Markets />)
    search('NVDA')
    await screen.findByTestId('markets-head')

    fireEvent.click(screen.getByTestId('markets-tvl-100000'))
    await waitFor(() => expect(marketsCalls().at(-1)).toMatchObject({ minTvlUsd: 100_000 }))
    fireEvent.click(screen.getByTestId('markets-lookalikes'))
    await waitFor(() =>
      expect(marketsCalls().at(-1)).toMatchObject({ minTvlUsd: 100_000, lookalikes: true }),
    )
    fireEvent.click(await screen.findByTestId('markets-deep'))
    await waitFor(() =>
      expect(marketsCalls().at(-1)).toMatchObject({
        minTvlUsd: 100_000,
        lookalikes: true,
        deep: true,
      }),
    )
    expect(useTradingUi.getState().markets).toMatchObject({
      minTvlUsd: 100_000,
      lookalikes: true,
      deep: true,
    })
    // Already deep: Deeper is not offered again.
    await waitFor(() => expect(screen.queryByTestId('markets-deep')).toBeNull())
    // A new token is a new read, and starts shallow.
    search('TSLA')
    expect(useTradingUi.getState().markets.deep).toBe(false)
  })

  // Live test 2026-10-06: Deeper was clicked on a read `limit` had cut, and
  // nothing changed — there was nothing deeper to read.
  it('offers Deeper only when the page cap ended the read, and counts what the limit cut', async () => {
    answer(() => ({
      ...NVDA_MARKETS,
      counts: { ...NVDA_MARKETS.counts, limited: 12, pageCapHit: false },
    }))
    renderDesk(<Markets />)
    search('NVDA')
    await screen.findByTestId('markets-head')
    expect(screen.getByTestId('markets-foot')).toHaveTextContent(
      '5 of 100 pools shown · 61 under $10k · 5 lookalikes hidden · 12 more over the limit',
    )
    expect(screen.queryByTestId('markets-deep')).toBeNull()
  })

  it('marks a lookalike row when they are asked for', async () => {
    const lookalike: MarketsPool = {
      ...NVDA_MARKETS.sections.quote[1]!,
      poolAddress: '0x9999999999999999999999999999999999999999',
      pair: 'NVDA/NVDA',
      counterparty: { ...NVDA_MARKETS.sections.quote[1]!.counterparty, lookalike: true },
    }
    answer(() => ({
      ...NVDA_MARKETS,
      sections: { quote: [lookalike], base: [] },
    }))
    renderDesk(<Markets />)
    search('NVDA')
    const row = await screen.findByTestId('markets-row')
    expect(row).toHaveAttribute('data-lookalike', 'true')
    expect(row.querySelector('[data-kind="lookalike"]')).toHaveTextContent('Lookalike')
    // Its name follows the pair, so two "NVDA/NVDA" rows can be told apart.
    expect(within(row).getByTestId('markets-cpname')).toHaveTextContent(lookalike.counterparty.name)
    // The empty section still says what it looked for.
    expect(screen.getByTestId('markets-section-base')).toHaveTextContent(
      'No pools against NVDA above $10k',
    )
  })
})

describe('Markets · states', () => {
  it('draws the rows’ shape while the read is on its way', async () => {
    answer(() => new Promise(() => {}))
    renderDesk(<Markets />)
    search('NVDA')
    const loading = await screen.findByTestId('markets-loading')
    expect(loading).toHaveTextContent('A first read of a token can take up to a minute')
  })

  it('says nothing is deep enough instead of drawing two empty lists', async () => {
    answer(() => ({
      ...NVDA_MARKETS,
      counts: { ...NVDA_MARKETS.counts, shown: 0, belowMinTvl: 95 },
      sections: { quote: [], base: [] },
    }))
    renderDesk(<Markets />)
    search('NVDA')
    expect(await screen.findByTestId('markets-empty')).toHaveTextContent('No pools above $10k')
    expect(screen.queryByTestId('markets-section-quote')).toBeNull()
    expect(screen.getByTestId('markets-foot')).toHaveTextContent('95 under $10k')
  })

  it('prints a premium under 0.05 % as a flat 0.0%, never −0.0%', async () => {
    const usdg = NVDA_MARKETS.sections.base[0]!
    answer(() => ({
      ...NVDA_MARKETS,
      sections: { ...NVDA_MARKETS.sections, base: [{ ...usdg, premiumPct: -0.004 }] },
    }))
    renderDesk(<Markets />)
    search('NVDA')
    const premium = await screen.findByTestId('markets-premium')
    expect(premium).toHaveTextContent(/^0\.0% vs oracle$/)
    expect(premium).toHaveAttribute('data-tone', 'flat')
  })

  it('says a 429 cut the read short in the footer, and offers Deeper again', async () => {
    answer(() => ({
      ...NVDA_MARKETS,
      partial: true,
      counts: { ...NVDA_MARKETS.counts, pageCapHit: false, rateLimited: true },
    }))
    renderDesk(<Markets />)
    search('NVDA')
    expect(await screen.findByTestId('markets-ratelimited')).toHaveTextContent(
      'The source rate-limited this read',
    )
    expect(screen.getByTestId('markets-foot')).toContainElement(
      screen.getByTestId('markets-ratelimited'),
    )
    expect(screen.getByTestId('markets-deep')).toBeInTheDocument()
    expect(screen.queryByTestId('markets-again')).toBeNull()
  })

  it('offers Read again on a deep read a 429 cut short', async () => {
    answer(() => ({
      ...NVDA_MARKETS,
      partial: true,
      counts: { ...NVDA_MARKETS.counts, pageCapHit: true, rateLimited: true },
      request: {
        kind: 'markets',
        params: { ...NVDA_MARKETS.request!.params, deep: true },
      },
    }))
    renderDesk(<Markets />)
    search('NVDA')
    const again = await screen.findByTestId('markets-again')
    expect(again).toHaveTextContent('Read again')
    expect(screen.queryByTestId('markets-deep')).toBeNull()
    const before = marketsCalls().length
    fireEvent.click(again)
    await waitFor(() => expect(marketsCalls().length).toBe(before + 1))
  })

  it('flags a rate-limited read as partial, with the source’s warning', async () => {
    answer(() => ({
      ...NVDA_MARKETS,
      partial: true,
      warnings: ['GeckoTerminal rate limit: showing the first 40 pools'],
    }))
    renderDesk(<Markets />)
    search('NVDA')
    expect(await screen.findByTestId('markets-partial')).toHaveTextContent('Partial')
    expect(screen.getByTestId('quote-warnings')).toHaveTextContent(
      'GeckoTerminal rate limit: showing the first 40 pools',
    )
  })

  it('reports a failed read in the gateway’s words, and reads again on retry', async () => {
    let fail = true
    answer(() => {
      if (fail) throw new Error('trading.markets.unavailable: GeckoTerminal did not answer')
      return NVDA_MARKETS
    })
    renderDesk(<Markets />)
    search('NVDA')
    const error = await screen.findByTestId('markets-error')
    expect(error).toHaveTextContent('GeckoTerminal did not answer')
    fail = false
    fireEvent.click(within(error).getByTestId('trading-error-retry'))
    expect(await screen.findByTestId('markets-head')).toBeInTheDocument()
  })

  it('badges a stale or paused oracle', async () => {
    answer(() => ({
      ...NVDA_MARKETS,
      token: {
        ...NVDA_MARKETS.token,
        oracle: { ...NVDA_MARKETS.token.oracle!, stale: true, paused: true },
      },
    }))
    renderDesk(<Markets />)
    search('NVDA')
    const badge = await screen.findByTestId('markets-oracle-badge')
    expect(badge).toHaveTextContent('Paused')
    expect(badge).toHaveAttribute('data-tone', 'danger')
  })

  it('caps a section at 40 rows and expands the rest in place', async () => {
    const many = Array.from({ length: 45 }, (_, i) => ({
      ...NVDA_MARKETS.sections.quote[0]!,
      poolAddress: `0x${String(i).padStart(40, '0')}`,
    }))
    answer(() => ({ ...NVDA_MARKETS, sections: { quote: many, base: [] } }))
    renderDesk(<Markets />)
    search('NVDA')
    const more = await screen.findByTestId('markets-more-quote')
    expect(more).toHaveTextContent('+5 more')
    expect(screen.getAllByTestId('markets-row')).toHaveLength(40)
    fireEvent.click(more)
    expect(screen.getAllByTestId('markets-row')).toHaveLength(45)
  })
})

describe('Markets · a symbol several tokens share', () => {
  const ai = (address: string, name: string, liquidityUsd: number | null): SearchToken => ({
    ...NVDA,
    address,
    symbol: 'AI',
    name,
    stockToken: false,
    priceUsd: 0.1,
    liquidityUsd,
  })
  const PIN = ai('0x5555555555555555555555555555555555555555', 'AI PIN', 15_000)
  const INU = ai(NVDA_MARKETS.sections.quote[0]!.counterparty.address, 'Artificial Inu', 131_000)

  beforeEach(() => {
    rpcCall.mockImplementation(async (method: string, params: { chainId?: number }) => {
      // The search lists AI PIN first: the live test took it.
      if (method === 'trading.tokens.search')
        return { tokens: params.chainId === 4663 ? [PIN, INU] : [] }
      if (method === 'trading.markets') return NVDA_MARKETS
      return {}
    })
  })

  it('reads the deepest match and says which token it is showing', async () => {
    renderDesk(<Markets />)
    search('AI')
    await screen.findByTestId('markets-head')
    expect(marketsCalls()[0]).toMatchObject({ target: INU.address, chainId: 4663 })
    expect(screen.getByTestId('markets-picked')).toHaveTextContent(
      'Showing Artificial Inu · 0x2e8c…1e18',
    )
    expect(screen.getByTestId('markets-matches')).toHaveTextContent('2 matches')
  })

  it('lets the user take another match from the list', async () => {
    renderDesk(<Markets />)
    search('AI')
    await screen.findByTestId('markets-head')
    fireEvent.click(screen.getByTestId('markets-matches'))
    const menu = screen.getByRole('menu', { name: 'Tokens with this symbol' })
    // Its own width, portalled out of the tab's scroller: the house 200 px
    // menu left the names 2 px.
    expect(menu).toHaveClass('trd-mk__matchmenu')
    expect(menu.parentElement).toBe(document.body)
    const items = within(menu).getAllByRole('menuitemcheckbox')
    // The full name, the symbol and short address under it, the liquidity.
    expect(items[0]!.querySelector('.mac-menu__label')).toHaveTextContent(/^Artificial Inu$/)
    expect(items[0]!.querySelector('.mac-menu__detail')).toHaveTextContent('AI · 0x2e8c…1e18')
    expect(within(items[0]!).getByTestId('markets-match-liquidity')).toHaveTextContent(
      'Liquidity$131K',
    )
    expect(items[1]!.querySelector('.mac-menu__label')).toHaveTextContent(/^AI PIN$/)
    expect(items[1]!.querySelector('.mac-menu__detail')).toHaveTextContent('AI · 0x5555…5555')
    expect(items[0]).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(items[1]!)
    await waitFor(() =>
      expect(marketsCalls().at(-1)).toMatchObject({ target: PIN.address, chainId: 4663 }),
    )
    expect(screen.queryByRole('menu')).toBeNull()
    await waitFor(() =>
      expect(screen.getByTestId('markets-picked')).toHaveTextContent(
        'Showing AI PIN · 0x5555…5555',
      ),
    )
    // The field still says what was typed; the list is still there to go back.
    expect(screen.getByTestId('markets-search')).toHaveValue('AI')
    expect(screen.getByTestId('markets-matches')).toHaveTextContent('2 matches')
  })

  it('lists the same matches on every search: the chosen chain only, deduped', async () => {
    const BASE_AI = {
      ...PIN,
      chainId: 8453,
      address: '0x8888888888888888888888888888888888888888',
      name: 'Base AI',
      liquidityUsd: 900_000,
    }
    let flip = false
    rpcCall.mockImplementation(async (method: string, params: { chainId?: number }) => {
      if (method === 'trading.tokens.search') {
        if (params.chainId === 8453) return { tokens: [BASE_AI] }
        // Each search answers in another order, with INU listed twice.
        flip = !flip
        return { tokens: flip ? [PIN, INU, { ...INU }] : [INU, PIN] }
      }
      if (method === 'trading.markets') return NVDA_MARKETS
      return {}
    })
    renderDesk(<Markets />)
    search('AI')
    await screen.findByTestId('markets-head')
    expect(screen.getByTestId('markets-matches')).toHaveTextContent('2 matches')
    search('NVDA')
    await waitFor(() => expect(screen.queryByTestId('markets-matches')).toBeNull())
    search('AI')
    await waitFor(() =>
      expect(screen.getByTestId('markets-picked')).toHaveTextContent('Showing Artificial Inu'),
    )
    expect(screen.getByTestId('markets-matches')).toHaveTextContent('2 matches')
  })

  it('offers no list when the symbol is one token', async () => {
    renderDesk(<Markets />)
    search('NVDA')
    await screen.findByTestId('markets-head')
    expect(screen.getByTestId('markets-picked')).toHaveTextContent(
      'Showing NVIDIA • Robinhood Token',
    )
    expect(screen.queryByTestId('markets-matches')).toBeNull()
  })
})

describe('Markets · the native coin', () => {
  it('names a zero-address counterparty ETH, never 0x000…', async () => {
    const ZERO = '0x0000000000000000000000000000000000000000'
    const weth = NVDA_MARKETS.sections.base[1]!
    const eth: MarketsPool = {
      ...weth,
      pair: 'NVDA/0x0000…0000',
      counterparty: {
        ...weth.counterparty,
        address: ZERO,
        symbol: '0x0000…0000',
        name: 'Ether',
        native: true,
      },
      swap: { chainId: 4663, tokenIn: NVDA_ADDRESS, tokenOut: ZERO },
    }
    answer(() => ({ ...NVDA_MARKETS, sections: { quote: [], base: [eth] } }))
    renderDesk(<Markets />)
    search('NVDA')
    const row = await screen.findByTestId('markets-row')
    expect(row.querySelector('.trd-mk__pairtext')).toHaveTextContent('NVDA/ETH')
    expect(row.querySelector('.trd-mk__in')).toHaveTextContent(/ ETH$/)
    expect(row).not.toHaveTextContent('0x0000')
    expect(within(row).getByTestId('markets-swap')).toHaveAccessibleName('Swap NVDA for ETH')
  })
})
