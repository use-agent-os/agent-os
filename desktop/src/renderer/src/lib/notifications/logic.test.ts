import { describe, expect, it } from 'vitest'
import { DEFAULT_NOTIFICATION_SETTINGS, type NotificationSettings } from '@shared/settings'
import {
  decideDelivery,
  diffSessionRuns,
  eventWanted,
  excerpt,
  formatDuration,
  muteUntilFor,
  runKind,
  sessionKeyFromHash,
  type DeliveryContext,
  type NotifyEvent,
  type RunTrack,
} from './logic'

const NOW = 1_800_000_000_000
const reply: NotifyEvent = {
  kind: 'reply',
  title: 'Reply ready',
  target: { type: 'session', key: 'agent:main:webchat:a' },
  durationMs: 12_000,
}
const background: DeliveryContext = { now: NOW, focused: false, currentSessionKey: null }
const front: DeliveryContext = { now: NOW, focused: true, currentSessionKey: 'other' }

function settings(patch: Partial<NotificationSettings> = {}): NotificationSettings {
  return { ...DEFAULT_NOTIFICATION_SETTINGS, ...patch }
}

describe('eventWanted', () => {
  it('follows the per-event switches', () => {
    expect(eventWanted(settings({ replyDone: false }), reply)).toBe(false)
    expect(eventWanted(settings(), { ...reply, kind: 'replyFailed' })).toBe(true)
    expect(eventWanted(settings({ replyFailed: false }), { ...reply, kind: 'replyFailed' })).toBe(
      false,
    )
    expect(eventWanted(settings({ approvals: false }), { ...reply, kind: 'approval' })).toBe(false)
    expect(eventWanted(settings({ gateway: false }), { ...reply, kind: 'gateway' })).toBe(false)
  })

  it('applies the reply-length threshold', () => {
    const s = settings({ replyMinSeconds: 30 })
    expect(eventWanted(s, { ...reply, durationMs: 12_000 })).toBe(false)
    expect(eventWanted(s, { ...reply, durationMs: 30_000 })).toBe(true)
    expect(eventWanted(s, { ...reply, durationMs: undefined })).toBe(false)
  })

  it('jobs: off / failures / all', () => {
    const job: NotifyEvent = { ...reply, kind: 'job' }
    const failed: NotifyEvent = { ...reply, kind: 'jobFailed' }
    expect(eventWanted(settings({ jobs: 'off' }), job)).toBe(false)
    expect(eventWanted(settings({ jobs: 'off' }), failed)).toBe(false)
    expect(eventWanted(settings({ jobs: 'failures' }), job)).toBe(false)
    expect(eventWanted(settings({ jobs: 'failures' }), failed)).toBe(true)
    expect(eventWanted(settings({ jobs: 'all' }), job)).toBe(true)
  })
})

describe('decideDelivery', () => {
  it('posts a system notification in the background, with sound and bounce', () => {
    expect(decideDelivery(settings(), reply, background)).toEqual({
      system: true,
      banner: false,
      sound: true,
      bounce: true,
      record: true,
      seen: false,
    })
  })

  it('does nothing when the master switch is off', () => {
    expect(decideDelivery(settings({ enabled: false }), reply, background).record).toBe(false)
  })

  it('only records while muted', () => {
    const d = decideDelivery(settings({ muteUntil: NOW + 1 }), reply, background)
    expect(d).toMatchObject({ system: false, banner: false, sound: false, record: true })
    // An expired mute no longer applies.
    expect(decideDelivery(settings({ muteUntil: NOW - 1 }), reply, background).system).toBe(true)
  })

  it('in front: banner, system or skip per whenActive', () => {
    expect(decideDelivery(settings({ whenActive: 'banner' }), reply, front)).toMatchObject({
      system: false,
      banner: true,
      sound: true,
    })
    expect(decideDelivery(settings({ whenActive: 'system' }), reply, front)).toMatchObject({
      system: true,
      banner: false,
    })
    expect(decideDelivery(settings({ whenActive: 'skip' }), reply, front)).toMatchObject({
      system: false,
      banner: false,
      sound: false,
      record: true,
    })
  })

  it('never bounces the Dock while the window is in front', () => {
    expect(decideDelivery(settings({ whenActive: 'system' }), reply, front).bounce).toBe(false)
  })

  it('only chimes for the session on screen', () => {
    const watching: DeliveryContext = { ...front, currentSessionKey: 'agent:main:webchat:a' }
    expect(decideDelivery(settings(), reply, watching)).toEqual({
      system: false,
      banner: false,
      sound: true,
      bounce: false,
      record: true,
      seen: true,
    })
    expect(decideDelivery(settings({ sound: false }), reply, watching).sound).toBe(false)
  })

  it('a test notification ignores the switches and the mute', () => {
    const test: NotifyEvent = { kind: 'test', title: 'AgentOS', target: { type: 'none' } }
    const s = settings({ enabled: false, muteUntil: NOW + 1, whenActive: 'skip' })
    expect(decideDelivery(s, test, front).system).toBe(true)
  })
})

