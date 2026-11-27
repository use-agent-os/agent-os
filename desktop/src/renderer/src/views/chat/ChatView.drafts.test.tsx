import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render } from '@testing-library/react'
import { createRef, useEffect } from 'react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { KeyboardShortcutProvider } from '@/components/KeyboardShortcuts'
import { useConnection } from '@/stores/connection'
import { sessionPath } from '~/components/sidebar/SessionList'
import { NEW_SESSION_DRAFT, readDraft } from '~/stores/composer-drafts'
import { useGateway } from '~/stores/gateway'
import { ChatView } from './ChatView'

// Text typed and not sent is the session's draft: it is still there after a
// look at another session or another page, and it stays out of the session
// the user looked at. The chat view is one mounted composer for every
// session, and unmounts for every other page.

const CHAT_A = 'agent:main:webchat:drafts-a'
const CHAT_B = 'agent:main:webchat:drafts-b'

const rpc = {
  waitForConnection: vi.fn(async () => {}),
  call: vi.fn(async (): Promise<unknown> => ({})),
  on: vi.fn(() => () => {}),
}
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))
vi.mock('@/views/chat/RoutePicker', () => ({ RoutePicker: () => null }))

const send = vi.fn()
vi.mock('@/views/chat/useTranscript', () => ({
  useTranscript: () => ({
    containerRef: createRef(),
    routerFxDockRef: createRef(),
    send,
    abort: vi.fn(),
    busy: false,
    routerFxEnabled: false,
    setRouterFxEnabled: vi.fn(),
    history: [],
    runState: { status: 'idle', label: '' },
    pinnedToTail: true,
    scrollToTail: vi.fn(),
    isCompactInFlightForCurrentSession: () => false,
    setStreamIdlePausedForApproval: vi.fn(),
    setPendingDelegates: vi.fn(),
  }),
}))

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

const nav: { go: ((path: string) => void) | null } = { go: null }

function Nav() {
  const navigate = useNavigate()
  useEffect(() => {
    nav.go = (path) => void navigate(path)
  }, [navigate])
  return null
}

function mount(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <KeyboardShortcutProvider>
          <Nav />
          <Routes>
            <Route path="/sessions/:key?" element={<ChatView />} />
            <Route path="/skills" element={<p>skills</p>} />
          </Routes>
        </KeyboardShortcutProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const composer = () => document.querySelector('textarea') as HTMLTextAreaElement
const type = (value: string) => fireEvent.change(composer(), { target: { value } })
const go = (path: string) => act(() => nav.go?.(path))

beforeEach(() => {
  send.mockClear()
  useConnection.getState().setState('connected')
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
})

describe('ChatView · unsent text is kept per session', () => {
  it('a new session’s draft survives opening another session', () => {
    mount('/sessions')
    type('aasdasd\ndasdsadsad')

    go(sessionPath(CHAT_A))
    expect(composer().value).toBe('')

    go('/sessions')
    expect(composer().value).toBe('aasdasd\ndasdsadsad')
  })

  it('a new session’s draft survives a visit to another page', () => {
    mount('/sessions')
    type('still thinking')

    go('/skills')
    expect(document.querySelector('textarea')).toBeNull()

    go('/sessions')
    expect(composer().value).toBe('still thinking')
  })

  it('each session keeps its own draft', () => {
    mount(sessionPath(CHAT_A))
    type('for a')

    go(sessionPath(CHAT_B))
    expect(composer().value).toBe('')
    type('for b')

    go(sessionPath(CHAT_A))
    expect(composer().value).toBe('for a')
    go(sessionPath(CHAT_B))
    expect(composer().value).toBe('for b')
  })

  it('sending uses up the draft: the next new session starts empty', async () => {
    mount('/sessions')
    type('ship it')
    await act(async () => {
      fireEvent.keyDown(composer(), { key: 'Enter' })
      await Promise.resolve()
    })
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    expect(readDraft(NEW_SESSION_DRAFT)).toBe('')

    go('/sessions')
    expect(composer().value).toBe('')
  })
})
