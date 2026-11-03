import { session } from 'electron'

/**
 * Every http(s)/ws(s) request the renderer makes passes through the hooks;
 * `isGatewayRequest` then picks out the ones bound for the gateway. The
 * configured endpoint can change while the app runs (Settings › Gateway), and
 * Electron's URL filter is fixed at install time, so the match happens per
 * request instead.
 */
export const GATEWAY_URL_PATTERNS = ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*']

/**
 * The origin to present on a gateway request: the target's own. `ws:` and
 * `http:` share an origin (as do `wss:` and `https:`), which is what the
 * gateway's guard compares against. Null for a URL that does not parse; the
 * headers are then left alone.
 */
export function gatewayOriginFor(url: string): string | null {
  try {
    const target = new URL(url)
    const scheme =
      target.protocol === 'ws:' ? 'http:' : target.protocol === 'wss:' ? 'https:' : target.protocol
    // Re-parse under the folded scheme so a default port (`:443` on wss) is
    // dropped the way a browser's Origin header drops it.
    return new URL(`${scheme}//${target.host}`).origin
  } catch {
    return null
  }
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === 'localhost' || hostname === '[::1]' || hostname.startsWith('127.')
}

/**
 * Whether a request is for the gateway: any loopback host on any port (a
 * managed or adopted gateway, whichever port it ended up on), or the origin
 * the settings currently name (a gateway on another machine). Anything else
 * the renderer fetches — token logos, chart data — is left as it is.
 */
export function isGatewayRequest(url: string, configuredOrigin: string | null): boolean {
  let hostname: string
  try {
    hostname = new URL(url).hostname
  } catch {
    return false
  }
  if (isLoopbackHost(hostname)) return true
  return configuredOrigin !== null && gatewayOriginFor(url) === configuredOrigin
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
 * `installGatewayCors`), so `Access-Control-Allow-Origin` names the gateway
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
 * The gateway's WebSocket guard admits its own Origin (loopback, or the
 * origin it is published under) or no Origin at all. A renderer loaded from
 * disk sends `Origin: file://` (and `null` for fetch), which it rejects with
 * close code 1008 — and its HTTP guard answers 403. Present the gateway's own
 * origin on every gateway request instead: the renderer is the operator, the
 * same trust the browser console gets when the gateway serves it. For a
 * gateway on another host that origin is whatever `[gateway] public_origin`
 * or `control_ui.allowed_origins` declares there.
 *
 * The response side has to match. Chromium checks a fetch's response against
 * the renderer's real origin, and the gateway reflects the Origin it was
 * shown, so the answer named the gateway itself and every fetch from the
 * renderer — bootstrap, approvals, uploads, artifact downloads — failed as a
 * CORS error. Rewrite the reflected origin back to the renderer's.
 *
 * `configuredOrigin` is read on every request so a changed endpoint takes
 * effect without reinstalling the hooks.
 */
export function installGatewayCors(appOrigin: string, configuredOrigin: () => string | null): void {
  const rendererOrigin = rendererCorsOrigin(appOrigin)
  const filter = { urls: GATEWAY_URL_PATTERNS }
  session.defaultSession.webRequest.onBeforeSendHeaders(filter, (details, callback) => {
    if (!isGatewayRequest(details.url, configuredOrigin())) {
      callback({ requestHeaders: details.requestHeaders })
      return
    }
    const requestHeaders = { ...details.requestHeaders }
    const origin = gatewayOriginFor(details.url)
    if (origin) requestHeaders.Origin = origin
    callback({ requestHeaders })
  })
  session.defaultSession.webRequest.onHeadersReceived(filter, (details, callback) => {
    const responseHeaders = details.responseHeaders ?? {}
    callback({
      responseHeaders: isGatewayRequest(details.url, configuredOrigin())
        ? withRendererOrigin(responseHeaders, rendererOrigin)
        : responseHeaders,
    })
  })
}
