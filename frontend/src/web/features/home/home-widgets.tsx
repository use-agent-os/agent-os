import { useEffect, useState, type CSSProperties, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { formatDuration, parseCron, nextRuns, type RawJob } from '@/views/cron/logic'
import { sessionTimestamp, type UsageRow } from '@/views/usage/logic'
import type { OverviewTone } from './overview-ui'
import '@/web/styles/page-home.css'

// Home's visual pieces: a progress ring, a column chart of recent sessions'
// tokens and the countdown to the next automation. All decorative: the
// figure each one draws is always in text beside it.

const clamp01 = (n: number) => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0)

/**
 * A ring that draws in once on mount and glides when its value changes
 * (a ticking countdown moves it every second without replaying the draw).
 * `value` is 0..1; `dashed` draws an empty dashed track (nothing to show).
 */
export function Ring({
  value,
  tone = 'accent',
  restTone,
  size = 'md',
  dashed = false,
  className,
  children,
}: {
  value: number
  tone?: OverviewTone
  /** Colours the unfilled part of the ring (e.g. failing checks). */
  restTone?: OverviewTone
  size?: 'sm' | 'md' | 'lg'
  dashed?: boolean
  className?: string
  children?: ReactNode
}) {
  const v = clamp01(value)
  return (
    <span
      aria-hidden
      data-ov-tone={tone}
      data-size={size}
      data-rest={restTone}
      data-empty={v === 0 || dashed || undefined}
      className={cn('hm-ring', className)}
    >
      <svg viewBox="0 0 100 100">
        <circle
          className="hm-ring-track"
          data-dashed={dashed || undefined}
          cx="50"
          cy="50"
          r="44"
        />
        {dashed ? null : (
          <circle
            className="hm-ring-fill"
            cx="50"
            cy="50"
            r="44"
            pathLength={100}
            style={{ '--ring-value': v * 100 } as CSSProperties}
          />
        )}
      </svg>
      {children ? <span className="hm-ring-center">{children}</span> : null}
    </span>
  )
}

/** Columns the chart has room for. */
const SLOTS = 14

export interface UsageColumn {
  key: string
  input: number
  output: number
}

/**
 * The newest sessions with usage, oldest first, so the chart reads left to
 * right in time. Every column is one session's lifetime tokens (the gateway
 * keeps no per-day record), ordered by when the session was last active.
 */
export function usageColumns(rows: readonly UsageRow[], max = SLOTS): UsageColumn[] {
  const num = (v: unknown) => {
    const n = Number(v)
    return Number.isFinite(n) && n > 0 ? n : 0
  }
  return rows
    .map((row, i) => ({
      key: String(row.session || row.sessionKey || row.key || i),
      input: num(row.inputTokens ?? row.input_tokens),
      output: num(row.outputTokens ?? row.output_tokens),
      at: sessionTimestamp(row) ?? 0,
    }))
    .filter((c) => c.input + c.output > 0)
    .sort((a, b) => b.at - a.at)
    .slice(0, max)
    .reverse()
    .map(({ key, input, output }) => ({ key, input, output }))
}

/** Heights of the ghost chart shown before there is any usage. */
const GHOST = [34, 52, 40, 66, 48, 80, 58, 44, 72, 38, 62, 90, 54, 70]

/**
 * Columns of input (lime) and output (blue) tokens, one per session. They
 * grow from the baseline on mount, a fifth of a stagger step apart. `ghost` draws a faint
 * placeholder of the same shape.
 */
export function UsageBars({
  columns,
  ghost = false,
  label,
}: {
  columns: readonly UsageColumn[]
  ghost?: boolean
  label: (column: UsageColumn) => string
}) {
  const peak = Math.max(1, ...columns.map((c) => c.input + c.output))
  // Empty slots on the left keep the chart's width steady with few sessions.
  const pad = ghost ? 0 : Math.max(0, SLOTS - columns.length)
  return (
    <div aria-hidden className="hm-bars" data-ghost={ghost || undefined}>
      {ghost
        ? GHOST.map((h, i) => (
            <span
              key={i}
              className="hm-bar"
              style={{ '--h': h, '--i': Math.min(i, 13) } as CSSProperties}
            />
          ))
        : [
            ...Array.from({ length: pad }, (_, i) => (
              <span key={`pad-${i}`} className="hm-bar" data-slot-empty />
            )),
            ...columns.map((c, i) => {
              const total = c.input + c.output
              return (
                <span
                  key={c.key}
                  title={label(c)}
                  className="hm-bar"
                  style={
                    {
                      '--h': Math.max(4, (total / peak) * 100),
                      '--out': total > 0 ? (c.output / total) * 100 : 0,
                      '--i': Math.min(i + pad, 13),
                    } as CSSProperties
                  }
                />
              )
            }),
          ]}
    </div>
  )
}

function toMs(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const ms = new Date(value).getTime()
  return Number.isNaN(ms) ? null : ms
}

const DAY_MS = 86_400_000

/**
 * The span of a job's current wait: from its last run (or, for a cron
 * expression, the scheduled run before the next) to its next run. Falls back
 * to the 24 hours before the next run.
 */
export function runCycle(job: RawJob): { start: number; end: number } | null {
  const end = toMs(job.next_run)
  if (end === null) return null
  const last = toMs(job.last_run)
  if (last !== null && last < end) return { start: last, end }
  const expression = String(job.expression || '')
  if (expression) {
    const runs = nextRuns(parseCron(expression), 2, end - 1, job.tz)
    const [first, second] = runs
    if (first && second) {
      const period = second.getTime() - first.getTime()
      if (period > 0) return { start: end - period, end }
    }
  }
  return { start: end - DAY_MS, end }
}

/**
 * The countdown to the next run, ticking once a second inside its own
 * component: the tick re-renders the ring and its text only, so no entrance
 * or count-up above it runs again.
 */
export function Countdown({ job, awaiting }: { job: RawJob; awaiting: string }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])
  const cycle = runCycle(job)
  const remaining = cycle ? cycle.end - now : 0
  const progress = cycle ? (now - cycle.start) / (cycle.end - cycle.start) : 0
  return (
    <Ring value={remaining > 0 ? progress : 1} tone="accent" size="lg">
      <span className="hm-countdown tabular-nums">
        {remaining > 0 ? formatDuration(remaining) : awaiting}
      </span>
    </Ring>
  )
}
