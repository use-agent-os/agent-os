// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from 'vitest'

type Details = Record<string, unknown>
type Listener = (details: Details, callback: (response: Details) => void) => void

// The hooks the module installs, captured so the tests can drive them: the
// helpers are pure, and `installLoopbackCors` only needs a session that
// records its listeners.
const hooks = vi.hoisted(() => ({
  filters: [] as unknown[],
  before: null as Listener | null,
  received: null as Listener | null,
}))

vi.mock('electron', () => ({
  session: {
    defaultSession: {
      webRequest: {
        onBeforeSendHeaders: (filter: unknown, listener: Listener) => {
          hooks.filters.push(filter)
          hooks.before = listener
        },
        onHeadersReceived: (filter: unknown, listener: Listener) => {
          hooks.filters.push(filter)
          hooks.received = listener
        },
      },
    },
  },
}))

const {
  gatewayOriginFor,
  installGatewayCors,
  isGatewayRequest,
  GATEWAY_URL_PATTERNS,
  rendererCorsOrigin,
  withRendererOrigin,
} = await import('./loopback-cors')

function run(listener: Listener | null, details: Details): Details {
  let out: Details = {}
  listener?.(details, (response) => (out = response))
  return out
}

describe('gatewayOriginFor', () => {
  it('is the target origin, with ws folded onto http', () => {
    expect(gatewayOriginFor('ws://127.0.0.1:18791/ws')).toBe('http://127.0.0.1:18791')
    expect(gatewayOriginFor('http://localhost:18791/api/v1/artifacts/a?x=1')).toBe(
      'http://localhost:18791',
    )
  })

  it('folds wss onto https, dropping a default port as a browser would', () => {
    expect(gatewayOriginFor('wss://gw.example.com/ws')).toBe('https://gw.example.com')
    expect(gatewayOriginFor('wss://gw.example.com:443/ws')).toBe('https://gw.example.com')
    expect(gatewayOriginFor('https://gw.example.com:8443/api')).toBe('https://gw.example.com:8443')
  })

  it('is null for a URL that does not parse', () => {
    expect(gatewayOriginFor('garbage')).toBeNull()
  })
})

describe('isGatewayRequest', () => {
  it('admits loopback on any port whatever the configured endpoint', () => {
    expect(isGatewayRequest('http://127.0.0.1:19000/api', 'https://gw.example.com')).toBe(true)
    expect(isGatewayRequest('ws://localhost:18791/ws', null)).toBe(true)
    expect(isGatewayRequest('http://[::1]:18791/api', null)).toBe(true)
  })

  it('admits the configured origin and nothing else off loopback', () => {
    expect(isGatewayRequest('wss://gw.example.com/ws', 'https://gw.example.com')).toBe(true)
    expect(isGatewayRequest('https://gw.example.com:443/api', 'https://gw.example.com')).toBe(true)
    expect(isGatewayRequest('https://assets.coingecko.com/x.png', 'https://gw.example.com')).toBe(
      false,
    )
    expect(isGatewayRequest('https://gw.example.com/api', null)).toBe(false)
    expect(isGatewayRequest('garbage', 'https://gw.example.com')).toBe(false)
  })
})

describe('rendererCorsOrigin', () => {
  it('is the literal null for a renderer loaded from disk', () => {
    // fetch sends an opaque origin as `null`, and expects it back verbatim.
    expect(rendererCorsOrigin('file:///app/out/renderer/')).toBe('null')
  })

  it('is the dev server origin as-is', () => {
    expect(rendererCorsOrigin('http://localhost:5173')).toBe('http://localhost:5173')
  })
})

describe('withRendererOrigin', () => {
  it('restates a reflected allow-origin for the renderer, keeping the rest', () => {
    expect(
      withRendererOrigin(
        { 'access-control-allow-origin': ['http://127.0.0.1:18791'], vary: ['Origin'] },
        'null',
      ),
    ).toEqual({ 'access-control-allow-origin': 'null', vary: ['Origin'] })
  })

  it('matches the header name case-insensitively without duplicating it', () => {
    const out = withRendererOrigin(
      { 'Access-Control-Allow-Origin': ['http://127.0.0.1:18791'], 'Content-Type': ['text/csv'] },
      'http://localhost:5173',
    )
    expect(out).toEqual({
      'Access-Control-Allow-Origin': 'http://localhost:5173',
      'Content-Type': ['text/csv'],
    })
    expect(
      Object.keys(out).filter((k) => k.toLowerCase() === 'access-control-allow-origin'),
    ).toHaveLength(1)
  })

  it('leaves a response without CORS headers alone', () => {
    // A loopback server that never opted into CORS must not become readable.
    const headers = { 'content-type': ['text/html'] }
    expect(withRendererOrigin(headers, 'null')).toBe(headers)
  })
})

