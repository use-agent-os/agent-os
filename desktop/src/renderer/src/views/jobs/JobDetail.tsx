import { useQuery } from '@tanstack/react-query'
import {
  ChevronRight,
  Copy,
  CopyPlus,
  MessageSquare,
  Pause,
  Pencil,
  Play,
  ShieldAlert,
  Trash2,
} from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { copyWithFallback } from '@/lib/clipboard'
import {
  jobCreatedFrom,
  jobElevated,
  jobSessionKey,
  type RawJob,
  type RawRun,
} from '@/views/cron/logic'
import { sessionPath } from '~/components/sidebar/SessionList'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import { useNow } from '~/lib/use-now'
import {
  clockLabel,
  countdown,
  describeSchedule,
  formatDurationMs,
  formatInterval,
  jobEverySeconds,
  jobHealth,
  jobScheduleKind,
  runOutcomes,
  timelineLabel,
  upcomingRuns,
  type JobHealth,
} from './logic'

type PayloadKind = 'reminder' | 'agent_turn' | 'system_event' | 'script'

function payloadKind(job: RawJob): PayloadKind {
  const kind = String(job.payloadKind || job.payload_kind || 'agent_turn')
  return kind === 'reminder' || kind === 'system_event' || kind === 'script' ? kind : 'agent_turn'
}

const HEALTH_TONE: Record<JobHealth, 'ok' | 'warn' | 'danger' | 'dim'> = {
  running: 'ok',
  active: 'ok',
  failing: 'danger',
  paused: 'dim',
}

function targetLabel(job: RawJob): string {
  const target = String(job.sessionTarget || job.session_target || 'isolated')
  const key = (
    target === 'main' || target === 'current' || target === 'isolated' || target === 'session'
      ? `jobs.target.${target}`
      : 'jobs.target.isolated'
  ) as MessageKey
  return t(key)
}

function deliveryLabel(job: RawJob): string {
  const d = job.delivery
  const mode = String(d?.mode || '').toLowerCase()
  if (mode === 'webhook') return `${t('jobs.detail.deliveryWebhook')} · ${d?.webhookUrl || ''}`
  if (mode === 'announce' || mode === 'channel') {
    const where = [d?.channelName, d?.to || d?.channelId].filter(Boolean).join(' · ')
    return where || mode
  }
  if (mode === 'none') return t('jobs.detail.deliveryNone')
  return t('jobs.detail.deliveryInferred')
}

