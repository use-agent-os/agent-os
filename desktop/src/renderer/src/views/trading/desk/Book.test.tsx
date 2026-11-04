import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTradingUi } from '~/stores/trading-ui'
import { holding, order, renderDesk, USDC, WALLET } from '../test-utils'
import type { MarketsPayload, Token, Totals, Wallet } from '../types'
import { Book } from './Book'

const rpcCall = vi.fn()
const rpc = { call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn(async () => {}) } }),
  isDesktop: () => true,
}))

const SECOND: Wallet = {
  address: '0x89e0fa1b2c3d4e5f60718293a4b5c6d7e8f9da97',
  label: 'Wallet 02',
  primary: false,
  createdAt: 1_700_000_000_000,
  chains: [8453],
}

function totals(valueUsd: number): Totals {
  return {
    valueUsd,
    costUsd: valueUsd,
    unrealizedUsd: 0,
    realizedUsd: 0,
    gasUsd: 0,
    change24hUsd: 0,
    change24hPct: 0,
  }
}

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string) => {
    switch (method) {
      case 'trading.status':
        return {
          enabled: true,
          chains: [{ chainId: 8453, name: 'Base', explorer: 'https://basescan.org' }],
          provider: 'uniswap',
          providers: [],
        }
      case 'wallet.list':
        return { wallets: [WALLET, SECOND], primary: WALLET.address }
      case 'trading.portfolio':
        return {
          totals: totals(1240.99),
          holdings: [holding({ token: USDC })],
          wallets: [
            { wallet: WALLET, totals: totals(1240.5) },
            { wallet: SECOND, totals: totals(0.49) },
          ],
          hiddenCount: 0,
          syncing: false,
        }
      default:
        return {}
    }
  })
})

/** The desk's missions hook, idle: nothing listed, nothing awaiting, no write in flight. */
function stubMissions(): React.ComponentProps<typeof Book>['missions'] {
  const actions = { pending: null, busy: false } as unknown
  return {
    missions: [],
    loading: false,
    running: new Set<string>(),
    create: vi.fn(async () => null),
    update: vi.fn(async () => true),
    setEnabled: vi.fn(),
    runNow: vi.fn(),
    remove: vi.fn(),
    busy: false,
    mandates: [],
    awaitingMandates: [],
    mandate: actions as React.ComponentProps<typeof Book>['missions']['mandate'],
    triggers: [],
    awaitingTriggers: [],
    trigger: actions as React.ComponentProps<typeof Book>['missions']['trigger'],
    brackets: [],
    awaitingBrackets: [],
    bracket: actions as React.ComponentProps<typeof Book>['missions']['bracket'],
  }
}

function render(extra: Partial<React.ComponentProps<typeof Book>> = {}) {
  renderDesk(
    <Book
      missions={stubMissions()}
      sessionKey="agent:trading:webchat:trading-test"
      onOpenSession={vi.fn()}
      wallets={[WALLET, SECOND]}
      primary={WALLET.address}
      provider="uniswap"
      providerReady
      unlocked
      collapsed={false}
      width={360}
      onResize={vi.fn()}
      onToggle={vi.fn()}
      onOpenSettings={vi.fn()}
      highlightOrder={null}
      {...extra}
    />,
  )
}

