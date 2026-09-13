// Desktop-only derivations for the Scheduled jobs view. Everything that talks
// to the gateway or parses a cron expression is the console's
// (`@/views/cron/logic`); this module owns what the desktop presents
// differently: health buckets for the list, the natural-language schedule
// builder that fronts the raw expression, interval/one-time conversions for
// native pickers, and the compact countdown the detail pane ticks.

import {
  explainCron,
  nextRuns,
  parseCron,
  type CronForm,
  type RawJob,
  type RawRun,
} from '@/views/cron/logic'

// ── Health ──────────────────────────────────────────────────────────────────

/**
 * One word for how a job is doing, for the list light and the filter chips.
 * `failing` wins over `active` so a job that is still scheduled but has been
 * erroring is findable; the console shows this only as a red dot.
 */
export type JobHealth = 'running' | 'failing' | 'active' | 'paused'

export function jobHealth(job: RawJob): JobHealth {
  if (!job.enabled) return 'paused'
  if (job.status === 'running') return 'running'
  const consecutive = Number(job.consecutive_errors ?? 0)
  const lastStatus = job.last_status
  if (consecutive > 0 || lastStatus === 'error' || lastStatus === 'fail') return 'failing'
  if (job.lastResult && job.last_run) return 'failing'
  return 'active'
}

export type JobFilter = 'all' | 'active' | 'paused' | 'failing'

export const JOB_FILTERS: readonly JobFilter[] = ['all', 'active', 'paused', 'failing']

export function matchesFilter(job: RawJob, filter: JobFilter): boolean {
  if (filter === 'all') return true
  const health = jobHealth(job)
  if (filter === 'active') return health === 'active' || health === 'running'
  return health === filter
}

export function filterCounts(jobs: readonly RawJob[]): Record<JobFilter, number> {
  const counts: Record<JobFilter, number> = { all: jobs.length, active: 0, paused: 0, failing: 0 }
  for (const job of jobs) {
    const health = jobHealth(job)
    if (health === 'paused') counts.paused += 1
    else if (health === 'failing') counts.failing += 1
    else counts.active += 1
  }
  return counts
}

function epoch(value: unknown): number {
  if (value == null || value === '') return NaN
  const ts = new Date(value as string | number).getTime()
  return Number.isNaN(ts) ? NaN : ts
}

/**
 * List order: what fires soonest first, paused jobs last, ties by name. A
 * failing job keeps its slot — it is still scheduled — and gets the red light
 * instead of being pushed around.
 */
export function orderJobs<T extends RawJob>(jobs: readonly T[]): T[] {
  return [...jobs].sort((a, b) => {
    const aPaused = !a.enabled
    const bPaused = !b.enabled
    if (aPaused !== bPaused) return aPaused ? 1 : -1
    const an = epoch(a.next_run)
    const bn = epoch(b.next_run)
    const aHas = !Number.isNaN(an)
    const bHas = !Number.isNaN(bn)
    if (aHas && bHas && an !== bn) return an - bn
    if (aHas !== bHas) return aHas ? -1 : 1
    return String(a.name || '').localeCompare(String(b.name || ''))
  })
}

// ── Schedule description ────────────────────────────────────────────────────

const SECOND = 1000
const MINUTE = 60 * SECOND
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export function jobScheduleKind(job: RawJob): 'cron' | 'every' | 'at' {
  const kind = String(job.scheduleKind || job.schedule_kind || 'cron')
  return kind === 'every' || kind === 'at' ? kind : 'cron'
}

/** The interval in seconds behind an `every` job, or 0 when it is not one. */
export function jobEverySeconds(job: RawJob): number {
  if (jobScheduleKind(job) !== 'every') return 0
  const raw = job.scheduleRaw ?? job.schedule_raw
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 ? n : 0
}

/** "5 min", "2 h", "1 d 4 h": a compact interval, whole units only. */
export function formatInterval(seconds: number): string {
  if (seconds <= 0) return ''
  if (seconds % 86400 === 0) return `${seconds / 86400} d`
  if (seconds % 3600 === 0) return `${seconds / 3600} h`
  if (seconds % 60 === 0) return `${seconds / 60} min`
  return `${seconds} s`
}

