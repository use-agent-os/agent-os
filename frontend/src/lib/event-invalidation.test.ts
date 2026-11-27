import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createEventInvalidator } from './event-invalidation'

const KEY = ['trading', 'orders'] as const

/** One observed query whose reads are answered by hand. */
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const answers: Array<(value: number) => void> = []
  const queryFn = vi.fn(() => new Promise<number>((resolve) => answers.push(resolve)))
  const observer = new QueryObserver(client, { queryKey: KEY, queryFn })
  const unsubscribe = observer.subscribe(() => {})
  const answer = async (value: number) => {
    answers.shift()!(value)
    await vi.advanceTimersByTimeAsync(0)
  }
  return { client, queryFn, answer, observer, unsubscribe }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('createEventInvalidator', () => {
  it('refetches once per burst of events', async () => {
    const { client, queryFn, answer, unsubscribe } = setup()
    await answer(1)
    const invalidator = createEventInvalidator(client, ['trading'])
    invalidator.notify()
    invalidator.notify()
    invalidator.notify()
    expect(queryFn).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(150)
    expect(queryFn).toHaveBeenCalledTimes(2)
    unsubscribe()
  })

  // The gateway cannot take a request back: every read re-sent while one is
  // still queued there is one more slot of the connection's start queue.
  it('never sends a second read while one is in flight, however many events arrive', async () => {
    const { client, queryFn, answer, observer, unsubscribe } = setup()
    await answer(1)
    const invalidator = createEventInvalidator(client, ['trading'])
    invalidator.notify()
    await vi.advanceTimersByTimeAsync(150)
    expect(queryFn).toHaveBeenCalledTimes(2)

    // The engine keeps emitting while the gateway has not answered yet.
    for (let i = 0; i < 20; i += 1) {
      invalidator.notify()
      await vi.advanceTimersByTimeAsync(150)
    }
    expect(queryFn).toHaveBeenCalledTimes(2)

    // The read that was in flight may predate the events: one more, and only one.
    await answer(2)
    expect(queryFn).toHaveBeenCalledTimes(3)
    await answer(3)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(queryFn).toHaveBeenCalledTimes(3)
    expect(observer.getCurrentResult().data).toBe(3)
    unsubscribe()
  })

  it('does nothing after dispose, not even the re-read it owed', async () => {
    const { client, queryFn, answer, unsubscribe } = setup()
    await answer(1)
    const invalidator = createEventInvalidator(client, ['trading'])
    invalidator.notify()
    await vi.advanceTimersByTimeAsync(150)
    invalidator.notify()
    await vi.advanceTimersByTimeAsync(150)
    invalidator.notify()
    invalidator.dispose()
    await answer(2)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(queryFn).toHaveBeenCalledTimes(2)
    unsubscribe()
  })
})
