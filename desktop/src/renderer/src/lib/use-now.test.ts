import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useNow } from './use-now'

const T0 = new Date('2026-11-15T08:00:00Z').getTime()

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(T0)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useNow', () => {
  it('ticks at the given interval', () => {
    const { result } = renderHook(() => useNow(1000))
    expect(result.current).toBe(T0)
    act(() => vi.advanceTimersByTime(1000))
    expect(result.current).toBe(T0 + 1000)
  })

  it('stays frozen while the interval is 0', () => {
    const { result } = renderHook(() => useNow(0))
    act(() => vi.advanceTimersByTime(60_000))
    expect(result.current).toBe(T0)
  })

  it('catches up at once when a frozen clock starts ticking', () => {
    // The notification bell mounts with the app and freezes its clock while
    // the popover is closed; opening it hours later must not show every item
    // as "now" until the first tick lands.
    const { result, rerender } = renderHook(({ ms }) => useNow(ms), {
      initialProps: { ms: 0 },
    })
    act(() => vi.advanceTimersByTime(3 * 60 * 60_000))
    rerender({ ms: 15_000 })
    act(() => vi.advanceTimersByTime(0))
    expect(result.current).toBe(T0 + 3 * 60 * 60_000)
  })
})
