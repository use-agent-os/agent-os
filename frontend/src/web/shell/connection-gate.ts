import { useEffect } from 'react'
import { create } from 'zustand'
import { useBootstrap } from '@/app/providers'
import { readStoredToken } from '@/lib/credentials'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useConnection } from '@/stores/connection'

export type ConnectReason = 'token_required' | 'auth_failed' | 'auth_rate_limited' | 'connecting'

/** Set while a Connect-screen attempt is in flight, so the workspace never flashes. */
export const useConnectAttempt = create<{ active: boolean; set(active: boolean): void }>((set) => ({
  active: false,
  set: (active) => set({ active }),
}))

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
