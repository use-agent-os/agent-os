import './jobs.css'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarClock, Plus, RefreshCw, Search, Sparkles, X } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { ModalShell } from '@/components/ModalShell'
import { filterJobs, type DeliveryTargetMap, type RawJob, type SaveBuild } from '@/views/cron/logic'
import '@/i18n/en/cron'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useGateway } from '~/stores/gateway'
import { useUi } from '~/stores/ui'
import { BLUEPRINTS, type Blueprint } from './blueprints'
import { JobDetail } from './JobDetail'
import { JobList } from './JobList'
import { JobSheet, type SheetState } from './JobSheet'
import { filterCounts, JOB_FILTERS, matchesFilter, orderJobs, type JobFilter } from './logic'

interface CronListResult {
  jobs?: RawJob[]
}
interface DeliveryTargetsResult {
  targets?: DeliveryTargetMap
}
/** cron.run: an accepted run reports its outcome; a blocked one says why. */
interface RunResult {
  success?: boolean
  status?: string
  reply?: string | null
  error?: string | null
  reason?: string
}

/** Gateway events after which the list or a run history is stale. */
const CRON_EVENTS = ['cron.run.start', 'cron.run.finished']

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/**
 * Scheduled jobs as a panel over the window, the way Mail's Activity or
 * Xcode's Organizer sit over the document: the job list and blueprints on the
 * left, the selected job on the right, Escape or the close button to leave.
 * It reads the `jobsOpen` flag from the UI store; the sidebar toggles it.
 */
export function JobsPanel() {
  const open = useUi((s) => s.jobsOpen)
  const close = useUi((s) => s.closeJobs)
  const titleId = useId()
  if (!open) return null
  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={close}
      overlayClassName="jobs-panel__overlay"
      className="jobs-panel"
    >
      <JobsPanelBody titleId={titleId} onClose={close} />
    </ModalShell>
  )
}

function JobsPanelBody({ titleId, onClose }: { titleId: string; onClose: () => void }) {
  const gatewayState = useGateway((s) => s.status.state)
  const connected = gatewayState === 'running'
  return connected ? (
    <ConnectedJobs titleId={titleId} onClose={onClose} />
  ) : (
    <>
      <PanelHeader titleId={titleId} subtitle="" onClose={onClose} />
      <div className="jobs-offline">
        <CalendarClock className="size-9 text-dim" strokeWidth={1.25} aria-hidden />
        <p className="text-[15px] font-semibold text-foreground">
          {gatewayState === 'starting'
            ? t('chat.waitingGateway')
            : t(`gateway.state.${gatewayState}`)}
        </p>
        <p className="max-w-sm">{t('chat.gatewayDown')}</p>
      </div>
    </>
  )
}

function PanelHeader({
  titleId,
  subtitle,
  onClose,
}: {
  titleId: string
  subtitle: string
  onClose: () => void
}) {
  return (
    <header className="jobs-panel__head">
      <div>
        <h1 id={titleId}>{t('jobs.title')}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      <Button
        variant="ghost"
        size="icon"
        aria-label={t('jobs.close')}
        title={t('jobs.close')}
        onClick={onClose}
      >
        <X className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
    </header>
  )
}

