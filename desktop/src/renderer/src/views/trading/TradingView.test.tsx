import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useGateway } from '~/stores/gateway'
import { useTradingUi } from '~/stores/trading-ui'
import { useUi } from '~/stores/ui'
import type { MissionsApi } from './desk/missions'
import { holding, LP_ROW, renderDesk, USDC, WALLET } from './test-utils'
import { TradingView } from './TradingView'
import type { LpBook } from './types'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn(async () => {}) } }),
  isDesktop: () => true,
}))

beforeEach(() => {
  rpcCall.mockReset()
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
  useUi.setState({ settingsOpen: false })
})

describe('TradingView · gate', () => {
  it('says it is loading while the first status is still on its way', () => {
    rpcCall.mockImplementation(() => new Promise(() => {}))
    renderDesk(<TradingView />)
    expect(screen.getByTestId('trading-loading')).toHaveTextContent('Loading the desk')
  })

  it('reports a status error as the gateway not answering, with a retry', async () => {
    let fail = true
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.status') {
        if (fail) throw new Error('socket closed')
        return {
          enabled: false,
          chains: [],
          limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
          unlockMode: 'auto',
          unlocked: false,
          syncing: false,
          lastSyncAt: null,
        }
      }
      if (method === 'wallet.status') throw new Error('socket closed')
      return {}
    })
    renderDesk(<TradingView />)
    const retry = await screen.findByTestId('trading-retry')
    expect(screen.getByRole('heading')).toHaveTextContent('Waiting for the gateway')
    // Never "Create your vault": nobody knows whether there is one.
    expect(screen.queryByTestId('vault-setup')).toBeNull()
    const before = rpcCall.mock.calls.filter((c) => c[0] === 'trading.status').length
    fail = false
    fireEvent.click(retry)
    await waitFor(() =>
      expect(rpcCall.mock.calls.filter((c) => c[0] === 'trading.status').length).toBeGreaterThan(
        before,
      ),
    )
  })
})

describe('TradingView · the swap route', () => {
  it('offers no provider to pick: swaps go through the AgentOS DEX', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      switch (method) {
        case 'trading.status':
          return {
            enabled: true,
            chains: [],
            limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
            unlockMode: 'auto',
            unlocked: true,
            syncing: false,
            lastSyncAt: null,
            provider: 'aggregator',
            providers: [
              {
                id: 'aggregator',
                label: 'AgentOS DEX',
                needsKey: false,
                keyConfigured: true,
                healthy: null,
              },
            ],
          }
        case 'wallet.status':
          return { initialized: true, unlocked: true, unlockMode: 'auto', walletCount: 1 }
        case 'wallet.list':
          return { wallets: [WALLET], primary: WALLET.address }
        case 'trading.limits':
          return { dailyCapUsd: 1000, spentTodayUsd: 0, thresholdUsd: 100, approvalTtlSeconds: 900 }
        default:
          return {}
      }
    })
    renderDesk(<TradingView />)

    const cta = await screen.findByTestId('swap-review')
    expect(cta).not.toHaveTextContent(/Uniswap|provider|key/i)
    expect(screen.queryByTestId('provider-pill')).toBeNull()
    expect(screen.queryByRole('button', { name: /Swap provider/ })).toBeNull()
    expect(screen.queryByRole('menuitemradio')).toBeNull()
    expect(screen.queryByText(/Uniswap API key/)).toBeNull()
    expect(rpcCall.mock.calls.some((c) => c[0] === 'trading.setProvider')).toBe(false)
  })
})

