import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUp, Loader2, Plus, Search } from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { gate, requireConnection, requireScheduler } from '@/lib/disabled-reason'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { cn } from '@/lib/utils'
import { RpcError } from '@/lib/ws-rpc'
import {
  activeChatSessionKey,
  filterJobs,
  jobDotState,
  jobRuntimeHealth,
  sortJobs,
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
import { StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import { Input, Select } from '@/web/components/ui/input'
import { PageHeader } from '@/web/components/ui/page-header'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { PAGE_CLASS } from '@/web/features/home/overview-ui'
import { useDocumentVisible } from '@/web/shell/session-data'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import '@/web/styles/page-automations.css'
import { AutomationEditor, type SaveRequest } from './AutomationEditor'
import { JobRow } from './JobRow'
import { RunHistory } from './RunHistory'
import { useArrival } from './schedule'
import { Upcoming } from './Upcoming'
import { ZeroState } from './ZeroState'

export { runNowStatus, type ManualRunResult } from './JobRow'

// `/automations` (task 8.5) on views/cron/logic, schedule first: what runs
// next and the next 24 hours as lanes, then one row per job with its schedule
// in words, run health and actions (run now, pause and resume, history, edit,
// delete); the create / edit sheet; a live refresh on `cron.run.finished`.
// With no jobs the page is a zero state with a gallery of templates.

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
  const listId = useId()
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
  const failingCount = list.filter(
    (j) => jobRuntimeHealth(j)?.degraded || jobDotState(j) === 'error',
  ).length
  const empty = !jobs.isPending && total === 0 && !jobs.isError

  let body
  if (jobs.isPending) {
    body = (
      <LoadingRegion label={t('webPagesWork.automationsLoading')} className="flex flex-col gap-5">
        <div className="am-upcoming">
          <Skeleton className="h-56 rounded-card" />
          <Skeleton className="h-56 rounded-card" />
        </div>
        <Skeleton className="h-9 w-full" />
        <div className="flex flex-col gap-px overflow-hidden rounded-card">
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-24 rounded-none" />
          ))}
        </div>
      </LoadingRegion>
    )
  } else if (total === 0 && jobs.isError) {
    // The error notice above says what failed; there is nothing to show under it.
    body = null
  } else if (total === 0) {
    body = <ZeroState onCreate={openCreate} disabledReason={control.reason} />
  } else {
    body = (
      <>
        <Upcoming jobs={list} animate={arriving} />
        <section aria-labelledby={listId} className="flex flex-col gap-3">
          <div className="am-toolbar">
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
              <h2 id={listId} className="text-section">
                {t('webPagesWork.automationsTitle')}
              </h2>
              <p className="am-summary">
                <span>{tPlural('webPagesWork.summaryTotal', total)}</span>
                <span>
                  <StatusDot tone="ok" className="size-1.5" />
                  {t('webPagesWork.summaryActive', { count: enabledCount })}
                </span>
                {total - enabledCount > 0 ? (
                  <span>
                    <StatusDot tone="neutral" className="size-1.5" />
                    {t('webPagesWork.summaryPaused', { count: total - enabledCount })}
                  </span>
                ) : null}
                {failingCount > 0 ? (
                  <span className="text-danger">
                    <StatusDot tone="danger" className="size-1.5" />
                    {t('webPagesWork.summaryFailing', { count: failingCount })}
                  </span>
                ) : null}
              </p>
            </div>
            <div className="am-toolbar-controls">
              <Input
                type="search"
                size="sm"
                aria-label={t('webPagesWork.automationsSearch')}
                icon={<Search />}
                wrapperClassName="min-w-0 flex-1 max-sm:basis-full sm:w-56 sm:flex-none"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t('webPagesWork.automationsSearch')}
              />
              <Select
                size="sm"
                aria-label={t('webPagesWork.sortBy')}
                className="w-auto max-sm:flex-1"
                value={sortCol}
                onChange={(e) => setSortCol(e.target.value as SortCol)}
              >
                {sortOptions().map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Select>
              <Button
                size="icon-sm"
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
          {visible.length === 0 ? (
            <EmptyState
              size="sm"
              icon={<Search />}
              title={t('webPagesWork.automationsNoMatch')}
              action={
                <Button variant="outline" onClick={() => setSearch('')}>
                  {t('webPagesWork.clearFilters')}
                </Button>
              }
            />
          ) : (
            <ul role="list" className={cn('am-list', arriving && 'anim-stagger')}>
              {visible.map((job, i) => (
                <li key={String(job.id ?? i)} className="min-w-0">
                  <JobRow
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
        </section>
      </>
    )
  }

  return (
    <div className={PAGE_CLASS}>
      <PageHeader
        title={t('webPagesWork.automationsTitle')}
        description={t('webPagesWork.automationsSubtitle')}
        actions={
          // The zero state carries its own primary action: one per view.
          empty ? undefined : (
            <Button
              variant="primary"
              aria-describedby={control.reason ? reasonId : undefined}
              onClick={() => openCreate(null)}
            >
              <Plus />
              {t('webPagesWork.automationsNew')}
            </Button>
          )
        }
        note={
          control.reason && !empty ? (
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