function ConnectedJobs({ titleId, onClose }: { titleId: string; onClose: () => void }) {
  const rpc = useRpc()
  const queryClient = useQueryClient()

  const [filter, setFilter] = useState<JobFilter>('all')
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [sheet, setSheet] = useState<SheetState>({ kind: 'closed' })
  const [pendingDelete, setPendingDelete] = useState<RawJob | null>(null)

  const jobsQuery = useQuery<RawJob[]>({
    queryKey: ['cron', 'list'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawJob[] | CronListResult>('cron.list', {})
      return Array.isArray(data) ? data : (data.jobs ?? [])
    },
    // next_run moves on every fire; the events below catch most of them, the
    // poll catches the rest (a job edited from the CLI, say).
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })

  const deliveryTargetsQuery = useQuery<DeliveryTargetsResult>({
    queryKey: ['cron', 'deliveryTargets'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<DeliveryTargetsResult>('channels.deliveryTargets', {})
    },
    refetchOnWindowFocus: false,
  })

  useEffect(() => {
    if (jobsQuery.isError) {
      toast.error(`${t('jobs.toast.loadFailed')}: ${errorText(jobsQuery.error)}`, {
        id: 'jobs-load-err',
      })
    }
  }, [jobsQuery.isError, jobsQuery.error])

  // Live updates: refetch on every run event. The shell keeps the connection
  // subscribed to the cron topic for the app's lifetime (job notifications,
  // lib/use-notifications), so this panel only listens; the poll above is
  // the backstop.
  useEffect(() => {
    const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['cron'] })
    const offs = CRON_EVENTS.map((event) => rpc.on(event, invalidate))
    return () => offs.forEach((off) => off())
  }, [rpc, queryClient])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['cron'] })

  const toggleMutation = useMutation({
    mutationFn: (job: RawJob) => rpc.call('cron.update', { id: job.id, enabled: !job.enabled }),
    onSuccess: (_data, job) => {
      toast.success(t(job.enabled ? 'jobs.toast.paused' : 'jobs.toast.resumed'), {
        id: 'jobs-toggle',
      })
      void invalidate()
    },
    onError: (err) =>
      toast.error(`${t('jobs.toast.updateFailed')}: ${errorText(err)}`, { id: 'jobs-toggle-err' }),
  })

  const runMutation = useMutation({
    mutationFn: (id: string) => rpc.call<RunResult>('cron.run', { id }),
    onSuccess: (res) => {
      if (res?.status === 'accepted' || res?.success) {
        const reply = res.reply ? `: ${res.reply.slice(0, 120)}` : ''
        toast.success(`${t('jobs.toast.runStarted')}${reply}`, { id: 'jobs-run' })
      } else if (res?.error) {
        toast.warning(`${t('jobs.toast.runFailed')}: ${res.error}`, { id: 'jobs-run' })
      } else {
        const why = res?.reason || res?.status || ''
        toast.warning(`${t('jobs.toast.runBlocked')}${why ? `: ${why}` : ''}`, { id: 'jobs-run' })
      }
      void invalidate()
    },
    onError: (err) =>
      toast.error(`${t('jobs.toast.runFailed')}: ${errorText(err)}`, { id: 'jobs-run-err' }),
  })

  const removeMutation = useMutation({
    mutationFn: (id: string) => rpc.call('cron.remove', { id }),
    onSuccess: (_data, id) => {
      toast.success(t('jobs.toast.deleted'), { id: 'jobs-remove' })
      if (selectedId === id) setSelectedId(null)
      setPendingDelete(null)
      void invalidate()
    },
    onError: (err) =>
      toast.error(`${t('jobs.toast.deleteFailed')}: ${errorText(err)}`, { id: 'jobs-remove-err' }),
  })

  const saveMutation = useMutation({
    mutationFn: (build: Extract<SaveBuild, { ok: true }>) =>
      rpc.call<RawJob>(build.method, build.payload),
    onSuccess: (saved, build) => {
      toast.success(
        t(build.method === 'cron.update' ? 'jobs.toast.updated' : 'jobs.toast.created'),
        { id: 'jobs-save' },
      )
      setSheet({ kind: 'closed' })
      if (saved && saved.id) setSelectedId(String(saved.id))
      void invalidate()
    },
    onError: (err) =>
      toast.error(`${t('jobs.toast.saveFailed')}: ${errorText(err)}`, { id: 'jobs-save-err' }),
  })

  const jobs = jobsQuery.data ?? []
  const counts = filterCounts(jobs)
  const visible = orderJobs(filterJobs(jobs, search).filter((j) => matchesFilter(j, filter)))

  // Master-detail: something is always selected when there is something to
  // select. A deleted or filtered-out selection falls back to the first row.
  const selected =
    (selectedId ? visible.find((j) => String(j.id) === selectedId) : undefined) ?? visible[0]
  const busy = toggleMutation.isPending || removeMutation.isPending
  const subtitle = jobsQuery.isSuccess
    ? `${jobs.length} ${jobs.length === 1 ? t('jobs.count.one') : t('jobs.count.many')}`
    : ''

  function openBlueprint(bp: Blueprint) {
    saveMutation.reset()
    setSheet({ kind: 'create', template: bp })
  }

  return (
    <>
      <PanelHeader titleId={titleId} subtitle={subtitle} onClose={onClose} />
      <div className="jobs">
        <section className="jobs-list" aria-label={t('jobs.title')}>
          <div className="jobs-list__head">
            <label className="mac-search app-no-drag flex-1">
              <Search className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
              <input
                type="search"
                placeholder={t('jobs.search')}
                aria-label={t('jobs.search')}
                autoComplete="off"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('jobs.refresh')}
              title={t('jobs.refresh')}
              disabled={jobsQuery.isFetching}
              onClick={() => void invalidate()}
            >
              <RefreshCw
                className={`size-4 text-muted-foreground${jobsQuery.isFetching ? ' jobs-spin' : ''}`}
                strokeWidth={1.75}
                aria-hidden
              />
            </Button>
          </div>

          {jobs.length > 0 ? (
            <div role="radiogroup" aria-label={t('jobs.title')} className="jobs-filters">
              {JOB_FILTERS.map((f) => (
                <button
                  key={f}
                  type="button"
                  role="radio"
                  aria-checked={filter === f}
                  className="jobs-filter"
                  data-filter={f}
                  onClick={() => setFilter(f)}
                >
                  <span>{t(`jobs.filter.${f}`)}</span>
                  <span className="jobs-filter__count">{counts[f]}</span>
                </button>
              ))}
            </div>
          ) : null}

          <div className="jobs-list__scroll">
            <JobList
              jobs={visible}
              loading={jobsQuery.isPending}
              error={jobsQuery.isError ? t('jobs.list.error') : null}
              emptyText={jobs.length === 0 ? t('jobs.list.none') : t('jobs.list.empty')}
              selectedId={selected ? String(selected.id) : null}
              onSelect={(id) => setSelectedId(id)}
            />
            <button
              type="button"
              className="jobs-add"
              aria-label={t('jobs.new')}
              title={t('jobs.new')}
              onClick={() => setSheet({ kind: 'create', template: null })}
            >
              <Plus className="size-4" strokeWidth={1.75} aria-hidden />
            </button>

            <div className="jobs-blueprints">
              <div className="jobs-blueprints__title">{t('jobs.blueprints')}</div>
              {BLUEPRINTS.map((bp) => (
                <button
                  key={bp.id}
                  type="button"
                  className="jobs-blueprint"
                  title={bp.hint}
                  onClick={() => openBlueprint(bp)}
                >
                  <Sparkles className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
                  <span className="jobs-blueprint__name">{bp.name}</span>
                </button>
              ))}
            </div>
          </div>
        </section>

        <section className="jobs-detail" aria-live="polite">
          {selected ? (
            <JobDetail
              key={String(selected.id)}
              job={selected}
              busy={busy}
              running={runMutation.isPending}
              onRun={() => runMutation.mutate(String(selected.id))}
              onToggle={() => toggleMutation.mutate(selected)}
              onEdit={() => setSheet({ kind: 'edit', job: selected })}
              onDuplicate={() =>
                setSheet({
                  kind: 'create',
                  template: { ...selected, id: undefined, name: `${selected.name || ''} copy` },
                })
              }
              onDelete={() => setPendingDelete(selected)}
            />
          ) : jobsQuery.isSuccess && jobs.length === 0 ? (
            <div className="jobs-detail__empty">
              <div className="jobs-empty__glyph" aria-hidden>
                <CalendarClock className="size-7" strokeWidth={1.25} />
              </div>
              <p className="jobs-empty__title">{t('jobs.empty.title')}</p>
              <p className="jobs-empty__body">{t('jobs.empty.body')}</p>
              <Button
                variant="primary"
                onClick={() => setSheet({ kind: 'create', template: null })}
              >
                <Plus className="size-3.5" strokeWidth={2} aria-hidden />
                {t('jobs.empty.cta')}
              </Button>
              <p className="jobs-empty__or">{t('jobs.empty.blueprints')}</p>
            </div>
          ) : (
            <div className="jobs-detail__empty">
              <CalendarClock className="size-8 text-dim" strokeWidth={1.25} aria-hidden />
              <p>{t('jobs.detail.select')}</p>
            </div>
          )}
        </section>
      </div>

      {pendingDelete ? (
        <DeleteConfirm
          job={pendingDelete}
          busy={removeMutation.isPending}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => removeMutation.mutate(String(pendingDelete.id))}
        />
      ) : null}

      {sheet.kind !== 'closed' ? (
        <JobSheet
          // Remount per seed so form state never leaks between jobs.
          key={
            sheet.kind === 'edit'
              ? `edit:${String(sheet.job.id)}`
              : `create:${String((sheet.template as Blueprint | null)?.id ?? '')}`
          }
          state={sheet}
          saving={saveMutation.isPending}
          saveError={saveMutation.isError ? errorText(saveMutation.error) : null}
          deliveryTargets={deliveryTargetsQuery.data?.targets}
          onCancel={() => {
            saveMutation.reset()
            setSheet({ kind: 'closed' })
          }}
          onSubmit={(build) => saveMutation.mutate(build)}
        />
      ) : null}
    </>
  )
}

function DeleteConfirm({
  job,
  busy,
  onCancel,
  onConfirm,
}: {
  job: RawJob
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      dismissible={!busy}
      overlayClassName="jobs-sheet__overlay"
      className="jobs-alert"
    >
      <h2 id={titleId} className="jobs-alert__title">
        {t('jobs.delete.title')}
      </h2>
      <p id={bodyId} className="jobs-alert__body">
        <strong>{String(job.name || job.id)}</strong> — {t('jobs.delete.body')}
      </p>
      <div className="jobs-alert__actions">
        <Button disabled={busy} onClick={onCancel}>
          {t('jobs.delete.cancel')}
        </Button>
        <Button variant="danger" disabled={busy} onClick={onConfirm}>
          {t('jobs.delete.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
