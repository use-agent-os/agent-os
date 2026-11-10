import { isGatewayUrl } from './api-origin'
import { readStoredToken } from './credentials'

/** Read the per-tab gateway token without letting blocked storage break a request. */
export function sessionAuthToken(): string {
  return readStoredToken()
}

/**
 * Headers for a REST request to `url`, with the tab's gateway token when one
 * is set. The token belongs to the gateway the console is connected to, so a
 * request to any other origin never carries it.
 */
export function authenticatedHeaders(
  url: string,
  extra: Record<string, string> = {},
): Record<string, string> {
  const headers = { ...extra }
  const token = sessionAuthToken()
  if (token && isGatewayUrl(url)) headers.Authorization = `Bearer ${token}`
  return headers
}
