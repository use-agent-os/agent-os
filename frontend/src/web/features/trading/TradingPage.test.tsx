// The Trading page end to end against a fake engine: the gates, each view's
// figures, the empty and error states, the privacy switch, and that nothing
// it renders offers to act.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import marketsFixture from '@/views/trading/__fixtures__/markets/nvda.json'
import '@/test/web'
import { renderWeb } from '@/test/render-web'
import { RpcError } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import {
  ETH,
  WALLET_A,
  WALLET_B,
  chainRead,
  entry,
  holding,
  lpBook,
  lpPosition,
  order,
  portfolio,
  token,
  totals,
  tradingRpc,
  tradingStatus,
  vaultStatus,
  wallet,
} from '@/test/web-trading'
import { BALANCE_MASK, reloadBalancePrivacy } from './safety'
import { TradingPage } from './TradingPage'

function renderPage(rpc = tradingRpc()) {
  return renderWeb(<TradingPage />, { rpc })
}

async function openView(name: RegExp) {
  await screen.findByRole('radiogroup', { name: /views/i })
  fireEvent.click(screen.getByRole('radio', { name }))
}

beforeEach(() => {
  useConnection.getState().setState('connected')
  localStorage.clear()
  reloadBalancePrivacy()
})

describe('TradingPage gates', () => {
  it('says trading is off and reads nothing else', async () => {
    const rpc = tradingRpc().handle('trading.status', tradingStatus({ enabled: false }))
    renderPage(rpc)
    expect(await screen.findByText('Trading is off')).toBeInTheDocument()
    expect(rpc.calls.map((c) => c.method)).toEqual(['trading.status'])
  })

  it('says when no vault exists', async () => {
    const rpc = tradingRpc().handle(
      'wallet.status',
      vaultStatus({ initialized: false, unlocked: false }),
    )
    renderPage(rpc)
    expect(await screen.findByText('No wallet vault yet')).toBeInTheDocument()
    expect(screen.getByText(/agentos wallet setup command/)).toBeInTheDocument()
    expect(screen.getByTestId('trading-page').textContent).not.toContain('`')
  })

  it('says the vault is locked and offers no way to unlock it', async () => {
    const rpc = tradingRpc().handle('wallet.status', vaultStatus({ unlocked: false }))
    renderPage(rpc)
    expect(await screen.findByText('The wallet vault is locked')).toBeInTheDocument()
    expect(screen.getByText(/agentos wallet unlock command/)).toBeInTheDocument()
    expect(screen.getByTestId('trading-page').textContent).not.toContain('`')
    expect(screen.queryByRole('button', { name: /unlock/i })).toBeNull()
    expect(document.querySelector('input[type="password"]')).toBeNull()
    expect(rpc.callsTo('wallet.list')).toEqual([])
  })

  it('says when there are no wallets', async () => {
    const rpc = tradingRpc().handle('wallet.list', { wallets: [], primary: null })
    renderPage(rpc)
    expect(await screen.findByText('No wallets yet')).toBeInTheDocument()
  })

  it('shows the engine error with a retry', async () => {
    const rpc = tradingRpc().handle('trading.status', () => {
      throw new Error('engine unreachable')
    })
    renderPage(rpc)
    expect(await screen.findByText('Could not read the trading engine')).toBeInTheDocument()
    expect(screen.getByText(/engine unreachable/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry|try again/i })).toBeInTheDocument()
  })
})

