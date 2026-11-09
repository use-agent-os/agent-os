import { useEffect, useMemo, useRef, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useAttentionInbox } from '@/lib/useAttentionInbox'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { deriveRuntimeTruth, type RuntimeTruth } from '@/lib/runtime-truth'
import { useClock } from '@/lib/useRuntimeTruth'
import type { RpcState } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'

/** How long a lost connection may stay lost before the console says so. */
export const RECONNECT_GRACE_MS = 2_000

/**
 * The connection state to *display* (connection spec "Reconnect grace
 * period"). `lib/ws-rpc` keeps its own reconnect logic; this only delays the
 * transition away from Connected by 2 s, so a short blip never flashes
 * Offline or disables controls. Every other transition shows at once.
 */
export function useDisplayedConnection(graceMs: number = RECONNECT_GRACE_MS): RpcState {
  const [displayed, setDisplayed] = useState<RpcState>(() => useConnection.getState().state)
  const shown = useRef(displayed)

  useEffect(() => {
    let timer: number | undefined
    const show = (state: RpcState) => {
      shown.current = state
      setDisplayed(state)
    }
    const onChange = (next: RpcState) => {
      if (timer !== undefined) window.clearTimeout(timer)
      timer = undefined
      if (next !== 'connected' && shown.current === 'connected') {
        timer = window.setTimeout(() => show(useConnection.getState().state), graceMs)
        return
      }
      show(next)
    }
    const unsubscribe = useConnection.subscribe((s, prev) => {
      if (s.state !== prev.state) onChange(s.state)
    })
    return () => {
      unsubscribe()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [graceMs])

  return displayed
}

/** The runtime truth, fed with the displayed (grace-delayed) connection state. */
export function useDisplayedTruth(): RuntimeTruth {
  const rpcState = useDisplayedConnection()
  const signals = useRuntimeSignals(
    useShallow((s) => ({
      everConnected: s.everConnected,
      lastFrameAt: s.lastFrameAt,
      tickIntervalMs: s.tickIntervalMs,
      protocol: s.protocol,
      handshake: s.handshake,
      shutdown: s.shutdown,
      doctor: s.doctor,
      doctorError: s.doctorError,
      ready: s.ready,
    })),
  )
  const { count } = useAttentionInbox()
  const now = useClock(1000)
  return useMemo(
    () => deriveRuntimeTruth({ rpcState, signals, attentionCount: count }, now),
    [rpcState, signals, count, now],
  )
}
