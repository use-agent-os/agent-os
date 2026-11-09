// The one owner of the browser's gateway credentials (overhaul-control-ui D10,
// connection spec "Credential handling in the browser").
//
// The gateway token lives in sessionStorage under `agentos.wsToken`: one tab,
// one origin, gone when the tab closes. A link may carry it once as a `token`
// query or fragment parameter; `consumeUrlCredentials()` takes it out of the
// address bar (history replacement, so Back never shows it) before the router
// or anything else reads the URL.

export const WS_TOKEN_KEY = 'agentos.wsToken'
/** The persisted WebSocket URL override (not a secret). */
export const WS_URL_KEY = 'agentos.wsUrl'

/** Read the per-tab gateway token; blocked storage reads as no token. */
export function readStoredToken(): string {
  try {
    return sessionStorage.getItem(WS_TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

/** Store (or, with an empty value, forget) the per-tab gateway token. */
export function storeToken(token: string | null | undefined): void {
  try {
    if (token) sessionStorage.setItem(WS_TOKEN_KEY, token)
    else sessionStorage.removeItem(WS_TOKEN_KEY)
  } catch {
    /* storage unavailable: the token still applies to this connection */
  }
}

export function readStoredWsUrl(): string {
  try {
    return localStorage.getItem(WS_URL_KEY) || ''
  } catch {
    return ''
  }
}

export function storeWsUrl(url: string | null | undefined): void {
  try {
    if (url) localStorage.setItem(WS_URL_KEY, url)
    else localStorage.removeItem(WS_URL_KEY)
  } catch {
    /* storage unavailable */
  }
}

export interface UrlCredentials {
  /** A token taken from the link (already stored for this tab). */
  token: string | null
  /** A gateway URL from the link, held until the user confirms it. */
  gatewayUrl: string | null
}

const CREDENTIAL_PARAMS = ['token', 'gatewayUrl'] as const

function stripParams(params: URLSearchParams, found: Record<string, string>): boolean {
  let changed = false
  for (const name of CREDENTIAL_PARAMS) {
    const value = params.get(name)
    if (value === null) continue
    if (value && !found[name]) found[name] = value
    params.delete(name)
    changed = true
  }
  return changed
}

let pendingGatewayUrl: string | null = null

/**
 * Take `token` / `gatewayUrl` out of the current URL (query and fragment),
 * store the token for this tab, hold the gateway URL for confirmation, and
 * replace the history entry so neither is visible any more.
 */
export function consumeUrlCredentials(
  location: Pick<Location, 'href'> = window.location,
  history: Pick<History, 'replaceState' | 'state'> = window.history,
): UrlCredentials {
  const url = new URL(location.href)
  const found: Record<string, string> = {}
  const query = new URLSearchParams(url.search)
  const queryChanged = stripParams(query, found)
  const fragment = new URLSearchParams(url.hash.replace(/^#/, ''))
  const hashChanged = url.hash.includes('=') && stripParams(fragment, found)

  if (queryChanged || hashChanged) {
    const search = query.toString()
    const hash = hashChanged ? fragment.toString() : url.hash.replace(/^#/, '')
    const clean = `${url.pathname}${search ? `?${search}` : ''}${hash ? `#${hash}` : ''}`
    history.replaceState(history.state, '', clean)
  }

  const token = found.token ?? null
  if (token) storeToken(token)
  const gatewayUrl = found.gatewayUrl ?? null
  if (gatewayUrl) pendingGatewayUrl = gatewayUrl
  return { token, gatewayUrl }
}

/** The gateway URL a link asked for, awaiting the user's confirmation. */
export function pendingGatewayUrlRequest(): string | null {
  return pendingGatewayUrl
}

export function clearPendingGatewayUrl(): void {
  pendingGatewayUrl = null
}