describe('TradingPage views', () => {
  it('shows the overview totals', async () => {
    renderPage()
    expect(await screen.findAllByText('$12,480.52')).not.toHaveLength(0)
  })

  it('lists holdings and hides every figure behind the privacy switch', async () => {
    renderPage()
    await openView(/holdings/i)
    const table = await screen.findByRole('table', { name: 'Holdings' })
    expect(within(table).getAllByText('ETH')).not.toHaveLength(0)
    expect(within(table).getByText('$4,800.00')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /hide amounts/i }))
    expect(within(table).queryByText('$4,800.00')).toBeNull()
    expect(within(table).getAllByText(BALANCE_MASK).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: /show amounts/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(localStorage.getItem('agentos-web.trading.balancesHidden')).toBe('true')
  })

  it('gives the folded "other" slice its own colour, never a holding\'s', async () => {
    const holdings = ['A', 'B', 'C', 'D', 'E', 'F', 'G'].map((sym, i) =>
      holding(token(sym), { allocationPct: 30 - i * 3 }),
    )
    renderPage(tradingRpc().handle('trading.portfolio', portfolio({ holdings })))
    const legend = await screen.findByText('Other')
    const item = legend.closest('li')!
    expect(item.querySelector('[data-series]')).toHaveAttribute('data-series', 'other')
  })

  it('shows the empty holdings state', async () => {
    const rpc = tradingRpc().handle(
      'trading.portfolio',
      portfolio({ holdings: [], totals: totals({ valueUsd: 0 }) }),
    )
    renderPage(rpc)
    await openView(/holdings/i)
    expect(await screen.findByText('No holdings')).toBeInTheDocument()
  })

  it('lists orders without a cancel or approve button', async () => {
    const rpc = tradingRpc()
    renderPage(rpc)
    await openView(/orders/i)
    expect(await screen.findAllByTestId('trd-order')).not.toHaveLength(0)
    for (const name of [/cancel/i, /approve/i, /reject/i, /pause/i]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(rpc.callsTo('trading.dca.list')).toEqual([{ all: true }])
  })

  it('lists an order awaiting approval as open and points to the desktop app', async () => {
    const rpc = tradingRpc().handle('trading.orders.list', {
      orders: [order({ orderId: 'w1', status: 'awaiting_approval' })],
    })
    renderPage(rpc)
    await openView(/orders/i)
    const open = await screen.findByRole('region', { name: 'Open orders' })
    expect(within(open).getAllByTestId('trd-order')).toHaveLength(1)
    expect(screen.queryByRole('region', { name: 'Finished orders' })).toBeNull()
    expect(screen.getByRole('note')).toHaveTextContent(/1 order waits for approval/)
  })

  it("reads a DCA plan's schedule as the engine words it, without doubling it", async () => {
    const rpc = tradingRpc().handle('trading.dca.list', {
      mandates: [
        {
          name: 'Weekly ETH',
          status: 'active',
          chain: { id: 8453 },
          token: ETH,
          schedule: { label: 'every week' },
          budget: { spentUsd: 100, capUsd: 500 },
        },
      ],
    })
    renderPage(rpc)
    await openView(/orders/i)
    const row = await screen.findByTestId('trd-automation-dca')
    expect(row).toHaveTextContent(/Runs every week/)
    expect(row).not.toHaveTextContent(/every every/i)
  })

  it('shows the orders error state', async () => {
    const rpc = tradingRpc().handle('trading.orders.list', () => {
      throw new Error('ledger busy')
    })
    renderPage(rpc)
    await openView(/orders/i)
    expect(await screen.findByText(/ledger busy/)).toBeInTheDocument()
  })

  it('shows history with a negative realized PnL and an explorer link', async () => {
    const rpc = tradingRpc().handle('trading.history', { entries: [entry()] })
    renderPage(rpc)
    await openView(/history/i)
    const pnl = await screen.findByRole('region', { name: 'Profit and loss' })
    const realized = within(pnl).getByText('−$84.10')
    expect(realized).toHaveAttribute('data-tone', 'down')
    await screen.findAllByTestId('trd-entry')
    const link = screen.getAllByRole('link').find((a) => a.getAttribute('href')?.includes('/tx/'))
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link?.getAttribute('href')).toMatch(/^https:\/\/basescan\.org\/tx\/0x[0-9a-f]{64}$/)
  })

  it("heads each day with the entries' own date (the engine sends milliseconds)", async () => {
    const day = 86_400_000
    const ts = Date.UTC(2025, 10, 20, 12)
    const rpc = tradingRpc().handle('trading.history', {
      entries: [entry({ id: 'a', ts }), entry({ id: 'b', ts: ts - 3 * day })],
    })
    renderPage(rpc)
    await openView(/history/i)
    const full = new Intl.DateTimeFormat(undefined, { dateStyle: 'full' })
    expect(await screen.findByRole('region', { name: full.format(ts) })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: full.format(ts - 3 * day) })).toBeInTheDocument()
  })

  it('shows the empty history state', async () => {
    renderPage(tradingRpc().handle('trading.history', { entries: [] }))
    await openView(/history/i)
    expect(await screen.findByText('No history yet')).toBeInTheDocument()
  })

  it('looks up markets only when asked and lists the pools', async () => {
    const rpc = tradingRpc().handle('trading.markets', marketsFixture)
    renderPage(rpc)
    await openView(/markets/i)
    expect(rpc.callsTo('trading.markets')).toEqual([])
    fireEvent.change(screen.getByRole('textbox'), { target: { value: ' NVDA ' } })
    fireEvent.submit(screen.getByRole('search'))
    expect((await screen.findAllByTestId('trd-pool')).length).toBeGreaterThan(0)
    for (const table of screen.getAllByRole('table'))
      expect(table).toHaveAccessibleName(/priced in/i)
    expect(rpc.callsTo('trading.markets')[0]).toMatchObject({ target: 'NVDA', chainId: 8453 })
    expect(screen.queryByRole('button', { name: /swap/i })).toBeNull()
  })

  describe('an ambiguous symbol', () => {
    const AERO = `0x${'3f'.repeat(20)}`
    const AERO_FAKE = `0x${'4e'.repeat(20)}`

    function ambiguous(candidates: unknown[], code = 'trading.invalid') {
      const err = new RpcError("Symbol 'AERO' is ambiguous on Base; pass the address")
      err.code = code
      err.details = { candidates }
      return err
    }

    function candidate(address: string, over: Record<string, unknown> = {}) {
      return {
        chainId: 8453,
        address,
        symbol: 'AERO',
        name: 'Aero',
        decimals: 18,
        logoUrl: 'https://evil.example/logo.png',
        ...over,
      }
    }

    async function search(rpc: ReturnType<typeof tradingRpc>, text = 'AERO') {
      renderPage(rpc)
      await openView(/markets/i)
      fireEvent.change(screen.getByRole('textbox'), { target: { value: text } })
      fireEvent.submit(screen.getByRole('search'))
    }

    it('lists the candidates, with no Retry, and reads the picked one by address', async () => {
      const rpc = tradingRpc().handle('trading.markets', (p: Record<string, unknown>) => {
        if (p.target === 'AERO')
          throw ambiguous([candidate(AERO), candidate(AERO_FAKE, { name: 'Aero Classic' })])
        return marketsFixture
      })
      await search(rpc)
      expect(await screen.findByText('Several tokens use this symbol. Pick one:')).toBeVisible()
      expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
      const list = screen.getByRole('list', { name: /several tokens/i })
      const picks = within(list).getAllByRole('button')
      expect(picks).toHaveLength(2)
      expect(picks[0]).toHaveAccessibleName(new RegExp(AERO))
      expect(picks[0]).toHaveTextContent('0x3f3f…3f3f')
      expect(document.querySelector('img')).toBeNull()

      fireEvent.click(within(list).getByRole('button', { name: /Aero Classic/ }))
      expect((await screen.findAllByTestId('trd-pool')).length).toBeGreaterThan(0)
      expect(rpc.callsTo('trading.markets').at(-1)).toEqual({ target: AERO_FAKE, chainId: 8453 })
      for (const params of rpc.callsTo('trading.markets'))
        expect(params).not.toHaveProperty('refresh')
    })

    it('shows a hostile name as clamped, inert text', async () => {
      const hostile = `<img src=x onerror="alert(1)">‮DSU​${'A'.repeat(80)}`
      const rpc = tradingRpc().handle('trading.markets', () => {
        throw ambiguous([candidate(AERO, { name: hostile, symbol: 'AE‮RO' })])
      })
      await search(rpc)
      const pick = await screen.findByRole('button', { name: /AERO/ })
      expect(document.querySelector('img')).toBeNull()
      expect(pick.textContent).not.toMatch(/[‮​]/)
      expect(pick.textContent).toContain('…')
      expect(pick.textContent).not.toContain('A'.repeat(60))
    })

    it('offers no malformed address, and lists at most eight', async () => {
      const many = Array.from({ length: 10 }, (_, i) =>
        candidate(`0x${String(i).repeat(40)}`, { name: `Aero ${i}` }),
      )
      const rpc = tradingRpc().handle('trading.markets', () => {
        throw ambiguous([candidate('0xnot-an-address', { name: 'Bad' }), ...many])
      })
      await search(rpc)
      const list = await screen.findByRole('list', { name: /several tokens/i })
      expect(within(list).getAllByRole('button')).toHaveLength(8)
      expect(within(list).queryByText('Bad')).toBeNull()
      expect(screen.getByText(/2 more matches are not listed/)).toBeInTheDocument()
    })

    it('falls back to the error when no candidate is valid', async () => {
      const rpc = tradingRpc().handle('trading.markets', () => {
        throw ambiguous([candidate('0x1234')])
      })
      await search(rpc)
      expect(await screen.findByText('Could not read the markets')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    })

    it('keeps the plain error state for any other failure', async () => {
      const rpc = tradingRpc().handle('trading.markets', () => {
        throw ambiguous([candidate(AERO)], 'trading.unavailable')
      })
      await search(rpc)
      expect(await screen.findByText('Could not read the markets')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
      expect(screen.queryByText(/several tokens/i)).toBeNull()
    })
  })

  it('lists liquidity positions from the cached book, never a fresh scan', async () => {
    const rpc = tradingRpc().handle(
      'trading.lp.book',
      lpBook([lpPosition(), lpPosition({ id: 'x', tokenId: '9', status: 'below-range' })]),
    )
    renderPage(rpc)
    await openView(/liquidity/i)
    expect(await screen.findAllByTestId('trd-lp')).toHaveLength(2)
    expect(screen.getByRole('table', { name: 'Liquidity' })).toBeInTheDocument()
    expect(screen.getByText('1 position out of range')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /collect|remove/i })).toBeNull()
    for (const params of rpc.callsTo('trading.lp.book'))
      expect(params).not.toHaveProperty('refresh')
  })

  it("labels a pair's pool share and shows gauge rewards, masked with the amounts", async () => {
    const staked = lpPosition({
      share: 0.0123,
      staked: true,
      rewards: {
        token: { address: '0xaero', symbol: 'AERO', decimals: 18, priceUsd: 1.1 },
        amount: { raw: '12500000000000000000', human: '12.5', usd: 13.75 },
      },
    })
    const book = lpBook([staked])
    book.totals.rewardsUsd = 13.75
    renderPage(tradingRpc().handle('trading.lp.book', book))
    await openView(/liquidity/i)
    const row = await screen.findByTestId('trd-lp')
    expect(row.textContent).toContain('1.23% of pool')
    expect(row.textContent).toContain('12.5 AERO')
    const summary = screen.getByTestId('trd-liquidity')
    expect(within(summary).getByText('Rewards')).toBeInTheDocument()
    expect(within(summary).getByText('$13.75')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /hide amounts/i }))
    expect(row.textContent).not.toContain('12.5 AERO')
    expect(within(summary).queryByText('$13.75')).toBeNull()
  })

  it('shows the empty liquidity state', async () => {
    renderPage(tradingRpc().handle('trading.lp.book', lpBook([])))
    await openView(/liquidity/i)
    expect(await screen.findByText('No liquidity positions')).toBeInTheDocument()
  })
})

