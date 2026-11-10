import { afterEach, describe, expect, it, vi } from 'vitest'
import { bindGatewaySocket } from './api-origin'
import { authenticatedHeaders, sessionAuthToken } from './http-auth'

afterEach(() => {
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('gateway REST authentication', () => {
  it('adds the per-tab WebSocket token without dropping caller headers', () => {
    sessionStorage.setItem('agentos.wsToken', 'token-123')

    expect(authenticatedHeaders('/api/approvals', { 'Content-Type': 'application/json' })).toEqual({
      'Content-Type': 'application/json',
      Authorization: 'Bearer token-123',
    })
  })

  it('returns only caller headers when no token is configured', () => {
    expect(authenticatedHeaders('/api/approvals', { Accept: 'application/json' })).toEqual({
      Accept: 'application/json',
    })
  })

  it('fails closed to an empty token when storage access is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked')
    })

    expect(sessionAuthToken()).toBe('')
    expect(authenticatedHeaders('/api/approvals')).toEqual({})
  })

  it('never sends the token to an origin other than the connected gateway', () => {
    sessionStorage.setItem('agentos.wsToken', 'token-B')
    const unbind = bindGatewaySocket({ currentUrl: 'ws://127.0.0.1:28812/ws' })
    try {
      expect(authenticatedHeaders('http://127.0.0.1:28812/api/approvals')).toEqual({
        Authorization: 'Bearer token-B',
      })
      // The page's own origin is another gateway now.
      expect(authenticatedHeaders('/api/approvals')).toEqual({})
      expect(authenticatedHeaders(`${window.location.origin}/api/approvals`)).toEqual({})
      expect(authenticatedHeaders('https://elsewhere.example/api')).toEqual({})
    } finally {
      unbind()
    }
  })
})
