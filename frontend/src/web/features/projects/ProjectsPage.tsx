import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Clock, FolderKanban, Plus, Search } from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import { useRpc } from '@/app/providers'
import { formatNumber, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  filterProjects,
  knowledgeExcerpt,
  projectAgentId,
  projectId as projectIdOf,
  projectName,
  projectSessionCount,
  sessionsInProject,
  sortProjects,
  type RawProject,
} from '@/views/projects/logic'
import { relTimeLabel } from '@/views/sessions/logic'
import { Button } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import { Input } from '@/web/components/ui/input'
import { PageHeader } from '@/web/components/ui/page-header'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { SESSIONS_QUERY_KEY, useAgents, useSessionsList } from '@/web/shell/session-data'
import type { SessionRow } from '@/web/shell/session-list'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { PAGE_CLASS } from '@/web/features/home/overview-ui'
import { useArrival } from '@/web/features/sessions/use-arrival'
import { CreateProjectDialog } from './CreateProjectDialog'
import { hueOf } from './hue'
import { ProjectDetail } from './ProjectDetail'
import {
  errorMessage,
  PROJECT_ROWS_QUERY_KEY,
  projectPath,
  ProjectTile,
  SessionPips,
  updatedAtOf,
  useProjectRows,
} from './project-ui'

// `/projects` and `/projects/:projectId` (task 8.3) on views/projects/logic:
// a gallery of project cards (a tinted initial tile, owner, knowledge excerpt,
// the session mix and last activity) ending in a "New project" tile; the
// detail page; create, rename, knowledge editing with compare-and-swap,
// delete, a new chat in the project, and live updates from `projects.changed`
// and `sessions.changed`.

