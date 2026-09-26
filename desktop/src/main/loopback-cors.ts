import { session } from 'electron'

/**
 * The gateway answers on loopback, on whichever port the user configured (or
 * an adopted gateway already holds), so the hooks match any port — the same
 * shape the renderer's CSP allows.
 */
export const LOOPBACK_URLS = [
  'http://127.0.0.1/*',
  'ws://127.0.0.1/*',
  'http://localhost/*',
  'ws://localhost/*',
]

/**
 * The origin to present on a loopback request: the target's own. `ws:` and
 * `http:` share an origin, which is what the gateway's guard compares
 * against. Null for a URL that does not parse; the headers are then left
 * alone.
 */
export function gatewayOriginFor(url: string): string | null {
  try {
    const target = new URL(url)
    const scheme = target.protocol === 'ws:' ? 'http:' : target.protocol
    return `${scheme}//${target.host}`
  } catch {
    return null
  }
}

/**
 * The origin Chromium checks a CORS response against: the renderer's real
 * one, not the one presented to the gateway. A renderer loaded from disk has
 * an opaque origin, which fetch sends — and expects back — as the literal
 * `null`; the dev server's origin is used as-is.
 */
export function rendererCorsOrigin(appOrigin: string): string {
  return appOrigin.startsWith('file:') ? 'null' : appOrigin
}

type HeaderMap = Record<string, string | string[]>

const ALLOW_ORIGIN = 'access-control-allow-origin'

/**
 * Restate the gateway's CORS answer for the renderer. The gateway reflects
 * the Origin it was shown (the desktop presents the gateway's own, see
 * `installLoopbackCors`), so `Access-Control-Allow-Origin` names the gateway
 * itself and never equals the renderer's origin. Only a response that
 * carries the header is touched: the server said yes to the origin it saw,
 * and that yes is repeated for the origin Chromium will compare. A loopback
 * server that never opted into CORS stays unreadable, as before. Header
 * names are matched case-insensitively (the gateway sends them lowercased).
 */
export function withRendererOrigin(headers: HeaderMap, rendererOrigin: string): HeaderMap {
  const name = Object.keys(headers).find((key) => key.toLowerCase() === ALLOW_ORIGIN)
  if (name === undefined) return headers
  const out: HeaderMap = {}
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() !== ALLOW_ORIGIN) out[key] = value
  }
  out[name] = rendererOrigin
  return out
}

/**
 * The gateway's WebSocket guard admits loopback Origins or no Origin at all.
 * A renderer loaded from disk sends `Origin: file://` (and `null` for fetch),
 * which it rejects with close code 1008 — and its HTTP guard answers 403.
 * Present the gateway's own origin on every loopback request instead: the
 * renderer is the local operator, the same trust the browser console gets
 * when the gateway serves it.
 *
 * The response side has to match. Chromium checks a fetch's response against
 * the renderer's real origin, and the gateway reflects the Origin it was
 * shown, so the answer named the gateway itself and every fetch from the
 * renderer — bootstrap, approvals, uploads, artifact downloads — failed as a
 * CORS error. Rewrite the reflected origin back to the renderer's.
 */
export function installLoopbackCors(appOrigin: string): void {
  const rendererOrigin = rendererCorsOrigin(appOrigin)
  const filter = { urls: LOOPBACK_URLS }
  session.defaultSession.webRequest.onBeforeSendHeaders(filter, (details, callback) => {
    const requestHeaders = { ...details.requestHeaders }
    const origin = gatewayOriginFor(details.url)
    if (origin) requestHeaders.Origin = origin
    callback({ requestHeaders })
  })
  session.defaultSession.webRequest.onHeadersReceived(filter, (details, callback) => {
    callback({
      responseHeaders: withRendererOrigin(details.responseHeaders ?? {}, rendererOrigin),
    })
  })
}
