import { afterEach, describe, expect, it } from 'vitest'
import { resolveWsUrl } from './bootstrap'

// jsdom exposes a mutable location; each test sets the page origin it needs and
// afterEach restores the jsdom default so cases stay isolated.
function setLocation(href: string) {
  const url = new URL(href)
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: {
      href: url.href,
      protocol: url.protocol,
      host: url.host,
    },
  })
}

describe('resolveWsUrl', () => {
  afterEach(() => {
    setLocation('http://localhost:3000/')
  })

  it('prefers the location-derived wss default when the server downgrades a same-host https page to ws://', () => {
    // TLS proxy dropped x-forwarded-proto: server saw http and emitted ws://
    // for a page served over https (app.js:191-195 always derived wss:// here).
    setLocation('https://console.example.com/control/')
    expect(resolveWsUrl('ws://console.example.com/ws')).toBe('wss://console.example.com/ws')
  })

  it('passes an already-secure wss ws_url through unchanged on an https page', () => {
    setLocation('https://console.example.com/control/')
    expect(resolveWsUrl('wss://console.example.com/ws')).toBe('wss://console.example.com/ws')
  })

  it('does not rewrite a ws:// URL that points at a different host', () => {
    // A deliberate cross-host target is not a proxy downgrade; leave it alone.
    setLocation('https://console.example.com/control/')
    expect(resolveWsUrl('ws://gateway.internal:18791/ws')).toBe('ws://gateway.internal:18791/ws')
  })

  it('leaves ws_url untouched on a plain http page (no downgrade to correct)', () => {
    setLocation('http://localhost:3000/')
    expect(resolveWsUrl('ws://localhost:3000/ws')).toBe('ws://localhost:3000/ws')
  })

  it('returns the raw value when ws_url is not parseable as a URL', () => {
    setLocation('https://console.example.com/control/')
    expect(resolveWsUrl('::not a url::')).toBe('::not a url::')
  })
})
