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
 * Resolve the WS URL to connect with, given the server-computed bootstrap
 * ws_url. Legacy (app.js:186-203) never used a server value — _autoConnect
 * always derived the scheme from location.protocol via getDefaultRpcUrl, so
 * an https page always produced wss://. The new console prefers the
 * server-computed ws_url, which is correct behind a well-configured proxy but
 * regresses when a TLS-terminating proxy omits x-forwarded-proto: the server
 * then sees plain http and emits ws:// for a page served over https.
 *
 * That downgrade is a mixed-content connection the browser blocks outright, so
 * we restore the legacy contract narrowly: when the page is https and the
 * server's ws_url is a same-host ws:// downgrade, prefer the location-derived
 * wss:// default. Any other ws_url — a different host, an already-wss URL, or
 * a page that is not https — passes through untouched.
 */
export function resolveWsUrl(bootstrapWsUrl: string): string {
  if (location.protocol !== 'https:') return bootstrapWsUrl
  let parsed: URL
  try {
    parsed = new URL(bootstrapWsUrl, location.href)
  } catch {
    return bootstrapWsUrl
  }
  if (parsed.protocol === 'ws:' && parsed.host === location.host) {
    return defaultWsUrl()
  }
  return bootstrapWsUrl
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
