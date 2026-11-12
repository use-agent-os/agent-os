import { useEffect, useState } from 'react'
import { nextRuns, parseCron, type RawJob } from '@/views/cron/logic'

// Time helpers of the Automations page: a ticking clock, the one-time arrival
// flag for entrance animations, and the fire times of a job inside a window
// (the "Next 24 hours" lanes and the template previews).

/** The current time, refreshed every `ms` while `active`. */
export function useNow(active = true, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => setNow(Date.now()), ms)
    return () => window.clearInterval(id)
  }, [active, ms])
  return now
}

/**
 * True until shortly after `ready` first turns true: put a one-time entrance
 * class on a list while it is, so rows rise in when the data first arrives and
 * never again on a refetch, a sort or a ticking value.
 */
export function useArrival(ready: boolean, ms = 700): boolean {
  const [settled, setSettled] = useState(false)
  useEffect(() => {
    if (!ready || settled) return
    const id = window.setTimeout(() => setSettled(true), ms)
    return () => window.clearTimeout(id)
  }, [ready, settled, ms])
  return !settled
}

/** A job's next run as epoch ms, or null when it has none (paused, running, unset). */
export function nextRunAt(job: RawJob): number | null {
  if (!job.enabled || job.status === 'running' || !job.next_run) return null
  const ts = new Date(job.next_run as string | number).getTime()
  return Number.isNaN(ts) ? null : ts
}

/** No more marks than this per lane: a dense job reads as a dense lane already. */
const MAX_MARKS = 96

/**
 * The fire times of a cron expression in [from, to), in the zone the scheduler
 * evaluates it in ('' = UTC). Empty when it does not parse.
 */
export function cronTimesBetween(
  expr: string,
  tz: string,
  from: number,
  to: number,
  max = MAX_MARKS,
): number[] {
  const parsed = parseCron(expr.trim())
  if (!parsed) return []
  const out: number[] = []
  let cursor = from - 60_000
  while (out.length < max) {
    const next = nextRuns(parsed, 1, cursor, tz)[0]
    if (!next || next.getTime() >= to) break
    if (next.getTime() >= from) out.push(next.getTime())
    cursor = next.getTime()
  }
  return out
}

/**
 * When an enabled job fires in [from, to). A cron job is expanded from its
 * expression; an interval job steps from its next run; anything else (a
 * one-shot, an unknown kind) is its next run alone.
 */
export function jobTimesBetween(job: RawJob, from: number, to: number): number[] {
  if (!job.enabled) return []
  const kind = String(job.scheduleKind || job.schedule_kind || '')
  const expr = String(job.expression || '')
  if ((kind === 'cron' || !kind) && parseCron(expr.trim()))
    return cronTimesBetween(expr, String(job.tz || ''), from, to)
  const first = nextRunAt(job)
  if (first == null || first >= to) return []
  if (kind === 'every') {
    const seconds = Number(job.scheduleRaw ?? job.schedule_raw)
    if (Number.isFinite(seconds) && seconds >= 60) {
      const out: number[] = []
      for (let t = first; t < to && out.length < MAX_MARKS; t += seconds * 1000)
        if (t >= from) out.push(t)
      return out
    }
  }
  return first >= from ? [first] : []
}
