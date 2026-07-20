export interface Bootstrap {
  version: string
  ws_url: string
  auth_mode: string
  base_path: string
  config_path: string
  features: { diagnostics: boolean }
}

/** BASE_URL is '/control/static/dist/'; the API lives at '/control/api/'. */
export function bootstrapUrl(): string {
  const base = import.meta.env.BASE_URL.replace(/static\/dist\/?$/, '')
  return `${base}api/bootstrap`
}

export async function fetchBootstrap(): Promise<Bootstrap> {
  const resp = await fetch(bootstrapUrl())
  if (!resp.ok) throw new Error(`bootstrap failed: ${resp.status}`)
  return (await resp.json()) as Bootstrap
}

/** app.js:192-195 — location-derived default RPC URL. */
export function defaultWsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
}

/**
 * Legacy inlined bootstrap data into the served HTML, so the shell could never
 * be blocked by a bootstrap failure: it always rendered, and _autoConnect
 * (app.js:186-203) connected with the location-derived default WS URL backed
 * by infinite reconnect backoff. When /api/bootstrap fails transiently, this
 * fallback keeps that contract instead of wedging on a placeholder.
 */
export function fallbackBootstrap(): Bootstrap {
  return {
    version: '',
    ws_url: defaultWsUrl(),
    auth_mode: '',
    base_path: import.meta.env.BASE_URL.replace(/static\/dist\/?$/, '').replace(/\/$/, '') || '/',
    config_path: '',
    features: { diagnostics: false },
  }
}
