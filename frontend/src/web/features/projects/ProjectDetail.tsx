import { useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  Loader2,
  MessageSquare,
  MessageSquarePlus,
  MessagesSquare,
  Trash2,
} from 'lucide-react'
import { useId, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { formatNumber, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { blockedBecause, gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  groupProjectSessionsByAgent,
  projectAgentId,
  projectId as projectIdOf,
  projectName,
  projectSessionCount,
  sessionsInProject,
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
import { Badge, StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { Textarea } from '@/web/components/ui/input'
import { SectionHeader } from '@/web/components/ui/section-header'
import { createSessionOptimistic, SESSIONS_QUERY_KEY } from '@/web/shell/session-data'
import { sessionChannel, sessionTitle, type SessionRow } from '@/web/shell/session-list'
import { ChannelIcon } from '@/web/shell/channel-icon'
import { sessionKeyToPath } from '@/web/shell/session-url'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { hueOf } from './hue'
import {
  errorMessage,
  PROJECT_KNOWLEDGE_MAX,
  PROJECT_NAME_MAX,
  PROJECT_ROWS_QUERY_KEY,
  projectPath,
  ProjectTile,
  sessionTone,
  updatedAtOf,
} from './project-ui'

// `/projects/:projectId`: a header band (breadcrumb with a project switcher,
// the mark, the name edited in place, the facts), then the project's
// sessions and its shared knowledge, with the session mix, the id and
// delete beside them.

function isConflict(error: unknown): boolean {
  return (error as { code?: string } | null)?.code === 'project.conflict'
}

/** A knowledge save lost the compare-and-swap: someone else saved first. */
interface Conflict {
  theirs: string
}

export function ProjectDetail({
  project,
  projects,
  sessions,
  refetch,
}: {
  project: RawProject
  /** Every project, for the switcher in the breadcrumb. */
  projects: RawProject[]
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
  const updated = updatedAtOf(project)
  const others = projects.filter((p) => projectIdOf(p) !== id)

  return (
    <article aria-label={name} className="anim-enter flex flex-col gap-4">
      <header className="pj-band" data-hue={hueOf(name)}>
        <nav aria-label={t('webPagesWork.projectBreadcrumb')}>
          <ol className="flex min-w-0 items-center gap-1 text-small">
            <li>
              <Link
                to="/projects"
                viewTransition
                className="press target inline-flex items-center rounded-sm px-1 text-text-muted hover:text-text"
              >
                {t('webPagesWork.backToProjects')}
              </Link>
            </li>
            <li aria-hidden className="text-text-muted">
              <ChevronRight className="size-3.5" />
            </li>
            <li className="min-w-0">
              {others.length ? (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      type="button"
                      aria-label={t('webPagesWork.projectSwitch')}
                      className="press target inline-flex max-w-full items-center gap-1 rounded-sm px-1 font-medium hover:bg-surface-3 data-[state=open]:bg-surface-3"
                    >
                      <span aria-current="page" className="truncate">
                        {name}
                      </span>
                      <ChevronDown className="size-3.5 shrink-0 text-text-muted" aria-hidden />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="max-h-80 min-w-60 overflow-y-auto">
                    <DropdownMenuLabel>{t('webPagesWork.projectSwitch')}</DropdownMenuLabel>
                    {others.map((p) => (
                      <DropdownMenuItem key={projectIdOf(p)} asChild>
                        <Link to={projectPath(p)} viewTransition>
                          <ProjectTile
                            name={projectName(p)}
                            size="sm"
                            className="!size-6 !text-small"
                          />
                          <span className="min-w-0 flex-1 truncate">{projectName(p)}</span>
                          <span className="text-small text-text-muted tabular-nums">
                            {formatNumber(projectSessionCount(p))}
                          </span>
                        </Link>
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              ) : (
                <span aria-current="page" className="block truncate px-1 font-medium">
                  {name}
                </span>
              )}
            </li>
          </ol>
        </nav>

        <div className="flex items-start gap-4">
          <ProjectTile name={nameDraft ?? name} size="lg" className="max-sm:hidden" />
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <label htmlFor={nameFieldId} className="sr-only">
              {t('webPagesWork.projectName')}
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <input
                id={nameFieldId}
                className="pj-name-field min-w-0 flex-[1_1_14rem]"
                value={nameDraft ?? name}
                maxLength={PROJECT_NAME_MAX}
                onChange={(e) => setNameDraft(e.target.value)}
                onKeyDown={(e) => {
                  // Enter saves the name, as Save name does; not mid-composition.
                  if (e.key === 'Escape' && nameDraft !== null) {
                    e.stopPropagation()
                    setNameDraft(null)
                    return
                  }
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
            <p className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
              <span className="pj-fact">
                <Bot aria-hidden />
                {owner ? (
                  <strong>{t('webPagesWork.projectOwner', { agent: owner })}</strong>
                ) : (
                  t('webPagesWork.projectNoOwner')
                )}
              </span>
              <span className="pj-fact">
                <MessageSquare aria-hidden />
                <strong className="tabular-nums">
                  {tPlural('webPagesWork.sessionCount', sessionTotal)}
                </strong>
              </span>
              {updated ? (
                <span className="pj-fact">
                  <Clock aria-hidden />
                  {t('webPagesWork.projectUpdated', { time: relTimeLabel(updated) })}
                </span>
              ) : null}
            </p>
          </div>
        </div>
      </header>

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

      <div className="pj-detail-grid">
        <div className="flex min-w-0 flex-col gap-4">
          <Card asChild padding="none">
            <section aria-label={t('webPagesWork.projectSessions')} className="flex flex-col">
              <div className="flex flex-wrap items-center gap-2 px-5 pt-4 pb-2">
                <h2 className="flex min-w-0 flex-[1_1_14rem] items-center gap-2 text-section [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-text-muted">
                  <MessagesSquare aria-hidden />
                  {t('webPagesWork.projectSessions')}
                  <span className="text-small font-normal text-text-muted tabular-nums">
                    {formatNumber(sessionTotal)}
                  </span>
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
                <p
                  role="note"
                  className="mx-5 mb-2 flex items-start gap-2 text-small text-text-muted"
                >
                  {t('webPagesWork.projectSessionsPartial', {
                    shown: projectSessions.length,
                    total: projectSessionCount(project),
                  })}
                </p>
              ) : null}
              {groups.length === 0 ? (
                partial ? null : (
                  <div className="flex flex-col items-center gap-2 px-5 pt-4 pb-7 text-center">
                    <span
                      aria-hidden
                      className="grid size-10 place-items-center rounded-card border border-border bg-surface-2 text-text-muted [&_svg]:size-5"
                    >
                      <MessagesSquare />
                    </span>
                    <p className="font-medium">{t('webPagesWork.sessionsEmpty')}</p>
                    <p className="max-w-xs text-pretty text-small text-text-muted">
                      {t('webPagesWork.projectSessionsEmptyHint')}
                    </p>
                  </div>
                )
              ) : (
                <div className="flex flex-col gap-3 px-2.5 pb-3">
                  {groups.map((group) => (
                    <div key={group.agentId} className="flex flex-col gap-0.5">
                      <SectionHeader
                        as="h3"
                        size="sm"
                        title={t('webPagesWork.agentGroup', { agent: group.agentId })}
                        className="px-2.5 pt-2 pb-1"
                      />
                      <ul role="list" className="anim-stagger flex flex-col">
                        {(group.items as SessionRow[]).map((s) => (
                          <li key={s.key}>
                            <Link
                              to={sessionKeyToPath(s.key)}
                              viewTransition
                              className="pj-session press target"
                            >
                              <span
                                className="pj-tile pj-hue"
                                data-tone="soft"
                                data-size="sm"
                                data-hue={hueOf(sessionChannel(s))}
                              >
                                <ChannelIcon channel={sessionChannel(s)} className="flex" />
                              </span>
                              <span className="min-w-0 flex-1 truncate font-medium">
                                {sessionTitle(s)}
                              </span>
                              <StatusDot className={cn('size-1.5', sessionTone(s))} />
                              <span className="w-20 text-right text-small whitespace-nowrap text-text-muted">
                                {relTimeLabel(s.updated_at ?? '')}
                              </span>
                              <ArrowRight
                                aria-hidden
                                className="pj-session-arrow size-4 shrink-0"
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

          <Card padding="none" className="flex flex-col">
            <div className="flex flex-col gap-1 px-5 pt-4 pb-3">
              <label htmlFor={knowledgeFieldId} className="text-section">
                {t('webPagesWork.knowledge')}
              </label>
              <p className="text-small text-text-muted">{t('webPagesWork.knowledgeHint')}</p>
            </div>
            <div className="px-5">
              <Textarea
                id={knowledgeFieldId}
                rows={10}
                value={knowledgeValue}
                maxLength={PROJECT_KNOWLEDGE_MAX}
                placeholder={t('webPagesWork.knowledgePlaceholder')}
                aria-describedby={counterId}
                onChange={(e) =>
                  setDraft((d) => ({ text: e.target.value, base: d?.base ?? updatedAtOf(project) }))
                }
                className="font-mono text-small leading-relaxed"
              />
            </div>
            {conflict ? (
              <div
                role="alert"
                className="anim-enter mx-5 mt-3 flex flex-col gap-2 rounded-card border border-warn/30 bg-warn/10 p-3"
              >
                <p className="flex items-center gap-2 font-medium">
                  <AlertTriangle
                    className="size-4 shrink-0 text-warn"
                    aria-hidden
                    data-status-icon
                  />
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
                  variant={dirty ? 'primary' : 'outline'}
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
        </div>

        <aside className="flex min-w-0 flex-col gap-4">
          <Card asChild padding="none">
            <section
              aria-label={t('webPagesWork.projectDetails')}
              className="flex flex-col gap-4 p-4"
            >
              <SectionHeader as="h2" size="sm" title={t('webPagesWork.projectDetails')} />
              <SessionMix sessions={projectSessions} />
              <dl className="pj-facts">
                <dt>{t('webPagesWork.projectDetailAgent')}</dt>
                <dd className="truncate">{owner || t('webPagesWork.projectNoOwner')}</dd>
                <dt>{t('webPagesWork.projectDetailSessions')}</dt>
                <dd>{formatNumber(sessionTotal)}</dd>
                <dt>{t('webPagesWork.projectDetailUpdated')}</dt>
                <dd>{updated ? relTimeLabel(updated) : t('webPagesWork.dash')}</dd>
                <dt>{t('webPagesWork.projectDetailId')}</dt>
                <dd className="truncate font-mono text-text-muted" title={id}>
                  {id}
                </dd>
              </dl>
            </section>
          </Card>

          <Card
            variant="plain"
            padding="none"
            className="flex flex-col gap-3 border-danger/25 bg-danger/[0.04] p-4"
          >
            <h2 className="text-label font-medium text-danger">
              {t('webPagesWork.projectDangerTitle')}
            </h2>
            <p className="text-small text-pretty text-text-muted">
              {t('webPagesWork.deleteProjectBody')}
            </p>
            <Button
              variant="outline"
              size="sm"
              className="self-start border-danger/40 text-danger hover:bg-danger/10"
              disabled={act.disabled}
              title={act.reason ?? undefined}
              onClick={() => setConfirmDelete(true)}
            >
              <Trash2 />
              {t('webPagesWork.deleteProject')}
            </Button>
          </Card>
        </aside>
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

const MIX = [
  {
    tone: 'text-ok',
    key: 'open',
    label: (count: string) => t('webPagesWork.summaryOpen', { count }),
  },
  {
    tone: 'text-info',
    key: 'done',
    label: (count: string) => t('webPagesWork.summaryCompleted', { count }),
  },
  {
    tone: 'text-danger',
    key: 'failed',
    label: (count: string) => t('webPagesWork.summaryFailed', { count }),
  },
] as const

/** The loaded sessions' status mix as one bar with a legend. */
function SessionMix({ sessions }: { sessions: SessionRow[] }) {
  if (sessions.length === 0) return null
  const tones = sessions.map(sessionTone)
  const parts = MIX.map((m) => ({ ...m, n: tones.filter((tone) => tone === m.tone).length }))
  const other = sessions.length - parts.reduce((n, p) => n + p.n, 0)
  return (
    <div className="flex flex-col gap-2">
      <span aria-hidden className="pj-mix">
        {parts
          .filter((p) => p.n > 0)
          .map((p) => (
            <span key={p.key} className={p.tone} style={{ flexGrow: p.n }} />
          ))}
        {other > 0 ? <span className="text-text-muted" style={{ flexGrow: other }} /> : null}
      </span>
      <ul
        aria-label={t('webPagesWork.projectSessionMix')}
        className="flex flex-wrap gap-x-3 gap-y-1 text-small text-text-muted"
      >
        {parts.map((p) => (
          <li key={p.key} className="inline-flex items-center gap-1.5 tabular-nums">
            <StatusDot className={cn('size-1.5', p.tone)} />
            {p.label(formatNumber(p.n))}
          </li>
        ))}
      </ul>
    </div>
  )
}
