import { beforeEach, describe, expect, it } from 'vitest'
import { useSessionMarks } from './session-marks'

const KEY = 'agentos-desktop.sessionMarks'

describe('useSessionMarks', () => {
  beforeEach(() => {
    localStorage.clear()
    useSessionMarks.setState({ pinned: new Set(), archived: new Set(), unread: new Set() })
  })

  it('toggles each mark and persists the sets', () => {
    const s = useSessionMarks.getState()
    s.setPinned('a', true)
    s.setArchived('b', true)
    s.setUnread('c', true)
    expect(useSessionMarks.getState().pinned.has('a')).toBe(true)
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual({
      pinned: ['a'],
      archived: ['b'],
      unread: ['c'],
    })
    useSessionMarks.getState().setPinned('a', false)
    expect(useSessionMarks.getState().pinned.size).toBe(0)
  })

  it('keeps the same set when nothing changes, so subscribers stay quiet', () => {
    const before = useSessionMarks.getState().unread
    useSessionMarks.getState().setUnread('x', false)
    expect(useSessionMarks.getState().unread).toBe(before)
  })

  it('clears every unread at once and forgets a deleted session everywhere', () => {
    const s = useSessionMarks.getState()
    s.setUnread('a', true)
    s.setUnread('b', true)
    s.setPinned('b', true)
    useSessionMarks.getState().markAllRead()
    expect(useSessionMarks.getState().unread.size).toBe(0)
    useSessionMarks.getState().forget('b')
    expect(useSessionMarks.getState().pinned.has('b')).toBe(false)
  })
})