describe('Book · the desk beside the chat', () => {
  it('heads the portfolio with the same wallet identity the full desk uses', async () => {
    render()
    // Not a row of nameless filter chips: the mark, the name, and the actions
    // that act on the wallet whose value is right below them.
    expect(await screen.findByTestId('wallet-head-address')).toHaveTextContent('All wallets')
    expect(screen.getByText('2 wallets')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('wallet-switcher'))
    fireEvent.click(screen.getAllByRole('menuitemradio')[2]!)
    expect(await screen.findByTestId('wallet-head-address')).toHaveTextContent('0x89e0…da97')
    expect(screen.getByRole('button', { name: 'Show QR' })).toBeInTheDocument()
  })

  it('keeps the wallet manager one click away, now inside the switcher', async () => {
    render()
    await screen.findByTestId('wallet-switcher')
    fireEvent.click(screen.getByTestId('wallet-switcher'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Manage wallets' }))
    // The manager lists every wallet, which is what the chip row used to do.
    const sheet = await screen.findByRole('dialog')
    expect(sheet).toHaveTextContent('Wallet 02')
    expect(sheet).toHaveTextContent('Main')
  })

  it('offers no rail toggle: there is no rail beside the chat', async () => {
    render()
    await screen.findByTestId('wallet-switcher')
    expect(screen.queryByTestId('rail-toggle')).toBeNull()
  })

  it('colours the hero by the book’s PnL, and the chip by the day’s move', async () => {
    // Up on the day, but the book as a whole is under water.
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.portfolio')
        return {
          totals: {
            ...totals(900),
            realizedUsd: -30,
            unrealizedUsd: -70,
            change24hUsd: 18.75,
            change24hPct: 2.1,
          },
          holdings: [holding({ token: USDC })],
          wallets: [{ wallet: WALLET, totals: totals(900) }],
          unpricedCount: 3,
          syncing: false,
        }
      if (method === 'trading.status') return { enabled: true, chains: [], providers: [] }
      if (method === 'wallet.list') return { wallets: [WALLET], primary: WALLET.address }
      return {}
    })
    render()
    const delta = await screen.findByTestId('book-delta')
    expect(delta).toHaveAttribute('data-tone', 'up')
    expect(delta).toHaveAttribute('title', expect.stringContaining('24h price move'))
    expect(delta.closest('.trd-hero')).toHaveAttribute('data-tone', 'down')
    // Three positions the totals could not price, said beside the figure.
    expect(screen.getByTestId('book-unpriced')).toHaveTextContent('3 unpriced')
  })

  it('says nothing about unpriced positions when every one has a price', async () => {
    render()
    await screen.findByTestId('wallet-switcher')
    expect(screen.queryByTestId('book-unpriced')).toBeNull()
  })
})

describe('Book · the spine', () => {
  // The spine is what is left of the BOOK on a narrow frame, and on a frame too
  // narrow to split, every button on it did nothing: `onToggle` flipped a
  // preference the concession chain overruled on the next render, and
  // `setBookTab` — which opens the panel as well as selecting a tab — was
  // overruled the same way. They looked like dead controls.
  it('opens the panel in place when the frame can still hold a split', () => {
    const onToggle = vi.fn()
    const onOpenDesk = vi.fn()
    render({ collapsed: true, cramped: false, onToggle, onOpenDesk })

    fireEvent.click(screen.getByTestId('book-open'))
    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(onOpenDesk).not.toHaveBeenCalled()

    // A spine tab opens the panel through `setBookTab` alone. Adding a toggle
    // here cancels that open — the two land in one render and flip `bookOpen`
    // straight back to false.
    fireEvent.click(screen.getByTestId('book-spine-orders'))
    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(onOpenDesk).not.toHaveBeenCalled()
  })

  it('offers the Desk instead when the frame has no room for a split', () => {
    const onToggle = vi.fn()
    const onOpenDesk = vi.fn()
    render({ collapsed: true, cramped: true, onToggle, onOpenDesk })

    const open = screen.getByTestId('book-open')
    // It must also SAY so — the old button promised an in-place open it could
    // not deliver, which is why it read as broken rather than as unavailable.
    expect(open).toHaveAccessibleName(/Desk/i)
    fireEvent.click(open)
    expect(onOpenDesk).toHaveBeenCalledTimes(1)
    expect(onToggle).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('book-spine-tools'))
    expect(onOpenDesk).toHaveBeenCalledTimes(2)
    expect(onToggle).not.toHaveBeenCalled()
  })

  it('falls back to the in-place toggle when no Desk handler is wired', () => {
    const onToggle = vi.fn()
    render({ collapsed: true, cramped: true, onToggle })
    fireEvent.click(screen.getByTestId('book-open'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })
})

describe('Book · rejecting from the Orders tab', () => {
  it("hands the desk's own order to the chat's reject path instead of a bare status flip", async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.orders.list')
        return { orders: [order({ orderId: 'mine', sessionKey: 'desk' })], pendingApprovals: 1 }
      if (method === 'trading.status') return { enabled: true, chains: [], providers: [] }
      return {}
    })
    const onReject = vi.fn(() => true)
    render({ onReject })
    fireEvent.click(await screen.findByTestId('book-tab-orders'))
    fireEvent.click(await screen.findByTestId('order-reject'))
    expect(onReject).toHaveBeenCalledWith(expect.objectContaining({ orderId: 'mine' }))
    expect(rpcCall.mock.calls.some(([m]) => m === 'trading.orders.reject')).toBe(false)
  })

  it('keeps the plain decision for an order the chat declines', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.orders.list')
        return { orders: [order({ orderId: 'theirs', sessionKey: 'other' })], pendingApprovals: 1 }
      if (method === 'trading.status') return { enabled: true, chains: [], providers: [] }
      return {}
    })
    render({ onReject: () => false })
    fireEvent.click(await screen.findByTestId('book-tab-orders'))
    fireEvent.click(await screen.findByTestId('order-reject'))
    await waitFor(() =>
      expect(rpcCall.mock.calls.some(([m]) => m === 'trading.orders.reject')).toBe(true),
    )
    // No note was written: no placeholder reason either.
    expect(rpcCall.mock.calls.find(([m]) => m === 'trading.orders.reject')?.[1]).toEqual({
      orderId: 'theirs',
    })
  })
})

