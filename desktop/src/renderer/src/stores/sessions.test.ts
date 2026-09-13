import { describe, expect, it } from 'vitest'
import { toSessionRow } from './sessions'

describe('toSessionRow', () => {
  it('prefers the display name, then derived title, then "New session"', () => {
    expect(toSessionRow({ key: 'agent:main:webchat:abc', display_name: 'Ops' }).title).toBe('Ops')
    expect(toSessionRow({ key: 'agent:main:webchat:abc', derived_title: 'Fix CI' }).title).toBe(
      'Fix CI',
    )
    // A bare session id is not a name anyone chose.
    expect(toSessionRow({ key: 'agent:main:webchat:abc' }).title).toBe('New session')
  })

  it('treats the gateway placeholder names as unnamed', () => {
    // The gateway seeds web sessions as "WebChat" until the titler renames
    // them; showing that would make every fresh chat look renamed.
    expect(toSessionRow({ key: 'agent:main:webchat:abc', display_name: 'WebChat' }).title).toBe(
      'New session',
    )
    expect(toSessionRow({ key: 'k', display_name: 'WebChat', derived_title: 'Fix CI' }).title).toBe(
      'Fix CI',
    )
  })

  it('normalizes updated_at from seconds, milliseconds and ISO strings', () => {
    expect(toSessionRow({ key: 'k', updated_at: 1_700_000_000 }).updatedAt).toBe(1_700_000_000_000)
    expect(toSessionRow({ key: 'k', updated_at: 1_700_000_000_000 }).updatedAt).toBe(
      1_700_000_000_000,
    )
    expect(toSessionRow({ key: 'k', updated_at: '2026-09-09T00:00:00Z' }).updatedAt).toBe(
      Date.parse('2026-09-09T00:00:00Z'),
    )
    expect(toSessionRow({ key: 'k' }).updatedAt).toBe(0)
  })

  it('marks a session live only while a task is queued or running', () => {
    expect(toSessionRow({ key: 'k', active_task: { status: 'running' } }).live).toBe(true)
    expect(toSessionRow({ key: 'k', active_task: { status: 'queued' } }).live).toBe(true)
    expect(toSessionRow({ key: 'k', last_task: { status: 'failed' } }).live).toBe(false)
    expect(toSessionRow({ key: 'k' }).live).toBe(false)
  })
})