describe('TradingPage untrusted and edge-case figures', () => {
  it('renders a hostile token name as inert, truncated text', async () => {
    const evil = token('<img src=x onerror=alert(1)>', {
      name: `<script>alert('x')</script>${'A'.repeat(200)}`,
      logoUrl: 'https://evil.example/logo.png',
    })
    const rpc = tradingRpc().handle('trading.portfolio', portfolio({ holdings: [holding(evil)] }))
    renderPage(rpc)
    await openView(/holdings/i)
    const table = await screen.findByRole('table', { name: 'Holdings' })
    expect(table.querySelector('script, img')).toBeNull()
    const name = within(table).getByText(/^<script>/)
    expect(name.textContent!.length).toBeLessThanOrEqual(28)
    expect(name.textContent!.endsWith('…')).toBe(true)
  })

  it('formats zero, dust and very large amounts without exponents', async () => {
    const rpc = tradingRpc().handle(
      'trading.portfolio',
      portfolio({
        holdings: [
          holding(ETH, { amount: '123456789012345678.5', valueUsd: 1e15, allocationPct: 100 }),
          holding(token('ZERO'), { amount: '0', valueUsd: 0, priceUsd: 0 }),
          holding(token('DUST'), { amount: '0.000000000000000001', valueUsd: 0.000001 }),
        ],
      }),
    )
    renderPage(rpc)
    await openView(/holdings/i)
    const table = await screen.findByRole('table', { name: 'Holdings' })
    expect(table.textContent).not.toMatch(/\de[+-]?\d/i)
    expect(table.textContent).not.toMatch(/NaN|Infinity|undefined/)
  })

  it('tones an unchanged price flat, not as a gain (the shared pnlTone)', async () => {
    renderPage()
    await openView(/holdings/i)
    const table = await screen.findByRole('table', { name: 'Holdings' })
    const usdc = within(table)
      .getAllByRole('row')
      .find((r) => /USDC/.test(r.textContent ?? ''))!
    expect(usdc.querySelector('[data-col="change"] .trd-num')!.getAttribute('data-tone')).toBe(
      'flat',
    )
  })

  it("masks a trigger's action, which names an amount, behind the privacy switch", async () => {
    const trigger = {
      id: 'trg_1',
      name: 'Dip buy',
      status: 'armed',
      chain: { id: 8453 },
      token: token('ETH'),
      bracket: null,
      condition: { label: 'under $3,800' },
      action: { label: 'buy $50 of ETH with USDC' },
    }
    const rpc = tradingRpc().handle('trading.trigger.list', {
      kind: 'triggers',
      triggers: [trigger],
      totals: { count: 1, armed: 1, awaiting: 0, triggered: 0 },
    })
    renderPage(rpc)
    await openView(/orders/i)
    const row = await screen.findByTestId('trd-automation-trigger')
    expect(row.textContent).toContain('buy $50 of ETH')
    fireEvent.click(screen.getByRole('button', { name: /hide/i }))
    await waitFor(() => expect(row.textContent).not.toContain('$50'))
    expect(row.textContent).toContain('under $3,800')
  })

  it('refetches what is on screen after a trading event', async () => {
    const rpc = tradingRpc()
    renderPage(rpc)
    await screen.findAllByText('$12,480.52')
    const before = rpc.callsTo('trading.portfolio').length
    act(() => rpc.emit('trading.changed'))
    await waitFor(() => expect(rpc.callsTo('trading.portfolio').length).toBeGreaterThan(before))
  })
})

