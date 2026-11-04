import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { sessionPath } from '~/components/sidebar/SessionRow'
import { useGateway } from '~/stores/gateway'
import {
  readTradingSessionFiled,
  readTradingSessionKey,
  writeTradingAgentVersion,
  writeTradingSessionFiled,
  writeTradingSessionKey,
} from '~/stores/trading-ui'
import { ChatView } from '~/views/chat/ChatView'
import { WALLET } from '../test-utils'
import { TRADING_AGENT_VERSION } from './agent'
import type { DeskProps } from './useDeskInstruments'
import { useTradingSession } from './useTradingSession'

const FIRST = 'agent:trading:webchat:trading-first1'
const OLDER = 'agent:trading:webchat:trading-older1'
const CTX = { wallets: [WALLET], chains: [8453], limits: null }

const gateway = vi.hoisted(() => ({
  patches: [] as { key: string; projectId: string; displayName: string }[],
  sent: new Set<string>(),
  // What `sessions.patch` does with a key: resolve, or throw like the gateway.
  onPatch: (() => {}) as (key: string, nth: number) => void,
}))

const rpc = vi.hoisted(() => ({
  waitForConnection: async () => {},
  on: () => () => {},
  call: async (method: string, params?: unknown): Promise<unknown> => {
    if (method === 'projects.list')
      return { projects: [{ project_id: 'p1', name: 'Trading desk' }] }
    if (method === 'sessions.patch') {
      const p = params as { key: string; projectId: string; displayName: string }
      const nth = gateway.patches.filter((x) => x.key === p.key).length
      gateway.patches.push(p)
      gateway.onPatch(p.key, nth)
      return {}
    }
    return {}
  },
}))
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))
vi.mock('@/views/chat/RoutePicker', () => ({ RoutePicker: () => null }))

// The transcript's send and per-session history, as `useTranscript` has them.
vi.mock('@/views/chat/useTranscript', async () => {
  const React = await import('react')
  return {
    useTranscript: (opts: { sessionKey: string }) => {
      const [history, setHistory] = React.useState<string[]>([])
      const [historySession, setHistorySession] = React.useState(opts.sessionKey)
      if (historySession !== opts.sessionKey) {
        setHistorySession(opts.sessionKey)
        setHistory([])
      }
      const container = React.useRef(null)
      const dock = React.useRef(null)
      return {
        containerRef: container,
        routerFxDockRef: dock,
        send: (text: string) => {
          gateway.sent.add(opts.sessionKey)
          setHistory((prev) => [...prev, text])
        },
        abort: () => {},
        busy: false,
        routerFxEnabled: false,
        setRouterFxEnabled: () => {},
        history,
        runState: { status: 'idle', label: '' },
        pinnedToTail: true,
        scrollToTail: () => {},
        isCompactInFlightForCurrentSession: () => false,
        setStreamIdlePausedForApproval: () => {},
        setPendingDelegates: () => {},
      }
    },
  }
})

vi.mock('~/views/trading/desk/useDeskInstruments', () => ({
  useDeskInstruments: () => ({
    region: null,
    dockAbove: null,
    seats: null,
    placeholder: undefined,
    still: false,
    modal: null,
    emptyHint: null,
    onFocusChange: () => {},
  }),
}))

/** The wiring useDeskFrame does: the desk's session hook behind the one chat. */
function Desk() {
  const session = useTradingSession(CTX, true)
  const navigate = useNavigate()
  const desk = {
    entering: false,
    onFirstSend: session.ensureFiled,
    onStartFresh: session.startFresh,
  } as unknown as DeskProps
  return (
    <>
      <ChatView desk={desk} />
      <button type="button" onClick={() => void navigate(sessionPath(OLDER))}>
        open older
      </button>
    </>
  )
}

function mountDesk() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[`/sessions/${encodeURIComponent(FIRST)}`]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/sessions/:key?" element={<Desk />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20))
  })
}

async function sendOnce(text: string) {
  const ta = screen.getByRole('textbox')
  fireEvent.change(ta, { target: { value: text } })
  await act(async () => {
    fireEvent.keyDown(ta, { key: 'Enter' })
  })
  await settle()
}

async function startFresh(): Promise<string> {
  const before = readTradingSessionKey()
  fireEvent.click(screen.getByTestId('chat-fresh'))
  await settle()
  const key = readTradingSessionKey()
  expect(key).not.toBe(before)
  return key
}

const patchedKeys = () => gateway.patches.map((p) => p.key)

beforeEach(() => {
  localStorage.clear()
  writeTradingAgentVersion(TRADING_AGENT_VERSION)
  writeTradingSessionKey(FIRST)
  writeTradingSessionFiled(true)
  gateway.patches = []
  gateway.sent = new Set()
  gateway.onPatch = () => {}
  useConnection.getState().setState('connected')
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
})

describe('desk filing across fresh sessions in one mount', () => {
  it('real gateway: an unsent desk key is filed by the mint-time patch', async () => {
    mountDesk()
    await settle()
    await sendOnce('first chat')
    const second = await startFresh()
    await sendOnce('second chat')
    const third = await startFresh()
    await sendOnce('third chat')
    expect(patchedKeys()).toEqual([second, third])
    expect(gateway.patches[0]).toMatchObject({ projectId: 'p1', displayName: 'Trading desk' })
  })

  it('a failed mint-time patch is retried by the first send of that session', async () => {
    gateway.onPatch = (_key, nth) => {
      if (nth === 0) throw new Error('gateway busy')
    }
    mountDesk()
    await settle()
    await sendOnce('first chat')
    const second = await startFresh()
    expect(patchedKeys()).toEqual([second])
    await sendOnce('second chat')
    await waitFor(() => {
      expect(patchedKeys().filter((k) => k === second)).toHaveLength(2)
    })
    expect(readTradingSessionFiled()).toBe(true)
  })

  it('the same text as the last session still counts as this session first send', async () => {
    gateway.onPatch = (_key, nth) => {
      if (nth === 0) throw new Error('gateway busy')
    }
    mountDesk()
    await settle()
    await sendOnce('gm')
    const second = await startFresh()
    await sendOnce('gm')
    await waitFor(() => {
      expect(patchedKeys().filter((k) => k === second)).toHaveLength(2)
    })
  })

  it('sending in an older desk chat never files or renames that chat', async () => {
    // The newest chat is left unfiled, so the filing path really runs.
    gateway.onPatch = (_key, nth) => {
      if (nth === 0) throw new Error('gateway busy')
    }
    mountDesk()
    await settle()
    const newest = await startFresh()
    expect(readTradingSessionFiled()).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'open older' }))
    await settle()
    await sendOnce('still here')
    await settle()
    expect(patchedKeys()).not.toContain(OLDER)
    expect(readTradingSessionKey()).toBe(newest)
  })
})
