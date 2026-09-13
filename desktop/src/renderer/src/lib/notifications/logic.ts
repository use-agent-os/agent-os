import type { NotifyKind, NotifyTarget } from '@shared/notify'
import type { NotificationSettings } from '@shared/settings'

/**
 * Every decision about notifications, as pure functions: whether an event
 * is wanted, where it goes (system, banner, sound, Dock), how a session's
 * run transitions become events, and the Do-not-disturb arithmetic. The
 * dispatcher and hooks around this file only carry state.
 */

// ── Events ──────────────────────────────────────────────────────────────────

export interface NotifyEvent {
  kind: NotifyKind
  title: string
  subtitle?: string
  body?: string
  target: NotifyTarget
  /** Replies: how long the turn ran. */
  durationMs?: number
}

export interface DeliveryContext {
  now: number
  /** The window is visible and has focus. */
  focused: boolean
  /** The session on screen, when the chat route is showing one. */
  currentSessionKey: string | null
}

export interface Delivery {
  /** Post a native notification. */
  system: boolean
  /** Show an in-app banner (toast) instead. */
  banner: boolean
  sound: boolean
  bounce: boolean
  /** Keep it in the bell's list. */
  record: boolean
  /** Recorded as already seen: the user was looking at it when it happened. */
  seen: boolean
}

const NONE: Delivery = {
  system: false,
  banner: false,
  sound: false,
  bounce: false,
  record: false,
  seen: true,
}

export function isMuted(s: Pick<NotificationSettings, 'muteUntil'>, now: number): boolean {
  return s.muteUntil !== null && s.muteUntil > now
}

/** The per-event switch, with the reply-length threshold folded in. */
export function eventWanted(s: NotificationSettings, ev: NotifyEvent): boolean {
  switch (ev.kind) {
    case 'reply':
      return s.replyDone && (ev.durationMs ?? 0) >= s.replyMinSeconds * 1000
    case 'replyFailed':
      return s.replyFailed
    case 'approval':
      return s.approvals
    case 'job':
      return s.jobs === 'all'
    case 'jobFailed':
      return s.jobs !== 'off'
    case 'gateway':
      return s.gateway
    case 'test':
      return true
  }
}

/**
 * Where an event goes. A test always shows. Muted events are recorded and
 * nothing else. An event about the session on screen, while the window is
 * in front, only chimes: the user is watching it happen.
 */
export function decideDelivery(
  s: NotificationSettings,
  ev: NotifyEvent,
  ctx: DeliveryContext,
): Delivery {
  const test = ev.kind === 'test'
  if (!test && !s.enabled) return NONE
  if (!test && !eventWanted(s, ev)) return NONE
  if (!test && isMuted(s, ctx.now)) return { ...NONE, record: true, seen: false }

  const watching =
    ctx.focused && ev.target.type === 'session' && ev.target.key === ctx.currentSessionKey
  if (watching) return { ...NONE, sound: s.sound, record: true, seen: true }

  if (ctx.focused && !test) {
    return {
      system: s.whenActive === 'system',
      banner: s.whenActive === 'banner',
      sound: s.sound && s.whenActive !== 'skip',
      bounce: false,
      record: true,
      seen: false,
    }
  }
  return {
    system: true,
    banner: false,
    sound: s.sound,
    bounce: s.bounce,
    record: true,
    seen: false,
  }
}

// ── Session runs ────────────────────────────────────────────────────────────

/** What the watcher needs from a sidebar row. */
export interface RunRow {
  key: string
  title: string
  live: boolean
  /** The gateway's run status word: running, queued, succeeded, failed, timeout, cancelled… */
  status: string
}

export interface RunTrack {
  startedAt: number
  title: string
}

export interface RunFinished {
  key: string
  title: string
  status: string
  durationMs: number
}

/** Terminal states the user caused themselves: not worth a notification. */
const QUIET_TERMINALS = new Set(['cancelled', 'interrupted', 'killed', 'abandoned'])

/**
 * Diff one snapshot of the session list against the runs being tracked. A
 * row that turns live starts a track; one that stops being live ends it and
 * yields a finished run. Rows that vanish while live are dropped silently
 * (deleted session, or the list shrank). The first snapshot only seeds.
 */
export function diffSessionRuns(
  tracked: ReadonlyMap<string, RunTrack>,
  rows: readonly RunRow[],
  now: number,
): { next: Map<string, RunTrack>; finished: RunFinished[] } {
  const next = new Map<string, RunTrack>()
  const finished: RunFinished[] = []
  for (const row of rows) {
    const track = tracked.get(row.key)
    if (row.live) {
      next.set(row.key, track ?? { startedAt: now, title: row.title })
    } else if (track && !QUIET_TERMINALS.has(row.status)) {
      finished.push({
        key: row.key,
        title: row.title || track.title,
        status: row.status,
        durationMs: Math.max(0, now - track.startedAt),
      })
    }
  }
  return { next, finished }
}

export function runKind(status: string): 'reply' | 'replyFailed' {
  return status === 'failed' || status === 'timeout' ? 'replyFailed' : 'reply'
}

// ── Do not disturb ──────────────────────────────────────────────────────────

export type MuteOption = 'off' | '30m' | '1h' | '3h' | 'tomorrow'
export const MUTE_OPTIONS: readonly MuteOption[] = ['off', '30m', '1h', '3h', 'tomorrow']

/** Epoch ms for an option; `tomorrow` is 9:00 local on the next day. */
export function muteUntilFor(option: MuteOption, now: number): number | null {
  switch (option) {
    case 'off':
      return null
    case '30m':
      return now + 30 * 60_000
    case '1h':
      return now + 60 * 60_000
    case '3h':
      return now + 3 * 60 * 60_000
    case 'tomorrow': {
      const d = new Date(now)
      d.setDate(d.getDate() + 1)
      d.setHours(9, 0, 0, 0)
      return d.getTime()
    }
  }
}

// ── Formatting ──────────────────────────────────────────────────────────────

/** "42s", "1m 12s", "1h 03m": how long a reply took, for the notification body. */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

/** The session key in a hash route like `#/sessions/agent%3Amain%3A…`, or null. */
export function sessionKeyFromHash(hash: string): string | null {
  const m = /^#\/sessions\/([^/?]+)/.exec(hash)
  if (!m?.[1]) return null
  try {
    return decodeURIComponent(m[1])
  } catch {
    return m[1]
  }
}

/** One line of a job summary, trimmed for a notification body. */
export function excerpt(text: string | null | undefined, max = 140): string {
  const line = String(text ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  if (line.length <= max) return line
  return `${line.slice(0, max - 1).trimEnd()}…`
}
