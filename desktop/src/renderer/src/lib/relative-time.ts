const MIN = 60_000
const HOUR = 60 * MIN
const DAY = 24 * HOUR

/** Compact age like the sidebar shows: "now", "5m", "3h", "4d", "2w"; "mo" past 8 weeks. */
export function shortAge(at: number, now = Date.now()): string {
  const delta = Math.max(0, now - at)
  if (delta < MIN) return 'now'
  if (delta < HOUR) return `${Math.floor(delta / MIN)}m`
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h`
  if (delta < 7 * DAY) return `${Math.floor(delta / DAY)}d`
  if (delta < 56 * DAY) return `${Math.floor(delta / (7 * DAY))}w`
  return `${Math.floor(delta / (30 * DAY))}mo`
}

export type DateGroup =
  { kind: 'today' } | { kind: 'yesterday' } | { kind: 'week' } | { kind: 'month'; label: string }

/** Which sidebar section a timestamp belongs to. Month groups carry their own label. */
export function dateGroup(at: number, now = Date.now()): DateGroup {
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  const todayStart = start.getTime()
  if (at >= todayStart) return { kind: 'today' }
  if (at >= todayStart - DAY) return { kind: 'yesterday' }
  if (at >= todayStart - 6 * DAY) return { kind: 'week' }
  const d = new Date(at)
  const sameYear = d.getFullYear() === start.getFullYear()
  const label = d.toLocaleDateString(
    'en-US',
    sameYear ? { month: 'long' } : { month: 'long', year: 'numeric' },
  )
  return { kind: 'month', label }
}

export function groupKey(g: DateGroup): string {
  return g.kind === 'month' ? `month:${g.label}` : g.kind
}
