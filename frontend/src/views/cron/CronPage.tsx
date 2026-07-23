import './cron.css'
import { useEffect, useId, useState } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence } from 'motion/react'
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CalendarClockIcon,
  PencilIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  SendIcon,
  SquareIcon,
  Trash2Icon,
} from 'lucide-react'
import { toast } from 'sonner'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '@/components/ui/button'
import { MotionListItem } from '@/lib/motion'
import { useRpc } from '@/app/providers'
import { relTime } from '@/views/overview/logic'
import { CronPanel } from './CronPanel'
import {
  activeChatSessionKey,
  explainCron,
  filterJobs,
  humanCountdownPast,
  isOkStatus,
  isUpcomingRun,
  jobDotState,
  jobKindClass,
  jobKindLabel,
  jobSchedule,
  jobTarget,
  nextRunAbs,
  nextRunText,
  runRow,
  sortJobs,
  type CronDot,
  type RawJob,
  type RawRun,
  type SaveBuild,
  type SortCol,
} from './logic'

// cron.js:1491 — the localStorage key that holds the active chat session.
const ACTIVE_SESSION_KEY = 'agentos_active_session'

// cron.js:1485-1494 — read the active chat session from the URL + localStorage
// (both reads tolerate access errors, like legacy). Kept impure-at-the-edge so
// activeChatSessionKey() in logic.ts stays a pure, tested transform.
function readActiveSessionKey(): string {
  let urlSession = ''
  try {
    urlSession = new URLSearchParams(window.location.search).get('session') || ''
  } catch {
    /* location unavailable */
  }
  let stored = ''
  try {
    stored = localStorage.getItem(ACTIVE_SESSION_KEY) || ''
  } catch {
    /* storage unavailable */
  }
  return activeChatSessionKey(urlSession, stored)
}

// The create/edit panel state: closed, creating (optional template seed), or
// editing an existing job.
type PanelState =
  | { kind: 'closed' }
  | { kind: 'create'; template: Partial<RawJob> | null }
  | { kind: 'edit'; job: RawJob }

// cron.js:832-843 — the three empty-state quick-start presets (seed the create
// panel). `hint` is UI-only; the rest is the template seed for seedForm.
const EMPTY_TEMPLATES: Array<Partial<RawJob> & { expression: string; hint: string }> = [
  {
    name: 'Daily standup nudge',
    expression: '0 9 * * 1-5',
    payloadKind: 'reminder',
    message: 'Good morning! Time for standup.',
    hint: 'Weekday morning reminder',
  },
  {
    name: 'Hourly health check',
    expression: '0 * * * *',
    payloadKind: 'agent_turn',
    message: 'Run a quick system health check and report any anomalies.',
    hint: 'Hourly agent check',
  },
  {
    name: 'Friday wrap-up',
    expression: '0 17 * * 5',
    payloadKind: 'agent_turn',
    message: "Summarize this week's work and propose next week's priorities.",
    hint: 'Friday agent wrap-up',
  },
]

// cron.js:341-342 — cron.list may return a bare array or {jobs:[…]}.
interface CronListResult {
  jobs?: RawJob[]
}
interface CronRunsResult {
  runs?: RawRun[]
}

// dot state → --tone bucket (status color ONLY via --tone; never hardcoded).
function dotTone(state: CronDot): string {
  return state === 'error' ? 'tone-danger' : state === 'off' ? 'tone-dim' : 'tone-ok'
}

function StatTile({
  label,
  value,
  hint,
  hero,
}: {
  label: string
  value: React.ReactNode
  hint: React.ReactNode
  hero?: boolean
}) {
  return (
    <div className={`cron-stat${hero ? ' cron-stat--hero' : ''}`} aria-label={label}>
      <span className="cron-stat__label t-label">{label}</span>
      <strong className="cron-stat__value t-data">{value}</strong>
      <span className="cron-stat__hint">{hint}</span>
    </div>
  )
}