/**
 * One sentence for a job's cadence, whatever kind it is: the cron humanizer's
 * words, "Every 5 min" for an interval, a date for a one-time job. The raw
 * expression is shown next to it, never instead of it.
 */
export function describeSchedule(job: RawJob, now: number = Date.now()): string {
  const kind = jobScheduleKind(job)
  if (kind === 'every') {
    const seconds = jobEverySeconds(job)
    return seconds ? `Every ${formatInterval(seconds)}` : 'Fixed interval'
  }
  if (kind === 'at') {
    const raw = job.scheduleRaw ?? job.schedule_raw ?? job.next_run
    const ts = epoch(raw)
    if (Number.isNaN(ts)) return 'Once'
    return `Once, ${clockLabel(new Date(ts), now)}`
  }
  return explainCron(String(job.expression || '')) || 'Custom cadence'
}

// ── Countdown / clock ───────────────────────────────────────────────────────

/** "in 4h 12m", "in 32s", "now", "3m ago": two units at most, no seconds past a minute. */
export function countdown(target: number, now: number = Date.now()): string {
  const diff = target - now
  const abs = Math.abs(diff)
  if (abs < SECOND) return 'now'
  let text: string
  if (abs < MINUTE) text = `${Math.floor(abs / SECOND)}s`
  else if (abs < HOUR) text = `${Math.floor(abs / MINUTE)}m ${Math.floor((abs % MINUTE) / SECOND)}s`
  else if (abs < DAY) text = `${Math.floor(abs / HOUR)}h ${Math.floor((abs % HOUR) / MINUTE)}m`
  else text = `${Math.floor(abs / DAY)}d ${Math.floor((abs % DAY) / HOUR)}h`
  return diff > 0 ? `in ${text}` : `${text} ago`
}

/** "Today 09:00", "Tomorrow 09:00", "Mon 15 Sep, 09:00". */
export function clockLabel(date: Date, now: number = Date.now()): string {
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  const dayIndex = Math.floor((date.getTime() - start.getTime()) / DAY)
  const clock = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (dayIndex === 0 && date.getTime() >= start.getTime()) return `Today ${clock}`
  if (dayIndex === 1) return `Tomorrow ${clock}`
  if (dayIndex === -1) return `Yesterday ${clock}`
  const day = date.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })
  return `${day}, ${clock}`
}

/** Two short lines for a timeline tick: "Tomorrow" / "Mon 14" over the clock time. */
export function timelineLabel(date: Date, now: number = Date.now()): { day: string; time: string } {
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  const dayIndex = Math.floor((date.getTime() - start.getTime()) / DAY)
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const day =
    dayIndex === 0
      ? 'Today'
      : dayIndex === 1
        ? 'Tomorrow'
        : date.toLocaleDateString([], { weekday: 'short', day: 'numeric' })
  return { day, time }
}

/**
 * The next `count` fire times for a job, so the detail pane can draw a
 * timeline: cron from its expression, interval from next_run stepping by the
 * period, one-time as its single date. Empty for a paused job — a timeline for
 * something that will not fire is a lie.
 */
export function upcomingRuns(job: RawJob, count: number, now: number = Date.now()): Date[] {
  if (!job.enabled) return []
  const kind = jobScheduleKind(job)
  const next = epoch(job.next_run)
  if (kind === 'at') return Number.isNaN(next) || next <= now ? [] : [new Date(next)]
  if (kind === 'every') {
    const seconds = jobEverySeconds(job)
    if (!seconds) return Number.isNaN(next) ? [] : [new Date(next)]
    const first = Number.isNaN(next) || next <= now ? now + seconds * SECOND : next
    return Array.from({ length: count }, (_, i) => new Date(first + i * seconds * SECOND))
  }
  const parsed = parseCron(String(job.expression || ''))
  if (!parsed) return Number.isNaN(next) ? [] : [new Date(next)]
  return nextRuns(parsed, count, now)
}

// ── Natural schedule builder ────────────────────────────────────────────────

/**
 * The cadences the builder can express without showing a cron expression.
 * `custom` is the escape hatch: the expression field becomes editable and the
 * other controls step aside.
 */
