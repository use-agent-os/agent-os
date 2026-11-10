import { useEffect } from 'react'
import { create } from 'zustand'
import { useBootstrap } from '@/app/providers'
import { readStoredToken, storeToken, storeWsUrl } from '@/lib/credentials'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import type { WsRpcClient } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'

export type ConnectReason = 'token_required' | 'auth_failed' | 'auth_rate_limited' | 'connecting'

/** How long an attempt may go without a handshake before the UI calls the gateway unreachable. */
export const CONNECT_TIMEOUT_MS = 10_000

interface ConnectAttemptState {
  /** Set while a Connect-screen attempt is in flight, so the workspace never flashes. */
  active: boolean
  /** The WebSocket URL the console is trying to reach (null when not switching). */
  url: string | null
  /** No handshake within CONNECT_TIMEOUT_MS: the gateway could not be reached. */
  unreachable: boolean
  set(active: boolean): void
}

export const useConnectAttempt = create<ConnectAttemptState>((set) => ({
  active: false,
  url: null,
  unreachable: false,
  set: (active) => set({ active }),
}))

let cancelPendingAttempt: (() => void) | null = null

/** Forget an attempt's bookkeeping (tests, and a newer attempt replacing it). */
export function cancelConnectAttempt(): void {
  cancelPendingAttempt?.()
  cancelPendingAttempt = null
  useConnectAttempt.setState({ active: false, url: null, unreachable: false })
}

/**
 * Connect this tab to `url` with `token` (connection spec "Connect screen").
 *
 * The token is stored for the tab at once: it is what the user just gave for
 * this gateway, and an empty one forgets the previous gateway's token. The URL
 * is only persisted once the gateway completes a handshake, so a mistyped or
 * unreachable address never outlives a reload. `holdScreen` keeps the Connect
 * screen up for the attempt instead of the workspace.
 */
export function connectGateway(
  rpc: Pick<WsRpcClient, 'connect' | 'on'>,
  opts: { url: string; token: string | null; defaultUrl: string; holdScreen: boolean },
): void {
  const { url, token, defaultUrl, holdScreen } = opts
  cancelPendingAttempt?.()
  storeToken(token)
  useRuntimeSignals.setState({ handshake: null })
  useConnectAttempt.setState({ active: holdScreen, url, unreachable: false })
  const off = rpc.on('_hello', () => {
    storeWsUrl(url === defaultUrl ? null : url)
    cancelConnectAttempt()
  })
  const timer = setTimeout(() => {
    useConnectAttempt.setState({ unreachable: true })
  }, CONNECT_TIMEOUT_MS)
  // A refusal is an answer: the gateway was reached, the screen says why.
  const offRefused = rpc.on('_handshakeError', () => clearTimeout(timer))
  // A socket that fails before it opens (connection refused) needs no wait.
  const offUnreachable = rpc.on('_unreachable', () => {
    clearTimeout(timer)
    useConnectAttempt.setState({ unreachable: true })
  })
  cancelPendingAttempt = () => {
    off()
    offRefused()
    offUnreachable()
    clearTimeout(timer)
  }
  rpc.connect(url, token || undefined)
}

/**
 * Whether the Connect screen replaces the workspace (connection spec "Connect
 * screen"): the handshake was refused for credentials, or the gateway wants a
 * token and this tab has none. A token the user just submitted keeps the
 * screen up until the handshake succeeds or is refused.
 */
export function useConnectGate(): ConnectReason | null {
  const bootstrap = useBootstrap()
  const handshake = useRuntimeSignals((s) => s.handshake)
  const state = useConnection((s) => s.state)
  const attempt = useConnectAttempt((s) => s.active)
  const clearAttempt = useConnectAttempt((s) => s.set)

  useEffect(() => {
    if (state === 'connected') clearAttempt(false)
  }, [state, clearAttempt])

  if (state === 'connected') return null
  if (handshake?.kind === 'auth_rate_limited') return 'auth_rate_limited'
  if (bootstrap.auth_mode === 'token' && !readStoredToken()) return 'token_required'
  if (handshake?.kind === 'auth_failed') return 'auth_failed'
  if (attempt) return 'connecting'
  return null
}
