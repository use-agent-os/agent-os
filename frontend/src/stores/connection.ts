import { create } from 'zustand'
import type { RpcState } from '@/lib/ws-rpc'

export const useConnection = create<{ state: RpcState; setState(s: RpcState): void }>((set) => ({
  state: 'disconnected',
  setState: (state) => set({ state }),
}))
