import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  FolderKanban,
  Loader2,
  MessageSquare,
  MessageSquarePlus,
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
import { Button } from '@/web/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { SESSIONS_QUERY_KEY, useAgents, useSessionsList } from '@/web/shell/session-data'
import { sessionTitle, type SessionRow } from '@/web/shell/session-list'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'

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

const fieldClass =
  'w-full rounded-md border border-border-control bg-surface-0 px-3 text-ui max-md:text-base'

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

  const list = projects.isPending ? (
    <LoadingRegion label={t('webPagesWork.projectsLoading')}>
      {Array.from({ length: 5 }, (_, i) => (
        <Skeleton key={i} className="my-2 h-14" />
      ))}
    </LoadingRegion>
  ) : rows.length === 0 ? (
    projects.isError ? null : (
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
    )
  ) : (
    <div className="flex flex-col gap-2">
      <label className="relative block">
        <span className="sr-only">{t('webPagesWork.projectsSearch')}</span>
        <Search
          className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-muted"
          aria-hidden
        />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('webPagesWork.projectsSearch')}
          className={cn(fieldClass, 'h-9 pl-8 touch:h-11')}
        />
      </label>
      {listed.length === 0 ? (
        <p className="px-1 py-4 text-text-muted">{t('webPagesWork.projectsNoMatch')}</p>
      ) : (
        <ul role="list" aria-label={t('webPagesWork.projectsList')} className="flex flex-col gap-1">
          {listed.map((p) => {
            const id = projectIdOf(p)
            const knowledge = String(p.knowledge ?? '')
            return (
              <li key={id}>
                <Link
                  to={`/projects/${encodeURIComponent(id)}`}
                  aria-current={id === projectId ? 'page' : undefined}
                  className={cn(
                    'target flex flex-col gap-0.5 rounded-md px-3 py-2',
                    id === projectId ? 'bg-surface-3' : 'hover:bg-surface-2',
                  )}
                >
                  <span className="truncate font-medium">{projectName(p)}</span>
                  <span className="text-small text-text-muted">
                    {tPlural('webPagesWork.sessionCount', projectSessionCount(p))}
                  </span>
                  {knowledge ? (
                    <span className="truncate text-small text-text-muted">
                      {knowledgeExcerpt(knowledge)}
                    </span>
                  ) : null}
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )

  let detail: ReactNode
  if (!projectId) {
    detail = rows.length ? (
      <p className="p-6 text-text-muted">{t('webPagesWork.projectsPick')}</p>
    ) : null
  } else if (selected) {
    detail = (
      <ProjectDetail
        key={projectId}
        project={selected}
        sessions={sessions.rows}
        refetch={async () => (await projects.refetch()).data ?? []}
      />
    )
  } else if (projects.isPending) {
    detail = null
  } else {
    detail = (
      <EmptyState
        title={t('webPagesWork.projectMissing')}
        description={t('webPagesWork.projectMissingBody')}
        action={
          <Button asChild variant="outline">
            <Link to="/projects">{t('webPagesWork.backToProjects')}</Link>
          </Button>
        }
      />
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-6">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold">{t('webPagesWork.projectsTitle')}</h1>
          <p className="text-text-muted">{t('webPagesWork.projectsSubtitle')}</p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Button
            variant="primary"
            disabled={act.disabled}
            title={act.reason ?? undefined}
            onClick={() => setCreating(true)}
          >
            <Plus />
            {t('webPagesWork.projectsNew')}
          </Button>
          {act.reason ? <span className="text-small text-text-muted">{act.reason}</span> : null}
        </div>
      </div>
      {projects.isError ? (
        <ErrorState
          title={t('webPagesWork.projectsLoadFailed')}
          message={errorMessage(projects.error)}
          onRetry={() => void projects.refetch()}
          retrying={projects.isFetching}
        />
      ) : null}
      <div className="grid gap-4 md:grid-cols-[minmax(14rem,18rem)_1fr]">
        {/* On a phone the list and the detail take turns: the URL decides. */}
        <section
          aria-label={t('webPagesWork.projectsList')}
          className={cn(projectId && 'max-md:hidden')}
        >
          {list}
        </section>
        <section className={cn('min-w-0', !projectId && 'max-md:hidden')}>
          {projectId ? (
            <Link
              to="/projects"
              className="target mb-2 inline-flex items-center gap-1 text-small text-text-muted md:hidden"
            >
              <ArrowLeft className="size-4" aria-hidden />
              {t('webPagesWork.backToProjects')}
            </Link>
          ) : null}
          {detail}
        </section>
      </div>
      {creating ? (
        <CreateProjectDialog agents={agents.data ?? []} onClose={() => setCreating(false)} />
      ) : null}
    </div>
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

  const projectSessions = sessionsInProject(sessions, id) as SessionRow[]
  const groups = groupProjectSessionsByAgent(projectSessions)
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
    setSaving('name')
    setError(null)
    try {
      const res = await update({ name: next }, updatedAtOf(project))
      applyFresh(res?.project)
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

  return (
    <article aria-label={name} className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <label htmlFor={nameFieldId} className="text-small text-text-muted">
          {t('webPagesWork.projectName')}
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <input
            id={nameFieldId}
            className={cn(fieldClass, 'h-10 min-w-0 flex-1 text-lg font-semibold')}
            value={nameDraft ?? name}
            maxLength={PROJECT_NAME_MAX}
            onChange={(e) => setNameDraft(e.target.value)}
          />
          {nameDraft !== null && nameDraft !== name ? (
            <Button
              variant="outline"
              disabled={nameGate.disabled || saving !== null}
              title={nameGate.reason ?? undefined}
              onClick={() => void saveName()}
            >
              {saving === 'name' ? <Loader2 className="animate-spin" /> : null}
              {t('webPagesWork.saveName')}
            </Button>
          ) : null}
        </div>
        <span className="text-small text-text-muted">
          {[
            projectAgentId(project)
              ? t('webPagesWork.projectOwner', { agent: projectAgentId(project) })
              : '',
            updatedAtOf(project)
              ? t('webPagesWork.projectUpdated', { time: relTimeLabel(updatedAtOf(project)) })
              : '',
          ]
            .filter(Boolean)
            .join(' · ')}
        </span>
      </div>

      {error ? (
        <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2">
          {error}
        </p>
      ) : null}

      <div className="flex flex-col gap-1">
        <label htmlFor={knowledgeFieldId} className="font-medium">
          {t('webPagesWork.knowledge')}
        </label>
        <p className="text-small text-text-muted">{t('webPagesWork.knowledgeHint')}</p>
        <textarea
          id={knowledgeFieldId}
          rows={8}
          value={knowledgeValue}
          maxLength={PROJECT_KNOWLEDGE_MAX}
          placeholder={t('webPagesWork.knowledgePlaceholder')}
          aria-describedby={counterId}
          onChange={(e) =>
            setDraft((d) => ({ text: e.target.value, base: d?.base ?? updatedAtOf(project) }))
          }
          className={cn(fieldClass, 'py-2 font-mono text-small')}
        />
        {conflict ? (
          <div
            role="alert"
            className="flex flex-col gap-2 rounded-md border border-warn/50 bg-warn/10 p-3"
          >
            <p className="font-medium">{t('webPagesWork.conflictTitle')}</p>
            <p className="text-text-muted">{t('webPagesWork.conflictBody')}</p>
            <p className="text-small font-medium">{t('webPagesWork.conflictTheirs')}</p>
            <pre
              aria-label={t('webPagesWork.conflictTheirs')}
              className="max-h-48 overflow-auto rounded border border-border bg-surface-0 p-2 font-mono text-small whitespace-pre-wrap"
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
        <div className="flex flex-wrap items-center gap-2">
          <span id={counterId} className="flex-1 text-small text-text-muted tabular-nums">
            {t('webPagesWork.knowledgeCounter', {
              count: knowledgeValue.length,
              max: PROJECT_KNOWLEDGE_MAX,
            })}
            {dirty ? ` · ${t('webPagesWork.unsaved')}` : ''}
          </span>
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
      </div>

      <section aria-label={t('webPagesWork.projectSessions')} className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="flex-1 font-medium">{t('webPagesWork.projectSessions')}</h2>
          <Button
            size="sm"
            variant="primary"
            onClick={() => navigate(`/new?project=${encodeURIComponent(id)}`)}
          >
            <MessageSquarePlus />
            {t('webPagesWork.newChatInProject')}
          </Button>
        </div>
        {groups.length === 0 ? (
          <p className="text-text-muted">{t('webPagesWork.projectNoSessions')}</p>
        ) : (
          groups.map((group) => (
            <div key={group.agentId}>
              <h3 className="text-small font-medium text-text-muted">
                {t('webPagesWork.agentGroup', { agent: group.agentId })}
              </h3>
              <ul role="list" className="flex flex-col">
                {(group.items as SessionRow[]).map((s) => (
                  <li key={s.key}>
                    <Link
                      to={sessionKeyToPath(s.key)}
                      className="target flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-surface-2"
                    >
                      <MessageSquare className="size-4 shrink-0 text-text-muted" aria-hidden />
                      <span className="min-w-0 flex-1 truncate">{sessionTitle(s)}</span>
                      <span className="text-small text-text-muted">
                        {relTimeLabel(s.updated_at ?? '')}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </section>

      <div className="border-t border-border pt-4">
        <Button
          variant="outline"
          className="text-danger"
          disabled={act.disabled}
          title={act.reason ?? undefined}
          onClick={() => setConfirmDelete(true)}
        >
          <Trash2 />
          {t('webPagesWork.deleteProject')}
        </Button>
      </div>

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
          <div className="flex flex-col gap-1">
            <label htmlFor={nameId} className="font-medium">
              {t('webPagesWork.projectName')}
            </label>
            <input
              id={nameId}
              autoFocus
              className={cn(fieldClass, 'h-9 touch:h-11')}
              value={name}
              maxLength={PROJECT_NAME_MAX}
              placeholder={t('webPagesWork.projectNamePlaceholder')}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={agentFieldId} className="font-medium">
              {t('webPagesWork.projectAgent')}
            </label>
            <select
              id={agentFieldId}
              className={cn(fieldClass, 'h-9 touch:h-11')}
              value={agentId}
              onChange={(e) => setAgentId(e.target.value)}
            >
              {agentChoices.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
            <span className="text-small text-text-muted">{t('webPagesWork.projectAgentHint')}</span>
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={knowledgeId} className="font-medium">
              {t('webPagesWork.knowledge')}
            </label>
            <textarea
              id={knowledgeId}
              rows={5}
              className={cn(fieldClass, 'py-2')}
              value={knowledge}
              maxLength={PROJECT_KNOWLEDGE_MAX}
              placeholder={t('webPagesWork.knowledgePlaceholder')}
              onChange={(e) => setKnowledge(e.target.value)}
            />
          </div>
          {error ? (
            <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2">
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
