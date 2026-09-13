import { useMemo } from 'react'
import { explainCron, nextRuns, parseCron, type CronForm } from '@/views/cron/logic'
import { t } from '~/i18n'
import {
  CADENCES,
  clockLabel,
  INTERVAL_UNITS,
  intervalToSeconds,
  localInputToIso,
  naturalToCron,
  timeZoneOptions,
  type BuilderState,
  type Cadence,
  type IntervalUnit,
  type NaturalSchedule,
} from './logic'

type ScheduleFields = Pick<CronForm, 'scheduleKind' | 'cron' | 'every' | 'at' | 'tz'>

const KINDS: ReadonlyArray<{ value: CronForm['scheduleKind']; label: string }> = [
  { value: 'cron', label: 'jobs.schedule.repeat' },
  { value: 'every', label: 'jobs.schedule.interval' },
  { value: 'at', label: 'jobs.schedule.once' },
] as const

/**
 * The schedule half of the sheet. People pick a cadence in words; the cron
 * expression is derived and shown, and only becomes the input when they ask
 * for it. Interval and one-time jobs get native number/date controls instead
 * of "seconds" and "ISO 8601".
 */
export function ScheduleEditor({
  form,
  builder,
  onForm,
  onBuilder,
}: {
  form: ScheduleFields
  builder: BuilderState
  onForm: (patch: Partial<ScheduleFields>) => void
  onBuilder: (patch: Partial<BuilderState>) => void
}) {
  const zones = useMemo(() => timeZoneOptions(), [])

  function setNatural(patch: Partial<NaturalSchedule>) {
    const natural = { ...builder.natural, ...patch }
    onBuilder({ natural })
    if (natural.cadence !== 'custom') onForm({ cron: naturalToCron(natural) })
  }

  function setCadence(cadence: Cadence) {
    const natural = { ...builder.natural, cadence }
    onBuilder({ natural })
    // Switching to custom keeps whatever the builder produced so far as the
    // starting text; switching away rebuilds from the builder's own state.
    if (cadence !== 'custom') onForm({ cron: naturalToCron(natural) })
  }

  function setInterval(patch: Partial<BuilderState['interval']>) {
    const interval = { ...builder.interval, ...patch }
    onBuilder({ interval })
    const seconds = intervalToSeconds(interval.value, interval.unit)
    onForm({ every: seconds ? String(seconds) : '' })
  }

  const natural = builder.natural
  const expression = form.cron.trim()
  const parsed = expression ? parseCron(expression) : null
  const preview = parsed ? nextRuns(parsed, 3) : []
  const sentence = parsed ? explainCron(expression) : ''

  return (
    <div className="jobs-form__stack">
      <div role="radiogroup" aria-label={t('jobs.section.schedule')} className="mac-segmented">
        {KINDS.map((k) => (
          <button
            key={k.value}
            type="button"
            role="radio"
            aria-checked={form.scheduleKind === k.value}
            className="mac-segment"
            onClick={() => onForm({ scheduleKind: k.value })}
          >
            {t(k.label as 'jobs.schedule.repeat')}
          </button>
        ))}
      </div>

      {form.scheduleKind === 'cron' ? (
        <>
          <div className="jobs-form__row">
            <span className="mac-label">{t('jobs.schedule.when')}</span>
            <div className="jobs-form__inline">
              <select
                className="mac-select"
                data-compact="true"
                aria-label={t('jobs.schedule.when')}
                value={natural.cadence}
                onChange={(e) => setCadence(e.target.value as Cadence)}
              >
                {CADENCES.map((c) => (
                  <option key={c} value={c}>
                    {t(`jobs.cadence.${c}`)}
                  </option>
                ))}
              </select>

              {natural.cadence === 'daily' || natural.cadence === 'weekly' ? (
                <>
                  <span className="jobs-form__glue">{t('jobs.schedule.at').toLowerCase()}</span>
                  <input
                    className="mac-input"
                    type="time"
                    aria-label={t('jobs.schedule.at')}
                    value={natural.time}
                    onChange={(e) => setNatural({ time: e.target.value || '09:00' })}
                  />
                </>
              ) : null}

              {natural.cadence === 'hourly' ? (
                <>
                  <span className="jobs-form__glue">
                    {t('jobs.schedule.atMinute').toLowerCase()}
                  </span>
                  <input
                    className="mac-input jobs-form__num"
                    type="number"
                    min={0}
                    max={59}
                    aria-label={t('jobs.schedule.atMinute')}
                    value={Number(natural.time.split(':')[1] ?? 0)}
                    onChange={(e) => {
                      const m = Math.min(59, Math.max(0, Number(e.target.value) || 0))
                      setNatural({ time: `00:${String(m).padStart(2, '0')}` })
                    }}
                  />
                </>
              ) : null}

              {natural.cadence === 'minutes' ? (
                <>
                  <span className="jobs-form__glue">{t('jobs.schedule.every').toLowerCase()}</span>
                  <input
                    className="mac-input jobs-form__num"
                    type="number"
                    min={1}
                    max={59}
                    aria-label={t('jobs.schedule.every')}
                    value={natural.everyMinutes}
                    onChange={(e) =>
                      setNatural({
                        everyMinutes: Math.min(59, Math.max(1, Number(e.target.value) || 1)),
                      })
                    }
                  />
                  <span className="jobs-form__glue">{t('jobs.schedule.minutes')}</span>
                </>
              ) : null}
            </div>
          </div>

          {natural.cadence === 'weekly' ? (
            <div className="jobs-form__row">
              <span className="mac-label">{t('jobs.schedule.days')}</span>
              <div className="jobs-days" role="group" aria-label={t('jobs.schedule.days')}>
                {[1, 2, 3, 4, 5, 6, 0].map((d) => {
                  const on = natural.days.includes(d)
                  return (
                    <button
                      key={d}
                      type="button"
                      className="jobs-day"
                      aria-pressed={on}
                      onClick={() =>
                        setNatural({
                          days: on ? natural.days.filter((x) => x !== d) : [...natural.days, d],
                        })
                      }
                    >
                      {t(`jobs.dow.${d as 0 | 1 | 2 | 3 | 4 | 5 | 6}`)}
                    </button>
                  )
                })}
              </div>
            </div>
          ) : null}

          <div className="jobs-form__row">
            <span className="mac-label">{t('jobs.schedule.expression')}</span>
            <div className="mac-field">
              {natural.cadence === 'custom' ? (
                <input
                  className="mac-input"
                  data-mono="true"
                  data-invalid={expression && !parsed ? 'true' : undefined}
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="0 9 * * 1-5"
                  aria-label={t('jobs.schedule.expression')}
                  value={form.cron}
                  onChange={(e) => onForm({ cron: e.target.value })}
                />
              ) : (
                <button
                  type="button"
                  className="jobs-expr"
                  title={t('jobs.cadence.custom')}
                  onClick={() => setCadence('custom')}
                >
                  <code>{expression || '—'}</code>
                </button>
              )}
              <span className="mac-help">
                {expression && !parsed
                  ? t('jobs.schedule.invalid')
                  : natural.cadence === 'custom'
                    ? t('jobs.schedule.expression.help')
                    : sentence}
              </span>
            </div>
          </div>

          {preview.length ? (
            <div className="jobs-form__row">
              <span className="mac-label">{t('jobs.schedule.preview')}</span>
              <ol className="jobs-preview">
                {preview.map((d, i) => (
                  <li key={i}>{clockLabel(d)}</li>
                ))}
              </ol>
            </div>
          ) : null}

          <div className="jobs-form__row">
            <span className="mac-label">{t('jobs.schedule.timezone')}</span>
            <select
              className="mac-select"
              aria-label={t('jobs.schedule.timezone')}
              value={form.tz || 'UTC'}
              onChange={(e) => onForm({ tz: e.target.value === 'UTC' ? '' : e.target.value })}
            >
              {zones.map((z) => (
                <option key={z} value={z}>
                  {z.replace(/_/g, ' ')}
                </option>
              ))}
            </select>
          </div>
        </>
      ) : null}

      {form.scheduleKind === 'every' ? (
        <div className="jobs-form__row">
          <span className="mac-label">{t('jobs.schedule.intervalEvery')}</span>
          <div className="jobs-form__inline">
            <input
              className="mac-input jobs-form__num"
              type="number"
              min={1}
              aria-label={t('jobs.schedule.intervalEvery')}
              value={builder.interval.value}
              onChange={(e) => setInterval({ value: Math.max(1, Number(e.target.value) || 1) })}
            />
            <select
              className="mac-select"
              data-compact="true"
              aria-label={t('jobs.schedule.interval')}
              value={builder.interval.unit}
              onChange={(e) => setInterval({ unit: e.target.value as IntervalUnit })}
            >
              {INTERVAL_UNITS.map((u) => (
                <option key={u} value={u}>
                  {t(`jobs.unit.${u}`)}
                </option>
              ))}
            </select>
          </div>
        </div>
      ) : null}

      {form.scheduleKind === 'at' ? (
        <div className="jobs-form__row">
          <span className="mac-label">{t('jobs.schedule.at')}</span>
          <input
            className="mac-input"
            type="datetime-local"
            aria-label={t('jobs.schedule.at')}
            value={builder.atLocal}
            onChange={(e) => {
              onBuilder({ atLocal: e.target.value })
              onForm({ at: localInputToIso(e.target.value) })
            }}
          />
        </div>
      ) : null}
    </div>
  )
}
