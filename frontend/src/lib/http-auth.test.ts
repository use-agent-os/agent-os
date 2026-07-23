import { afterEach, describe, expect, it, vi } from 'vitest'
import { authenticatedHeaders, sessionAuthToken } from './http-auth'

afterEach(() => {
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('gateway REST authentication', () => {
  it('adds the per-tab WebSocket token without dropping caller headers', () => {
    sessionStorage.setItem('agentos.wsToken', 'token-123')

    expect(authenticatedHeaders({ 'Content-Type': 'application/json' })).toEqual({
      'Content-Type': 'application/json',
      Authorization: 'Bearer token-123',
    })
  })

  it('returns only caller headers when no token is configured', () => {
    expect(authenticatedHeaders({ Accept: 'application/json' })).toEqual({
      Accept: 'application/json',
    })
  })

  it('fails closed to an empty token when storage access is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked')
    })

    expect(sessionAuthToken()).toBe('')
    expect(authenticatedHeaders()).toEqual({})
  })
})
