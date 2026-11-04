import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, useLocation } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { writeReturnSession, writeTradingSessionKey } from '~/stores/trading-ui'
import { useDeskToggle } from './useDeskToggle'

const NEWEST = 'agent:trading:webchat:trading-newest'
const OLDER = 'agent:trading:webchat:trading-older'
const CHAT = 'agent:main:webchat:plain'
const path = (key: string) => `/sessions/${encodeURIComponent(key)}`

function mount(at: string) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[at]}>{children}</MemoryRouter>
  )
  return renderHook(() => ({ desk: useDeskToggle(), location: useLocation() }), { wrapper })
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  writeTradingSessionKey(NEWEST)
})

describe('useDeskToggle', () => {
  it('reads an older desk chat as Trading, and leaves it for the chat it came from', () => {
    writeReturnSession(CHAT)
    const { result } = mount(path(OLDER))
    expect(result.current.desk.mode).toBe('trading')

    act(() => result.current.desk.toggle())

    expect(result.current.location.pathname).toBe(path(CHAT))
    expect(result.current.desk.mode).toBe('chat')
  })

  it('enters from a chat at the newest desk chat, remembering the chat', () => {
    const { result } = mount(path(CHAT))
    expect(result.current.desk.mode).toBe('chat')

    act(() => result.current.desk.toggle())

    expect(result.current.location.pathname).toBe(path(NEWEST))
    expect(result.current.location.state).toEqual({ enterDesk: true })
    expect(result.current.desk.mode).toBe('trading')
  })
})
