import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CalendarClock,
  Copy,
  History,
  Loader2,
  Pause,
  Pencil,
  Play,
  Plus,
  Search,
  Trash2,
  Zap,
} from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { formatNumber, t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { copyWithFallback } from '@/lib/clipboard'
import { gate, requireConnection, requireScheduler, type Gate } from '@/lib/disabled-reason'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { cn } from '@/lib/utils'
import { RpcError } from '@/lib/ws-rpc'
import {
  activeChatSessionKey,
  CONCURRENCY_POLICIES,
  concurrencyLabel,
  explainCron,
  filterJobs,
  humanCountdownPast,
  isOkStatus,
  isUpcomingRun,
  jobConcurrency,
  jobCreatedFrom,
  jobDotState,
  jobElevated,
  jobKindLabel,
  jobRuntimeHealth,
  jobSchedule,
  jobTarget,
  nextRunAbs,
  nextRunText,
  shortCronId,
  sortJobs,
  type ConcurrencyPolicy,
  type DeliveryTargetMap,
  type RawJob,
  type SortCol,
} from '@/views/cron/logic'
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
import { PageHeader } from '@/web/components/ui/page-header'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { AutomationEditor, type SaveRequest } from './AutomationEditor'
import { RunHistory } from './RunHistory'

// `/automations` (task 8.5) on views/cron/logic: job cards with run health,
// the create / edit sheet, run now, pause and resume, delete, run history, and
// a live refresh on `cron.run.finished`. With no jobs the page explains what
// lands here and offers New automation plus the legacy quick-start presets.

const JOBS_QUERY_KEY = ['cron', 'jobs'] as const
const TARGETS_QUERY_KEY = ['cron', 'deliveryTargets'] as const
/** The run's record (and health) lands shortly after `cron.run.finished`. */
const RUN_RECORD_SETTLE_MS = 1500
/** Where the chat workspace records the last open session (cron's "current session"). */
const ACTIVE_SESSION_STORAGE = 'agentos.ui.lastSession'

type Editor =
  | { kind: 'closed' }
  | { kind: 'create'; template: Partial<RawJob> | null }
  | { kind: 'edit'; job: RawJob }

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function readActiveSessionKey(): string {
  try {
    return activeChatSessionKey('', localStorage.getItem(ACTIVE_SESSION_STORAGE) || '')
  } catch {
    return ''
  }
}

/** The three empty-state quick starts; each seeds the create sheet. */
function emptyTemplates(): (Partial<RawJob> & { expression: string; hint: string })[] {
  return [
    {
      name: t('webPagesWork.templateStandupName'),
      expression: '0 9 * * 1-5',
      payloadKind: 'reminder',
      message: t('webPagesWork.templateStandupMessage'),
      hint: t('webPagesWork.templateStandupHint'),
    },
    {
      name: t('webPagesWork.templateHealthName'),
      expression: '0 * * * *',
      payloadKind: 'agent_turn',
      message: t('webPagesWork.templateHealthMessage'),
      hint: t('webPagesWork.templateHealthHint'),
    },
    {
      name: t('webPagesWork.templateWrapName'),
      expression: '0 17 * * 5',
      payloadKind: 'agent_turn',
      message: t('webPagesWork.templateWrapMessage'),
      hint: t('webPagesWork.templateWrapHint'),
    },
  ]
}

function sortOptions(): { value: SortCol; label: string }[] {
  return [
    { value: 'next_run', label: t('webPagesWork.sortNextRun') },
    { value: 'name', label: t('webPagesWork.sortName') },
    { value: 'last_run', label: t('webPagesWork.sortLastRun') },
    { value: 'payloadKind', label: t('webPagesWork.sortKind') },
    { value: 'expression', label: t('webPagesWork.sortSchedule') },
  ]
}

export function AutomationsPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const connection = useDisplayedConnection()
  // The scheduler is optional on a gateway. We learn it is off from the
  // gateway's own UNAVAILABLE answer, tied to the connection it came from.
  const connectedAt = useRuntimeSignals((s) => s.connectedAt)
  const [schedulerDownAt, setSchedulerDownAt] = useState<number | null | undefined>(undefined)
  const schedulerDown = schedulerDownAt !== undefined && schedulerDownAt === connectedAt
  const control = gate(requireConnection(connection), requireScheduler(schedulerDown))
  const noteSchedulerError = (e: unknown) => {
    if (e instanceof RpcError && e.code === 'UNAVAILABLE')
      setSchedulerDownAt(useRuntimeSignals.getState().connectedAt)
  }

  const [search, setSearch] = useState('')
  const [sortCol, setSortCol] = useState<SortCol>('next_run')
  const [sortAsc, setSortAsc] = useState(true)
  const [editor, setEditor] = useState<Editor>({ kind: 'closed' })
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [history, setHistory] = useState<RawJob | null>(null)
  const [pendingDelete, setPendingDelete] = useState<RawJob | null>(null)
  const [deleting, setDeleting] = useState(false)
  const reasonId = useId()

  const jobs = useQuery({
    queryKey: JOBS_QUERY_KEY,
    queryFn: async (): Promise<RawJob[]> => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawJob[] | { jobs?: RawJob[] }>('cron.list', {})
      return Array.isArray(data) ? data : (data?.jobs ?? [])
    },
  })

  // Known recipients per channel. A failure is quiet: the form falls back to
  // a text box for the chat id.
  const targets = useQuery({
    queryKey: TARGETS_QUERY_KEY,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<{ targets?: DeliveryTargetMap }>('channels.deliveryTargets', {})
    },
    staleTime: 60_000,
  })

  // cron.subscribe for the page's lifetime (best effort before the handshake).
  useEffect(() => {
    let cancelled = false
    rpc
      .waitForConnection()
      .then(() => (cancelled ? undefined : rpc.call('cron.subscribe', {})))
      .catch(() => {
        /* the subscription is best effort */
      })
    return () => {
      cancelled = true
      rpc.call('cron.unsubscribe', {}).catch(() => {
        /* best effort while disconnecting */
      })
    }
  }, [rpc])

  // A finished run refreshes jobs and any open history at once, then once
  // more after the scheduler has recorded the result (its health streak).
  useEffect(() => {
    const settles = new Set<number>()
    const off = rpc.on('cron.run.finished', () => {
      void queryClient.invalidateQueries({ queryKey: ['cron'] })
      const id = window.setTimeout(() => {
        settles.delete(id)
        void queryClient.invalidateQueries({ queryKey: ['cron'] })
      }, RUN_RECORD_SETTLE_MS)
      settles.add(id)
    })
    return () => {
      off()
      for (const id of settles) window.clearTimeout(id)
    }
  }, [rpc, queryClient])

  const refresh = () => queryClient.invalidateQueries({ queryKey: JOBS_QUERY_KEY })
  const list = useMemo(() => jobs.data ?? [], [jobs.data])
  const visible = useMemo(
    () => sortJobs(filterJobs(list, search), sortCol, sortAsc),
    [list, search, sortCol, sortAsc],
  )

  const openCreate = (template: Partial<RawJob> | null) => {
    setSaveError(null)
    setEditor({ kind: 'create', template })
  }

  const save = async (request: SaveRequest) => {
    setSaving(true)
    setSaveError(null)
    try {
      await rpc.call(request.method, request.payload)
      toast.success(
        request.method === 'cron.update'
          ? t('webPagesWork.automationUpdated')
          : t('webPagesWork.automationCreated'),
      )
      setEditor({ kind: 'closed' })
      void refresh()
    } catch (e) {
      noteSchedulerError(e)
      setSaveError(t('webPagesWork.automationSaveFailed', { message: errorMessage(e) }))
    } finally {
      setSaving(false)
    }
  }

  const remove = async (job: RawJob) => {
    setDeleting(true)
    try {
      await rpc.call('cron.remove', { id: job.id })
      toast.success(t('webPagesWork.automationDeleted'))
      if (history?.id === job.id) setHistory(null)
      setPendingDelete(null)
      void refresh()
    } catch (e) {
      setPendingDelete(null)
      toast.error(t('webPagesWork.automationDeleteFailed', { message: errorMessage(e) }))
    } finally {
      setDeleting(false)
    }
  }

  const total = list.length
  const enabledCount = list.filter((j) => j.enabled).length
  const upcoming = list.filter((j) => isUpcomingRun(j)).length
  const degraded = list.filter((j) => jobRuntimeHealth(j)?.degraded).length

  let body
  if (jobs.isPending) {
    body = (
      <LoadingRegion label={t('webPagesWork.automationsLoading')}>
        <div className="grid gap-3 md:grid-cols-2">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-44" />
          ))}
        </div>
      </LoadingRegion>
    )
  } else if (total === 0 && jobs.isError) {
    // The error notice above says what failed; there is nothing to show under it.
    body = null
  } else if (total === 0) {
    body = (
      <EmptyState
        icon={<CalendarClock />}
        title={t('webPagesWork.automationsEmpty')}
        description={t('webPagesWork.automationsEmptyBody')}
        action={
          <div className="flex flex-col items-center gap-4">
            <Button variant="primary" onClick={() => openCreate(null)}>
              <Plus />
              {t('webPagesWork.automationsNew')}
            </Button>
            <div className="flex flex-col items-center gap-2">
              <span className="text-small text-text-muted">{t('webPagesWork.presetsLabel')}</span>
              <ul role="list" className="flex flex-col gap-2">
                {emptyTemplates().map((tpl) => (
                  <li key={tpl.expression}>
                    <button
                      type="button"
                      onClick={() => openCreate(tpl)}
                      className="target flex w-full flex-col items-start gap-0.5 rounded-md border border-border px-3 py-2 text-left hover:bg-surface-2"
                    >
                      <span className="font-medium">{tpl.name}</span>
                      <span className="text-small text-text-muted">
                        <code className="font-mono">{tpl.expression}</code> · {tpl.hint}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        }
      />
    )
  } else {
    body = (
      <>
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            { label: t('webPagesWork.statActive'), value: enabledCount },
            { label: t('webPagesWork.statPaused'), value: total - enabledCount },
            { label: t('webPagesWork.statUpcoming'), value: upcoming },
            { label: t('webPagesWork.statDegraded'), value: degraded },
          ].map((tile) => (
            <div
              key={tile.label}
              className="rounded-lg border border-border bg-surface-1 px-3 py-2"
            >
              <dt className="text-small text-text-muted">{tile.label}</dt>
              <dd className="text-lg font-semibold tabular-nums">{formatNumber(tile.value)}</dd>
            </div>
          ))}
        </dl>
        <div className="flex flex-wrap items-center gap-2">
          <label className="relative block min-w-0 flex-[1_1_14rem]">
            <span className="sr-only">{t('webPagesWork.automationsSearch')}</span>
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-muted"
              aria-hidden
            />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('webPagesWork.automationsSearch')}
              className="h-9 w-full rounded-md border border-border-control bg-surface-0 pr-2 pl-8 touch:h-11 max-md:text-base"
            />
          </label>
          <label className="flex items-center gap-2">
            <span className="text-small text-text-muted">{t('webPagesWork.sortBy')}</span>
            <select
              className="h-9 rounded-md border border-border-control bg-surface-0 px-2 touch:h-11"
              value={sortCol}
              onChange={(e) => setSortCol(e.target.value as SortCol)}
            >
              {sortOptions().map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <Button
            size="icon"
            variant="outline"
            aria-label={
              sortAsc ? t('webPagesWork.sortAscending') : t('webPagesWork.sortDescending')
            }
            onClick={() => setSortAsc((v) => !v)}
          >
            {sortAsc ? <ArrowUp /> : <ArrowDown />}
          </Button>
        </div>
        {visible.length === 0 ? (
          <EmptyState
            title={t('webPagesWork.automationsNoMatch')}
            action={
              <Button variant="outline" onClick={() => setSearch('')}>
                {t('webPagesWork.clearFilters')}
              </Button>
            }
          />
        ) : (
          <ul role="list" className="grid gap-3 md:grid-cols-2">
            {visible.map((job, i) => (
              <li key={String(job.id ?? i)} className="min-w-0">
                <JobCard
                  job={job}
                  control={control}
                  onSchedulerError={noteSchedulerError}
                  onChanged={() => void refresh()}
                  onEdit={() => {
                    setSaveError(null)
                    setEditor({ kind: 'edit', job })
                  }}
                  onHistory={() => setHistory(job)}
                  onDelete={() => setPendingDelete(job)}
                />
              </li>
            ))}
          </ul>
        )}
      </>
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-6">
      <PageHeader
        title={t('webPagesWork.automationsTitle')}
        description={t('webPagesWork.automationsSubtitle')}
        actions={
          <Button
            variant="primary"
            aria-describedby={control.reason ? reasonId : undefined}
            onClick={() => openCreate(null)}
          >
            <Plus />
            {t('webPagesWork.automationsNew')}
          </Button>
        }
        note={
          control.reason ? (
            <span id={reasonId} className="text-small text-text-muted">
              {control.reason}
            </span>
          ) : null
        }
      />
      {jobs.isError ? (
        <ErrorState
          title={t('webPagesWork.automationsLoadFailed')}
          message={errorMessage(jobs.error)}
          onRetry={() => void jobs.refetch()}
          retrying={jobs.isFetching}
        />
      ) : null}
      {body}

      {editor.kind !== 'closed' ? (
        <AutomationEditor
          key={editor.kind === 'edit' ? `edit:${String(editor.job.id)}` : 'create'}
          job={editor.kind === 'edit' ? editor.job : null}
          template={editor.kind === 'create' ? editor.template : null}
          activeSessionKey={readActiveSessionKey()}
          deliveryTargets={targets.data?.targets}
          saving={saving}
          saveError={saveError}
          control={control}
          onCancel={() => setEditor({ kind: 'closed' })}
          onSubmit={(request) => void save(request)}
        />
      ) : null}

      {history ? (
        <RunHistory
          jobId={String(history.id ?? '')}
          jobName={String(history.name || history.id || '')}
          onClose={() => setHistory(null)}
        />
      ) : null}

      <AlertDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => !open && !deleting && setPendingDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('webPagesWork.deleteAutomationTitle', {
                name: String(pendingDelete?.name || pendingDelete?.id || ''),
              })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('webPagesWork.deleteAutomationBody')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>{t('webPagesWork.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              disabled={deleting}
              onClick={(e) => {
                e.preventDefault()
                if (pendingDelete) void remove(pendingDelete)
              }}
            >
              {deleting ? <Loader2 className="animate-spin" /> : null}
              {t('webPagesWork.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

// ── One job ──────────────────────────────────────────────────────────────────

function JobCard({
  job,
  control,
  onSchedulerError,
  onChanged,
  onEdit,
  onHistory,
  onDelete,
}: {
  job: RawJob
  /** Run, pause and overlap need a connected gateway with a running scheduler. */
  control: Gate
  onSchedulerError: (e: unknown) => void
  onChanged: () => void
  onEdit: () => void
  onHistory: () => void
  onDelete: () => void
}) {
  const rpc = useRpc()
  const reasonId = useId()
  const [busy, setBusy] = useState<'run' | 'toggle' | 'concurrency' | null>(null)
  const [status, setStatus] = useState<{ tone: 'ok' | 'warn' | 'danger'; text: string } | null>(
    null,
  )

  const id = String(job.id ?? '')
  const name = String(job.name || job.id || '')
  const enabled = !!job.enabled
  const dot = jobDotState(job)
  const health = jobRuntimeHealth(job)
  const concurrency = jobConcurrency(job)
  const elevated = jobElevated(job)
  const createdFrom = jobCreatedFrom(job)
  const human = explainCron(job.expression || '')
  const lastStatus = job.last_status || (job.last_run ? 'ok' : null)
  const message = String(job.message || job.prompt || '').trim()
  const failures = health ? tPlural('webPagesWork.failures', health.consecutiveFailures) : ''

  const act = async (
    kind: 'run' | 'toggle' | 'concurrency',
    fn: () => Promise<{ tone: 'ok' | 'warn' | 'danger'; text: string } | null>,
  ) => {
    setBusy(kind)
    setStatus(null)
    try {
      setStatus(await fn())
      onChanged()
    } catch (e) {
      onSchedulerError(e)
      setStatus({
        tone: 'danger',
        text: t('webPagesWork.actionFailed', { message: errorMessage(e) }),
      })
    } finally {
      setBusy(null)
    }
  }

  const runNow = () =>
    act('run', async () => {
      const res = await rpc.call<{ reply?: string; error?: string } | null>('cron.run', { id })
      if (res?.reply)
        return { tone: 'ok', text: t('webPagesWork.runReply', { reply: res.reply.slice(0, 160) }) }
      if (res?.error)
        return { tone: 'danger', text: t('webPagesWork.runError', { error: res.error }) }
      return { tone: 'ok', text: t('webPagesWork.runTriggered') }
    })
  const toggle = () =>
    act('toggle', async () => {
      await rpc.call('cron.update', { id, enabled: !enabled })
      return { tone: 'ok', text: enabled ? t('webPagesWork.paused') : t('webPagesWork.resumed') }
    })
  const setConcurrency = (policy: ConcurrencyPolicy) =>
    act('concurrency', async () => {
      await rpc.call('cron.update', { id, concurrency: policy })
      return null
    })
  const copyId = async () => {
    try {
      await copyWithFallback(id)
      toast.success(t('webPagesWork.copiedId'))
    } catch (e) {
      setStatus({ tone: 'warn', text: t('webPagesWork.copyFailed', { message: errorMessage(e) }) })
    }
  }

  const dotClass =
    health?.degraded || dot === 'error' ? 'bg-danger' : dot === 'off' ? 'bg-text-muted' : 'bg-ok'
  const stateLabel = health?.degraded
    ? t('webPagesWork.stateDegraded')
    : dot === 'error'
      ? t('webPagesWork.stateFailing')
      : enabled
        ? t('webPagesWork.stateActive')
        : t('webPagesWork.statePaused')

  return (
    <article
      aria-label={name}
      data-degraded={health?.degraded ? 'true' : undefined}
      className={cn(
        'flex h-full flex-col gap-3 rounded-lg border bg-surface-1 p-4',
        health?.degraded ? 'border-danger/50' : 'border-border',
      )}
    >
      <header className="flex flex-wrap items-center gap-2">
        <span
          role="img"
          aria-label={stateLabel}
          className={cn('size-2.5 shrink-0 rounded-full', dotClass)}
        />
        <h2 className="min-w-0 flex-1 truncate font-semibold">{name}</h2>
        <span className="rounded-full bg-surface-3 px-2 text-small">{jobKindLabel(job)}</span>
        {elevated ? (
          <span className="rounded-full border border-danger/50 px-2 text-small text-danger">
            {t('webPagesWork.elevated', { mode: elevated })}
          </span>
        ) : null}
        {health?.degraded ? (
          <span
            className="inline-flex items-center gap-1 rounded-full border border-danger/50 px-2 text-small text-danger"
            title={failures}
          >
            <AlertTriangle className="size-3" aria-hidden data-status-icon />
            {t('webPagesWork.stateDegraded')}
          </span>
        ) : null}
      </header>

      <div className="flex flex-col gap-0.5">
        <code className="font-mono text-small">{jobSchedule(job)}</code>
        {human ? <span className="text-small text-text-muted">{human}</span> : null}
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-small">
        <dt className="text-text-muted">{t('webPagesWork.cardNextRun')}</dt>
        <dd className="min-w-0">
          {enabled ? (
            <>
              {nextRunText(job)}
              {nextRunAbs(job) ? (
                <span className="text-text-muted"> · {nextRunAbs(job)}</span>
              ) : null}
            </>
          ) : (
            <span className="text-text-muted">{t('webPagesWork.statePaused')}</span>
          )}
        </dd>
        <dt className="text-text-muted">{t('webPagesWork.cardLastRun')}</dt>
        <dd className="min-w-0">
          {job.last_run
            ? humanCountdownPast(new Date(job.last_run as string | number))
            : t('webPagesWork.dash')}
          {lastStatus ? (
            <span className={isOkStatus(lastStatus) ? 'text-ok' : 'text-danger'}>
              {' · '}
              {lastStatus}
            </span>
          ) : null}
        </dd>
        {health && (health.degraded || health.consecutiveFailures > 0 || job.last_run) ? (
          <>
            <dt className="text-text-muted">{t('webPagesWork.cardHealth')}</dt>
            <dd className={health.consecutiveFailures ? 'text-danger' : 'text-ok'}>
              {health.consecutiveFailures ? failures : t('webPagesWork.healthy')}
            </dd>
          </>
        ) : null}
        {health?.lastError ? (
          <>
            <dt className="text-text-muted">{t('webPagesWork.cardLastError')}</dt>
            <dd className="line-clamp-3 min-w-0 break-words text-danger">{health.lastError}</dd>
          </>
        ) : null}
        <dt className="text-text-muted">{t('webPagesWork.cardTarget')}</dt>
        <dd className="min-w-0 truncate" title={jobTarget(job)}>
          {jobTarget(job)}
        </dd>
        {createdFrom ? (
          <>
            <dt className="text-text-muted">{t('webPagesWork.cardCreatedFrom')}</dt>
            <dd className="min-w-0 truncate font-mono" title={createdFrom}>
              {createdFrom}
            </dd>
          </>
        ) : null}
        {id ? (
          <>
            <dt className="text-text-muted">{t('webPagesWork.cardId')}</dt>
            <dd className="flex min-w-0 items-center gap-1 font-mono" title={id}>
              {shortCronId(id)}
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={t('webPagesWork.copyId', { id })}
                onClick={() => void copyId()}
              >
                <Copy />
              </Button>
            </dd>
          </>
        ) : null}
        {concurrency ? (
          <>
            <dt className="text-text-muted">{t('webPagesWork.cardOverlap')}</dt>
            <dd>
              <select
                aria-label={t('webPagesWork.cardOverlapFor', { name })}
                className="h-7 rounded-md border border-border-control bg-surface-0 px-1 touch:h-11"
                value={concurrency}
                disabled={busy !== null || control.disabled}
                title={control.reason ?? undefined}
                onChange={(e) => void setConcurrency(e.target.value as ConcurrencyPolicy)}
              >
                {CONCURRENCY_POLICIES.map((policy) => (
                  <option key={policy} value={policy}>
                    {concurrencyLabel(policy)}
                  </option>
                ))}
              </select>
            </dd>
          </>
        ) : null}
      </dl>

      {message ? (
        <p className="line-clamp-3 rounded-md bg-surface-2 px-2 py-1 text-small">{message}</p>
      ) : null}

      {status ? (
        <p
          role={status.tone === 'danger' ? 'alert' : 'status'}
          className={cn(
            'text-small break-words',
            status.tone === 'danger' ? 'text-danger' : status.tone === 'warn' ? 'text-warn' : '',
          )}
        >
          {status.text}
        </p>
      ) : null}

      <footer className="mt-auto flex flex-wrap items-center gap-1.5">
        <Button
          size="sm"
          variant="outline"
          disabled={busy !== null || control.disabled}
          aria-describedby={control.reason ? reasonId : undefined}
          aria-label={t('webPagesWork.runNowFor', { name })}
          onClick={() => void runNow()}
        >
          {busy === 'run' ? <Loader2 className="animate-spin" /> : <Zap />}
          {t('webPagesWork.runNow')}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={busy !== null || control.disabled}
          aria-describedby={control.reason ? reasonId : undefined}
          aria-label={
            enabled ? t('webPagesWork.pauseFor', { name }) : t('webPagesWork.resumeFor', { name })
          }
          onClick={() => void toggle()}
        >
          {enabled ? <Pause /> : <Play />}
          {enabled ? t('webPagesWork.pause') : t('webPagesWork.resume')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label={t('webPagesWork.historyFor', { name })}
          onClick={onHistory}
        >
          <History />
          {t('webPagesWork.history')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label={t('webPagesWork.editFor', { name })}
          onClick={onEdit}
        >
          <Pencil />
          {t('webPagesWork.edit')}
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          className="ml-auto text-danger"
          aria-label={t('webPagesWork.deleteFor', { name })}
          onClick={onDelete}
        >
          <Trash2 />
        </Button>
        {control.reason ? (
          <span id={reasonId} className="w-full text-small text-text-muted">
            {control.reason}
          </span>
        ) : null}
      </footer>
    </article>
  )
}