describe('TradingPage wallet switch', () => {
  it("never shows one wallet's figures while another wallet's read is in flight", async () => {
    let releaseB: (() => void) | null = null
    const rpc = tradingRpc()
      .handle('wallet.list', {
        wallets: [wallet(WALLET_A, 'Main', true), wallet(WALLET_B, 'Side')],
        primary: WALLET_A,
      })
      .handle('trading.portfolio', (params: Record<string, unknown>) =>
        params.wallet === WALLET_B
          ? new Promise((resolve) => {
              releaseB = () =>
                resolve(portfolio({ totals: totals({ valueUsd: 77 }), holdings: [] }))
            })
          : portfolio(),
      )
    renderPage(rpc)
    expect(await screen.findAllByText('$12,480.52')).not.toHaveLength(0)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: WALLET_B } })
    await waitFor(() => expect(releaseB).not.toBeNull())
    expect(screen.queryByText('$12,480.52')).toBeNull()

    act(() => releaseB!())
    expect(await screen.findAllByText('$77.00')).not.toHaveLength(0)
  })
})

describe('TradingPage wallet removed elsewhere', () => {
  it('falls back to all wallets instead of reading a wallet that is gone', async () => {
    let list = [wallet(WALLET_A, 'Main', true), wallet(WALLET_B, 'Side')]
    const rpc = tradingRpc().handle('wallet.list', () => ({ wallets: list, primary: WALLET_A }))
    renderPage(rpc)
    const picker = await screen.findByRole('combobox')
    fireEvent.change(picker, { target: { value: WALLET_B } })
    await waitFor(() =>
      expect(rpc.callsTo('trading.portfolio')).toContainEqual({ wallet: WALLET_B }),
    )

    list = [wallet(WALLET_A, 'Main', true)]
    act(() => rpc.emit('_hello'))
    await waitFor(() => expect(screen.getByRole('combobox')).toHaveValue(''))
    await waitFor(() => expect(rpc.callsTo('trading.portfolio').at(-1)).toEqual({}))
  })
})

