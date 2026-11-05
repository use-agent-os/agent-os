import { act, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readTradingSessionFiled, writeTradingAgentVersion } from '~/stores/trading-ui'
import { TRADING_AGENT_VERSION } from './agent'
import { WALLET } from '../test-utils'
import { useTradingSession } from './useTradingSession'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))

const PROJECT = { project_id: 'p1', name: 'Trading desk', knowledge: '' }

function wrapper({ children }: { children: ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('useTradingSession filing', () => {
  beforeEach(() => {
    localStorage.clear()
    // The agent itself is someone else's concern here.
    writeTradingAgentVersion(TRADING_AGENT_VERSION)
    vi.useFakeTimers()
    rpcCall.mockReset()
  })
  afterEach(() => vi.useRealTimers())

  it('retries the patch until the gateway has created the session', async () => {
    let patches = 0
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'projects.list') return { projects: [PROJECT] }
      if (method === 'sessions.patch') {
        patches += 1
        if (patches < 3) throw new Error('Session not found')
        return {}
      }
      return {}
    })
    const { result } = renderHook(
      () => useTradingSession({ wallets: [WALLET], chains: [8453], limits: null }),
      { wrapper },
    )
    await flush()
    // Mount tries once (a previous launch may have left the chat unfiled)
    // and does not loop: a brand-new key has no session to file yet.
    expect(patches).toBe(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    expect(patches).toBe(1)
    expect(readTradingSessionFiled()).toBe(false)

    // The first send is where the session appears, so that is what retries.
    act(() => result.current.ensureFiled())
    await flush()
    expect(patches).toBe(2)
    expect(readTradingSessionFiled()).toBe(false)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500)
    })
    expect(patches).toBe(3)
    expect(readTradingSessionFiled()).toBe(true)
    const filed = rpcCall.mock.calls.filter(([m]) => m === 'sessions.patch').map(([, p]) => p)
    // Before a send the row is only labelled; from the first send on the
    // gateway is titling the chat, so filing must leave the name alone.
    expect(filed[0]).toMatchObject({ projectId: 'p1', displayName: 'Trading desk' })
    for (const patch of filed.slice(1)) {
      expect(patch).toMatchObject({ projectId: 'p1' })
      expect(patch).not.toHaveProperty('displayName')
    }
    // The knowledge was refreshed on the way in: the primary rule is in it.
    const update = rpcCall.mock.calls.find(([m]) => m === 'projects.update')?.[1] as {
      knowledge: string
    }
    expect(update.knowledge).toContain(`Primary wallet: Main (${WALLET.address})`)
  })

  it('rewrites the knowledge when the wallets change after filing', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'projects.list') return { projects: [PROJECT] }
      return {}
    })
    const { result, rerender } = renderHook(
      (p: { wallets: (typeof WALLET)[] }) =>
        useTradingSession({ wallets: p.wallets, chains: [8453], limits: null }),
      { wrapper, initialProps: { wallets: [WALLET] } },
    )
    await flush()
    act(() => result.current.ensureFiled())
    await flush()
    expect(readTradingSessionFiled()).toBe(true)
    const before = rpcCall.mock.calls.filter(([m]) => m === 'projects.update').length

    const second = {
      ...WALLET,
      address: '0x2222222222222222222222222222222222222222',
      label: '10k',
    }
    rerender({
      wallets: [
        { ...WALLET, primary: false },
        { ...second, primary: true },
      ],
    })
    await flush()
    const updates = rpcCall.mock.calls.filter(([m]) => m === 'projects.update')
    expect(updates.length).toBe(before + 1)
    const last = updates[updates.length - 1]?.[1] as { knowledge: string }
    expect(last.knowledge).toContain(`Primary wallet: 10k (${second.address})`)
    expect(last.knowledge).toContain(
      `Other wallets, only when the user names them: Main (${WALLET.address})`,
    )
  })
})
