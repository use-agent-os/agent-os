// The one owner of the browser's gateway credentials (overhaul-control-ui D10,
// connection spec "Credential handling in the browser").
//
// The gateway token lives in sessionStorage under `agentos.wsToken`: one tab,
// one origin, gone when the tab closes. A link may carry it once as a `token`
// query or fragment parameter; `consumeUrlCredentials()` takes it out of the
// address bar (history replacement, so Back never shows it) before the router
// or anything else reads the URL.
//
// A token that came with a `gatewayUrl` in the same link belongs to that
// gateway: it is held with the URL until the user confirms the switch, and is
// never stored for (or sent to) the gateway this tab is already using.

export const WS_TOKEN_KEY = 'agentos.wsToken'
/** The persisted WebSocket URL override (not a secret). */
export const WS_URL_KEY = 'agentos.wsUrl'

// Where sessionStorage refuses a write (blocked storage, some private modes),
// the token is kept in memory for the life of the page instead, so a short
// connection drop does not send the user back to the Connect screen.
let memoryToken = ''

/** Read the per-tab gateway token; blocked storage falls back to the in-memory copy. */
export function readStoredToken(): string {
  try {
    return sessionStorage.getItem(WS_TOKEN_KEY) || memoryToken
  } catch {
    return memoryToken
  }
}

/** Store (or, with an empty value, forget) the per-tab gateway token. */
export function storeToken(token: string | null | undefined): void {
  try {
    if (token) sessionStorage.setItem(WS_TOKEN_KEY, token)
    else sessionStorage.removeItem(WS_TOKEN_KEY)
    memoryToken = ''
  } catch {
    memoryToken = token || ''
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
  /**
   * A token taken from the link. Stored for this tab when the link names no
   * gateway; held with the gateway URL until confirmation when it does.
   */
  token: string | null
  /** A gateway URL from the link, held until the user confirms it. */
  gatewayUrl: string | null
}

/** A gateway switch a link asked for, awaiting the user's confirmation. */
export interface GatewayUrlRequest {
  url: string
  /** The token the same link carried, meant for that gateway only. */
  token: string | null
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

let pendingRequest: GatewayUrlRequest | null = null

/**
 * Take `token` / `gatewayUrl` out of the current URL (query and fragment),
 * store the token for this tab (or, with a gateway URL, hold both for
 * confirmation), and replace the history entry so neither is visible any more.
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
  const gatewayUrl = found.gatewayUrl ?? null
  if (gatewayUrl) pendingRequest = { url: gatewayUrl, token }
  else if (token) storeToken(token)
  return { token, gatewayUrl }
}

/** The gateway switch a link asked for, awaiting the user's confirmation. */
export function pendingGatewayUrlRequest(): GatewayUrlRequest | null {
  return pendingRequest
}

export function clearPendingGatewayUrl(): void {
  pendingRequest = null
}