describe('TradingPage unfinished or failed chain reads', () => {
  const RPC_429 = 'base: HTTP 429 from https://mainnet.base.org Too Many Requests'

  function emptyPortfolio(over: Parameters<typeof portfolio>[0] = {}) {
    return portfolio({ holdings: [], totals: totals({ valueUsd: 0 }), ...over })
  }

  it('says a sync is running beside the totals, and that an empty list is not final', async () => {
    const rpc = tradingRpc().handle(
      'trading.portfolio',
      emptyPortfolio({ syncing: true, syncKind: 'initial' }),
    )
    renderPage(rpc)
    const overview = await screen.findByTestId('trd-overview')
    expect(within(overview).getByRole('status')).toHaveTextContent('Syncing with the chain…')
    await openView(/holdings/i)
    expect(await screen.findByText('Not read from the chain yet')).toBeInTheDocument()
    expect(screen.queryByText('No holdings')).toBeNull()
  })

  it('keeps a background pass quiet, as the desktop does', async () => {
    const rpc = tradingRpc().handle(
      'trading.portfolio',
      portfolio({ syncing: true, syncKind: 'background' }),
    )
    renderPage(rpc)
    await screen.findByTestId('trd-overview')
    expect(screen.queryByText('Syncing with the chain…')).toBeNull()
  })

  it('warns, naming the network, when a read failed, with the reason as inert text', async () => {
    const rpc = tradingRpc()
      .handle('trading.portfolio', emptyPortfolio())
      .handle('wallet.balances', {
        balances: [],
        chains: [chainRead({ status: 'failed', reason: RPC_429 })],
      })
    renderPage(rpc)
    const warning = await screen.findByTestId('trd-read-warning')
    expect(warning).toHaveAttribute('role', 'note')
    expect(warning).toHaveTextContent('Could not fully read Base.')
    expect(warning).toHaveTextContent(RPC_429)
    expect(warning.querySelector('a')).toBeNull()
    // Read without ever asking the engine to go to the chain.
    expect(rpc.callsTo('wallet.balances').every((p) => !('refresh' in p))).toBe(true)

    // Holdings: the warning, and no "No holdings" as a final word.
    await openView(/holdings/i)
    expect(await screen.findByText('Not read from the chain yet')).toBeInTheDocument()
    expect(screen.queryByText('No holdings')).toBeNull()
    expect(screen.getByTestId('trd-read-warning')).toBeInTheDocument()

    // With the amounts hidden the warning stays: it names no amount.
    fireEvent.click(screen.getByRole('button', { name: /hide amounts/i }))
    expect(screen.getByTestId('trd-read-warning')).toHaveTextContent('Could not fully read Base.')
  })

  it('clamps a long reason', async () => {
    const rpc = tradingRpc().handle('wallet.balances', {
      chains: [chainRead({ status: 'partial', reason: 'x'.repeat(5000) })],
    })
    renderPage(rpc)
    const warning = await screen.findByTestId('trd-read-warning')
    expect(warning.textContent!.length).toBeLessThan(400)
  })

  it("shows only the picked wallet's failed reads", async () => {
    const rpc = tradingRpc()
      .handle('wallet.list', {
        wallets: [wallet(WALLET_A, 'Main', true), wallet(WALLET_B, 'Side')],
        primary: WALLET_A,
      })
      .handle('wallet.balances', {
        chains: [
          chainRead(),
          chainRead({ wallet: WALLET_B, status: 'failed', reason: 'rpc down' }),
        ],
      })
    renderPage(rpc)
    expect(await screen.findByTestId('trd-read-warning')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: WALLET_A } })
    await waitFor(() =>
      expect(rpc.callsTo('wallet.balances')).toContainEqual({ address: WALLET_A }),
    )
    await waitFor(() => expect(screen.queryByTestId('trd-read-warning')).toBeNull())
    fireEvent.change(screen.getByRole('combobox'), { target: { value: WALLET_B } })
    expect(await screen.findByTestId('trd-read-warning')).toHaveTextContent('rpc down')
  })

  it('keeps the plain empty state once every read is good and no sync runs', async () => {
    renderPage(tradingRpc().handle('trading.portfolio', emptyPortfolio()))
    await openView(/holdings/i)
    expect(await screen.findByText('No holdings')).toBeInTheDocument()
    expect(screen.queryByTestId('trd-read-warning')).toBeNull()
  })
})

