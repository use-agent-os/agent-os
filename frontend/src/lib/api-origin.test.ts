import { afterEach, describe, expect, it } from 'vitest'
import {
  apiOrigin,
  apiUrl,
  bindGatewaySocket,
  gatewayOrigin,
  hostControlBase,
  isGatewayUrl,
  socketHttpOrigin,
  urlBase,
} from './api-origin'

afterEach(() => {
  delete window.__AGENTOS_ENV__
})

describe('api-origin', () => {
  it('is same-origin relative when no host env is set', () => {
    expect(apiOrigin()).toBe('')
    expect(apiUrl('/api/approvals')).toBe('/api/approvals')
    expect(hostControlBase()).toBeNull()
    expect(urlBase()).toBe(window.location.origin)
  })

  it('prefixes gateway URLs with the host origin when set', () => {
    window.__AGENTOS_ENV__ = { apiOrigin: 'http://127.0.0.1:18791/', controlBase: '/control' }
    expect(apiOrigin()).toBe('http://127.0.0.1:18791')
    expect(apiUrl('api/v1/files/upload')).toBe('http://127.0.0.1:18791/api/v1/files/upload')
    expect(hostControlBase()).toBe('/control')
    expect(urlBase()).toBe('http://127.0.0.1:18791')
  })

  it('ignores a blank control base', () => {
    window.__AGENTOS_ENV__ = { controlBase: '  ' }
    expect(hostControlBase()).toBeNull()
  })

  it('derives the HTTP origin from the socket the console is connected to', () => {
    const page = window.location.origin
    expect(socketHttpOrigin(`ws://${window.location.host}/ws`)).toBe('')
    expect(socketHttpOrigin('ws://127.0.0.1:28812/ws', page)).toBe('http://127.0.0.1:28812')
    expect(socketHttpOrigin('wss://gw.example/ws', page)).toBe('https://gw.example')
    expect(socketHttpOrigin('not a url', page)).toBe('')
    expect(socketHttpOrigin('ftp://x/ws', page)).toBe('')

    const socket = { currentUrl: 'ws://127.0.0.1:28812/ws' }
    const unbind = bindGatewaySocket(socket)
    try {
      expect(gatewayOrigin()).toBe('http://127.0.0.1:28812')
      expect(apiUrl('/api/approvals')).toBe('http://127.0.0.1:28812/api/approvals')
      expect(urlBase()).toBe('http://127.0.0.1:28812')
      expect(isGatewayUrl('http://127.0.0.1:28812/api/approvals')).toBe(true)
      expect(isGatewayUrl('/api/approvals')).toBe(false)
      // Back on the page's own gateway: relative again.
      socket.currentUrl = `ws://${window.location.host}/ws`
      expect(apiUrl('/api/approvals')).toBe('/api/approvals')
      expect(isGatewayUrl('/api/approvals')).toBe(true)
    } finally {
      unbind()
    }
    expect(gatewayOrigin()).toBe('')
  })

  it('isGatewayUrl compares whole origins, so no look-alike URL passes (AOS-11)', () => {
    window.__AGENTOS_ENV__ = { apiOrigin: 'http://127.0.0.1:18791' }
    expect(isGatewayUrl('http://127.0.0.1:18791/api/v1/artifacts/1')).toBe(true)
    for (const url of [
      'https://attacker.invalid/px',
      'http://127.0.0.1:18791.attacker.invalid/px',
      'http://127.0.0.1:18791@attacker.invalid/px',
      'http://127.0.0.1:187910/px',
      'https://127.0.0.1:18791/px',
      'http://localhost:18791/px',
      '//attacker.invalid/px',
      'javascript:alert(1)',
      'not a url',
    ]) {
      expect(isGatewayUrl(url), url).toBe(false)
    }
  })

  it('a host env (the desktop) wins over the socket', () => {
    window.__AGENTOS_ENV__ = { apiOrigin: 'http://127.0.0.1:18791' }
    const unbind = bindGatewaySocket({ currentUrl: 'ws://127.0.0.1:28812/ws' })
    try {
      expect(apiUrl('/api/approvals')).toBe('http://127.0.0.1:18791/api/approvals')
    } finally {
      unbind()
    }
  })
})
