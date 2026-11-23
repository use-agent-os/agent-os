// The Trading page end to end against a fake engine: the gates, each view's
// figures, the empty and error states, the privacy switch, and that nothing
// it renders offers to act.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import marketsFixture from '@/views/trading/__fixtures__/markets/nvda.json'
import '@/test/web'
import { renderWeb } from '@/test/render-web'
import { useConnection } from '@/stores/connection'
import {
  ETH,
  WALLET_A,
  WALLET_B,
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