describe('TradingPage engine text behind the privacy switch', () => {
  const REASON = 'trading.insufficient_balance: Public test key A holds 0 USDC, needs 149.986351'

  it("masks a failed order's reason with the amounts, keeping its status", async () => {
    const rpc = tradingRpc().handle('trading.orders.list', {
      orders: [order({ status: 'failed', reason: REASON, txHash: null })],
    })
    renderPage(rpc)
    await openView(/orders/i)
    const row = await screen.findByTestId('trd-order')
    expect(row.textContent).toContain('needs 149.986351')
    fireEvent.click(screen.getByRole('button', { name: /hide amounts/i }))
    await waitFor(() => expect(row.textContent).not.toContain('149.986351'))
    expect(row.textContent).not.toContain('holds 0 USDC')
    expect(within(row).getByText('Failed')).toBeInTheDocument()
  })

  it("masks a history entry's note with the amounts", async () => {
    const rpc = tradingRpc().handle('trading.history', {
      entries: [entry({ note: 'Sent 12.5 USDC to cold storage' })],
    })
    renderPage(rpc)
    await openView(/history/i)
    expect(await screen.findByText('Sent 12.5 USDC to cold storage')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /hide amounts/i }))
    await waitFor(() => expect(screen.queryByText(/12\.5 USDC/)).toBeNull())
  })

  it('leaves an alert-only trigger\'s "notify" readable with the amounts hidden', async () => {
    const trigger = {
      id: 'trg_2',
      name: 'Price alert',
      status: 'armed',
      chain: { id: 8453 },
      token: token('ETH'),
      bracket: null,
      condition: { label: 'under $3,000' },
      action: { label: 'notify' },
    }
    const rpc = tradingRpc().handle('trading.trigger.list', {
      kind: 'triggers',
      triggers: [trigger],
      totals: { count: 1, armed: 1, awaiting: 0, triggered: 0 },
    })
    localStorage.setItem('agentos-web.trading.balancesHidden', 'true')
    reloadBalancePrivacy()
    renderPage(rpc)
    await openView(/orders/i)
    const row = await screen.findByTestId('trd-automation-trigger')
    expect(row.textContent).toContain('notify')
    expect(row.textContent).not.toContain(BALANCE_MASK)
  })
})

