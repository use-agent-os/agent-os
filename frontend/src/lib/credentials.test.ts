import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  WS_TOKEN_KEY,
  clearPendingGatewayUrl,
  consumeUrlCredentials,
  pendingGatewayUrlRequest,
  readStoredToken,
  storeToken,
} from './credentials'
import { sessionAuthToken } from './http-auth'

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  clearPendingGatewayUrl()
})

function fakeHistory() {
  return { state: { idx: 3 }, replaceState: vi.fn() }
}

describe('consumeUrlCredentials', () => {
  it('takes a fragment token, stores it for the tab and strips it from the URL', () => {
    const history = fakeHistory()
    const out = consumeUrlCredentials({ href: 'http://h/control/chat#token=abc' }, history)
    expect(out.token).toBe('abc')
    expect(sessionStorage.getItem(WS_TOKEN_KEY)).toBe('abc')
    expect(localStorage.length).toBe(0)
    expect(history.replaceState).toHaveBeenCalledWith({ idx: 3 }, '', '/control/chat')
  })

  it('takes a query token and keeps the other parameters', () => {
    const history = fakeHistory()
    consumeUrlCredentials({ href: 'http://h/control/usage?range=7d&token=t1#x' }, history)
    expect(readStoredToken()).toBe('t1')
    expect(history.replaceState).toHaveBeenCalledWith(
      expect.anything(),
      '',
      '/control/usage?range=7d#x',
    )
  })

  it('holds a gatewayUrl for confirmation instead of applying it', () => {
    const history = fakeHistory()
    const out = consumeUrlCredentials(
      { href: 'http://h/control/?gatewayUrl=wss%3A%2F%2Fother.example%2Fws' },
      history,
    )
    expect(out.gatewayUrl).toBe('wss://other.example/ws')
    expect(pendingGatewayUrlRequest()).toBe('wss://other.example/ws')
    expect(localStorage.getItem('agentos.wsUrl')).toBeNull()
    expect(history.replaceState).toHaveBeenCalledWith(expect.anything(), '', '/control/')
  })

  it('leaves a URL without credentials untouched', () => {
    const history = fakeHistory()
    expect(consumeUrlCredentials({ href: 'http://h/control/chat?x=1#frag' }, history)).toEqual({
      token: null,
      gatewayUrl: null,
    })
    expect(history.replaceState).not.toHaveBeenCalled()
  })

  it('really removes the token from the address bar and history', () => {
    window.history.pushState(null, '', '/control/chat#token=secret')
    consumeUrlCredentials()
    expect(window.location.href).not.toContain('secret')
    expect(window.location.pathname).toBe('/control/chat')
  })
})

describe('per-tab storage', () => {
  it('keeps the token in sessionStorage only, so a new tab starts without one', () => {
    storeToken('abc')
    expect(sessionAuthToken()).toBe('abc')
    // A new tab gets a fresh sessionStorage; localStorage never held it.
    expect(Object.keys({ ...localStorage })).not.toContain(WS_TOKEN_KEY)
    sessionStorage.clear()
    expect(readStoredToken()).toBe('')
  })

  it('tolerates blocked storage', () => {
    const spy = vi.spyOn(sessionStorage, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readStoredToken()).toBe('')
    spy.mockRestore()
  })
})
