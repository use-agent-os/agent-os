import type { CSSProperties } from 'react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webPagesWork'

// A schedule drawn as lanes: one row per automation, a tick for every time it
// fires inside the window, hour (or day) gridlines and a "Now" edge. Used for
// "Next 24 hours" on a populated page and, ghosted, for the week the templates
// would fill on the empty page. Times come from the jobs' own schedules.

export type LaneTone = 'accent' | 'info' | 'ok' | 'warn' | 'muted'

export interface Lane {
  id: string
  name: string
  tone: LaneTone
  times: number[]
}

const HOUR = 3_600_000
const DAY = 24 * HOUR

/** Local midnights strictly inside (from, to). */
function midnights(from: number, to: number): number[] {
  const out: number[] = []
  const d = new Date(from)
  d.setHours(24, 0, 0, 0)
  while (d.getTime() < to) {
    out.push(d.getTime())
    d.setDate(d.getDate() + 1)
  }
  return out
}

/** Local hours divisible by `step` strictly inside (from, to). */
function hourMarks(from: number, to: number, step: number): number[] {
  const out: number[] = []
  const d = new Date(from)
  d.setMinutes(0, 0, 0)
  d.setHours(d.getHours() + 1)
  while (d.getTime() < to) {
    if (d.getHours() % step === 0) out.push(d.getTime())
    d.setHours(d.getHours() + 1)
  }
  return out
}

/** The day spans of a week window long enough to carry a label (from a "Now" edge on). */
function daySegments(from: number, to: number, cuts: number[]): [number, number][] {
  const edges = [from, ...cuts, to]
  const out: [number, number][] = []
  for (let i = 0; i < edges.length - 1; i++) {
    const [a, b] = [edges[i]!, edges[i + 1]!]
    if (b - a >= DAY * 0.6) out.push([a, b])
  }
  return out
}

function pct(ts: number, from: number, to: number): string {
  return `${(((ts - from) / (to - from)) * 100).toFixed(3)}%`
}

const clock = (ts: number) =>
  new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const weekday = (ts: number) => new Date(ts).toLocaleDateString([], { weekday: 'short' })

export function Timeline({
  lanes,
  from,
  span,
  animate,
  ghost = false,
  label,
}: {
  lanes: Lane[]
  from: number
  span: 'day' | 'week'
  /** Draw the ticks in (first arrival only). */
  animate: boolean
  /** A decorative preview: no names for assistive tech, no hover detail. */
  ghost?: boolean
  label?: string
}) {
  const to = from + (span === 'day' ? DAY : 7 * DAY)
  const lines = span === 'day' ? hourMarks(from, to, 6) : midnights(from, to)
  const days = span === 'day' ? midnights(from, to) : []
  return (
    <div
      className="am-timeline"
      data-span={span}
      data-animate={animate || undefined}
      data-ghost={ghost || undefined}
      aria-hidden={ghost || undefined}
    >
      <ul role="list" aria-label={label} className="am-lanes">
        {lanes.map((lane, i) => (
          <li
            key={lane.id}
            className="am-lane"
            style={{ '--lane-i': Math.min(i, 8) } as CSSProperties}
            aria-label={
              ghost
                ? undefined
                : `${lane.name}: ${
                    lane.times.length
                      ? tPlural('webPagesWork.laneRuns', lane.times.length, {
                          first: clock(lane.times[0]!),
                        })
                      : t('webPagesWork.laneNone')
                  }`
            }
          >
            <span className="am-lane-name" title={lane.name}>
              <span aria-hidden className="am-lane-dot" data-tone={lane.tone} />
              <span className="truncate">{lane.name}</span>
            </span>
            <span aria-hidden className="am-track" data-tone={lane.tone}>
              {lines.map((ts) => (
                <span key={`l${ts}`} className="am-gridline" style={{ left: pct(ts, from, to) }} />
              ))}
              {days.map((ts) => (
                <span
                  key={`d${ts}`}
                  className="am-gridline"
                  data-day
                  style={{ left: pct(ts, from, to) }}
                />
              ))}
              {lane.times.map((ts) => (
                <span key={ts} className="am-tick" style={{ left: pct(ts, from, to) }} />
              ))}
            </span>
          </li>
        ))}
      </ul>
      <div aria-hidden className="am-axis">
        <span className="am-axis-spacer" />
        <span className="am-axis-scale">
          <span className="am-axis-label" data-now style={{ left: '0%' }}>
            {t('webPagesWork.timelineNow')}
          </span>
          {span === 'day'
            ? lines
                .filter((ts) => (ts - from) / (to - from) > 0.1)
                .map((ts) => (
                  <span
                    key={ts}
                    className="am-axis-label"
                    data-day={days.includes(ts) || undefined}
                    style={{ left: pct(ts, from, to) }}
                  >
                    {days.includes(ts) ? weekday(ts) : clock(ts)}
                  </span>
                ))
            : daySegments(from, to, lines).map(([start, end]) => (
                <span
                  key={start}
                  className="am-axis-label"
                  data-mid
                  style={{ left: pct((start + end) / 2, from, to) }}
                >
                  {weekday(start)}
                </span>
              ))}
        </span>
      </div>
    </div>
  )
}
