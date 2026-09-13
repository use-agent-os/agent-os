/** Runtime view of the gateway process as seen by the desktop shell. */
export type GatewayState = 'stopped' | 'starting' | 'running' | 'stopping' | 'error'

export interface GatewayStatus {
  state: GatewayState
  /** Present while the app manages the process. */
  pid: number | null
  /** Base HTTP URL, e.g. http://127.0.0.1:18791. */
  url: string | null
  /** Last error message, if `state === 'error'`. */
  error: string | null
}

export const STOPPED_GATEWAY: GatewayStatus = {
  state: 'stopped',
  pid: null,
  url: null,
  error: null,
}