describe('diffSessionRuns', () => {
  const rows = (live: boolean, status = live ? 'running' : 'succeeded') => [
    { key: 'a', title: 'Alpha', live, status },
  ]

  it('seeds on the first snapshot without reporting anything', () => {
    const { next, finished } = diffSessionRuns(new Map(), rows(true), NOW)
    expect(finished).toEqual([])
    expect(next.get('a')).toEqual({ startedAt: NOW, title: 'Alpha' })
  })

  it('reports a run when a live row settles, with its duration', () => {
    const tracked = new Map<string, RunTrack>([['a', { startedAt: NOW - 5_000, title: 'Alpha' }]])
    const { next, finished } = diffSessionRuns(tracked, rows(false), NOW)
    expect(finished).toEqual([{ key: 'a', title: 'Alpha', status: 'succeeded', durationMs: 5_000 }])
    expect(next.size).toBe(0)
  })

  it('keeps the original start across snapshots', () => {
    const tracked = new Map<string, RunTrack>([['a', { startedAt: NOW - 5_000, title: 'Alpha' }]])
    const { next } = diffSessionRuns(tracked, rows(true), NOW)
    expect(next.get('a')?.startedAt).toBe(NOW - 5_000)
  })

  it('stays quiet for runs the user stopped', () => {
    const tracked = new Map<string, RunTrack>([['a', { startedAt: NOW, title: 'Alpha' }]])
    expect(diffSessionRuns(tracked, rows(false, 'cancelled'), NOW).finished).toEqual([])
  })

  it('drops rows that vanished while live', () => {
    const tracked = new Map<string, RunTrack>([['gone', { startedAt: NOW, title: 'x' }]])
    const { next, finished } = diffSessionRuns(tracked, [], NOW)
    expect(finished).toEqual([])
    expect(next.size).toBe(0)
  })

  it('maps terminal status to an event kind', () => {
    expect(runKind('succeeded')).toBe('reply')
    expect(runKind('failed')).toBe('replyFailed')
    expect(runKind('timeout')).toBe('replyFailed')
  })
})

describe('muteUntilFor', () => {
  it('offsets from now, and tomorrow means 9:00 next day', () => {
    expect(muteUntilFor('off', NOW)).toBeNull()
    expect(muteUntilFor('30m', NOW)).toBe(NOW + 30 * 60_000)
    expect(muteUntilFor('1h', NOW)).toBe(NOW + 60 * 60_000)
    expect(muteUntilFor('3h', NOW)).toBe(NOW + 3 * 60 * 60_000)
    const tomorrow = new Date(muteUntilFor('tomorrow', NOW)!)
    const today = new Date(NOW)
    expect(tomorrow.getHours()).toBe(9)
    expect(tomorrow.getMinutes()).toBe(0)
    expect(tomorrow.getTime()).toBeGreaterThan(today.getTime())
    expect(tomorrow.getTime() - today.getTime()).toBeLessThanOrEqual(33 * 3_600_000)
  })
})

describe('formatting', () => {
  it('formatDuration', () => {
    expect(formatDuration(900)).toBe('1s')
    expect(formatDuration(42_000)).toBe('42s')
    expect(formatDuration(72_000)).toBe('1m 12s')
    expect(formatDuration(3_780_000)).toBe('1h 03m')
  })

  it('sessionKeyFromHash', () => {
    expect(sessionKeyFromHash('#/sessions/agent%3Amain%3Awebchat%3Aabc')).toBe(
      'agent:main:webchat:abc',
    )
    expect(sessionKeyFromHash('#/sessions')).toBeNull()
    expect(sessionKeyFromHash('#/projects/p1')).toBeNull()
  })

  it('excerpt collapses whitespace and trims with an ellipsis', () => {
    expect(excerpt('  a\n\n b  ')).toBe('a b')
    expect(excerpt('x'.repeat(200), 20)).toBe(`${'x'.repeat(19)}…`)
    expect(excerpt(null)).toBe('')
  })
})
