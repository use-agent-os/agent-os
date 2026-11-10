/**
 * Where the gateway API lives, for hosts that are NOT served by the gateway.
 *
 * In the browser console the page is served by the gateway itself, so every
 * REST/WS URL is same-origin and relative (`/api/...`, `ws://host/ws`). The
 * desktop app loads its renderer from disk instead, and points this module at
 * the gateway it manages by setting `window.__AGENTOS_ENV__` before React
 * mounts. Everything that builds a gateway URL goes through here so the two
 * hosts share one code path.
 */
export interface AgentosHostEnv {
  /** Absolute origin of the gateway, e.g. `http://127.0.0.1:18791`. */
  apiOrigin?: string
  /** Control UI mount path on the gateway, e.g. `/control`. */
  controlBase?: string
}

declare global {
  interface Window {
    __AGENTOS_ENV__?: AgentosHostEnv
  }
}

function env(): AgentosHostEnv {
  return (typeof window !== 'undefined' && window.__AGENTOS_ENV__) || {}
}

/** Gateway origin when hosted off-gateway; empty string in the browser console. */
export function apiOrigin(): string {
  return (env().apiOrigin || '').replace(/\/+$/, '')
}

/** Something that knows the WebSocket URL it connects to (`WsRpcClient`). */
export interface GatewaySocket {
  readonly currentUrl: string
}

let boundSocket: GatewaySocket | null = null

/**
 * Point the HTTP helpers at the gateway `socket` connects to (the web
 * console's client). After a switch to another gateway, its REST calls
 * (approvals, uploads, artifacts) must go to that gateway, never to the one
 * that served the page: the tab's token belongs to the socket's gateway.
 */
export function bindGatewaySocket(socket: GatewaySocket | null): () => void {
  boundSocket = socket
  return () => {
    if (boundSocket === socket) boundSocket = null
  }
}

function pageOrigin(): string {
  return typeof window !== 'undefined' ? window.location.origin : ''
}

/**
 * The HTTP origin of the gateway behind a WebSocket URL (`ws://h:p/ws` ->
 * `http://h:p`); '' when it is the page's own origin (or the URL is unusable),
 * so same-origin URLs stay relative.
 */
export function socketHttpOrigin(wsUrl: string, page: string = pageOrigin()): string {
  if (!wsUrl) return ''
  try {
    const url = new URL(wsUrl, page || undefined)
    const scheme =
      url.protocol === 'wss:' || url.protocol === 'https:'
        ? 'https:'
        : url.protocol === 'ws:' || url.protocol === 'http:'
          ? 'http:'
          : ''
    if (!scheme) return ''
    const origin = `${scheme}//${url.host}`
    return origin === page ? '' : origin
  } catch {
    return ''
  }
}

/**
 * The origin every gateway HTTP call goes to: the host's gateway off-gateway
 * (desktop), else the gateway the console's socket is connected to when that
 * is not the page's origin, else '' (relative, same origin).
 */
export function gatewayOrigin(): string {
  return apiOrigin() || socketHttpOrigin(boundSocket?.currentUrl ?? '')
}

/** Absolute URL for a gateway-rooted path (`/api/...`). Relative when on-gateway. */
export function apiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`
  return `${gatewayOrigin()}${suffix}`
}

/** Explicit control mount path for off-gateway hosts, else null. */
export function hostControlBase(): string | null {
  const base = env().controlBase
  return typeof base === 'string' && base.trim() ? base : null
}

/**
 * Base used to resolve relative gateway URLs with `new URL(raw, base)`. Off
 * gateway this must be the gateway origin, never the document origin
 * (`file://` on the desktop, which the URL parser cannot resolve against).
 */
export function urlBase(): string {
  return gatewayOrigin() || pageOrigin() || 'http://localhost'
}

/**
 * Whether `url` goes to the gateway this tab is signed in to: the only place
 * its token may be sent.
 */
export function isGatewayUrl(url: string): boolean {
  try {
    // Resolved as fetch() resolves it: a relative URL goes to the page.
    const page = typeof window !== 'undefined' ? window.location.href : undefined
    return new URL(url, page).origin === new URL(urlBase()).origin
  } catch {
    return false
  }
}
