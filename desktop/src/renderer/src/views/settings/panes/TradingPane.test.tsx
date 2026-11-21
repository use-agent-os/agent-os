import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { useSettings } from '~/stores/settings'
import { useUi } from '~/stores/ui'
import { TradingPane } from './TradingPane'
import { isLoopbackHost, isOperatorRefusal, readOnlyAdvice } from './trading-access'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))

const toastError = vi.fn()
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: (...a: unknown[]) => toastError(...a) },
}))

/** The engine's refusal of a trading write from a connection that is not the operator. */
const REFUSAL = new Error("trading settings are the user's to change; an agent cannot set trading")

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

/** The calls the default wiring answers, for a test that changes one of them. */
let baseImpl: (method: string, params?: unknown) => Promise<unknown>

beforeEach(() => {
  rpcCall.mockReset()
  toastError.mockClear()
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
  baseImpl = rpcCall.getMockImplementation() as typeof baseImpl
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
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.probe')
        return { ok: false, latencyMs: 120, error: 'Aggregator is misconfigured' }
      if (method === 'config.snapshot') return { config: { trading: {} }, revision: 'r1' }
      if (method === 'trading.status')
        return {
          enabled: true,
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
    expect(rpcCall).toHaveBeenCalledWith('trading.probe', {})
  })
})

describe('TradingPane · a window the gateway does not take trading changes from', () => {
  it('says so once at the top, with the way out, and makes every control read-only with its reason', async () => {
    rpcCall.mockImplementation(async (method: string, params?: unknown) =>
      method === 'flows.list'
        ? { flows: [], writable: false, read_only_reason: 'This window is connected as an agent' }
        : baseImpl(method, params),
    )
    mount()
    expect(await screen.findByTestId('trading-readonly')).toHaveTextContent(
      'Trading settings are read-only in this window.',
    )
    // A gateway on this Mac: the app has to run it.
    expect(screen.getByTestId('trading-readonly').parentElement).toHaveTextContent(
      'Settings › Gateway › Managed',
    )
    const reason = 'Read-only: the gateway does not take trading changes from this window.'
    await waitFor(() => expect(screen.getByLabelText('Ask me above')).toBeDisabled())
    expect(screen.getByLabelText('Ask me above')).toHaveAttribute('title', reason)
    expect(screen.getByRole('switch', { name: 'Trading' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: 'Ask me each time' })).toBeDisabled()
    expect(screen.getByTestId('vault-mode-reason')).toHaveTextContent(reason)

    useUi.setState({ settingsSection: 'trading' })
    fireEvent.click(screen.getByTestId('trading-readonly-open'))
    expect(useUi.getState().settingsSection).toBe('gateway')
    expect(rpcCall).not.toHaveBeenCalledWith('config.patch', expect.anything())
  })

  it('asks for the token when the gateway is on another machine', async () => {
    const settings = useSettings.getState().settings
    useSettings.setState({
      settings: { ...settings, gateway: { ...settings.gateway, mode: 'external', host: 'gw.lan' } },
    })
    rpcCall.mockImplementation(async (method: string, params?: unknown) =>
      method === 'flows.list' ? { flows: [], writable: false } : baseImpl(method, params),
    )
    mount()
    expect((await screen.findByTestId('trading-readonly')).parentElement).toHaveTextContent(
      'Add that token in Settings › Gateway',
    )
    useSettings.setState({ settings })
  })

  it('learns it from the first refused write: the field shows the saved value again and the page says why', async () => {
    rpcCall.mockImplementation(async (method: string, params?: unknown) => {
      if (method === 'config.patch') throw REFUSAL
      return baseImpl(method, params)
    })
    mount()
    const field = await screen.findByLabelText('Ask me above')
    expect(screen.queryByTestId('trading-readonly')).toBeNull()
    fireEvent.change(field, { target: { value: '150' } })
    fireEvent.blur(field)
    await waitFor(() => expect(field).toHaveValue('100'))
    expect(rpcCall).toHaveBeenCalledWith('config.patch', {
      patch: { trading: { approval_threshold_usd: 150 } },
      expectedRevision: 'r1',
    })
    expect(await screen.findByTestId('trading-readonly')).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledWith(
      'Not saved: the gateway does not take trading changes from this window.',
      expect.anything(),
    )
    expect(field).toBeDisabled()
  })

  it('puts the saved value back after any failed save, and keeps the reason for other failures', async () => {
    rpcCall.mockImplementation(async (method: string, params?: unknown) => {
      if (method === 'config.patch') throw new Error('revision conflict')
      return baseImpl(method, params)
    })
    mount()
    const field = await screen.findByLabelText('Daily cap')
    fireEvent.change(field, { target: { value: '5000' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    fireEvent.blur(field)
    await waitFor(() => expect(field).toHaveValue('1000'))
    expect(toastError).toHaveBeenCalledWith('Could not save: revision conflict', expect.anything())
    expect(screen.queryByTestId('trading-readonly')).toBeNull()
    expect(field).not.toBeDisabled()
  })

  it('reverts a limit on the first Escape without closing the sheet', async () => {
    const sheetKey = vi.fn()
    useConnection.getState().setState('connected')
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <div onKeyDown={(e) => sheetKey(e.key)}>
          <TradingPane />
        </div>
      </QueryClientProvider>,
    )
    const field = await screen.findByLabelText('Ask me above')
    fireEvent.change(field, { target: { value: '7' } })
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(field).toHaveValue('100')
    expect(sheetKey).not.toHaveBeenCalled()
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(sheetKey).toHaveBeenCalledWith('Escape')
  })

  it('says why the unlock mode cannot change before the vault exists', async () => {
    rpcCall.mockImplementation(async (method: string, params?: unknown) =>
      method === 'wallet.status'
        ? { initialized: false, unlocked: false, unlockMode: 'auto', walletCount: 0, primary: null }
        : baseImpl(method, params),
    )
    mount()
    expect(await screen.findByTestId('vault-mode-reason')).toHaveTextContent(
      'Create the vault first, on the Trading desk.',
    )
    expect(screen.getByRole('radio', { name: 'Ask me each time' })).toBeDisabled()
  })
})

describe('trading-access', () => {
  it('recognises the refusal by its code or the config refusal’s wording', () => {
    expect(isOperatorRefusal(REFUSAL)).toBe(true)
    expect(
      isOperatorRefusal(
        new Error('trading settings are the user’s to change; an agent cannot set trading'),
      ),
    ).toBe(true)
    expect(
      isOperatorRefusal(Object.assign(new Error('nope'), { code: 'trading.operator_required' })),
    ).toBe(true)
    expect(isOperatorRefusal(new Error('revision conflict'))).toBe(false)
  })

  it('tells a local gateway from a remote one', () => {
    for (const host of ['127.0.0.1', 'localhost', '::1', '127.0.1.1', 'agentos.localhost'])
      expect(isLoopbackHost(host)).toBe(true)
    expect(isLoopbackHost('192.168.1.20')).toBe(false)
    expect(readOnlyAdvice({ mode: 'external', host: '127.0.0.1' })).toBe('local')
    expect(readOnlyAdvice({ mode: 'managed', host: '127.0.0.1' })).toBe('local')
    expect(readOnlyAdvice({ mode: 'external', host: 'gw.example.com' })).toBe('remote')
  })
})
