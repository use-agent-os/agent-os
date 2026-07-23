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
    // 'ws://[bad' has an invalid IPv6 host bracket and throws in `new URL`
    // even with a base — this genuinely exercises the catch branch. (The
    // previous input '::not a url::' parsed fine relative to the base and
    // only covered the final passthrough.)
    setLocation('https://console.example.com/control/')
    expect(resolveWsUrl('ws://[bad')).toBe('ws://[bad')
  })

  it('returns a same-host ws:// URL unchanged on an http page (early return, not rewrite)', () => {
    // Kills the surviving mutant: without the non-https early return, this
    // same-host ws input would be rewritten to defaultWsUrl()'s /ws path.
    setLocation('http://localhost:3000/')
    expect(resolveWsUrl('ws://localhost:3000/custom-path')).toBe('ws://localhost:3000/custom-path')
  })
})
