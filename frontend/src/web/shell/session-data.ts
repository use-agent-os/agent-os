import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { create } from 'zustand'
import { useRpc } from '@/app/providers'
import type { WsRpcClient } from '@/lib/ws-rpc'
import { projectId, projectName, type RawProject } from '@/views/projects/logic'
import { SESSIONS_LIST_LIMIT } from '@/views/sessions/logic'
import type { SessionRow } from './session-list'

// The live Sessions list (D6, shell spec "Live session list"): one React Query
// cache filled by sessions.list, invalidated by `sessions.changed`, refetched
// every 5 s while the tab is visible, plus provisional rows for sessions this
// tab is creating.

export const SESSIONS_QUERY_KEY = ['sessions', 'list'] as const
export const SESSIONS_REFRESH_MS = 5_000

interface SessionsListResponse {
  sessions?: SessionRow[]
}

export async function fetchSessions(rpc: WsRpcClient): Promise<SessionRow[]> {
  await rpc.waitForConnection()
  const data = await rpc.call<SessionsListResponse>('sessions.list', { limit: SESSIONS_LIST_LIMIT })
  return (data?.sessions ?? []).filter((row): row is SessionRow => typeof row?.key === 'string')
}

function subscribeVisibility(onChange: () => void): () => void {
  document.addEventListener('visibilitychange', onChange)
  return () => document.removeEventListener('visibilitychange', onChange)
}

export function useDocumentVisible(): boolean {
  return useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState !== 'hidden',
    () => true,
  )
}

interface ProvisionalState {
  rows: SessionRow[]
  add(row: SessionRow): void
  remove(key: string): void
}

export const useProvisionalSessions = create<ProvisionalState>((set) => ({
  rows: [],
  add: (row) => set((s) => ({ rows: [...s.rows.filter((r) => r.key !== row.key), row] })),
  remove: (key) => set((s) => ({ rows: s.rows.filter((r) => r.key !== key) })),
}))

export function useSessionsList() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const visible = useDocumentVisible()
  const query = useQuery({
    queryKey: SESSIONS_QUERY_KEY,
    queryFn: () => fetchSessions(rpc),
    refetchInterval: visible ? SESSIONS_REFRESH_MS : false,
    refetchIntervalInBackground: false,
  })

  useEffect(
    () =>
      rpc.on('sessions.changed', () => {
        void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
      }),
    [rpc, queryClient],
  )

  const provisional = useProvisionalSessions((s) => s.rows)
  const rows = useMemo(() => {
    const listed = query.data ?? []
    const known = new Set(listed.map((r) => r.key))
    return [...provisional.filter((r) => !known.has(r.key)), ...listed]
  }, [query.data, provisional])

  return { ...query, rows }
}

export interface CreateSessionParams {
  agentId: string
  projectId?: string | null
  displayName?: string
}

let provisionalSeq = 0

/**
 * Create a session with an immediate provisional row. The row is replaced by
 * the listed one on success and removed if creation fails.
 */
export async function createSessionOptimistic(
  rpc: WsRpcClient,
  queryClient: QueryClient,
  params: CreateSessionParams,
): Promise<string> {
  const provisionalKey = `agent:${params.agentId}:webchat:~provisional-${++provisionalSeq}`
  const store = useProvisionalSessions.getState()
  store.add({
    key: provisionalKey,
    agent_id: params.agentId,
    project_id: params.projectId ?? null,
    display_name: params.displayName,
    updated_at: Date.now(),
    provisional: true,
  })
  try {
    const result = await rpc.call<{ key?: string }>('sessions.create', {
      agentId: params.agentId,
      ...(params.projectId ? { projectId: params.projectId } : {}),
      ...(params.displayName ? { displayName: params.displayName } : {}),
    })
    if (!result?.key) throw new Error('sessions.create returned no key')
    await queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
    return result.key
  } finally {
    useProvisionalSessions.getState().remove(provisionalKey)
  }
}

interface AgentsListResponse {
  agents?: { id?: string; name?: string; isBuiltin?: boolean }[]
  default?: string
  defaultAgent?: string
}

export interface AgentOption {
  id: string
  name: string
}

export const AGENTS_QUERY_KEY = ['agents', 'list'] as const

export function useAgents() {
  const rpc = useRpc()
  return useQuery({
    queryKey: AGENTS_QUERY_KEY,
    queryFn: async (): Promise<AgentOption[]> => {
      await rpc.waitForConnection()
      const data = await rpc.call<AgentsListResponse>('agents.list', {})
      return (data?.agents ?? [])
        .filter((a): a is { id: string; name?: string } => typeof a?.id === 'string' && !!a.id)
        .map((a) => ({ id: a.id, name: a.name || a.id }))
    },
    staleTime: 30_000,
  })
}

export const PROJECTS_QUERY_KEY = ['projects', 'list'] as const

export function useProjectNames() {
  const rpc = useRpc()
  return useQuery({
    queryKey: PROJECTS_QUERY_KEY,
    queryFn: async (): Promise<Map<string, string>> => {
      await rpc.waitForConnection()
      const data = await rpc.call<{ projects?: RawProject[] }>('projects.list', {})
      return new Map(
        (data?.projects ?? [])
          .filter((p) => projectId(p))
          .map((p) => [projectId(p), projectName(p)]),
      )
    },
    staleTime: 30_000,
  })
}