export type Cadence = 'daily' | 'weekly' | 'hourly' | 'minutes' | 'custom'

export const CADENCES: readonly Cadence[] = ['daily', 'weekly', 'hourly', 'minutes', 'custom']

export interface NaturalSchedule {
  cadence: Cadence
  /** "HH:MM" for daily/weekly; the minute of hourly is read from it too. */
  time: string
  /** 0 = Sunday … 6 = Saturday; weekly only. */
  days: number[]
  /** every N minutes; minutes only. */
  everyMinutes: number
}

export const WEEKDAYS = [1, 2, 3, 4, 5]
export const DEFAULT_NATURAL: NaturalSchedule = {
  cadence: 'daily',
  time: '09:00',
  days: WEEKDAYS,
  everyMinutes: 15,
}

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function splitTime(time: string): { h: number; m: number } {
  const match = /^(\d{1,2}):(\d{2})$/.exec(time.trim())
  if (!match) return { h: 9, m: 0 }
  const h = Math.min(23, Math.max(0, Number(match[1])))
  const m = Math.min(59, Math.max(0, Number(match[2])))
  return { h, m }
}

/** Build the 5-field expression for a natural schedule. `custom` yields ''. */
export function naturalToCron(schedule: NaturalSchedule): string {
  const { h, m } = splitTime(schedule.time)
  switch (schedule.cadence) {
    case 'daily':
      return `${m} ${h} * * *`
    case 'weekly': {
      const days = [...new Set(schedule.days)].filter((d) => d >= 0 && d <= 6).sort((a, b) => a - b)
      return `${m} ${h} * * ${days.length ? days.join(',') : '*'}`
    }
    case 'hourly':
      return `${m} * * * *`
    case 'minutes': {
      const n = Math.min(59, Math.max(1, Math.floor(schedule.everyMinutes || 1)))
      return n === 1 ? '* * * * *' : `*/${n} * * * *`
    }
    case 'custom':
      return ''
  }
}

/**
 * Recognise an expression as one of the natural cadences, so an existing job
 * opens in the builder with the right controls, not as raw text. Anything
 * with a day-of-month, month, or an irregular minute list is `custom`.
 */
export function cronToNatural(expr: string): NaturalSchedule {
  const parsed = parseCron(expr)
  if (!parsed) return { ...DEFAULT_NATURAL, cadence: 'custom' }
  const { minute, hour, dom, month, dow } = parsed
  if (!dom.all || !month.all) return { ...DEFAULT_NATURAL, cadence: 'custom' }

  const minutes = minute.all ? null : [...minute.set!].sort((a, b) => a - b)
  const hours = hour.all ? null : [...hour.set!].sort((a, b) => a - b)

  if (minutes && minutes.length === 1 && hours && hours.length === 1) {
    const time = `${pad2(hours[0]!)}:${pad2(minutes[0]!)}`
    if (dow.all) return { ...DEFAULT_NATURAL, cadence: 'daily', time }
    const days = [...dow.set!].sort((a, b) => a - b)
    return { ...DEFAULT_NATURAL, cadence: 'weekly', time, days }
  }
  if (!dow.all) return { ...DEFAULT_NATURAL, cadence: 'custom' }
  if (minutes && minutes.length === 1 && !hours) {
    return { ...DEFAULT_NATURAL, cadence: 'hourly', time: `00:${pad2(minutes[0]!)}` }
  }
  if (!hours) {
    if (!minutes) return { ...DEFAULT_NATURAL, cadence: 'minutes', everyMinutes: 1 }
    if (minutes.length > 1 && minutes[0] === 0) {
      const step = minutes[1]! - minutes[0]!
      const regular = minutes.every((v, i) => v === i * step)
      const complete = 60 - minutes[minutes.length - 1]! <= step
      if (regular && complete) {
        return { ...DEFAULT_NATURAL, cadence: 'minutes', everyMinutes: step }
      }
    }
  }
  return { ...DEFAULT_NATURAL, cadence: 'custom' }
}

// ── Interval (every) ↔ value + unit ─────────────────────────────────────────

