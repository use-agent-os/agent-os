import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { TradingPane } from './TradingPane'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
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

describe('TradingPane', () => {
  it('shows the vault state, the networks and their health', async () => {
    mount()
    expect(await screen.findByText('Unlocked')).toBeInTheDocument()
    expect(screen.getByText('Base · 8453')).toBeInTheDocument()
    expect(screen.getByText('Reachable')).toBeInTheDocument()
    expect(screen.getByText('Robinhood Chain · 4663')).toBeInTheDocument()
    expect(screen.getByText('Not checked')).toBeInTheDocument()
  })
})

describe('TradingPane · Swap provider', () => {
  it('offers no provider to choose and no Uniswap key to enter', async () => {
    // Swaps go through the AgentOS DEX only. A config left over from when
    // there was a choice (a provider, a stored key) is neither shown nor
    // editable here.
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'config.snapshot')
        return {
          config: { trading: { provider: 'uniswap', uniswap_api_key: 'uni-123' } },
          revision: 'r1',
        }
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
    expect(await screen.findByTestId('aggregator-test')).toBeInTheDocument()
    expect(screen.getAllByText('AgentOS DEX').length).toBeGreaterThan(0)
    expect(screen.queryByRole('radio', { name: 'Uniswap' })).toBeNull()
    expect(screen.queryByRole('radio', { name: 'AgentOS DEX' })).toBeNull()
    expect(screen.queryByLabelText('API key')).toBeNull()
    expect(screen.queryByTestId('trading-key-test')).toBeNull()
    expect(screen.queryByTestId('trading-key-save')).toBeNull()
    expect(screen.queryByText(/Uniswap/)).toBeNull()
    expect(rpcCall.mock.calls.some((c) => c[0] === 'config.patch')).toBe(false)
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
