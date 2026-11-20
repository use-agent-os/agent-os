import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { useConnection } from '@/stores/connection'
import { t } from '~/i18n'
import type { CuratedMemoryData, MemoryFileRow, MemoryStatusWire } from '@/views/memory/logic'
import { AGENT_ID, type RecallStatusWire } from './logic'

export const MEMORY_KEY = ['settings', 'memory'] as const

export function errorText(error: unknown): string {
  if (error instanceof Error) return error.message
  if (error && typeof error === 'object' && 'message' in error) {
    return String((error as { message: unknown }).message)
  }
  return String(error)
}

/**
 * A failed settings write as a toast. A stale revision (someone else wrote
 * the config since it was read) reloads the snapshot and asks for Save again;
 * the card's draft survives the reload.
 */
export async function reportSaveError(
  err: unknown,
  reload: () => Promise<void>,
  toastId: string,
): Promise<void> {
  if (/revision mismatch/i.test(errorText(err))) {
    toast.error(t('settings.memory.conflict'), { id: toastId })
    await reload()
  } else {
    toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, { id: toastId })
  }
}

export function useConnected(): boolean {
  return useConnection((s) => s.state === 'connected')
}

/**
 * `memory.index` without force is an incremental sync that also reports the
 * index's counts; the console reads the status the same way.
 */
export function useIndexStatus() {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery({
    queryKey: [...MEMORY_KEY, 'status'],
    enabled: connected,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<MemoryStatusWire>('memory.index', { agentId: AGENT_ID, force: false })
    },
  })
}

export function useFacts(target: 'memory' | 'user') {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery({
    queryKey: [...MEMORY_KEY, 'curated', target],
    enabled: connected,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<CuratedMemoryData>('memory.curated.get', { agentId: AGENT_ID, target })
    },
  })
}

export function useDocuments() {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery({
    queryKey: [...MEMORY_KEY, 'documents'],
    enabled: connected,
    queryFn: async () => {
      await rpc.waitForConnection()
      const res = await rpc.call<{ documents?: MemoryFileRow[] }>('memory.knowledge_base.list', {
        agentId: AGENT_ID,
      })
      return res?.documents ?? []
    },
  })
}

export function useSourceFiles() {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery({
    queryKey: [...MEMORY_KEY, 'files'],
    enabled: connected,
    queryFn: async () => {
      await rpc.waitForConnection()
      const res = await rpc.call<{ files?: MemoryFileRow[] }>('memory.list', {
        agentId: AGENT_ID,
        source: 'all',
      })
      return res?.files ?? []
    },
  })
}

export function useRecallStatus() {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery({
    queryKey: [...MEMORY_KEY, 'recall'],
    enabled: connected,
    refetchInterval: 30_000,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<RecallStatusWire>('memory.recall.status', { limit: 10 })
    },
  })
}

/**
 * Runs one memory write at a time: `busy` names it for the button that
 * started it, a failure becomes a toast, success refreshes every memory read.
 */
export function useMemoryAction() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)
  const run = useCallback(
    async <T>(key: string, work: () => Promise<T>): Promise<T | undefined> => {
      setBusy(key)
      try {
        const result = await work()
        await queryClient.invalidateQueries({ queryKey: MEMORY_KEY })
        return result
      } catch (error) {
        toast.error(errorText(error), { id: `stg-memory-${key}` })
        return undefined
      } finally {
        setBusy(null)
      }
    },
    [queryClient],
  )
  return { rpc, busy, run }
}

export type MemoryAction = ReturnType<typeof useMemoryAction>
