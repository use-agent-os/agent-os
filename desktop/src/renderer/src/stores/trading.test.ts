import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { useConnection } from '@/stores/connection'
import type { AuthResult } from '@shared/app'
import { DEFAULT_SETTINGS, type TouchIdMode } from '@shared/settings'
import { isTouchIdDeclined, resetBiometricGateForTests } from '~/lib/biometric-gate'
import { desktopApi } from '~/lib/desktop-api'
import { order } from '~/views/trading/test-utils'
import type { Bracket, Mandate, Trigger } from '~/views/trading/types'
import { useSettings } from './settings'
import {
  TRADING_EVENTS,
  TRADING_KEYS,
  useBracketActions,
  useBrackets,
  useMandateActions,
  useTriggerActions,
  useTriggers,
  useOrderDecision,
  useOrders,
  useWalletMutation,
} from './trading'

const rpcCall = vi.fn()
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return createElement(QueryClientProvider, { client }, children)
}

beforeEach(() => {
  useConnection.getState().setState('connected')
  rpcCall.mockReset()
  rpcCall.mockResolvedValue({ orders: [] })
})

describe('TRADING_KEYS.orders', () => {
  it('keys the page size and the wallet, so two pages never share one entry', () => {
    expect(TRADING_KEYS.orders(undefined, 50)).not.toEqual(TRADING_KEYS.orders(undefined, 100))
    expect(TRADING_KEYS.orders('awaiting_approval', 20)).not.toEqual(
      TRADING_KEYS.orders(undefined, 20),
    )
    expect(TRADING_KEYS.orders(undefined, 50, '0xabc')).not.toEqual(
      TRADING_KEYS.orders(undefined, 50),
    )
    expect(TRADING_KEYS.orders(undefined, 50)).toEqual(TRADING_KEYS.orders(undefined, 50))
    // Every orders key still lives under the one prefix the invalidator sweeps.
    expect(TRADING_KEYS.orders(undefined, 50).slice(0, 2)).toEqual(['trading', 'orders'])
  })
})

describe('price triggers in the store', () => {
  it('refreshes on the trigger events and keys the list under trading/trigger', () => {
    expect(TRADING_EVENTS).toContain('trading.trigger.changed')
    expect(TRADING_EVENTS).toContain('trading.trigger.fired')
    expect(TRADING_KEYS.trigger()).toEqual(['trading', 'trigger', 'live'])
    expect(TRADING_KEYS.trigger(true)).toEqual(['trading', 'trigger', 'all'])
  })

  it('reads trading.trigger.list, every one with all, and tolerates an empty answer', async () => {
    rpcCall.mockResolvedValue({ kind: 'triggers', triggers: [{ id: 'trg_1' }], totals: null })
    const { result } = renderHook(() => useTriggers(true), { wrapper })
    await waitFor(() => expect(result.current.triggers).toHaveLength(1))
    expect(rpcCall).toHaveBeenCalledWith('trading.trigger.list', { all: true })
    rpcCall.mockResolvedValue({})
    const empty = renderHook(() => useTriggers(), { wrapper })
    await waitFor(() => expect(empty.result.current.isSuccess).toBe(true))
    expect(empty.result.current.triggers).toEqual([])
    expect(rpcCall).toHaveBeenCalledWith('trading.trigger.list', {})
  })
})

describe('brackets in the store', () => {
  it('refreshes on trading.bracket.changed and keys the list under trading/bracket', () => {
    expect(TRADING_EVENTS).toContain('trading.bracket.changed')
    expect(TRADING_KEYS.bracket()).toEqual(['trading', 'bracket', 'live'])
    expect(TRADING_KEYS.bracket(true)).toEqual(['trading', 'bracket', 'all'])
  })

  it('reads trading.bracket.list, every one with all, and tolerates an empty answer', async () => {
    rpcCall.mockResolvedValue({ kind: 'brackets', brackets: [{ id: 'brk_1' }], totals: null })
    const { result } = renderHook(() => useBrackets(true), { wrapper })
    await waitFor(() => expect(result.current.brackets).toHaveLength(1))
    expect(rpcCall).toHaveBeenCalledWith('trading.bracket.list', { all: true })
    rpcCall.mockResolvedValue({})
    const empty = renderHook(() => useBrackets(), { wrapper })
    await waitFor(() => expect(empty.result.current.isSuccess).toBe(true))
    expect(empty.result.current.brackets).toEqual([])
    expect(rpcCall).toHaveBeenCalledWith('trading.bracket.list', {})
  })
})

