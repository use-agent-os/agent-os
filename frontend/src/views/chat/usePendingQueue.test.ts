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

  it('enqueues while busy and clears the composer', () => {
    const { bridge, state } = makeBridge()
    state.text = 'draft'
    const { result } = renderHook(() => usePendingQueue(bridge))
    let ok = false
    act(() => {
      ok = result.current.enqueue(item('queued'))
    })
    expect(ok).toBe(true)
    expect(result.current.length).toBe(1)
    // composer/attachments/intent cleared after enqueue.
    expect(state.text).toBe('')
  })

  it('rejects the 6th enqueue at MAX_PENDING', () => {
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

  it('pops all pending into the composer, FIFO-joined', () => {
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

  it('pops just the tail on Alt+↑', () => {
    const { bridge, state } = makeBridge()
    const { result } = renderHook(() => usePendingQueue(bridge))
    act(() => void result.current.enqueue(item('one')))
    act(() => void result.current.enqueue(item('two')))
    act(() => result.current.popTail())
    expect(result.current.length).toBe(1)
    expect(state.text).toBe('two')
  })

  it('drains the head after a terminal event when idle', () => {
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

  it('does NOT drain when a stream resumed before the debounce fires', () => {
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

  it('clearAll empties the queue and cancels the drain timer', () => {
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

  describe('per-session queues', () => {
    // The chat route element stays mounted across a session switch; the queue
    // must not follow the user into the next session.
    const render = (bridge: PendingComposerBridge) =>
      renderHook(({ sessionKey }) => usePendingQueue(bridge, sessionKey), {
        initialProps: { sessionKey: 'agent:main:a' },
      })

    it("does not drain one session's queue into another after a switch", () => {
      const { bridge, state } = makeBridge()
      const { result, rerender } = render(bridge)
      act(() => void result.current.enqueue(item('for A')))
      rerender({ sessionKey: 'agent:main:b' })
      expect(result.current.length).toBe(0)
      expect(result.current.queue).toEqual([])
      // B's subscribe replays its last `.done` → the transcript schedules a drain.
      act(() => {
        result.current.scheduleDrainAfterTerminal()
        vi.advanceTimersByTime(50)
      })
      expect(state.sent).toHaveLength(0)
    })

    it('does not fire a drain armed in the outgoing session', () => {
      const { bridge, state } = makeBridge()
      const { result, rerender } = render(bridge)
      act(() => void result.current.enqueue(item('for A')))
      act(() => result.current.scheduleDrainAfterTerminal())
      rerender({ sessionKey: 'agent:main:b' })
      act(() => void vi.advanceTimersByTime(50))
      expect(state.sent).toHaveLength(0)
    })

    it('keeps a queue for its session and drains it there on return', () => {
      const { bridge, state } = makeBridge()
      const { result, rerender } = render(bridge)
      act(() => void result.current.enqueue(item('for A')))
      rerender({ sessionKey: 'agent:main:b' })
      act(() => void result.current.enqueue(item('for B')))
      rerender({ sessionKey: 'agent:main:a' })
      expect(result.current.queue.map((q) => q.text)).toEqual(['for A'])
      act(() => {
        result.current.scheduleDrainAfterTerminal()
        vi.advanceTimersByTime(50)
      })
      expect(state.sent.map((s) => s.text)).toEqual(['for A'])
      rerender({ sessionKey: 'agent:main:b' })
      expect(result.current.queue.map((q) => q.text)).toEqual(['for B'])
    })

    it('scopes clear, pop and remove to the shown session', () => {
      const { bridge, state } = makeBridge()
      const { result, rerender } = render(bridge)
      act(() => void result.current.enqueue(item('a1')))
      act(() => void result.current.enqueue(item('a2')))
      rerender({ sessionKey: 'agent:main:b' })
      act(() => void result.current.enqueue(item('b1')))
      act(() => void result.current.enqueue(item('b2')))
      act(() => result.current.remove(0))
      expect(result.current.queue.map((q) => q.text)).toEqual(['b2'])
      act(() => void result.current.popAllIntoComposer())
      expect(state.text).toBe('b2')
      act(() => result.current.clearAll())
      rerender({ sessionKey: 'agent:main:a' })
      expect(result.current.queue.map((q) => q.text)).toEqual(['a1', 'a2'])
    })
  })
})
