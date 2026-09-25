import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useGateway } from '~/stores/gateway'
import { writeTradingSessionKey } from '~/stores/trading-ui'
import { SessionRoute } from './SessionRoute'

const frameState = { fullDesk: true }
// The mock keeps the argument so the test can assert what the route hands the frame.
// Its strip takes the slot ref the way StatusStrip does in Chat mode.
const useDeskFrame = vi.fn((input: unknown) => ({
  input,
  strip: (
    <div
      data-testid="strip"
      ref={(input as { sessionSlot?: (el: HTMLDivElement | null) => void }).sessionSlot}
    />
  ),
  banner: <div data-testid="frame-banner" />,
  desk: null,
  book: null,
  fullDesk: frameState.fullDesk,
  sheet: null,
  frameRef: { current: null },
  collapsed: false,
}))
vi.mock('./TradingDesk', () => ({ useDeskFrame: (input: unknown) => useDeskFrame(input) }))
vi.mock('../TradingView', () => ({
  TradingView: () => <div data-testid="trading-view" />,
}))
const chatProps: { last: { actionsSlot?: HTMLElement | null } | null } = { last: null }
vi.mock('~/views/chat/ChatView', () => ({
  ChatView: (props: { actionsSlot?: HTMLElement | null }) => {
    chatProps.last = props
    return <div data-testid="chat-view" />
  },
}))

const KEY = 'agent:trading:webchat:trading-t1'

function mount(key = KEY) {
  return render(
    <MemoryRouter initialEntries={[`/sessions/${encodeURIComponent(key)}`]}>
      <Routes>
        <Route path="/sessions/:key" element={<SessionRoute />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  writeTradingSessionKey(KEY)
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
  useDeskFrame.mockClear()
  chatProps.last = null
})

describe('SessionRoute', () => {
  it('lets the full desk carry its own gate: the frame banner is not shown twice', () => {
    frameState.fullDesk = true
    mount()
    expect(screen.getByTestId('trading-view')).toBeInTheDocument()
    expect(screen.queryByTestId('frame-banner')).toBeNull()
    expect(screen.getByTestId('strip')).toBeInTheDocument()
  })

  it('shows the frame banner over the chat + book layout', () => {
    frameState.fullDesk = false
    mount()
    expect(screen.getByTestId('chat-view')).toBeInTheDocument()
    expect(screen.getByTestId('frame-banner')).toBeInTheDocument()
  })

  it('hands the frame the derived mode, not a hardcoded one', () => {
    frameState.fullDesk = false
    mount()
    expect(useDeskFrame).toHaveBeenCalledWith(
      expect.objectContaining({ mode: 'trading', active: true, sessionKey: KEY }),
    )
  })

  it("hands the chat the strip's slot for its actions in Chat mode, and none at the desk", () => {
    frameState.fullDesk = false
    const { unmount } = mount('agent:main:webchat:plain')
    expect(chatProps.last?.actionsSlot).toBe(screen.getByTestId('strip'))
    unmount()
    // At the desk the strip is the desk's: the chat keeps a row of its own.
    mount()
    expect(screen.getByTestId('chat-view')).toBeInTheDocument()
    expect(chatProps.last?.actionsSlot).toBeUndefined()
  })
})
