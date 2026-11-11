import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowUpRight,
  Bot,
  Check,
  ChevronRight,
  Clock,
  FolderKanban,
  Loader2,
  MessageSquare,
  MessageSquarePlus,
  MessagesSquare,
  Plus,
  Search,
  Trash2,
} from 'lucide-react'
import { useEffect, useId, useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { blockedBecause, gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  filterProjects,
  groupProjectSessionsByAgent,
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
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { Badge } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import { Input, Select, Textarea } from '@/web/components/ui/input'
import { PageHeader } from '@/web/components/ui/page-header'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import {
  createSessionOptimistic,
  SESSIONS_QUERY_KEY,
  useAgents,
  useSessionsList,
} from '@/web/shell/session-data'
import { sessionTitle, type SessionRow } from '@/web/shell/session-list'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useArrival } from '@/web/features/sessions/StatStrip'
import '@/web/styles/work.css'

// `/projects` and `/projects/:projectId` (task 8.3) on views/projects/logic:
// list, create, rename, knowledge editing with compare-and-swap, delete, a new
// chat in the project, and live updates from `projects.changed` and
// `sessions.changed`.

/** Full project rows. Shares the ['projects'] prefix with the shell's name map. */
const PROJECT_ROWS_QUERY_KEY = ['projects', 'rows'] as const

// Mirror PROJECT_NAME_MAX_CHARS / PROJECT_KNOWLEDGE_MAX_CHARS in
// src/agentos/session/manager.py: the gateway rejects anything longer.
const PROJECT_NAME_MAX = 200
const PROJECT_KNOWLEDGE_MAX = 24_000

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function isConflict(error: unknown): boolean {
  return (error as { code?: string } | null)?.code === 'project.conflict'
}

function updatedAtOf(p: RawProject): number {
  return Number(p.updated_at ?? p.updatedAt ?? 0) || 0
}

function useProjectRows() {
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

  const rows = useMemo(() => sortProjects(projects.data ?? []), [projects.data])
  const listed = useMemo(() => filterProjects(rows, query), [rows, query])
  const selected = projectId ? (rows.find((p) => projectIdOf(p) === projectId) ?? null) : null
  // Cards rise in once, when the list first arrives.
  const arriving = useArrival(!projects.isPending)

  const listing = projectId ? 'rail' : 'grid'
  const searchField = (
    <Input
      type="search"
      aria-label={t('webPagesWork.projectsSearch')}
      icon={<Search />}
      wrapperClassName={listing === 'grid' ? 'w-full sm:max-w-sm' : 'w-full'}
      value={query}
      onChange={(e) => setQuery(e.target.value)}
      placeholder={t('webPagesWork.projectsSearch')}
    />
  )

  const list = projects.isPending ? (
    <ProjectsSkeleton listing={listing} />
  ) : rows.length === 0 ? (
    projects.isError ? null : (
      <Card variant="plain" padding="none" className="border-dashed">
        <EmptyState
          icon={<FolderKanban />}
          title={t('webPagesWork.projectsEmpty')}
          description={t('webPagesWork.projectsEmptyBody')}
          action={
            <Button variant="primary" disabled={act.disabled} onClick={() => setCreating(true)}>
              <Plus />
              {t('webPagesWork.projectsNew')}
            </Button>
          }
        />
      </Card>
    )
  ) : (
    <div className="flex flex-col gap-3">
      {searchField}
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
          className={cn(
            arriving && 'anim-stagger',
            listing === 'grid'
              ? 'grid gap-3 sm:grid-cols-2 xl:grid-cols-3'
              : 'flex flex-col gap-0.5',
          )}
        >
          {listed.map((p) =>
            listing === 'grid' ? (
              <ProjectCard key={projectIdOf(p)} project={p} />
            ) : (
              <ProjectRailItem
                key={projectIdOf(p)}
                project={p}
                active={projectIdOf(p) === projectId}
              />
            ),
          )}
        </ul>
      )}
    </div>
  )

  let detail: ReactNode = null
  if (selected) {
    detail = (
      <ProjectDetail
        key={projectId}
        project={selected}
        sessions={sessions.rows}
        refetch={async () => (await projects.refetch()).data ?? []}
      />
    )
  } else if (projectId && projects.isPending) {
    detail = (
      <div aria-hidden className="flex flex-col gap-4">
        <Card padding="lg" className="flex items-center gap-4">
          <Skeleton className="size-12 shrink-0" />
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-3 w-12" />
            <Skeleton className="h-10 w-full" />
          </div>
        </Card>
        <Skeleton className="h-64 rounded-card" />
      </div>
    )
  } else if (projectId) {
    detail = (
      <Card variant="plain" padding="none">
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
      </Card>
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 p-4 md:p-6">
      <PageHeader
        title={t('webPagesWork.projectsTitle')}
        description={t('webPagesWork.projectsSubtitle')}
        actions={
          <Button
            variant="primary"
            disabled={act.disabled}
            title={act.reason ?? undefined}
            onClick={() => setCreating(true)}
          >
            <Plus />
            {t('webPagesWork.projectsNew')}
          </Button>
        }
        note={act.reason ? <span className="text-small text-text-muted">{act.reason}</span> : null}
      />
      {projects.isError ? (
        <ErrorState
          title={t('webPagesWork.projectsLoadFailed')}
          message={errorMessage(projects.error)}
          onRetry={() => void projects.refetch()}
          retrying={projects.isFetching}
        />
      ) : null}
      {listing === 'grid' ? (
        <section aria-label={t('webPagesWork.projectsList')}>{list}</section>
      ) : (
        <div className="grid items-start gap-6 md:grid-cols-[minmax(13rem,16rem)_1fr]">
          {/* On a phone the list and the detail take turns: the URL decides. */}
          <section
            aria-label={t('webPagesWork.projectsList')}
            className="sticky top-4 max-md:hidden"
          >
            {list}
          </section>
          <section className="min-w-0">
            <Link
              to="/projects"
              viewTransition
              className="press target mb-3 inline-flex items-center gap-1 rounded-md text-small text-text-muted hover:text-text md:hidden"
            >
              <ArrowLeft className="size-4" aria-hidden />
              {t('webPagesWork.backToProjects')}
            </Link>
            {detail}
          </section>
        </div>
      )}
      {creating ? (
        <CreateProjectDialog agents={agents.data ?? []} onClose={() => setCreating(false)} />
      ) : null}
    </div>
  )
}

