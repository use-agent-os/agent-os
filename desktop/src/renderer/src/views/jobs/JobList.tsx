import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import type { RawJob } from '@/views/cron/logic'
import { t } from '~/i18n'
import { quick } from '~/lib/motion'
import { useNow } from '~/lib/use-now'
import { countdown, describeSchedule, jobHealth } from './logic'

/**
 * The source list: one row per job, light for health, name, cadence in words,
 * and how soon it fires. Rows are buttons, not links, because selection here
 * is view state — the URL stays /jobs.
 */
export function JobList({
  jobs,
  loading,
  error,
  emptyText,
  selectedId,
  onSelect,
}: {
  jobs: RawJob[]
  loading: boolean
  error: string | null
  emptyText: string
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  // The trailing countdown only needs minute resolution at list width.
  const now = useNow(15_000)
  const reduce = useReducedMotion()

  return (
    <div className="jobs-list__rows" role="listbox" aria-label={t('jobs.title')}>
      {loading ? <p className="jobs-list__note">{t('jobs.list.loading')}</p> : null}
      {error ? <p className="jobs-list__note text-danger">{error}</p> : null}
      {!loading && !error && jobs.length === 0 ? (
        <p className="jobs-list__note">{emptyText}</p>
      ) : null}
      <AnimatePresence initial={false}>
        {jobs.map((job) => {
          const id = String(job.id ?? '')
          const health = jobHealth(job)
          const next = job.next_run ? new Date(job.next_run as string | number).getTime() : NaN
          const when = !job.enabled
            ? t('jobs.detail.paused')
            : health === 'running'
              ? t('jobs.health.running')
              : Number.isNaN(next)
                ? ''
                : next <= now
                  ? t('jobs.detail.awaiting')
                  : countdown(next, now)
          return (
            <motion.button
              key={id}
              type="button"
              role="option"
              aria-selected={selectedId === id}
              className="jobs-row app-no-drag"
              data-health={health}
              layout={!reduce}
              initial={reduce ? false : { opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduce ? undefined : { opacity: 0, height: 0 }}
              transition={quick}
              onClick={() => onSelect(id)}
            >
              <span className="jobs-row__light" aria-hidden />
              <span className="jobs-row__body">
                <span className="jobs-row__name">{String(job.name || job.id || '')}</span>
                <span className="jobs-row__sub">{describeSchedule(job, now)}</span>
              </span>
              <span className="jobs-row__when">{when}</span>
            </motion.button>
          )
        })}
      </AnimatePresence>
    </div>
  )
}