describe('installGatewayCors', () => {
  beforeEach(() => {
    hooks.filters = []
    hooks.before = null
    hooks.received = null
  })

  it('presents the gateway origin on the way out and the renderer origin on the way back', () => {
    installGatewayCors('file:///app/out/renderer/', () => null)
    expect(hooks.filters).toEqual([{ urls: GATEWAY_URL_PATTERNS }, { urls: GATEWAY_URL_PATTERNS }])

    expect(
      run(hooks.before, {
        url: 'ws://127.0.0.1:18791/ws',
        requestHeaders: { Origin: 'file://', Accept: '*/*' },
      }),
    ).toEqual({ requestHeaders: { Origin: 'http://127.0.0.1:18791', Accept: '*/*' } })

    expect(
      run(hooks.received, {
        url: 'http://127.0.0.1:18791/api/v1/artifacts/a',
        responseHeaders: {
          'access-control-allow-origin': ['http://127.0.0.1:18791'],
          'access-control-allow-credentials': ['true'],
        },
      }),
    ).toEqual({
      responseHeaders: {
        'access-control-allow-origin': 'null',
        'access-control-allow-credentials': ['true'],
      },
    })
  })

  it('uses the dev server origin for a dev renderer', () => {
    installGatewayCors('http://localhost:5173', () => null)
    expect(
      run(hooks.received, {
        url: 'http://127.0.0.1:18791/control/api/bootstrap',
        responseHeaders: { 'access-control-allow-origin': ['http://127.0.0.1:18791'] },
      }),
    ).toEqual({ responseHeaders: { 'access-control-allow-origin': 'http://localhost:5173' } })
  })

  it('treats the configured remote gateway like loopback, read at request time', () => {
    let configured: string | null = null
    installGatewayCors('file:///app/out/renderer/', () => configured)
    const before = {
      url: 'wss://gw.example.com/ws',
      requestHeaders: { Origin: 'file://', Accept: '*/*' },
    }
    // Not the gateway yet: nothing is presented.
    expect(run(hooks.before, before)).toEqual({ requestHeaders: before.requestHeaders })

    configured = 'https://gw.example.com'
    expect(run(hooks.before, before)).toEqual({
      requestHeaders: { Origin: 'https://gw.example.com', Accept: '*/*' },
    })
    expect(
      run(hooks.received, {
        url: 'https://gw.example.com/api/v1/artifacts/a',
        responseHeaders: { 'access-control-allow-origin': ['https://gw.example.com'] },
      }),
    ).toEqual({ responseHeaders: { 'access-control-allow-origin': 'null' } })
  })

  it('leaves other https hosts alone', () => {
    installGatewayCors('file:///app/out/renderer/', () => 'https://gw.example.com')
    const headers = { Origin: 'file://', Accept: '*/*' }
    expect(
      run(hooks.before, { url: 'https://assets.coingecko.com/x.png', requestHeaders: headers }),
    ).toEqual({ requestHeaders: headers })
    const response = { 'access-control-allow-origin': ['*'] }
    expect(
      run(hooks.received, { url: 'https://assets.coingecko.com/x.png', responseHeaders: response }),
    ).toEqual({ responseHeaders: response })
  })

  it('leaves headers untouched when the request URL does not parse', () => {
    installGatewayCors('file:///app/out/renderer/', () => null)
    expect(run(hooks.before, { url: 'garbage', requestHeaders: { Accept: '*/*' } })).toEqual({
      requestHeaders: { Accept: '*/*' },
    })
    expect(run(hooks.received, { url: 'garbage' })).toEqual({ responseHeaders: {} })
  })
})
