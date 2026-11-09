import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useState } from 'react'
import { create } from 'zustand'
import { useRpc } from '@/app/providers'
import { configValueAt, type ConfigData } from '@/views/config/logic'
import { fieldErrors, isRevisionConflict, isWriteBlocked } from './config-errors'

// Explicit-save editing of gateway configuration (overhaul-control-ui D9,
// settings spec "Explicit save with pending-change review", "Save conflicts
// and validation", "Restart-required changes"). Saves send the revision the
// editor loaded; a conflict writes nothing and offers Reload or Overwrite.

export const CONFIG_SNAPSHOT_KEY = ['config', 'editor'] as const

export interface ConfigSnapshot {
  config: ConfigData
  revision?: string | null
  writeBlocked?: boolean
}

interface SaveResult {
  restartRequired?: boolean
  restartReasons?: string[]
  pendingRestart?: boolean
}

/** A restart the user has been told about and not acted on. */
export const useRestartNotice = create<{
  reasons: string[]
  show(reasons: string[]): void
  clear(): void
}>((set) => ({
  reasons: [],
  show: (reasons) => set({ reasons: reasons.length ? reasons : ['configuration'] }),
  clear: () => set({ reasons: [] }),
}))

export function useConfigSnapshot() {
  const rpc = useRpc()
  return useQuery({
    queryKey: CONFIG_SNAPSHOT_KEY,
    queryFn: async (): Promise<ConfigSnapshot> => {
      await rpc.waitForConnection()
      try {
        const snapshot = await rpc.call<ConfigSnapshot>('config.snapshot')
        if (snapshot && 'config' in snapshot) return snapshot
      } catch {
        /* older gateway: fall back below */
      }
      const config = await rpc.call<ConfigData>('config.get')
      return { config: config ?? {} }
    },
  })
}

export interface PendingChange {
  path: string
  old: unknown
  next: unknown
}

export type SaveState =
  | { kind: 'idle' }
  | { kind: 'saving' }
  | { kind: 'conflict' }
  | { kind: 'invalid'; fields: Record<string, string> }
  | { kind: 'error'; message: string }

export interface ConfigEditor {
  ready: boolean
  config: ConfigData
  value(path: string): unknown
  set(path: string, value: unknown): void
  changes: PendingChange[]
  discard(): void
  save(): Promise<boolean>
  /** After a conflict: load the newer configuration and drop the edits. */
  reload(): Promise<void>
  /** After a conflict: reapply the edits on the newer revision and save. */
  overwrite(): Promise<boolean>
  state: SaveState
  writeBlocked: boolean
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

export function useConfigEditor(): ConfigEditor {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const snapshot = useConfigSnapshot()
  const [edits, setEdits] = useState<Record<string, unknown>>({})
  const [state, setState] = useState<SaveState>({ kind: 'idle' })
  const config = useMemo(() => snapshot.data?.config ?? {}, [snapshot.data])

  const changes = useMemo(
    () =>
      Object.entries(edits)
        .map(([path, next]) => ({ path, old: configValueAt(config, path), next }))
        .filter((c) => !same(c.old, c.next)),
    [edits, config],
  )

  const set = useCallback((path: string, value: unknown) => {
    setEdits((prev) => ({ ...prev, [path]: value }))
    setState((s) => (s.kind === 'invalid' || s.kind === 'error' ? { kind: 'idle' } : s))
  }, [])

  const value = useCallback(
    (path: string) => (path in edits ? edits[path] : configValueAt(config, path)),
    [edits, config],
  )

  const write = useCallback(
    async (revision: string | null | undefined, patches: Record<string, unknown>) => {
      setState({ kind: 'saving' })
      try {
        const result = await rpc.call<SaveResult>('config.patch', {
          patches,
          ...(revision ? { expectedRevision: revision } : {}),
        })
        setEdits({})
        setState({ kind: 'idle' })
        if (result?.restartRequired) useRestartNotice.getState().show(result.restartReasons ?? [])
        await queryClient.invalidateQueries({ queryKey: CONFIG_SNAPSHOT_KEY })
        return true
      } catch (error) {
        if (isRevisionConflict(error)) setState({ kind: 'conflict' })
        else {
          const fields = fieldErrors(error)
          if (Object.keys(fields).length) setState({ kind: 'invalid', fields })
          else
            setState({
              kind: 'error',
              message: error instanceof Error ? error.message : String(error),
            })
        }
        return false
      }
    },
    [rpc, queryClient],
  )

  const patches = useCallback(
    () => Object.fromEntries(changes.map((c) => [c.path, c.next])),
    [changes],
  )

  const save = useCallback(async () => {
    if (changes.length === 0) return true
    return write(snapshot.data?.revision, patches())
  }, [changes.length, write, snapshot.data?.revision, patches])

  const reload = useCallback(async () => {
    setEdits({})
    setState({ kind: 'idle' })
    await snapshot.refetch()
  }, [snapshot])

  const overwrite = useCallback(async () => {
    const pending = patches()
    const fresh = await snapshot.refetch()
    return write(fresh.data?.revision, pending)
  }, [patches, snapshot, write])

  const discard = useCallback(() => {
    setEdits({})
    setState({ kind: 'idle' })
  }, [])

  return {
    ready: snapshot.isSuccess,
    config,
    value,
    set,
    changes,
    discard,
    save,
    reload,
    overwrite,
    state,
    writeBlocked:
      Boolean(snapshot.data?.writeBlocked) ||
      (state.kind === 'error' && isWriteBlocked(state.message)),
  }
}
