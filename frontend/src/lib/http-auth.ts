const WS_TOKEN_KEY = 'agentos.wsToken'

/** Read the per-tab gateway token without letting blocked storage break a request. */
export function sessionAuthToken(): string {
  try {
    return sessionStorage.getItem(WS_TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

/** Add the gateway bearer token to a same-origin REST request when configured. */
export function authenticatedHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const headers = { ...extra }
  const token = sessionAuthToken()
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}
