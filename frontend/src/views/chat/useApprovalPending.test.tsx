import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useApprovals, type Approval } from '@/services/approval-monitor'
import { computeHasPendingForSession, useApprovalPending } from './useApprovalPending'

const approval = (sessionKey: string, snake = false): Approval =>
  (snake ? { id: 'a', session_key: sessionKey } : { id: 'a', sessionKey }) as Approval

afterEach(() => {
  // Reset the shared store between tests.
  useApprovals.setState({ pending: [], count: 0 })
})

describe('computeHasPendingForSession (chat.js:4686-4689)', () => {
  it('is true when a pending item matches the session (camel alias)', () => {
    expect(computeHasPendingForSession([approval('sess-1')], 'sess-1')).toBe(true)
  })
  it('is true when a pending item matches the session (snake alias)', () => {
    expect(computeHasPendingForSession([approval('sess-1', true)], 'sess-1')).toBe(true)
  })
  it('is false when no pending item matches the session', () => {
    expect(computeHasPendingForSession([approval('other')], 'sess-1')).toBe(false)
  })
  it('is false for an empty pending list', () => {
    expect(computeHasPendingForSession([], 'sess-1')).toBe(false)
  })
})

describe('useApprovalPending — idle-pause gate (chat.js:6216-6233)', () => {
  it('pauses (true) when a matching approval is pending for the session', () => {
    const setPaused = vi.fn()
    act(() => {
      useApprovals.setState({ pending: [approval('sess-1')], count: 1 })
    })
    renderHook(() => useApprovalPending('sess-1', setPaused))
    expect(setPaused).toHaveBeenLastCalledWith(true)
  })

  it('does not pause when the pending approval is for another session', () => {
    const setPaused = vi.fn()
    act(() => {
      useApprovals.setState({ pending: [approval('other')], count: 1 })
    })
    renderHook(() => useApprovalPending('sess-1', setPaused))
    expect(setPaused).toHaveBeenLastCalledWith(false)
  })

  it('resumes (false) when the pending set clears — resolving advances the stream', () => {
    const setPaused = vi.fn()
    act(() => {
      useApprovals.setState({ pending: [approval('sess-1')], count: 1 })
    })
    renderHook(() => useApprovalPending('sess-1', setPaused))
    expect(setPaused).toHaveBeenLastCalledWith(true)

    // The approvals view resolves → monitor re-polls → store updates with the
    // item gone. The hook re-fires with false (unpause), restoring 'running'.
    act(() => {
      useApprovals.setState({ pending: [], count: 0 })
    })
    expect(setPaused).toHaveBeenLastCalledWith(false)
  })

  it('re-evaluates when the active session changes', () => {
    const setPaused = vi.fn()
    act(() => {
      useApprovals.setState({ pending: [approval('sess-2')], count: 1 })
    })
    const { rerender } = renderHook(({ key }) => useApprovalPending(key, setPaused), {
      initialProps: { key: 'sess-1' },
    })
    expect(setPaused).toHaveBeenLastCalledWith(false)
    // Switch to the session that HAS a pending approval → pause.
    rerender({ key: 'sess-2' })
    expect(setPaused).toHaveBeenLastCalledWith(true)
  })
})
