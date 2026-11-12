import { PauseCircle } from 'lucide-react'
import { useId, useMemo } from 'react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'
import {
  explainCron,
  humanCountdown,
  humanTime,
  jobKindClass,
  jobSchedule,
  type RawJob,
} from '@/views/cron/logic'
import { StatusDot } from '@/web/components/ui/badge'
import { jobTimesBetween, nextRunAt, useNow } from './schedule'
import { Timeline, type Lane, type LaneTone } from './Timeline'

// The top of a populated Automations page: what runs next, large, with a
// ticking countdown and the few runs after it, beside the next 24 hours drawn
// as one lane per automation.

const DAY = 86_400_000
const LATER = 3

/** A lane's colour by what the job does: reminders and scripts vs agent work. */
export function laneTone(job: RawJob): LaneTone {
  return jobKindClass(job) === 'is-agent' ? 'info' : 'accent'
}

interface Run {
  ts: number
  job: RawJob
}

/** Countdown text that ticks on its own, so nothing around it re-renders. */
function Countdown({ ts, className }: { ts: number; className?: string }) {
  const now = useNow(true, 1000)
  return <span className={className}>{humanCountdown(new Date(ts), now)}</span>
}

export function Upcoming({ jobs, animate }: { jobs: RawJob[]; animate: boolean }) {
  const titleId = useId()
  const lanesId = useId()
  // The window moves once a minute; the countdowns tick on their own.
  const minute = Math.floor(useNow(true, 30_000) / 60_000) * 60_000

  const enabled = useMemo(
    () =>
      jobs
        .filter((j) => j.enabled)
        .sort((a, b) => (nextRunAt(a) ?? Infinity) - (nextRunAt(b) ?? Infinity)),
    [jobs],
  )
  const lanes: Lane[] = useMemo(
    () =>
      enabled.slice(0, 8).map((job, i) => ({
        id: String(job.id ?? i),
        name: String(job.name || job.id || ''),
        tone: laneTone(job),
        times: jobTimesBetween(job, minute, minute + DAY),
      })),
    [enabled, minute],
  )
  // Each job's next run (the scheduler's own next_run), soonest first: the
  // lead is the first, "Then" the next few other jobs, so a job that fires
  // every few minutes does not fill the whole list.
  const runs: Run[] = useMemo(
    () =>
      enabled
        .map((job) => ({ ts: nextRunAt(job), job }))
        .filter((run): run is Run => run.ts != null)
        .sort((a, b) => a.ts - b.ts)
        .slice(0, LATER + 1),
    [enabled],
  )
  const totalRuns = lanes.reduce((n, lane) => n + lane.times.length, 0)
  const [next, ...later] = runs

  return (
    <div className="am-upcoming">
      <section
        aria-labelledby={titleId}
        className="am-next"
        data-tone={next ? laneTone(next.job) : 'muted'}
      >
        <h2 id={titleId} className="am-eyebrow flex items-center gap-2">
          {next ? <StatusDot tone="accent" live className="size-1.5" /> : null}
          {t('webPagesWork.upNext')}
        </h2>
        {next ? (
          <>
            <p className="am-next-count tabular-nums">
              <Countdown ts={next.ts} />
            </p>
            <div className="flex min-w-0 flex-col gap-0.5">
              <p className="truncate text-section">{String(next.job.name || next.job.id)}</p>
              <p className="truncate text-small text-text-muted">
                {explainCron(String(next.job.expression || '')) || jobSchedule(next.job)}
                {' · '}
                {humanTime(new Date(next.ts))}
              </p>
            </div>
            {later.length ? (
              <ol role="list" aria-label={t('webPagesWork.upNextThen')} className="am-later">
                {later.map((run) => (
                  <li key={`${String(run.job.id)}:${run.ts}`} className="am-later-row">
                    <span aria-hidden className="am-lane-dot" data-tone={laneTone(run.job)} />
                    <span className="min-w-0 flex-1 truncate">
                      {String(run.job.name || run.job.id)}
                    </span>
                    <Countdown
                      ts={run.ts}
                      className="shrink-0 text-small text-text-muted tabular-nums"
                    />
                  </li>
                ))}
              </ol>
            ) : null}
          </>
        ) : (
          <div className="flex flex-col gap-1.5">
            <p className="flex items-center gap-2 text-section [&_svg]:size-5 [&_svg]:text-text-muted">
              <PauseCircle aria-hidden />
              {t('webPagesWork.upNextNone')}
            </p>
            <p className="text-small text-pretty text-text-muted">
              {t('webPagesWork.upNextNoneBody')}
            </p>
          </div>
        )}
      </section>

      <section aria-labelledby={lanesId} className="am-day">
        <header className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
          <h2 id={lanesId} className="am-eyebrow">
            {t('webPagesWork.next24')}
          </h2>
          <p className="text-small text-text-muted tabular-nums">
            {tPlural('webPagesWork.next24Runs', totalRuns)}
          </p>
        </header>
        {lanes.length ? (
          <Timeline
            lanes={lanes}
            from={minute}
            span="day"
            animate={animate}
            label={t('webPagesWork.next24')}
          />
        ) : (
          <p className="text-small text-text-muted">{t('webPagesWork.upNextNoneBody')}</p>
        )}
        <p aria-hidden className="am-legend">
          <span>
            <span className="am-lane-dot" data-tone="accent" />
            {t('webPagesWork.legendReminder')}
          </span>
          <span>
            <span className="am-lane-dot" data-tone="info" />
            {t('webPagesWork.legendAgent')}
          </span>
        </p>
      </section>
    </div>
  )
}