describe('useOrders', () => {
  it('fetches each page size on its own', async () => {
    renderHook(
      () => {
        useOrders(undefined, true, 50)
        useOrders(undefined, true, 100)
      },
      { wrapper },
    )
    await waitFor(() =>
      expect(rpcCall.mock.calls.filter((c) => c[0] === 'trading.orders.list')).toHaveLength(2),
    )
    const limits = rpcCall.mock.calls
      .filter((c) => c[0] === 'trading.orders.list')
      .map((c) => (c[1] as { limit: number }).limit)
      .sort((a, b) => a - b)
    expect(limits).toEqual([50, 100])
  })
})

describe('useOrderDecision', () => {
  it('rejects with no placeholder reason (the engine stored "user: user")', async () => {
    rpcCall.mockResolvedValue({ order: {} })
    const { result } = renderHook(() => useOrderDecision(), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({ orderId: 'o1', approve: false })
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.orders.reject', { orderId: 'o1' })
    await act(async () => {
      await result.current.mutateAsync({ orderId: 'o2', approve: true })
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.orders.approve', { orderId: 'o2' })
  })

  it("echoes a single order's approval digest", async () => {
    rpcCall.mockResolvedValue({ order: {} })
    const { result } = renderHook(() => useOrderDecision(), { wrapper })
    const parked = order({ orderId: 'o4', approval: { digest: 'd4' } })
    await act(async () => {
      await result.current.mutateAsync({ orderId: 'o4', approve: true, order: parked })
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.orders.approve', {
      orderId: 'o4',
      approvalDigest: 'd4',
    })
  })

  it("approves a multisend leg only with the batch's digest, read beside every leg", async () => {
    const leg = order({ orderId: 'o5', batchId: 'bat_1', approval: { digest: null, legCount: 2 } })
    rpcCall.mockImplementation(async (method: string) =>
      method === 'trading.orders.batch'
        ? { batchId: 'bat_1', orders: [leg], approval: { digest: 'dbatch', legCount: 2 } }
        : { order: {} },
    )
    const { result } = renderHook(() => useOrderDecision(), { wrapper })
    await act(async () => {
      await result.current.mutateAsync({ orderId: 'o5', approve: true, order: leg })
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.orders.batch', { batchId: 'bat_1' })
    expect(rpcCall).toHaveBeenLastCalledWith('trading.orders.approve', {
      orderId: 'o5',
      approvalDigest: 'dbatch',
    })
  })
})

describe('Touch ID in front of every approval and vault write', () => {
  let authenticate: MockInstance<(reason: string) => Promise<AuthResult>>
  function setMode(touchId: TouchIdMode): void {
    useSettings.setState({
      loaded: true,
      settings: { ...structuredClone(DEFAULT_SETTINGS), security: { touchId } },
    })
  }
  beforeEach(() => {
    resetBiometricGateForTests()
    rpcCall.mockResolvedValue({ order: {} })
    authenticate = vi.spyOn(desktopApi().app, 'authenticate')
  })
  afterEach(() => {
    authenticate.mockRestore()
    setMode('off')
  })

  it('sends nothing when the prompt is cancelled, and the decision once when it passes', async () => {
    setMode('high')
    const { result } = renderHook(() => useOrderDecision(), { wrapper })
    const big = order({ orderId: 'o9', amountIn: '0.4', valueUsd: 900 })
    authenticate.mockResolvedValueOnce({ ok: false, reason: 'cancelled' } satisfies AuthResult)
    let err: unknown = null
    await act(async () => {
      err = await result.current
        .mutateAsync({ orderId: 'o9', approve: true, order: big })
        .catch((e: unknown) => e)
    })
    expect(isTouchIdDeclined(err)).toBe(true)
    expect(authenticate).toHaveBeenCalledWith('approve 0.4 ETH → USDC on Base')
    expect(rpcCall).not.toHaveBeenCalledWith('trading.orders.approve', expect.anything())

    authenticate.mockResolvedValueOnce({ ok: true } satisfies AuthResult)
    await act(async () => {
      await result.current.mutateAsync({ orderId: 'o9', approve: true, order: big })
    })
    expect(rpcCall.mock.calls.filter(([m]) => m === 'trading.orders.approve')).toEqual([
      ['trading.orders.approve', { orderId: 'o9' }],
    ])
  })

  it('lets a low-risk approval through on High-risk, and never prompts for a reject', async () => {
    setMode('high')
    const { result } = renderHook(() => useOrderDecision(), { wrapper })
    const small = order({ orderId: 'o3', valueUsd: 20 })
    await act(async () => {
      await result.current.mutateAsync({ orderId: 'o3', approve: true, order: small })
      await result.current.mutateAsync({ orderId: 'o3', approve: false, order: small })
    })
    expect(authenticate).not.toHaveBeenCalled()
    expect(rpcCall).toHaveBeenCalledWith('trading.orders.approve', { orderId: 'o3' })
    expect(rpcCall).toHaveBeenCalledWith('trading.orders.reject', { orderId: 'o3' })
  })

  it('asks for a DCA mandate approval on Every approval, and a decline sends nothing', async () => {
    setMode('all')
    rpcCall.mockResolvedValue({ kind: 'mandate', mandate: { id: 'dca_1', name: 'DCA ETH' } })
    const { result } = renderHook(() => useMandateActions(), { wrapper })
    const m = {
      id: 'dca_1',
      name: 'DCA ETH',
      chain: { id: 8453, key: 'base', name: 'Base' },
      budget: { usdPerRun: 5, capUsd: 50 },
      runs: { max: null },
    } as unknown as Mandate
    authenticate.mockResolvedValueOnce({ ok: false, reason: 'cancelled' } satisfies AuthResult)
    let res: unknown = 'unset'
    await act(async () => {
      res = await result.current.approve(m)
    })
    expect(res).toBeNull()
    expect(authenticate).toHaveBeenCalledWith('approve the DCA “DCA ETH”, up to $50.00, on Base')
    expect(rpcCall).not.toHaveBeenCalledWith('trading.dca.approve', expect.anything())
    authenticate.mockResolvedValueOnce({ ok: true } satisfies AuthResult)
    await act(async () => {
      await result.current.approve(m)
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.dca.approve', { mandateId: 'dca_1' })
  })

  it('asks before arming a price trigger, and a decline sends nothing', async () => {
    setMode('high')
    rpcCall.mockResolvedValue({ kind: 'trigger', trigger: { id: 'trg_1', name: 'Stop-loss ETH' } })
    const { result } = renderHook(() => useTriggerActions(), { wrapper })
    const tr = {
      id: 'trg_1',
      name: 'Stop-loss ETH',
      kind: 'sell',
      chain: { id: 8453, key: 'base', name: 'Base' },
      token: { symbol: 'ETH' },
      condition: { direction: 'below', priceUsd: 3800, label: 'under $3,800' },
      action: { kind: 'sell', amountPct: 100, amountUsd: null, amount: null, estimatedUsd: 776 },
    } as unknown as Trigger
    authenticate.mockResolvedValueOnce({ ok: false, reason: 'cancelled' } satisfies AuthResult)
    let res: unknown = 'unset'
    await act(async () => {
      res = await result.current.approve(tr)
    })
    expect(res).toBeNull()
    expect(authenticate).toHaveBeenCalledWith(
      'arm the trigger “Stop-loss ETH”: sell 100 % of ETH when under $3,800, on Base',
    )
    expect(rpcCall).not.toHaveBeenCalledWith('trading.trigger.approve', expect.anything())
    authenticate.mockResolvedValueOnce({ ok: true } satisfies AuthResult)
    await act(async () => {
      await result.current.approve(tr)
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.trigger.approve', { triggerId: 'trg_1' })
    // Reject never prompts.
    authenticate.mockClear()
    await act(async () => {
      await result.current.reject(tr)
    })
    expect(authenticate).not.toHaveBeenCalled()
    expect(rpcCall).toHaveBeenCalledWith('trading.trigger.reject', { triggerId: 'trg_1' })
  })

  it('asks before arming a bracket and before its Sell now, naming both lines and the size', async () => {
    setMode('high')
    rpcCall.mockResolvedValue({ kind: 'bracket', bracket: { id: 'brk_1', name: 'Protect ETH' } })
    const { result } = renderHook(() => useBracketActions(), { wrapper })
    const b = {
      id: 'brk_1',
      name: 'Protect ETH',
      kind: 'sell',
      chain: { id: 8453, key: 'base', name: 'Base' },
      token: { symbol: 'ETH' },
      lines: {
        takeProfitUsd: 4560,
        stopLossUsd: 3420,
        trailPct: null,
        takeProfitLabel: 'over $4,560',
        stopLossLabel: 'under $3,420',
      },
      action: {
        kind: 'sell',
        amountPct: 100,
        amountUsd: null,
        amount: null,
        tpPct: null,
        estimatedUsd: 776,
      },
    } as unknown as Bracket
    authenticate.mockResolvedValueOnce({ ok: false, reason: 'cancelled' } satisfies AuthResult)
    let res: unknown = 'unset'
    await act(async () => {
      res = await result.current.approve(b)
    })
    expect(res).toBeNull()
    expect(authenticate).toHaveBeenCalledWith(
      'arm the bracket “Protect ETH”: sell 100 % of ETH · take profit over $4,560 · stop under $3,420, on Base',
    )
    expect(rpcCall).not.toHaveBeenCalledWith('trading.bracket.approve', expect.anything())
    authenticate.mockResolvedValueOnce({ ok: true } satisfies AuthResult)
    await act(async () => {
      await result.current.approve(b)
    })
    expect(rpcCall).toHaveBeenCalledWith('trading.bracket.approve', { bracketId: 'brk_1' })
    authenticate.mockResolvedValueOnce({ ok: false, reason: 'cancelled' } satisfies AuthResult)
    await act(async () => {
      await result.current.fire(b, 'sl')
    })
    expect(authenticate).toHaveBeenLastCalledWith(
      'fire the bracket “Protect ETH” now: sell 100 % of ETH · take profit over $4,560 · stop under $3,420, on Base',
    )
    expect(rpcCall).not.toHaveBeenCalledWith('trading.bracket.fire', expect.anything())
    // Reject, pause and stop never prompt.
    authenticate.mockClear()
    await act(async () => {
      await result.current.reject(b)
      await result.current.pause(b)
      await result.current.stop(b)
    })
    expect(authenticate).not.toHaveBeenCalled()
    expect(rpcCall).toHaveBeenCalledWith('trading.bracket.reject', { bracketId: 'brk_1' })
  })

  it('asks before a key export or a wallet removal whenever it is not Off', async () => {
    setMode('high')
    const { result } = renderHook(() => useWalletMutation(), { wrapper })
    const address = '0x1234567890abcdef1234567890abcdef1234abcd'
    authenticate.mockResolvedValue({ ok: false, reason: 'unavailable' } satisfies AuthResult)
    for (const method of ['wallet.export', 'wallet.remove']) {
      const err = await act(async () =>
        result.current
          .mutateAsync({ method, params: { address, password: 'pw', format: 'privateKey' } })
          .catch((e: unknown) => e),
      )
      expect(isTouchIdDeclined(err)).toBe(true)
    }
    expect(authenticate.mock.calls.map(([r]) => r)).toEqual([
      'export the private key of 0x1234…abcd',
      'remove the wallet 0x1234…abcd',
    ])
    // The password never left the renderer.
    expect(rpcCall).not.toHaveBeenCalledWith('wallet.export', expect.anything())
    expect(rpcCall).not.toHaveBeenCalledWith('wallet.remove', expect.anything())
    // Other vault writes are not gated.
    await act(async () => {
      await result.current.mutateAsync({ method: 'wallet.rename', params: { address } })
    })
    expect(authenticate).toHaveBeenCalledTimes(2)
    expect(rpcCall).toHaveBeenCalledWith('wallet.rename', { address })
  })
})