// ── List items ───────────────────────────────────────────────────────────────

function projectPath(p: RawProject): string {
  return `/projects/${encodeURIComponent(projectIdOf(p))}`
}

/** A project in the overview grid: name, owner, knowledge, sessions, last change. */
function ProjectCard({ project }: { project: RawProject }) {
  const knowledge = String(project.knowledge ?? '')
  const owner = projectAgentId(project)
  const updated = updatedAtOf(project)
  return (
    <li className="min-w-0">
      <Card asChild variant="interactive" padding="none">
        <Link
          to={projectPath(project)}
          viewTransition
          className="group flex h-full flex-col gap-3 p-4"
        >
          <span className="flex items-start gap-3">
            <ProjectGlyph />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-section">{projectName(project)}</span>
              {owner ? (
                <span className="truncate text-small text-text-muted">
                  {t('webPagesWork.projectOwner', { agent: owner })}
                </span>
              ) : null}
            </span>
            <ArrowUpRight
              aria-hidden
              className="size-4 shrink-0 -translate-x-1 text-text-muted opacity-0 transition-[opacity,translate] duration-[var(--duration-base)] ease-[var(--ease-out)] group-hover:translate-x-0 group-hover:opacity-100 group-focus-visible:translate-x-0 group-focus-visible:opacity-100"
            />
          </span>
          <span
            className={cn(
              'line-clamp-2 min-h-[2lh] text-small',
              knowledge ? 'text-text-muted' : 'text-text-muted italic',
            )}
          >
            {knowledge ? knowledgeExcerpt(knowledge, 140) : t('webPagesWork.projectNoKnowledge')}
          </span>
          <span className="mt-auto flex items-center gap-3 border-t border-border pt-3 text-small text-text-muted">
            <span className="inline-flex items-center gap-1.5 tabular-nums">
              <MessageSquare className="size-3.5" aria-hidden />
              {tPlural('webPagesWork.sessionCount', projectSessionCount(project))}
            </span>
            {updated ? (
              <span className="ml-auto inline-flex items-center gap-1.5 truncate">
                <Clock className="size-3.5 shrink-0" aria-hidden />
                {relTimeLabel(updated)}
              </span>
            ) : null}
          </span>
        </Link>
      </Card>
    </li>
  )
}

