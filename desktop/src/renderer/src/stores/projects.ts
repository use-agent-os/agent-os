import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { projectId, projectName, sortProjects, type RawProject } from '@/views/projects/logic'
import type { AgentEntry } from '@/views/sessions/logic'
import { useConnection } from '@/stores/connection'
import { t } from '~/i18n'

interface ProjectsList {
  projects?: RawProject[]
  count?: number
}

/** Events after which the project list is stale: CRUD, or a session moved. */
const INVALIDATING_EVENTS = ['projects.changed', 'sessions.changed', '_hello']

/**
 * The gateway's projects, newest-updated first. Same shape the web console
 * reads (projects.list), refetched on every projects/sessions event and on
 * reconnect. Both the sidebar folders and the project page read from here.
 */
export function useProjects(): {
  projects: RawProject[]
  byId: Map<string, RawProject>
  loading: boolean
  error: string | null
} {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const connected = useConnection((s) => s.state === 'connected')

  const query = useQuery<ProjectsList>({
    queryKey: ['projects'],
    enabled: connected,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<ProjectsList>('projects.list', {})
    },
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  })

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const invalidate = () => {
      if (timer) return
      timer = setTimeout(() => {
        timer = null
        void queryClient.invalidateQueries({ queryKey: ['projects'] })
      }, 150)
    }
    const offs = INVALIDATING_EVENTS.map((event) => rpc.on(event, invalidate))
    return () => {
      offs.forEach((off) => off())
      if (timer) clearTimeout(timer)
    }
  }, [rpc, queryClient])

  const projects = useMemo(
    () => sortProjects(query.data?.projects ?? []).filter((p) => projectId(p)),
    [query.data],
  )
  const byId = useMemo(() => new Map(projects.map((p) => [projectId(p), p])), [projects])

  return {
    projects,
    byId,
    loading: connected && query.isPending,
    error: query.error
      ? query.error instanceof Error
        ? query.error.message
        : String(query.error)
      : null,
  }
}

/** Invalidate everything a project mutation can change. */
export function invalidateProjects(queryClient: ReturnType<typeof useQueryClient>): void {
  void queryClient.invalidateQueries({ queryKey: ['projects'] })
  void queryClient.invalidateQueries({ queryKey: ['sessions'] })
}

interface AgentsList {
  agents?: AgentEntry[]
}

/** Registry agents (agents.list), for the new-project row and the page's chip. */
export function useAgents(): AgentEntry[] {
  const rpc = useRpc()
  const connected = useConnection((s) => s.state === 'connected')
  const query = useQuery<AgentsList>({
    queryKey: ['agents'],
    enabled: connected,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<AgentsList>('agents.list', {})
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  })
  return useMemo(() => (query.data?.agents ?? []).filter((a) => a.id), [query.data])
}

/**
 * File a session into a project (or out of one with null). Shared by the
 * sidebar's drag-and-drop and the chat header's menu so both toast alike.
 */
export function useMoveSession(): {
  move: (sessionKey: string, project: RawProject | null) => void
  pending: boolean
} {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: ({ sessionKey, project }: { sessionKey: string; project: RawProject | null }) =>
      rpc.call('sessions.patch', {
        key: sessionKey,
        projectId: project ? projectId(project) : null,
      }),
    onSuccess: (_data, { project }) => {
      toast.success(
        project
          ? `${t('projects.toast.moved')} ${projectName(project)}`
          : t('projects.toast.removed'),
        { id: 'projects-move' },
      )
      invalidateProjects(queryClient)
    },
    onError: (err) =>
      toast.error(`${t('projects.toast.moveFailed')}: ${errorText(err)}`, {
        id: 'projects-move-err',
      }),
  })
  return {
    move: (sessionKey, project) => mutation.mutate({ sessionKey, project }),
    pending: mutation.isPending,
  }
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}