export function ProjectsPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const { projectId = '' } = useParams()
  const projects = useProjectRows()
  const sessions = useSessionsList()
  const agents = useAgents()
  const connection = useDisplayedConnection()
  const act = gate(requireConnection(connection))
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const reasonId = useId()

  // Live updates: a rename, delete or membership change from another client
  // shows up here without a manual refresh.
  useEffect(() => {
    const offProjects = rpc.on('projects.changed', () => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    })
    const offSessions = rpc.on('sessions.changed', () => {
      void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: PROJECT_ROWS_QUERY_KEY })
    })
    return () => {
      offProjects()
      offSessions()
    }
  }, [rpc, queryClient])

  // The gallery and a project share this component, so the page's scroller
  // keeps its offset across them: opening a card low in the gallery would
  // land mid-way down the project. Start each one at the top.
  const pageRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    for (let el = pageRef.current?.parentElement; el; el = el.parentElement) {
      if (el.scrollTop > 0) {
        el.scrollTop = 0
        break
      }
    }
  }, [projectId])

  const rows = useMemo(() => sortProjects(projects.data ?? []), [projects.data])
  const listed = useMemo(() => filterProjects(rows, query), [rows, query])
  const selected = projectId ? (rows.find((p) => projectIdOf(p) === projectId) ?? null) : null
  // Cards rise in once, when the list first arrives.
  const arriving = useArrival(!projects.isPending)
  const empty = !projects.isPending && !projects.isError && rows.length === 0

  const create = () => setCreating(true)
  const newButton = (primary: boolean) => (
    <Button
      variant={primary ? 'primary' : 'outline'}
      disabled={act.disabled}
      aria-describedby={act.reason ? reasonId : undefined}
      onClick={create}
    >
      <Plus />
      {t('webPagesWork.projectsNew')}
    </Button>
  )

  let body: ReactNode
  if (projectId) {
    if (selected) {
      body = (
        <ProjectDetail
          key={projectId}
          project={selected}
          projects={rows}
          sessions={sessions.rows}
          refetch={async () => (await projects.refetch()).data ?? []}
        />
      )
    } else if (projects.isPending) {
      body = <DetailSkeleton />
    } else {
      body = (
        <EmptyState
          icon={<FolderKanban />}
          title={t('webPagesWork.projectMissing')}
          description={t('webPagesWork.projectMissingBody')}
          action={
            <Button asChild variant="outline">
              <Link to="/projects" viewTransition>
                {t('webPagesWork.backToProjects')}
              </Link>
            </Button>
          }
        />
      )
    }
  } else if (projects.isPending) {
    body = <GallerySkeleton />
  } else if (rows.length === 0) {
    body = projects.isError ? null : <ProjectsEmpty action={newButton(true)} />
  } else {
    body = (
      <section aria-label={t('webPagesWork.projectsList')} className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Input
            type="search"
            aria-label={t('webPagesWork.projectsSearch')}
            icon={<Search />}
            wrapperClassName="flex-[1_1_14rem] sm:max-w-xs"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('webPagesWork.projectsSearch')}
          />
          <span className="ml-auto text-small text-text-muted tabular-nums">
            {tPlural('webPagesWork.projectsCount', rows.length)}
          </span>
        </div>
        {listed.length === 0 ? (
          <EmptyState
            size="sm"
            icon={<Search />}
            title={t('webPagesWork.projectsNoMatch')}
            action={
              <Button variant="outline" size="sm" onClick={() => setQuery('')}>
                {t('webPagesWork.clearFilters')}
              </Button>
            }
          />
        ) : (
          <ul
            role="list"
            aria-label={t('webPagesWork.projectsList')}
            className={cn('pj-grid', arriving && 'anim-stagger')}
          >
            {listed.map((p) => (
              <ProjectCard
                key={projectIdOf(p)}
                project={p}
                sessions={sessionsInProject(sessions.rows, projectIdOf(p)) as SessionRow[]}
              />
            ))}
            {query.trim() ? null : (
              <li className="min-w-0">
                <button
                  type="button"
                  className="pj-new press target"
                  disabled={act.disabled}
                  title={act.reason ?? undefined}
                  onClick={create}
                >
                  <span aria-hidden className="pj-new-icon">
                    <Plus />
                  </span>
                  <span className="font-medium text-text">{t('webPagesWork.projectsNew')}</span>
                  <span className="text-small">{t('webPagesWork.projectsNewCard')}</span>
                </button>
              </li>
            )}
          </ul>
        )}
      </section>
    )
  }

  return (
    <div ref={pageRef} className={PAGE_CLASS}>
      <PageHeader
        title={t('webPagesWork.projectsTitle')}
        description={t('webPagesWork.projectsSubtitle')}
        // The empty state below carries the one primary action.
        actions={newButton(!empty)}
        note={
          act.reason ? (
            <span id={reasonId} className="text-small text-text-muted">
              {act.reason}
            </span>
          ) : null
        }
      />
      {projects.isError ? (
        <ErrorState
          title={t('webPagesWork.projectsLoadFailed')}
          message={errorMessage(projects.error)}
          onRetry={() => void projects.refetch()}
          retrying={projects.isFetching}
        />
      ) : null}
      {body}
      {creating ? (
        <CreateProjectDialog agents={agents.data ?? []} onClose={() => setCreating(false)} />
      ) : null}
    </div>
  )
}

// ── Gallery ──────────────────────────────────────────────────────────────────

/** A project card: mark, name, owner, knowledge, the session mix, last change. */
function ProjectCard({ project, sessions }: { project: RawProject; sessions: SessionRow[] }) {
  const name = projectName(project)
  const knowledge = String(project.knowledge ?? '')
  const owner = projectAgentId(project)
  const updated = updatedAtOf(project)
  const count = Math.max(projectSessionCount(project), sessions.length)
  return (
    <li className="min-w-0">
      <Card asChild variant="interactive" padding="none">
        <Link
          to={projectPath(project)}
          viewTransition
          // The flex column here, not in page-projects.css: the interactive
          // card variant sets `block`, and utilities outrank component rules.
          className="pj-card flex flex-col"
          data-hue={hueOf(name)}
        >
          <span className="flex items-start gap-3">
            <ProjectTile name={name} />
            <span className="flex min-w-0 flex-1 flex-col pt-0.5">
              <span className="truncate text-section">{name}</span>
              <span className="truncate text-small text-text-muted">
                {owner
                  ? t('webPagesWork.projectOwner', { agent: owner })
                  : t('webPagesWork.projectNoOwner')}
              </span>
            </span>
            <ArrowUpRight aria-hidden className="pj-card-arrow size-4 shrink-0" />
          </span>
          <span className={cn('pj-excerpt', !knowledge && 'italic')}>
            {knowledge ? knowledgeExcerpt(knowledge, 180) : t('webPagesWork.projectNoKnowledge')}
          </span>
          <span className="pj-card-foot">
            <SessionPips
              sessions={sessions}
              more={t('webPagesWork.projectSessionMore', {
                count: formatNumber(sessions.length - 10),
              })}
            />
            <span className="whitespace-nowrap">{tPlural('webPagesWork.sessionCount', count)}</span>
            {updated ? (
              <span className="ml-auto inline-flex min-w-0 items-center gap-1.5">
                <Clock className="size-3.5 shrink-0" aria-hidden />
                <span className="truncate">{relTimeLabel(updated)}</span>
              </span>
            ) : null}
          </span>
        </Link>
      </Card>
    </li>
  )
}

