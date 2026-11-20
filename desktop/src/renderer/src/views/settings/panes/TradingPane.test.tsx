import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { TradingPane } from './TradingPane'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
const openExternal = vi.fn(async () => {})
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal } }),
  isDesktop: () => true,
}))

function mount() {
  useConnection.getState().setState('connected')
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TradingPane />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  rpcCall.mockReset()
  openExternal.mockClear()
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'config.snapshot') {
      return {
        config: { trading: { enabled: true, approval_threshold_usd: 100, daily_cap_usd: 1000 } },
        revision: 'r1',
      }
    }
    if (method === 'trading.status') {
      return {
        enabled: true,
        apiKeyConfigured: false,
        chains: [
          {
            chainId: 8453,
            key: 'base',
            name: 'Base',
            native: 'ETH',
            explorer: 'https://basescan.org',
            rpcUrl: 'https://mainnet.base.org',
            healthy: true,
          },
          {
            chainId: 4663,
            key: 'robinhood',
            name: 'Robinhood Chain',
            native: 'ETH',
            explorer: 'https://robin.etherscan.io',
            rpcUrl: 'https://rpc.mainnet.chain.robinhood.com',
            healthy: null,
          },
        ],
        limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
        unlockMode: 'auto',
        unlocked: true,
        syncing: false,
        lastSyncAt: null,
      }
    }
    if (method === 'wallet.status') {
      return {
        initialized: true,
        unlocked: true,
        unlockMode: 'auto',
        walletCount: 2,
        primary: '0x1',
        vaultPath: '/opt/agentos/wallets',
      }
    }
    if (method === 'trading.probe') return { ok: true, latencyMs: 412, error: null }
    if (method === 'config.patch') return { restartRequired: false }
    return {}
  })
})

describe('TradingPane · Test key', () => {
  it('tries the typed key against Uniswap and reports the verdict, then saves it with the revision', async () => {
    mount()
    const test = await screen.findByTestId('trading-key-test')
    expect(test).toBeDisabled()
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'uni-123' } })
    expect(test).not.toBeDisabled()
    fireEvent.click(test)
    await waitFor(() =>
      expect(screen.getByTestId('trading-key-probe')).toHaveAttribute('data-verdict', 'ok'),
    )
    expect(rpcCall).toHaveBeenCalledWith('trading.probe', {
      provider: 'uniswap',
      apiKey: 'uni-123',
    })
    expect(screen.getByTestId('trading-key-probe')).toHaveTextContent('Key works. 412 ms')

    fireEvent.click(screen.getByTestId('trading-key-save'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('config.patch', {
        patch: { trading: { uniswap_api_key: 'uni-123' } },
        expectedRevision: 'r1',
      }),
    )
  })

  it('shows Uniswap’s rejection verbatim', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.probe')
        return { ok: false, latencyMs: 200, error: '401 Unauthorized' }
      if (method === 'config.snapshot') return { config: { trading: {} }, revision: 'r1' }
      if (method === 'trading.status')
        return {
          enabled: true,
          apiKeyConfigured: false,
          chains: [],
          limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
          unlockMode: 'auto',
          unlocked: true,
          syncing: false,
          lastSyncAt: null,
        }
      if (method === 'wallet.status')
        return {
          initialized: false,
          unlocked: false,
          unlockMode: 'auto',
          walletCount: 0,
          primary: null,
          vaultPath: '',
        }
      return {}
    })
    mount()
    fireEvent.change(await screen.findByLabelText('API key'), { target: { value: 'bad' } })
    fireEvent.click(screen.getByTestId('trading-key-test'))
    await waitFor(() =>
      expect(screen.getByTestId('trading-key-probe')).toHaveAttribute('data-verdict', 'bad'),
    )
    expect(screen.getByTestId('trading-key-probe')).toHaveTextContent('401 Unauthorized')
    expect(screen.getByText('Not created')).toBeInTheDocument()
  })

  it('shows the vault state, the networks and their health', async () => {
    mount()
    expect(await screen.findByText('Unlocked')).toBeInTheDocument()
    expect(screen.getByText('Base · 8453')).toBeInTheDocument()
    expect(screen.getByText('Reachable')).toBeInTheDocument()
    expect(screen.getByText('Robinhood Chain · 4663')).toBeInTheDocument()
    expect(screen.getByText('Not checked')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Get an API key/ }))
    expect(openExternal).toHaveBeenCalledWith('https://developers.uniswap.org/dashboard')
  })
})

describe('TradingPane · Swap provider', () => {
  it('switches the provider through config.patch with the revision and idles the Uniswap key', async () => {
    mount()
    // A config that names no provider is on the default: the aggregator. The
    // Uniswap key is then kept but not in use, and the hint says so.
    const aggregator = await screen.findByRole('radio', { name: /^AgentOS DEX/ })
    expect(aggregator).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('uniswap-key-hint')).toHaveTextContent('Only used while')
    fireEvent.click(screen.getByRole('radio', { name: /^Uniswap/ }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('config.patch', {
        patch: { trading: { provider: 'uniswap' } },
        expectedRevision: 'r1',
      }),
    )
  })

  it('greys the Uniswap key hint when the aggregator is the provider', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.snapshot')
        return { config: { trading: { provider: 'aggregator' } }, revision: 'r1' }
      if (method === 'trading.status')
        return {
          enabled: true,
          apiKeyConfigured: false,
          provider: 'aggregator',
          providers: [
            {
              id: 'aggregator',
              label: 'AgentOS DEX',
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
          chains: [],
          limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
          unlockMode: 'auto',
          unlocked: true,
          syncing: false,
          lastSyncAt: null,
        }
      if (method === 'wallet.status')
        return {
          initialized: true,
          unlocked: true,
          unlockMode: 'auto',
          walletCount: 1,
          primary: '0x1',
          vaultPath: '',
        }
      return {}
    })
    mount()
    expect(await screen.findByRole('radio', { name: /^AgentOS DEX/ })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(screen.getByTestId('uniswap-key-hint')).toHaveTextContent(
      'Only used while Uniswap is the swap provider.',
    )
  })

  it('tests the aggregator connection and says what it answered', async () => {
    rpcCall.mockImplementation(async (method: string, params?: { provider?: string }) => {
      if (method === 'trading.probe' && params?.provider === 'aggregator')
        return { ok: false, latencyMs: 120, error: 'Aggregator is misconfigured' }
      if (method === 'config.snapshot') return { config: { trading: {} }, revision: 'r1' }
      if (method === 'trading.status')
        return {
          enabled: true,
          apiKeyConfigured: true,
          provider: 'uniswap',
          providers: [],
          chains: [],
          limits: { approvalThresholdUsd: 100, dailyCapUsd: 1000, approvalTtlSeconds: 900 },
          unlockMode: 'auto',
          unlocked: true,
          syncing: false,
          lastSyncAt: null,
        }
      if (method === 'wallet.status')
        return {
          initialized: true,
          unlocked: true,
          unlockMode: 'auto',
          walletCount: 1,
          primary: '0x1',
          vaultPath: '',
        }
      return {}
    })
    mount()
    fireEvent.click(await screen.findByTestId('aggregator-test'))
    const probe = await screen.findByTestId('aggregator-probe')
    expect(probe).toHaveAttribute('data-verdict', 'bad')
    expect(probe).toHaveTextContent('Not reachable:')
    expect(probe).toHaveTextContent('Aggregator is misconfigured')
    expect(rpcCall).toHaveBeenCalledWith('trading.probe', { provider: 'aggregator' })
  })
})
