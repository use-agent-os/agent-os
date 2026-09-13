import { create } from 'zustand'
import type { WsRpcClient } from '@/lib/ws-rpc'
import { useApprovals } from '@/services/approval-monitor'
import { derivePetState, type PetState } from '@shared/pet'
import { useGateway } from './gateway'
import { useLive } from './live'

/** How long a one-shot beat (wave, jump, failed) holds before activity resumes. */
export const PET_BEAT_MS = 3200

interface PetStore {
  /** A transient beat that outranks the steady signals until it expires. */
  beat: { state: 'wave' | 'jump' | 'failed'; until: number } | null
  /** Live, derived on every change of the signals below. */
  state: PetState
  poke(): void
  recompute(): void
}

let beatTimer: ReturnType<typeof setTimeout> | null = null

/**
 * The pet's activity state, Hermes' priority order over the signals the
 * app already tracks: a failed/finished task event (beat), approvals
 * waiting, a turn streaming (useLive), the gateway down (failed).
 */
export const usePet = create<PetStore>((set, get) => {
  const compute = (): PetState => {
    const now = Date.now()
    const beat = get().beat && get().beat!.until > now ? get().beat : null
    const busy = useLive.getState().ids.size > 0
    const awaiting = useApprovals.getState().pending.length > 0
    const gatewayDown = useGateway.getState().status.state === 'error'
    return derivePetState({
      error: beat?.state === 'failed' || gatewayDown,
      celebrate: beat?.state === 'jump',
      justCompleted: beat?.state === 'wave',
      awaitingInput: awaiting,
      busy,
    })
  }
  return {
    beat: null,
    state: 'idle',
    poke() {
      setBeat('wave')
    },
    recompute() {
      const state = compute()
      if (state !== get().state) set({ state })
    },
  }
})

function setBeat(state: 'wave' | 'jump' | 'failed'): void {
  usePet.setState({ beat: { state, until: Date.now() + PET_BEAT_MS } })
  usePet.getState().recompute()
  if (beatTimer) clearTimeout(beatTimer)
  beatTimer = setTimeout(() => {
    usePet.setState({ beat: null })
    usePet.getState().recompute()
  }, PET_BEAT_MS)
}

/**
 * Subscribe once from the app root. Task terminals come from the gateway
 * event stream; the steady signals are other stores.
 */
export function bindPetSignals(rpc: WsRpcClient): () => void {
  const recompute = () => usePet.getState().recompute()
  const offs = [
    useLive.subscribe(recompute),
    useApprovals.subscribe(recompute),
    useGateway.subscribe(recompute),
    rpc.on('task.succeeded', () => setBeat('wave')),
    rpc.on('task.failed', () => setBeat('failed')),
    rpc.on('task.timeout', () => setBeat('failed')),
  ]
  recompute()
  return () => offs.forEach((off) => off())
}
