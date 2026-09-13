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

/** Absolute URL for a gateway-rooted path (`/api/...`). Relative when on-gateway. */
export function apiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`
  return `${apiOrigin()}${suffix}`
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
  return (
    apiOrigin() || (typeof window !== 'undefined' ? window.location.origin : 'http://localhost')
  )
}
