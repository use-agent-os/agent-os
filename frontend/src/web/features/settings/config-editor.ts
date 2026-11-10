import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { create } from 'zustand'
import { useRpc } from '@/app/providers'
import { configValueAt, type ConfigData } from '@/views/config/logic'
import { SETTINGS_SNAPSHOT_QUERY_KEY } from '@/views/settings/snapshot'
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
}

/**
 * The restart notice. What needs a restart comes from the configuration
 * snapshot (`pendingRestart` / `restartReasons`), so the notice also shows a
 * restart another client caused and survives a reload; a write that answers
 * `restartRequired` only raises it for a gateway too old to report it there.
 * "Later" hides it until the set of reasons changes.
 */
export const useRestartNotice = create<{
  requested: boolean
  dismissed: string | null
  request(): void
  dismiss(signature: string): void
  clear(): void
}>((set) => ({
  requested: false,
  dismissed: null,
  request: () => set({ requested: true, dismissed: null }),
  dismiss: (signature) => set({ requested: false, dismissed: signature }),
  clear: () => set({ requested: false, dismissed: null }),
}))

/** After any configuration write: raise the restart notice if asked, refetch both views. */
export async function afterConfigWrite(
  queryClient: QueryClient,
  result: { restartRequired?: boolean } | null | undefined,
): Promise<void> {
  if (result?.restartRequired) useRestartNotice.getState().request()
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: CONFIG_SNAPSHOT_KEY }),
    queryClient.invalidateQueries({ queryKey: SETTINGS_SNAPSHOT_QUERY_KEY }),
  ])
}

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
  /** The revision the edits are based on: pinned at the first edit, else the loaded one. */
  revision: string | null
  value(path: string): unknown
  set(path: string, value: unknown): void
  /** A field holding text that is not a valid value yet (JSON): blocks Save. */
  setInvalid(path: string, invalid: boolean): void
  invalid: string[]
  changes: PendingChange[]
  /** Bumped whenever the edits are dropped or saved, so fields drop local drafts. */
  generation: number
  discard(): void
  save(): Promise<boolean>
  /** Load the newest configuration and drop the edits. */
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
  const [invalidPaths, setInvalidPaths] = useState<string[]>([])
  const [state, setState] = useState<SaveState>({ kind: 'idle' })
  const [generation, setGeneration] = useState(0)
  // The revision the first edit was made against. A refetch that moves the
  // snapshot under unsaved edits is a conflict, not a silent rebase.
  const [pinned, setPinned] = useState<string | null>(null)
  const loadedRevision = snapshot.data?.revision ?? null
  const loadedRef = useRef(loadedRevision)
  useEffect(() => {
    loadedRef.current = loadedRevision
  }, [loadedRevision])
  const config = useMemo(() => snapshot.data?.config ?? {}, [snapshot.data])

  const changes = useMemo(
    () =>
      Object.entries(edits)
        .map(([path, next]) => ({ path, old: configValueAt(config, path), next }))
        .filter((c) => !same(c.old, c.next)),
    [edits, config],
  )
  const dirty = changes.length > 0 || invalidPaths.length > 0

  const set = useCallback((path: string, value: unknown) => {
    setPinned((p) => p ?? loadedRef.current)
    setEdits((prev) => ({ ...prev, [path]: value }))
    setState((s) => (s.kind === 'invalid' || s.kind === 'error' ? { kind: 'idle' } : s))
  }, [])

  const setInvalid = useCallback((path: string, invalid: boolean) => {
    if (invalid) setPinned((p) => p ?? loadedRef.current)
    setInvalidPaths((prev) => {
      const has = prev.includes(path)
      if (invalid === has) return prev
      return invalid ? [...prev, path] : prev.filter((p) => p !== path)
    })
  }, [])

  const value = useCallback(
    (path: string) => (path in edits ? edits[path] : configValueAt(config, path)),
    [edits, config],
  )

  const reset = useCallback(() => {
    setEdits({})
    setInvalidPaths([])
    setPinned(null)
    setState({ kind: 'idle' })
    setGeneration((g) => g + 1)
  }, [])

  const write = useCallback(
    async (revision: string | null | undefined, patches: Record<string, unknown>) => {
      setState({ kind: 'saving' })
      try {
        const result = await rpc.call<SaveResult>('config.patch', {
          patches,
          ...(revision ? { expectedRevision: revision } : {}),
        })
        reset()
        await afterConfigWrite(queryClient, result)
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
    [rpc, queryClient, reset],
  )

  const patches = useCallback(
    () => Object.fromEntries(changes.map((c) => [c.path, c.next])),
    [changes],
  )

  const base = pinned ?? loadedRevision
  const moved = dirty && pinned !== null && loadedRevision !== null && loadedRevision !== pinned

  const save = useCallback(async () => {
    if (invalidPaths.length > 0) return false
    if (changes.length === 0) return true
    return write(base, patches())
  }, [invalidPaths.length, changes.length, write, base, patches])

  const reload = useCallback(async () => {
    reset()
    await snapshot.refetch()
  }, [snapshot, reset])

  const overwrite = useCallback(async () => {
    if (invalidPaths.length > 0) return false
    const pending = patches()
    const fresh = await snapshot.refetch()
    return write(fresh.data?.revision, pending)
  }, [invalidPaths.length, patches, snapshot, write])

  return {
    ready: snapshot.isSuccess,
    config,
    revision: base,
    value,
    set,
    setInvalid,
    invalid: invalidPaths,
    changes,
    generation,
    discard: reset,
    save,
    reload,
    overwrite,
    state: moved && state.kind === 'idle' ? { kind: 'conflict' } : state,
    writeBlocked:
      Boolean(snapshot.data?.writeBlocked) ||
      (state.kind === 'error' && isWriteBlocked(state.message)),
  }
}