/** A project in the side list next to an open project. */
function ProjectRailItem({ project, active }: { project: RawProject; active: boolean }) {
  return (
    <li>
      <Link
        to={projectPath(project)}
        viewTransition
        aria-current={active ? 'page' : undefined}
        className={cn(
          'press target flex items-center gap-2.5 rounded-control px-2.5 py-2',
          active
            ? 'bg-accent-tint text-text forced-colors:bg-[Highlight] forced-colors:text-[HighlightText] forced-colors:forced-color-adjust-none'
            : 'text-text-muted hover:bg-surface-2 hover:text-text',
        )}
      >
        <FolderKanban
          aria-hidden
          className={cn('size-4 shrink-0', active ? 'text-accent' : 'text-text-muted')}
        />
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate font-medium">{projectName(project)}</span>
          <span className="text-small text-text-muted tabular-nums">
            {tPlural('webPagesWork.sessionCount', projectSessionCount(project))}
          </span>
        </span>
      </Link>
    </li>
  )
}

/** The folder mark in an accent-tinted tile. */
function ProjectGlyph({ size = 'md' }: { size?: 'md' | 'lg' }) {
  return (
    <span
      aria-hidden
      className={cn(
        'grid shrink-0 place-items-center rounded-control border border-accent/20 bg-accent-tint text-accent shadow-highlight',
        size === 'lg' ? 'size-12 max-sm:hidden [&_svg]:size-6' : 'size-9 [&_svg]:size-4.5',
      )}
    >
      <FolderKanban />
    </span>
  )
}

function ProjectsSkeleton({ listing }: { listing: 'grid' | 'rail' }) {
  return (
    <LoadingRegion label={t('webPagesWork.projectsLoading')} className="flex flex-col gap-3">
      <Skeleton className={cn('h-9', listing === 'grid' ? 'w-full sm:max-w-sm' : 'w-full')} />
      {listing === 'grid' ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 3 }, (_, i) => (
            <Card key={i} padding="md" className="flex flex-col gap-3">
              <div className="flex items-center gap-3">
                <Skeleton className="size-9 shrink-0" />
                <div className="flex flex-1 flex-col gap-1.5">
                  <Skeleton className="h-4 w-32" />
                  <Skeleton className="h-3 w-20" />
                </div>
              </div>
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-4 w-28" />
            </Card>
          ))}
        </div>
      ) : (
        Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-12" />)
      )}
    </LoadingRegion>
  )
}

// ── Detail ───────────────────────────────────────────────────────────────────

/** A knowledge save lost the compare-and-swap: someone else saved first. */
interface Conflict {
  theirs: string
}