describe('TradingPage connection lost', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('says the figures may be stale while the gateway is unreachable, and clears on reconnect', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    renderPage()
    expect(await screen.findAllByText('$12,480.52')).not.toHaveLength(0)
    expect(screen.queryByText(/Connection to the gateway lost/)).toBeNull()

    act(() => useConnection.getState().setState('disconnected'))
    // The shell's grace period: a short blip says nothing.
    expect(screen.queryByText(/Connection to the gateway lost/)).toBeNull()
    await act(() => vi.advanceTimersByTimeAsync(2_000))
    const notice = screen.getByText(
      'Connection to the gateway lost. Figures below may be out of date.',
    )
    expect(notice).toHaveAttribute('role', 'status')
    // The last figures stay, under the notice.
    expect(screen.getAllByText('$12,480.52')).not.toHaveLength(0)

    act(() => useConnection.getState().setState('connected'))
    await waitFor(() => expect(screen.queryByText(/Connection to the gateway lost/)).toBeNull())
  })
})

describe('TradingPage explorer links', () => {
  it('give each link an accessible name that starts with its visible text', async () => {
    renderPage()
    await screen.findByTestId('trd-overview')
    const walletLink = screen
      .getAllByRole('link')
      .find((a) => a.getAttribute('href')?.includes('/address/'))!
    expect(walletLink).not.toHaveAttribute('aria-label')
    const visible = walletLink.querySelector('.trd-mono')!.textContent!
    expect(walletLink).toHaveAccessibleName(`${visible}, View Main on the Base explorer`)

    await openView(/orders/i)
    const txLink = (await screen.findAllByRole('link')).find((a) =>
      a.getAttribute('href')?.includes('/tx/'),
    )!
    const hash = txLink.querySelector('.trd-mono')!.textContent!
    expect(txLink).toHaveAccessibleName(`${hash}, Open the transaction on the explorer`)
  })
})