describe('TradingView · the sync state', () => {
  function engine(sync: { syncing: boolean; syncKind?: string | null }) {
    rpcCall.mockImplementation(async (method: string) => {
      switch (method) {
        case 'trading.status':
          return {
            enabled: true,
            chains: [],
            limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
            unlockMode: 'auto',
            unlocked: true,
            lastSyncAt: Date.now() - 5_000,
            ...sync,
          }
        case 'wallet.status':
          return { initialized: true, unlocked: true, unlockMode: 'auto', walletCount: 1 }
        case 'wallet.list':
          return { wallets: [WALLET], primary: WALLET.address }
        case 'trading.portfolio':
          return {
            totals: { valueUsd: 0 },
            holdings: [],
            wallets: [],
            updatedAt: 0,
            ...sync,
          }
        default:
          return {}
      }
    })
  }

  it('keeps the routine pass quiet: the desk shows how fresh it is instead', async () => {
    // Many wallets: the engine's own pass runs most of the time.
    engine({ syncing: true, syncKind: 'background' })
    renderDesk(<TradingView />)
    expect(await screen.findByText(/Last sync/)).toBeInTheDocument()
    expect(screen.queryByText('Syncing')).toBeNull()
    expect(screen.getByRole('button', { name: 'Resync from chain' })).toBeEnabled()
  })

  it.each(['requested', 'initial'])(
    'shows a %s pass, which someone is waiting on',
    async (kind) => {
      engine({ syncing: true, syncKind: kind })
      renderDesk(<TradingView />)
      expect(await screen.findByText('Syncing')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Resync from chain' })).toBeDisabled()
    },
  )

  it('reads an older engine, which sends no kind, as it always did', async () => {
    engine({ syncing: true })
    renderDesk(<TradingView />)
    expect(await screen.findByText('Syncing')).toBeInTheDocument()
  })
})

const LP_BOOK: LpBook = {
  version: 1,
  kind: 'book',
  ready: true,
  refreshing: false,
  readAt: Date.now() / 1000 - 30,
  partialScan: true,
  warnings: ['Base: one gauge was not read'],
  wallets: [{ address: WALLET.address, label: 'Main' }],
  chains: [],
  protocols: [
    { key: 'uniswap_v3', name: 'Uniswap V3', count: 1, valueUsd: 1304 },
    { key: 'aero_slipstream', name: 'Aero Slipstream', count: 1, valueUsd: 1996 },
  ],
  positions: [
    LP_ROW,
    {
      ...LP_ROW,
      id: '8453:aero_slipstream:900:0xabc',
      tokenId: '900',
      status: 'in-range',
      distancePct: null,
      range: { priceLower: 2000, priceUpper: 3000 },
      protocol: { key: 'aero_slipstream', name: 'Aero Slipstream', kind: 'concentrated' },
      staked: true,
      gauge: '0xgauge',
      valueUsd: 1996,
      rewards: {
        token: { address: '0xaero', symbol: 'xAERO', decimals: 18, priceUsd: 0.5 },
        amount: { raw: '12500000000000000000', human: '12.5', usd: 6.25 },
      },
    },
  ],
  totals: { valueUsd: 3300, feesUsd: 8, rewardsUsd: 6.25, count: 2, outOfRange: 1, unpriced: 0 },
}

describe('TradingView · Markets and the ticket', () => {
  beforeEach(() => {
    useTradingUi.setState({
      deskMode: true,
      sheet: null,
      bookTab: 'portfolio',
      bookOpen: false,
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
    rpcCall.mockImplementation(async (method: string) => {
      switch (method) {
        case 'trading.status':
          return {
            enabled: true,
            chains: [],
            limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
            unlockMode: 'auto',
            unlocked: true,
            syncing: false,
            lastSyncAt: null,
            provider: 'aggregator',
            providers: [],
          }
        case 'wallet.status':
          return { initialized: true, unlocked: true, unlockMode: 'auto', walletCount: 1 }
        case 'wallet.list':
          return { wallets: [WALLET], primary: WALLET.address }
        case 'trading.portfolio':
          return {
            totals: {
              valueUsd: 900,
              costUsd: 880,
              unrealizedUsd: 20,
              realizedUsd: 0,
              gasUsd: 0,
              change24hUsd: 0,
              change24hPct: 0,
            },
            holdings: [holding({ token: USDC })],
            wallets: [],
            syncing: false,
          }
        case 'trading.lp.book':
          return LP_BOOK
        default:
          return {}
      }
    })
  })

  // The engine reads the chain for LP positions only while someone asks, so
  // the desk must not ask until the tab is the one on screen.
  // Only Uniswap V4 has engine writes: its rows carry the actions, and one
  // parks an order whose card (on Approvals) is where it is approved.
  it('removes a V4 position from its row and lands on the approval', async () => {
    const v4: LpBook['positions'][number] = {
      ...LP_ROW,
      id: '4663:uniswap_v4:1307109:0xabc',
      tokenId: '1307109',
      chain: { id: 4663, key: 'robinhood', name: 'Robinhood Chain', explorer: '' },
      protocol: { key: 'uniswap_v4', name: 'Uniswap V4', kind: 'concentrated' },
    }
    const base = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params?: unknown) => {
      if (method === 'trading.lp.book') return { ...LP_BOOK, positions: [v4, LP_ROW] }
      if (method === 'trading.lp.remove') return { order: { orderId: 'ord_lp1' } }
      return base(method, params)
    })
    renderDesk(<TradingView />)
    await screen.findByTestId('holding-row')
    fireEvent.click(screen.getByTestId('tab-liquidity'))
    const rows = await screen.findAllByTestId('lp-row')
    // The V3 row is listed, not managed.
    expect(within(rows[1]!).queryByTestId('lp-remove')).toBeNull()
    fireEvent.click(within(rows[0]!).getByTestId('lp-remove'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('trading.lp.remove', {
        chainId: 4663,
        tokenId: '1307109',
        initiator: 'manual',
      }),
    )
    await waitFor(() =>
      expect(screen.getByTestId('tab-approvals')).toHaveAttribute('aria-selected', 'true'),
    )
  })

  it('reads liquidity only on its tab, and lists each position', async () => {
    renderDesk(<TradingView />)
    await screen.findByTestId('holding-row')
    expect(rpcCall.mock.calls.some(([method]) => method === 'trading.lp.book')).toBe(false)
    fireEvent.click(screen.getByTestId('tab-liquidity'))
    const rows = await screen.findAllByTestId('lp-row')
    expect(rpcCall).toHaveBeenCalledWith('trading.lp.book', {})
    expect(rows).toHaveLength(2)
    // Out of range first, as the engine sent them.
    expect(rows[0]).toHaveAttribute('data-status', 'above-range')
    expect(rows[0]).toHaveTextContent('WETH / USDC')
    expect(rows[0]).toHaveTextContent('Uniswap V3 · 0.05% · #77')
    expect(rows[0]).toHaveTextContent('Above range')
    expect(rows[1]).toHaveTextContent('Aero Slipstream')
    expect(rows[1]).toHaveTextContent('Staked')
    expect(rows[1]).toHaveTextContent('12.5 xAERO')
    const summary = screen.getByTestId('lp-summary')
    expect(summary).toHaveTextContent('$3,300.00')
    expect(summary).toHaveTextContent('1 out of range')
    expect(screen.getByTestId('lp-partial')).toHaveAttribute(
      'title',
      'Base: one gauge was not read',
    )
    // A protocol chip narrows the table; the totals stay the book's.
    fireEvent.click(screen.getByRole('radio', { name: /Aero Slipstream/ }))
    expect(screen.getAllByTestId('lp-row')).toHaveLength(1)
    // The refresh button asks the engine to read the chain now.
    fireEvent.click(screen.getByTestId('lp-refresh'))
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('trading.lp.book', { refresh: true }))
  })

  it('puts Markets right after Holdings', async () => {
    renderDesk(<TradingView />)
    await screen.findByTestId('trading-desk')
    const tabs = screen.getAllByRole('tab').map((tab) => tab.getAttribute('data-testid'))
    expect(tabs).toEqual([
      'tab-holdings',
      'tab-markets',
      'tab-liquidity',
      'tab-history',
      'tab-orders',
      'tab-approvals',
      'tab-tools',
    ])
    fireEvent.click(screen.getByTestId('tab-markets'))
    expect(await screen.findByTestId('markets-prompt')).toBeInTheDocument()
  })

  it('fills the ticket from a Holdings row as before, leaving the BOOK as it was', async () => {
    renderDesk(<TradingView />)
    const row = await screen.findByTestId('holding-row')
    fireEvent.click(row.querySelector('button[aria-label="Swap USDC"]')!)
    await waitFor(() => expect(screen.getAllByTestId('token-button')[0]).toHaveTextContent('USDC'))
    expect(screen.getByTestId('tab-holdings')).toHaveAttribute('aria-selected', 'true')
    expect(useTradingUi.getState()).toMatchObject({ bookTab: 'portfolio', bookOpen: false })
  })

  // The BOOK beside the chat has had Automations and Tools; the full desk
  // offered neither, so the DCAs running in the background were out of sight.
  function missions(awaiting = 0): MissionsApi {
    const actions = { pending: null } as unknown
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
      awaitingMandates: Array.from({ length: awaiting }) as MissionsApi['awaitingMandates'],
      mandate: actions as MissionsApi['mandate'],
      triggers: [],
      awaitingTriggers: [],
      trigger: actions as MissionsApi['trigger'],
      brackets: [],
      awaitingBrackets: [],
      bracket: actions as MissionsApi['bracket'],
    }
  }

  it('offers Automations and Tools at the full desk, with the asks counted', async () => {
    renderDesk(
      <TradingView
        automations={{
          missions: missions(2),
          sessionKey: 'agent:trading:s1',
          onOpenSession: vi.fn(),
        }}
      />,
    )
    await screen.findByTestId('trading-desk')
    const tabs = screen.getAllByRole('tab').map((tab) => tab.getAttribute('data-testid'))
    expect(tabs).toEqual([
      'tab-holdings',
      'tab-markets',
      'tab-liquidity',
      'tab-history',
      'tab-orders',
      'tab-approvals',
      'tab-automations',
      'tab-tools',
    ])
    expect(screen.getByTestId('tab-automations')).toHaveTextContent('Automations2')
    fireEvent.click(screen.getByTestId('tab-automations'))
    expect(await screen.findByTestId('automations')).toBeInTheDocument()
    expect(rpcCall).toHaveBeenCalledWith('cron.list', expect.anything())
    fireEvent.click(screen.getByTestId('tab-tools'))
    expect(await screen.findByTestId('tools-panel')).toBeInTheDocument()
  })

  it("hands Send to the desk's chat, which owns the sheet", async () => {
    renderDesk(
      <TradingView
        automations={{
          missions: missions(),
          sessionKey: 'agent:trading:s1',
          onOpenSession: vi.fn(),
        }}
      />,
    )
    await screen.findByTestId('trading-desk')
    fireEvent.click(screen.getByTestId('tab-tools'))
    fireEvent.click(await screen.findByTestId('tool-send'))
    expect(useTradingUi.getState()).toMatchObject({ sheet: 'send', deskMode: false })
  })

  it('opens Inspect in place, without leaving the desk', async () => {
    renderDesk(<TradingView />)
    await screen.findByTestId('trading-desk')
    fireEvent.click(screen.getByTestId('tab-tools'))
    fireEvent.click(await screen.findByTestId('tool-inspect'))
    expect(useTradingUi.getState()).toMatchObject({ sheet: null, deskMode: true })
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })
})