/** The right pane: what the job is, when it fires next, and what happened when it did. */
export function JobDetail({
  job,
  busy,
  running,
  onRun,
  onToggle,
  onEdit,
  onDuplicate,
  onDelete,
}: {
  job: RawJob
  busy: boolean
  running: boolean
  onRun: () => void
  onToggle: () => void
  onEdit: () => void
  onDuplicate: () => void
  onDelete: () => void
}) {
  const now = useNow(1000)
  const id = String(job.id ?? '')
  const name = String(job.name || job.id || '')
  const kind = payloadKind(job)
  const health = jobHealth(job)
  const elevated = jobElevated(job)
  const scheduleKind = jobScheduleKind(job)
  const upcoming = upcomingRuns(job, 5, now)
  const nextTs = job.next_run ? new Date(job.next_run as string | number).getTime() : NaN
  const lastTs = job.last_run ? new Date(job.last_run as string | number).getTime() : NaN
  const message = String(job.message || job.prompt || '').trim()
  const script = String(job.script || '').trim()
  const createdFrom = jobCreatedFrom(job)
  const sessionKey = jobSessionKey(job)
  const lastError = typeof job.lastResult === 'string' ? job.lastResult.trim() : ''

  async function copyId() {
    try {
      await copyWithFallback(id)
      toast.success(t('jobs.detail.copied'), { id: 'jobs-copy', duration: 1400 })
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err)
      toast.warning(`${t('jobs.detail.copyFailed')}: ${reason}`, {
        id: 'jobs-copy',
        duration: 2500,
      })
    }
  }

  let nextPrimary: string
  let nextSecondary: string
  if (!job.enabled) {
    nextPrimary = t('jobs.detail.paused')
    nextSecondary = t('jobs.detail.pausedHint')
  } else if (health === 'running') {
    nextPrimary = t('jobs.detail.running')
    nextSecondary = ''
  } else if (Number.isNaN(nextTs)) {
    nextPrimary = '—'
    nextSecondary = t('jobs.detail.awaiting')
  } else if (nextTs <= now) {
    nextPrimary = t('jobs.detail.awaiting')
    nextSecondary = clockLabel(new Date(nextTs), now)
  } else {
    nextPrimary = countdown(nextTs, now)
    nextSecondary = clockLabel(new Date(nextTs), now)
  }

  return (
    <article className="jobs-pane" aria-label={name}>
      <header className="jobs-pane__head">
        <div className="jobs-pane__title">
          <h1>{name}</h1>
          <div className="jobs-pane__chips">
            <span className="mac-chip" data-tone={HEALTH_TONE[health]}>
              {t(`jobs.health.${health}`)}
            </span>
            <span className="mac-chip">{t(`jobs.kind.${kind}`)}</span>
            {elevated ? (
              <span className="mac-chip" data-tone="danger" title={t('jobs.detail.elevatedHint')}>
                <ShieldAlert className="size-3" strokeWidth={2} aria-hidden />
                {t('jobs.detail.elevated')}
              </span>
            ) : null}
          </div>
        </div>
        <div className="jobs-pane__actions">
          <Button
            variant="primary"
            disabled={busy || running || health === 'running'}
            onClick={onRun}
          >
            <Play className="size-3.5" strokeWidth={2} aria-hidden />
            {running ? t('jobs.action.running') : t('jobs.action.run')}
          </Button>
          <Button disabled={busy} onClick={onToggle}>
            {job.enabled ? (
              <Pause className="size-3.5" strokeWidth={2} aria-hidden />
            ) : (
              <Play className="size-3.5" strokeWidth={2} aria-hidden />
            )}
            {job.enabled ? t('jobs.action.pause') : t('jobs.action.resume')}
          </Button>
          <Button
            size="icon"
            aria-label={t('jobs.action.edit')}
            title={t('jobs.action.edit')}
            onClick={onEdit}
          >
            <Pencil className="size-3.5" strokeWidth={1.75} aria-hidden />
          </Button>
          <Button
            size="icon"
            aria-label={t('jobs.action.duplicate')}
            title={t('jobs.action.duplicate')}
            onClick={onDuplicate}
          >
            <CopyPlus className="size-3.5" strokeWidth={1.75} aria-hidden />
          </Button>
          <Button
            size="icon"
            aria-label={t('jobs.action.delete')}
            title={t('jobs.action.delete')}
            disabled={busy}
            onClick={onDelete}
          >
            <Trash2 className="size-3.5 text-danger" strokeWidth={1.75} aria-hidden />
          </Button>
        </div>
      </header>

      <section className="jobs-hero" data-health={health}>
        <div className="jobs-hero__next">
          <span className="mac-label">{t('jobs.detail.nextRun')}</span>
          <strong className="jobs-hero__countdown">{nextPrimary}</strong>
          {nextSecondary ? <span className="jobs-hero__abs">{nextSecondary}</span> : null}
        </div>
        <div className="jobs-hero__schedule">
          <span className="jobs-hero__sentence">{describeSchedule(job, now)}</span>
          <span className="jobs-hero__facts">
            {scheduleKind === 'cron' ? (
              <>
                <code>{String(job.expression || '')}</code>
                <span className="jobs-hero__dot" aria-hidden />
                <span>{String(job.tz || '') || t('jobs.detail.utc')}</span>
              </>
            ) : scheduleKind === 'every' ? (
              <>
                <span>{t('jobs.detail.interval')}</span>
                <code>{formatInterval(jobEverySeconds(job)) || String(job.scheduleRaw ?? '')}</code>
              </>
            ) : (
              <span>{t('jobs.detail.once')}</span>
            )}
          </span>
        </div>
        {upcoming.length > 1 ? (
          <ol className="jobs-timeline" aria-label={t('jobs.detail.upcoming')}>
            {upcoming.map((d, i) => {
              const { day, time } = timelineLabel(d, now)
              return (
                <li key={i} className="jobs-timeline__item">
                  <span className="jobs-timeline__tick" aria-hidden />
                  <span className="jobs-timeline__label">
                    <span>{day}</span>
                    <span className="jobs-timeline__time">{time}</span>
                  </span>
                </li>
              )
            })}
          </ol>
        ) : null}
      </section>

      <section className="jobs-facts">
        <Fact label={t('jobs.detail.runs')} value={String(job.run_count ?? 0)} mono />
        <Fact
          label={t('jobs.detail.errors')}
          value={String(job.error_count ?? 0)}
          mono
          tone={Number(job.error_count ?? 0) > 0 ? 'danger' : undefined}
        />
        <Fact
          label={t('jobs.detail.lastRun')}
          value={Number.isNaN(lastTs) ? t('jobs.detail.never') : countdown(lastTs, now)}
          hint={Number.isNaN(lastTs) ? undefined : clockLabel(new Date(lastTs), now)}
        />
        <Fact
          label={t('jobs.detail.target')}
          value={targetLabel(job)}
          hint={sessionKey || undefined}
        />
        <Fact label={t('jobs.detail.agent')} value={String(job.agentId || 'main')} mono />
        <Fact label={t('jobs.detail.delivery')} value={deliveryLabel(job)} />
        {createdFrom ? (
          <Fact label={t('jobs.detail.createdFrom')} value={createdFrom} mono />
        ) : null}
        <Fact
          label={t('jobs.detail.id')}
          value={id}
          mono
          action={
            <Button
              variant="ghost"
              size="icon"
              className="jobs-fact__copy"
              aria-label={t('jobs.detail.copyId')}
              title={t('jobs.detail.copyId')}
              onClick={() => void copyId()}
            >
              <Copy className="size-3" strokeWidth={1.75} aria-hidden />
            </Button>
          }
        />
      </section>

      {lastError && health === 'failing' ? (
        <section className="jobs-card jobs-card--danger">
          <span className="mac-label">{t('jobs.detail.lastError')}</span>
          <pre className="jobs-card__pre">{lastError}</pre>
        </section>
      ) : null}

      {message || script ? (
        <section className="jobs-card">
          {message ? (
            <>
              <span className="mac-label">
                {kind === 'reminder'
                  ? t('jobs.detail.reminder')
                  : kind === 'system_event'
                    ? t('jobs.detail.event')
                    : t('jobs.detail.prompt')}
              </span>
              <p className="jobs-card__text">{message}</p>
            </>
          ) : null}
          {script ? (
            <dl className="jobs-card__kv">
              <dt>{kind === 'script' ? t('jobs.detail.script') : t('jobs.detail.preRun')}</dt>
              <dd>
                <code>{script}</code>
              </dd>
              {Array.isArray(job.scriptArgs) && job.scriptArgs.length ? (
                <>
                  <dt>{t('jobs.detail.args')}</dt>
                  <dd>
                    <code>{(job.scriptArgs as unknown[]).map(String).join(' ')}</code>
                  </dd>
                </>
              ) : null}
              {job.workdir ? (
                <>
                  <dt>{t('jobs.detail.workdir')}</dt>
                  <dd>
                    <code>{String(job.workdir)}</code>
                  </dd>
                </>
              ) : null}
            </dl>
          ) : null}
        </section>
      ) : null}

      <RunHistory jobId={id} />
    </article>
  )
}

