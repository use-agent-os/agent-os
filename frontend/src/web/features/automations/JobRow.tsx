import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  History,
  Loader2,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  ShieldAlert,
  Trash2,
  Zap,
} from 'lucide-react'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import { copyWithFallback } from '@/lib/clipboard'
import type { Gate } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  CONCURRENCY_POLICIES,
  concurrencyLabel,
  explainCron,
  humanCountdownPast,
  isOkStatus,
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
  type ConcurrencyPolicy,
  type RawJob,
} from '@/views/cron/logic'
import { Badge, StatusDot } from '@/web/components/ui/badge'
import { Button } from '@/web/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuItemIndicator,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'
import { useNow } from './schedule'
import { laneTone } from './Upcoming'

// One automation as a row: the schedule in words is the headline (cron small
// beside it), then when it runs next (a ticking countdown) and last, and the
// actions grouped: Run now, then Pause / History / Edit, and the rest (copy
// the id, the overlap policy, Delete) in the row menu.

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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
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
      <span className="block font-medium tabular-nums">{nextRunText(job, now)}</span>
      {abs ? <span className="block text-small text-text-muted">{abs}</span> : null}
    </>
  )
}

type Status = { tone: 'ok' | 'warn' | 'danger'; text: string }

export function JobRow({
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
  const [status, setStatus] = useState<Status | null>(null)

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

  const act = async (kind: 'run' | 'toggle' | 'concurrency', fn: () => Promise<Status | null>) => {
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

  const stateLabel = running
    ? t('webPagesWork.stateRunning')
    : health?.degraded
      ? t('webPagesWork.stateDegraded')
      : dot === 'error'
        ? t('webPagesWork.stateFailing')
        : enabled
          ? t('webPagesWork.stateActive')
          : t('webPagesWork.statePaused')
  const stateTone = running ? 'accent' : failing ? 'danger' : enabled ? 'ok' : 'neutral'
  const blocked = busy !== null || control.disabled

  return (
    <article
      aria-label={name}
      className="am-row"
      data-paused={!enabled || undefined}
      data-failing={failing || undefined}
      data-degraded={health?.degraded ? 'true' : undefined}
    >
      <div className="am-row-head">
        <StatusDot tone={stateTone} live={running} className="size-2 shrink-0" />
        <h2 className="min-w-0 truncate font-medium">{name}</h2>
        <span className="am-kind" data-tone={laneTone(job)}>
          {jobKindLabel(job)}
        </span>
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
      </div>

      <div className="am-row-when">
        <p className="am-row-schedule">{human || schedule}</p>
        {human ? (
          <code className="truncate font-mono text-small text-text-muted" title={schedule}>
            {schedule}
          </code>
        ) : null}
      </div>

      <dl className="am-row-meta">
        <div>
          <dt>{t('webPagesWork.cardNextRun')}</dt>
          <dd>
            {enabled ? (
              <NextRun job={job} />
            ) : (
              <span className="block font-medium text-text-muted">
                {t('webPagesWork.statePaused')}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt>{t('webPagesWork.cardLastRun')}</dt>
          <dd>
            <span className="block">
              {job.last_run
                ? humanCountdownPast(new Date(job.last_run as string | number))
                : t('webPagesWork.lastRunNever')}
            </span>
            {lastStatus ? (
              <span
                className={cn(
                  'block text-small',
                  isOkStatus(lastStatus) ? 'text-ok' : 'text-danger',
                )}
              >
                {lastStatus}
              </span>
            ) : null}
          </dd>
        </div>
        <div className="am-row-target">
          <dt>{t('webPagesWork.cardTarget')}</dt>
          <dd className="truncate" title={jobTarget(job)}>
            {jobTarget(job)}
          </dd>
        </div>
      </dl>

      <div className="am-row-actions">
        <Button
          size="sm"
          variant="outline"
          disabled={blocked}
          aria-describedby={control.reason ? reasonId : undefined}
          aria-label={t('webPagesWork.runNowFor', { name })}
          onClick={() => void runNow()}
        >
          {busy === 'run' ? <Loader2 className="animate-spin" /> : <Zap />}
          {t('webPagesWork.runNow')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="text-text-muted hover:text-text"
          disabled={blocked}
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
          <span className="am-label">
            {enabled ? t('webPagesWork.pause') : t('webPagesWork.resume')}
          </span>
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          className="text-text-muted hover:text-text"
          title={t('webPagesWork.history')}
          aria-label={t('webPagesWork.historyFor', { name })}
          onClick={onHistory}
        >
          <History />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          className="text-text-muted hover:text-text"
          title={t('webPagesWork.edit')}
          aria-label={t('webPagesWork.editFor', { name })}
          onClick={onEdit}
        >
          <Pencil />
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              size="icon-sm"
              variant="ghost"
              className="text-text-muted hover:text-text"
              title={t('webPagesWork.moreActions')}
              aria-label={t('webPagesWork.moreActionsFor', { name })}
            >
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-56">
            {id ? (
              <DropdownMenuItem onSelect={() => void copyId()}>
                <Copy />
                <span className="flex-1">{t('webPagesWork.copyIdItem')}</span>
                <span className="font-mono text-small text-text-muted">{shortCronId(id)}</span>
              </DropdownMenuItem>
            ) : null}
            {concurrency ? (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuLabel>{t('webPagesWork.cardOverlap')}</DropdownMenuLabel>
                <DropdownMenuRadioGroup
                  value={concurrency}
                  onValueChange={(v) => void setConcurrency(v as ConcurrencyPolicy)}
                >
                  {CONCURRENCY_POLICIES.map((policy) => (
                    <DropdownMenuRadioItem
                      key={policy}
                      value={policy}
                      disabled={blocked}
                      className="pl-7"
                    >
                      <DropdownMenuItemIndicator className="absolute left-2 grid size-4 place-items-center">
                        <span className="size-1.5 rounded-full bg-accent" />
                      </DropdownMenuItemIndicator>
                      {concurrencyLabel(policy)}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </>
            ) : null}
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="danger" onSelect={onDelete}>
              <Trash2 />
              {t('webPagesWork.delete')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {message || createdFrom ? (
        <p className="am-row-note">
          {message ? <span className="truncate">{message}</span> : null}
          {createdFrom ? (
            <span className="shrink-0 font-mono" title={createdFrom}>
              {t('webPagesWork.createdFromShort', { source: createdFrom })}
            </span>
          ) : null}
        </p>
      ) : null}

      {troubled || health?.lastError ? (
        <div role="note" className="am-row-trouble">
          <AlertTriangle aria-hidden data-status-icon />
          <span className="min-w-0">
            {troubled ? <span className="font-medium">{failures}</span> : null}
            {troubled && health?.lastError ? ' · ' : null}
            {health?.lastError ? (
              <span className="font-mono break-words">{health.lastError}</span>
            ) : null}
          </span>
        </div>
      ) : null}

      {status ? (
        <p
          role={status.tone === 'danger' ? 'alert' : 'status'}
          className={cn(
            'am-row-status anim-fade-in',
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

      {control.reason ? (
        <span id={reasonId} className="am-row-reason text-small text-text-muted">
          {control.reason}
        </span>
      ) : null}
    </article>
  )
}