export type IntervalUnit = 'seconds' | 'minutes' | 'hours' | 'days'

export const INTERVAL_UNITS: readonly IntervalUnit[] = ['seconds', 'minutes', 'hours', 'days']

const UNIT_SECONDS: Record<IntervalUnit, number> = {
  seconds: 1,
  minutes: 60,
  hours: 3600,
  days: 86400,
}

export function intervalToSeconds(value: number, unit: IntervalUnit): number {
  if (!Number.isFinite(value) || value <= 0) return 0
  return Math.round(value * UNIT_SECONDS[unit])
}

/** The largest unit that divides the interval evenly, so 3600 shows as 1 hour. */
export function secondsToInterval(seconds: number): { value: number; unit: IntervalUnit } {
  if (!Number.isFinite(seconds) || seconds <= 0) return { value: 15, unit: 'minutes' }
  for (const unit of ['days', 'hours', 'minutes'] as const) {
    if (seconds % UNIT_SECONDS[unit] === 0) return { value: seconds / UNIT_SECONDS[unit], unit }
  }
  return { value: seconds, unit: 'seconds' }
}

// ── One-time (at) ↔ datetime-local ──────────────────────────────────────────

/** ISO (any offset) → the "YYYY-MM-DDTHH:MM" a datetime-local input holds, in local time. */
export function isoToLocalInput(iso: string): string {
  const ts = epoch(iso)
  if (Number.isNaN(ts)) return ''
  const d = new Date(ts)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/**
 * datetime-local value → ISO 8601 with the machine's offset spelled out, so
 * the gateway stores the instant the person meant rather than re-reading the
 * wall-clock string as UTC.
 */
export function localInputToIso(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim())
  if (!match) return ''
  const d = new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
    Number(match[6] ?? 0),
  )
  if (Number.isNaN(d.getTime())) return ''
  const offsetMin = -d.getTimezoneOffset()
  const sign = offsetMin >= 0 ? '+' : '-'
  const abs = Math.abs(offsetMin)
  const offset = `${sign}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`
  return `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${pad2(Number(match[6] ?? 0))}${offset}`
}

/** The Mac's IANA zone, or '' when the runtime cannot say (then the gateway uses UTC). */
export function localTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch {
    return ''
  }
}

/** IANA zones for the picker; the local one first, then the rest alphabetically. */
export function timeZoneOptions(local: string = localTimeZone()): string[] {
  let zones: string[] = []
  try {
    const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] }
    zones = intl.supportedValuesOf ? intl.supportedValuesOf('timeZone') : []
  } catch {
    zones = []
  }
  const rest = zones.filter((z) => z !== local && z !== 'UTC').sort()
  return [...(local ? [local] : []), 'UTC', ...rest]
}

// ── Form seeding for the desktop builder ────────────────────────────────────

/**
 * The builder's own state, derived from the shared form once when the sheet
 * opens. A new job starts on the Mac's own clock: daily at 09:00 in the local
 * zone, not a blank expression evaluated in UTC.
 */
export interface BuilderState {
  natural: NaturalSchedule
  interval: { value: number; unit: IntervalUnit }
  atLocal: string
}

export function seedBuilder(form: CronForm, isEdit: boolean): BuilderState {
  const natural =
    form.scheduleKind === 'cron' && form.cron.trim()
      ? cronToNatural(form.cron)
      : isEdit
        ? { ...DEFAULT_NATURAL, cadence: 'custom' as Cadence }
        : DEFAULT_NATURAL
  return {
    natural,
    interval: secondsToInterval(Number(form.every)),
    atLocal: form.at ? isoToLocalInput(form.at) : '',
  }
}

// ── Run history ─────────────────────────────────────────────────────────────

/** The last N outcomes, oldest first, for a status strip under the header. */
export function runOutcomes(runs: readonly RawRun[], count: number): Array<'ok' | 'error'> {
  return runs
    .slice(0, count)
    .reverse()
    .map((r) => (r.status === 'ok' || r.success === true ? 'ok' : 'error'))
}

/** "1.2s", "840ms", "3m 05s": durations as a human reads them, not raw ms. */
export function formatDurationMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${pad2(Math.round(s % 60))}s`
}
