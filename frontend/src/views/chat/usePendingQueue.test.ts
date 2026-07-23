import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePendingQueue, type PendingComposerBridge } from './usePendingQueue'
import type { PendingAttachment, PendingItem } from './logic'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

// A mutable fake composer the bridge reads/writes, standing in for the real
// composer + attachments + intent that ChatPage owns.
function makeBridge(overrides: Partial<PendingComposerBridge> = {}) {
  const state = {
    text: '',
    attachments: [] as PendingAttachment[],
    intent: null as string | null,
    streaming: false,
    compacting: false,
    sent: [] as { text: string; attachments: PendingAttachment[]; intent: string | null }[],
  }
  const bridge: PendingComposerBridge = {
    getComposerText: () => state.text,
    setComposerText: (t) => {
      state.text = t
    },
    getAttachments: () => state.attachments,
    setAttachments: (a) => {
      state.attachments = a
    },
    getIntent: () => state.intent,
    setIntent: (i) => {
      state.intent = i
    },
    sendDrainedHead: (text, attachments, intent) => {
      state.sent.push({ text, attachments, intent })
    },
    isStreaming: () => state.streaming,
    isCompactInFlight: () => state.compacting,
    ...overrides,
  }
  return { bridge, state }
}

const item = (text: string): PendingItem => ({ text, attachments: [], intent: null })

describe('usePendingQueue', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('enqueues while busy and clears the composer (chat.js:8505-8531)', () => {
    const { bridge, state } = makeBridge()
    state.text = 'draft'
    const { result } = renderHook(() => usePendingQueue(bridge))
    let ok = false
    act(() => {
      ok = result.current.enqueue(item('queued'))
    })
    expect(ok).toBe(true)
    expect(result.current.length).toBe(1)
    // chat.js:8525-8528 — composer/attachments/intent cleared after enqueue.
    expect(state.text).toBe('')
  })

  it('rejects the 6th enqueue at MAX_PENDING (chat.js:8511)', () => {
    const { bridge } = makeBridge()
    const { result } = renderHook(() => usePendingQueue(bridge))
    for (let i = 0; i < 5; i++) act(() => void result.current.enqueue(item(`m${i}`)))
    let ok = true
    act(() => {
      ok = result.current.enqueue(item('overflow'))
    })
    expect(ok).toBe(false)
    expect(result.current.length).toBe(5)
  })

  it('pops all pending into the composer, FIFO-joined (chat.js:8596)', () => {
    const { bridge, state } = makeBridge()
    const { result } = renderHook(() => usePendingQueue(bridge))
    act(() => void result.current.enqueue(item('alpha')))
    act(() => void result.current.enqueue(item('beta')))
    let recovered = false
    act(() => {
      recovered = result.current.popAllIntoComposer()
    })
    expect(recovered).toBe(true)
    expect(result.current.length).toBe(0)
    expect(state.text).toBe('alpha\nbeta')
  })

  it('pops just the tail on Alt+↑ (chat.js:8560)', () => {
    const { bridge, state } = makeBridge()
    const { result } = renderHook(() => usePendingQueue(bridge))
    act(() => void result.current.enqueue(item('one')))
    act(() => void result.current.enqueue(item('two')))
    act(() => result.current.popTail())
    expect(result.current.length).toBe(1)
    expect(state.text).toBe('two')
  })

  it('drains the head after a terminal event when idle (chat.js:8644-8650)', () => {
    const { bridge, state } = makeBridge()
    const { result } = renderHook(() => usePendingQueue(bridge))
    act(() => void result.current.enqueue(item('first')))
    act(() => void result.current.enqueue(item('second')))
    act(() => {
      result.current.scheduleDrainAfterTerminal()
      vi.advanceTimersByTime(50)
    })
    // The head was sent; the tail remains queued.
    expect(state.sent.map((s) => s.text)).toEqual(['first'])
    expect(result.current.length).toBe(1)
  })

  it('does NOT drain when a stream resumed before the debounce fires (chat.js:8649)', () => {
    const { bridge, state } = makeBridge()
    const { result } = renderHook(() => usePendingQueue(bridge))
    act(() => void result.current.enqueue(item('queued')))
    act(() => {
      result.current.scheduleDrainAfterTerminal()
      // A new turn started in the 50ms window → the re-check bails.
      state.streaming = true
      vi.advanceTimersByTime(50)
    })
    expect(state.sent).toHaveLength(0)
    expect(result.current.length).toBe(1)
  })

  it('clearAll empties the queue and cancels the drain timer (chat.js:8466/8637)', () => {
    const { bridge, state } = makeBridge()
    const { result } = renderHook(() => usePendingQueue(bridge))
    act(() => void result.current.enqueue(item('q')))
    act(() => {
      result.current.scheduleDrainAfterTerminal()
      result.current.clearAll()
      vi.advanceTimersByTime(50)
    })
    expect(result.current.length).toBe(0)
    expect(state.sent).toHaveLength(0)
  })
})