function ProjectDetail({
  project,
  sessions,
  refetch,
}: {
  project: RawProject
  sessions: SessionRow[]
  refetch: () => Promise<RawProject[]>
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const id = projectIdOf(project)
  const name = projectName(project)
  const knowledge = String(project.knowledge ?? '')
  const nameFieldId = useId()
  const knowledgeFieldId = useId()
  const counterId = useId()
  const online = requireConnection(useDisplayedConnection())
  const act = gate(online)

  const [nameDraft, setNameDraft] = useState<string | null>(null)
  // The knowledge draft remembers the version it was started from, so the
  // compare-and-swap protects edits made while a live refresh lands.
  const [draft, setDraft] = useState<{ text: string; base: number } | null>(null)
  const [conflict, setConflict] = useState<Conflict | null>(null)
  const [saving, setSaving] = useState<'name' | 'knowledge' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [starting, setStarting] = useState(false)
  const owner = projectAgentId(project)

  const projectSessions = sessionsInProject(sessions, id) as SessionRow[]
  const groups = groupProjectSessionsByAgent(projectSessions)
  // The shared list holds only the newest sessions; the project's own count is exact.
  const partial = projectSessionCount(project) > projectSessions.length
  const knowledgeValue = draft?.text ?? knowledge
  const dirty = draft !== null && draft.text !== knowledge

  const applyFresh = (fresh: RawProject | undefined) => {
    if (!fresh) return
    queryClient.setQueryData<RawProject[]>(PROJECT_ROWS_QUERY_KEY, (prev) =>
      prev?.map((p) => (projectIdOf(p) === id ? { ...p, ...fresh } : p)),
    )
  }

  const update = (fields: { name?: string; knowledge?: string }, expectedUpdatedAt: number) =>
    rpc.call<{ project?: RawProject }>('projects.update', {
      projectId: id,
      ...fields,
      expectedUpdatedAt,
    })

  const saveName = async () => {
    const next = (nameDraft ?? '').trim()
    const sentBase = updatedAtOf(project)
    setSaving('name')
    setError(null)
    try {
      const res = await update({ name: next }, sentBase)
      applyFresh(res?.project)
      // The rename bumped the project's version. A knowledge draft started on
      // the version this rename replaced has seen every change but our own,
      // so move it onto the new version: otherwise its save reads as someone
      // else's conflict.
      const renamed = res?.project ? updatedAtOf(res.project) : 0
      if (renamed) setDraft((d) => (d && d.base === sentBase ? { ...d, base: renamed } : d))
      setNameDraft(null)
      toast.success(t('webPagesWork.projectRenamed'))
    } catch (e) {
      if (isConflict(e)) {
        await refetch()
        setError(t('webPagesWork.renameConflict'))
      } else setError(t('webPagesWork.projectSaveFailed', { message: errorMessage(e) }))
    } finally {
      setSaving(null)
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    }
  }

  const saveKnowledge = async (expectedUpdatedAt: number) => {
    if (!draft) return
    setSaving('knowledge')
    setError(null)
    try {
      const res = await update({ knowledge: draft.text }, expectedUpdatedAt)
      applyFresh(res?.project)
      setDraft(null)
      setConflict(null)
      toast.success(t('webPagesWork.knowledgeSaved'))
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    } catch (e) {
      if (isConflict(e)) {
        // Someone saved first. Keep this draft, fetch theirs, and let the
        // user choose: take their text, or overwrite it with this one.
        const latest = (await refetch()).find((p) => projectIdOf(p) === id)
        setConflict({ theirs: String(latest?.knowledge ?? '') })
        if (latest) setDraft((d) => (d ? { ...d, base: updatedAtOf(latest) } : d))
      } else setError(t('webPagesWork.projectSaveFailed', { message: errorMessage(e) }))
    } finally {
      setSaving(null)
    }
  }

  const deleteProject = async () => {
    setDeleting(true)
    setError(null)
    try {
      await rpc.call('projects.delete', { projectId: id })
      toast.success(t('webPagesWork.projectDeleted'))
      setConfirmDelete(false)
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      void queryClient.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY })
      navigate('/projects')
    } catch (e) {
      setConfirmDelete(false)
      setError(t('webPagesWork.projectDeleteFailed', { message: errorMessage(e) }))
    } finally {
      setDeleting(false)
    }
  }

  // As the old Projects view did: the session starts at once under the
  // project's owning agent and opens in chat. A project without an owner goes
  // through /new, where the agent is chosen.
  const startChat = async () => {
    if (!owner) {
      navigate(`/new?project=${encodeURIComponent(id)}`)
      return
    }
    setStarting(true)
    setError(null)
    try {
      const key = await createSessionOptimistic(rpc, queryClient, {
        agentId: owner,
        projectId: id,
      })
      navigate(sessionKeyToPath(key))
    } catch (e) {
      setError(t('webPagesWork.createFailed', { message: errorMessage(e) }))
    } finally {
      setStarting(false)
    }
  }

  const nameGate = gate(
    online,
    nameDraft === null || nameDraft.trim() === name
      ? blockedBecause(t('webPagesWork.noChanges'))
      : null,
    nameDraft !== null && !nameDraft.trim() ? blockedBecause(t('webPagesWork.nameRequired')) : null,
  )
  const knowledgeGate = gate(
    online,
    !dirty && !conflict ? blockedBecause(t('webPagesWork.noChanges')) : null,
  )

  const sessionTotal = Math.max(projectSessionCount(project), projectSessions.length)

  return (
    <article aria-label={name} className="anim-enter flex flex-col gap-4">
      <Card padding="lg" className="flex flex-col gap-4">
        <div className="flex items-start gap-4">
          <ProjectGlyph size="lg" />
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <label htmlFor={nameFieldId} className="text-label text-text-muted">
              {t('webPagesWork.projectName')}
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                id={nameFieldId}
                size="lg"
                className="flex-1 text-section"
                value={nameDraft ?? name}
                maxLength={PROJECT_NAME_MAX}
                onChange={(e) => setNameDraft(e.target.value)}
                onKeyDown={(e) => {
                  // Enter saves the name, as Save name does; not mid-composition.
                  if (e.key !== 'Enter' || e.nativeEvent.isComposing) return
                  e.preventDefault()
                  if (!nameGate.disabled && saving === null) void saveName()
                }}
              />
              {nameDraft !== null && nameDraft !== name ? (
                <Button
                  className="anim-fade-in"
                  variant="outline"
                  disabled={nameGate.disabled || saving !== null}
                  title={nameGate.reason ?? undefined}
                  onClick={() => void saveName()}
                >
                  {saving === 'name' ? <Loader2 className="animate-spin" /> : <Check />}
                  {t('webPagesWork.saveName')}
                </Button>
              ) : null}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {owner ? (
            <Badge tone="neutral">
              <Bot aria-hidden />
              {t('webPagesWork.projectOwner', { agent: owner })}
            </Badge>
          ) : null}
          <Badge tone="neutral">
            <MessageSquare aria-hidden />
            {tPlural('webPagesWork.sessionCount', sessionTotal)}
          </Badge>
          {updatedAtOf(project) ? (
            <Badge tone="neutral">
              <Clock aria-hidden />
              {t('webPagesWork.projectUpdated', { time: relTimeLabel(updatedAtOf(project)) })}
            </Badge>
          ) : null}
        </div>
      </Card>

      {error ? (
        <p
          role="alert"
          className="anim-fade-in flex items-start gap-2 rounded-card border border-danger/30 bg-danger/10 px-3 py-2"
        >
          <AlertTriangle
            className="mt-0.5 size-4 shrink-0 text-danger"
            aria-hidden
            data-status-icon
          />
          {error}
        </p>
      ) : null}

      <Card padding="none" className="flex flex-col">
        <div className="flex flex-col gap-1 px-5 pt-4 pb-3">
          <label
            htmlFor={knowledgeFieldId}
            className="flex items-center gap-2 text-section [&_svg]:size-4 [&_svg]:text-text-muted"
          >
            {t('webPagesWork.knowledge')}
          </label>
          <p className="text-small text-text-muted">{t('webPagesWork.knowledgeHint')}</p>
        </div>
        <div className="px-5">
          <Textarea
            id={knowledgeFieldId}
            rows={9}
            value={knowledgeValue}
            maxLength={PROJECT_KNOWLEDGE_MAX}
            placeholder={t('webPagesWork.knowledgePlaceholder')}
            aria-describedby={counterId}
            onChange={(e) =>
              setDraft((d) => ({ text: e.target.value, base: d?.base ?? updatedAtOf(project) }))
            }
            className="font-mono text-small"
          />
        </div>
        {conflict ? (
          <div
            role="alert"
            className="anim-enter mx-5 mt-3 flex flex-col gap-2 rounded-card border border-warn/30 bg-warn/10 p-3"
          >
            <p className="flex items-center gap-2 font-medium">
              <AlertTriangle className="size-4 shrink-0 text-warn" aria-hidden data-status-icon />
              {t('webPagesWork.conflictTitle')}
            </p>
            <p className="text-text-muted">{t('webPagesWork.conflictBody')}</p>
            <p className="text-label">{t('webPagesWork.conflictTheirs')}</p>
            <pre
              tabIndex={0}
              aria-label={t('webPagesWork.conflictTheirs')}
              className="max-h-48 overflow-auto rounded-control bg-surface-inset p-2 font-mono text-small whitespace-pre-wrap shadow-inset"
            >
              {conflict.theirs || t('webPagesWork.conflictEmpty')}
            </pre>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setDraft(null)
                  setConflict(null)
                }}
              >
                {t('webPagesWork.conflictUseTheirs')}
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={act.disabled || saving !== null}
                title={act.reason ?? undefined}
                onClick={() => void saveKnowledge(draft?.base ?? updatedAtOf(project))}
              >
                {t('webPagesWork.conflictOverwrite')}
              </Button>
            </div>
          </div>
        ) : null}
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border px-5 py-3">
          <span id={counterId} className="text-small text-text-muted tabular-nums">
            {t('webPagesWork.knowledgeCounter', {
              count: knowledgeValue.length,
              max: PROJECT_KNOWLEDGE_MAX,
            })}
            {dirty ? <span className="sr-only"> · {t('webPagesWork.unsaved')}</span> : null}
          </span>
          {dirty ? (
            <Badge tone="warn" dot className="anim-fade-in" aria-hidden>
              {t('webPagesWork.unsaved')}
            </Badge>
          ) : null}
          <span className="flex-1" />
          {dirty ? (
            <Button variant="ghost" size="sm" onClick={() => setDraft(null)}>
              {t('webPagesWork.discard')}
            </Button>
          ) : null}
          {conflict ? null : (
            <Button
              size="sm"
              variant="primary"
              disabled={knowledgeGate.disabled || saving !== null}
              title={knowledgeGate.reason ?? undefined}
              onClick={() => void saveKnowledge(draft?.base ?? updatedAtOf(project))}
            >
              {saving === 'knowledge' ? <Loader2 className="animate-spin" /> : null}
              {t('webPagesWork.saveKnowledge')}
            </Button>
          )}
        </div>
      </Card>

      <Card asChild padding="none">
        <section aria-label={t('webPagesWork.projectSessions')} className="flex flex-col">
          <div className="flex flex-wrap items-center gap-2 px-5 pt-4 pb-3">
            <h2 className="flex flex-1 items-center gap-2 text-section [&_svg]:size-4 [&_svg]:text-text-muted">
              <MessagesSquare aria-hidden />
              {t('webPagesWork.projectSessions')}
            </h2>
            <Button
              size="sm"
              variant="outline"
              disabled={starting || (!!owner && act.disabled)}
              title={owner ? (act.reason ?? undefined) : undefined}
              onClick={() => void startChat()}
            >
              {starting ? <Loader2 className="animate-spin" /> : <MessageSquarePlus />}
              {t('webPagesWork.newChatInProject')}
            </Button>
          </div>
          {partial ? (
            <p role="note" className="mx-5 mb-3 flex items-start gap-2 text-small text-text-muted">
              {t('webPagesWork.projectSessionsPartial', {
                shown: projectSessions.length,
                total: projectSessionCount(project),
              })}
            </p>
          ) : null}
          {groups.length === 0 ? (
            partial ? null : (
              <div className="mx-5 mb-5 flex items-center gap-3 rounded-card border border-dashed border-border-strong px-4 py-5 text-text-muted">
                <span
                  aria-hidden
                  className="grid size-9 shrink-0 place-items-center rounded-control bg-surface-2 [&_svg]:size-4"
                >
                  <MessagesSquare />
                </span>
                <p className="text-pretty">{t('webPagesWork.projectNoSessions')}</p>
              </div>
            )
          ) : (
            <div className="flex flex-col gap-3 px-3 pb-3">
              {groups.map((group) => (
                <div key={group.agentId} className="flex flex-col gap-0.5">
                  <h3 className="flex items-center gap-1.5 px-2 pb-1 text-label tracking-[0.04em] text-text-muted uppercase [&_svg]:size-3.5">
                    <Bot aria-hidden />
                    {t('webPagesWork.agentGroup', { agent: group.agentId })}
                  </h3>
                  <ul role="list" className="flex flex-col">
                    {(group.items as SessionRow[]).map((s) => (
                      <li key={s.key}>
                        <Link
                          to={sessionKeyToPath(s.key)}
                          viewTransition
                          className="press group target flex items-center gap-2.5 rounded-control px-2 py-2 hover:bg-surface-2"
                        >
                          <MessageSquare className="size-4 shrink-0 text-text-muted" aria-hidden />
                          <span className="min-w-0 flex-1 truncate">{sessionTitle(s)}</span>
                          <span className="text-small whitespace-nowrap text-text-muted">
                            {relTimeLabel(s.updated_at ?? '')}
                          </span>
                          <ChevronRight
                            aria-hidden
                            className="size-4 shrink-0 text-text-muted opacity-0 transition-opacity duration-[var(--duration-fast)] group-hover:opacity-100"
                          />
                        </Link>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </section>
      </Card>

      <Card
        variant="plain"
        className="flex flex-wrap items-center gap-x-4 gap-y-3 border-danger/25 bg-danger/[0.04]"
      >
        <p className="min-w-0 flex-[1_1_16rem] text-small text-text-muted">
          {t('webPagesWork.deleteProjectBody')}
        </p>
        <Button
          variant="outline"
          className="border-danger/40 text-danger hover:bg-danger/10"
          disabled={act.disabled}
          title={act.reason ?? undefined}
          onClick={() => setConfirmDelete(true)}
        >
          <Trash2 />
          {t('webPagesWork.deleteProject')}
        </Button>
      </Card>

      <AlertDialog
        open={confirmDelete}
        onOpenChange={(open) => !deleting && setConfirmDelete(open)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('webPagesWork.deleteProjectTitle', { name })}</AlertDialogTitle>
            <AlertDialogDescription>{t('webPagesWork.deleteProjectBody')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>{t('webPagesWork.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              disabled={deleting}
              onClick={(e) => {
                e.preventDefault()
                void deleteProject()
              }}
            >
              {deleting ? <Loader2 className="animate-spin" /> : null}
              {t('webPagesWork.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </article>
  )
}

// ── Create ───────────────────────────────────────────────────────────────────

function CreateProjectDialog({
  agents,
  onClose,
}: {
  agents: { id: string; name: string }[]
  onClose: () => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [agentId, setAgentId] = useState(() =>
    agents.some((a) => a.id === 'main') || agents.length === 0 ? 'main' : agents[0]!.id,
  )
  const [knowledge, setKnowledge] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nameId = useId()
  const agentFieldId = useId()
  const knowledgeId = useId()
  const reasonId = useId()
  const createGate = gate(
    requireConnection(useDisplayedConnection()),
    !name.trim() ? blockedBecause(t('webPagesWork.nameRequired')) : null,
  )

  const submit = async () => {
    if (createGate.disabled) return
    setBusy(true)
    setError(null)
    try {
      const res = await rpc.call<{ project?: RawProject }>('projects.create', {
        agentId,
        name: name.trim(),
        knowledge,
      })
      toast.success(t('webPagesWork.projectCreated'))
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      onClose()
      const id = res?.project ? projectIdOf(res.project) : ''
      if (id) navigate(`/projects/${encodeURIComponent(id)}`)
    } catch (e) {
      setError(t('webPagesWork.projectCreateFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  const agentChoices = agents.length ? agents : [{ id: 'main', name: 'main' }]
  return (
    <Dialog open onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogContent>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <DialogHeader>
            <DialogTitle>{t('webPagesWork.createProjectTitle')}</DialogTitle>
            <DialogDescription>{t('webPagesWork.createProjectBody')}</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={nameId} className="text-label">
              {t('webPagesWork.projectName')}
            </label>
            <Input
              id={nameId}
              autoFocus
              value={name}
              maxLength={PROJECT_NAME_MAX}
              placeholder={t('webPagesWork.projectNamePlaceholder')}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={agentFieldId} className="text-label">
              {t('webPagesWork.projectAgent')}
            </label>
            <Select id={agentFieldId} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              {agentChoices.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </Select>
            <span className="text-small text-text-muted">{t('webPagesWork.projectAgentHint')}</span>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={knowledgeId} className="text-label">
              {t('webPagesWork.knowledge')}
            </label>
            <Textarea
              id={knowledgeId}
              rows={5}
              className="font-mono text-small"
              value={knowledge}
              maxLength={PROJECT_KNOWLEDGE_MAX}
              placeholder={t('webPagesWork.knowledgePlaceholder')}
              onChange={(e) => setKnowledge(e.target.value)}
            />
          </div>
          {error ? (
            <p
              role="alert"
              className="anim-fade-in rounded-control border border-danger/30 bg-danger/10 px-3 py-2"
            >
              {error}
            </p>
          ) : null}
          <DialogFooter className="items-center">
            {createGate.reason ? (
              <span id={reasonId} className="text-small text-text-muted sm:mr-auto">
                {createGate.reason}
              </span>
            ) : null}
            <Button variant="outline" disabled={busy} onClick={onClose}>
              {t('webPagesWork.cancel')}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={busy || createGate.disabled}
              aria-describedby={createGate.reason ? reasonId : undefined}
            >
              {busy ? <Loader2 className="animate-spin" /> : null}
              {t('webPagesWork.createProject')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