function Fact({
  label,
  value,
  hint,
  mono,
  tone,
  action,
}: {
  label: string
  value: string
  hint?: string
  mono?: boolean
  tone?: 'danger'
  action?: React.ReactNode
}) {
  return (
    <div className="jobs-fact" data-tone={tone}>
      <span className="mac-label">{label}</span>
      <span className="jobs-fact__row">
        <span className="jobs-fact__value" data-mono={mono ? 'true' : undefined} title={value}>
          {value}
        </span>
        {action}
      </span>
      {hint ? (
        <span className="jobs-fact__hint" title={hint}>
          {hint}
        </span>
      ) : null}
    </div>
  )
}

interface CronRunsResult {
  runs?: RawRun[]
}
interface CronRunOutput {
  output?: string
}

function RunHistory({ jobId }: { jobId: string }) {
  const rpc = useRpc()
  const navigate = useNavigate()
  const now = useNow(15_000)
  const [openId, setOpenId] = useState<string | null>(null)

  const runsQuery = useQuery<RawRun[]>({
    queryKey: ['cron', 'runs', jobId],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawRun[] | CronRunsResult>('cron.runs', { id: jobId, limit: 20 })
      return Array.isArray(data) ? data : (data.runs ?? [])
    },
    refetchOnWindowFocus: false,
  })

  const outputQuery = useQuery<CronRunOutput>({
    queryKey: ['cron', 'runOutput', jobId, openId],
    enabled: Boolean(openId),
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<CronRunOutput>('cron.runOutput', { id: jobId, runId: openId })
    },
    refetchOnWindowFocus: false,
  })

  const runs = runsQuery.data ?? []
  const outcomes = runOutcomes(runs, 12)

  return (
    <section className="jobs-runs" aria-label={t('jobs.runs.title')}>
      <header className="jobs-runs__head">
        <h2>{t('jobs.runs.title')}</h2>
        {outcomes.length ? (
          <span className="jobs-runs__strip" aria-hidden>
            {outcomes.map((o, i) => (
              <span key={i} data-outcome={o} />
            ))}
          </span>
        ) : null}
      </header>

      {runsQuery.isPending ? (
        <p className="jobs-runs__note">{t('jobs.runs.loading')}</p>
      ) : runsQuery.isError ? (
        <p className="jobs-runs__note text-danger">{t('jobs.runs.error')}</p>
      ) : runs.length === 0 ? (
        <p className="jobs-runs__note">{t('jobs.runs.empty')}</p>
      ) : (
        <ol className="jobs-runs__list">
          {runs.map((run, i) => {
            const runId = run.id ? String(run.id) : `#${i}`
            const ok = run.status === 'ok' || run.success === true
            const started = run.started_at != null ? new Date(run.started_at).getTime() : NaN
            const summary = run.summary ? String(run.summary) : ''
            const error = typeof run.error === 'string' ? run.error : ''
            const preview = (summary || error).split('\n').join(' ').trim()
            const hasOutput = Boolean(summary || error)
            const isOpen = openId === runId
            const canOpenChat = Boolean(run.sessionKey) && run.chatAvailable !== false
            const delivery = run.deliveryStatus ?? run.delivery_status
            const delivered =
              delivery && typeof delivery === 'object'
                ? Object.values(delivery as Record<string, unknown>).some(Boolean)
                : Boolean(delivery)
            return (
              <li key={runId} className="jobs-run" data-open={isOpen} data-ok={ok}>
                <div className="jobs-run__row">
                  <span className="jobs-run__light" aria-hidden />
                  <span className="jobs-run__time">
                    {Number.isNaN(started) ? '—' : clockLabel(new Date(started), now)}
                  </span>
                  <span className="jobs-run__status">
                    {ok ? t('jobs.runs.ok') : t('jobs.runs.failed')}
                  </span>
                  <span className="jobs-run__duration">{formatDurationMs(run.duration_ms)}</span>
                  {delivered ? (
                    <span className="mac-chip" data-tone="ok">
                      {t('jobs.runs.delivered')}
                    </span>
                  ) : null}
                  <span className="jobs-run__spacer" />
                  {canOpenChat ? (
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={t('jobs.runs.openChat')}
                      title={t('jobs.runs.openChat')}
                      onClick={() => void navigate(sessionPath(String(run.sessionKey)))}
                    >
                      <MessageSquare className="size-3.5" strokeWidth={1.75} aria-hidden />
                    </Button>
                  ) : null}
                  <button
                    type="button"
                    className="jobs-run__disclose"
                    disabled={!hasOutput}
                    aria-expanded={isOpen}
                    aria-label={isOpen ? t('jobs.runs.hideOutput') : t('jobs.runs.showOutput')}
                    onClick={() => setOpenId(isOpen ? null : runId)}
                  >
                    <ChevronRight className="size-3.5" strokeWidth={1.75} aria-hidden />
                  </button>
                </div>
                {!isOpen && preview ? (
                  <button
                    type="button"
                    className="jobs-run__preview"
                    onClick={() => setOpenId(runId)}
                  >
                    {preview.length > 160 ? `${preview.slice(0, 160)}…` : preview}
                  </button>
                ) : null}
                {isOpen ? (
                  <div className="jobs-run__output">
                    <pre>
                      {outputQuery.data?.output || summary || error || t('jobs.runs.noOutput')}
                    </pre>
                    {outputQuery.isPending && run.id ? (
                      <span className="jobs-runs__note">{t('jobs.runs.loadingOutput')}</span>
                    ) : outputQuery.isError ? (
                      <span className="jobs-runs__note">{t('jobs.runs.outputError')}</span>
                    ) : null}
                  </div>
                ) : null}
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}
