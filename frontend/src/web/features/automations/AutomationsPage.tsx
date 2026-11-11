import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowUp,
  BellRing,
  CalendarClock,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Copy,
  History,
  Loader2,
  Pause,
  PauseCircle,
  Pencil,
  Play,
  PlayCircle,
  Plus,
  Search,
  SearchX,
  ShieldAlert,
  Sparkles,
  Timer,
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
import { Badge, StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import { Input, Select } from '@/web/components/ui/input'
import { PageHeader } from '@/web/components/ui/page-header'
import { EmptyState, ErrorState, Skeleton } from '@/web/components/ui/states'
import { StatStrip, useArrival, WorkSkeleton } from '@/web/features/sessions/StatStrip'
import { useDocumentVisible } from '@/web/shell/session-data'
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
/**
 * The slow fallback poll. `cron.run.finished` keeps the cards current while the
 * subscription holds; this bounds how stale they get if an event is missed.
 */
export const JOBS_POLL_MS = 60_000
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
  const visibleTab = useDocumentVisible()

  const jobs = useQuery({
    queryKey: JOBS_QUERY_KEY,
    queryFn: async (): Promise<RawJob[]> => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawJob[] | { jobs?: RawJob[] }>('cron.list', {})
      return Array.isArray(data) ? data : (data?.jobs ?? [])
    },
    refetchInterval: visibleTab ? JOBS_POLL_MS : false,
    refetchIntervalInBackground: false,
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

  // cron.subscribe on every connection while the page is open. The gateway
  // keeps subscriptions per connection, so a reconnect (a new `connectedAt`)
  // starts without one and must subscribe again (the shell refetches queries
  // on a reconnect, which covers runs that finished while the socket was down).
  useEffect(() => {
    let cancelled = false
    rpc
      .waitForConnection()
      .then(() => (cancelled ? undefined : rpc.call('cron.subscribe', {})))
      .catch(() => {
        /* the subscription is best effort; the slow poll covers a miss */
      })
    return () => {
      cancelled = true
    }
  }, [rpc, connectedAt])
  useEffect(
    () => () => {
      rpc.call('cron.unsubscribe', {}).catch(() => {
        /* best effort while disconnecting */
      })
    },
    [rpc],
  )

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
  // Cards rise in once, when the jobs first arrive; never on a poll or a tick.
  const arriving = useArrival(!jobs.isPending)

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
      <WorkSkeleton label={t('webPagesWork.automationsLoading')} cells={4}>
        <div className="flex gap-2">
          <Skeleton className="h-9 flex-1" />
          <Skeleton className="h-9 w-40" />
          <Skeleton className="size-9" />
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          {Array.from({ length: 2 }, (_, i) => (
            <Card key={i} padding="md" className="flex flex-col gap-4">
              <div className="flex items-center gap-2">
                <Skeleton className="size-2.5 rounded-full" />
                <Skeleton className="h-4 w-40" />
                <Skeleton className="ml-auto h-5 w-16 rounded-full" />
              </div>
              <Skeleton className="h-[5.5rem] rounded-card" />
              <div className="flex flex-col gap-2">
                <Skeleton className="h-3 w-2/3" />
                <Skeleton className="h-3 w-1/2" />
                <Skeleton className="h-3 w-3/5" />
              </div>
              <Skeleton className="h-7 w-full" />
            </Card>
          ))}
        </div>
      </WorkSkeleton>
    )
  } else if (total === 0 && jobs.isError) {
    // The error notice above says what failed; there is nothing to show under it.
    body = null
  } else if (total === 0) {
    body = (
      <Card variant="plain" padding="none">
        <EmptyState
          icon={<CalendarClock />}
          title={t('webPagesWork.automationsEmpty')}
          description={t('webPagesWork.automationsEmptyBody')}
          action={
            <div className="flex w-full flex-col items-center gap-5">
              <Button variant="primary" onClick={() => openCreate(null)}>
                <Plus />
                {t('webPagesWork.automationsNew')}
              </Button>
              <div className="flex w-full flex-col gap-2">
                <span className="text-label tracking-[0.04em] text-text-muted uppercase">
                  {t('webPagesWork.presetsLabel')}
                </span>
                <ul role="list" className="anim-stagger flex flex-col gap-2">
                  {emptyTemplates().map((tpl) => (
                    <li key={tpl.expression}>
                      <button
                        type="button"
                        onClick={() => openCreate(tpl)}
                        className="lift group target flex w-full items-center gap-3 rounded-card border border-border bg-surface-1 px-3 py-2.5 text-left shadow-card"
                      >
                        <span
                          aria-hidden
                          className="grid size-8 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted transition-colors duration-[var(--duration-fast)] group-hover:bg-accent-tint group-hover:text-accent [&_svg]:size-4"
                        >
                          {tpl.payloadKind === 'reminder' ? <BellRing /> : <Sparkles />}
                        </span>
                        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <span className="font-medium">{tpl.name}</span>
                          <span className="truncate text-small text-text-muted">
                            <code className="font-mono">{tpl.expression}</code> · {tpl.hint}
                          </span>
                        </span>
                        <ChevronRight
                          aria-hidden
                          className="size-4 shrink-0 text-text-muted transition-transform duration-[var(--duration-base)] group-hover:translate-x-0.5"
                        />
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          }
        />
      </Card>
    )
  } else {
    body = (
      <>
        <StatStrip
          items={[
            {
              label: t('webPagesWork.statActive'),
              icon: <PlayCircle />,
              value: formatNumber(enabledCount),
            },
            {
              label: t('webPagesWork.statPaused'),
              icon: <PauseCircle />,
              value: formatNumber(total - enabledCount),
            },
            {
              label: t('webPagesWork.statUpcoming'),
              icon: <CalendarClock />,
              value: formatNumber(upcoming),
            },
            {
              label: t('webPagesWork.statDegraded'),
              icon: <CircleAlert />,
              value: formatNumber(degraded),
              tone: degraded > 0 ? 'danger' : 'default',
            },
          ]}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Input
            type="search"
            aria-label={t('webPagesWork.automationsSearch')}
            icon={<Search />}
            wrapperClassName="flex-[1_1_14rem]"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('webPagesWork.automationsSearch')}
          />
          <div className="flex items-center gap-2 max-sm:w-full">
            <label className="flex min-w-0 flex-1 items-center gap-2">
              <span className="shrink-0 text-label text-text-muted">
                {t('webPagesWork.sortBy')}
              </span>
              <Select
                className="sm:w-40"
                value={sortCol}
                onChange={(e) => setSortCol(e.target.value as SortCol)}
              >
                {sortOptions().map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Select>
            </label>
            <Button
              size="icon"
              variant="outline"
              aria-label={
                sortAsc ? t('webPagesWork.sortAscending') : t('webPagesWork.sortDescending')
              }
              onClick={() => setSortAsc((v) => !v)}
            >
              <ArrowUp
                className={cn(
                  'transition-[rotate] duration-[var(--duration-base)] ease-[var(--ease-out)]',
                  !sortAsc && 'rotate-180',
                )}
              />
            </Button>
          </div>
        </div>
        <div className="@container">
          {visible.length === 0 ? (
            <Card variant="plain" padding="none">
              <EmptyState
                icon={<SearchX />}
                title={t('webPagesWork.automationsNoMatch')}
                action={
                  <Button variant="outline" onClick={() => setSearch('')}>
                    {t('webPagesWork.clearFilters')}
                  </Button>
                }
              />
            </Card>
          ) : (
            <ul
              role="list"
              className={cn('grid gap-4 @3xl:grid-cols-2', arriving && 'anim-stagger')}
            >
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
        </div>
      </>
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 p-4 md:p-6">
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

/** `cron.run`'s answer: the run's outcome, or why it did not start (busy, disabled, …). */
export interface ManualRunResult {
  success?: boolean
  status?: string
  reply?: string | null
  error?: string | null
  reason?: string | null
}

/**
 * What Run now reports. Failure is decided first: a run that failed or never
 * started can still carry text, and showing that as a reply would read as
 * success.
 */
export function runNowStatus(res: ManualRunResult | null): {
  tone: 'ok' | 'danger'
  text: string
} {
  if (res?.error) return { tone: 'danger', text: t('webPagesWork.runError', { error: res.error }) }
  if (res?.success === false) {
    const reason = res.reason || res.status || ''
    return res.status && res.status !== 'accepted'
      ? { tone: 'danger', text: t('webPagesWork.runRefused', { reason }) }
      : { tone: 'danger', text: t('webPagesWork.runError', { error: reason || res.reply || '' }) }
  }
  if (res?.reply)
    return { tone: 'ok', text: t('webPagesWork.runReply', { reply: res.reply.slice(0, 160) }) }
  return { tone: 'ok', text: t('webPagesWork.runTriggered') }
}

/** The current time, refreshed every `ms` while `active`. */
function useNow(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => setNow(Date.now()), ms)
    return () => window.clearInterval(id)
  }, [active, ms])
  return now
}

/**
 * The next-run countdown. It ticks every second on its own, so only this
 * text re-renders: nothing around it remounts or replays an entrance.
 */
function NextRun({ job }: { job: RawJob }) {
  const now = useNow(!!job.enabled && !!job.next_run)
  const abs = nextRunAbs(job, now)
  return (
    <>
      <p className="text-title tabular-nums">{nextRunText(job, now)}</p>
      {abs ? <p className="text-small text-text-muted">{abs}</p> : null}
    </>
  )
}

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
  const schedule = jobSchedule(job)
  const lastStatus = job.last_status || (job.last_run ? 'ok' : null)
  const message = String(job.message || job.prompt || '').trim()
  const failures = health ? tPlural('webPagesWork.failures', health.consecutiveFailures) : ''
  const running = job.status === 'running'
  const failing = !!health?.degraded || dot === 'error'
  const troubled = !!health && (health.degraded || health.consecutiveFailures > 0)

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
      const res = await rpc.call<ManualRunResult | null>('cron.run', { id })
      return runNowStatus(res)
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

  const stateLabel = health?.degraded
    ? t('webPagesWork.stateDegraded')
    : dot === 'error'
      ? t('webPagesWork.stateFailing')
      : enabled
        ? t('webPagesWork.stateActive')
        : t('webPagesWork.statePaused')
  const stateTone = failing ? 'danger' : enabled ? 'ok' : 'neutral'

  return (
    <Card
      asChild
      padding="none"
      className={cn(
        'flex h-full flex-col transition-[border-color,box-shadow] duration-[var(--duration-base)] ease-[var(--ease-out)] hover:border-border-strong',
        failing && 'border-danger/35',
      )}
    >
      <article
        aria-label={name}
        data-degraded={health?.degraded ? 'true' : undefined}
        className="@container"
      >
        <header className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 px-4 pt-4">
          <StatusDot tone={running ? 'accent' : stateTone} live={running} className="size-2.5" />
          <h2 className="min-w-0 flex-1 truncate text-section">{name}</h2>
          <span className="flex flex-wrap items-center gap-1.5">
            <Badge tone="neutral">{jobKindLabel(job)}</Badge>
            {elevated ? (
              <Badge tone="danger">
                <ShieldAlert aria-hidden data-status-icon />
                {t('webPagesWork.elevated', { mode: elevated })}
              </Badge>
            ) : null}
            <Badge tone={stateTone} title={health?.degraded ? failures : undefined}>
              {failing ? <AlertTriangle aria-hidden data-status-icon /> : null}
              {stateLabel}
            </Badge>
          </span>
        </header>

        <div
          className={cn(
            'mx-4 mt-3 grid gap-x-4 gap-y-2 rounded-card bg-surface-inset px-4 py-3 shadow-inset sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end',
            !enabled && 'opacity-80',
          )}
        >
          <div className="flex min-w-0 flex-col gap-0.5">
            <p className="flex items-center gap-1.5 text-label text-text-muted [&_svg]:size-3.5">
              <CalendarClock aria-hidden />
              {t('webPagesWork.sortSchedule')}
            </p>
            <p className="text-section text-pretty">{human || schedule}</p>
            {human ? (
              <code className="truncate font-mono text-small text-text-muted" title={schedule}>
                {schedule}
              </code>
            ) : null}
          </div>
          <div className="flex min-w-0 flex-col gap-0.5 sm:items-end sm:text-right">
            <p className="flex items-center gap-1.5 text-label text-text-muted [&_svg]:size-3.5">
              <Timer aria-hidden />
              {t('webPagesWork.cardNextRun')}
            </p>
            {enabled ? (
              <NextRun job={job} />
            ) : (
              <p className="flex items-center gap-1.5 text-title text-text-muted [&_svg]:size-4">
                <Pause aria-hidden />
                {t('webPagesWork.statePaused')}
              </p>
            )}
          </div>
        </div>

        {troubled || health?.lastError ? (
          <dl className="mx-4 mt-3 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-card border border-danger/25 bg-danger/[0.06] px-3 py-2 text-small">
            {troubled ? (
              <>
                <dt className="text-text-muted">{t('webPagesWork.cardHealth')}</dt>
                <dd className="font-medium text-danger">{failures}</dd>
              </>
            ) : null}
            {health?.lastError ? (
              <>
                <dt className="text-text-muted">{t('webPagesWork.cardLastError')}</dt>
                <dd className="line-clamp-3 min-w-0 font-mono break-words text-danger">
                  {health.lastError}
                </dd>
              </>
            ) : null}
          </dl>
        ) : null}

        <dl className="mx-4 mt-3 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-small">
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
          {health && !troubled && job.last_run ? (
            <>
              <dt className="text-text-muted">{t('webPagesWork.cardHealth')}</dt>
              <dd className="text-ok">{t('webPagesWork.healthy')}</dd>
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
              <dt className="self-center text-text-muted">{t('webPagesWork.cardId')}</dt>
              <dd className="flex min-w-0 items-center gap-1 font-mono text-text-muted" title={id}>
                {shortCronId(id)}
                <Button
                  size="icon-sm"
                  variant="ghost"
                  className="text-text-muted"
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
              <dt className="self-center text-text-muted">{t('webPagesWork.cardOverlap')}</dt>
              <dd className="min-w-0">
                <Select
                  size="sm"
                  aria-label={t('webPagesWork.cardOverlapFor', { name })}
                  className="w-auto max-w-full text-small"
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
                </Select>
              </dd>
            </>
          ) : null}
        </dl>

        {message ? (
          <p className="mx-4 mt-3 line-clamp-2 border-l-2 border-border-strong pl-3 text-small text-text-muted">
            {message}
          </p>
        ) : null}

        {status ? (
          <p
            role={status.tone === 'danger' ? 'alert' : 'status'}
            className={cn(
              'anim-fade-in mx-4 mt-3 flex items-start gap-1.5 text-small break-words [&_svg]:mt-px [&_svg]:size-3.5 [&_svg]:shrink-0',
              status.tone === 'danger'
                ? 'text-danger'
                : status.tone === 'warn'
                  ? 'text-warn'
                  : 'text-ok',
            )}
          >
            {status.tone === 'ok' ? (
              <CheckCircle2 aria-hidden data-status-icon />
            ) : (
              <AlertTriangle aria-hidden data-status-icon />
            )}
            <span className="min-w-0">{status.text}</span>
          </p>
        ) : null}

        <div aria-hidden className="min-h-4 flex-1" />
        <footer className="flex flex-wrap items-center gap-1 border-t border-border px-3 py-2.5">
          <Button
            size="sm"
            variant="secondary"
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
            {busy === 'toggle' ? (
              <Loader2 className="animate-spin" />
            ) : enabled ? (
              <Pause />
            ) : (
              <Play />
            )}
            {enabled ? t('webPagesWork.pause') : t('webPagesWork.resume')}
          </Button>
          <span className="flex-1" />
          <Button
            size="sm"
            variant="ghost"
            className="text-text-muted hover:text-text @max-[26rem]:w-7 @max-[26rem]:px-0"
            title={t('webPagesWork.history')}
            aria-label={t('webPagesWork.historyFor', { name })}
            onClick={onHistory}
          >
            <History />
            <span className="@max-[26rem]:sr-only">{t('webPagesWork.history')}</span>
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="text-text-muted hover:text-text @max-[26rem]:w-7 @max-[26rem]:px-0"
            title={t('webPagesWork.edit')}
            aria-label={t('webPagesWork.editFor', { name })}
            onClick={onEdit}
          >
            <Pencil />
            <span className="@max-[26rem]:sr-only">{t('webPagesWork.edit')}</span>
          </Button>
          <span aria-hidden className="mx-0.5 h-5 w-px bg-border-strong" />
          <Button
            size="icon-sm"
            variant="ghost"
            className="text-danger hover:bg-danger/10"
            aria-label={t('webPagesWork.deleteFor', { name })}
            title={t('webPagesWork.delete')}
            onClick={onDelete}
          >
            <Trash2 />
          </Button>
          {control.reason ? (
            <span id={reasonId} className="w-full px-1 text-small text-text-muted">
              {control.reason}
            </span>
          ) : null}
        </footer>
      </article>
    </Card>
  )
}
