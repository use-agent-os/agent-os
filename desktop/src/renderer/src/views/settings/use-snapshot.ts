import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'
import { useRpc } from '@/app/providers'
import { useConnection } from '@/stores/connection'
import { loadSettingsSnapshot, type SettingsSnapshot } from '@/views/settings/snapshot'

export const SNAPSHOT_KEY = ['settings', 'snapshot'] as const

/**
 * The gateway's configuration as one atomic read (`config.snapshot`): the
 * config, the onboarding catalog and status, and the revision every guided
 * write must carry so a stale form cannot overwrite a newer file. Shared by
 * the Models, Router and Advanced sections; a save invalidates it.
 */
export function useConfigSnapshot() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const connected = useConnection((s) => s.state === 'connected')
  const query = useQuery<SettingsSnapshot>({
    queryKey: SNAPSHOT_KEY,
    enabled: connected,
    refetchOnWindowFocus: false,
    queryFn: () => loadSettingsSnapshot(rpc),
  })
  const reload = useCallback(
    () => queryClient.invalidateQueries({ queryKey: SNAPSHOT_KEY }),
    [queryClient],
  )
  return { connected, query, snapshot: query.data, reload }
}

/** Attach the snapshot revision to a guided write, the way the console does. */
export function withRevision<T extends Record<string, unknown>>(
  snapshot: SettingsSnapshot | undefined,
  params: T,
): T & { expectedRevision?: string } {
  if (snapshot?.writeBlocked) {
    throw new Error('The gateway configuration changed outside AgentOS. Restart the gateway first.')
  }
  return snapshot?.revision ? { ...params, expectedRevision: snapshot.revision } : params
}