describe('Book · Markets and swaps asked for from anywhere', () => {
  const MARKETS = JSON.parse(
    readFileSync('src/renderer/src/views/trading/__fixtures__/markets/nvda.json', 'utf8'),
  ) as MarketsPayload
  const NVDA: Token = {
    chainId: 4663,
    address: MARKETS.token.address,
    symbol: 'NVDA',
    name: 'NVIDIA • Robinhood Token',
    decimals: 18,
    logoUrl: null,
    native: false,
    verified: true,
    stockToken: true,
  }
  const AI: Token = {
    ...NVDA,
    address: MARKETS.sections.quote[0]!.counterparty.address,
    symbol: 'AI',
    verified: false,
    stockToken: false,
  }

  function reset() {
    useTradingUi.setState({
      bookTab: 'portfolio',
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
  }
  beforeEach(() => {
    reset()
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: unknown) => {
      if (method === 'trading.portfolio')
        return {
          totals: totals(1240.99),
          holdings: [
            holding({ token: USDC }),
            holding({ chainId: 4663, token: NVDA, amount: '2' }),
          ],
          wallets: [{ wallet: WALLET, totals: totals(1240.99) }],
          hiddenCount: 0,
          syncing: false,
        }
      if (method === 'trading.markets') return MARKETS
      return base(method, params)
    })
  })
  afterEach(reset)

  it('puts Markets right after Swap', () => {
    render()
    const tabs = screen.getAllByRole('tab').map((tab) => tab.getAttribute('data-testid'))
    expect(tabs).toEqual([
      'book-tab-portfolio',
      'book-tab-swap',
      'book-tab-markets',
      'book-tab-orders',
      'book-tab-history',
      'book-tab-automations',
      'book-tab-tools',
    ])
  })

  it('keeps the Holdings Swap button: the ticket opens on that token', async () => {
    render()
    const rows = await screen.findAllByTestId('holding-row')
    const usdc = rows.find((r) => r.textContent?.includes('USDC'))!
    fireEvent.click(usdc.querySelector('button[aria-label="Swap USDC"]')!)
    expect(await screen.findByTestId('book-tab-swap')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getAllByTestId('token-button')[0]).toHaveTextContent('USDC')
    // Taken, so a later mount does not fill the ticket with it again.
    await waitFor(() => expect(useTradingUi.getState().swapRequest).toBeNull())
  })

  it('fills the ticket from a swap asked for elsewhere (a chat card, a Markets row)', async () => {
    render()
    await screen.findAllByTestId('holding-row')
    act(() => useTradingUi.getState().requestSwap({ chainId: 4663, tokenIn: NVDA, tokenOut: AI }))
    expect(await screen.findByTestId('book-tab-swap')).toHaveAttribute('aria-selected', 'true')
    const legs = screen.getAllByTestId('token-button')
    expect(legs[0]).toHaveTextContent('NVDA')
    expect(legs[1]).toHaveTextContent('AI')
  })

  it('offers Markets on a Stock Token holding only, and opens the tab on it', async () => {
    render()
    const rows = await screen.findAllByTestId('holding-row')
    const usdc = rows.find((r) => r.textContent?.includes('USDC'))!
    expect(usdc.querySelector('[data-testid="holding-markets"]')).toBeNull()
    const nvda = rows.find((r) => r.textContent?.includes('NVDA'))!
    fireEvent.click(nvda.querySelector('[data-testid="holding-markets"]')!)
    expect(await screen.findByTestId('book-tab-markets')).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByTestId('markets-head')).toHaveTextContent('NVDA')
    expect(screen.getByTestId('markets-search')).toHaveValue('NVDA')
    expect(rpcCall).toHaveBeenCalledWith(
      'trading.markets',
      expect.objectContaining({ target: MARKETS.token.address, chainId: 4663 }),
    )

    // A row's Swap lands on the ticket with the pair.
    fireEvent.click(screen.getAllByTestId('markets-swap')[0]!)
    expect(await screen.findByTestId('book-tab-swap')).toHaveAttribute('aria-selected', 'true')
    const legs = screen.getAllByTestId('token-button')
    expect(legs[0]).toHaveTextContent('NVDA')
    expect(legs[1]).toHaveTextContent('AI')
  })
})
