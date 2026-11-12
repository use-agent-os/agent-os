import { useQuery } from '@tanstack/react-query'
import type { CSSProperties } from 'react'
import { useRpc } from '@/app/providers'
import { cn } from '@/lib/utils'
import type { RawProject } from '@/views/projects/logic'
import { projectId as projectIdOf } from '@/views/projects/logic'
import { sessionRunStatus, sessionVisualStatus } from '@/views/sessions/logic'
import type { SessionRow } from '@/web/shell/session-list'
import { hueOf, initialOf } from './hue'
import '@/web/styles/page-projects.css'

/** Full project rows. Shares the ['projects'] prefix with the shell's name map. */
export const PROJECT_ROWS_QUERY_KEY = ['projects', 'rows'] as const

// Mirror PROJECT_NAME_MAX_CHARS / PROJECT_KNOWLEDGE_MAX_CHARS in
// src/agentos/session/manager.py: the gateway rejects anything longer.
export const PROJECT_NAME_MAX = 200
export const PROJECT_KNOWLEDGE_MAX = 24_000

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function updatedAtOf(p: RawProject): number {
  return Number(p.updated_at ?? p.updatedAt ?? 0) || 0
}

export function projectPath(p: RawProject): string {
  return `/projects/${encodeURIComponent(projectIdOf(p))}`
}

export function useProjectRows() {
  const rpc = useRpc()
  return useQuery({
    queryKey: PROJECT_ROWS_QUERY_KEY,
    queryFn: async (): Promise<RawProject[]> => {
      await rpc.waitForConnection()
      const data = await rpc.call<{ projects?: RawProject[] }>('projects.list', {})
      return data?.projects ?? []
    },
  })
}

/** A project's mark: its initial on a tile tinted from its name. Decorative. */
export function ProjectTile({
  name,
  size = 'md',
  hue,
  className,
}: {
  name: string
  size?: 'sm' | 'md' | 'lg'
  /** A fixed palette slot instead of the one the name picks. */
  hue?: number
  className?: string
}) {
  return (
    <span
      aria-hidden
      data-size={size}
      data-hue={hue ?? hueOf(name)}
      className={cn('pj-tile pj-hue', className)}
    >
      {initialOf(name)}
    </span>
  )
}

/** A session's colour on the status scale: executing, open, done, failed, aborted. */
export function sessionTone(row: SessionRow): string {
  const run = sessionRunStatus(row)
  if (run === 'queued' || run === 'running') return 'text-accent'
  const visual = sessionVisualStatus(row)
  if (visual === 'running') return 'text-ok'
  if (visual === 'done') return 'text-info'
  if (visual === 'failed' || visual === 'timeout') return 'text-danger'
  return 'text-text-muted'
}

const MAX_PIPS = 10

/** One pip per loaded session in its status colour; the count is in text beside it. */
export function SessionPips({ sessions, more }: { sessions: SessionRow[]; more: string }) {
  if (sessions.length === 0) return null
  const shown = sessions.slice(0, MAX_PIPS)
  return (
    <span aria-hidden className="pj-pips">
      {shown.map((s, i) => (
        <span
          key={s.key}
          className={sessionTone(s)}
          style={{ '--i': Math.min(i, 8) } as CSSProperties}
        />
      ))}
      {sessions.length > MAX_PIPS ? <span className="pj-pips-more">{more}</span> : null}
    </span>
  )
}