// ── Run-history drawer (cron.js:863-923) ─────────────────────────────────────
function RunsDrawer({
  jobId,
  jobName,
  onClose,
}: {
  jobId: string
  jobName: string
  onClose: () => void
}) {
  const rpc = useRpc()
  const navigate = useNavigate()

  // cron.js:883 — cron.runs {id, limit:10}; a failure surfaces an inline note.
  const runsQuery = useQuery<RawRun[]>({
    queryKey: ['cron', 'runs', jobId],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawRun[] | CronRunsResult>('cron.runs', { id: jobId, limit: 10 })
      return Array.isArray(data) ? data : (data.runs ?? [])
    },
    refetchOnWindowFocus: false,
  })

  const runs = runsQuery.data ?? []

  return (
    <div className="cron-detail panel" aria-label={`Run history for ${jobName}`}>
      <div className="cron-detail__head">
        <div>
          <span className="cron-detail__eyebrow t-label">Run history</span>
          <strong className="cron-detail__name">{jobName}</strong>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={onClose}
          aria-label="Close run history"
        >
          Close
        </Button>
      </div>

      {runsQuery.isError ? (
        <p className="cron-muted">Failed to load run history.</p>
      ) : runsQuery.isLoading ? (
        <p className="cron-muted">Loading…</p>
      ) : runs.length === 0 ? (
        <p className="cron-muted">No run history yet.</p>
      ) : (
        <div className="cron-runs-scroll" role="region" aria-label="Run history table" tabIndex={0}>
          <table className="cron-runs">
            <thead>
              <tr>
                <th>Time</th>
                <th>Status</th>
                <th>Duration</th>
                <th>Delivery</th>
                <th>Reply</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((r, i) => {
                const row = runRow(r, relTime)
                return (
                  <tr key={i}>
                    <td className="cron-mono">{row.timeLabel}</td>
                    <td>
                      <span className={`cron-status ${row.statusOk ? 'tone-ok' : 'tone-danger'}`}>
                        {row.status}
                      </span>
                    </td>
                    <td className="cron-mono">{row.duration}</td>
                    <td>{row.delivery}</td>
                    <td className="cron-runs__reply">{row.reply}</td>
                    <td>
                      {row.sessionKey ? (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            navigate('/chat?session=' + encodeURIComponent(row.sessionKey))
                          }
                        >
                          → Chat
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Job card (cron.js:612-650) ───────────────────────────────────────────────
function JobCard({
  job,
  busy,
  selected,
  onOpen,
  onToggle,
  onRun,
  onEdit,
  onDelete,
}: {
  job: RawJob
  busy: boolean
  selected: boolean
  onOpen: (id: string) => void
  onToggle: (job: RawJob) => void
  onRun: (id: string) => void
  onEdit: (job: RawJob) => void
  onDelete: (job: RawJob) => void
}) {
  const id = String(job.id ?? '')
  const name = String(job.name || job.id || '')
  const enabled = !!job.enabled
  const dot = jobDotState(job)
  const lastStatus = job.last_status || (job.last_run ? 'ok' : null)
  const lastRun = job.last_run ? humanCountdownPast(new Date(job.last_run as string | number)) : '—'
  const nextRun = nextRunText(job)
  const nextAbs = nextRunAbs(job)
  const schedule = jobSchedule(job)
  const human = explainCron(job.expression || '') || ''
  const kind = jobKindLabel(job)
  const kindClass = jobKindClass(job)
  const target = jobTarget(job)
  const message = String(job.message || job.prompt || '').trim()

  return (
    <article
      className={`panel cron-card ${dotTone(dot)}${selected ? ' is-selected' : ''}`}
      aria-label={`Cron job ${name}`}
    >
      <header className="cron-card__head">
        <span
          className={`cron-card__dot tone-${dot === 'error' ? 'danger' : dot === 'off' ? 'dim' : 'ok'}`}
          aria-hidden="true"
        />
        <button
          type="button"
          className="cron-card__name"
          title="Show run history"
          onClick={() => onOpen(id)}
        >
          {name}
        </button>
        <span className={`cron-pill cron-pill--${kindClass}`}>{kind}</span>
      </header>

      <div className="cron-card__schedule">
        <code className="cron-expr">{schedule}</code>
        {human ? <span className="cron-card__human">{human}</span> : null}
      </div>

      <dl className="cron-card__meta">
        <div>
          <dt className="t-label">Target</dt>
          <dd className="t-data">{target}</dd>
        </div>
        <div>
          <dt className="t-label">Last run</dt>
          <dd className="t-data">
            {lastRun}
            {lastStatus ? (
              <>
                {' · '}
                <span
                  className={`cron-status ${isOkStatus(lastStatus) ? 'tone-ok' : 'tone-danger'}`}
                >
                  {lastStatus}
                </span>
              </>
            ) : null}
          </dd>
        </div>
        <div>
          <dt className="t-label">Next run</dt>
          <dd className="t-data">
            {enabled ? (
              <>
                <span className="cron-mono">{nextRun}</span>
                {nextAbs ? <span className="cron-card__abs"> · {nextAbs}</span> : null}
              </>
            ) : (
              <span className="cron-muted">paused</span>
            )}
          </dd>
        </div>
        {message ? (
          <div className="cron-card__message">
            <dt className="t-label">Prompt</dt>
            <dd className="t-data">
              {message.length > 140 ? message.slice(0, 140) + '…' : message}
            </dd>
          </div>
        ) : null}
      </dl>

      <footer className="cron-card__actions">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy}
          aria-label={`Run ${name} now`}
          onClick={() => onRun(id)}
        >
          <SendIcon />
          <span>Run</span>
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy}
          aria-label={`${enabled ? 'Pause' : 'Resume'} ${name}`}
          onClick={() => onToggle(job)}
        >
          {enabled ? <SquareIcon /> : <SendIcon />}
          <span>{enabled ? 'Pause' : 'Resume'}</span>
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-label={`Edit ${name}`}
          onClick={() => onEdit(job)}
        >
          <PencilIcon />
          <span>Edit</span>
        </Button>
        <Button
          type="button"
          size="sm"
          variant="destructive"
          disabled={busy}
          aria-label={`Delete ${name}`}
          onClick={() => onDelete(job)}
        >
          <Trash2Icon />
        </Button>
      </footer>
    </article>
  )
}

// ── Delete confirmation (cron.js:773-787) ────────────────────────────────────
function DeleteConfirm({
  jobName,
  busy,
  onCancel,
  onConfirm,
}: {
  jobName: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()

  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      onClose={onCancel}
      dismissible={!busy}
      overlayClassName="cron-modal__overlay"
      className="cron-modal panel"
    >
      <h2 id={titleId} className="cron-modal__title">
        Delete schedule
      </h2>
      <p className="cron-modal__body">
        Delete <strong>{jobName}</strong>? This cannot be undone.
      </p>
      <footer className="cron-modal__foot">
        <Button type="button" variant="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>
          Delete
        </Button>
      </footer>
    </ModalShell>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
export function CronPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()

  const [search, setSearch] = useState('')
  const [sortCol, setSortCol] = useState<SortCol>('next_run')
  const [sortAsc, setSortAsc] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<RawJob | null>(null)
  const [panel, setPanel] = useState<PanelState>({ kind: 'closed' })

  useEffect(() => {
    document.title = 'Cron - AgentOS Control'
  }, [])

  // cron.js:339-347 — cron.list after waitForConnection (array or {jobs}).
  const jobsQuery = useQuery<RawJob[]>({
    queryKey: ['cron'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawJob[] | CronListResult>('cron.list', {})
      return Array.isArray(data) ? data : (data.jobs ?? [])
    },
    refetchOnWindowFocus: false,
  })

  // cron.js:346 — load-failure toast (stable id so repeats dedupe).
  useEffect(() => {
    if (jobsQuery.isError) {
      const err = jobsQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load cron jobs: ' + message, { id: 'cron-load-err' })
    }
  }, [jobsQuery.isError, jobsQuery.error])

  // cron.js:306-316,318-321 — the subscribe/unsubscribe lifecycle. Legacy
  // subscribes on render and unsubscribes on destroy. In React this is a mount
  // effect: cron.subscribe after the WS handshake (best-effort; a pre-connect
  // call rejects with "Not connected"), and cron.unsubscribe in the cleanup.
  // The cleanup runs on every unmount (incl. StrictMode's dev double-invoke),
  // so no subscription leaks across remounts.
  useEffect(() => {
    let cancelled = false
    rpc
      .waitForConnection()
      .then(() => {
        if (!cancelled) return rpc.call('cron.subscribe', {})
        return undefined
      })
      .catch(() => {
        /* subscription is best-effort */
      })
    return () => {
      cancelled = true
      rpc.call('cron.unsubscribe', {}).catch(() => {
        /* best-effort; ignore disconnected state */
      })
    }
  }, [rpc])

  // cron.js:313-315 — cron.run.finished → invalidate the job list AND any open
  // runs drawer (targeted refetch). Cleaned up on unmount so the listener never
  // leaks (StrictMode-safe: the unsub closure removes exactly this handler).
  useEffect(() => {
    const unsub = rpc.on('cron.run.finished', () => {
      void queryClient.invalidateQueries({ queryKey: ['cron'] })
    })
    return () => {
      unsub()
    }
  }, [rpc, queryClient])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['cron'] })

  // cron.js:735-744 — enable/disable toggle → cron.update {id, enabled:!enabled}.
  const toggleMutation = useMutation({
    mutationFn: (job: RawJob) => rpc.call('cron.update', { id: job.id, enabled: !job.enabled }),
    onSuccess: (_data, job) => {
      toast.info(`Job ${job.enabled ? 'paused' : 'resumed'}`, { id: 'cron-toggle' })
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Update failed: ' + message, { id: 'cron-toggle-err' })
    },
  })

  // cron.js:746-766 — run-now → cron.run {id}; surface reply/error/triggered.
  const runMutation = useMutation({
    mutationFn: (id: string) => rpc.call<{ reply?: string; error?: string }>('cron.run', { id }),
    onSuccess: (res) => {
      if (res && res.reply) {
        toast.success('Run complete: ' + res.reply.substring(0, 120), { id: 'cron-run' })
      } else if (res && res.error) {
        toast.warning('Run failed: ' + res.error, { id: 'cron-run' })
      } else {
        toast.success('Job triggered', { id: 'cron-run' })
      }
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Run failed: ' + message, { id: 'cron-run-err' })
    },
  })

  // cron.js:768-788 — delete after confirmation → cron.remove {id}.
  const removeMutation = useMutation({
    mutationFn: (id: string) => rpc.call('cron.remove', { id }),
    onSuccess: (_data, id) => {
      toast.info('Job deleted', { id: 'cron-remove' })
      if (selectedId === id) setSelectedId(null)
      setPendingDelete(null)
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Delete failed: ' + message, { id: 'cron-remove-err' })
    },
  })

  // cron.js:1240-1250 — save the create/edit panel → cron.create OR cron.update
  // (method + full payload assembled in logic.ts::buildSavePayload). Success
  // toast + close + reload; failure toast.
  const saveMutation = useMutation({
    mutationFn: (build: Extract<SaveBuild, { ok: true }>) => rpc.call(build.method, build.payload),
    onSuccess: (_data, build) => {
      toast.success(build.method === 'cron.update' ? 'Schedule updated' : 'Schedule created', {
        id: 'cron-save',
      })
      setPanel({ kind: 'closed' })
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Save failed: ' + message, { id: 'cron-save-err' })
    },
  })

  const jobs = jobsQuery.data ?? []
  const busy = toggleMutation.isPending || runMutation.isPending || removeMutation.isPending

  // cron.js:381-405 — summary stats.
  const total = jobs.length
  const enabledCount = jobs.filter((j) => j.enabled).length
  const paused = total - enabledCount
  const upcoming = jobs.filter((j) => isUpcomingRun(j)).length
  const reminders = jobs.filter((j) => (j.payloadKind || j.payload_kind) === 'reminder').length
  const agentTasks = jobs.filter((j) => (j.payloadKind || j.payload_kind) === 'agent_turn').length

  // cron.js:562-570 — filter then sort.
  const visible = sortJobs(filterJobs(jobs, search), sortCol, sortAsc)

  const selectedJob = selectedId ? jobs.find((j) => String(j.id) === selectedId) : undefined

  return (
    <div className="cron-stage">
      <header className="cron-stage__header">
        <div className="cron-stage__title-block">
          <span className="t-label">Control · Schedule</span>
          <h1 className="t-display">Cron</h1>
          <p className="cron-stage__subtitle">
            Time-driven tasks — orchestrate reminders, agent turns, and recurring work.
          </p>
        </div>
        <div className="cron-stage__actions">
          <Button
            variant="outline"
            title="Refresh"
            className="text-xs uppercase tracking-[0.14em]"
            disabled={jobsQuery.isFetching}
            onClick={() => void invalidate()}
          >
            <RefreshCwIcon className={jobsQuery.isFetching ? 'cron-refresh-spin' : undefined} />
            <span>{jobsQuery.isFetching ? 'Refreshing…' : 'Refresh'}</span>
          </Button>
          <Button
            className="text-xs uppercase tracking-[0.14em]"
            onClick={() => setPanel({ kind: 'create', template: null })}
          >
            <PlusIcon />
            <span>New job</span>
          </Button>
        </div>
      </header>

      <section
        className={`cron-command${jobsQuery.isFetching ? ' is-loading' : ''}`}
        aria-label="Schedule operations"
        aria-busy={jobsQuery.isFetching}
      >
        <div className="cron-command__toolbar">
          <div className="cron-command__heading">
            <span className="cron-command__icon" aria-hidden="true">
              <CalendarClockIcon />
            </span>
            <div>
              <span className="t-label">Automation clock</span>
              <strong>Schedule posture</strong>
            </div>
          </div>
          <span className="cron-command__meta t-data">
            <span className={enabledCount ? 'tone-ok' : 'tone-dim'} aria-hidden="true" />
            {enabledCount ? `${enabledCount} active` : 'Scheduler idle'}
          </span>
        </div>
        <div className="cron-stats" aria-label="Cron summary">
          <StatTile
            label="Active schedules"
            hero
            value={enabledCount}
            hint={paused ? `${paused} paused` : total ? 'all enabled' : 'none configured'}
          />
          <StatTile
            label="Upcoming runs"
            value={upcoming}
            hint={upcoming ? 'scheduled ahead' : 'no upcoming runs'}
          />
          <StatTile label="Reminders" value={reminders} hint="static reminders" />
          <StatTile label="Agent tasks" value={agentTasks} hint="scheduled turns" />
        </div>
      </section>

      <section className="cron-list">
        <div className="cron-list__head">
          <div className="cron-list__heading">
            <h2 className="cron-list__title">{search ? 'Matching schedules' : 'All schedules'}</h2>
            <span className="cron-list__count t-data">
              {visible.length}
              {search ? ` of ${total}` : ''}
            </span>
          </div>
          <div className="cron-list__tools">
            <div className="cron-search-wrap">
              <SearchIcon aria-hidden="true" />
              <input
                className="cron-search t-data"
                type="search"
                placeholder="Search jobs…"
                autoComplete="off"
                aria-label="Search jobs"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <label className="cron-sort">
              <span className="t-label">Sort</span>
              <select
                className="cron-sort__select t-data"
                aria-label="Sort jobs"
                value={sortCol}
                onChange={(e) => setSortCol(e.target.value as SortCol)}
              >
                <option value="next_run">Next run</option>
                <option value="name">Name</option>
                <option value="last_run">Last run</option>
                <option value="payloadKind">Kind</option>
                <option value="sessionTarget">Target</option>
                <option value="expression">Schedule</option>
              </select>
            </label>
            <Button
              variant="outline"
              size="icon-sm"
              title={sortAsc ? 'Ascending' : 'Descending'}
              aria-label={`Sort direction: ${sortAsc ? 'ascending' : 'descending'}`}
              onClick={() => setSortAsc((v) => !v)}
            >
              {sortAsc ? <ArrowUpIcon /> : <ArrowDownIcon />}
            </Button>
          </div>
        </div>

        {jobs.length === 0 ? (
          <div className="cron-empty">
            <div className="cron-empty__title">No schedules yet.</div>
            <p className="cron-empty__msg">
              Create your first cron job to wake an agent, fire a reminder, or kick off recurring
              work — all on time, all on your terms.
            </p>
            <div className="cron-empty__actions">
              <Button type="button" onClick={() => setPanel({ kind: 'create', template: null })}>
                <PlusIcon />
                <span>Create your first schedule</span>
              </Button>
            </div>
            <div className="cron-empty__hints">
              <span className="cron-empty__hints-label t-label">Try a preset</span>
              {EMPTY_TEMPLATES.map((tpl) => (
                <button
                  key={tpl.name}
                  type="button"
                  className="cron-empty-hint"
                  onClick={() => setPanel({ kind: 'create', template: tpl })}
                >
                  <code>{tpl.expression}</code>
                  <span>{tpl.hint}</span>
                </button>
              ))}
            </div>
          </div>
        ) : visible.length === 0 ? (
          <div className="cron-empty">
            <div className="cron-empty__title">No matches</div>
            <p className="cron-empty__msg">
              No schedules match your search. Try a different query, or clear it to see everything.
            </p>
          </div>
        ) : (
          <div className="cron-cards">
            <AnimatePresence initial={false}>
              {visible.map((job, i) => (
                <MotionListItem key={String(job.id ?? i)}>
                  <JobCard
                    job={job}
                    busy={busy}
                    selected={selectedId === String(job.id)}
                    onOpen={(id) => setSelectedId((cur) => (cur === id ? null : id))}
                    onToggle={(j) => toggleMutation.mutate(j)}
                    onRun={(id) => runMutation.mutate(id)}
                    onEdit={(j) => setPanel({ kind: 'edit', job: j })}
                    onDelete={(j) => setPendingDelete(j)}
                  />
                </MotionListItem>
              ))}
            </AnimatePresence>
          </div>
        )}

        {selectedJob ? (
          <RunsDrawer
            jobId={String(selectedJob.id)}
            jobName={String(selectedJob.name || selectedJob.id)}
            onClose={() => setSelectedId(null)}
          />
        ) : null}
      </section>

      {pendingDelete ? (
        <DeleteConfirm
          jobName={String(pendingDelete.name || pendingDelete.id)}
          busy={removeMutation.isPending}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => removeMutation.mutate(String(pendingDelete.id))}
        />
      ) : null}

      {panel.kind !== 'closed' ? (
        <CronPanel
          // Remount on a new seed so form state resets between create/edit and
          // between different jobs (mirrors the agents-dialog key strategy).
          key={panel.kind === 'edit' ? 'edit:' + String(panel.job.id) : 'create'}
          job={panel.kind === 'edit' ? panel.job : null}
          template={panel.kind === 'create' ? panel.template : null}
          activeSessionKey={readActiveSessionKey()}
          saving={saveMutation.isPending}
          onCancel={() => setPanel({ kind: 'closed' })}
          onSubmit={(build) => saveMutation.mutate(build)}
        />
      ) : null}
    </div>
  )
}