describe('TradingView · the wallet rail', () => {
  const SECOND = {
    ...WALLET,
    address: '0x2222222222222222222222222222222222222222',
    label: 'Second',
    primary: false,
  }

  it('opens the wallet manager from the rail', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      switch (method) {
        case 'trading.status':
          return {
            enabled: true,
            chains: [],
            limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
            unlockMode: 'auto',
            unlocked: true,
            syncing: false,
            lastSyncAt: null,
            provider: 'aggregator',
            providers: [],
          }
        case 'wallet.status':
          return { initialized: true, unlocked: true, unlockMode: 'auto', walletCount: 2 }
        case 'wallet.list':
          return { wallets: [WALLET, SECOND], primary: WALLET.address }
        default:
          return {}
      }
    })
    renderDesk(<TradingView />)
    await waitFor(() =>
      expect(screen.getByTestId('trading-desk')).toHaveAttribute('data-rail', 'on'),
    )
    const rail = screen.getByRole('complementary', { name: 'Wallets' })
    fireEvent.click(within(rail).getByRole('button', { name: 'Manage wallets' }))
    expect(await screen.findAllByTestId('manage-address')).toHaveLength(2)
  })
})

describe('TradingView · one wallet selected', () => {
  const SECOND = {
    ...WALLET,
    address: '0x2222222222222222222222222222222222222222',
    label: '10k',
    primary: false,
  }
  const asks = (address: string) => ({ wallet: { address, label: null } })

  beforeEach(() => {
    rpcCall.mockImplementation(async (method: string) => {
      switch (method) {
        case 'trading.status':
          return {
            enabled: true,
            chains: [],
            limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
            unlockMode: 'auto',
            unlocked: true,
            syncing: false,
            lastSyncAt: null,
            provider: 'aggregator',
            providers: [],
          }
        case 'wallet.status':
          return { initialized: true, unlocked: true, unlockMode: 'auto', walletCount: 2 }
        case 'wallet.list':
          return { wallets: [WALLET, SECOND], primary: WALLET.address }
        case 'trading.orders.list':
          return { orders: [], pendingApprovals: 0 }
        default:
          return {}
      }
    })
  })

  it('scopes Orders and the Automations count to that wallet', async () => {
    const missions = {
      missions: [],
      running: new Set<string>(),
      awaitingMandates: [asks(WALLET.address), asks(SECOND.address)],
      awaitingTriggers: [asks(WALLET.address)],
      awaitingBrackets: [asks(SECOND.address)],
    } as unknown as MissionsApi
    renderDesk(
      <TradingView
        automations={{ missions, sessionKey: 'agent:trading:s1', onOpenSession: vi.fn() }}
      />,
    )
    await screen.findByTestId('trading-desk')
    expect(screen.getByTestId('tab-automations')).toHaveTextContent('Automations4')
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('trading.orders.list', { limit: 50 }))

    const rows = await screen.findAllByTestId('wallet-row')
    fireEvent.click(rows.find((r) => r.textContent?.includes('10k'))!)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('trading.orders.list', {
        wallet: SECOND.address,
        limit: 50,
      }),
    )
    expect(screen.getByTestId('tab-automations')).toHaveTextContent('Automations2')
  })
})