/** What a gallery looks like, ghosted, with the call to action over it. */
const GHOSTS = [
  { name: 'Research', hue: 0 },
  { name: 'Launch plan', hue: 3 },
  { name: 'Support', hue: 4 },
]

function ProjectsEmpty({ action }: { action: ReactNode }) {
  const titleId = useId()
  return (
    <section aria-labelledby={titleId} className="pj-empty anim-enter">
      <div aria-hidden className="pj-ghosts pj-grid anim-stagger">
        {GHOSTS.map(({ name, hue }, i) => (
          <div
            key={name}
            className="pj-card flex flex-col rounded-card border border-border"
            data-hue={hue}
          >
            <span className="flex items-center gap-3">
              <ProjectTile name={name} hue={hue} />
              <span className="flex flex-1 flex-col gap-2">
                <span className="pj-ghost-bar" style={{ width: `${60 - i * 8}%` }} />
                <span className="pj-ghost-bar w-1/3 opacity-60" />
              </span>
            </span>
            <span className="flex flex-col gap-2">
              <span className="pj-ghost-bar w-full opacity-70" />
              <span className="pj-ghost-bar w-11/12 opacity-70" />
              <span className="pj-ghost-bar w-2/3 opacity-70" />
            </span>
            <span className="pj-card-foot">
              <span className="pj-ghost-bar w-16" />
              <span className="pj-ghost-bar ml-auto w-10" />
            </span>
          </div>
        ))}
      </div>
      <div className="pj-empty-copy">
        <span aria-hidden className="pj-tile pj-hue" data-size="lg" data-hue="0">
          <FolderKanban className="size-6" />
        </span>
        <h2 id={titleId} className="text-section">
          {t('webPagesWork.projectsEmpty')}
        </h2>
        <p className="text-pretty text-text-muted">{t('webPagesWork.projectsEmptyBody')}</p>
        <div className="mt-2">{action}</div>
      </div>
    </section>
  )
}

function GallerySkeleton() {
  return (
    <LoadingRegion label={t('webPagesWork.projectsLoading')} className="flex flex-col gap-4">
      <Skeleton className="h-9 w-full sm:max-w-xs" />
      <div className="pj-grid">
        {Array.from({ length: 3 }, (_, i) => (
          <Card key={i} padding="none" className="flex min-h-52 flex-col gap-3 p-[1.125rem]">
            <div className="flex items-center gap-3">
              <Skeleton className="size-11 shrink-0 rounded-card" />
              <div className="flex flex-1 flex-col gap-1.5">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-3 w-20" />
              </div>
            </div>
            <Skeleton className="h-12 w-full" />
            <Skeleton className="mt-auto h-4 w-28" />
          </Card>
        ))}
      </div>
    </LoadingRegion>
  )
}

function DetailSkeleton() {
  return (
    <div aria-hidden className="flex flex-col gap-4">
      <Card padding="lg" className="flex items-center gap-4">
        <Skeleton className="size-14 shrink-0 rounded-[14px]" />
        <div className="flex flex-1 flex-col gap-2">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-8 w-2/3" />
        </div>
      </Card>
      <Skeleton className="h-64 rounded-card" />
    </div>
  )
}
