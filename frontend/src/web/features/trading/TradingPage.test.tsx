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
  })

  it('says the vault is locked and offers no way to unlock it', async () => {
    const rpc = tradingRpc().handle('wallet.status', vaultStatus({ unlocked: false }))
    renderPage(rpc)
    expect(await screen.findByText('The wallet vault is locked')).toBeInTheDocument()
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
    const table = await screen.findByRole('table')
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
    expect(screen.getByText('1 position out of range')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /collect|remove/i })).toBeNull()
    for (const params of rpc.callsTo('trading.lp.book'))
      expect(params).not.toHaveProperty('refresh')
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
    const table = await screen.findByRole('table')
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
    const table = await screen.findByRole('table')
    expect(table.textContent).not.toMatch(/\de[+-]?\d/i)
    expect(table.textContent).not.toMatch(/NaN|Infinity|undefined/)
  })

  it('tones an unchanged price flat, not as a gain (the shared pnlTone)', async () => {
    renderPage()
    await openView(/holdings/i)
    const table = await screen.findByRole('table')
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
