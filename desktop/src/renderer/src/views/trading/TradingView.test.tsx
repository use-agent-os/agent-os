import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useGateway } from '~/stores/gateway'
import { useUi } from '~/stores/ui'
import { renderDesk, WALLET } from './test-utils'
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
