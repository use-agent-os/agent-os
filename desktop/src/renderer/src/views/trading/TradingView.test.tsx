import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useGateway } from '~/stores/gateway'
import { useTradingUi } from '~/stores/trading-ui'
import { useUi } from '~/stores/ui'
import type { MissionsApi } from './desk/missions'
import { holding, renderDesk, USDC, WALLET } from './test-utils'
import { TradingView } from './TradingView'
import type { ProviderId } from './types'

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
          apiKeyConfigured: false,
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

describe('TradingView · the venue pill', () => {
  it('switches the route from the desk head, and the ticket follows it at once', async () => {
    // One engine: trading.setProvider changes what trading.status reports.
    let provider: ProviderId = 'aggregator'
    rpcCall.mockImplementation(async (method: string, params?: { provider?: ProviderId }) => {
      switch (method) {
        case 'trading.status':
          return {
            enabled: true,
            apiKeyConfigured: false,
            chains: [],
            limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
            unlockMode: 'auto',
            unlocked: true,
            syncing: false,
            lastSyncAt: null,
            provider,
            providers: [
              {
                id: 'aggregator',
                label: 'AgentOS Aggregator',
                needsKey: false,
                keyConfigured: true,
                healthy: null,
              },
              {
                id: 'uniswap',
                label: 'Uniswap',
                needsKey: true,
                keyConfigured: false,
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
        case 'trading.setProvider':
          provider = params?.provider ?? provider
          return { provider, restartRequired: false }
        default:
          return {}
      }
    })
    renderDesk(<TradingView />)

    const pill = await screen.findByTestId('provider-pill')
    expect(pill).toHaveTextContent('AgentOS Aggregator')
    // The keyless aggregator: the ticket has no key to ask for.
    expect(screen.getByTestId('swap-review')).not.toHaveTextContent('Add a Uniswap key')

    fireEvent.click(pill)
    fireEvent.click(screen.getByRole('menuitemradio', { name: /^Uniswap/ }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('trading.setProvider', { provider: 'uniswap' }),
    )

    // The pill and the ticket read the same status: one refetch moves both, and
    // the ticket keeps its gate — Uniswap without a key sends you to Settings.
    await waitFor(() => expect(screen.getByTestId('provider-pill')).toHaveTextContent('Uniswap'))
    const cta = screen.getByTestId('swap-review')
    expect(cta).toHaveTextContent('Add a Uniswap key')
    expect(screen.getByTestId('add-key')).toBeInTheDocument()
    fireEvent.click(cta)
    expect(useUi.getState()).toMatchObject({ settingsOpen: true, settingsSection: 'trading' })
  })
})

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
            apiKeyConfigured: false,
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
        default:
          return {}
      }
    })
  })

  it('puts Markets right after Holdings', async () => {
    renderDesk(<TradingView />)
    await screen.findByTestId('trading-desk')
    const tabs = screen.getAllByRole('tab').map((tab) => tab.getAttribute('data-testid'))
    expect(tabs).toEqual([
      'tab-holdings',
      'tab-markets',
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
