import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { MemoryRouter, Route, Routes, useParams } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FlowRunOpen } from '@/views/chat/transcript/flowRun'
import { useConnection } from '@/stores/connection'
import { useGateway } from '~/stores/gateway'
import { ChatView } from './ChatView'

// "Open in Flows" on an Image Flow run card (docs/flows.md): the chat hands
// the shared transcript a handler that opens the desktop's editor for that
// flow. The web console passes none, so its cards carry no button.
const KEY = 'agent:main:webchat:flows-test'
const rpc = {
  waitForConnection: vi.fn(async () => {}),
  call: vi.fn(async () => ({})),
  on: vi.fn(() => () => {}),
}
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))
vi.mock('@/views/chat/RoutePicker', () => ({ RoutePicker: () => null }))

const seen: { onOpenFlow: FlowRunOpen | null | undefined } = { onOpenFlow: undefined }

vi.mock('@/views/chat/useTranscript', () => ({
  useTranscript: (opts: { onOpenFlow?: FlowRunOpen | null }) => {
    seen.onOpenFlow = opts.onOpenFlow
    return {
      containerRef: createRef(),
      routerFxDockRef: createRef(),
      send: vi.fn(),
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
    }
  },
}))

function FlowMarker() {
  const { id } = useParams()
  return <div data-testid="flow-editor">{id}</div>
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[`/sessions/${encodeURIComponent(KEY)}`]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/sessions/:key?" element={<ChatView desk={null} />} />
          <Route path="/flows/:id" element={<FlowMarker />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  seen.onOpenFlow = undefined
  useConnection.getState().setState('connected')
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
})

describe('ChatView · Image Flow run cards', () => {
  it('opens the flow in the Flows editor', () => {
    mount()
    expect(typeof seen.onOpenFlow).toBe('function')

    act(() => seen.onOpenFlow!('flw_ab12cd34'))

    expect(screen.getByTestId('flow-editor').textContent).toBe('flw_ab12cd34')
  })
})
